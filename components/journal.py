import streamlit as st
import pandas as pd
from pandas.api.types import is_numeric_dtype
from components.database import Database

def render_journal_tab():
    st.header("🗄️ Trade Database (SQLite)")
    
    db = Database()
    trades = db.fetch_trades(limit=1000)
    
    if not trades:
        st.info("Database is empty. Waiting for closed trades...")
        return

    # 1. Load Data
    df = pd.DataFrame(trades)
    
    # 2. Expand JSON Data (The "Unpacking" Magic)
    # This turns {"momentum": 15} into a real column named "momentum"
    if 'meta_json' in df.columns:
        meta_list = df['meta_json'].tolist()
        meta_df = pd.json_normalize(meta_list)
        df = pd.concat([df.drop('meta_json', axis=1), meta_df], axis=1)

    # --- STANDARD FILTERS ---
    with st.expander("🔎 Filter Options", expanded=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            strategies = list(df['strategy_id'].unique())
            sel_strat = st.multiselect("Strategy", strategies, default=strategies)
        with c2:
            reasons = list(df['close_reason'].unique())
            sel_reason = st.multiselect("Exit Reason", reasons, default=reasons)
        with c3:
            # Date Filter (Optional implementation)
            st.caption(f"Showing last {len(df)} trades")

        # --- DYNAMIC METRIC FILTERS ---
        # This loop finds ANY new numeric column (like 'momentum') and makes a slider
        standard_cols = ['ticket', 'strategy_id', 'symbol', 'action', 'open_time', 'close_time', 
                         'open_price', 'close_price', 'sl', 'tp', 'pnl', 'commission', 'swap', 
                         'close_reason', 'duration_sec']
        
        dynamic_cols = [c for c in df.columns if c not in standard_cols]
        
        if dynamic_cols:
            st.divider()
            st.caption("✨ Strategy-Specific Metrics detected:")
            dc_cols = st.columns(len(dynamic_cols) if len(dynamic_cols) < 4 else 4)
            
            for i, col in enumerate(dynamic_cols):
                # Only make sliders for numbers
                if is_numeric_dtype(df[col]):
                    with dc_cols[i % 4]:
                        min_val = float(df[col].min())
                        max_val = float(df[col].max())
                        
                        # If all values are the same, don't show slider
                        if min_val < max_val:
                            user_range = st.slider(
                                f"{col.replace('_', ' ').title()}", 
                                min_value=min_val, 
                                max_value=max_val, 
                                value=(min_val, max_val)
                            )
                            # Apply the filter instantly
                            df = df[(df[col] >= user_range[0]) & (df[col] <= user_range[1])]

    # Apply Standard Filters
    if sel_strat:
        df = df[df['strategy_id'].isin(sel_strat)]
    if sel_reason:
        df = df[df['close_reason'].isin(sel_reason)]

    # --- METRICS ROW ---
    if not df.empty:
        total_pnl = df['pnl'].sum()
        win_count = len(df[df['pnl'] > 0])
        win_rate = (win_count / len(df)) * 100 if len(df) > 0 else 0
        
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total PnL", f"${total_pnl:,.2f}")
        m2.metric("Win Rate", f"{win_rate:.1f}%")
        m3.metric("Trade Count", len(df))
        m4.metric("Avg Duration", f"{df['duration_sec'].mean():.1f}s")

        # --- DATA TABLE ---
        # Define column order: Ticket -> Strategy -> PnL -> Custom Metrics -> The rest
        base_cols = ['ticket', 'strategy_id', 'pnl', 'close_reason']
        # Grab dynamic columns that exist in the filtered dataframe
        found_dynamic = [c for c in dynamic_cols if c in df.columns]
        
        final_cols = base_cols + found_dynamic + ['duration_sec', 'open_time']
        
        # Ensure we only ask for columns that actually exist
        valid_cols = [c for c in final_cols if c in df.columns]

        st.dataframe(
            df[valid_cols].style.background_gradient(subset=['pnl'], cmap='RdYlGn', vmin=-10, vmax=10),
            use_container_width=True,
            hide_index=True
        )
    else:
        st.warning("No trades match your filters.")