import os
import json
import zmq
import torch
import torch.nn as nn
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import io
import MetaTrader5 as mt5
from PIL import Image
from transformers import AutoProcessor, SiglipVisionModel

# --- CONFIG ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "system_config.json")

with open(CONFIG_FILE, "r") as f:
    config = json.load(f)

REGIME_PORT = config['system'].get('zmq_regime_port', 5557)
SYMBOL = config['strategies']['QT_Velocity']['symbol']
VT_CONFIG = config['ml_pipeline']['vision_transformer']
WINDOW_SIZE = VT_CONFIG['data_generation']['window_size_minutes']
MODEL_PATH = os.path.join(BASE_DIR, VT_CONFIG['model_save_path'])

# --- MODEL ARCHITECTURE ---
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
    """Draws the exact training chart geometry entirely in RAM."""
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
    
    # Volume Profile
    hist, bins = np.histogram(df['close'], bins=40, weights=df['tick_volume'])
    hist_scaled = (hist / hist.max()) * (WINDOW_SIZE * 0.3)
    ax.barh(bins[:-1], hist_scaled, height=(bins[1]-bins[0])*0.9, color='white', alpha=0.15, align='center', zorder=1)

    # Candles
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

def run_regime_server():
    print("🔭 Starting SigLIP Regime Watchtower...")
    
    # 1. Connect to MT5 (Same logic as Trade_Manager)
    mt5_path = config['system'].get('mt5_terminal_path')
    if mt5_path and os.path.exists(mt5_path):
        mt5.initialize(path=mt5_path)
    else:
        mt5.initialize()
    print("✅ Watchtower connected to MT5 Data Feed.")

    # 2. Load Model
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    processor = AutoProcessor.from_pretrained("google/siglip-base-patch16-224")
    classes_dict = VT_CONFIG['classes']
    
    model = SiglipClassifier(num_classes=len(classes_dict))
    model.load_state_dict(torch.load(MODEL_PATH, map_location=device, weights_only=True))
    model.to(device)
    model.eval()

    # 3. Setup ZMQ
    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.bind(f"tcp://*:{REGIME_PORT}")
    print(f"📡 Watchtower listening for Quantower on Port {REGIME_PORT}...")

    try:
        while True:
            message = socket.recv_string() # Wait for Quantower to ping
            
            try:
                # Instantly pull perfect MT5 data to avoid Train-Serve skew
                rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1, 0, WINDOW_SIZE)
                if rates is None or len(rates) < WINDOW_SIZE:
                    socket.send_json({"error": "Insufficient MT5 Data"})
                    continue
                
                df = pd.DataFrame(rates)
                
                # Draw & Predict
                img = draw_chart_to_memory(df)
                inputs = processor(images=img, return_tensors="pt")
                pixel_values = inputs['pixel_values'].to(device)
                
                with torch.no_grad():
                    outputs = model(pixel_values)
                    _, predicted = torch.max(outputs.data, 1)
                    
                pred_idx = predicted.item()
                regime_name = classes_dict[str(pred_idx)]
                
                print(f"👁️ Evaluated Window. Regime: {pred_idx} ({regime_name})")
                socket.send_json({"signal": pred_idx, "regime": regime_name, "status": "success"})

            except Exception as e:
                socket.send_json({"status": "error", "message": str(e)})

    except KeyboardInterrupt:
        print("\nShutting down Watchtower.")
    finally:
        socket.close()
        context.term()
        mt5.shutdown()

if __name__ == "__main__":
    run_regime_server()