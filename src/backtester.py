import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from src.database import fetch_candles_from_db, fetch_history_fyers

def get_expiry_dates(fyers_client, symbol="NSE:NIFTY50-INDEX"):
    try:
        # Fyers API v3 optionchain input payload structure
        data = {
            "symbol": symbol,
            "strikecount": 10,
            "timestamp": ""
        }
        response = fyers_client.optionchain(data=data)
        if response.get("s") == "ok":
            expiries = response.get("data", {}).get("expiryData", [])
            return expiries
    except Exception as e:
        print(f"Option chain error: {e}")
    return []

def resolve_option_symbols(underlying_symbol, expiry_date_str, spot_price, strike_range=5, step=50):
    """
    Dynamically generates Call and Put option symbols for given strike prices
    surrounding the ATM spot price. Fyers format: NSE:NIFTY26NOV2424000CE
    """
    atm_strike = round(spot_price / step) * step
    strikes = [atm_strike + (i * step) for i in range(-strike_range, strike_range + 1)]
    
    # Parse expiry string to format code (e.g. 24NOV24)
    dt = datetime.strptime(expiry_date_str, "%d-%b-%Y")
    yy = dt.strftime("%y")
    mmm = dt.strftime("%b").upper()
    dd = dt.strftime("%d")
    
    # Base index prefix extraction (e.g. NSE:NIFTY50-INDEX -> NIFTY)
    prefix = underlying_symbol.split(":")[1].split("-")[0]
    if "NIFTY" in prefix:
        prefix = "NIFTY"
    elif "BANKNIFTY" in prefix:
        prefix = "BANKNIFTY"

    option_symbols = []
    for strike in strikes:
        ce_symbol = f"NSE:{prefix}{yy}{mmm}{dd}{int(strike)}CE"
        pe_symbol = f"NSE:{prefix}{yy}{mmm}{dd}{int(strike)}PE"
        option_symbols.append((strike, ce_symbol, pe_symbol))
        
    return option_symbols

class BacktestEngine:
    def __init__(self, initial_capital=100000.0):
        self.initial_capital = initial_capital
        
    def run_sma_crossover(self, df, fast_window=9, slow_window=21, qty=50):
        df = df.copy()
        df["fast_sma"] = df["close"].rolling(fast_window).mean()
        df["slow_sma"] = df["close"].rolling(slow_window).mean()
        
        df["signal"] = 0
        df.loc[df["fast_sma"] > df["slow_sma"], "signal"] = 1
        df.loc[df["fast_sma"] < df["slow_sma"], "signal"] = -1
        df["position"] = df["signal"].shift(1).fillna(0)
        
        trades = []
        cash = self.initial_capital
        in_trade = False
        entry_price = 0
        entry_time = None
        trade_type = None
        
        for i in range(1, len(df)):
            row = df.iloc[i]
            prev_pos = df.iloc[i-1]["position"]
            curr_pos = row["position"]
            
            if curr_pos != prev_pos:
                # Close existing trade
                if in_trade:
                    exit_price = row["open"]
                    pnl = (exit_price - entry_price) * qty if trade_type == "LONG" else (entry_price - exit_price) * qty
                    cash += pnl
                    trades.append({
                        "Entry Time": entry_time,
                        "Exit Time": row["timestamp"] if "timestamp" in row else i,
                        "Type": trade_type,
                        "Entry Price": entry_price,
                        "Exit Price": exit_price,
                        "Qty": qty,
                        "PnL": pnl,
                        "Return (%)": (pnl / (entry_price * qty)) * 100,
                        "Cumulative Capital": cash
                    })
                    in_trade = False
                
                # Open new trade
                if curr_pos == 1:
                    entry_price = row["open"]
                    entry_time = row["timestamp"] if "timestamp" in row else i
                    trade_type = "LONG"
                    in_trade = True
                elif curr_pos == -1:
                    entry_price = row["open"]
                    entry_time = row["timestamp"] if "timestamp" in row else i
                    trade_type = "SHORT"
                    in_trade = True
                    
        trades_df = pd.DataFrame(trades)
        
        # Summary Statistics
        total_trades = len(trades_df)
        winning_trades = len(trades_df[trades_df["PnL"] > 0]) if total_trades > 0 else 0
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        net_profit = trades_df["PnL"].sum() if total_trades > 0 else 0
        max_drawdown = 0
        if total_trades > 0:
            peak = trades_df["Cumulative Capital"].cummax()
            dd = (trades_df["Cumulative Capital"] - peak) / peak * 100
            max_drawdown = dd.min()

        summary = {
            "Initial Capital": self.initial_capital,
            "Final Capital": self.initial_capital + net_profit,
            "Net Profit / Loss": net_profit,
            "Total Trades": total_trades,
            "Win Rate (%)": win_rate,
            "Max Drawdown (%)": max_drawdown
        }
        
        return summary, trades_df, df