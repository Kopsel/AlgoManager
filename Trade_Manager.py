import zmq
import MetaTrader5 as mt5
import json
import os
import time
import sys
import signal
import traceback
from datetime import datetime

from components.database import Database

# --- CONFIG & STATE ---
CONFIG_FILE = "system_config.json"
MEMORY_FILE = "trade_memory.json"
last_config_mtime = 0
config = {} 
last_snapshot_time = 0
SNAPSHOT_INTERVAL = 60

tracked_tickets = {}
trade_metadata = {}  
trade_mfe_mae = {}   
basket_start_equity = None 

db = Database()
context = None
socket = None

def graceful_shutdown(sig, frame):
    print("\nManager: 🛑 Releasing Port 5555 and MT5...")
    global socket, context
    if socket:
        socket.setsockopt(zmq.LINGER, 0)
        socket.close()
    if context:
        context.term()
    mt5.shutdown()
    sys.exit(0)

signal.signal(signal.SIGINT, graceful_shutdown)
if os.name == 'nt':
    signal.signal(signal.SIGBREAK, graceful_shutdown)

def get_file_mtime(filepath):
    if os.path.exists(filepath): return os.path.getmtime(filepath)
    return 0

def load_config():
    global last_config_mtime, config
    if not os.path.exists(CONFIG_FILE): return False
    
    current_mtime = get_file_mtime(CONFIG_FILE)
    if current_mtime > last_config_mtime:
        for attempt in range(5):
            try:
                with open(CONFIG_FILE, "r") as f:
                    new_config = json.load(f)
                    if 'system' in new_config and 'strategies' in new_config:
                        config = new_config
                        last_config_mtime = current_mtime
                        # print("Manager: Configuration Loaded.") <-- SILENCED SPAM
                        return True
            except Exception as e:
                time.sleep(0.05)
        print("Manager: Config Read Failed after 5 retries.")
        return False
    return bool(config)

def save_trade_memory():
    """Saves active tickets and their AI metadata to disk to survive crashes."""
    try:
        memory_state = {
            "tracked_tickets": tracked_tickets,
            "trade_metadata": trade_metadata,
            "trade_mfe_mae": trade_mfe_mae
        }
        with open(MEMORY_FILE, "w") as f:
            json.dump(memory_state, f, indent=4)
    except Exception as e:
        print(f"Manager: Failed to save trade memory: {e}")

def load_trade_memory():
    """Restores active tickets and metadata from disk on startup."""
    global tracked_tickets, trade_metadata, trade_mfe_mae
    if not os.path.exists(MEMORY_FILE): return
    
    try:
        with open(MEMORY_FILE, "r") as f:
            memory_state = json.load(f)
            # JSON converts integer dictionary keys to strings, so we must convert them back
            tracked_tickets = {int(k): v for k, v in memory_state.get("tracked_tickets", {}).items()}
            trade_metadata = {int(k): v for k, v in memory_state.get("trade_metadata", {}).items()}
            trade_mfe_mae = {int(k): v for k, v in memory_state.get("trade_mfe_mae", {}).items()}
        print(f"Manager: Restored {len(tracked_tickets)} active tickets from memory.")
    except Exception as e:
        print(f"Manager: Failed to load trade memory: {e}")

def connect_mt5():
    if not config:
        if not load_config(): return False
    sys_conf = config.get('system', {})
    path = sys_conf.get('mt5_terminal_path')
    expected_account = sys_conf.get('authorized_account_number')
    
    if path and os.path.exists(path):
        if not mt5.initialize(path=path): return False
    else:
        if not mt5.initialize(): return False

    current_info = mt5.account_info()
    if current_info is None: return False
    if expected_account and current_info.login != expected_account:
        mt5.shutdown()
        return False
    print(f"Manager: Connected to Account {current_info.login}")
    return True

def sync_positions_on_startup():
    if not config: load_config()
    strategies = config.get('strategies', {})
    magic_map = {v['magic_number']: k for k, v in strategies.items()}
    positions = mt5.positions_get()
    count = 0
    if positions:
        for pos in positions:
            if pos.magic in magic_map:
                strat_id = magic_map[pos.magic]
                tracked_tickets[pos.ticket] = strat_id
                trade_mfe_mae[pos.ticket] = {'mfe': pos.profit, 'mae': pos.profit}
                count += 1
    print(f"Manager: Synced {count} existing positions.")

