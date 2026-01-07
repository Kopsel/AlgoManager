import streamlit as st
import time
import MetaTrader5 as mt5
from datetime import datetime, timedelta

# Import Components
from components.utils import load_config, init_mt5
from components.live_monitor import render_live_panel
from components.strategy_lab import render_strategy_lab
from components.history import render_history_tab
from components.journal import render_journal_tab  # <--- NEW COMPONENT
from components.analytics import render_analytics_tab

st.set_page_config(page_title="Algo Command", layout="wide")

# --- SESSION STATE INITIALIZATION ---
if 'history_data' not in st.session_state:
    st.session_state.history_data = []  
if 'session_full_history' not in st.session_state:
    st.session_state.session_full_history = [] 

# Initialize Reset Threshold to CURRENT LATEST TICKET on startup
if 'reset_ticket_threshold' not in st.session_state:
    config = load_config()
    if config:
        path = config['system'].get('mt5_terminal_path')
        if init_mt5(path):
            now = datetime.now()
            # Look back 7 days to find the very last ticket ID
            recents = mt5.history_deals_get(now - timedelta(days=7), now + timedelta(days=1))
            if recents and len(recents) > 0:
                st.session_state.reset_ticket_threshold = recents[-1].ticket
            else:
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
            
            now = datetime.now()
            deals = mt5.history_deals_get(now - timedelta(days=7), now + timedelta(days=1))
            if deals and len(deals) > 0:
                max_ticket = max(d.ticket for d in deals)
                st.session_state.reset_ticket_threshold = max_ticket
            
            st.success("Session Reset!")
            st.rerun()

    # --- CALCULATE METRICS (GLOBAL EXPOSURE) ---
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

    # Determine Direction String & Color
    if global_net_lots > 0:
        direction_str = "LONG 🐂"
        delta_color = "normal" # Green (Positive delta)
    elif global_net_lots < 0:
        direction_str = "SHORT 🐻"
        delta_color = "inverse" # Red (Negative delta)
    else:
        direction_str = "FLAT ⚪"
        delta_color = "off"

    # --- TOP METRICS ROW ---
    if acc:
        kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)
        
        # 1. Money Stats
        kpi1.metric("Balance", f"${acc.balance:,.2f}")
        kpi2.metric("Equity", f"${acc.equity:,.2f}", delta=f"{acc.equity - acc.balance:.2f}")
        
        # 2. Strategy Count
        active_count = sum(1 for s in strategies.values() if s['enabled'])
        kpi3.metric("Active Bots", f"{active_count} / {len(strategies)}")

        # 3. Risk / Exposure Stats
        kpi4.metric("Open Positions", f"{global_total_open}")
        kpi5.metric("Net Direction", direction_str, f"{global_net_lots:+.2f} Lots", delta_color=delta_color)
        kpi6.metric("Position Delta", f"{global_net_count:+}", help="Positive = More Buys, Negative = More Sells")

    # --- TABS (Updated with Journal) ---
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["📈 Live Pulse", "⚙️ Strategy Lab", "📜 History", "🗄️ Journal", "📊 Analytics"])

    with tab1:
        render_live_panel(strategies, config)

    with tab2:
        render_strategy_lab(strategies, config)

    with tab3:
        render_history_tab(strategies)
        
    with tab4:
        # [NEW] This renders the SQLite Table
        render_journal_tab()
    
    with tab5:
        render_analytics_tab()

    # Auto-Refresh Logic (Poll every 1s)
    time.sleep(1)
    st.rerun()

if __name__ == "__main__":
    main()