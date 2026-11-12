import duckdb
import pandas as pd
from datetime import datetime, timedelta

DB_PATH = "market_data.duckdb"

def init_db():
    conn = duckdb.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_candles (
            symbol VARCHAR,
            resolution VARCHAR,
            timestamp TIMESTAMP,
            epoch BIGINT,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume BIGINT,
            PRIMARY KEY (symbol, resolution, timestamp)
        )
    """)
    conn.close()

def store_candles(symbol, resolution, df):
    if df.empty:
        return
    conn = duckdb.connect(DB_PATH)
    # Register pandas dataframe and insert into DuckDB efficiently
    conn.register("df_candles", df)
    conn.execute(f"""
        INSERT OR REPLACE INTO historical_candles 
        SELECT 
            '{symbol}' AS symbol, 
            '{resolution}' AS resolution, 
            CAST(to_timestamp(epoch) AS TIMESTAMP) AS timestamp, 
            epoch, open, high, low, close, volume 
        FROM df_candles
    """)
    conn.close()

def fetch_candles_from_db(symbol, resolution, start_date, end_date):
    conn = duckdb.connect(DB_PATH)
    query = f"""
        SELECT epoch, open, high, low, close, volume
        FROM historical_candles 
        WHERE symbol = ? AND resolution = ? AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp ASC
    """
    df = conn.execute(query, [symbol, resolution, start_date, end_date]).fetchdf()
    conn.close()
    return df

def fetch_history_fyers(fyers_client, symbol, resolution="1", start_date="2026-01-01", end_date="2026-03-01"):
    init_db()
    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date, "%Y-%m-%d")
    
    all_candles = []
    curr_start = s_dt
    
    while curr_start < e_dt:
        curr_end = min(curr_start + timedelta(days=90), e_dt)
        
        data = {
            "symbol": symbol,
            "resolution": str(resolution),
            "date_format": "1",
            "range_from": curr_start.strftime("%Y-%m-%d"),
            "range_to": curr_end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
            "oi_flag": "1"
        }
        
        response = fyers_client.history(data=data)
        if response.get("s") == "ok" and "candles" in response:
            candles = response["candles"]
            if candles:
                df_chunk = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
                all_candles.append(df_chunk)
        else:
            print(f"Fyers History API Error for {symbol}: {response}")
        
        curr_start = curr_end + timedelta(days=1)
        
    if all_candles:
        full_df = pd.concat(all_candles, ignore_index=True).drop_duplicates(subset=["epoch"])
        store_candles(symbol, resolution, full_df)
        return full_df
    return pd.DataFrame()