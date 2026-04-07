import logging
import os
import json
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # Set this env var OR use /start
DESO_NODE = "https://node.deso.org"
POLL_INTERVAL = 30  # seconds between block checks
UNSTAKE_TXN_TYPE = 37  # TxnTypeJSON for UNSTAKE in DeSo protocol

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# In-memory state (resets on restart — fine for a notification bot)
state = {
    "last_block": 0,
    "chat_ids": set(),
}


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


def nanos_to_deso(nanos) -> str:
    if not nanos:
        return "Unknown"
    return f"{int(nanos) / 1e9:,.4f} DESO"


def short_key(key: str) -> str:
    if not key or key == "Unknown":
        return "Unknown"
    return f"{key[:8]}...{key[-6:]}" if len(key) > 14 else key


def build_notification(txn: dict, height: int) -> str:
    staker = txn.get("PublicKeyBase58Check", "Unknown")
    txn_hash = txn.get("TransactionIDBase58Check", "Unknown")
    meta = txn.get("TxnMeta") or {}

    validator = meta.get("ValidatorPublicKeyBase58Check", "Unknown")
    amount_nanos = meta.get("UnstakeAmountNanos") or meta.get("StakeAmountNanos")

    return (
        f"<b>DeSo Unstake Detected</b>\n\n"
        f"Block: <code>#{height}</code>\n"
        f"Staker: <code>{short_key(staker)}</code>\n"
        f"Validator: <code>{short_key(validator)}</code>\n"
        f"Amount: <b>{nanos_to_deso(amount_nanos)}</b>\n"
        f"Tx: <code>{short_key(txn_hash)}</code>\n"
        f"<a href=\"https://explorer.deso.com/txn/{txn_hash}\">View on Explorer</a>"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state["chat_ids"].add(chat_id)
    await update.message.reply_text(
        f"DeSo Unstake Monitor active!\n\n"
        f"Your chat ID: <code>{chat_id}</code>\n\n"
        f"You'll receive a notification whenever an UNSTAKE transaction is detected on the DeSo blockchain.",
        parse_mode="HTML",
    )
    logger.info(f"Registered chat_id: {chat_id}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    last = state["last_block"]
    await update.message.reply_text(
        f"Status: Running\nLast block checked: <code>{last}</code>",
        parse_mode="HTML",
    )


async def check_unstakes(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Collect all chat IDs (env var + /start registrations)
    chat_ids = set(state["chat_ids"])
    if TELEGRAM_CHAT_ID:
        chat_ids.add(int(TELEGRAM_CHAT_ID))

    if not chat_ids:
        logger.warning("No chat IDs registered. Send /start to the bot or set TELEGRAM_CHAT_ID.")
        return

    try:
        latest_height = get_latest_block_height()

        # On first run, start from the current tip (don't backfill)
        if state["last_block"] == 0:
            state["last_block"] = latest_height
            logger.info(f"Initialized at block #{latest_height}")
            return

        if latest_height <= state["last_block"]:
            return  # No new blocks

        for height in range(state["last_block"] + 1, latest_height + 1):
            logger.info(f"Scanning block #{height}")
            try:
                block = get_block(height)
                transactions = block.get("Transactions") or []

                for txn in transactions:
                    if txn.get("TxnTypeJSON") == UNSTAKE_TXN_TYPE:
                        msg = build_notification(txn, height)
                        for chat_id in chat_ids:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=msg,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                        logger.info(f"Notified unstake in block #{height}")

            except Exception as e:
                logger.error(f"Error processing block #{height}: {e}")

            state["last_block"] = height

    except Exception as e:
        logger.error(f"Error in check_unstakes: {e}")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))

    app.job_queue.run_repeating(check_unstakes, interval=POLL_INTERVAL, first=5)

    logger.info("DeSo Unstake Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
