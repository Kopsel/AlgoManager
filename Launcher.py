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

def launch_dashboard():
    """Helper function to launch the dashboard with specific Streamlit flags"""
    print("Launcher: 🚀 Starting Dashboard UI...")
    
    # Windows specific flag for new window
    CREATE_NEW_CONSOLE = subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
    
    cmd = [
        "streamlit", "run", DASHBOARD_SCRIPT,
        "--server.address=0.0.0.0",     # Allow Tailscale/LAN connections
        "--server.port=8501",           # Fixed Port
        "--theme.base=dark",            # Dark Mode
        "--server.headless=true",       # Don't pop up browser on VM
        "--global.developmentMode=false"
    ]
    
    return subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE)

def main():
    print("--- ALGOTRADING SYSTEM LAUNCHER ---")
    
    # 1. Load Config
    try:
        config = load_config()
    except Exception as e:
        print(f"CRITICAL: system_config.json error!\n{e}")
        input("Press Enter to exit...")
        return

    processes = []

    # 2. Start the Trade Manager (The Core)
    if os.path.exists(MANAGER_SCRIPT):
        print("Launcher: Starting Trade Manager...")
        # Standard launch for Manager (closes on exit is fine usually, or use /k if it crashes too)
        CREATE_NEW_CONSOLE = subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
        manager_proc = subprocess.Popen(
            ["python", MANAGER_SCRIPT],
            creationflags=CREATE_NEW_CONSOLE
        )
        processes.append(manager_proc)
    else:
        print(f"CRITICAL: {MANAGER_SCRIPT} not found.")
        input("Press Enter to exit...")
        return

    # 3. Start the Dashboard (The UI)
    dash_proc = None
    if os.path.exists(DASHBOARD_SCRIPT):
        dash_proc = launch_dashboard()
        if dash_proc:
            print("Launcher: Dashboard running on http://localhost:8501")
    else:
        print(f"Warning: {DASHBOARD_SCRIPT} not found.")

    print("Launcher: Waiting 3s for Manager to initialize...")
    time.sleep(3)

    # 4. Start Strategies (DEBUG MODE - KEEPS WINDOW OPEN)
    print("Launcher: Loading Strategies...")
    strategies = config.get("strategies", {})
    
    for strat_id, settings in strategies.items():
        if settings.get("enabled", False):
            script_rel_path = settings.get("script")
            
            if script_rel_path:
                script_abs_path = os.path.abspath(script_rel_path)
                
                if os.path.exists(script_abs_path):
                    print(f"Launcher: Starting Strategy {strat_id} (Debug Mode)...")
                    
                    # COMMAND EXPLANATION:
                    # cmd /k : Run command and KEEP window open (Prevent closing on error)
                    cmd = ["cmd", "/k", "python", script_abs_path]
                    
                    CREATE_NEW_CONSOLE = subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
                    p = subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE)
                    processes.append(p)
                else:
                    print(f"❌ ERROR: Script not found: {script_abs_path}")
            else:
                print(f"Warning: Strategy {strat_id} enabled but no script path.")

    print(f"\n--- SYSTEM RUNNING: {len(processes)} Processes Active ---")
    print("Keep this window open. Press Ctrl+C to kill all bots.")

    # 5. Monitor Loop (The Heartbeat)
    try:
        while True:
            time.sleep(2)
            
            # Check A: Is Trade Manager alive?
            if manager_proc.poll() is not None:
                print("CRITICAL: Trade Manager died! Shutting down system.")
                break
            
            # Check B: Is Dashboard alive? (Auto-Restart)
            if dash_proc is not None and dash_proc.poll() is not None:
                print("⚠️ WARNING: Dashboard crashed (likely network). Restarting...")
                dash_proc = launch_dashboard()

    except KeyboardInterrupt:
        print("\nLauncher: Stopping all processes...")

    # 6. Cleanup
    if dash_proc: processes.append(dash_proc) # Ensure dash is in list for cleanup
    
    for p in processes:
        if p.poll() is None: # If still running
            try:
                p.terminate()
            except:
                pass
            
    print("Launcher: System Shutdown Complete.")

if __name__ == "__main__":
    main()