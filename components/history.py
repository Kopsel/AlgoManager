import streamlit as st
import pandas as pd
import plotly.express as px
import MetaTrader5 as mt5
from datetime import datetime, timedelta

def render_history_tab(strategies):
    st.subheader("Historic Analysis")
    days = st.slider("Lookback Days", 1, 30, 7)
    from_date = datetime.now() - timedelta(days=days)
    
    # We apply the same Reset Filter here so 'History' tab matches the session view?
    # Usually History tab ignores reset and shows full history. Let's show FULL history here.
    history = mt5.history_deals_get(from_date, datetime.now() + timedelta(days=1))
    
    if history and len(history) > 0:
        df_hist = pd.DataFrame(list(history), columns=history[0]._asdict().keys())
        df_deals = df_hist[df_hist['entry'] == 1].copy()
        
        if not df_deals.empty:
            df_deals['time'] = pd.to_datetime(df_deals['time'], unit='s')
            magic_map = {v['magic_number']: k for k, v in strategies.items()}
            df_deals['Strategy'] = df_deals['magic'].map(magic_map).fillna("Unknown")
            
            summary = df_deals.groupby('Strategy').agg({
                'profit': 'sum',
                'ticket': 'count',
                'volume': 'sum'
            }).rename(columns={'ticket': 'Trades', 'volume': 'Total Vol'})
            
            st.table(summary)
            
            df_deals = df_deals.sort_values('time')
            df_deals['Cumulative Profit'] = df_deals.groupby('Strategy')['profit'].cumsum()
            fig = px.line(df_deals, x='time', y='Cumulative Profit', color='Strategy', markers=True)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No closed trades found.")
    else:
        st.warning("No history found.")