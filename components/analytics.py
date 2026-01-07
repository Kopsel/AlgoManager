import streamlit as st
import pandas as pd
import plotly.express as px
from components.database import Database

def render_analytics_tab():
    st.header("📊 Deep Performance Analytics")
    
    db = Database()
    trades = db.fetch_trades(limit=5000) # Get lots of history
    
    if not trades:
        st.info("No data available.")
        return

    df = pd.DataFrame(trades)
    
    # --- 1. DATA PREPARATION ---
    # Convert string timestamps to datetime objects
    df['close_time'] = pd.to_datetime(df['close_time'])
    
    # Extract "Features" for Filtering
    df['Day'] = df['close_time'].dt.date
    df['Weekday'] = df['close_time'].dt.day_name()
    df['Hour'] = df['close_time'].dt.hour
    
    # --- 2. AGGREGATE STATS (Daily/Weekly) ---
    c1, c2 = st.columns(2)
    
    with c1:
        st.subheader("📆 Daily Performance")
        daily_stats = df.groupby('Day')['pnl'].sum().reset_index()
        # Calculate cumulative PnL
        daily_stats['Cumulative'] = daily_stats['pnl'].cumsum()
        
        # Color chart based on profit/loss
        fig_daily = px.bar(daily_stats, x='Day', y='pnl', 
                           color='pnl', color_continuous_scale=['red', 'green'])
        st.plotly_chart(fig_daily, use_container_width=True)

    with c2:
        st.subheader("📈 Equity Curve (Closed Trades)")
        fig_cum = px.line(daily_stats, x='Day', y='Cumulative', markers=True)
        st.plotly_chart(fig_cum, use_container_width=True)

    st.divider()

    # --- 3. ADVANCED HEATMAPS (Time of Day / Weekday) ---
    st.subheader("🕰️ Time & Day Analysis")
    st.caption("When are your strategies actually making money?")
    
    col_a, col_b = st.columns(2)
    
    with col_a:
        # Group by Weekday
        day_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
        day_stats = df.groupby('Weekday')['pnl'].sum().reindex(day_order).reset_index()
        
        fig_day = px.bar(day_stats, x='Weekday', y='pnl', title="PnL by Weekday",
                         color='pnl', color_continuous_scale='RdYlGn')
        st.plotly_chart(fig_day, use_container_width=True)
        
    with col_b:
        # Group by Hour of Day
        hour_stats = df.groupby('Hour')['pnl'].sum().reset_index()
        fig_hour = px.bar(hour_stats, x='Hour', y='pnl', title="PnL by Hour of Day",
                          color='pnl', color_continuous_scale='RdYlGn')
        st.plotly_chart(fig_hour, use_container_width=True)

    # --- 4. STRATEGY COMPARISON ---
    st.divider()
    st.subheader("🤖 Strategy Comparison")
    
    strat_perf = df.groupby('strategy_id').agg({
        'pnl': 'sum',
        'ticket': 'count',
        'duration_sec': 'mean'
    }).reset_index()
    
    strat_perf['Avg Profit per Trade'] = strat_perf['pnl'] / strat_perf['ticket']
    
    st.dataframe(strat_perf.style.background_gradient(subset=['pnl'], cmap='RdYlGn'), use_container_width=True)