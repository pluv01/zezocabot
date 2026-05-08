"""
Dormant Whale Activity Telegram Bot — Polling Mode
====================================================
Runs as a Railway WORKER service. No public domain or port needed.
Telegram polling works reliably on Railway workers once any stale
webhook is deleted on startup (handled automatically in _post_init).

Architecture:
  - PTB Application with run_polling() — simple, no web server needed
  - APScheduler polls Moralis every N minutes for ETH/BSC whale activity
  - whales.json stores wallet list (mount a Railway Volume at /app)

Required env vars  (set in Railway → Variables):
  TELEGRAM_TOKEN          BotFather token
  TELEGRAM_CHAT_ID        Your personal Telegram user/chat ID
  MORALIS_API_KEY         Moralis Web3 API key

Optional env vars:
  HELIUS_API_KEY          Needed for Solana autopopulate holder lookups
  MIN_USD_VALUE           Default 10000
  DORMANT_DAYS_MIN        Default 45
  MORALIS_POLL_MINS       Default 5
  AUTO_POPULATE_HOUR      UTC hour for daily scan, default 6
  WHALES_FILE             Default whales.json
"""

import asyncio
import logging
import os
import sys

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

from autopopulate import find_dormant_whale_wallets
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
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

log = logging.getLogger("whale_bot")


# ── Env validation ────────────────────────────────────────────────────────────
def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        log.critical("MISSING required env var: %s — exiting", name)
        sys.exit(1)
    return val


TELEGRAM_TOKEN   = _require_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")
MORALIS_API_KEY  = _require_env("MORALIS_API_KEY")

# No webhook server needed — polling mode works reliably on Railway workers.

MIN_USD_VALUE     = int(os.environ.get("MIN_USD_VALUE",    "10000"))
DORMANT_DAYS_MIN  = int(os.environ.get("DORMANT_DAYS_MIN", "45"))
MORALIS_POLL_MINS = int(os.environ.get("MORALIS_POLL_MINS","5"))
WHALES_FILE           = os.environ.get("WHALES_FILE", "whales.json")
HELIUS_API_KEY        = os.environ.get("HELIUS_API_KEY", "")
AUTO_POPULATE_HOUR    = int(os.environ.get("AUTO_POPULATE_HOUR", "6"))  # UTC hour for daily run

store = WhaleStore(WHALES_FILE)

_app:        Application | None               = None
_event_loop: asyncio.AbstractEventLoop | None = None


# ── Alert dispatch ────────────────────────────────────────────────────────────

async def _send_telegram(text: str) -> None:
    try:
        await _app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        log.error("Alert send failed: %s", e)


def dispatch_alert(text: str) -> None:
    """Thread-safe: schedule alert on PTB event loop from APScheduler thread."""
    if _app is None or _event_loop is None:
        log.warning("Alert dropped — bot not ready")
        return
    asyncio.run_coroutine_threadsafe(_send_telegram(text), _event_loop)


# ══════════════════════════════════════════════════════════════════════════════
# Update logger — log EVERY incoming update for debugging
# ══════════════════════════════════════════════════════════════════════════════

