"""
alert_builder.py
----------------
Builds formatted Telegram alert strings and parses raw API events.

Alert format example:
━━━━━━━━━━━━━━━━━━━━
🟣 SOLANA WHALE WOKE UP
━━━━━━━━━━━━━━━━━━━━
💤 Dormant for: 47 days
🐳 Wallet: 7xKp…3fQz  (Solscan)
🪙 Token: BONK
📦 Amount: 14,200,000 BONK
💵 ~USD: $18,400
🔗 TX: abc123…
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Explorer URL templates
EXPLORERS = {
    "sol": "https://solscan.io/account/{}",
    "eth": "https://etherscan.io/address/{}",
    "bsc": "https://bscscan.com/address/{}",
    "sol_tx": "https://solscan.io/tx/{}",
    "eth_tx": "https://etherscan.io/tx/{}",
    "bsc_tx": "https://bscscan.com/tx/{}",
}

CHAIN_EMOJI = {
    "sol": "🟣",
    "eth": "🔷",
    "bsc": "🟡",
}


def shorten(addr: str, front: int = 6, back: int = 4) -> str:
    """Shorten a wallet/tx address for display."""
    if not addr or len(addr) <= front + back + 1:
        return addr
    return f"{addr[:front]}…{addr[-back:]}"


def build_alert(
    chain: str,
    whale_address: str,
    token_symbol: str,
    token_name: str,
    amount: float,
    usd_value: float,
    dormant_days: int,
    tx_hash: str,
    action: str = "moved",  # "moved" | "sold" | "transferred"
) -> str:
    """
    Assemble the Telegram HTML alert message.
    All parameters should be pre-validated before calling this.
    """
    chain = chain.lower()
    emoji = CHAIN_EMOJI.get(chain, "⛓")
    chain_label = chain.upper()

    # Build explorer links
    wallet_url = EXPLORERS.get(chain, "{}").format(whale_address)
    tx_url = EXPLORERS.get(f"{chain}_tx", "{}").format(tx_hash)

    wallet_display = shorten(whale_address)
    tx_display = shorten(tx_hash)

    # Format numbers nicely
    amount_str = f"{amount:,.0f}" if amount >= 1 else f"{amount:.4f}"
    usd_str = f"${usd_value:,.0f}"

    # Token display
    if token_symbol and token_name and token_symbol != token_name:
        token_display = f"{token_name} (<b>{token_symbol}</b>)"
    elif token_symbol:
        token_display = f"<b>{token_symbol}</b>"
    else:
        token_display = "<i>Unknown token</i>"

    # Dormancy urgency indicator
    if dormant_days >= 180:
        dormancy_emoji = "🚨"
    elif dormant_days >= 90:
        dormancy_emoji = "⚠️"
    else:
        dormancy_emoji = "💤"

    # Action emoji
    action_emoji = "🔴" if action in ("sold", "sold/transferred") else "🟢"

    return (
        f"🚨 <b>DORMANT WHALE ALERT</b> 🚨\n"
        f"{'─' * 22}\n"
        f"{emoji} <b>Chain:</b> {chain_label}\n"
        f"{dormancy_emoji} <b>Dormant for:</b> {dormant_days} days\n"
        f"🐳 <b>Wallet:</b> <a href='{wallet_url}'>{wallet_display}</a>\n"
        f"🪙 <b>Token:</b> {token_display}\n"
        f"{action_emoji} <b>Action:</b> {action.upper()}\n"
        f"📦 <b>Amount:</b> {amount_str} {token_symbol or ''}\n"
        f"💰 <b>Value:</b> {usd_str}\n"
        f"🔗 <b>TX:</b> <a href='{tx_url}'>{tx_display}</a>\n"
        f"{'─' * 22}\n"
        f"👁 <i>This wallet was silent for {dormant_days}d before this move.</i>"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Helius event parser
# ══════════════════════════════════════════════════════════════════════════════

def parse_helius_event(event: dict, store, min_usd: int, dormant_days_min: int) -> str | None:
    """
    Parse a Helius enhanced transaction event.
    Returns a formatted alert string, or None if thresholds not met.

    Helius enhanced transaction structure (simplified):
    {
      "signature": "tx_hash",
      "timestamp": 1700000000,
      "feePayer": "wallet_address",
      "tokenTransfers": [
        {
          "fromUserAccount": "...",
          "toUserAccount":   "...",
          "mint":            "token_mint_address",
          "tokenAmount":     1000000.0,
          "tokenStandard":   "Fungible",
        }
      ],
      "events": {
        "swap": {
          "tokenOutputs": [{ "mint": "...", "tokenAmount": 100 }],
          "nativeOutput": { "amount": 500000000 }
        }
      }
    }
    """
    try:
        sig = event.get("signature", "")
        ts = event.get("timestamp", 0)
        fee_payer = event.get("feePayer", "")

        # Find which tracked whale triggered this
        whale = store.get_whale(fee_payer)
        if not whale:
            # Check all token transfer participants
            for transfer in event.get("tokenTransfers", []):
                whale = store.get_whale(transfer.get("fromUserAccount", ""))
                if whale:
                    fee_payer = whale["address"]
                    break

        if not whale:
            return None  # Not one of our tracked whales

        # Check dormancy — only alert if dormant long enough
        old_ts = whale.get("last_active_ts")
        if old_ts:
            days_dormant = int((ts - old_ts) / 86400)
        else:
            days_dormant = dormant_days_min  # assume dormant if no history

        # Update last-active before threshold check (so we don't miss the window)
        store.update_last_active(fee_payer, ts)

        if days_dormant < dormant_days_min:
            log.debug("Whale %s active only %d days ago, skipping", fee_payer, days_dormant)
            return None

        # Extract token transfer info
        token_transfers = event.get("tokenTransfers", [])
        if not token_transfers:
            return None

        # Pick the largest transfer by amount
        best = max(token_transfers, key=lambda t: t.get("tokenAmount", 0))
        token_amount = best.get("tokenAmount", 0)
        mint = best.get("mint", "")
        token_symbol = best.get("symbol") or best.get("tokenSymbol") or ""
        token_name   = best.get("tokenName") or token_symbol

        # Get USD value — Helius sometimes includes it
        usd_value = best.get("tokenAmountUsd") or _estimate_usd_from_event(event, token_amount)

        if usd_value < min_usd:
            log.debug("USD value $%.0f below threshold $%d, skipping", usd_value, min_usd)
            return None

        # Determine action direction
        from_addr = best.get("fromUserAccount", "")
        action = "sold" if from_addr.lower() == fee_payer.lower() else "received"

        return build_alert(
            chain="sol",
            whale_address=fee_payer,
            token_symbol=token_symbol,
            token_name=token_name,
            amount=token_amount,
            usd_value=usd_value,
            dormant_days=days_dormant,
            tx_hash=sig,
            action=action,
        )

    except Exception:
        log.exception("Error parsing Helius event: %s", event.get("signature", "?"))
        return None


def _estimate_usd_from_event(event: dict, token_amount: float) -> float:
    """
    Rough fallback: if Helius doesn't provide USD value directly,
    try to infer from native SOL output in a swap event.
    SOL price is approximated at $150 (update or pull from an oracle in production).
    """
    SOL_PRICE_APPROX = 150.0
    LAMPORTS_PER_SOL = 1_000_000_000

    try:
        swap = event.get("events", {}).get("swap", {})
        native_out = swap.get("nativeOutput", {})
        lamports = native_out.get("amount", 0)
        if lamports:
            return (lamports / LAMPORTS_PER_SOL) * SOL_PRICE_APPROX
    except Exception:
        pass
    return 0.0
