"""
Dormant Whale Activity Telegram Bot
=====================================
Monitors known whale wallets across Solana, ETH, and BSC.
Alerts when a dormant whale (45+ days inactive) makes a significant
memecoin move (> $10k USD).

Architecture:
  - python-telegram-bot v21+ with ApplicationBuilder + run_polling()
  - Flask handles incoming Helius webhooks (Solana) in a daemon thread
  - APScheduler polls Moralis every N minutes for ETH/BSC wallets
  - Cross-thread alerts sent safely via asyncio.run_coroutine_threadsafe()
    using the Application's event loop (the correct PTB v21 approach)
  - whales.json is the persistent wallet store

Railway notes:
  - Procfile uses "worker: python bot.py" (no web dyno / PORT needed)
  - Set a Volume mounted at /app so whales.json survives redeploys
  - All config comes from environment variables (see .env.example)
"""

import asyncio
import logging
import os
import sys
import threading

from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from alert_builder import build_alert, parse_helius_event
from helius_webhook import register_address_with_helius, verify_helius_signature
from moralis_monitor import check_evm_whales
from whale_store import WhaleStore

# ── Logging ───────────────────────────────────────────────────────────────────
# stdout + structured format works well with Railway's log viewer
logging.basicConfig(
    stream=sys.stdout,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
    force=True,   # override any library-level root handlers
)
# Silence noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

log = logging.getLogger("whale_bot")


# ── Startup env validation ────────────────────────────────────────────────────
def _require_env(name: str) -> str:
    """Exit immediately with a clear message if a required env var is missing."""
    val = os.environ.get(name, "").strip()
    if not val:
        log.critical("MISSING required environment variable: %s", name)
        sys.exit(1)
    return val


TELEGRAM_TOKEN        = _require_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID      = _require_env("TELEGRAM_CHAT_ID")
HELIUS_API_KEY        = _require_env("HELIUS_API_KEY")
MORALIS_API_KEY       = _require_env("MORALIS_API_KEY")

# Optional / defaulted
HELIUS_WEBHOOK_SECRET = os.environ.get("HELIUS_WEBHOOK_SECRET", "")
HELIUS_WEBHOOK_URL    = os.environ.get("HELIUS_WEBHOOK_URL", "")
MIN_USD_VALUE         = int(os.environ.get("MIN_USD_VALUE", "10000"))
DORMANT_DAYS_MIN      = int(os.environ.get("DORMANT_DAYS_MIN", "45"))
MORALIS_POLL_MINS     = int(os.environ.get("MORALIS_POLL_MINS", "5"))
FLASK_PORT            = int(os.environ.get("PORT", "8080"))

# Whale store (JSON file — mount /app as Railway Volume for persistence)
WHALES_FILE = os.environ.get("WHALES_FILE", "whales.json")
store = WhaleStore(WHALES_FILE)

# PTB Application and its event loop — set in main() / post_init before threads use them
_app: Application | None = None
_event_loop: asyncio.AbstractEventLoop | None = None


# ══════════════════════════════════════════════════════════════════════════════
# Alert dispatch (thread-safe)
# ══════════════════════════════════════════════════════════════════════════════

async def _send_telegram(message: str) -> None:
    """Coroutine: send a Telegram HTML message. Runs on the PTB event loop."""
    try:
        await _app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        log.error("Telegram send failed: %s", e)


def dispatch_alert(message: str) -> None:
    """
    Thread-safe alert sender for Flask / APScheduler threads.

    PTB v21 runs its own asyncio event loop in the main thread.
    We schedule a coroutine on that loop using run_coroutine_threadsafe() —
    the correct PTB v21 pattern. Never access _app.bot._request internals.
    """
    if _app is None or _event_loop is None:
        log.warning("Alert dropped — bot not initialised yet")
        return
    asyncio.run_coroutine_threadsafe(_send_telegram(message), _event_loop)


# ══════════════════════════════════════════════════════════════════════════════
# Telegram command handlers
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🐳 <b>Dormant Whale Monitor</b>\n\n"
        "Track whale wallets on SOL, ETH, and BSC.\n"
        "Alerts fire when a dormant wallet wakes up with a large move.\n\n"
        "<b>Commands:</b>\n"
        "• /addwhale <code>&lt;address&gt; &lt;chain&gt;</code>\n"
        "• /listwhales\n"
        "• /removewhale <code>&lt;address&gt;</code>\n"
        "• /status",
        parse_mode="HTML",
    )


