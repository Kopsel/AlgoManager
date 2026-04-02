import sqlite3
import pandas as pd
import MetaTrader5 as mt5
import json
import os
import numpy as np
import torch
import torch.nn as nn
import matplotlib.pyplot as plt
import io
from PIL import Image
from transformers import AutoProcessor, SiglipVisionModel

# ==========================================
# ⚙️ LOAD GLOBAL CONFIGURATION
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'system_config.json')

with open(CONFIG_PATH, 'r') as f:
    config = json.load(f)

DB_PATH = os.path.join(BASE_DIR, config['system']['db_path'])
JSON_COL = config['ml_pipeline']['alpha_filter']['json_column_name']
BROKER_OFFSET_HOURS = config['system'].get('broker_utc_offset_hours', 3)

VT_CONFIG = config['ml_pipeline']['vision_transformer']
WINDOW_SIZE = VT_CONFIG['data_generation']['window_size_minutes']
MODEL_PATH = os.path.join(BASE_DIR, VT_CONFIG['model_save_path'])

SYMBOL_MAP = {"ES.M26": "US500"}

# ==========================================
# 🧠 REBUILD SIGLIP ARCHITECTURE FOR INFERENCE
# ==========================================
class SiglipClassifier(nn.Module):
    def __init__(self, num_classes):
        super().__init__()
        self.vision_model = SiglipVisionModel.from_pretrained("google/siglip-base-patch16-224")
        self.classifier = nn.Sequential(
            nn.Dropout(0.3),  
            nn.Linear(self.vision_model.config.hidden_size, num_classes)
        )

    def forward(self, pixel_values):
        outputs = self.vision_model(pixel_values=pixel_values)
        return self.classifier(outputs.pooler_output)

def draw_chart_to_memory(df):
    """Draws the exact training chart geometry entirely in RAM, including VWAP & Daily Open."""
    fig_inches = VT_CONFIG['data_generation']['canvas_resolution'] / 100.0
    y_min, y_max = df['low'].min(), df['high'].max()
    y_padding = (y_max - y_min) * 0.02
    
    fig = plt.figure(figsize=(fig_inches, fig_inches), dpi=100, facecolor='black')
    ax = fig.add_axes([0, 0, 1, 1]) 
    ax.set_facecolor('black')
    ax.set_xlim(-1, WINDOW_SIZE)
    ax.set_ylim(y_min - y_padding, y_max + y_padding)
    ax.axis('off') 

    x_coords = np.arange(len(df))
    
    # 1. Volume Profile
    hist, bins = np.histogram(df['close'], bins=40, weights=df['tick_volume'])
    hist_scaled = (hist / hist.max()) * (WINDOW_SIZE * 0.3)
    ax.barh(bins[:-1], hist_scaled, height=(bins[1]-bins[0])*0.9, color='white', alpha=0.15, align='center', zorder=1)

    # 2. Daily Open & VWAP Lines
    if 'VWAP' in df.columns:
        ax.plot(x_coords, df['VWAP'], color='yellow', linewidth=2.5, zorder=2)
    if 'Daily_Open' in df.columns:
        ax.plot(x_coords, df['Daily_Open'], color='dodgerblue', linewidth=2.5, zorder=2)

    # 3. Candles
    colors = np.where(df['close'] >= df['open'], 'lime', 'red')
    ax.vlines(x_coords, ymin=df['low'], ymax=df['high'], color=colors, linewidth=2.0, zorder=3)
    
    body_min = np.minimum(df['open'], df['close'])
    body_max = np.maximum(df['open'], df['close'])
    doji_mask = body_min == body_max
    body_max[doji_mask] += (y_max - y_min) * 0.0005 
    ax.vlines(x_coords, ymin=body_min, ymax=body_max, color=colors, linewidth=8.0, zorder=3)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, facecolor='black', edgecolor='none')
    plt.close(fig)
    buf.seek(0)
    
    return Image.open(buf).convert("RGB")

