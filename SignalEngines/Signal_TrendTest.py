import zmq
import MetaTrader5 as mt5
import json
import time
import os
import sys
import random
import traceback

# --- IDENTITY ---
MY_STRATEGY_ID = "TREND_TEST_01"

# --- PATH SETUP ---
script_dir = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(script_dir, "..", "system_config.json")
last_config_mtime = 0

def get_file_mtime(filepath):
    if os.path.exists(filepath): return os.path.getmtime(filepath)
    return 0

def load_config():
    global last_config_mtime
    if not os.path.exists(CONFIG_FILE):
        print(f"Config not found: {CONFIG_FILE}")
        input("Press Enter to exit...")
        sys.exit(1)
        
    last_config_mtime = get_file_mtime(CONFIG_FILE)
    with open(CONFIG_FILE, "r") as f:
        data = json.load(f)
        return data['strategies'][MY_STRATEGY_ID], data['system']

def run_engine():
    # 1. Load Settings
    my_conf, sys_conf = load_config()
    SYMBOL = my_conf['symbol']
    
    # [FIX] Use the custom path from config!
    mt5_path = sys_conf.get('mt5_terminal_path')
    
    # 2. Connect to MT5
    if mt5_path and os.path.exists(mt5_path):
        if not mt5.initialize(path=mt5_path):
            print(f"MT5 Init Failed (Path: {mt5_path})")
            print(mt5.last_error())
            return
    else:
        if not mt5.initialize():
            print("MT5 Init Failed (Default Path)")
            return
    
    if not mt5.symbol_select(SYMBOL, True):
        print(f"Failed to select {SYMBOL}")
        return

    # 3. Setup ZMQ
    context = zmq.Context()
    socket = context.socket(zmq.REQ)
    socket.connect(f"tcp://{sys_conf['zmq_host']}:{sys_conf['zmq_port']}")
    
    print(f"--- Chaos Engine Started ({SYMBOL}) ---")
    print("WARNING: This will trigger trades endlessly!")

    while True:
        # Hot Reload
        if get_file_mtime(CONFIG_FILE) > last_config_mtime:
            try:
                my_conf, _ = load_config()
                print("Config Reloaded")
            except: pass
            
        # --- SIMPLE LOGIC (GUARANTEED SIGNAL) ---
        tick = mt5.symbol_info_tick(SYMBOL)
        
        if tick is not None:
            # 50/50 Chance to Buy or Sell
            signal = random.choice(["BUY", "SELL"])
            
            # Print Status
            print(f"Price: {tick.ask:.2f} | 🎲 Random Signal: {signal}            ", end='\r')

            # --- FIRE SIGNAL ---
            try:
                luck_value = random.randint(1, 100) # Generate a number 1-100
                socket.send_json({
                    "strategy_id": MY_STRATEGY_ID,
                    "symbol": SYMBOL,
                    "action": signal,
                    "dynamic_tp": 2.0, # Fixed Scalp Target
                    "extra_metrics": {
                        "mode": "Chaos_Test",
                        "tick_price": tick.ask,
                        "luck_factor": luck_value  # <--- NEW METRIC
                    }
                })
                
                # Wait for reply to prevent ZMQ desync
                socket.recv_string()
                
            except zmq.ZMQError:
                print("ZMQ Error - Is Manager Running?")
                time.sleep(1)

        else:
            print(f"Waiting for market data ({SYMBOL})...", end='\r')

        # FAST COOLDOWN (2 Seconds)
        time.sleep(5)

if __name__ == "__main__":
    try:
        run_engine()
    except Exception as e:
        print("\nCRITICAL ERROR:")
        traceback.print_exc()
        # [FIX] Keeps the window open so you can read the error
        input("Press Enter to close...")