def update_mfe_mae():
    positions = mt5.positions_get()
    if not positions: return
    for pos in positions:
        ticket = pos.ticket
        if ticket in trade_mfe_mae:
            if pos.type == mt5.POSITION_TYPE_BUY:
                current_point_dist = pos.price_current - pos.price_open
            else:
                current_point_dist = pos.price_open - pos.price_current
            
            if current_point_dist > trade_mfe_mae[ticket]['mfe']:
                trade_mfe_mae[ticket]['mfe'] = current_point_dist
            if current_point_dist < trade_mfe_mae[ticket]['mae']:
                trade_mfe_mae[ticket]['mae'] = current_point_dist

def record_equity_snapshot():
    global last_snapshot_time
    if time.time() - last_snapshot_time < SNAPSHOT_INTERVAL: return
    acc = mt5.account_info()
    if not acc: return
    positions = mt5.positions_get()
    count = len(positions) if positions else 0
    
    strategies = config.get('strategies', {})
    magic_map = {v['magic_number']: k for k, v in strategies.items()}
    strat_pl = {k: 0.0 for k in strategies.keys()} 
    
    if positions:
        for pos in positions:
            s_id = magic_map.get(pos.magic, "Manual/Other")
            strat_pl[s_id] = strat_pl.get(s_id, 0.0) + pos.profit + pos.swap
            
    db.log_equity_snapshot(acc.balance, acc.equity, count, strat_pl)
    last_snapshot_time = time.time()

def check_closed_trades():
    live_positions = mt5.positions_get()
    if live_positions is None: return 
    live_ticket_ids = {p.ticket for p in live_positions}
    missing_tickets = [t for t in tracked_tickets.keys() if t not in live_ticket_ids]
    
    for ticket in missing_tickets:
        strat_id = tracked_tickets[ticket]
        deals = mt5.history_deals_get(position=ticket)
        if deals is None or len(deals) == 0: continue
            
        entry_deal = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_IN), None)
        exit_deal = next((d for d in deals if d.entry in [mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT]), None)
        
        if exit_deal:
            meta = trade_metadata.get(ticket, {})
            ml_id = meta.get('ml_feature_id')
            
            mfe_mae_data = trade_mfe_mae.get(ticket, {'mfe': 0.0, 'mae': 0.0})
            mfe_val = round(mfe_mae_data['mfe'], 2)
            mae_val = round(mfe_mae_data['mae'], 2)
            
            net_pl = exit_deal.profit + exit_deal.swap + exit_deal.commission
            duration = 0
            open_price = 0
            open_time = datetime.fromtimestamp(exit_deal.time)

            action = "BUY" if entry_deal and entry_deal.type == mt5.DEAL_TYPE_BUY else "SELL"
            
            if entry_deal:
                duration = exit_deal.time - entry_deal.time
                open_price = entry_deal.price
                open_time = datetime.fromtimestamp(entry_deal.time)

            if action == "BUY":
                pnl_pts = exit_deal.price - open_price
            else:
                pnl_pts = open_price - exit_deal.price

            reason = "Unknown"
            if exit_deal.reason in [mt5.DEAL_REASON_CLIENT, mt5.DEAL_REASON_EXPERT]:
                if exit_deal.comment and "Basket Close" in exit_deal.comment:
                    reason = "Basket Close"
                else:
                    reason = "Manual Close"
            elif exit_deal.reason == mt5.DEAL_REASON_SL: reason = "Stop Loss"
            elif exit_deal.reason == mt5.DEAL_REASON_TP: reason = "Take Profit"

            print(f"💰 Closed: {strat_id} | ${net_pl:.2f} ({pnl_pts:.2f} pts) | {reason} | MFE: {mfe_val} pts / MAE: {mae_val} pts")
            
            sl_mem = meta.get('sl_price_memory', 0.0)
            tp_mem = meta.get('tp_price_memory', 0.0)

            trade_record = {
                "ticket": ticket,
                "ml_feature_id": ml_id,
                "strategy_id": strat_id,
                "symbol": exit_deal.symbol,
                "action": action,
                "open_time": open_time,
                "close_time": datetime.fromtimestamp(exit_deal.time),
                "duration": duration,
                "open_price": open_price,
                "close_price": exit_deal.price,
                "sl": sl_mem,  
                "tp": tp_mem,  
                "net_pnl": round(net_pl, 2),
                "pnl_points": round(pnl_pts, 2),
                "commission": exit_deal.commission,
                "swap": exit_deal.swap,
                "reason": reason,
                "mfe": mfe_val,
                "mae": mae_val
            }
            
            # --- FIX: ISOLATE DB ERROR AND FORCE MEMORY CLEANUP ---
            try:
                db.log_trade(trade_record)
            except Exception as e:
                if "UNIQUE" in str(e):
                    print(f"Manager: Trade {ticket} was already logged. Skipping.")
                else:
                    print(f"Manager: DB Warning (Log Trade): {e}")
            finally:
                # Always safely delete it from RAM so we don't get stuck in a loop
                if ticket in tracked_tickets: del tracked_tickets[ticket]
                if ticket in trade_metadata: del trade_metadata[ticket]
                if ticket in trade_mfe_mae: del trade_mfe_mae[ticket]
                
                # Immediately write the clean state to disk
                save_trade_memory()

