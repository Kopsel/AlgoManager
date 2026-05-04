import zmq
import json
import sys
import os
import time
import pandas as pd
import xgboost as xgb

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

from components.database import Database

# --- CONFIG & AI MODEL ---
CONFIG_FILE = os.path.join(BASE_DIR, "system_config.json")
config = {}
last_config_mtime = 0
ai_model = None

def get_file_mtime(filepath):
    if os.path.exists(filepath): return os.path.getmtime(filepath)
    return 0

def load_config_and_model():
    global config, last_config_mtime, ai_model
    
    current_mtime = get_file_mtime(CONFIG_FILE)
    if current_mtime > last_config_mtime:
        try:
            with open(CONFIG_FILE, "r") as f:
                new_config = json.load(f)
                
            config = new_config
            last_config_mtime = current_mtime
            print("🔄 ML Brain: Configuration Reloaded.")
            
        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"⚠️ Warning: Could not read config file: {e}")
            
        if ai_model is None and config:
            model_path = os.path.join(BASE_DIR, config.get('ml_pipeline', {}).get('alpha_filter', {}).get('model_save_path', ''))
            if os.path.exists(model_path):
                ai_model = xgb.XGBClassifier()
                ai_model.load_model(model_path)
                print(f"🧠 AI Alpha Filter Loaded from {model_path}")

def process_qt_velocity(payload):
    # --- EMERGENCY SYSTEM LOCK CHECK ---
    risk_cfg = config.get('risk_management', {}).get('emergency_protocols', {})
    if risk_cfg.get('system_locked', False): 
        action = "BUY" if payload.get('trigger', {}).get('speed_delta', 0) < 0 else "SELL"
        return action, {"confidence": 0.0}, 0.0, True
        
    trigger = payload.get('trigger', {})
    
    # NEW: Pull the fixed volume directly from the strategy config
    fixed_volume = config.get('strategies', {}).get('QT_Velocity', {}).get('volume', 0.01)
    
    if ai_model is None:
        speed = trigger.get('speed_delta', 0)
        action = "BUY" if speed < 0 else "SELL"
        return action, {"confidence": 0.0, "speed": speed}, fixed_volume, False
        
    context_data = payload['context']
    temporal = payload['temporal']
    dom = payload['dom']
    
    action = "BUY" if trigger['speed_delta'] < 0 else "SELL"
    is_buy = 1 if action == "BUY" else 0
    
    # --- TIER 1: FRONT DOOR REGIME FILTER ---
    regime = int(context_data.get('macro_regime_state', 1))
    regime_blocked = False
    
    if regime == 0 and action == "SELL":
        regime_blocked = True
        print("🔭 WATCHTOWER: Regime 0 (Bull) -> Blocked counter-trend SELL")
    elif regime == 2 and action == "BUY":
        regime_blocked = True
        print("🔭 WATCHTOWER: Regime 2 (Bear) -> Blocked counter-trend BUY")
    # ----------------------------------------
    
    bids = dom['bid_sizes']
    asks = dom['ask_sizes']
    total_liq = sum(bids) + sum(asks)
    if total_liq == 0: total_liq = 1
    
    dom_features = {}
    for i, b in enumerate(bids): dom_features[f'bid_norm_{i}'] = b / total_liq
    for i, a in enumerate(asks): dom_features[f'ask_norm_{i}'] = a / total_liq

    feature_dict = {
        **trigger, **context_data, 
        'hour': temporal['hour'], 'day_of_week': temporal['day_of_week'], 'is_buy': is_buy,
        **dom_features
    }
    
    feature_dict.pop('sma_1m', None)
    feature_dict.pop('sma_5m', None)
    
    df_live = pd.DataFrame([feature_dict])
    
    probabilities = ai_model.predict_proba(df_live)
    win_confidence = probabilities[0][1]
    
    custom_metrics = {
        "confidence": float(win_confidence),
        "speed": trigger.get('speed_delta', 0),
        "absorption": trigger.get('absorption_ratio', 0),
        "macro_regime": regime,
        "pa_5m_range": float(context_data.get('pa_5m_range', 0.0))
    }

    # --- DYNAMIC AI CONFIDENCE FILTER ---
    min_conf = config.get('ml_pipeline', {}).get('alpha_filter', {}).get('min_entry_confidence', 0.60)
    ai_blocked = win_confidence < min_conf
    
    final_blocked = bool(ai_blocked or regime_blocked)
    
    return action, custom_metrics, fixed_volume, final_blocked

def run_ml_brain():
    print("🧠 Starting ML Router Brain (Fixed Sizing + Dynamic Threshold)...")
    load_config_and_model() 
    db = Database()
    context = zmq.Context()
    
    receiver_socket = context.socket(zmq.REP)
    receiver_socket.bind(f"tcp://*:{config['system']['zmq_brain_port']}")
    
    manager_socket = context.socket(zmq.REQ)
    manager_socket.connect(f"tcp://localhost:{config['system']['zmq_port']}")

    print("✅ Listening to Quantower | Connected to MT5 Manager")

    try:
        while True:
            load_config_and_model() 
            
            message = receiver_socket.recv_string()
            receiver_socket.send_string("ACK") 
            
            payload = json.loads(message)
            symbol = payload.get('symbol', 'UNKNOWN')
            strategy_id = payload.get('strategy_id', 'UNKNOWN_STRATEGY')
            timestamp = payload.get('timestamp', 0)
            
            if strategy_id == "QT_Velocity":
                action, custom_metrics, volume, final_blocked = process_qt_velocity(payload)
            else:
                continue

            volume = round(float(volume), 2) if volume is not None else 0.0

            payload['ai_decision'] = {
                "confidence": custom_metrics.get("confidence", 0),
                "blocked": final_blocked,
                "volume": volume
            }

            if final_blocked:
                print(f"🚫 BLOCKED by AI | Confidence: {payload['ai_decision']['confidence']*100:.1f}%")
                ml_id = int(time.time() * 1000000)
                db.insert_ml_snapshot(strategy_id, symbol, timestamp, payload, explicit_id=ml_id)
                continue 
                
            print(f"✅ APPROVED by AI | Confidence: {payload['ai_decision']['confidence']*100:.1f}% -> {volume} Lots")

            ml_id = int(time.time() * 1000000) 
            custom_metrics["ml_feature_id"] = ml_id
            
            trade_command = {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "action": action,
                "volume": volume, 
                "extra_metrics": custom_metrics
            }

            manager_socket.send_json(trade_command)
            db.insert_ml_snapshot(strategy_id, symbol, timestamp, payload, explicit_id=ml_id)
            mt5_reply = manager_socket.recv_string()
            print(f"MT5 Reply: {mt5_reply}")

    except KeyboardInterrupt:
        print("\nShutting down ML Brain.")
    finally:
        receiver_socket.close()
        manager_socket.close()
        context.term()

if __name__ == "__main__":
    run_ml_brain()