async def log_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs before every handler via the update_queue pre-processor.
    Logs the raw update so you can confirm Railway is receiving Telegram pushes.
    Remove or set LOG_UPDATES=false once confirmed working.
    """
    if os.environ.get("LOG_UPDATES", "true").lower() != "false":
        log.info(
            "UPDATE received: type=%s from=user_id:%s chat_id:%s text=%r",
            list(update._get_attrs())[0] if update else "?",
            update.effective_user.id   if update.effective_user else "none",
            update.effective_chat.id   if update.effective_chat else "none",
            (update.message.text[:60] if update.message and update.message.text else ""),
        )


# ══════════════════════════════════════════════════════════════════════════════
# Command handlers
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/start from user_id=%s chat_id=%s",
             update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(
        "🐳 <b>Dormant Whale Monitor</b>\n\n"
        "I alert you when a dormant whale wallet wakes up with a large memecoin move.\n\n"
        "<b>Commands:</b>\n"
        "• /addwhale <code>&lt;address&gt; &lt;chain&gt;</code>\n"
        "• /listwhales\n"
        "• /removewhale <code>&lt;address&gt;</code>\n"
        "• /status\n"
        "• /help",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/help from user_id=%s", update.effective_user.id)
    await update.message.reply_text(
        "📖 <b>Whale Bot Help</b>\n\n"
        "<b>/addwhale</b> <code>&lt;address&gt; &lt;chain&gt;</code>\n"
        "  Example: <code>/addwhale 0xABC...123 eth</code>\n"
        "  Chains: <code>sol</code>, <code>eth</code>, <code>bsc</code>\n\n"
        "<b>/listwhales</b> — all tracked wallets\n\n"
        "<b>/removewhale</b> <code>&lt;address&gt;</code> — stop tracking\n\n"
        "<b>/status</b> — config and stats\n\n"
        f"💡 Alerts fire when a wallet dormant {DORMANT_DAYS_MIN}+ days "
        f"moves more than ${MIN_USD_VALUE:,}.",
        parse_mode="HTML",
    )


async def cmd_addwhale(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/addwhale from user_id=%s args=%s", update.effective_user.id, ctx.args)
    args = ctx.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /addwhale <code>&lt;address&gt; &lt;chain&gt;</code>\n"
            "Example: <code>/addwhale 0xABC...123 eth</code>",
            parse_mode="HTML",
        )
        return

    address = args[0].strip()
    chain   = args[1].strip().lower()
    if chain not in ("sol", "eth", "bsc"):
        await update.message.reply_text(
            "Chain must be <code>sol</code>, <code>eth</code>, or <code>bsc</code>.",
            parse_mode="HTML",
        )
        return

    store.add_whale(address, chain)
    log.info("Whale added: %s (%s)", address, chain)

    note = ""
    if chain == "sol":
        note = "\n\n⚠️ <i>Solana alerts require Helius webhook integration (not active in this build).</i>"

    await update.message.reply_text(
        f"✅ Tracking <code>{address}</code> ({chain.upper()}){note}",
        parse_mode="HTML",
    )


async def cmd_listwhales(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/listwhales from user_id=%s", update.effective_user.id)
    whales = store.list_whales()
    if not whales:
        await update.message.reply_text("No whales tracked yet. Use /addwhale.")
        return

    lines = [f"<b>Tracked Whales ({len(whales)})</b>\n"]
    for w in whales:
        addr    = w["address"]
        short   = f"{addr[:6]}…{addr[-4:]}"
        last    = w.get("last_active_date") or "never seen"
        dormant = w.get("dormant_days", -1)
        d_str   = f"{dormant}d ago" if dormant >= 0 else "unknown"
        lines.append(f"• <code>{short}</code> [{w['chain'].upper()}] — {last} ({d_str})")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_removewhale(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/removewhale from user_id=%s", update.effective_user.id)
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /removewhale <code>&lt;address&gt;</code>", parse_mode="HTML"
        )
        return
    address = args[0].strip()
    if store.remove_whale(address):
        log.info("Whale removed: %s", address)
        await update.message.reply_text(f"🗑 Removed <code>{address}</code>", parse_mode="HTML")
    else:
        await update.message.reply_text("⚠️ Address not found.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/status from user_id=%s", update.effective_user.id)
    whales    = store.list_whales()
    sol_count = sum(1 for w in whales if w["chain"] == "sol")
    evm_count = len(whales) - sol_count
    await update.message.reply_text(
        f"<b>🐳 Whale Bot Status</b>\n"
        f"{'─' * 22}\n"
        f"🟢 Running (polling mode)\n"
        f"📋 Wallets: {len(whales)} (SOL: {sol_count} | EVM: {evm_count})\n"
        f"💰 Min alert: ${MIN_USD_VALUE:,}\n"
        f"💤 Dormancy: {DORMANT_DAYS_MIN} days\n"
        f"🔄 EVM poll: {MORALIS_POLL_MINS} min",
        parse_mode="HTML",
    )


async def fallback_echo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo handler — confirms end-to-end delivery. Remove once commands work."""
    text = update.message.text if update.message else ""
    log.info("Fallback echo from user_id=%s: %r", update.effective_user.id, text[:60])
    await update.message.reply_text(
        "✅ Webhook delivery confirmed — bot is alive!\n"
        "Try /start for commands.",
    )


# ══════════════════════════════════════════════════════════════════════════════
# PTB lifecycle hooks
# ══════════════════════════════════════════════════════════════════════════════

