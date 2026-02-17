import sqlite3
import json
import os
from datetime import datetime

# --- PATH FIX ---
# Forces the DB to be created in the directory of the main script (Launcher.py), not inside /components
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILE = os.path.join(BASE_DIR, "trading_system.db")

class Database:
    def __init__(self):
        self.conn = None
        self.initialize()

    def get_connection(self):
        """Creates a connection with row factory for dictionary-like access"""
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        """Creates tables if they don't exist"""
        conn = self.get_connection()
        c = conn.cursor()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                ticket INTEGER PRIMARY KEY,
                strategy_id TEXT,
                symbol TEXT,
                action TEXT,
                open_time TIMESTAMP,
                close_time TIMESTAMP,
                duration_sec REAL,
                open_price REAL,
                close_price REAL,
                sl REAL,
                tp REAL,
                pnl REAL,
                commission REAL,
                swap REAL,
                close_reason TEXT,
                meta_json TEXT
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS equity_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP,
                balance REAL,
                equity REAL,
                open_positions INTEGER
            )
        ''')
        
        conn.commit()
        conn.close()

    def get_todays_stats(self):
        """
        Calculates total trades and PnL for the current day from the DB.
        This allows the dashboard to persist stats after a refresh.
        """
        conn = self.get_connection()
        c = conn.cursor()
        try:
            # Get start of today (Midnight)
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_str = today_start.strftime('%Y-%m-%d %H:%M:%S')
            
            # Query: Total PnL and Trade Count for Today
            # We filter by close_time >= today at 00:00:00
            c.execute("""
                SELECT COUNT(*), SUM(pnl) 
                FROM trades 
                WHERE close_time >= ?
            """, (today_str,))
            
            row = c.fetchone()
            count = row[0] if row[0] else 0
            pnl = row[1] if row[1] else 0.0
            
            return {"daily_pnl": pnl, "trade_count": count}
            
        except Exception as e:
            print(f"DB Read Error: {e}")
            return {"daily_pnl": 0.0, "trade_count": 0}
        finally:
            conn.close()

    def log_trade(self, trade_dict):
        """
        Saves a trade to the DB.
        """
        conn = self.get_connection()
        c = conn.cursor()
        
        meta_data = trade_dict.get('extra_metrics', {})
        meta_json_str = json.dumps(meta_data)
        
        try:
            # Convert datetime objects to string to ensure SQLite compatibility
            o_time = trade_dict['open_time']
            c_time = trade_dict['close_time']
            if isinstance(o_time, datetime): o_time = o_time.strftime('%Y-%m-%d %H:%M:%S')
            if isinstance(c_time, datetime): c_time = c_time.strftime('%Y-%m-%d %H:%M:%S')

            c.execute('''
                INSERT OR REPLACE INTO trades (
                    ticket, strategy_id, symbol, action, open_time, close_time, 
                    duration_sec, open_price, close_price, sl, tp, 
                    pnl, commission, swap, close_reason, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trade_dict['ticket'],
                trade_dict['strategy_id'],
                trade_dict['symbol'],
                trade_dict['action'],
                o_time,
                c_time,
                trade_dict['duration'],
                trade_dict['open_price'],
                trade_dict['close_price'],
                trade_dict.get('sl', 0),
                trade_dict.get('tp', 0),
                trade_dict['net_pnl'],
                trade_dict['commission'],
                trade_dict['swap'],
                trade_dict['reason'],
                meta_json_str
            ))
            conn.commit()
            print(f"Database: Trade {trade_dict['ticket']} saved.")
        except Exception as e:
            print(f"Database Error: {e}")
        finally:
            conn.close()

    def log_equity_snapshot(self, balance, equity, open_positions):
        """Logs the current account state"""
        conn = self.get_connection()
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO equity_history (timestamp, balance, equity, open_positions)
                VALUES (?, ?, ?, ?)
            ''', (datetime.now(), balance, equity, open_positions))
            conn.commit()
        except Exception as e:
            print(f"DB Snapshot Error: {e}")
        finally:
            conn.close()

    def fetch_equity_history(self, limit=1000):
        """Fetches equity curve for charting"""
        if not os.path.exists(DB_FILE): return []
        conn = self.get_connection()
        c = conn.cursor()
        try:
            c.execute("SELECT * FROM equity_history ORDER BY timestamp DESC LIMIT ?", (limit,))
            return c.fetchall()
        finally:
            conn.close()

    def fetch_trades(self, strategy_id=None, limit=100):
        """Retrieves trades."""
        if not os.path.exists(DB_FILE):
            return []
            
        conn = self.get_connection()
        c = conn.cursor()
        
        query = "SELECT * FROM trades"
        params = []
        
        if strategy_id:
            query += " WHERE strategy_id = ?"
            params.append(strategy_id)
            
        query += " ORDER BY close_time DESC LIMIT ?"
        params.append(limit)
        
        try:
            c.execute(query, params)
            rows = c.fetchall()
            
            results = []
            for row in rows:
                d = dict(row)
                if d['meta_json']:
                    d['meta_json'] = json.loads(d['meta_json'])
                results.append(d)
            return results
        except Exception:
            return []
        finally:
            conn.close()