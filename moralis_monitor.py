"""
moralis_monitor.py
------------------
Polls Moralis Web3 API for ETH and BSC whale wallet activity.
Called on a schedule by APScheduler every few minutes.

Moralis endpoints used:
  - GET /wallets/{address}/history  → recent transactions
  - GET /{address}/erc20/transfers  → ERC-20 token transfers

We track the last-seen transaction hash per wallet to avoid re-alerting
on the same transaction across poll cycles.

Moralis docs: https://docs.moralis.io/web3-data-api/evm
"""

import logging
import time
from datetime import datetime, timezone

import requests

from alert_builder import build_alert

log = logging.getLogger(__name__)

MORALIS_BASE = "https://deep-index.moralis.io/api/v2.2"

# Chain IDs for Moralis API
CHAIN_ID = {
    "eth": "0x1",    # Ethereum mainnet
    "bsc": "0x38",   # BNB Smart Chain mainnet
}

# In-memory set of (address, tx_hash) we've already alerted on this session
_seen_txs: set[str] = set()


def check_evm_whales(
    whales: list[dict],
    api_key: str,
    store,
    min_usd: int,
    dormant_days_min: int,
) -> list[str]:
    """
    Check each ETH/BSC whale for new token transfers.
    Returns a list of alert message strings (may be empty).
    """
    alerts = []
    for whale in whales:
        chain = whale["chain"]
        address = whale["address"]
        try:
            msgs = _check_one_whale(whale, api_key, store, min_usd, dormant_days_min)
            alerts.extend(msgs)
        except Exception:
            log.exception("Error checking EVM whale %s (%s)", address, chain)

    return alerts


def _check_one_whale(
    whale: dict,
    api_key: str,
    store,
    min_usd: int,
    dormant_days_min: int,
) -> list[str]:
    """Fetch recent ERC-20 transfers for one whale and return alert strings."""
    address = whale["address"]
    chain   = whale["chain"]
    chain_id = CHAIN_ID.get(chain, "0x1")

    headers = {"X-API-Key": api_key}
    params  = {
        "chain": chain_id,
        "limit": 10,       # only check the 10 most recent transfers
        "order": "DESC",
    }

    url = f"{MORALIS_BASE}/{address}/erc20/transfers"
    r = requests.get(url, headers=headers, params=params, timeout=15)

    if r.status_code == 429:
        log.warning("Moralis rate limit hit for %s — backing off", address)
        time.sleep(5)
        return []

    r.raise_for_status()
    data = r.json()
    transfers = data.get("result", [])

    if not transfers:
        return []

    alerts = []
    for tx in transfers:
        tx_hash = tx.get("transaction_hash", "")

        # Skip if we've already processed this tx
        cache_key = f"{address}:{tx_hash}"
        if cache_key in _seen_txs:
            continue
        _seen_txs.add(cache_key)

        alert = _evaluate_transfer(tx, whale, chain, store, min_usd, dormant_days_min)
        if alert:
            alerts.append(alert)

    return alerts


def _evaluate_transfer(
    tx: dict,
    whale: dict,
    chain: str,
    store,
    min_usd: int,
    dormant_days_min: int,
) -> str | None:
    """
    Evaluate one ERC-20 transfer event and return an alert string if thresholds met.

    Moralis ERC-20 transfer fields (relevant):
    {
      "transaction_hash": "0x...",
      "block_timestamp":  "2024-11-15T12:00:00.000Z",
      "from_address":     "0x...",
      "to_address":       "0x...",
      "token_name":       "Bonk",
      "token_symbol":     "BONK",
      "value_decimal":    "14200000.0",
      "usd_price":        "0.0000012",     # price per token (may be absent)
      "total_usd_value":  "17.04",         # Moralis sometimes provides this
    }
    """
    try:
        tx_hash   = tx.get("transaction_hash", "")
        from_addr = tx.get("from_address", "").lower()
        whale_addr = whale["address"].lower()

        # Only alert on outbound transfers (whale selling/moving out)
        if from_addr != whale_addr:
            return None

        # Parse timestamp
        ts_str = tx.get("block_timestamp", "")
        tx_ts = _parse_iso_ts(ts_str)

        # Compute dormancy
        old_ts = whale.get("last_active_ts")
        if old_ts and tx_ts > old_ts:
            days_dormant = int((tx_ts - old_ts) / 86400)
        else:
            days_dormant = dormant_days_min  # treat unknown as dormant

        # Update store with new activity timestamp
        store.update_last_active(whale["address"], tx_ts)

        if days_dormant < dormant_days_min:
            log.debug("EVM whale %s was active %dd ago, skipping", whale["address"], days_dormant)
            return None

        # Parse amounts
        token_symbol = tx.get("token_symbol", "")
        token_name   = tx.get("token_name", "") or token_symbol
        value_str    = tx.get("value_decimal") or tx.get("value", "0")
        token_amount = float(value_str) if value_str else 0.0

        # USD value: Moralis may provide it directly or via per-token price
        usd_value = float(tx.get("total_usd_value") or 0)
        if usd_value == 0:
            usd_price = float(tx.get("usd_price") or 0)
            usd_value = token_amount * usd_price

        if usd_value < min_usd:
            log.debug("EVM transfer $%.0f below min $%d, skipping", usd_value, min_usd)
            return None

        return build_alert(
            chain=chain,
            whale_address=whale["address"],
            token_symbol=token_symbol,
            token_name=token_name,
            amount=token_amount,
            usd_value=usd_value,
            dormant_days=days_dormant,
            tx_hash=tx_hash,
            action="sold/transferred",
        )

    except Exception:
        log.exception("Error evaluating EVM transfer")
        return None


def _parse_iso_ts(ts_str: str) -> int:
    """Parse ISO 8601 timestamp string to unix int. Returns 0 on failure."""
    if not ts_str:
        return 0
    try:
        # Handle fractional seconds
        ts_str = ts_str.rstrip("Z").split(".")[0]
        dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0
