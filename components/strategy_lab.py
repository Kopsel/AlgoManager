import streamlit as st
from components.utils import save_config

def render_strategy_lab(strategies, config):
    st.subheader("Manage Strategies")
    for strat_id, settings in strategies.items():
        with st.expander(f"{strat_id} Settings"):
            with st.form(f"edit_{strat_id}"):
                c1, c2 = st.columns(2)
                with c1:
                    new_enabled = st.checkbox("Enabled", value=settings['enabled'])
                    new_vol = st.number_input("Lot Size", value=settings['volume'])
                    new_fallback = st.number_input("Threshold", value=settings['parameters'].get('fallback_threshold', 0.25))
                with c2:
                    st.write("Limits")
                    limits = settings.get('trade_limits', {})
                    new_tp_mult = st.number_input("Vol Multiplier", value=limits.get('tp_volatility_multiplier', 0.5))
                    new_cooldown = st.number_input("Cooldown (s)", value=settings['parameters'].get('cooldown_sec', 10))

                if st.form_submit_button("💾 Save"):
                    config['strategies'][strat_id]['enabled'] = new_enabled
                    config['strategies'][strat_id]['volume'] = new_vol
                    config['strategies'][strat_id]['parameters']['fallback_threshold'] = new_fallback
                    config['strategies'][strat_id]['parameters']['cooldown_sec'] = new_cooldown
                    config['strategies'][strat_id]['trade_limits']['tp_volatility_multiplier'] = new_tp_mult
                    save_config(config)