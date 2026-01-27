import zmq
import MetaTrader5 as mt5
import json
import time
import pandas as pd
import numpy as np
import os
import sys
import traceback
from datetime import datetime, timedelta

# --- IDENTITY ---
MY_STRATEGY_ID = "SPEED_US500_01"

# --- PATH FIX ---
script_dir = os.path.dirname(os.path.abspath(__file__))
# Try parent directory first (Standard), then current (Flat)
config_path_1 = os.path.join(script_dir, "..", "system_config.json")
config_path_2 = os.path.join(script_dir, "system_config.json")

if os.path.exists(config_path_1):
    CONFIG_FILE = config_path_1
elif os.path.exists(config_path_2):
    CONFIG_FILE = config_path_2
else:
    print(f"CRITICAL ERROR: Config file not found. Checked:\n1. {config_path_1}\n2. {config_path_2}")
    time.sleep(10)
    sys.exit(1)

def load_config():
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
    """Calculate dynamic TP based on recent volatility."""
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

def calibrate_dynamic_threshold(symbol, time_window_sec, lookback_days, percentile):
    """
    Calibrate threshold based on historical tick data.
    """
    print(f"Calibrating dynamic threshold from {lookback_days} days of data...", flush=True)
    
    from_time = datetime.now() - timedelta(days=lookback_days)
    ticks = mt5.copy_ticks_from(symbol, from_time, 100000, mt5.COPY_TICKS_ALL)
    
    if ticks is None or len(ticks) < 1000:
        print(f"⚠️  Calibration: Not enough data ({len(ticks) if ticks else 0} ticks), using fallback", flush=True)
        return None
    
    df = pd.DataFrame(ticks)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    # Group by time_window_sec windows and calculate deltas
    window_str = f"{time_window_sec}S"
    df['window'] = df['time'].dt.floor(window_str)
    
    deltas = []
    for window, group in df.groupby('window'):
        if len(group) > 1:
            delta = abs(group['ask'].iloc[-1] - group['ask'].iloc[0])
            deltas.append(delta)
    
    if not deltas:
        print(f"⚠️  Calibration: No deltas calculated, using fallback", flush=True)
        return None
    
    deltas_series = pd.Series(deltas)
    threshold = deltas_series.quantile(percentile)
    
    print(f"✓ Dynamic threshold calibrated: {threshold:.6f} (percentile: {percentile * 100:.0f}%)", flush=True)
    
    return float(threshold)

