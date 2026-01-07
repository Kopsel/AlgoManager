import subprocess
import time
import json
import sys
import os

# --- CONFIG ---
CONFIG_FILE = "system_config.json"
MANAGER_SCRIPT = "Trade_Manager.py"
DASHBOARD_SCRIPT = "Dashboard.py"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: {CONFIG_FILE} not found.")
        sys.exit(1)
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def launch_process(command_list, title):
    """
    Launches a process in a NEW completely separate terminal window.
    """
    print(f"Launcher: Starting {title}...")
    
    # Windows specific flag to open new window
    CREATE_NEW_CONSOLE = subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
    
    try:
        p = subprocess.Popen(
            command_list,
            creationflags=CREATE_NEW_CONSOLE
        )
        return p
    except Exception as e:
        print(f"Error starting {title}: {e}")
        return None

def main():
    print("--- ALGOTRADING SYSTEM LAUNCHER ---")
    
    # 1. Load Config
    try:
        config = load_config()
    except json.JSONDecodeError as e:
        print(f"CRITICAL: system_config.json has a syntax error!\n{e}")
        input("Press Enter to exit...")
        return

    processes = []

    # 2. Start the Trade Manager (The Core)
    if os.path.exists(MANAGER_SCRIPT):
        manager_proc = launch_process(["python", MANAGER_SCRIPT], "Trade Manager (Core)")
        if manager_proc:
            processes.append(manager_proc)
    else:
        print(f"CRITICAL: {MANAGER_SCRIPT} not found.")
        input("Press Enter to exit...")
        return

    # 3. Start the Dashboard (The UI)
    if os.path.exists(DASHBOARD_SCRIPT):
        dash_proc = launch_process(["python", "-m", "streamlit", "run", DASHBOARD_SCRIPT], "Dashboard UI")
        if dash_proc:
            processes.append(dash_proc)
            print("Launcher: Dashboard launching on http://localhost:8501")
    else:
        print(f"Warning: {DASHBOARD_SCRIPT} not found. UI will not run.")

    print("Launcher: Waiting 3s for Manager to initialize...")
    time.sleep(3)

    # 4. Start Strategies from JSON
    print("Launcher: Loading Strategies...")
    strategies = config.get("strategies", {})
    
    for strat_id, settings in strategies.items():
        if settings.get("enabled", False):
            script_rel_path = settings.get("script")
            
            if script_rel_path:
                # Convert to absolute path to ensure Python finds it
                script_abs_path = os.path.abspath(script_rel_path)
                
                if os.path.exists(script_abs_path):
                    p = launch_process(["python", script_abs_path], f"Strategy: {strat_id}")
                    if p: processes.append(p)
                else:
                    print(f"❌ ERROR: Could not find script for {strat_id}")
                    print(f"   Looking at: {script_abs_path}")
            else:
                print(f"Warning: Strategy {strat_id} enabled but no script path defined.")

    print(f"\n--- SYSTEM RUNNING: {len(processes)} Processes Active ---")
    print("Keep this window open. Press Ctrl+C to kill all bots.")

    # 5. Monitor Loop
    try:
        while True:
            time.sleep(1)
            # Check if Manager is still alive (Critical Component)
            if manager_proc.poll() is not None:
                print("CRITICAL: Trade Manager has crashed! Shutting down system.")
                break
    except KeyboardInterrupt:
        print("\nLauncher: Stopping all processes...")

    # 6. Cleanup
    for p in processes:
        if p.poll() is None:
            p.terminate()
            
    print("Launcher: System Shutdown Complete.")

if __name__ == "__main__":
    main()