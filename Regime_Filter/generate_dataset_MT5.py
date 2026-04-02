import pandas as pd
import numpy as np
import os
import matplotlib.pyplot as plt
import json

# --- Config Initialization ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Assumes config.json is in the root (one folder up from the script, or adjust as needed)
ROOT_DIR = os.path.dirname(BASE_DIR) if "Regime_Filter" in BASE_DIR else BASE_DIR
CONFIG_PATH = os.path.join(ROOT_DIR, "system_config.json")

with open(CONFIG_PATH, 'r') as f:
    config = json.load(f)

vt_config = config['ml_pipeline']['vision_transformer']
gen_config = vt_config['data_generation']
algo_config = vt_config['algorithmic_labeling']
classes_dict = vt_config['classes']

WINDOW_SIZE = gen_config['window_size_minutes']
STEP_SIZE = gen_config['generation_interval_minutes']
RESOLUTION = gen_config['canvas_resolution']
FIG_INCHES = RESOLUTION / 100.0

FORWARD_HORIZON = algo_config['forward_horizon_minutes']
TARGET_MULT = algo_config['target_atr_multiplier']
PAIN_MULT = algo_config['pain_atr_multiplier']

CSV_FILE = os.path.join(BASE_DIR, "US500_Q1_2026.csv")  
DATASET_DIR = os.path.join(BASE_DIR, "dataset")

# Create specific class folders automatically
for class_name in classes_dict.values():
    os.makedirs(os.path.join(DATASET_DIR, class_name), exist_ok=True)

