import MetaTrader5 as mt5
import pandas as pd
import random
import json
import os
import numpy as np
import sqlite3
from datetime import datetime, timedelta, timezone

# ==========================================
# ⚙️ LOAD CENTRAL CONFIGURATION
# ==========================================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'system_config.json')

try:
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    print(f"❌ Could not find {CONFIG_PATH}.")
    exit(1)

DB_PATH = os.path.join(BASE_DIR, config['system']['db_path'])
UTC_OFFSET = config['system'].get('broker_utc_offset_hours', 3) 
SYMBOL = config['strategies']['QT_Velocity']['symbol']
TP_POINTS = config['strategies']['QT_Velocity']['trade_limits']['tp_points']
HORIZON_MINUTES = config['ml_pipeline']['labeling']['horizon_minutes']
SPREAD_ALLOWANCE = config['ml_pipeline']['labeling']['spread_allowance']

SYMBOL_MAP = {"ES.M26": "US500"} 
mt5_symbol = SYMBOL_MAP.get(SYMBOL, SYMBOL)

NUM_ITERATIONS = 100

def run_event_monte_carlo():
    print(f"🔌 Connecting to MetaTrader 5...")
    if not mt5.initialize():
        print("❌ MT5 initialization failed")
        return

    print(f"📂 Fetching exact trade timestamps from Database...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # We only pull trades where features exist 
    cursor.execute("SELECT features_json FROM ml_features WHERE features_json IS NOT NULL")
    rows = cursor.fetchall()
    
    trade_events = []
    actual_bot_wins = 0
    total_valid_events = 0
    
    # 1. Pre-fetch the exact TICK data and Evaluate the Bot's Actual Trade
    print(f"⏳ Pre-fetching Tick data and evaluating {len(rows)} actual bot trades...")
    for row in rows:
        try:
            data = json.loads(row[0])
            trade_utc_ms = data.get('timestamp', 0)
            speed_delta = data.get('trigger', {}).get('speed_delta', 0)
            
            if trade_utc_ms == 0:
                continue
                
            # Infer direction: Negative velocity = BUY, Positive velocity = SELL
            bot_direction = "BUY" if speed_delta < 0 else "SELL"
                
            trade_utc_dt = datetime.fromtimestamp(trade_utc_ms / 1000.0, tz=timezone.utc)
            broker_time_start = trade_utc_dt + timedelta(hours=UTC_OFFSET)
            
            # MT5's tick API prefers naive datetimes representing local broker time
            broker_time_start_naive = broker_time_start.replace(tzinfo=None)
            broker_time_end_naive = (broker_time_start + timedelta(minutes=HORIZON_MINUTES + 1)).replace(tzinfo=None)
            
            # --- STEP A: Fetch the 10-minute forward horizon first ---
            rates = mt5.copy_rates_range(mt5_symbol, mt5.TIMEFRAME_M1, broker_time_start_naive, broker_time_end_naive)
            if rates is None or len(rates) == 0:
                continue # If we don't even have M1 data, the event is untestable
                
            df_rates = pd.DataFrame(rates)
            total_valid_events += 1
            
            # --- STEP B: Try for Tick Precision, Fallback to M1 Open ---
            exact_entry_price = None
            ticks = mt5.copy_ticks_from(mt5_symbol, broker_time_start_naive, mt5.COPY_TICKS_ALL, 1)
            
            if ticks is not None and len(ticks) > 0:
                # Precision entry: We found the exact tick!
                exact_entry_price = (ticks[0]['bid'] + ticks[0]['ask']) / 2.0
            else:
                # Fallback entry: Broker purged the tick data. Use M1 open.
                exact_entry_price = df_rates.iloc[0]['open']
                
            # --- GRADE THE ACTUAL BOT TRADE ---
            bot_won = False
            if bot_direction == "BUY":
                if (df_rates['high'] >= exact_entry_price + TP_POINTS + SPREAD_ALLOWANCE).any():
                    bot_won = True
            elif bot_direction == "SELL":
                if (df_rates['low'] <= exact_entry_price - TP_POINTS - SPREAD_ALLOWANCE).any():
                    bot_won = True
                    
            if bot_won:
                actual_bot_wins += 1
            
            # Save event for the Monte Carlo coin-flip baseline
            trade_events.append({
                'timestamp': broker_time_start_naive,
                'open_price': exact_entry_price,
                'horizon_df': df_rates
            })
        except Exception as e:
            # Silently skip malformed rows
            pass
            
    conn.close()
    
    if total_valid_events == 0:
        print("❌ No valid market data found for the DB timestamps.")
        mt5.shutdown()
        return

    actual_bot_win_rate = (actual_bot_wins / total_valid_events) * 100

    print(f"✅ Successfully loaded and graded {total_valid_events} actual trades.")
    print(f"📈 Running Monte Carlo Convergence Test on {mt5_symbol}...")
    print(f"   Simulating {NUM_ITERATIONS} batches of {total_valid_events} coin-flip trades...\n")

    batch_win_rates = []

    # 2. Run the Monte Carlo Simulation (The Coin Flip)
    for i in range(NUM_ITERATIONS):
        mc_wins = 0
        
        for event in trade_events:
            action = random.choice(["BUY", "SELL"])
            open_price = event['open_price']
            horizon_df = event['horizon_df']
            
            is_win = False
            if action == "BUY":
                if (horizon_df['high'] >= open_price + TP_POINTS + SPREAD_ALLOWANCE).any():
                    is_win = True
            elif action == "SELL":
                if (horizon_df['low'] <= open_price - TP_POINTS - SPREAD_ALLOWANCE).any():
                    is_win = True
            
            if is_win: 
                mc_wins += 1

        win_rate = (mc_wins / total_valid_events) * 100
        batch_win_rates.append(win_rate)
        
        if (i + 1) % 10 == 0 or i == 0:
            print(f"   Batch {i+1}/{NUM_ITERATIONS}: {win_rate:.2f}% Win Rate")

    final_average = np.mean(batch_win_rates)
    
    print("\n========================================")
    print("🎯 FINAL SYSTEM EDGE ANALYSIS")
    print("========================================")
    print(f"Total Events Analyzed: {total_valid_events}")
    print(f"Actual Bot Win Rate:     {actual_bot_win_rate:.2f}%")
    print(f"Random Coin-Flip Rate:   {final_average:.2f}%")
    
    edge = actual_bot_win_rate - final_average
    
    if edge > 0:
        print(f"True Mathematical Edge:  +{edge:.2f}% ✅")
        print("Conclusion: Your velocity triggers provide a genuine statistical advantage.")
    else:
        print(f"True Mathematical Edge:  {edge:.2f}% ❌")
        print("Conclusion: The market geometry is carrying the strategy. The AI needs better features.")
    print("========================================\n")

    mt5.shutdown()

if __name__ == "__main__":
    run_event_monte_carlo()