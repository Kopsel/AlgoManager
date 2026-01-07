import zmq
import MetaTrader5 as mt5
import json
import time
import pandas as pd
import numpy as np
import os
import sys
from datetime import datetime, timedelta

# --- IDENTITY ---
MY_STRATEGY_ID = "SPEED_US500_01"

# --- PATH FIX ---
script_dir = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(script_dir, "..", "system_config.json")

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"CRITICAL ERROR: Config file not found at: {CONFIG_FILE}")
        input("Press Enter to exit...") 
        sys.exit(1)
    with open(CONFIG_FILE, "r") as f:
        data = json.load(f)
        return data['strategies'][MY_STRATEGY_ID], data['system']

# --- RSI INDICATOR FUNCTION ---
def calculate_rsi(prices, period=14):
    """
    Calculates RSI using pure NumPy/Pandas.
    """
    if len(prices) < period + 1:
        return 50.0 # Neutral default
        
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)

    # Use Exponential Moving Average (Wilder's Smoothing approximation)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    if rsi.empty: return 50.0
    return rsi.iloc[-1]

def calculate_volatility_tp(df, limits):
    if not limits.get('use_volatility_based_tp', False):
        return limits.get('tp_points', 1.0)

    lookback = limits.get('volatility_lookback_sec', 60)
    multiplier = limits.get('tp_volatility_multiplier', 0.5)
    
    last_tick_time = df['time'].iloc[-1]
    cutoff = last_tick_time - pd.Timedelta(seconds=lookback)
    
    recent_vol = df[df['time'] >= cutoff]
    
    if recent_vol.empty:
        return limits.get('tp_points', 1.0)
    
    high = recent_vol['ask'].max()
    low = recent_vol['ask'].min()
    volatility_range = high - low
    
    dynamic_tp = volatility_range * multiplier
    
    min_tp = limits.get('min_tp_points', 0.5)
    max_tp = limits.get('max_tp_points', 5.0)
    
    return float(max(min_tp, min(dynamic_tp, max_tp)))

def run_speed_engine():
    # 1. Setup
    my_conf, sys_conf = load_config()
    SYMBOL = my_conf['symbol']
    PARAMS = my_conf['parameters']
    
    # 2. Connect
    path = sys_conf.get('mt5_terminal_path')
    if path and os.path.exists(path):
        if not mt5.initialize(path=path):
            sys.exit(1)
    else:
        mt5.initialize()

    if not mt5.symbol_select(SYMBOL, True):
        sys.exit(1)

    # 3. Thresholds
    threshold = PARAMS['fallback_threshold']
    
    # RSI Config
    use_rsi = PARAMS.get('use_rsi_filter', False)
    rsi_period = PARAMS.get('rsi_period', 14)
    rsi_upper = PARAMS.get('rsi_upper', 70)
    rsi_lower = PARAMS.get('rsi_lower', 30)
    
    # Timeframe Mapping
    tf_str = PARAMS.get('rsi_timeframe', 'M1')
    TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M2": mt5.TIMEFRAME_M2,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
    }
    selected_tf = TIMEFRAME_MAP.get(tf_str, mt5.TIMEFRAME_M1)

    # 4. Connect ZMQ
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.connect(f"tcp://{sys_conf['zmq_host']}:{sys_conf['zmq_port']}")
    
    print(f"--- Monitoring {SYMBOL} | Threshold: {threshold:.2f} ---")
    if use_rsi:
        print(f"--- RSI Filter ON (TF: {tf_str} | Period: {rsi_period} | Sell>{rsi_upper} | Buy<{rsi_lower}) ---")

    last_processed_tick_time = None
    last_rsi_check = 0
    current_rsi = 50.0

    # 5. Loop
    while True:
        # Request tick history
        from_time = datetime.now() - timedelta(minutes=5)
        ticks = mt5.copy_ticks_from(SYMBOL, from_time, 2000, mt5.COPY_TICKS_ALL)
        
        if ticks is not None and len(ticks) > 10:
            df = pd.DataFrame(ticks)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            
            # --- CONTEXT: Fetch Candles for RSI ---
            # Update RSI every 2s
            if use_rsi and (time.time() - last_rsi_check > 2): 
                # [FIX] Use the dynamic timeframe from config
                rates = mt5.copy_rates_from(SYMBOL, selected_tf, datetime.now(), 100)
                if rates is not None and len(rates) > rsi_period:
                    df_rates = pd.DataFrame(rates)
                    current_rsi = calculate_rsi(df_rates['close'], rsi_period)
                last_rsi_check = time.time()

            data_now = df.iloc[-1]['time']
            
            # Deduplication
            if last_processed_tick_time == data_now:
                time.sleep(0.01)
                continue
            
            last_processed_tick_time = data_now

            # Calculate Window
            cutoff_speed = data_now - pd.Timedelta(seconds=PARAMS['time_window_sec'])
            recent_speed = df[df['time'] >= cutoff_speed]
            
            if not recent_speed.empty:
                price_now = recent_speed.iloc[-1]['ask']
                price_start = recent_speed.iloc[0]['ask']
                delta = price_now - price_start
                
                # --- STATUS DISPLAY ---
                rsi_str = f"RSI({tf_str}): {current_rsi:.1f}" if use_rsi else ""
                print(f"Speed: {delta:+.2f} | {rsi_str}     ", end='\r', flush=True)

                # --- TRIGGER LOGIC ---
                if abs(delta) > threshold:
                    
                    # 1. Determine Direction
                    action = "SELL" if delta > 0 else "BUY"
                    
                    # 2. APPLY EDGE FILTERS (RSI ONLY)
                    is_valid = True
                    rejection_reason = ""

                    if use_rsi:
                        # If Speed UP (Sell Signal), RSI must be Overbought (e.g. > 65)
                        if action == "SELL" and current_rsi < rsi_upper:
                            is_valid = False
                            rejection_reason = f"RSI too low ({current_rsi:.1f})"

                        # If Speed DOWN (Buy Signal), RSI must be Oversold (e.g. < 35)
                        if action == "BUY" and current_rsi > rsi_lower:
                            is_valid = False
                            rejection_reason = f"RSI too high ({current_rsi:.1f})"

                    # 3. EXECUTE OR REJECT
                    if is_valid:
                        calculated_tp = calculate_volatility_tp(df, my_conf['trade_limits'])
                        print(f"\n🚀 TRIGGER! {action} | Speed: {delta:.2f} | RSI: {current_rsi:.1f} | TP: {calculated_tp:.2f}")
                        
                        socket.send_json({
                            "strategy_id": MY_STRATEGY_ID,
                            "symbol": SYMBOL,
                            "action": action,
                            "dynamic_tp": calculated_tp,
                            "extra_metrics": {
                                "rsi": current_rsi if use_rsi else 0,
                                "rsi_tf": tf_str
                            }
                        })
                        
                        try:
                            print(f"Manager: {socket.recv_string()}")
                        except zmq.ZMQError:
                            print("Manager not responding...")
                            
                        print(f"Cooling down for {PARAMS['cooldown_sec']}s...")
                        time.sleep(PARAMS['cooldown_sec'])
                    else:
                        pass

        time.sleep(0.01)

if __name__ == "__main__":
    try:
        run_speed_engine()
    except Exception:
        pass