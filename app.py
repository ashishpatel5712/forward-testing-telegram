import asyncio
from datetime import datetime
import numpy as np
import pandas as pd
import sqlite3
import streamlit as st
from pathlib import Path
import streamlit as st
import collector

# Optional local config fallback
try:
    import config
    INITIAL_CAPITAL = getattr(config, "INITIAL_CAPITAL", 1000000.0)
    TOTAL_QTY = getattr(config, "TOTAL_QTY", 50)
    NUM_LOTS = getattr(config, "NUM_LOTS", 1)
except ImportError:
    INITIAL_CAPITAL = 1000000.0
    TOTAL_QTY = 50
    NUM_LOTS = 1

DB_FILE = "trades.db"

# Initialize database tables on load
collector.init_db()

# --- Streamlit UI Configuration ---
# With this:
    # Locate the icon relative to app.py
ICON_PATH = Path(__file__).parent / "icon.png"
st.set_page_config(
    page_title="Forward Tester",
    page_icon=str(ICON_PATH) if ICON_PATH.exists() else "⚡",
    layout="wide",
)
# --- Sidebar: Direct Telegram Fetch Control ---
with st.sidebar:
    st.header("⚙️ Controls")
    if st.button("🔄 Fetch Today's Tips from Telegram", use_container_width=True):
        with st.spinner("Scanning Telegram channels for today's signals..."):
            try:
                # Direct in-process async call compatible with cloud hosting
                asyncio.run(collector.fetch_daily_tips())
                st.success("Successfully fetched new tips!")
                st.rerun()
            except Exception as e:
                st.error(f"Error fetching tips: {e}")

    st.divider()
    st.caption("Click the button above anytime to scan Telegram channels and load pending tips into the review pane.")

# --- Database Helpers ---
def get_db_connection():
    return sqlite3.connect(DB_FILE)

def load_staged_trades():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM staged_trades WHERE status = 'PENDING'", conn)
    conn.close()
    return df

def load_executed_trades():
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM trade_log ORDER BY id DESC", conn)
    conn.close()
    return df

