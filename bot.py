import logging
import os
import time
import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DESO_NODE = "https://node.deso.org"
DESO_PRICE_URL = "https://node.deso.org/api/v0/get-exchange-rate"
POLL_INTERVAL = 30
PRICE_TTL = 300
UNSTAKE_TXN_TYPE = 37

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

state = {
    "last_block": 0,
    "chat_ids": set(),
    "min_usd": 0.0,
    "deso_price_usd": None,
    "price_updated_at": 0.0,
}


# ── Database ──────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id BIGINT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
        conn.commit()
    logger.info("Database initialized")


def load_from_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id FROM subscribers")
            state["chat_ids"] = {row[0] for row in cur.fetchall()}

            cur.execute("SELECT value FROM settings WHERE key = 'min_usd'")
            row = cur.fetchone()
            state["min_usd"] = float(row[0]) if row else 0.0

    logger.info(f"Loaded {len(state['chat_ids'])} subscriber(s), min_usd=${state['min_usd']}")


def db_add_subscriber(chat_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO subscribers (chat_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (chat_id,)
            )
        conn.commit()


def db_remove_subscriber(chat_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subscribers WHERE chat_id = %s", (chat_id,))
        conn.commit()


def db_set_min_usd(amount: float) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO settings (key, value) VALUES ('min_usd', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (str(amount),))
        conn.commit()


# ── Price ─────────────────────────────────────────────────────────────────────

def get_deso_price() -> float | None:
    now = time.time()
    if state["deso_price_usd"] and now - state["price_updated_at"] < PRICE_TTL:
        return state["deso_price_usd"]
    try:
        resp = requests.get(DESO_PRICE_URL, timeout=10)
        resp.raise_for_status()
        price = resp.json()["USDCentsPerDeSoExchangeRate"] / 100
        state["deso_price_usd"] = price
        state["price_updated_at"] = now
        logger.info(f"DeSo price refreshed: ${price}")
        return price
    except Exception as e:
        logger.error(f"Price fetch failed: {e}")
        return state["deso_price_usd"]


# ── DeSo API ──────────────────────────────────────────────────────────────────

def get_latest_block_height() -> int:
    resp = requests.post(f"{DESO_NODE}/api/v1/node-info", json={}, timeout=10)
    resp.raise_for_status()
    return resp.json()["LatestBlockHeight"]


def get_block(height: int) -> dict:
    resp = requests.post(
        f"{DESO_NODE}/api/v1/block",
        json={"Height": height, "FullBlock": True},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ── Helpers ───────────────────────────────────────────────────────────────────

def nanos_to_deso(nanos) -> float | None:
    return int(nanos) / 1e9 if nanos else None


def short_key(key: str) -> str:
    if not key or key == "Unknown":
        return "Unknown"
    return f"{key[:8]}...{key[-6:]}" if len(key) > 14 else key


def build_notification(txn: dict, height: int, deso_amount: float | None, usd_value: float | None) -> str:
    staker = txn.get("PublicKeyBase58Check", "Unknown")
    txn_hash = txn.get("TransactionIDBase58Check", "Unknown")
    meta = txn.get("TxnMeta") or {}
    validator = meta.get("ValidatorPublicKeyBase58Check", "Unknown")

    amount_str = f"{deso_amount:,.4f} DESO" if deso_amount is not None else "Unknown"
    usd_str = f"(~${usd_value:,.2f})" if usd_value is not None else ""

    return (
        f"<b>DeSo Unstake Detected</b>\n\n"
        f"Block: <code>#{height}</code>\n"
        f"Staker: <code>{short_key(staker)}</code>\n"
        f"Validator: <code>{short_key(validator)}</code>\n"
        f"Amount: <b>{amount_str}</b> {usd_str}\n"
        f"Tx: <code>{short_key(txn_hash)}</code>\n"
        f"<a href=\"https://explorer.deso.com/txn/{txn_hash}\">View on Explorer</a>"
    )


# ── Commands ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    already_subscribed = chat_id in state["chat_ids"]
    state["chat_ids"].add(chat_id)
    db_add_subscriber(chat_id)

    price = get_deso_price()
    price_str = f"${price:,.4f}" if price else "unavailable"
    min_usd = state["min_usd"]
    threshold_str = f"${min_usd:,.2f}" if min_usd > 0 else "None (all unstakes)"
    msg = "Welcome back! You're already subscribed." if already_subscribed else "You're now subscribed to DeSo unstake alerts!"

    await update.message.reply_text(
        f"<b>DeSo Unstake Monitor</b>\n"
        f"{msg}\n\n"
        f"DeSo price: <b>{price_str}</b>\n"
        f"Min threshold: <b>{threshold_str}</b>\n\n"
        f"<b>Commands:</b>\n"
        f"/setmin &lt;amount&gt; — set minimum USD value\n"
        f"  e.g. <code>/setmin 10000</code> → only notify if &gt;$10,000\n"
        f"  e.g. <code>/setmin 0</code> → notify for all unstakes\n"
        f"/price — current DeSo price\n"
        f"/settings — show current config\n"
        f"/stop — unsubscribe from alerts\n"
        f"/status — last block scanned",
        parse_mode="HTML",
    )
    logger.info(f"{'Re-registered' if already_subscribed else 'Registered'} chat_id: {chat_id} — total: {len(state['chat_ids'])}")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state["chat_ids"].discard(chat_id)
    db_remove_subscriber(chat_id)
    await update.message.reply_text("You've been unsubscribed. Send /start anytime to re-subscribe.")
    logger.info(f"Unsubscribed chat_id: {chat_id} — remaining: {len(state['chat_ids'])}")


async def setmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /setmin <amount>\nExample: /setmin 10000")
        return
    try:
        amount = float(context.args[0].replace(",", "").replace("$", ""))
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please provide a valid number. Example: /setmin 10000")
        return

    state["min_usd"] = amount
    db_set_min_usd(amount)
    price = get_deso_price()

    if amount == 0:
        await update.message.reply_text("Threshold cleared. You'll be notified of <b>all</b> unstakes.", parse_mode="HTML")
    else:
        deso_equiv = f"~{amount / price:,.2f} DESO" if price else "unknown DESO"
        await update.message.reply_text(
            f"Threshold set to <b>${amount:,.2f}</b> ({deso_equiv} at current price).\n"
            f"Only unstakes above this amount will trigger a notification.",
            parse_mode="HTML",
        )


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    price = get_deso_price()
    if price:
        min_usd = state["min_usd"]
        deso_thresh = f"{min_usd / price:,.2f} DESO" if min_usd > 0 else "N/A"
        await update.message.reply_text(
            f"DeSo price: <b>${price:,.4f}</b>\n"
            f"Your threshold: <b>{'${:,.2f}'.format(min_usd) if min_usd > 0 else 'None'}</b>"
            f"{f' = {deso_thresh}' if min_usd > 0 else ''}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("Could not fetch price right now. Try again shortly.")


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    price = get_deso_price()
    min_usd = state["min_usd"]
    price_str = f"${price:,.4f}" if price else "unavailable"
    threshold_str = f"${min_usd:,.2f}" if min_usd > 0 else "None (all unstakes)"
    deso_equiv = f"(~{min_usd / price:,.2f} DESO)" if price and min_usd > 0 else ""

    await update.message.reply_text(
        f"<b>Current Settings</b>\n\n"
        f"DeSo price: <b>{price_str}</b>\n"
        f"Min threshold: <b>{threshold_str}</b> {deso_equiv}\n"
        f"Subscribers: <b>{len(state['chat_ids'])}</b>\n"
        f"Last block: <code>{state['last_block']}</code>",
        parse_mode="HTML",
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Status: Running\n"
        f"Subscribers: <b>{len(state['chat_ids'])}</b>\n"
        f"Last block checked: <code>{state['last_block']}</code>",
        parse_mode="HTML",
    )


# ── Polling loop ──────────────────────────────────────────────────────────────

async def check_unstakes(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_ids = set(state["chat_ids"])
    if not chat_ids:
        logger.warning("No subscribers. Have users send /start to the bot.")
        return

    try:
        latest_height = get_latest_block_height()

        if state["last_block"] == 0:
            state["last_block"] = latest_height
            logger.info(f"Initialized at block #{latest_height}")
            return

        if latest_height <= state["last_block"]:
            return

        price = get_deso_price()

        for height in range(state["last_block"] + 1, latest_height + 1):
            logger.info(f"Scanning block #{height}")
            try:
                block = get_block(height)
                for txn in (block.get("Transactions") or []):
                    if txn.get("TxnTypeJSON") != UNSTAKE_TXN_TYPE:
                        continue

                    meta = txn.get("TxnMeta") or {}
                    amount_nanos = meta.get("UnstakeAmountNanos") or meta.get("StakeAmountNanos")
                    deso_amount = nanos_to_deso(amount_nanos)
                    usd_value = (deso_amount * price) if (deso_amount and price) else None

                    if state["min_usd"] > 0 and (usd_value is None or usd_value < state["min_usd"]):
                        logger.info(f"Skipping — below threshold (${usd_value} < ${state['min_usd']})")
                        continue

                    msg = build_notification(txn, height, deso_amount, usd_value)
                    for chat_id in chat_ids:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    logger.info(f"Notified {len(chat_ids)} subscriber(s) — block #{height}")

            except Exception as e:
                logger.error(f"Error processing block #{height}: {e}")

            state["last_block"] = height

    except Exception as e:
        logger.error(f"Error in check_unstakes: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set.")
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set.")

    init_db()
    load_from_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("setmin", setmin))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("status", status))

    app.job_queue.run_repeating(check_unstakes, interval=POLL_INTERVAL, first=5)

    logger.info("DeSo Unstake Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
