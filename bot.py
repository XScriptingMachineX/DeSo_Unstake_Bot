import logging
import os
import time
import psycopg2
import requests
from dotenv import load_dotenv
from telegram import Update, Chat
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
    # chat_id -> min_usd (0 = notify all)
    "subscribers": {},
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
                    chat_id BIGINT PRIMARY KEY,
                    min_usd FLOAT NOT NULL DEFAULT 0
                );
                ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS min_usd FLOAT NOT NULL DEFAULT 0;
            """)
        conn.commit()
    logger.info("Database initialized")


def load_from_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, min_usd FROM subscribers")
            state["subscribers"] = {row[0]: row[1] for row in cur.fetchall()}
    logger.info(f"Loaded {len(state['subscribers'])} subscriber(s)")


def db_add_subscriber(chat_id: int, min_usd: float = 0) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO subscribers (chat_id, min_usd) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (chat_id, min_usd)
            )
        conn.commit()


def db_remove_subscriber(chat_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subscribers WHERE chat_id = %s", (chat_id,))
        conn.commit()


def db_set_min_usd(chat_id: int, amount: float) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE subscribers SET min_usd = %s WHERE chat_id = %s",
                (amount, chat_id)
            )
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

# Simple in-memory cache so we don't hammer the API for the same keys
_username_cache: dict[str, str] = {}

def get_deso_username(public_key: str) -> str:
    if not public_key or public_key == "Unknown":
        return "Anonymous"
    if public_key in _username_cache:
        return _username_cache[public_key]
    try:
        resp = requests.post(
            f"{DESO_NODE}/api/v0/get-single-profile",
            json={"PublicKeyBase58Check": public_key},
            timeout=10,
        )
        resp.raise_for_status()
        profile = resp.json().get("Profile")
        username = profile.get("Username") if profile else None
        result = f"@{username}" if username else "Anonymous"
    except Exception:
        result = "Anonymous"
    _username_cache[public_key] = result
    return result


def get_latest_block_height() -> int:
    resp = requests.post(f"{DESO_NODE}/api/v1/node-info", json={}, timeout=10)
    resp.raise_for_status()
    return resp.json()["DeSoStatus"]["LatestBlockHeight"]


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


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if chat.type == Chat.PRIVATE:
        return True
    member = await chat.get_member(update.effective_user.id)
    return member.status in ("creator", "administrator")


def build_notification(txn: dict, height: int, deso_amount: float | None, usd_value: float | None) -> str:
    staker = txn.get("PublicKeyBase58Check", "Unknown")
    txn_hash = txn.get("TransactionIDBase58Check", "Unknown")
    meta = txn.get("TxnMeta") or {}
    validator = meta.get("ValidatorPublicKeyBase58Check", "Unknown")

    staker_username = get_deso_username(staker)
    validator_username = get_deso_username(validator)

    amount_str = f"{deso_amount:,.4f} DESO" if deso_amount is not None else "Unknown"
    usd_str = f"(~${usd_value:,.2f})" if usd_value is not None else ""

    return (
        f"<b>DeSo Unstake Detected</b>\n\n"
        f"Block: <code>#{height}</code>\n"
        f"Staker: {staker_username} <code>{short_key(staker)}</code>\n"
        f"Validator: {validator_username} <code>{short_key(validator)}</code>\n"
        f"Amount: <b>{amount_str}</b> {usd_str}\n"
        f"Tx: <code>{short_key(txn_hash)}</code>\n"
        f"<a href=\"https://explorer.deso.com/txn/{txn_hash}\">View on Explorer</a>"
    )


# ── Commands ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    is_group = chat_type in (Chat.GROUP, Chat.SUPERGROUP)

    already_subscribed = chat_id in state["subscribers"]
    if not already_subscribed:
        state["subscribers"][chat_id] = 0
        db_add_subscriber(chat_id)

    price = get_deso_price()
    price_str = f"${price:,.4f}" if price else "unavailable"
    min_usd = state["subscribers"].get(chat_id, 0)
    threshold_str = f"${min_usd:,.2f}" if min_usd > 0 else "None (all unstakes)"
    msg = "Already subscribed!" if already_subscribed else (
        "This group is now subscribed to DeSo unstake alerts!" if is_group
        else "You're now subscribed to DeSo unstake alerts!"
    )

    admin_note = "\n\nOnly group admins can use /setmin." if is_group else ""

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
        f"/stop — unsubscribe from alerts"
        f"{admin_note}",
        parse_mode="HTML",
    )
    logger.info(f"{'Re-registered' if already_subscribed else 'Registered'} chat_id: {chat_id} ({chat_type})")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    is_group = chat_type in (Chat.GROUP, Chat.SUPERGROUP)

    # Only admins can unsubscribe a group
    if is_group and not await is_admin(update, context):
        await update.message.reply_text("Only group admins can unsubscribe this chat.")
        return

    state["subscribers"].pop(chat_id, None)
    db_remove_subscriber(chat_id)
    await update.message.reply_text("Unsubscribed. Send /start anytime to re-subscribe.")
    logger.info(f"Unsubscribed chat_id: {chat_id}")


async def setmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if chat_id not in state["subscribers"]:
        await update.message.reply_text("Please send /start first to subscribe.")
        return

    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can change the threshold.")
        return

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

    state["subscribers"][chat_id] = amount
    db_set_min_usd(chat_id, amount)
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
    if not price:
        await update.message.reply_text("Could not fetch price right now. Try again shortly.")
        return

    chat_id = update.effective_chat.id
    min_usd = state["subscribers"].get(chat_id, 0)
    deso_thresh = f"{min_usd / price:,.2f} DESO" if min_usd > 0 else "N/A"

    await update.message.reply_text(
        f"DeSo price: <b>${price:,.4f}</b>\n"
        f"Threshold: <b>{'${:,.2f}'.format(min_usd) if min_usd > 0 else 'None'}</b>"
        f"{f' = {deso_thresh}' if min_usd > 0 else ''}",
        parse_mode="HTML",
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    price = get_deso_price()
    min_usd = state["subscribers"].get(chat_id, 0)
    price_str = f"${price:,.4f}" if price else "unavailable"
    threshold_str = f"${min_usd:,.2f}" if min_usd > 0 else "None (all unstakes)"
    deso_equiv = f"(~{min_usd / price:,.2f} DESO)" if price and min_usd > 0 else ""

    await update.message.reply_text(
        f"<b>Settings for this chat</b>\n\n"
        f"DeSo price: <b>{price_str}</b>\n"
        f"Min threshold: <b>{threshold_str}</b> {deso_equiv}\n"
        f"Total subscribers: <b>{len(state['subscribers'])}</b>\n"
        f"Last block: <code>{state['last_block']}</code>",
        parse_mode="HTML",
    )


# ── Polling loop ──────────────────────────────────────────────────────────────

async def check_unstakes(context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = dict(state["subscribers"])
    if not subscribers:
        logger.warning("No subscribers.")
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
                    msg = build_notification(txn, height, deso_amount, usd_value)

                    for chat_id, min_usd in subscribers.items():
                        if min_usd > 0 and (usd_value is None or usd_value < min_usd):
                            logger.info(f"Skipping chat {chat_id} — below threshold")
                            continue
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=msg,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )

                    logger.info(f"Processed unstake in block #{height}")

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

    app.job_queue.run_repeating(check_unstakes, interval=POLL_INTERVAL, first=5)

    logger.info("DeSo Unstake Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