def run_speed_engine():
    # --- 1. LOAD CONFIG ---
    try:
        my_conf, sys_conf = load_config()
    except Exception as e:
        print(f"❌ Error Loading Config: {e}", flush=True)
        time.sleep(10)
        return

    # --- EXTRACT ALL CONFIG PARAMETERS ---
    SYMBOL = my_conf['symbol']
    VOLUME = my_conf['volume']
    MAGIC = my_conf['magic_number']
    
    # Parameters (nested)
    PARAMS = my_conf['parameters']
    TIME_WINDOW_SEC = PARAMS['time_window_sec']
    COOLDOWN_SEC = PARAMS['cooldown_sec']
    USE_DYNAMIC_THRESHOLD = PARAMS.get('use_dynamic_threshold', False)
    FALLBACK_THRESHOLD = PARAMS['fallback_threshold']
    
    # RSI Config
    USE_RSI = PARAMS.get('use_rsi_filter', False)
    RSI_PERIOD = PARAMS.get('rsi_period', 14)
    RSI_UPPER = PARAMS.get('rsi_upper', 70)
    RSI_LOWER = PARAMS.get('rsi_lower', 30)
    RSI_TIMEFRAME = PARAMS.get('rsi_timeframe', 'M1')
    
    # Trade Limits
    TRADE_LIMITS = my_conf['trade_limits']
    SL_POINTS = TRADE_LIMITS.get('sl_points', 0)
    TP_POINTS = TRADE_LIMITS.get('tp_points', 1.0)
    USE_VOL_TP = TRADE_LIMITS.get('use_volatility_based_tp', False)
    
    # Timeframe Mapping
    TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1, "M2": mt5.TIMEFRAME_M2, "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1,
    }
    SELECTED_TF = TIMEFRAME_MAP.get(RSI_TIMEFRAME, mt5.TIMEFRAME_M1)
    
    # --- 2. CALIBRATE DYNAMIC THRESHOLD ---
    if USE_DYNAMIC_THRESHOLD:
        CALIBRATION = PARAMS.get('calibration', {})
        LOOKBACK_DAYS = CALIBRATION.get('lookback_days', 5)
        PERCENTILE = CALIBRATION.get('percentile', 0.5)
        
        calibrated = calibrate_dynamic_threshold(
            SYMBOL, TIME_WINDOW_SEC, LOOKBACK_DAYS, PERCENTILE
        )
        if calibrated is not None:
            FALLBACK_THRESHOLD = calibrated
        else:
            print(f"⚠️  Falling back to static threshold: {FALLBACK_THRESHOLD:.6f}", flush=True)
    
    # --- 3. CONNECT MT5 ---
    path = sys_conf.get('mt5_terminal_path')
    if path and os.path.exists(path):
        if not mt5.initialize(path=path):
            print(f"❌ MT5 Init Failed at {path}", flush=True)
            sys.exit(1)
    else:
        if not mt5.initialize():
            print(f"❌ MT5 Init Failed (default path)", flush=True)
            sys.exit(1)

    if not mt5.symbol_select(SYMBOL, True):
        print(f"❌ Failed to select symbol {SYMBOL}", flush=True)
        sys.exit(1)
    print(f"✓ Symbol selected: {SYMBOL}", flush=True)

    # --- 4. CONNECT ZMQ (ROBUST / SELF-HEALING) ---
    zmq_host = sys_conf['zmq_host']
    zmq_port = sys_conf['zmq_port']
    context = zmq.Context()
    
    # Helper Function to Rebuild Connection cleanly
    def connect_socket():
        # print(f"Connecting to Trade Manager...", flush=True)
        sock = context.socket(zmq.REQ)
        sock.connect(f"tcp://{zmq_host}:{zmq_port}")
        # IMPORTANT: Timeout ensures we don't freeze forever if Manager is busy
        sock.setsockopt(zmq.RCVTIMEO, 2000) # 2 seconds timeout
        sock.setsockopt(zmq.LINGER, 0)
        return sock

    socket = connect_socket()
    print(f"✓ ZMQ connected to {zmq_host}:{zmq_port}", flush=True)
    
    # --- 5. PRINT CONFIGURATION SUMMARY ---
    print("\n" + "="*70, flush=True)
    print(f"STRATEGY: {MY_STRATEGY_ID}", flush=True)
    print(f"Threshold: {FALLBACK_THRESHOLD:.6f} {'(DYNAMIC)' if USE_DYNAMIC_THRESHOLD else '(STATIC)'}", flush=True)
    print("="*70 + "\n", flush=True)

    last_processed_tick_time = None
    last_rsi_check = 0
    current_rsi = 50.0
    signal_count = 0

    # --- 6. MAIN LOOP ---
    try:
        while True:
            # Request tick history
            from_time = datetime.now() - timedelta(minutes=5)
            ticks = mt5.copy_ticks_from(SYMBOL, from_time, 2000, mt5.COPY_TICKS_ALL)
            
            if ticks is not None and len(ticks) > 10:
                df = pd.DataFrame(ticks)
                df['time'] = pd.to_datetime(df['time'], unit='s')
                
                # --- RSI UPDATE (every 2s) ---
                if USE_RSI and (time.time() - last_rsi_check > 2): 
                    rates = mt5.copy_rates_from(SYMBOL, SELECTED_TF, datetime.now(), 100)
                    if rates is not None and len(rates) > RSI_PERIOD:
                        df_rates = pd.DataFrame(rates)
                        current_rsi = calculate_rsi(df_rates['close'], RSI_PERIOD)
                    last_rsi_check = time.time()

                data_now = df.iloc[-1]['time']
                
                # --- DEDUPLICATION ---
                if last_processed_tick_time == data_now:
                    time.sleep(0.01)
                    continue
                
                last_processed_tick_time = data_now

                # --- CALCULATE SPEED (DELTA) ---
                cutoff_speed = data_now - pd.Timedelta(seconds=TIME_WINDOW_SEC)
                recent_speed = df[df['time'] >= cutoff_speed]
                
                if not recent_speed.empty:
                    price_now = recent_speed.iloc[-1]['ask']
                    price_start = recent_speed.iloc[0]['ask']
                    delta = price_now - price_start
                    
                    # --- STATUS DISPLAY ---
                    rsi_str = f"RSI: {current_rsi:.1f}" if USE_RSI else ""
                    print(f"Speed: {delta:+.6f} | {rsi_str}     ", end='\r', flush=True)

                    # --- TRIGGER LOGIC ---
                    if abs(delta) > FALLBACK_THRESHOLD:
                        
                        # 1. Determine Direction
                        action = "SELL" if delta > 0 else "BUY"
                        
                        # 2. APPLY EDGE FILTERS (RSI)
                        is_valid = True

                        if USE_RSI:
                            if action == "SELL" and current_rsi < RSI_UPPER: is_valid = False
                            if action == "BUY" and current_rsi > RSI_LOWER: is_valid = False

                        # 3. EXECUTE OR REJECT
                        if is_valid:
                            # Calculate TP (static or dynamic)
                            calculated_tp = calculate_volatility_tp(df, TRADE_LIMITS) if USE_VOL_TP else TP_POINTS
                            
                            signal_count += 1
                            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                            
                            print(f"\n[{timestamp}] 🚀 SIGNAL #{signal_count} | {action:4s} | Speed: {delta:+.6f} | TP: {calculated_tp:.2f}", flush=True)
                            
                            payload = {
                                "strategy_id": MY_STRATEGY_ID,
                                "symbol": SYMBOL,
                                "action": action,
                                "dynamic_tp": calculated_tp,
                                "extra_metrics": {
                                    "rsi": current_rsi if USE_RSI else 0,
                                    "speed": delta,
                                    "sl_points": SL_POINTS,
                                    "volume": VOLUME,
                                    "magic": MAGIC
                                }
                            }
                            
                            # --- ROBUST SENDING BLOCK (FIXED) ---
                            try:
                                # 1. Send Request
                                socket.send_json(payload)
                                
                                # 2. Receive Reply (BLOCKING with 2s Timeout)
                                # IMPORTANT: We removed zmq.NOBLOCK. We MUST wait for reply or timeout.
                                response = socket.recv_string()
                                print(f"        Manager: {response}", flush=True)
                                
                            except zmq.Again:
                                print(f"        ⚠️ Timeout: Manager busy. Resetting socket...", flush=True)
                                socket.close()
                                socket = connect_socket() # RECONNECT ON TIMEOUT
                                
                            except zmq.ZMQError as e:
                                print(f"        ❌ ZMQ Error: {e}. Rebuilding connection...", flush=True)
                                socket.close()
                                socket = connect_socket() # RECONNECT ON CRASH
                            
                            print(f"        Cooling down for {COOLDOWN_SEC}s...", flush=True)
                            time.sleep(COOLDOWN_SEC)

            time.sleep(0.01)

    except KeyboardInterrupt:
        print("\n\n⏹️  Engine stopped by user", flush=True)
    except Exception as e:
        print(f"\n\n❌ Engine crashed: {e}", flush=True)
        traceback.print_exc()
    finally:
        mt5.shutdown()
        socket.close()
        context.term()
        print("✓ Cleanup complete", flush=True)

if __name__ == "__main__":
    run_speed_engine()