def backfill_regimes():
    print("🔌 Connecting to MetaTrader 5 for Historical Vision Backfilling...")
    mt5_path = config['system'].get('mt5_terminal_path')
    if mt5_path and os.path.exists(mt5_path):
        mt5.initialize(path=mt5_path)
    else:
        mt5.initialize()

    print("🧠 Loading SigLIP Watchtower Weights into VRAM...")
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    processor = AutoProcessor.from_pretrained("google/siglip-base-patch16-224")
    classes_dict = VT_CONFIG['classes']
    
    model = SiglipClassifier(num_classes=len(classes_dict))
    model.load_state_dict(torch.load(MODEL_PATH, map_location=device, weights_only=True))
    model.to(device)
    model.eval()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    query = f"SELECT id, timestamp, symbol, {JSON_COL} FROM ml_features"
    df_features = pd.read_sql(query, conn)
    print(f"🔍 Found {len(df_features)} total ML snapshots to verify/overwrite...")

    updated_count = 0

    for index, row in df_features.iterrows():
        try:
            data = json.loads(row[JSON_COL])
            qt_symbol = row['symbol']
            symbol = SYMBOL_MAP.get(qt_symbol, qt_symbol)
            
            # 1. TIME CONVERSION
            timestamp_ms = row['timestamp']
            utc_timestamp_sec = int(timestamp_ms / 1000)
            broker_timestamp = utc_timestamp_sec + (BROKER_OFFSET_HOURS * 3600)

            # 2. FETCH 24 HOURS OF HISTORICAL CHART DATA TO CALCULATE PROPER VWAP
            rates_24h = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_M1, broker_timestamp, 1440)
            if rates_24h is None or len(rates_24h) < WINDOW_SIZE:
                print(f"⚠️ Not enough 24h MT5 data for ID {row['id']}. Skipping.")
                continue
            
            df_24h = pd.DataFrame(rates_24h)
            
            # 3. RECONSTRUCT THE INDICATORS
            df_24h['date'] = pd.to_datetime(df_24h['time'], unit='s').dt.date
            daily_open_map = df_24h.groupby('date')['open'].first()
            df_24h['Daily_Open'] = df_24h['date'].map(daily_open_map)

            df_24h['typical_price'] = (df_24h['high'] + df_24h['low'] + df_24h['close']) / 3
            df_24h['vol_price'] = df_24h['typical_price'] * df_24h['tick_volume']
            df_24h['VWAP'] = df_24h.groupby('date').apply(
                lambda x: x['vol_price'].cumsum() / x['tick_volume'].cumsum()
            ).reset_index(level=0, drop=True)

            # 4. SLICE THE EXACT 90-MINUTE WINDOW FOR THE IMAGE
            df_rates = df_24h.tail(WINDOW_SIZE).reset_index(drop=True)

            # 5. DRAW AND PREDICT
            img = draw_chart_to_memory(df_rates)
            inputs = processor(images=img, return_tensors="pt")
            pixel_values = inputs['pixel_values'].to(device)
            
            with torch.no_grad():
                outputs = model(pixel_values)
                _, predicted = torch.max(outputs.data, 1)
                
            pred_idx = predicted.item()

            # 6. INJECT (OR OVERWRITE) THE REGIME INTO THE JSON
            data['context']['macro_regime_state'] = pred_idx

            # 7. UPDATE THE DATABASE
            new_json_string = json.dumps(data)
            cursor.execute(f"UPDATE ml_features SET {JSON_COL} = ? WHERE id = ?", (new_json_string, row['id']))
            updated_count += 1

            if updated_count % 50 == 0:
                print(f"🔄 Processed & Overwritten {updated_count} images...")
                conn.commit()

        except Exception as e:
            print(f"⚠️ Error processing row {row['id']}: {e}")
            continue

    conn.commit()
    conn.close()
    mt5.shutdown()

    print(f"✅ BATCH BACKFILL COMPLETE. AI Watchtower successfully reapplied to {updated_count} trades!")

if __name__ == "__main__":
    backfill_regimes()