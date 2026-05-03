import sqlite3
import pandas as pd
import MetaTrader5 as mt5
import json
import os
from datetime import datetime, timedelta, timezone

# --- PATHING & CONFIG ---
script_dir = os.path.dirname(os.path.abspath(__file__))
base_dir = os.path.dirname(script_dir)
config_path = os.path.join(base_dir, 'system_config.json')
db_path = os.path.join(base_dir, 'trading_system.db')

with open(config_path, 'r') as f:
    config = json.load(f)

NEW_HORIZON = 8 # The strict new limit
TP_POINTS = config['strategies']['QT_Velocity']['trade_limits']['tp_points']
SPREAD_ALLOWANCE = config['ml_pipeline']['labeling']['spread_allowance']
UTC_OFFSET = config['system'].get('broker_utc_offset_hours', 3) 
SYMBOL_MAP = {"ES.M26": "US500"} 

def run_surgical_migration():
    if not mt5.initialize():
        print("❌ MT5 init failed.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # We only audit existing WINS. (If it failed in 10 mins, it definitely failed in 8)
    query = "SELECT id, timestamp, symbol, features_json FROM ml_features WHERE target_label = 1"
    wins_to_audit = pd.read_sql(query, conn)
    
    print(f"🔍 Auditing {len(wins_to_audit)} historical WINS against the strict {NEW_HORIZON}-minute rule...")
    
    demoted_to_loss = 0
    preserved_safe = 0
    missing_data = 0

    for index, row in wins_to_audit.iterrows():
        feature_id = row['id']
        qt_symbol = row['symbol']
        symbol = SYMBOL_MAP.get(qt_symbol, qt_symbol) 
        
        utc_start = int(row['timestamp'] / 1000)
        broker_start = utc_start + (UTC_OFFSET * 3600)
        broker_end = broker_start + (NEW_HORIZON * 60) # Only fetch 8 minutes
        
        try:
            payload = json.loads(row['features_json'])
            action = "BUY" if payload.get('trigger', {}).get('speed_delta', 0) < 0 else "SELL"
        except:
            continue

        # Fetch MT5 data (Fallback to M1 open if tick is missing)
        exact_entry = None
        ticks = mt5.copy_ticks_range(symbol, broker_start, broker_start + 60, mt5.COPY_TICKS_ALL)
        
        if ticks is not None and len(ticks) > 0:
            exact_entry = (ticks[0]['bid'] + ticks[0]['ask']) / 2.0
        else:
            # Fallback to M1 open if broker purged the tick
            rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, broker_start, broker_start + 60)
            if rates is not None and len(rates) > 0:
                exact_entry = rates[0]['open']
        
        if exact_entry is None:
            missing_data += 1
            continue # Fail safe: leave as WIN

        # Check the new 8-minute window
        rates_8m = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, broker_start, broker_end)
        if rates_8m is None or len(rates_8m) == 0:
            missing_data += 1
            continue
            
        df_8m = pd.DataFrame(rates_8m)
        
        # Did it hit the target inside the strict 8 minutes?
        survived = False
        if action == "BUY":
            if df_8m['high'].max() >= exact_entry + TP_POINTS + SPREAD_ALLOWANCE:
                survived = True
        elif action == "SELL":
            if df_8m['low'].min() <= exact_entry - TP_POINTS - SPREAD_ALLOWANCE:
                survived = True

        if survived:
            preserved_safe += 1
        else:
            # The trade was a 10-minute WIN, but an 8-minute LOSS. Demote it.
            cursor.execute("UPDATE ml_features SET target_label = 0 WHERE id = ?", (feature_id,))
            demoted_to_loss += 1

    conn.commit()
    conn.close()
    mt5.shutdown()

    print("\n" + "="*50)
    print("✅ SURGICAL MIGRATION COMPLETE")
    print("="*50)
    print(f"Fast Wins Preserved (< 8m): {preserved_safe}")
    print(f"Slow Wins Demoted to Loss:  {demoted_to_loss}")
    print(f"Data Missing (Left Alone):  {missing_data}")
    print("="*50)

if __name__ == "__main__":
    run_surgical_migration()