# --- Simulation Engine ---
def fetch_or_simulate_price_path(entry_price, start_time_str):
    """
    Simulates intraday 1-minute candle bars from the tip time until 15:30.
    """
    start_dt = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
    end_dt = start_dt.replace(hour=15, minute=30, second=0)

    minutes = max(15, int((end_dt - start_dt).total_seconds() // 60))

    np.random.seed(int(start_dt.timestamp()) % 10000)
    shocks = np.random.normal(loc=0.0002, scale=0.008, size=minutes)
    price_series = entry_price * np.cumprod(1 + shocks)

    candles = []
    current_time = start_dt
    for p in price_series:
        high = p * (1 + abs(np.random.normal(0, 0.003)))
        low = p * (1 - abs(np.random.normal(0, 0.003)))
        candles.append({
            "timestamp": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(p, 2)
        })
        current_time = pd.Timestamp(current_time) + pd.Timedelta(minutes=1)

    return pd.DataFrame(candles)

def run_simulation(trade_row):
    """Executes trade path against target, stop loss, or 15:15 market close."""
    entry_price = float(trade_row["entry_ref"])
    capital_deployed = round(entry_price * TOTAL_QTY, 2)

    # Fallback target (+2%) and stop loss (-5%)
    target = float(trade_row["target"]) if pd.notnull(trade_row["target"]) else round(entry_price * 1.02, 2)
    sl = float(trade_row["sl"]) if pd.notnull(trade_row["sl"]) else round(entry_price * 0.95, 2)

    candles_df = fetch_or_simulate_price_path(entry_price, trade_row["msg_time"])

    exit_price = None
    exit_reason = "EOD_EXIT"
    exit_time = None

    for _, candle in candles_df.iterrows():
        if candle["low"] <= sl:
            exit_price = sl
            exit_reason = "STOP_LOSS"
            exit_time = candle["timestamp"]
            break
        elif candle["high"] >= target:
            exit_price = target
            exit_reason = "TARGET"
            exit_time = candle["timestamp"]
            break

    if exit_price is None:
        last_candle = candles_df.iloc[-1]
        exit_price = last_candle["close"]
        exit_reason = "MARKET_CLOSE (15:15)"
        exit_time = last_candle["timestamp"]

    pnl = round((exit_price - entry_price) * TOTAL_QTY, 2)
    pnl_pct = round((pnl / capital_deployed) * 100, 2)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO trade_log (
            channel_name, symbol, entry_time, entry_price, exit_time,
            exit_price, lots, capital_deployed, pnl, pnl_pct, exit_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade_row["channel_name"], trade_row["symbol"], trade_row["msg_time"],
        entry_price, exit_time, exit_price, NUM_LOTS,
        capital_deployed, pnl, pnl_pct, exit_reason
    ))
    cursor.execute("UPDATE staged_trades SET status = 'EXECUTED' WHERE id = ?", (trade_row["id"],))
    conn.commit()
    conn.close()

    return pnl, pnl_pct, exit_reason

# --- Main Dashboard Header & Metrics ---
st.title("⚡ Telegram Options Signal Forward-Tester")

executed_df = load_executed_trades()
total_pnl = executed_df["pnl"].sum() if not executed_df.empty else 0.0
current_capital = INITIAL_CAPITAL + total_pnl

m1, m2, m3, m4 = st.columns(4)
m1.metric("Initial Capital", f"₹{INITIAL_CAPITAL:,.2f}")
m2.metric("Current Capital", f"₹{current_capital:,.2f}", delta=f"₹{total_pnl:,.2f}")
m3.metric("Trades Executed", len(executed_df))
win_rate = (len(executed_df[executed_df["pnl"] > 0]) / len(executed_df) * 100) if len(executed_df) > 0 else 0
m4.metric("Win Rate", f"{win_rate:.1f}%")

st.markdown("---")

# 1. Review Pane (Staged Trades)
st.subheader("📋 Review Pane: Staged Tips Awaiting Execution")
staged_df = load_staged_trades()

if staged_df.empty:
    st.info("No pending trades waiting for review. Click '🔄 Fetch Today's Tips from Telegram' in the sidebar to scan for new signals.")
else:
    for _, row in staged_df.iterrows():
        with st.container():
            c1, c2, c3, c4, c5, c6 = st.columns([2, 2, 2, 2, 2, 1.5])
            c1.markdown(f"**Channel:** `{row['channel_name']}`")
            c2.markdown(f"**Contract:** `{row['symbol']}`")
            c3.markdown(f"**Signal Time:** {row['msg_time']}")
            c4.markdown(f"**Entry Ref:** ₹{row['entry_ref']}")

            tgt_display = f"₹{row['target']}" if pd.notnull(row['target']) else "2% Deployed (Default)"
            sl_display = f"₹{row['sl']}" if pd.notnull(row['sl']) else "5% Deployed (Default)"
            c5.markdown(f"**SL / TGT:** {sl_display} / {tgt_display}")

            if c6.button("⚡ Execute", key=f"btn_{row['id']}"):
                pnl, pnl_pct, reason = run_simulation(row)
                st.toast(f"Executed {row['symbol']}! PnL: ₹{pnl} ({pnl_pct}%) via {reason}")
                st.rerun()
            st.divider()

# 2. Executed Trades & Trade Log
st.subheader("📊 Performance & Execution Log")
if not executed_df.empty:
    st.dataframe(
        executed_df[[
            "id", "channel_name", "symbol", "entry_time", "entry_price",
            "exit_time", "exit_price", "capital_deployed", "pnl", "pnl_pct", "exit_reason"
        ]],
        use_container_width=True
    )
else:
    st.write("No trades logged yet.")

# 3. Channel Performance Analytics & Equity Curves
st.markdown("---")
st.subheader("📈 Channel Performance Comparison (CAGR, Drawdown & Risk)")

if not executed_df.empty:
    channel_stats = []
    channels = executed_df["channel_name"].unique()

    for ch_name in channels:
        group = executed_df[executed_df["channel_name"] == ch_name].sort_values("id")

        total_ch_pnl = group["pnl"].sum()
        trades_count = len(group)
        win_trades = len(group[group["pnl"] > 0])
        win_rate_ch = (win_trades / trades_count) * 100 if trades_count > 0 else 0

        gross_profit = group[group["pnl"] > 0]["pnl"].sum()
        gross_loss = abs(group[group["pnl"] < 0]["pnl"].sum())
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0)

        equity_series = INITIAL_CAPITAL + group["pnl"].cumsum()
        peak = equity_series.cummax()
        drawdown = (equity_series - peak) / peak * 100
        max_drawdown = abs(drawdown.min())

        try:
            start_date = pd.to_datetime(group["entry_time"]).min()
            end_date = pd.to_datetime(group["exit_time"]).max()
            days = max(1, (end_date - start_date).days)
            end_value = INITIAL_CAPITAL + total_ch_pnl
            cagr = (((end_value / INITIAL_CAPITAL) ** (365.0 / days)) - 1) * 100
        except Exception:
            days = 0
            cagr = 0.0

        channel_stats.append({
            "Channel": ch_name,
            "Total Trades": trades_count,
            "Win Rate (%)": f"{win_rate_ch:.1f}%",
            "Net PnL (₹)": f"₹{total_ch_pnl:,.2f}",
            "Max Drawdown (%)": f"{max_drawdown:.2f}%",
            "Profit Factor": f"{profit_factor:.2f}",
            "CAGR (%)": f"{cagr:.2f}%" if days >= 7 else "N/A (< 7 Days)"
        })

    stats_df = pd.DataFrame(channel_stats)
    st.table(stats_df)

    # Interactive Equity Curve Selection
    st.markdown("### 📉 Channel Equity Curves")
    st.write("Click a channel below to display its capital progression curve:")

    cols = st.columns(len(channels))

    if "selected_channel_curve" not in st.session_state:
        st.session_state.selected_channel_curve = None

    for i, ch_name in enumerate(channels):
        if cols[i].button(f"Show Equity Curve: {ch_name}", key=f"btn_curve_{ch_name}"):
            st.session_state.selected_channel_curve = ch_name

    active_channel = st.session_state.selected_channel_curve
    if active_channel:
        ch_trades = executed_df[executed_df["channel_name"] == active_channel].sort_values("id").copy()
        ch_trades["Equity (₹)"] = INITIAL_CAPITAL + ch_trades["pnl"].cumsum()
        ch_trades["Trade Sequence"] = range(1, len(ch_trades) + 1)

        baseline = pd.DataFrame([{"Trade Sequence": 0, "Equity (₹)": INITIAL_CAPITAL}])
        plot_df = pd.concat([baseline, ch_trades[["Trade Sequence", "Equity (₹)"]]], ignore_index=True)

        st.write(f"#### Cumulative Capital Curve: **{active_channel}**")
        st.line_chart(plot_df.set_index("Trade Sequence")["Equity (₹)"])
else:
    st.info("Execute trades in the review pane above to compute channel metrics.")
