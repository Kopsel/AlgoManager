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

# --- PATH FINDER ---
script_dir = os.path.dirname(os.path.abspath(__file__))
config_path_1 = os.path.join(script_dir, "..", "system_config.json")
config_path_2 = os.path.join(script_dir, "system_config.json")

if os.path.exists(config_path_1): CONFIG_FILE = config_path_1
elif os.path.exists(config_path_2): CONFIG_FILE = config_path_2
else:
    print("CRITICAL ERROR: Config file not found.")
    time.sleep(10)
    sys.exit(1)

def load_config():
    with open(CONFIG_FILE, "r") as f:
        data = json.load(f)
        return data['strategies'][MY_STRATEGY_ID], data['system']

# --- RSI SERIES FUNCTION ---
def calculate_rsi_series(prices, period=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_volatility_tp(df, limits):
    if not limits.get('use_volatility_based_tp', False): return limits.get('tp_points', 1.0)
    lookback = limits.get('volatility_lookback_sec', 60)
    multiplier = limits.get('tp_volatility_multiplier', 0.5)
    last_tick_time = df['time'].iloc[-1]
    cutoff = last_tick_time - pd.Timedelta(seconds=lookback)
    recent_vol = df[df['time'] >= cutoff]
    if recent_vol.empty: return limits.get('tp_points', 1.0)
    high = recent_vol['ask'].max()
    low = recent_vol['ask'].min()
    return float(max(limits.get('min_tp_points', 0.5), min((high - low) * multiplier, limits.get('max_tp_points', 5.0))))

# --- NEW: TIME-SPECIFIC CALIBRATION (Fully Configurable) ---
def calibrate_time_specific_threshold(symbol, time_window_sec, lookback_days, percentile, time_slice_minutes):
    # 1. Fetch data for the last X days
    from_time = datetime.now() - timedelta(days=lookback_days)
    ticks = mt5.copy_ticks_from(symbol, from_time, 1000000, mt5.COPY_TICKS_ALL) 
    
    if ticks is None or len(ticks) < 1000: return None
        
    df = pd.DataFrame(ticks)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    # 2. Add "Minute of Day" helper column
    df['minute_of_day'] = df['time'].dt.hour * 60 + df['time'].dt.minute
    
    # 3. Determine the "Time Window" relevant to NOW
    now = datetime.now()
    current_minute_of_day = now.hour * 60 + now.minute
    
    # Use the variable from Config instead of hardcoded 30
    min_bound = current_minute_of_day - time_slice_minutes
    max_bound = current_minute_of_day + time_slice_minutes
    
    # Handle midnight wraparound
    if min_bound < 0:
        mask = (df['minute_of_day'] >= (1440 + min_bound)) | (df['minute_of_day'] <= max_bound)
    elif max_bound > 1440:
        mask = (df['minute_of_day'] >= min_bound) | (df['minute_of_day'] <= (max_bound - 1440))
    else:
        mask = (df['minute_of_day'] >= min_bound) & (df['minute_of_day'] <= max_bound)
        
    df_filtered = df.loc[mask].copy()
    
    if df_filtered.empty: return None
        
    # 4. Calculate Volatility on this TIME-SPECIFIC slice
    window_str = f"{time_window_sec}s"
    df_filtered['window'] = df_filtered['time'].dt.floor(window_str)
    
    deltas = []
    for window, group in df_filtered.groupby('window'):
        if len(group) > 1:
            delta = abs(group['ask'].iloc[-1] - group['ask'].iloc[0])
            deltas.append(delta)
            
    if not deltas: return None
    
    deltas_series = pd.Series(deltas)
    threshold = deltas_series.quantile(percentile)
    
    return float(threshold)

def run_speed_engine():
    try: my_conf, sys_conf = load_config()
    except: return

    SYMBOL = my_conf['symbol']
    VOLUME = my_conf['volume']
    MAGIC = my_conf['magic_number']
    PARAMS = my_conf['parameters']
    
    # 1. Speed Params
    TIME_WINDOW_SEC = PARAMS['time_window_sec']
    COOLDOWN_SEC = PARAMS['cooldown_sec']
    FALLBACK_THRESHOLD = PARAMS['fallback_threshold']
    USE_DYNAMIC_THRESHOLD = PARAMS.get('use_dynamic_threshold', False)
    
    # 2. Calibration Params (ALL extracted from config)
    CALIB_CONF = PARAMS.get('calibration', {})
    CALIB_DAYS = CALIB_CONF.get('lookback_days', 5)
    CALIB_PERCENTILE = CALIB_CONF.get('percentile', 0.5)
    CALIB_INTERVAL_MIN = CALIB_CONF.get('recalibrate_minutes', 10) 
    CALIB_SLICE_MIN = CALIB_CONF.get('time_slice_minutes', 30) # Default 30 if missing
    
    # 3. RSI Params
    USE_RSI = PARAMS.get('use_rsi_filter', False)
    RSI_PERIOD = PARAMS.get('rsi_period', 14)
    RSI_UPPER = PARAMS.get('rsi_upper', 70)
    RSI_LOWER = PARAMS.get('rsi_lower', 30)
    RSI_LOOKBACK_CANDLES = PARAMS.get('rsi_latch_minutes', 60)
    
    RSI_TF_STR = PARAMS.get('rsi_timeframe', 'M1')
    TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1, "M2": mt5.TIMEFRAME_M2, "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1
    }
    SELECTED_TF = TIMEFRAME_MAP.get(RSI_TF_STR, mt5.TIMEFRAME_M1)

    TRADE_LIMITS = my_conf['trade_limits']
    SL_POINTS = TRADE_LIMITS.get('sl_points', 0)
    TP_POINTS = TRADE_LIMITS.get('tp_points', 1.0)
    USE_VOL_TP = TRADE_LIMITS.get('use_volatility_based_tp', False)

    # --- INITIAL CALIBRATION ---
    if USE_DYNAMIC_THRESHOLD:
        print(f"init Time-Specific Calibration (Days: {CALIB_DAYS}, Slice: +/-{CALIB_SLICE_MIN}m)...", end="", flush=True)
        calibrated = calibrate_time_specific_threshold(SYMBOL, TIME_WINDOW_SEC, CALIB_DAYS, CALIB_PERCENTILE, CALIB_SLICE_MIN)
        if calibrated: 
            FALLBACK_THRESHOLD = calibrated
            print(f" Done. Thr: {FALLBACK_THRESHOLD:.5f}")
        else:
            print(" Failed. Using Fallback.")

    if not mt5.initialize(): sys.exit(1)
    if not mt5.symbol_select(SYMBOL, True): sys.exit(1)

    zmq_host, zmq_port = sys_conf['zmq_host'], sys_conf['zmq_port']
    context = zmq.Context()
    
    def connect_zmq():
        s = context.socket(zmq.REQ)
        s.connect(f"tcp://{zmq_host}:{zmq_port}")
        s.setsockopt(zmq.RCVTIMEO, 2000) 
        s.setsockopt(zmq.LINGER, 0)
        return s

    socket = connect_zmq()
    print(f"✓ ZMQ connected. Strategy: {MY_STRATEGY_ID}")
    print(f"✓ SPEED: M1 Ticks | RSI: {RSI_TF_STR} | Latch: {RSI_LOOKBACK_CANDLES} min")
    print(f"✓ SEASONAL CALIB: Comparing Current Time +/- {CALIB_SLICE_MIN}m vs Last {CALIB_DAYS} Days (Every {CALIB_INTERVAL_MIN} min)", flush=True)

    last_processed_tick_time = None
    last_rsi_check = 0
    
    # Recalibration Timer
    last_calibration_time = time.time()
    RECALIBRATE_INTERVAL_SEC = CALIB_INTERVAL_MIN * 60

    recent_max_rsi = 50.0 
    recent_min_rsi = 50.0
    current_rsi = 50.0
    
    signal_count = 0

    try:
        while True:
            # --- 0. AUTO-RECALIBRATE (Time Specific) ---
            if USE_DYNAMIC_THRESHOLD and (time.time() - last_calibration_time > RECALIBRATE_INTERVAL_SEC):
                print(f" [SeasonCalib]...", end='', flush=True) 
                new_threshold = calibrate_time_specific_threshold(SYMBOL, TIME_WINDOW_SEC, CALIB_DAYS, CALIB_PERCENTILE, CALIB_SLICE_MIN)
                if new_threshold:
                    FALLBACK_THRESHOLD = new_threshold
                last_calibration_time = time.time()

            # --- 1. Get Ticks ---
            from_time = datetime.now() - timedelta(minutes=5)
            ticks = mt5.copy_ticks_from(SYMBOL, from_time, 2000, mt5.COPY_TICKS_ALL)
            
            if ticks is not None and len(ticks) > 10:
                df = pd.DataFrame(ticks)
                df['time'] = pd.to_datetime(df['time'], unit='s')
                
                # --- 2. RSI Update ---
                if USE_RSI and (time.time() - last_rsi_check > 2):
                    fetch_count = max(100, RSI_LOOKBACK_CANDLES + 10)
                    rates = mt5.copy_rates_from_pos(SYMBOL, SELECTED_TF, 0, fetch_count)
                    
                    if rates is not None and len(rates) > RSI_PERIOD:
                        df_rates = pd.DataFrame(rates)
                        rsi_series = calculate_rsi_series(df_rates['close'], RSI_PERIOD)
                        current_rsi = rsi_series.iloc[-1]
                        recent_max_rsi = rsi_series.iloc[-RSI_LOOKBACK_CANDLES:].max()
                        recent_min_rsi = rsi_series.iloc[-RSI_LOOKBACK_CANDLES:].min()
                        
                    last_rsi_check = time.time()

                data_now = df.iloc[-1]['time']
                if last_processed_tick_time == data_now:
                    time.sleep(0.01)
                    continue
                last_processed_tick_time = data_now

                cutoff = data_now - pd.Timedelta(seconds=TIME_WINDOW_SEC)
                recent = df[df['time'] >= cutoff]
                
                if not recent.empty:
                    delta = recent.iloc[-1]['ask'] - recent.iloc[0]['ask']
                    
                    if USE_RSI:
                        can_sell = "✅" if recent_max_rsi > RSI_UPPER else "❌"
                        can_buy  = "✅" if recent_min_rsi < RSI_LOWER else "❌"
                        rsi_txt = f"Curr:{current_rsi:.1f} | Max:{recent_max_rsi:.1f}({can_sell}) | Min:{recent_min_rsi:.1f}({can_buy})"
                    else:
                        rsi_txt = "OFF"
                        
                    print(f"Thr:{FALLBACK_THRESHOLD:.3f} | Speed:{delta:+.3f} | {rsi_txt}      ", end='\r', flush=True)

                    if abs(delta) > FALLBACK_THRESHOLD:
                        action = "SELL" if delta > 0 else "BUY"
                        is_valid = True
                        
                        if USE_RSI:
                            if action == "SELL" and recent_max_rsi < RSI_UPPER: is_valid = False
                            if action == "BUY" and recent_min_rsi > RSI_LOWER: is_valid = False

                        if is_valid:
                            tp = calculate_volatility_tp(df, TRADE_LIMITS) if USE_VOL_TP else TP_POINTS
                            signal_count += 1
                            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                            print(f"\n[{ts}] 🚀 SIGNAL #{signal_count} | {action} | Speed: {delta:+.6f} | TP: {tp:.2f}", flush=True)
                            
                            payload = {
                                "strategy_id": MY_STRATEGY_ID, "symbol": SYMBOL, "action": action, "dynamic_tp": tp,
                                "extra_metrics": {"rsi": current_rsi, "speed": delta, "magic": MAGIC}
                            }
                            
                            try:
                                socket.send_json(payload)
                                print(f"        Manager: {socket.recv_string()}", flush=True)
                            except (zmq.Again, zmq.ZMQError):
                                print(f"        ⚠️ Comms Error. Resetting...", flush=True)
                                socket.close()
                                socket = connect_zmq()
                            
                            time.sleep(COOLDOWN_SEC)

            time.sleep(0.01)

    except KeyboardInterrupt: print("\nStopped.")
    finally: mt5.shutdown(); socket.close(); context.term()

if __name__ == "__main__":
    run_speed_engine()