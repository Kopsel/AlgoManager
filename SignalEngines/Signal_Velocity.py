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
config_path_1 = os.path.join(script_dir, "..", "system_config.json")
config_path_2 = os.path.join(script_dir, "system_config.json")

if os.path.exists(config_path_1):
    CONFIG_FILE = config_path_1
elif os.path.exists(config_path_2):
    CONFIG_FILE = config_path_2
else:
    print(f"CRITICAL ERROR: Config file not found.")
    time.sleep(10)
    sys.exit(1)

def load_config():
    with open(CONFIG_FILE, "r") as f:
        data = json.load(f)
        return data['strategies'][MY_STRATEGY_ID], data['system']

# --- RSI INDICATOR FUNCTION ---
def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0 
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    if rsi.empty: return 50.0
    return rsi.iloc[-1]

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
    volatility_range = high - low
    dynamic_tp = volatility_range * multiplier
    min_tp = limits.get('min_tp_points', 0.5)
    max_tp = limits.get('max_tp_points', 5.0)
    return float(max(min_tp, min(dynamic_tp, max_tp)))

def calibrate_dynamic_threshold(symbol, time_window_sec, lookback_days, percentile):
    print(f"Calibrating dynamic threshold...", flush=True)
    from_time = datetime.now() - timedelta(days=lookback_days)
    ticks = mt5.copy_ticks_from(symbol, from_time, 100000, mt5.COPY_TICKS_ALL)
    if ticks is None or len(ticks) < 1000: return None
    df = pd.DataFrame(ticks)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    window_str = f"{time_window_sec}S"
    df['window'] = df['time'].dt.floor(window_str)
    deltas = []
    for window, group in df.groupby('window'):
        if len(group) > 1:
            delta = abs(group['ask'].iloc[-1] - group['ask'].iloc[0])
            deltas.append(delta)
    if not deltas: return None
    deltas_series = pd.Series(deltas)
    threshold = deltas_series.quantile(percentile)
    print(f"✓ Threshold: {threshold:.6f}", flush=True)
    return float(threshold)

def run_speed_engine():
    # 1. LOAD CONFIG
    try:
        my_conf, sys_conf = load_config()
    except Exception as e:
        print(f"❌ Error Loading Config: {e}", flush=True)
        return

    # EXTRACT PARAMS
    SYMBOL = my_conf['symbol']
    VOLUME = my_conf['volume']
    MAGIC = my_conf['magic_number']
    PARAMS = my_conf['parameters']
    TIME_WINDOW_SEC = PARAMS['time_window_sec']
    COOLDOWN_SEC = PARAMS['cooldown_sec']
    FALLBACK_THRESHOLD = PARAMS['fallback_threshold']
    USE_DYNAMIC_THRESHOLD = PARAMS.get('use_dynamic_threshold', False)
    USE_RSI = PARAMS.get('use_rsi_filter', False)
    RSI_PERIOD = PARAMS.get('rsi_period', 14)
    RSI_UPPER = PARAMS.get('rsi_upper', 70)
    RSI_LOWER = PARAMS.get('rsi_lower', 30)
    RSI_TIMEFRAME = PARAMS.get('rsi_timeframe', 'M1')
    TRADE_LIMITS = my_conf['trade_limits']
    SL_POINTS = TRADE_LIMITS.get('sl_points', 0)
    TP_POINTS = TRADE_LIMITS.get('tp_points', 1.0)
    USE_VOL_TP = TRADE_LIMITS.get('use_volatility_based_tp', False)
    
    TIMEFRAME_MAP = {"M1": mt5.TIMEFRAME_M1, "M2": mt5.TIMEFRAME_M2, "M5": mt5.TIMEFRAME_M5}
    SELECTED_TF = TIMEFRAME_MAP.get(RSI_TIMEFRAME, mt5.TIMEFRAME_M1)

    if USE_DYNAMIC_THRESHOLD:
        calibrated = calibrate_dynamic_threshold(SYMBOL, TIME_WINDOW_SEC, 5, 0.5)
        if calibrated: FALLBACK_THRESHOLD = calibrated

    # 3. CONNECT MT5
    if not mt5.initialize(): sys.exit(1)
    if not mt5.symbol_select(SYMBOL, True): sys.exit(1)

    # 4. CONNECT ZMQ
    zmq_host = sys_conf['zmq_host']
    zmq_port = sys_conf['zmq_port']
    context = zmq.Context()
    
    # --- HELPER TO RESET CONNECTION ---
    def connect_zmq():
        s = context.socket(zmq.REQ)
        s.connect(f"tcp://{zmq_host}:{zmq_port}")
        return s

    socket = connect_zmq()
    print(f"✓ ZMQ connected to {zmq_host}:{zmq_port}", flush=True)

    # 6. MAIN LOOP
    print(f"Engine Running... Threshold: {FALLBACK_THRESHOLD:.5f}", flush=True)
    last_processed_tick_time = None
    last_rsi_check = 0
    current_rsi = 50.0
    signal_count = 0

    try:
        while True:
            from_time = datetime.now() - timedelta(minutes=5)
            ticks = mt5.copy_ticks_from(SYMBOL, from_time, 2000, mt5.COPY_TICKS_ALL)
            
            if ticks is not None and len(ticks) > 10:
                df = pd.DataFrame(ticks)
                df['time'] = pd.to_datetime(df['time'], unit='s')
                
                if USE_RSI and (time.time() - last_rsi_check > 2):
                    rates = mt5.copy_rates_from(SYMBOL, SELECTED_TF, datetime.now(), 100)
                    if rates is not None and len(rates) > RSI_PERIOD:
                        current_rsi = calculate_rsi(pd.DataFrame(rates)['close'], RSI_PERIOD)
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
                    print(f"Speed: {delta:+.6f} | RSI: {current_rsi:.1f}    ", end='\r', flush=True)

                    if abs(delta) > FALLBACK_THRESHOLD:
                        action = "SELL" if delta > 0 else "BUY"
                        is_valid = True
                        if USE_RSI:
                            if action == "SELL" and current_rsi < RSI_UPPER: is_valid = False
                            if action == "BUY" and current_rsi > RSI_LOWER: is_valid = False

                        if is_valid:
                            tp = calculate_volatility_tp(df, TRADE_LIMITS) if USE_VOL_TP else TP_POINTS
                            signal_count += 1
                            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                            print(f"\n[{ts}] 🚀 SIGNAL #{signal_count} | {action} | Speed: {delta:+.6f}", flush=True)
                            
                            payload = {
                                "strategy_id": MY_STRATEGY_ID, "symbol": SYMBOL, "action": action, "dynamic_tp": tp,
                                "extra_metrics": {"rsi": current_rsi, "speed": delta, "magic": MAGIC}
                            }
                            
                            # --- CRITICAL FIX HERE ---
                            # We wrap BOTH send and recv in try/except
                            try:
                                socket.send_json(payload)
                                # We remove NOBLOCK to ensure we wait for reply, preventing the desync
                                response = socket.recv_string() 
                                print(f"        Manager: {response}", flush=True)
                                
                            except zmq.ZMQError:
                                print(f"        ⚠️ Manager not responding. Resetting connection...", flush=True)
                                # THIS IS THE KEY: Close and Re-open to clear the "stuck" state
                                socket.close()
                                socket = connect_zmq()
                            
                            time.sleep(COOLDOWN_SEC)

            time.sleep(0.01)

    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        print(f"\nError: {e}")
        traceback.print_exc()
    finally:
        mt5.shutdown()
        socket.close()
        context.term()

if __name__ == "__main__":
    run_speed_engine()