async def cmd_addwhale(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/addwhale <address> <chain>  — chain must be sol | eth | bsc"""
    args = ctx.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /addwhale <address> <chain>\n"
            "Chains: <code>sol</code>, <code>eth</code>, <code>bsc</code>",
            parse_mode="HTML",
        )
        return

    address = args[0].strip()
    chain   = args[1].strip().lower()

    if chain not in ("sol", "eth", "bsc"):
        await update.message.reply_text(
            "Chain must be one of: <code>sol</code>, <code>eth</code>, <code>bsc</code>",
            parse_mode="HTML",
        )
        return

    store.add_whale(address, chain)
    log.info("Added whale: %s (%s)", address, chain)

    helius_note = ""
    if chain == "sol":
        if HELIUS_WEBHOOK_URL:
            ok = register_address_with_helius(address, HELIUS_API_KEY)
            helius_note = "\n✅ Registered with Helius" if ok else "\n⚠️ Helius registration failed — check logs"
        else:
            helius_note = "\n⚠️ HELIUS_WEBHOOK_URL not set — register manually in Helius dashboard"

    await update.message.reply_text(
        f"✅ Now tracking <code>{address}</code> ({chain.upper()}){helius_note}",
        parse_mode="HTML",
    )


async def cmd_listwhales(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    whales = store.list_whales()
    if not whales:
        await update.message.reply_text("No whales tracked yet. Use /addwhale to add one.")
        return

    lines = [f"<b>Tracked Whales ({len(whales)})</b>\n"]
    for w in whales:
        addr        = w["address"]
        short       = f"{addr[:6]}…{addr[-4:]}"
        last        = w.get("last_active_date") or "never seen"
        dormant     = w.get("dormant_days", -1)
        dormant_str = f"{dormant}d ago" if dormant >= 0 else "unknown"
        lines.append(
            f"• <code>{short}</code> [{w['chain'].upper()}]\n"
            f"  Last active: {last} ({dormant_str})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_removewhale(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Usage: /removewhale <address>")
        return

    address = args[0].strip()
    if store.remove_whale(address):
        await update.message.reply_text(f"🗑 Removed <code>{address}</code>", parse_mode="HTML")
        log.info("Removed whale: %s", address)
    else:
        await update.message.reply_text("⚠️ Address not found in whale list.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    whales    = store.list_whales()
    sol_count = sum(1 for w in whales if w["chain"] == "sol")
    evm_count = len(whales) - sol_count

    await update.message.reply_text(
        f"<b>🐳 Whale Bot Status</b>\n"
        f"{'─' * 20}\n"
        f"🟢 Status: Running\n"
        f"📋 Wallets tracked: {len(whales)}\n"
        f"   • SOL: {sol_count}  |  EVM: {evm_count}\n"
        f"💰 Min alert value: ${MIN_USD_VALUE:,}\n"
        f"💤 Dormancy threshold: {DORMANT_DAYS_MIN} days\n"
        f"🔄 EVM poll interval: {MORALIS_POLL_MINS} min\n"
        f"📁 Store: {WHALES_FILE}",
        parse_mode="HTML",
    )




# ══════════════════════════════════════════════════════════════════════════════
# Fallback handler — confirms the bot is receiving messages at all
# ══════════════════════════════════════════════════════════════════════════════

async def fallback_echo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Catches any non-command text message and echoes it back.
    Useful for diagnosing "bot starts but doesn't respond" issues on Railway.
    Remove or comment out once you've confirmed commands work.
    """
    text = update.message.text if update.message else "(no text)"
    log.info("Fallback received from %s: %s", update.effective_user.id, text)
    await update.message.reply_text(
        f"✅ Bot is alive and receiving messages.\n"
        f"You sent: <code>{text}</code>\n\n"
        f"Try a command: /start",
        parse_mode="HTML",
    )

# ══════════════════════════════════════════════════════════════════════════════
# Flask server — Helius webhook receiver
# ══════════════════════════════════════════════════════════════════════════════

flask_app = Flask(__name__)


@flask_app.route("/helius-webhook", methods=["POST"])
def helius_webhook_endpoint():
    """
    Helius pushes enhanced transaction events here for all registered SOL addresses.
    Signature is verified if HELIUS_WEBHOOK_SECRET is set.
    """
    raw_body   = request.get_data()
    sig_header = request.headers.get("Helius-Signature", "")

    if HELIUS_WEBHOOK_SECRET and not verify_helius_signature(raw_body, sig_header, HELIUS_WEBHOOK_SECRET):
        log.warning("Helius webhook: invalid signature — rejecting request")
        return jsonify({"error": "invalid signature"}), 401

    events = request.get_json(force=True, silent=True) or []
    if not isinstance(events, list):
        events = [events]

    processed = 0
    for event in events:
        try:
            msg = parse_helius_event(event, store, MIN_USD_VALUE, DORMANT_DAYS_MIN)
            if msg:
                dispatch_alert(msg)
                processed += 1
        except Exception:
            log.exception("Error processing Helius event")

    return jsonify({"ok": True, "processed": processed}), 200


@flask_app.route("/health", methods=["GET"])
def health_check():
    """Health probe — hit https://your-app.railway.app/health to verify."""
    return jsonify({
        "status":         "ok",
        "whales_tracked": len(store.list_whales()),
        "bot_ready":      _app is not None,
    }), 200


def _run_flask() -> None:
    log.info("Flask webhook server listening on port %d", FLASK_PORT)
    flask_app.run(host="0.0.0.0", port=FLASK_PORT, use_reloader=False, debug=False)


# ══════════════════════════════════════════════════════════════════════════════
# APScheduler — EVM polling via Moralis
# ══════════════════════════════════════════════════════════════════════════════

def _poll_evm_whales() -> None:
    """Runs on a background thread every MORALIS_POLL_MINS minutes."""
    evm_whales = [w for w in store.list_whales() if w["chain"] in ("eth", "bsc")]
    if not evm_whales:
        return

    log.info("EVM poll: checking %d wallet(s)…", len(evm_whales))
    try:
        alerts = check_evm_whales(evm_whales, MORALIS_API_KEY, store, MIN_USD_VALUE, DORMANT_DAYS_MIN)
        for msg in alerts:
            dispatch_alert(msg)
        if alerts:
            log.info("EVM poll: dispatched %d alert(s)", len(alerts))
    except Exception:
        log.exception("EVM poll encountered an error")


# ══════════════════════════════════════════════════════════════════════════════
# PTB hooks
# ══════════════════════════════════════════════════════════════════════════════

async def _post_init(application: Application) -> None:
    """
    Called by PTB after the event loop is running, before polling begins.
    Capture the running loop here — this is the correct PTB v21 pattern
    for enabling threadsafe alert dispatch from Flask / APScheduler threads.
    """
    global _event_loop
    _event_loop = asyncio.get_running_loop()

    # ── CRITICAL: delete any registered webhook ───────────────────────────────
    # Telegram will NOT deliver polling updates if a webhook URL is set.
    # This happens silently — the bot starts fine but never receives messages.
    # Always delete on startup so polling works cleanly.
    try:
        deleted = await application.bot.delete_webhook(drop_pending_updates=True)
        if deleted:
            log.info("Webhook deleted — polling will now receive updates")
        else:
            log.info("No webhook was registered")
    except TelegramError as e:
        log.warning("Could not delete webhook (non-fatal): %s", e)

    # Confirm bot identity — proves the token works and API is reachable
    try:
        me = await application.bot.get_me()
        log.info("Bot identity confirmed: @%s (id=%s)", me.username, me.id)
    except TelegramError as e:
        log.error("get_me() failed — check TELEGRAM_TOKEN: %s", e)

    # Startup health summary printed to Railway logs
    whales = store.list_whales()
    print("", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print("🐳  Dormant Whale Monitor — READY", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"   Wallets tracked : {len(whales)}", flush=True)
    print(f"   Min USD value   : ${MIN_USD_VALUE:,}", flush=True)
    print(f"   Dormancy min    : {DORMANT_DAYS_MIN} days", flush=True)
    print(f"   EVM poll every  : {MORALIS_POLL_MINS} min", flush=True)
    print(f"   Webhook port    : {FLASK_PORT}", flush=True)
    print(f"   Store file      : {WHALES_FILE}", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print("", flush=True)

    log.info("PTB event loop ready — threadsafe alerts enabled")


async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global PTB error handler — logs exceptions without crashing the bot.
    Without this, unhandled errors in handlers silently kill polling on some PTB versions.
    """
    log.error("Unhandled PTB error (update=%s): %s", update, ctx.error, exc_info=ctx.error)


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global _app

    log.info("Building Telegram application…")

    _app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(_post_init)          # captures loop + deletes webhook before polling
        .connect_timeout(30)            # seconds to establish connection to Telegram
        .read_timeout(30)               # seconds to wait for a response
        .write_timeout(30)              # seconds to wait when sending
        .pool_timeout(30)               # seconds to wait for a connection from the pool
        .get_updates_read_timeout(45)   # long-poll window; Telegram holds for ~30s
        .build()
    )

    # Command handlers
    _app.add_handler(CommandHandler("start",       cmd_start))
    _app.add_handler(CommandHandler("addwhale",    cmd_addwhale))
    _app.add_handler(CommandHandler("listwhales",  cmd_listwhales))
    _app.add_handler(CommandHandler("removewhale", cmd_removewhale))
    _app.add_handler(CommandHandler("status",      cmd_status))

    # Fallback: catches plain text messages — remove once commands confirmed working
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_echo))

    # Global error handler — keeps the bot alive on bad updates or network blips
    _app.add_error_handler(_error_handler)

    # Flask in a daemon thread (dies when main thread exits)
    flask_thread = threading.Thread(target=_run_flask, name="flask-webhook", daemon=True)
    flask_thread.start()

    # APScheduler for EVM polling
    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={"misfire_grace_time": 60, "coalesce": True},
    )
    scheduler.add_job(_poll_evm_whales, "interval", minutes=MORALIS_POLL_MINS, id="evm_poll")
    scheduler.start()
    log.info("APScheduler started — EVM poll every %d min", MORALIS_POLL_MINS)

    # run_polling blocks until SIGINT / SIGTERM (Railway sends SIGTERM on redeploy)
    # drop_pending_updates=True avoids replaying a command backlog on restart
    log.info("Starting Telegram polling (drop_pending_updates=True)…")
    _app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    # Reached only after Ctrl-C / SIGTERM
    log.info("Shutting down scheduler…")
    scheduler.shutdown(wait=False)
    log.info("Bot stopped cleanly.")


if __name__ == "__main__":
    main()