async def _post_init(application: Application) -> None:
    """
    Runs inside the PTB event loop before polling starts.
    Deletes any stale webhook (would silently block polling) and confirms identity.
    """
    global _event_loop
    _event_loop = asyncio.get_running_loop()

    # Delete any registered webhook — if one exists, Telegram ignores getUpdates entirely
    try:
        deleted = await application.bot.delete_webhook(drop_pending_updates=True)
        log.info("delete_webhook: %s", "cleared stale webhook" if deleted else "none registered")
    except TelegramError as e:
        log.warning("delete_webhook failed (non-fatal): %s", e)

    # Confirm token + API reachability — exits cleanly if token is wrong
    try:
        me = await application.bot.get_me()
        log.info("Bot identity: @%s (id=%s)", me.username, me.id)
    except TelegramError as e:
        log.critical("get_me() failed — check TELEGRAM_TOKEN: %s", e)
        sys.exit(1)

    # Startup summary
    whales = store.list_whales()
    print("", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print("🐳  Dormant Whale Monitor — READY",   flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print(f"   Mode            : polling",        flush=True)
    print(f"   Wallets tracked : {len(whales)}",  flush=True)
    print(f"   Min USD value   : ${MIN_USD_VALUE:,}", flush=True)
    print(f"   Dormancy min    : {DORMANT_DAYS_MIN} days", flush=True)
    print(f"   EVM poll every  : {MORALIS_POLL_MINS} min", flush=True)
    print(f"   Auto-populate   : daily at {AUTO_POPULATE_HOUR:02d}:00 UTC", flush=True)
    print(f"   Helius key      : {'set' if HELIUS_API_KEY else 'NOT SET (SOL disabled)'}", flush=True)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", flush=True)
    print("", flush=True)


async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    err = ctx.error
    if isinstance(err, (TimedOut, NetworkError)):
        log.warning("Network issue (transient): %s", err)
    else:
        log.error("Unhandled PTB error: %s", err, exc_info=err)




# ══════════════════════════════════════════════════════════════════════════════
# Auto-populate commands
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_autopopulate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /autopopulate [chain]
    Scans trending memecoins, finds wallets with large positions that
    have been INACTIVE for DORMANT_DAYS_MIN+ days, and adds them to tracking.
    """
    chain = (ctx.args[0].strip().lower() if ctx.args else "sol")
    if chain not in ("sol", "eth", "bsc"):
        await update.message.reply_text(
            "Usage: /autopopulate [chain]\\n"
            "Chains: <code>sol</code> (default), <code>eth</code>, <code>bsc</code>",
            parse_mode="HTML",
        )
        return

    log.info("/autopopulate chain=%s by user_id=%s", chain, update.effective_user.id)
    status_msg = await update.message.reply_text(
        f"🔍 Scanning trending memecoins on <b>{chain.upper()}</b> for dormant whales…\\n"
        f"<i>Filtering for wallets inactive {DORMANT_DAYS_MIN}+ days. Takes ~30s.</i>",
        parse_mode="HTML",
    )

    try:
        existing = {w["address"] for w in store.list_whales()}
        new_whales = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: find_dormant_whale_wallets(
                chain=chain,
                dormant_days_min=DORMANT_DAYS_MIN,
                helius_api_key=HELIUS_API_KEY,
                moralis_api_key=MORALIS_API_KEY,
                existing_addresses=existing,
            )
        )

        if not new_whales:
            await status_msg.edit_text(
                f"😴 No new dormant whales found on {chain.upper()} this scan.\\n"
                f"Try again later — trending tokens rotate frequently."
            )
            return

        for w in new_whales:
            store.add_whale(w["address"], w["chain"])
            # Write dormancy metadata directly if available
            if w.get("last_active_ts"):
                store.update_last_active(w["address"], w["last_active_ts"])

        lines = [f"✅ <b>Added {len(new_whales)} dormant whale(s) on {chain.upper()}</b>\\n"]
        for w in new_whales[:15]:   # show first 15 to avoid Telegram message length limit
            addr    = w["address"]
            dormant = w.get("dormant_days", "?")
            last    = w.get("last_active_date", "unknown")
            lines.append(f"• <code>{addr}</code> — {dormant}d dormant (last: {last})")

        if len(new_whales) > 15:
            lines.append(f"<i>…and {len(new_whales) - 15} more. Use /listwhales to see all.</i>")

        await status_msg.edit_text("\\n".join(lines), parse_mode="HTML")
        log.info("Auto-populate added %d whales for %s", len(new_whales), chain)

    except Exception as e:
        log.exception("Auto-populate failed for %s", chain)
        await status_msg.edit_text(f"❌ Auto-populate failed: {e}\\nCheck logs for details.")


async def cmd_autopopulate_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/autopopulateall — runs autopopulate across sol, eth, and bsc sequentially."""
    log.info("/autopopulateall by user_id=%s", update.effective_user.id)
    await update.message.reply_text(
        "🌐 Running auto-populate across <b>SOL → ETH → BSC</b>…\\n"
        "<i>This will take ~90 seconds total.</i>",
        parse_mode="HTML",
    )
    total = 0
    for chain in ("sol", "eth", "bsc"):
        try:
            existing = {w["address"] for w in store.list_whales()}
            new_whales = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda c=chain: find_dormant_whale_wallets(
                    chain=c,
                    dormant_days_min=DORMANT_DAYS_MIN,
                    helius_api_key=HELIUS_API_KEY,
                    moralis_api_key=MORALIS_API_KEY,
                    existing_addresses=existing,
                )
            )
            for w in new_whales:
                store.add_whale(w["address"], w["chain"])
                if w.get("last_active_ts"):
                    store.update_last_active(w["address"], w["last_active_ts"])
            total += len(new_whales)
            await update.message.reply_text(
                f"{'✅' if new_whales else '😴'} <b>{chain.upper()}</b>: "
                f"{len(new_whales)} dormant whale(s) added",
                parse_mode="HTML",
            )
        except Exception:
            log.exception("Auto-populate failed for %s", chain)
            await update.message.reply_text(f"❌ <b>{chain.upper()}</b> failed — check logs", parse_mode="HTML")

    await update.message.reply_text(
        f"🏁 Done. <b>{total} total dormant whale(s)</b> added across all chains.\\n"
        f"Use /listwhales to review.",
        parse_mode="HTML",
    )


async def cmd_clearwhales(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/clearwhales — removes all tracked wallets after confirmation."""
    args = ctx.args or []
    if not args or args[0].lower() != "confirm":
        count = len(store.list_whales())
        await update.message.reply_text(
            f"⚠️ This will remove all <b>{count}</b> tracked wallets.\\n\\n"
            f"To confirm: <code>/clearwhales confirm</code>",
            parse_mode="HTML",
        )
        return

    count = len(store.list_whales())
    store.clear_all()
    log.info("All %d whales cleared by user_id=%s", count, update.effective_user.id)
    await update.message.reply_text(
        f"🗑 Cleared {count} wallet(s). Whale list is now empty.\\n"
        f"Use /autopopulate to rebuild it.",
    )


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


def _daily_autopopulate() -> None:
    """
    Runs once daily (at AUTO_POPULATE_HOUR UTC) via APScheduler.
    Scans all three chains for new dormant whales and adds them silently.
    Results are dispatched as a Telegram summary alert.
    """
    log.info("Daily auto-populate starting…")
    total_added = 0
    summary_lines = ["🌅 <b>Daily Whale Scan Complete</b>\n"]

    for chain in ("sol", "eth", "bsc"):
        try:
            existing = {w["address"] for w in store.list_whales()}
            new_whales = find_dormant_whale_wallets(
                chain=chain,
                dormant_days_min=DORMANT_DAYS_MIN,
                helius_api_key=HELIUS_API_KEY,
                moralis_api_key=MORALIS_API_KEY,
                existing_addresses=existing,
            )
            for w in new_whales:
                store.add_whale(w["address"], w["chain"])
                if w.get("last_active_ts"):
                    store.update_last_active(w["address"], w["last_active_ts"])
            total_added += len(new_whales)
            emoji = "✅" if new_whales else "😴"
            summary_lines.append(f"{emoji} {chain.upper()}: {len(new_whales)} new dormant whale(s)")
            log.info("Daily auto-populate %s: %d added", chain, len(new_whales))
        except Exception:
            log.exception("Daily auto-populate failed for %s", chain)
            summary_lines.append(f"❌ {chain.upper()}: scan failed")

    summary_lines.append(f"\n📋 Total tracked: {len(store.list_whales())} wallets")
    if total_added > 0:
        dispatch_alert("\n".join(summary_lines))
    else:
        log.info("Daily auto-populate: no new whales found across all chains")


def main() -> None:
    global _app

    log.info("Building application in polling mode…")

    _app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(_post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_read_timeout(45)
        .build()
    )

    # Log every update first (set LOG_UPDATES=false to disable)
    _app.add_handler(MessageHandler(filters.ALL, log_update), group=-1)

    # Commands
    _app.add_handler(CommandHandler("start",       cmd_start))
    _app.add_handler(CommandHandler("help",        cmd_help))
    _app.add_handler(CommandHandler("addwhale",    cmd_addwhale))
    _app.add_handler(CommandHandler("listwhales",  cmd_listwhales))
    _app.add_handler(CommandHandler("removewhale", cmd_removewhale))
    _app.add_handler(CommandHandler("status",        cmd_status))
    _app.add_handler(CommandHandler("autopopulate",  cmd_autopopulate))
    _app.add_handler(CommandHandler("autopopulateall", cmd_autopopulate_all))
    _app.add_handler(CommandHandler("clearwhales",   cmd_clearwhales))

    # Fallback echo — remove once commands confirmed working
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_echo))

    _app.add_error_handler(_error_handler)

    # EVM polling
    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={"misfire_grace_time": 60, "coalesce": True},
    )
    scheduler.add_job(_poll_evm_whales, "interval", minutes=MORALIS_POLL_MINS, id="evm_poll")
    # Daily auto-populate: scan trending memecoins and add any new dormant whales found
    scheduler.add_job(
        _daily_autopopulate,
        "cron",
        hour=AUTO_POPULATE_HOUR,
        minute=0,
        id="daily_autopopulate",
    )
    scheduler.start()
    log.info("APScheduler started — EVM poll every %d min", MORALIS_POLL_MINS)

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