def close_all_positions(reason="Global Basket Trigger"):
    positions = mt5.positions_get()
    if positions is None or len(positions) == 0: return

    print(f"\n--- CLOSING ALL ({reason}) ---")
    for pos in positions:
        tick = mt5.symbol_info_tick(pos.symbol)
        if not tick: continue
        type_close = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if type_close == mt5.ORDER_TYPE_SELL else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": pos.ticket,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": type_close,
            "price": price,
            "magic": pos.magic,
            "comment": "Basket Close",
        }
        mt5.order_send(request)

def check_basket_logic():
    global basket_start_equity
    load_config() 
    
    risk_cfg = config.get('risk_management', {}).get('emergency_protocols', {})
    if risk_cfg.get('system_locked', False): 
        return
        
    risk = config.get('risk_management', {})
    
    if not risk.get('basket_enabled', False):
        basket_start_equity = None 
        return

    acc = mt5.account_info()
    if acc is None: return

    positions = mt5.positions_get()
    
    if positions is None or len(positions) == 0:
        if basket_start_equity is not None or risk.get('active_basket_anchor_usd') is not None:
            basket_start_equity = None
            config['risk_management']['active_basket_anchor_usd'] = None
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            print("Manager: 🧹 Basket cleared from memory and config.")
        return 

    current_equity = acc.equity

    if basket_start_equity is None:
        saved_anchor = risk.get('active_basket_anchor_usd')
        
        if saved_anchor is not None:
            basket_start_equity = saved_anchor
            print(f"Manager: 🔄 Resumed Active Basket. Original Anchor Equity: ${basket_start_equity:.2f}")
        else:
            basket_start_equity = current_equity
            config['risk_management']['active_basket_anchor_usd'] = basket_start_equity
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            print(f"Manager: 🎯 New Basket Started. Anchor Equity saved to config: ${basket_start_equity:.2f}")

    tp_limit = risk.get('basket_take_profit_usd')
    if tp_limit and tp_limit > 0:
        target_amount = basket_start_equity + tp_limit
        if current_equity >= target_amount:
            print(f"\n!!! BASKET TP HIT (Equity: ${current_equity:.2f} >= Target: ${target_amount:.2f}) !!!")
            close_all_positions(reason="Equity Target Reached")
            
            basket_start_equity = None
            config['risk_management']['active_basket_anchor_usd'] = None
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)

