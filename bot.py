"""
Dormant Whale Activity Telegram Bot
=====================================
Polling-only version — no Flask, no webhook server.

Monitors ETH + BSC whale wallets via Moralis (APScheduler).
Solana (Helius) requires a public HTTP endpoint; add that back
as a separate service once you've confirmed commands work.

Deploy on Railway as a WORKER (Procfile: "worker: python bot.py").
No PORT binding, no web dyno — just a long-running Python process.
"""

import asyncio
import logging
import os
import sys
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from moralis_monitor import check_evm_whales
from whale_store import WhaleStore

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("whale_bot")


# ── Env validation ────────────────────────────────────────────────────────────
def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        log.critical("MISSING required environment variable: %s — exiting", name)
        sys.exit(1)
    return val


TELEGRAM_TOKEN   = _require_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")
MORALIS_API_KEY  = _require_env("MORALIS_API_KEY")

MIN_USD_VALUE     = int(os.environ.get("MIN_USD_VALUE",    "10000"))
DORMANT_DAYS_MIN  = int(os.environ.get("DORMANT_DAYS_MIN", "45"))
MORALIS_POLL_MINS = int(os.environ.get("MORALIS_POLL_MINS","5"))
WHALES_FILE       = os.environ.get("WHALES_FILE", "whales.json")

store = WhaleStore(WHALES_FILE)

# Set in _post_init, used by APScheduler thread to dispatch alerts
_app:        Application | None                  = None
_event_loop: asyncio.AbstractEventLoop | None    = None


# ── Alert dispatch (thread-safe from APScheduler) ─────────────────────────────

async def _send_telegram(text: str) -> None:
    try:
        await _app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        log.error("Failed to send alert: %s", e)


def dispatch_alert(text: str) -> None:
    """Schedule an alert coroutine on the PTB event loop from any thread."""
    if _app is None or _event_loop is None:
        log.warning("Alert dropped — bot not ready: %s", text[:60])
        return
    asyncio.run_coroutine_threadsafe(_send_telegram(text), _event_loop)


