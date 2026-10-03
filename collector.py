import asyncio
from datetime import datetime
import os
import re
import sqlite3
import streamlit as st
from telethon import TelegramClient
from telethon.sessions import StringSession

# 1. Fallback to local config.py if available
try:
    import config
    API_ID = getattr(config, "API_ID", None)
    API_HASH = getattr(config, "API_HASH", None)
    SESSION_STRING = getattr(config, "TELEGRAM_SESSION", "")
except ImportError:
    API_ID = None
    API_HASH = None
    SESSION_STRING = ""

# 2. Check environment variables
API_ID = int(os.getenv("API_ID", API_ID)) if os.getenv("API_ID") else API_ID
API_HASH = os.getenv("API_HASH", API_HASH)
SESSION_STRING = os.getenv("TELEGRAM_SESSION", SESSION_STRING)

# 3. Read Streamlit Cloud Secrets (for cloud deployment)
try:
    if hasattr(st, "secrets"):
        if "API_ID" in st.secrets:
            API_ID = int(st.secrets["API_ID"])
        if "API_HASH" in st.secrets:
            API_HASH = st.secrets["API_HASH"]
        if "TELEGRAM_SESSION" in st.secrets:
            SESSION_STRING = st.secrets["TELEGRAM_SESSION"]
except Exception:
    pass

CHANNELS = [
    -1001480995890,              # MARKET PLUS RESEARCH
    "mcx_crudeoil_oil_comodity", # MCX CRUDEOIL COMODITY
]

def init_db():
    """Initializes the SQLite database tables."""
    conn = sqlite3.connect("trades.db")
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS staged_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_name TEXT,
        msg_time TEXT,
        raw_text TEXT,
        symbol TEXT,
        action TEXT,
        entry_ref REAL,
        sl REAL,
        target REAL,
        status TEXT DEFAULT 'PENDING'
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trade_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_name TEXT,
        symbol TEXT,
        entry_time TEXT,
        entry_price REAL,
        exit_time TEXT,
        exit_price REAL,
        lots INTEGER,
        capital_deployed REAL,
        pnl REAL,
        pnl_pct REAL,
        exit_reason TEXT
    )
    """)
    conn.commit()
    conn.close()

def parse_trade_message(text):
    """
    Parses trade tip messages for index options (NIFTY/BANKNIFTY).
    Handles hashtags (#NIFTY), separators (Above @ 180, ABOVE - 100),
    typos ('Traget'), multi-targets (120/130/150+), and non-numeric SLs (SL VIP, SL-PREMIUM).
    """
    if not text:
        return None

    clean_text = text.replace("*", "").replace("_", "")

    # 1. Match Contract: optional '#', index name, 5-digit strike, CE/PE
    contract_match = re.search(r"(?i)#?(?:NIFTY|BANKNIFTY)?\s*(\d{5})\s*(CE|PE)", clean_text)
    if not contract_match:
        return None
    strike, opt_type = contract_match.groups()

    # 2. Match Entry Price (handles: ABOVE - 100, Above @ 180, BUY: 100, CMP 100)
    entry_match = re.search(r"(?i)(?:ABOVE|BUY|AT|CMP|ENTRY)[\s\:\-@]+([0-9]+(?:\.[0-9]+)?)", clean_text)
    if not entry_match:
        return None
    entry_ref = float(entry_match.group(1))

    # 3. Match Target: extracts first target level (handles TARGET, TRAGET, TRG, TGT)
    tgt_match = re.search(r"(?i)(?:TARGET|TRAGET|TRG|TGT)[\s\:\-@]+([0-9]+(?:\.[0-9]+)?)", clean_text)
    target = float(tgt_match.group(1)) if tgt_match else None

    # 4. Match Stop Loss (numeric only; non-numeric values like 'SL VIP' or 'SL-PREMIUM' default to None)
    sl_match = re.search(r"(?i)\bSL[\s\:\-@]+([0-9]+(?:\.[0-9]+)?)\b", clean_text)
    sl = float(sl_match.group(1)) if sl_match else None

    return {
        "symbol": f"NIFTY_{strike}_{opt_type.upper()}",
        "action": "BUY",
        "entry_ref": entry_ref,
        "sl": sl,
        "target": target
    }

async def fetch_daily_tips():
    init_db()
    conn = sqlite3.connect("trades.db")
    cursor = conn.cursor()

    local_today = datetime.now().astimezone().date()

    # Choose StringSession if available, else local session file
    session = StringSession(SESSION_STRING) if SESSION_STRING else "session_name"

    async with TelegramClient(session, API_ID, API_HASH) as client:
        await client.get_dialogs()

        for ch in CHANNELS:
            target = int(ch) if isinstance(ch, str) and ch.lstrip("-").isdigit() else ch
            print(f"Reading messages from: {target}...")

            try:
                entity = await client.get_entity(target)
                display_name = getattr(entity, "title", str(target))

                async for msg in client.iter_messages(entity, limit=1000):
                    if not msg.date:
                        continue

                    msg_local_dt = msg.date.astimezone()

                    # Stop channel loop when reaching dates prior to today
                    if msg_local_dt.date() < local_today:
                        break

                    if msg_local_dt.date() == local_today:
                        content = msg.raw_text or msg.message or msg.text
                        if not content:
                            continue

                        parsed = parse_trade_message(content)
                        if parsed:
                            formatted_time = msg_local_dt.strftime("%Y-%m-%d %H:%M:%S")

                            # Avoid duplicate entries
                            cursor.execute(
                                "SELECT id FROM staged_trades WHERE symbol = ? AND msg_time = ?",
                                (parsed["symbol"], formatted_time)
                            )
                            if cursor.fetchone() is None:
                                cursor.execute("""
                                INSERT INTO staged_trades (
                                    channel_name, msg_time, raw_text, symbol, action, entry_ref, sl, target
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    display_name,
                                    formatted_time,
                                    content,
                                    parsed["symbol"],
                                    parsed["action"],
                                    parsed["entry_ref"],
                                    parsed["sl"],
                                    parsed["target"]
                                ))
                                print(f"  -> Captured: {parsed['symbol']} from {display_name}")
            except Exception as e:
                print(f"  -> Failed to read {target}: {e}")

    conn.commit()
    conn.close()
    print("\nDone! Today's trades are saved to trades.db.")

def run_fetch():
    """Helper to run the asynchronous fetch cleanly inside Streamlit."""
    asyncio.run(fetch_daily_tips())

if __name__ == "__main__":
    run_fetch()