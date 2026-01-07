import json
import os
import MetaTrader5 as mt5
import streamlit as st

CONFIG_FILE = "system_config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE): return {}
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def save_config(new_config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(new_config, f, indent=2)
    st.success("Configuration Saved! (Updates apply automatically ⚡)")

def init_mt5(path):
    if not mt5.initialize(path=path): return False
    return True

def get_strategy_name(magic, strategies):
    for name, data in strategies.items():
        if data['magic_number'] == magic: return name
    return str(magic)