# ══════════════════════════════════════════════════════════════════════════════
# Command handlers
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/start received from user_id=%s", update.effective_user.id)
    await update.message.reply_text(
        "🐳 <b>Dormant Whale Monitor</b>\n\n"
        "I watch whale wallets and alert you when a dormant wallet "
        "wakes up with a large memecoin move.\n\n"
        "<b>Commands:</b>\n"
        "• /addwhale <code>&lt;address&gt; &lt;chain&gt;</code> — track a wallet\n"
        "• /listwhales — show all tracked wallets\n"
        "• /removewhale <code>&lt;address&gt;</code> — stop tracking\n"
        "• /status — bot config and stats\n"
        "• /help — show this message\n\n"
        "<i>Chains supported: sol (alerts via polling), eth, bsc</i>",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/help received from user_id=%s", update.effective_user.id)
    await update.message.reply_text(
        "📖 <b>Whale Bot Help</b>\n\n"
        "<b>/addwhale</b> <code>&lt;address&gt; &lt;chain&gt;</code>\n"
        "  Add a wallet to monitor. Example:\n"
        "  <code>/addwhale 0xABC...123 eth</code>\n\n"
        "<b>/listwhales</b>\n"
        "  List all tracked wallets with last-seen dates.\n\n"
        "<b>/removewhale</b> <code>&lt;address&gt;</code>\n"
        "  Stop monitoring a wallet.\n\n"
        "<b>/status</b>\n"
        "  Show current config: thresholds, poll interval, wallet count.\n\n"
        "💡 <i>Alerts fire when a wallet dormant for "
        f"{DORMANT_DAYS_MIN}+ days moves more than ${MIN_USD_VALUE:,} in a single tx.</i>",
        parse_mode="HTML",
    )


async def cmd_addwhale(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/addwhale received from user_id=%s args=%s", update.effective_user.id, ctx.args)
    args = ctx.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /addwhale <code>&lt;address&gt; &lt;chain&gt;</code>\n"
            "Chains: <code>sol</code>, <code>eth</code>, <code>bsc</code>\n\n"
            "Example: <code>/addwhale 0xABC...123 eth</code>",
            parse_mode="HTML",
        )
        return

    address = args[0].strip()
    chain   = args[1].strip().lower()

    if chain not in ("sol", "eth", "bsc"):
        await update.message.reply_text(
            "⚠️ Chain must be <code>sol</code>, <code>eth</code>, or <code>bsc</code>.",
            parse_mode="HTML",
        )
        return

    store.add_whale(address, chain)
    log.info("Whale added: %s (%s)", address, chain)

    note = ""
    if chain == "sol":
        note = (
            "\n\n⚠️ <i>Solana monitoring in polling-only mode requires a Helius webhook "
            "endpoint. This wallet is saved but won't trigger alerts until you "
            "re-enable the Flask webhook server.</i>"
        )

    await update.message.reply_text(
        f"✅ Now tracking <code>{address}</code> ({chain.upper()}){note}",
        parse_mode="HTML",
    )


async def cmd_listwhales(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/listwhales received from user_id=%s", update.effective_user.id)
    whales = store.list_whales()
    if not whales:
        await update.message.reply_text(
            "No whales tracked yet.\nUse /addwhale to add one."
        )
        return

    lines = [f"<b>Tracked Whales ({len(whales)})</b>\n"]
    for w in whales:
        addr        = w["address"]
        short       = f"{addr[:6]}…{addr[-4:]}"
        chain       = w["chain"].upper()
        last        = w.get("last_active_date") or "never seen"
        dormant     = w.get("dormant_days", -1)
        dormant_str = f"{dormant}d ago" if dormant >= 0 else "unknown"
        lines.append(
            f"• <code>{short}</code> [{chain}]\n"
            f"  Last active: {last} ({dormant_str})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_removewhale(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/removewhale received from user_id=%s", update.effective_user.id)
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Usage: /removewhale <code>&lt;address&gt;</code>", parse_mode="HTML")
        return

    address = args[0].strip()
    if store.remove_whale(address):
        log.info("Whale removed: %s", address)
        await update.message.reply_text(
            f"🗑 Removed <code>{address}</code>", parse_mode="HTML"
        )
    else:
        await update.message.reply_text("⚠️ Address not found in whale list.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/status received from user_id=%s", update.effective_user.id)
    whales    = store.list_whales()
    sol_count = sum(1 for w in whales if w["chain"] == "sol")
    evm_count = len(whales) - sol_count

    await update.message.reply_text(
        f"<b>🐳 Whale Bot Status</b>\n"
        f"{'─' * 22}\n"
        f"🟢 Status: Running\n"
        f"📋 Wallets tracked: {len(whales)}\n"
        f"   SOL: {sol_count}  |  ETH+BSC: {evm_count}\n"
        f"💰 Min alert value: ${MIN_USD_VALUE:,}\n"
        f"💤 Dormancy min: {DORMANT_DAYS_MIN} days\n"
        f"🔄 EVM poll: every {MORALIS_POLL_MINS} min\n"
        f"⚡ Mode: polling-only (no webhook server)",
        parse_mode="HTML",
    )


async def fallback_echo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catches any plain text that isn't a command.
    Useful for diagnosing delivery issues — if this replies, polling works.
    Safe to remove once you've confirmed /start responds.
    """
    text = update.message.text if update.message else ""
    log.info("Fallback message from user_id=%s: %s", update.effective_user.id, text[:60])
    await update.message.reply_text(
        "✅ Bot is alive!\n"
        "I received your message. Try /start for the command list.",
    )


# ══════════════════════════════════════════════════════════════════════════════
# PTB lifecycle hooks
# ══════════════════════════════════════════════════════════════════════════════

async def _post_init(application: Application) -> None:
    """
    Runs inside the PTB event loop before polling starts.
    - Deletes any registered webhook (would silently block polling)
    - Confirms bot identity via get_me()
    - Captures the event loop for APScheduler thread-safe dispatch
    """
    global _event_loop
    _event_loop = asyncio.get_running_loop()

    # Delete webhook — MUST happen before polling or Telegram ignores getUpdates
    try:
        deleted = await application.bot.delete_webhook(drop_pending_updates=True)
        log.info("delete_webhook: %s", "cleared" if deleted else "nothing to clear")
    except TelegramError as e:
        log.warning("delete_webhook failed (non-fatal): %s", e)

    # Confirm token + API reachability
    try:
        me = await application.bot.get_me()
        log.info("Bot identity: @%s (id=%s)", me.username, me.id)
    except TelegramError as e:
        log.error("get_me() failed — is TELEGRAM_TOKEN correct? %s", e)
        sys.exit(1)   # no point continuing if token is broken

    # Print startup summary
    whales = store.list_whales()
    print("", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print("🐳  Dormant Whale Monitor — READY",   flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"   Mode            : polling-only",   flush=True)
    print(f"   Wallets tracked : {len(whales)}",  flush=True)
    print(f"   Min USD value   : ${MIN_USD_VALUE:,}", flush=True)
    print(f"   Dormancy min    : {DORMANT_DAYS_MIN} days", flush=True)
    print(f"   EVM poll every  : {MORALIS_POLL_MINS} min", flush=True)
    print(f"   Store file      : {WHALES_FILE}",  flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print("", flush=True)


async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Keeps the bot alive through network blips and bad updates.
    TimedOut / NetworkError are normal on Railway — log at WARNING, not ERROR.
    """
    err = ctx.error
    if isinstance(err, (TimedOut, NetworkError)):
        log.warning("Network issue (will retry): %s", err)
    else:
        log.error("Unhandled PTB error: %s", err, exc_info=err)


# ══════════════════════════════════════════════════════════════════════════════
# APScheduler — EVM polling via Moralis
# ══════════════════════════════════════════════════════════════════════════════

def _poll_evm_whales() -> None:
    evm_whales = [w for w in store.list_whales() if w["chain"] in ("eth", "bsc")]
    if not evm_whales:
        return
    log.info("EVM poll: checking %d wallet(s)…", len(evm_whales))
    try:
        alerts = check_evm_whales(
            evm_whales, MORALIS_API_KEY, store, MIN_USD_VALUE, DORMANT_DAYS_MIN
        )
        for msg in alerts:
            dispatch_alert(msg)
        if alerts:
            log.info("EVM poll: sent %d alert(s)", len(alerts))
    except Exception:
        log.exception("EVM poll error")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global _app

    log.info("Building Telegram application…")

    _app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(_post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_read_timeout(45)   # Telegram long-polls for ~30s; give it headroom
        .build()
    )

    # Commands
    _app.add_handler(CommandHandler("start",       cmd_start))
    _app.add_handler(CommandHandler("help",        cmd_help))
    _app.add_handler(CommandHandler("addwhale",    cmd_addwhale))
    _app.add_handler(CommandHandler("listwhales",  cmd_listwhales))
    _app.add_handler(CommandHandler("removewhale", cmd_removewhale))
    _app.add_handler(CommandHandler("status",      cmd_status))

    # Fallback: plain text → confirms polling is working end-to-end
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_echo))

    # Error handler
    _app.add_error_handler(_error_handler)

    # EVM polling scheduler
    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={"misfire_grace_time": 60, "coalesce": True},
    )
    scheduler.add_job(_poll_evm_whales, "interval", minutes=MORALIS_POLL_MINS, id="evm_poll")
    scheduler.start()
    log.info("APScheduler started — EVM poll every %d min", MORALIS_POLL_MINS)

    # Blocking — exits only on SIGTERM (Railway redeploy) or SIGINT (Ctrl-C)
    log.info("Starting Telegram polling…")
    _app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    log.info("Shutting down…")
    scheduler.shutdown(wait=False)
    log.info("Clean exit.")


if __name__ == "__main__":
    main()
