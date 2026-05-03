import MetaTrader5 as mt5
import pandas as pd
import random
import json
import os
import numpy as np
import sqlite3
import time
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

SYMBOL_MAP = config['system'].get('symbol_mapping', {})
mt5_symbol = SYMBOL_MAP.get(SYMBOL, SYMBOL)

MC_ITERATIONS = 500  # For the fast memory-based directional test

def run_deep_edge_analysis():
    print("========================================")
    print("🔬 ADVANCED STATISTICAL EDGE ANALYSIS")
    print("========================================\n")
    
    print(f"🔌 Connecting to MetaTrader 5...")
    if not mt5.initialize():
        print("❌ MT5 initialization failed")
        return

    print(f"📂 Fetching exact trade signals from Database...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT features_json FROM ml_features WHERE features_json IS NOT NULL")
    rows = cursor.fetchall()
    conn.close()
    
    valid_events = []
    
    mech_wins = 0
    mech_total = 0
    
    ai_wins = 0
    ai_total = 0
    
    min_timestamp = None
    max_timestamp = None

    print(f"⏳ Pre-fetching Tick data and grading {len(rows)} actual signals...")
    
    for row in rows:
        try:
            data = json.loads(row[0])
            trade_utc_ms = data.get('timestamp', 0)
            speed_delta = data.get('trigger', {}).get('speed_delta', 0)
            
            # Identify if AI blocked it. Older rows might not have this, so default to False for raw testing
            ai_decision = data.get('ai_decision', {})
            is_blocked = ai_decision.get('blocked', False)
            
            if trade_utc_ms == 0: continue
            
            bot_direction = "BUY" if speed_delta < 0 else "SELL"
            
            trade_utc_dt = datetime.fromtimestamp(trade_utc_ms / 1000.0, tz=timezone.utc)
            broker_time_start = trade_utc_dt + timedelta(hours=UTC_OFFSET)
            
            broker_time_start_naive = broker_time_start.replace(tzinfo=None)
            broker_time_end_naive = (broker_time_start + timedelta(minutes=HORIZON_MINUTES + 1)).replace(tzinfo=None)
            
            # Track time range for the Timing Monte Carlo later
            if min_timestamp is None or broker_time_start_naive < min_timestamp: min_timestamp = broker_time_start_naive
            if max_timestamp is None or broker_time_start_naive > max_timestamp: max_timestamp = broker_time_start_naive
            
            # Fetch 1-Minute Horizon Candles
            rates = mt5.copy_rates_range(mt5_symbol, mt5.TIMEFRAME_M1, broker_time_start_naive, broker_time_end_naive)
            if rates is None or len(rates) == 0: continue 
                
            df_rates = pd.DataFrame(rates)
            
            # --- THE CORRECT MID-PRICE FIX (Ask/Bid Precision) ---
            exact_bid = None
            exact_ask = None
            ticks = mt5.copy_ticks_from(mt5_symbol, broker_time_start_naive, mt5.COPY_TICKS_ALL, 1)
            
            if ticks is not None and len(ticks) > 0:
                exact_bid = ticks[0]['bid']
                exact_ask = ticks[0]['ask']
            else:
                exact_bid = df_rates.iloc[0]['open']
                exact_ask = exact_bid + SPREAD_ALLOWANCE
                
            # --- STRICT ASYMMETRIC GRADING ---
            bot_won = False
            if bot_direction == "BUY":
                if (df_rates['high'] >= exact_ask + TP_POINTS).any(): bot_won = True
            elif bot_direction == "SELL":
                if (df_rates['low'] <= exact_bid - TP_POINTS - SPREAD_ALLOWANCE).any(): bot_won = True
                    
            # Update Mechanical Stats (Everything)
            mech_total += 1
            if bot_won: mech_wins += 1
            
            # Update AI Stats (Only Unblocked)
            if not is_blocked:
                ai_total += 1
                if bot_won: ai_wins += 1
                
            # Save for Monte Carlo
            valid_events.append({
                'timestamp': broker_time_start_naive,
                'exact_bid': exact_bid,
                'exact_ask': exact_ask,
                'direction': bot_direction,
                'horizon_df': df_rates
            })
            
        except Exception:
            pass

    if mech_total == 0:
        print("❌ No valid market data found.")
        mt5.shutdown()
        return

    mech_win_rate = (mech_wins / mech_total) * 100
    ai_win_rate = (ai_wins / ai_total) * 100 if ai_total > 0 else 0.0

    print(f"✅ Graded {mech_total} total signals ({ai_total} approved by AI).")
    
    # ==========================================
    # 🎲 TEST A: DIRECTIONAL MONTE CARLO
    # ==========================================
    print(f"\n📉 Running Directional Test (Shuffling signals to remove Trend Drift bias)...")
    
    # We maintain the EXACT Long/Short ratio of your bot to prevent fake edges
    actual_directions = [e['direction'] for e in valid_events]
    dir_mc_win_rates = []

    for i in range(MC_ITERATIONS):
        shuffled_directions = actual_directions.copy()
        random.shuffle(shuffled_directions) # Break the correlation between timestamp and direction
        
        mc_wins = 0
        for idx, event in enumerate(valid_events):
            action = shuffled_directions[idx]
            horizon_df = event['horizon_df']
            
            is_win = False
            if action == "BUY":
                if (horizon_df['high'] >= event['exact_ask'] + TP_POINTS).any(): is_win = True
            elif action == "SELL":
                if (horizon_df['low'] <= event['exact_bid'] - TP_POINTS - SPREAD_ALLOWANCE).any(): is_win = True
            
            if is_win: mc_wins += 1

        dir_mc_win_rates.append((mc_wins / mech_total) * 100)
        
    avg_directional_random = np.mean(dir_mc_win_rates)
    
    # ==========================================
    # ⏱️ TEST B: TIMING MONTE CARLO
    # ==========================================
    print(f"⏱️ Running Timing Test (Fetching random timestamps to prove temporal edge)...")
    
    time_mc_wins = 0
    time_mc_total = 0
    
    time_span_seconds = int((max_timestamp - min_timestamp).total_seconds())
    
    # We test 1 full batch of random timestamps equal to the size of your dataset
    for event in valid_events:
        # Pick a completely random time within the dataset's history
        random_offset = random.randint(0, time_span_seconds)
        rand_time = min_timestamp + timedelta(seconds=random_offset)
        end_time = rand_time + timedelta(minutes=HORIZON_MINUTES + 1)
        
        rates = mt5.copy_rates_range(mt5_symbol, mt5.TIMEFRAME_M1, rand_time, end_time)
        if rates is None or len(rates) == 0: continue
            
        df_rates = pd.DataFrame(rates)
        exact_bid = df_rates.iloc[0]['open']
        exact_ask = exact_bid + SPREAD_ALLOWANCE
        action = event['direction'] # Keep the bot's direction, just test a new time
        
        is_win = False
        if action == "BUY":
            if (df_rates['high'] >= exact_ask + TP_POINTS).any(): is_win = True
        elif action == "SELL":
            if (df_rates['low'] <= exact_bid - TP_POINTS - SPREAD_ALLOWANCE).any(): is_win = True
                
        time_mc_total += 1
        if is_win: time_mc_wins += 1

    avg_timing_random = (time_mc_wins / time_mc_total) * 100 if time_mc_total > 0 else 0.0

    mt5.shutdown()

    # ==========================================
    # 📊 FINAL REPORT
    # ==========================================
    print("\n" + "="*50)
    print("🧠 BOT PERFORMANCE (THE ALPHA)")
    print("="*50)
    print(f"Total Signals Fired:      {mech_total}")
    print(f"Mechanical Win Rate:      {mech_win_rate:.2f}% (Raw Velocity Trigger)")
    print(f"AI Approved Trades:       {ai_total} ({(ai_total/mech_total)*100:.1f}% passage rate)")
    print(f"AI Filtered Win Rate:     {ai_win_rate:.2f}%")
    print(f"⭐ AI Lift:               +{ai_win_rate - mech_win_rate:.2f}% (Value added by ML Filter)")

    print("\n" + "="*50)
    print("🎲 MONTE CARLO BASELINES (THE MARKET)")
    print("="*50)
    print(f"Directional Random Rate:  {avg_directional_random:.2f}% (Trend Drift Baseline)")
    print(f"Timing Random Rate:       {avg_timing_random:.2f}% (Volatility Baseline)")
    
    base_market_rate = max(avg_directional_random, avg_timing_random)
    
    print("\n" + "="*50)
    print("🎯 FINAL SYSTEM EDGE VERDICT")
    print("="*50)
    
    mech_edge = mech_win_rate - base_market_rate
    ai_edge = ai_win_rate - base_market_rate
    
    print(f"Raw Mechanical Edge:      {'+' if mech_edge > 0 else ''}{mech_edge:.2f}%")
    print(f"Final System Edge (AI):   {'+' if ai_edge > 0 else ''}{ai_edge:.2f}%")
    
    print("\n📌 CONCLUSION:")
    if ai_edge > 2.0:
        print("MASSIVE ALPHA DETECTED ✅")
        print("Your Velocity Trigger finds unique entry pockets, and your AI successfully")
        print("filters out the noise. Combined with a DCA Grid, this is highly monetizable.")
    elif ai_edge > 0:
        print("SLIGHT EDGE VERIFIED ⚠️")
        print("Your system beats random chance, but the margin is thin. The Grid is doing")
        print("most of the heavy lifting. Consider adding more features to the AI.")
    else:
        print("NEGATIVE EDGE DETECTED ❌")
        print("The market geometry is artificially inflating your win rate. A random")
        print("coin-flip at random times performs better than the AI.")
    print("========================================\n")

if __name__ == "__main__":
    run_deep_edge_analysis()