def manage_grids():
    """Handles the 8-Minute Pivot, Regime Freezing, and Continuous DCA Averaging"""
    
    # 1. Check for Emergency Locks
    risk_cfg = config.get('risk_management', {}).get('emergency_protocols', {})
    if risk_cfg.get('system_locked', False): 
        return 

    grid_cfg = config.get('risk_management', {}).get('grid_recovery')
    if not grid_cfg: return
    
    positions = mt5.positions_get()
    if not positions: return
    
    longs = [p for p in positions if p.type == mt5.POSITION_TYPE_BUY]
    shorts = [p for p in positions if p.type == mt5.POSITION_TYPE_SELL]
    
    # =================================================================
    # 🛡️ THE TIER 2 REGIME FILTER (LIVE DATABASE LOOKUP)
    # =================================================================
    try:
        latest_regimes = db.fetch_regimes(limit=1)
        # If DB has data, use the newest regime. If not, default to 1 (Chop).
        current_live_regime = latest_regimes[0]['regime'] if latest_regimes else 1 
    except Exception as e:
        print(f"Manager: DB Regime Fetch Failed ({e}). Defaulting to Chop (1).")
        current_live_regime = 1 
    # =================================================================

    def process_basket(basket_positions, direction):
        if not basket_positions: return
        anchor = min(basket_positions, key=lambda p: p.time)
        
        symbol = anchor.symbol
        tick = mt5.symbol_info_tick(symbol)
        if not tick: return
        
        # Synchronize with MT5 Broker time to prevent Timezone Bugs
        broker_now = tick.time
        time_open_mins = (broker_now - anchor.time) / 60.0
        
        # 1. Has the initial scalp expired?
        if time_open_mins >= grid_cfg['activation_timer_minutes']:
            meta = trade_metadata.get(anchor.ticket, {})
            
            # =================================================================
            # 🛑 APPLY THE REGIME FREEZE LOGIC
            # =================================================================
            aligned = False
            if direction == "LONG" and current_live_regime in grid_cfg['regime_alignment']['allow_long_grids_in']: 
                aligned = True
            if direction == "SHORT" and current_live_regime in grid_cfg['regime_alignment']['allow_short_grids_in']: 
                aligned = True
            
            if not aligned:
                # The live regime contradicts our basket direction. 
                # We return immediately, freezing the grid to protect equity.
                return 
            # =================================================================

            # 3. Dynamic Volatility Math (ATR x Confidence)
            conf = meta.get('confidence', 0.50)
            scale_cfg = grid_cfg['continuous_scaling']
            
            # Clamp confidence and calculate ratio
            conf_clamped = max(scale_cfg['baseline_confidence'], min(scale_cfg['max_confidence'], conf))
            ratio = (conf_clamped - scale_cfg['baseline_confidence']) / (scale_cfg['max_confidence'] - scale_cfg['baseline_confidence'])
            
            # Fetch Live MT5 Candles for ATR
            atr_period = scale_cfg.get('atr_period', 14)
            tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}
            tf = tf_map.get(scale_cfg.get('atr_timeframe', 'M5'), mt5.TIMEFRAME_M5)
            
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, atr_period + 1)
            atr_val = scale_cfg.get('fallback_step_points', 3.0)
            
            if rates is not None and len(rates) >= atr_period + 1:
                highs = [r[2] for r in rates]
                lows = [r[3] for r in rates]
                closes = [r[4] for r in rates]
                
                tr_list = []
                for i in range(1, len(rates)):
                    hl = highs[i] - lows[i]
                    hc = abs(highs[i] - closes[i-1])
                    lc = abs(lows[i] - closes[i-1])
                    tr_list.append(max(hl, hc, lc))
                
                atr_val = sum(tr_list[-atr_period:]) / atr_period

            # Apply Confidence Multiplier to ATR
            min_mult = scale_cfg.get('min_atr_multiplier', 0.5)
            max_mult = scale_cfg.get('max_atr_multiplier', 2.0)
            
            multiplier = min_mult + ratio * (max_mult - min_mult)
            step_pts = atr_val * multiplier
            
            # Calculate extreme open price to measure step distance
            if direction == "LONG":
                worst_price = min(p.price_open for p in basket_positions)
                should_add = tick.ask <= (worst_price - step_pts)
            else:
                worst_price = max(p.price_open for p in basket_positions)
                should_add = tick.bid >= (worst_price + step_pts)
            
            # 4. Execute Grid Step (Using strict fixed anchor volume)
            if should_add:
                fixed_vol = anchor.volume 
                
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": fixed_vol,
                    "type": mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL,
                    "price": tick.ask if direction == "LONG" else tick.bid,
                    "magic": anchor.magic,
                    "comment": f"Grid ({step_pts:.1f}pt)",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                
                res = mt5.order_send(req)
                if res.retcode == mt5.TRADE_RETCODE_DONE:
                    new_ticket = res.order
                    print(f"🕸️ GRID ADDED: {direction} | Conf: {conf:.2f} | Step: {step_pts:.1f} | Vol: {fixed_vol}")
                    
                    # --- THE FIX: TRACK THE NEW GRID BULLET ---
                    # 1. Figure out which strategy this belongs to
                    strat_id = tracked_tickets.get(anchor.ticket, str(anchor.magic))
                    
                    # 2. Add it to the tracking loops
                    tracked_tickets[new_ticket] = strat_id
                    trade_mfe_mae[new_ticket] = {'mfe': 0.0, 'mae': 0.0}
                    
                    # 3. Inherit the anchor's metadata (this passes down the ml_feature_id!)
                    trade_metadata[new_ticket] = meta.copy()
                    trade_metadata[new_ticket]['sl_price_memory'] = 0.0 # Grids don't use hard SLs
                    trade_metadata[new_ticket]['tp_price_memory'] = 0.0 # TP is managed by the basket loop
                    
                    save_trade_memory()
                    # ------------------------------------------
                    
            # 5. Update Basket TP (Remove SLs)
            total_vol = sum(p.volume for p in basket_positions)
            if total_vol > 0:
                avg_price = sum(p.price_open * p.volume for p in basket_positions) / total_vol
                target_tp = avg_price + grid_cfg['basket_tp_points'] if direction == "LONG" else avg_price - grid_cfg['basket_tp_points']
                target_tp = round(target_tp, mt5.symbol_info(symbol).digits)
                
                for p in basket_positions:
                    if abs(p.tp - target_tp) > 0.01 or p.sl != 0.0:
                        mt5.order_send({
                            "action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": symbol,
                            "tp": target_tp, "sl": 0.0 
                        })

    process_basket(longs, "LONG")
    process_basket(shorts, "SHORT")