def generate_and_label():
    print(f"📥 Loading MT5 data from {CSV_FILE}...")
    try:
        df = pd.read_csv(CSV_FILE, sep='\t')
        if len(df.columns) == 1:
            df = pd.read_csv(CSV_FILE, sep=',')
    except FileNotFoundError:
        print(f"❌ Error: Could not find {CSV_FILE}.")
        return
    
    df.columns = [col.strip('<>').upper() for col in df.columns]
    df['Time'] = pd.to_datetime(df['DATE'] + ' ' + df['TIME'], format='%Y.%m.%d %H:%M:%S')
    df.set_index('Time', inplace=True)
    df.sort_index(inplace=True)

    df.rename(columns={'TICKVOL': 'Volume'}, inplace=True)
    cols_to_keep = ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'Volume']
    df = df[cols_to_keep]
    df.rename(columns=lambda x: x.capitalize(), inplace=True)

    if df.empty: return

    # Session/Anchors
    df['Session'] = df.index.date
    df['Daily_Open'] = df.groupby('Session')['Open'].transform('first')
    df['Typical_Price'] = (df['High'] + df['Low'] + df['Close']) / 3
    df['PV'] = df['Typical_Price'] * df['Volume']
    df['Cum_Vol'] = df.groupby('Session')['Volume'].cumsum()
    df['Cum_PV'] = df.groupby('Session')['PV'].cumsum()
    df['VWAP'] = df['Cum_PV'] / df['Cum_Vol']

    session_changes = df['Session'] != df['Session'].shift(1)
    df.loc[session_changes, ['VWAP', 'Daily_Open']] = np.nan

    # Calculate True Range for ATR math
    df['Prev_Close'] = df['Close'].shift(1)
    df['TR'] = df[['High', 'Prev_Close']].max(axis=1) - df[['Low', 'Prev_Close']].min(axis=1)

    print(f"⚙️ Generating & Auto-Labeling at {STEP_SIZE}-min steps...")
    
    counts = {v: 0 for v in classes_dict.values()}
    total_generated = 0
    
    # We must stop early enough so the forward window doesn't go out of bounds
    for i in range(WINDOW_SIZE, len(df) - FORWARD_HORIZON, STEP_SIZE):
        window_df = df.iloc[i-WINDOW_SIZE : i].copy()
        
        # 1. Weekend Gap Filter (Skip plotting garbage)
        if window_df.index[-1] - window_df.index[0] > pd.Timedelta(hours=2):
            continue 
            
        # 2. ALGORITHMIC LABELING LOGIC (Dynamic Volatility)
        forward_df = df.iloc[i : i+FORWARD_HORIZON]
        base_price = window_df['Close'].iloc[-1]
        
        # Calculate ATR based strictly on the image's 90-minute window
        current_atr = window_df['TR'].mean()
        # Fallback if ATR is exactly 0 (market closed)
        if current_atr == 0: current_atr = 0.25 
        
        target_pts = current_atr * TARGET_MULT
        pain_pts = current_atr * PAIN_MULT
        
        # Simulate chronology: walk forward to see what hits first
        final_label = classes_dict["2"] # Default to chop
        
        for _, f_row in forward_df.iterrows():
            high, low = f_row['High'], f_row['Low']
            
            hit_long_target = high >= (base_price + target_pts)
            hit_long_pain = low <= (base_price - pain_pts)
            
            hit_short_target = low <= (base_price - target_pts)
            hit_short_pain = high >= (base_price + pain_pts)

            if hit_long_target and hit_long_pain: hit_long_target = False
            if hit_short_target and hit_short_pain: hit_short_target = False

            if hit_long_target and not hit_long_pain:
                final_label = classes_dict["0"]
                break
            if hit_short_target and not hit_short_pain:
                final_label = classes_dict["1"]
                break
            if hit_long_pain and hit_short_pain:
                break # It whipsawed our pain threshold in both directions: Pure Chop.

        # 3. DRAW AND SAVE THE IMAGE IN THE CORRECT FOLDER
        y_min = window_df['Low'].min()
        y_max = window_df['High'].max()
        y_padding = (y_max - y_min) * 0.02
        y_min -= y_padding
        y_max += y_padding

        fig = plt.figure(figsize=(FIG_INCHES, FIG_INCHES), dpi=100, facecolor='black')
        ax = fig.add_axes([0, 0, 1, 1]) 
        ax.set_facecolor('black')
        ax.set_xlim(-1, WINDOW_SIZE)
        ax.set_ylim(y_min, y_max)
        ax.axis('off') 

        x_coords = np.arange(len(window_df))

        hist, bins = np.histogram(window_df['Close'], bins=40, weights=window_df['Volume'])
        hist_scaled = (hist / hist.max()) * (WINDOW_SIZE * 0.3)
        ax.barh(bins[:-1], hist_scaled, height=(bins[1]-bins[0])*0.9, color='white', alpha=0.15, align='center', zorder=1)

        ax.plot(x_coords, window_df['VWAP'], color='yellow', linewidth=2.5, zorder=2)
        ax.plot(x_coords, window_df['Daily_Open'], color='dodgerblue', linewidth=2.5, zorder=2)

        colors = np.where(window_df['Close'] >= window_df['Open'], 'lime', 'red')
        
        ax.vlines(x_coords, ymin=window_df['Low'], ymax=window_df['High'], color=colors, linewidth=2.0, zorder=3)
        
        body_min = np.minimum(window_df['Open'], window_df['Close'])
        body_max = np.maximum(window_df['Open'], window_df['Close'])
        doji_mask = body_min == body_max
        body_max[doji_mask] += (y_max - y_min) * 0.0005 

        ax.vlines(x_coords, ymin=body_min, ymax=body_max, color=colors, linewidth=8.0, zorder=3)

        timestamp_str = window_df.index[-1].strftime("%Y%m%d_%H%M")
        # Save directly to the algorithmic class folder!
        save_path = os.path.join(DATASET_DIR, final_label, f"siglip_{timestamp_str}.png")
        
        fig.savefig(save_path, dpi=100, facecolor='black', edgecolor='none')
        plt.close(fig)
                 
        counts[final_label] += 1
        total_generated += 1
        if total_generated % 50 == 0:
            print(f"   ⏳ Generated {total_generated} images. Distribution: {counts}")

    print(f"\n✅ COMPLETE! Total images: {total_generated}. Final Distribution: {counts}")

if __name__ == "__main__":
    generate_and_label()