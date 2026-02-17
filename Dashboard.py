import streamlit as st
import time
import MetaTrader5 as mt5
import pandas as pd 
import json 
from datetime import datetime, timedelta

# Import Components
from components.utils import load_config, init_mt5
from components.live_monitor import render_live_panel
from components.strategy_lab import render_strategy_lab
from components.history import render_history_tab
from components.journal import render_journal_tab 
from components.analytics import render_analytics_tab
from components.database import Database

st.set_page_config(page_title="Algo Command", layout="wide")

# --- 1. DATABASE STATE RESTORATION ---
if 'data_restored' not in st.session_state:
    try:
        db = Database()
        
        # A. Restore Daily Stats (PnL & Count)
        stats = db.get_todays_stats()
        st.session_state['daily_pnl'] = stats['daily_pnl']
        st.session_state['daily_trades'] = stats['trade_count']
        
        # B. Restore Equity Curve
        equity_raw = db.fetch_equity_history(limit=200) 
        equity_clean = []
        
        for row in equity_raw:
            d = dict(row)
            
            clean_row = {
                'Balance': d['balance'],
                'Equity': d['equity'],
            }
            
            # Unpack Strategy Performance
            if 'strategy_performance' in d and d['strategy_performance']:
                try:
                    strat_data = json.loads(d['strategy_performance'])
                    for k, v in strat_data.items():
                        clean_row[f"PL_{k}"] = v
                except:
                    pass 
            
            # Timezone Fix
            if 'timestamp' in d:
                try:
                    ts_pandas = pd.to_datetime(d['timestamp'])
                    ts_python = ts_pandas.to_pydatetime()
                    clean_row['time_unix'] = ts_python.timestamp()
                    clean_row['time'] = ts_python.strftime('%H:%M:%S')
                except Exception:
                    continue 
            
            equity_clean.append(clean_row)
        
        equity_clean.reverse()
        
        st.session_state['history_data'] = equity_clean
        st.session_state['session_full_history'] = equity_clean.copy()
        
        st.session_state['data_restored'] = True
        print(f"Dashboard: Restored {len(equity_clean)} Snapshots")
        
    except Exception as e:
        print(f"Dashboard Load Error: {e}")
        st.session_state['daily_pnl'] = 0.0
        st.session_state['daily_trades'] = 0
        st.session_state['history_data'] = []
        st.session_state['session_full_history'] = []

# --- 2. SESSION STATE INITIALIZATION ---
if 'history_data' not in st.session_state:
    st.session_state.history_data = []  
if 'session_full_history' not in st.session_state:
    st.session_state.session_full_history = [] 

# --- CRITICAL FIX: Initialize Reset Threshold to MIDNIGHT ---
if 'reset_ticket_threshold' not in st.session_state:
    config = load_config()
    if config:
        path = config['system'].get('mt5_terminal_path')
        if init_mt5(path):
            # 1. Define Midnight (Start of current day)
            now = datetime.now()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            
            # 2. Find the last ticket BEFORE midnight
            # We fetch history ending exactly at midnight
            history_before = mt5.history_deals_get(midnight - timedelta(days=7), midnight)
            
            if history_before and len(history_before) > 0:
                # The threshold is the last trade of yesterday
                st.session_state.reset_ticket_threshold = history_before[-1].ticket
            else:
                # If no trades before midnight (fresh account), threshold is 0
                st.session_state.reset_ticket_threshold = 0
        else:
            st.session_state.reset_ticket_threshold = 0
    else:
        st.session_state.reset_ticket_threshold = 0

def main():
    st.title("⚡ Algo Trading Command Center v2.1")
    
    config = load_config()
    if not config: return

    path = config['system'].get('mt5_terminal_path')
    if not init_mt5(path):
        st.error(f"Failed to connect to MT5 at {path}")
        return

    # --- SIDEBAR: RESET BUTTON ---
    with st.sidebar:
        st.header("Session Controls")
        if st.button("🔄 Reset Tracking Today", type="primary"):
            st.session_state.history_data = []
            st.session_state.session_full_history = []
            
            st.session_state['daily_pnl'] = 0.0
            st.session_state['daily_trades'] = 0
            
            # Reset Threshold to CURRENT latest ticket (effectively clearing today's history from view)
            now = datetime.now()
            deals = mt5.history_deals_get(now - timedelta(days=7), now + timedelta(days=1))
            if deals and len(deals) > 0:
                max_ticket = max(d.ticket for d in deals)
                st.session_state.reset_ticket_threshold = max_ticket
            
            st.success("Session View Reset!")
            st.rerun()

    # --- CALCULATE LIVE METRICS ---
    acc = mt5.account_info()
    strategies = config.get('strategies', {})
    
    positions = mt5.positions_get()
    
    global_net_lots = 0.0
    global_net_count = 0
    global_total_open = 0
    
    if positions:
        for pos in positions:
            global_total_open += 1
            if pos.type == mt5.POSITION_TYPE_BUY:
                global_net_lots += pos.volume
                global_net_count += 1
            elif pos.type == mt5.POSITION_TYPE_SELL:
                global_net_lots -= pos.volume
                global_net_count -= 1

    # Determine Direction
    if global_net_lots > 0:
        direction_str = "LONG 🐂"
        delta_color = "normal" 
    elif global_net_lots < 0:
        direction_str = "SHORT 🐻"
        delta_color = "inverse" 
    else:
        direction_str = "FLAT ⚪"
        delta_color = "off"

    # --- TOP METRICS ROW ---
    if acc:
        kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)
        
        kpi1.metric("Balance", f"${acc.balance:,.2f}")
        kpi2.metric("Equity", f"${acc.equity:,.2f}", delta=f"{acc.equity - acc.balance:.2f}")
        
        # 3. Daily PnL (Persisted)
        daily_pnl = st.session_state.get('daily_pnl', 0.0)
        daily_trades = st.session_state.get('daily_trades', 0)
        kpi3.metric("Daily PnL", f"${daily_pnl:,.2f}", f"{daily_trades} Trades")

        # 4. Risk / Exposure Stats
        kpi4.metric("Open Positions", f"{global_total_open}")
        kpi5.metric("Net Direction", direction_str, f"{global_net_lots:+.2f} Lots", delta_color=delta_color)
        kpi6.metric("Position Delta", f"{global_net_count:+}", help="Positive = More Buys, Negative = More Sells")

    # --- TABS ---
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["📈 Live Pulse", "⚙️ Strategy Lab", "📜 History", "🗄️ Journal", "📊 Analytics"])

    with tab1:
        render_live_panel(strategies, config)

    with tab2:
        render_strategy_lab(strategies, config)

    with tab3:
        render_history_tab(strategies)
        
    with tab4:
        render_journal_tab()
    
    with tab5:
        render_analytics_tab()

    time.sleep(1)
    st.rerun()

if __name__ == "__main__":
    main()