def execute_trade(signal_data):
    load_config()
    
    # --- EMERGENCY SYSTEM LOCK CHECK ---
    risk_cfg = config.get('risk_management', {}).get('emergency_protocols', {})
    if risk_cfg.get('system_locked', False): 
        return "Manager: REJECTED (Emergency System Lock Active)"
        
    # --- FIX: TRANSLATE QUANTOWER SYMBOL TO MT5 SYMBOL ---
    qt_symbol = signal_data['symbol']
    sys_cfg = config.get('system', {})
    symbol = sys_cfg.get('symbol_mapping', {}).get(qt_symbol, qt_symbol)

    action = signal_data['action']

    # --- CONCURRENCY HEDGE CHECK ---
    positions = mt5.positions_get(symbol=symbol)
    if positions:
        if action == "BUY" and any(p.type == mt5.POSITION_TYPE_BUY for p in positions):
            return "Manager: REJECTED (Long Basket Active)"
        if action == "SELL" and any(p.type == mt5.POSITION_TYPE_SELL for p in positions):
            return "Manager: REJECTED (Short Basket Active)"

    strategies = config.get('strategies', {})
    strat_id = signal_data['strategy_id']
    if strat_id not in strategies: return "Manager: Unknown Strategy"
    
    settings = strategies[strat_id]
    if not settings['enabled']: return "Manager: Strategy Disabled"
    
    magic = settings['magic_number']
    volume = round(float(signal_data.get('volume', settings['volume'])), 2)
    
    limits = settings.get('trade_limits', {})
    sl_points = limits.get('sl_points', 0)
    tp_points = float(signal_data.get('dynamic_tp', limits.get('tp_points', 1.0)))
    
    sym_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not sym_info or not tick: return "Manager: No Data"
    
    digits = sym_info.digits
    tick_size = sym_info.trade_tick_size
    if tick_size == 0: tick_size = sym_info.point # Fallback
    
    def norm_price(raw_p):
        return round(round(raw_p / tick_size) * tick_size, digits)
    
    order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if action == "BUY" else tick.bid
    
    # 1. Send the initial order WITHOUT SL or TP
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "magic": magic,
        "comment": strat_id,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE: 
        return f"Manager: Failed ({result.comment})"
    
    # --- 2. THE UPGRADED BULDOZER RETRY LOOP ---
    tp_anchored = False
    actual_fill_price = result.price 
    sl_price = 0.0
    tp_price = 0.0
    
    for attempt in range(10): 
        time.sleep(0.2) 
        
        pos_check = mt5.positions_get(ticket=result.order)
        if not pos_check or len(pos_check) == 0:
            tp_anchored = True # Position was already closed
            break
            
        actual_fill_price = pos_check[0].price_open
            
        if sl_points > 0:
            raw_sl = actual_fill_price - sl_points if action == "BUY" else actual_fill_price + sl_points
            sl_price = norm_price(raw_sl)
        else: sl_price = 0.0
            
        if tp_points > 0:
            raw_tp = actual_fill_price + tp_points if action == "BUY" else actual_fill_price - tp_points
            tp_price = norm_price(raw_tp)
        else: tp_price = 0.0

        if sl_price > 0 or tp_price > 0:
            mod_request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": result.order,
                "symbol": symbol,
                "sl": sl_price,
                "tp": tp_price
            }
            mod_result = mt5.order_send(mod_request)
            
            if mod_result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"🎯 TP Successfully Anchored to {tp_price} (Attempt {attempt+1})")
                tp_anchored = True
                break
            elif mod_result.retcode == 10016:
                tick_now = mt5.symbol_info_tick(symbol)
                sym_info_live = mt5.symbol_info(symbol)
                
                if tick_now and sym_info_live:
                    min_dist = max((tick_now.ask - tick_now.bid), (sym_info_live.trade_stops_level * sym_info_live.point))
                    
                    if action == "BUY" and tp_price > 0 and tick_now.bid >= (tp_price - min_dist):
                        print(f"🚀 PROXIMITY PROFIT: Closing immediately.")
                        mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "position": result.order, "symbol": symbol, "volume": pos_check[0].volume, "type": mt5.ORDER_TYPE_SELL, "price": tick_now.bid, "magic": magic, "comment": "Proximity TP Close"})
                        tp_anchored = True
                        break
                        
                    elif action == "SELL" and tp_price > 0 and tick_now.ask <= (tp_price + min_dist):
                        print(f"🚀 PROXIMITY PROFIT: Closing immediately.")
                        mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "position": result.order, "symbol": symbol, "volume": pos_check[0].volume, "type": mt5.ORDER_TYPE_BUY, "price": tick_now.ask, "magic": magic, "comment": "Proximity TP Close"})
                        tp_anchored = True
                        break
                        
                    elif sl_price > 0 and ((action == "BUY" and tick_now.bid <= (sl_price + min_dist)) or (action == "SELL" and tick_now.ask >= (sl_price - min_dist))):
                        print(f"💥 PROXIMITY STOP: Closing immediately to protect equity.")
                        mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "position": result.order, "symbol": symbol, "volume": pos_check[0].volume, "type": mt5.ORDER_TYPE_SELL if action == "BUY" else mt5.ORDER_TYPE_BUY, "price": tick_now.bid if action == "BUY" else tick_now.ask, "magic": magic, "comment": "Proximity SL Close"})
                        tp_anchored = True
                        break
                        
            print(f"⚠️ Modify Failed (Attempt {attempt+1}): {mod_result.comment} (Code: {mod_result.retcode})")
        else:
            tp_anchored = True 
            break

    if not tp_anchored:
        print(f"🚨 CRITICAL: Failed to anchor TP after 10 attempts! Position {result.order} is naked!")

    # --- 3. Logging & Memory ---
    tracked_tickets[result.order] = strat_id
    trade_mfe_mae[result.order] = {'mfe': 0.0, 'mae': 0.0}
    
    meta = signal_data.get('extra_metrics', {})
    meta['sl_price_memory'] = sl_price
    meta['tp_price_memory'] = tp_price
    trade_metadata[result.order] = meta
        
    return f"Manager: OPENED {action} (Ticket: {result.order}) | Vol: {volume} | Exact TP: {tp_price}"

def run_manager():
    global socket, context
    if not load_config(): return
    sys_conf = config.get('system', {})
    zmq_port = sys_conf.get('zmq_port', 5555)

    context = zmq.Context()
    socket = context.socket(zmq.REP)
    try: 
        socket.bind(f"tcp://*:{zmq_port}")
    except zmq.ZMQError as e:
        print(f"CRITICAL: Port {zmq_port} is busy.")
        return

    socket.setsockopt(zmq.RCVTIMEO, 100) 
    if not connect_mt5(): return
    print(f"--- Manager Listening on Port {zmq_port} ---")
    
    db.initialize()
    load_trade_memory()
    sync_positions_on_startup()

    while True:
        try:
            try:
                msg = socket.recv_json(flags=zmq.NOBLOCK)
                resp = execute_trade(msg)
                socket.send_string(resp)
            except zmq.Again:
                pass

            update_mfe_mae() 
            check_closed_trades()
            check_basket_logic()
            manage_grids() 
            record_equity_snapshot()
            time.sleep(0.01)

        except KeyboardInterrupt: 
            graceful_shutdown(None, None)
        except Exception: 
            traceback.print_exc()

if __name__ == "__main__":
    run_manager()