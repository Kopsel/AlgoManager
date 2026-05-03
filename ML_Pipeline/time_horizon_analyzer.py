import MetaTrader5 as mt5
import pandas as pd
import json
import os
import numpy as np
import sqlite3
import matplotlib.pyplot as plt
from datetime import datetime, timedelta, timezone
import warnings

warnings.filterwarnings('ignore')

# ==========================================
# ⚙️ CONFIGURATION & PATHS
# ==========================================
script_dir = os.path.dirname(os.path.abspath(__file__))
base_dir = os.path.dirname(script_dir)
config_path = os.path.join(base_dir, 'system_config.json')
db_path = os.path.join(base_dir, 'trading_system.db')

try:
    with open(config_path, 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    print(f"❌ Could not find {config_path}.")
    exit(1)

# Parameters
SYMBOL = config['strategies']['QT_Velocity']['symbol']
TP_POINTS = config['strategies']['QT_Velocity']['trade_limits']['tp_points']
SPREAD_ALLOWANCE = config['ml_pipeline']['labeling']['spread_allowance']
UTC_OFFSET = config['system'].get('broker_utc_offset_hours', 3) 
MAX_TEST_HORIZON_MINUTES = 20 # We look up to 20 minutes out

SYMBOL_MAP = {"ES.M26": "US500"} 
mt5_symbol = SYMBOL_MAP.get(SYMBOL, SYMBOL)

def run_horizon_analysis():
    print("🔌 Connecting to MetaTrader 5...")
    if not mt5.initialize():
        print("❌ MT5 initialization failed")
        return

    print(f"📂 Fetching exact trade timestamps from Database...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT features_json FROM ml_features WHERE features_json IS NOT NULL")
    rows = cursor.fetchall()
    conn.close()

    winning_times = []
    total_events = 0

    print(f"⏳ Evaluating Time-to-Target for {len(rows)} bot events (Max Horizon: {MAX_TEST_HORIZON_MINUTES}m)...")
    
    for row in rows:
        try:
            data = json.loads(row[0])
            trade_utc_ms = data.get('timestamp', 0)
            speed_delta = data.get('trigger', {}).get('speed_delta', 0)
            
            if trade_utc_ms == 0: continue
            
            bot_direction = "BUY" if speed_delta < 0 else "SELL"
            
            trade_utc_dt = datetime.fromtimestamp(trade_utc_ms / 1000.0, tz=timezone.utc)
            broker_time_start = trade_utc_dt + timedelta(hours=UTC_OFFSET)
            broker_time_start_naive = broker_time_start.replace(tzinfo=None)
            broker_time_end_naive = (broker_time_start + timedelta(minutes=MAX_TEST_HORIZON_MINUTES)).replace(tzinfo=None)
            
            # Fetch the forward horizon
            rates = mt5.copy_rates_range(mt5_symbol, mt5.TIMEFRAME_M1, broker_time_start_naive, broker_time_end_naive)
            if rates is None or len(rates) == 0: continue
            
            df_rates = pd.DataFrame(rates)
            df_rates['time'] = pd.to_datetime(df_rates['time'], unit='s')
            total_events += 1
            
            # Find Exact Entry
            exact_entry_price = df_rates.iloc[0]['open']
            ticks = mt5.copy_ticks_from(mt5_symbol, broker_time_start_naive, mt5.COPY_TICKS_ALL, 1)
            if ticks is not None and len(ticks) > 0:
                exact_entry_price = (ticks[0]['bid'] + ticks[0]['ask']) / 2.0
            
            # Step through each minute to find EXACTLY when it hit the target
            target_hit = False
            for index, bar in df_rates.iterrows():
                minutes_elapsed = (bar['time'] - df_rates.iloc[0]['time']).total_seconds() / 60.0
                
                if bot_direction == "BUY":
                    if bar['high'] >= exact_entry_price + TP_POINTS + SPREAD_ALLOWANCE:
                        winning_times.append(minutes_elapsed)
                        target_hit = True
                        break
                elif bot_direction == "SELL":
                    if bar['low'] <= exact_entry_price - TP_POINTS - SPREAD_ALLOWANCE:
                        winning_times.append(minutes_elapsed)
                        target_hit = True
                        break
                        
        except Exception as e:
            continue

    mt5.shutdown()

    if not winning_times:
        print("❌ No winning trades found to analyze.")
        return

    # --- STATISTICAL ANALYSIS & PLOTTING ---
    times_array = np.array(winning_times)
    
    # We round up to the nearest minute for the histogram bins
    times_array_ceil = np.ceil(times_array)
    
    p50 = np.percentile(times_array, 50)
    p80 = np.percentile(times_array, 80)
    p90 = np.percentile(times_array, 90)
    p95 = np.percentile(times_array, 95)

    print("\n" + "="*50)
    print("⏱️ TIME-TO-TARGET DECAY ANALYSIS")
    print("="*50)
    print(f"Total Scalp Events Analyzed: {total_events}")
    print(f"Total Successful 1.0pt Hits: {len(winning_times)}")
    print("-" * 50)
    print(f"50% of wins happen within:  {p50:.1f} minutes")
    print(f"80% of wins happen within:  {p80:.1f} minutes")
    print(f"90% of wins happen within:  {p90:.1f} minutes  <-- (Optimal Grid Cutoff)")
    print(f"95% of wins happen within:  {p95:.1f} minutes")
    print("="*50 + "\n")

    # Matplotlib Visualization
    plt.figure(figsize=(12, 6))
    bins = np.arange(0, MAX_TEST_HORIZON_MINUTES + 1, 1)
    n, bins, patches = plt.hist(times_array, bins=bins, color='royalblue', edgecolor='black', alpha=0.7)
    
    # Highlight the recommended cutoff zone
    plt.axvline(x=p90, color='red', linestyle='dashed', linewidth=2, label=f'90th Percentile Cutoff ({p90:.1f} min)')
    
    plt.title("Time-in-Trade Distribution for Successful 1.0pt Scalps", fontsize=14, fontweight='bold')
    plt.xlabel("Minutes Until Take Profit Hit", fontsize=12)
    plt.ylabel("Number of Trades", fontsize=12)
    plt.xticks(bins)
    plt.grid(axis='y', alpha=0.3)
    plt.legend()
    
    # Shade the "Dead Zone" (Grid Territory)
    plt.axvspan(p90, MAX_TEST_HORIZON_MINUTES, color='red', alpha=0.1)
    plt.text(p90 + 0.5, max(n)*0.5, "Convert to Grid\n(Alpha has decayed)", color='darkred', fontsize=11)
    
    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    run_horizon_analysis()