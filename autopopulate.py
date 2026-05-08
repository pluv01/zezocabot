"""
autopopulate.py
---------------
Finds DORMANT memecoin whale wallets — wallets that:
  1. Held a large position in a popular memecoin (> $20k or > 0.5% supply)
  2. Have NOT made any transaction in the past DORMANT_DAYS_MIN days

Why this matters: current top holders are likely active traders.
We want wallets that accumulated a large bag and then went silent —
those are the ones worth watching for a "wake-up" alert.

Strategy per chain:
  Solana  → DexScreener trending tokens → Helius token largest accounts
            → filter each holder's last tx date via getSignaturesForAddress
  ETH/BSC → DexScreener trending tokens → Moralis ERC-20 top holders
            → filter by last ERC-20 transfer date via Moralis wallet history

APIs used (all free tier, no paid plan needed):
  DexScreener  https://docs.dexscreener.com/api/reference   (no key)
  Helius RPC   https://docs.helius.dev/                      (free key)
  Moralis      https://docs.moralis.io/web3-data-api/evm     (free key)
"""

import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("whale_bot.autopopulate")

# ── API endpoints ─────────────────────────────────────────────────────────────
DEXSCREENER_PROFILES  = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_SEARCH    = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKEN     = "https://api.dexscreener.com/latest/dex/tokens/{}"
HELIUS_RPC            = "https://mainnet.helius-rpc.com/?api-key={}"
MORALIS_BASE          = "https://deep-index.moralis.io/api/v2.2"

# ── Chain maps ────────────────────────────────────────────────────────────────
DEXSCREENER_CHAIN = {"sol": "solana", "eth": "ethereum", "bsc": "bsc"}
MORALIS_CHAIN_ID  = {"eth": "0x1",    "bsc": "0x38"}

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_HOLDER_USD     = 20_000   # wallet must hold > $20k of the token
MIN_HOLDER_PCT     = 0.5      # OR > 0.5% of total supply
MAX_TOKENS_SCAN    = 8        # trending tokens to scan per run
MAX_HOLDERS_CHECK  = 20       # top N holders to inspect per token
MAX_NEW_WHALES     = 50       # hard cap on wallets added per autopopulate call

# Well-known non-whale addresses to always exclude
# (bridges, DEX programs, burn addresses, token vaults)
EXCLUDE_ADDRESSES = {
    # Solana system / token program accounts
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "So11111111111111111111111111111111111111112",
    # EVM zero / burn
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    # Common Solana DEX/AMM vaults (Raydium, Orca, etc.)
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium authority
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",  # Orca
}


# ══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════════

def find_dormant_whale_wallets(
    chain: str,
    dormant_days_min: int,
    helius_api_key: str = "",
    moralis_api_key: str = "",
    existing_addresses: set | None = None,
) -> list[dict]:
    """
    Main entry point. Returns a list of newly discovered dormant whale dicts:
      [{"address": "...", "chain": "sol", "last_active_ts": 1700000000,
        "last_active_date": "2024-10-01", "dormant_days": 52}, ...]

    Args:
        chain              : "sol", "eth", or "bsc"
        dormant_days_min   : minimum days of inactivity to qualify
        helius_api_key     : required for Solana
        moralis_api_key    : required for ETH/BSC
        existing_addresses : already-tracked addresses to skip (dedup)
    """
    existing = {a.lower() for a in (existing_addresses or set())}
    log.info(
        "Auto-populate: chain=%s dormant_min=%dd existing=%d",
        chain, dormant_days_min, len(existing),
    )

    tokens = _get_trending_tokens(chain)
    if not tokens:
        log.warning("No trending tokens found for %s", chain)
        return []
    log.info("Trending tokens found: %d — scanning up to %d", len(tokens), MAX_TOKENS_SCAN)

    new_whales: list[dict] = []
    seen_this_run: set[str] = set()

    for token in tokens[:MAX_TOKENS_SCAN]:
        if len(new_whales) >= MAX_NEW_WHALES:
            break

        mint      = token["address"]
        symbol    = token.get("symbol", "?")
        price_usd = token.get("price_usd", 0.0)
        supply    = token.get("total_supply", 0.0)

        log.info(
            "Scanning %s (%s…) price=$%.6f",
            symbol, mint[:10], price_usd,
        )

        # Step A: get large holders of this token
        try:
            holders = _get_large_holders(
                chain, mint, price_usd, supply,
                helius_api_key, moralis_api_key,
            )
        except Exception:
            log.exception("Failed fetching holders for %s %s", symbol, mint[:10])
            time.sleep(1)
            continue

        log.info("  %s: %d large holders found, checking dormancy…", symbol, len(holders))

        # Step B: for each large holder, check if they're dormant
        for holder in holders[:MAX_HOLDERS_CHECK]:
            if len(new_whales) >= MAX_NEW_WHALES:
                break

            addr = holder["address"]
            addr_lower = addr.lower()

            # Skip duplicates and known exclusions
            if addr_lower in existing or addr_lower in seen_this_run:
                continue
            if addr_lower in {e.lower() for e in EXCLUDE_ADDRESSES}:
                log.debug("  Skipping excluded address: %s…", addr[:10])
                continue

            # Step C: fetch last activity date for this wallet
            try:
                last_ts = _get_last_active_ts(chain, addr, helius_api_key, moralis_api_key)
            except Exception:
                log.debug("  Could not get last_active_ts for %s…: %s", addr[:10], "")
                time.sleep(0.3)
                continue

            now_ts     = int(datetime.now(timezone.utc).timestamp())
            dormant_days = int((now_ts - last_ts) / 86400) if last_ts else dormant_days_min

            if dormant_days < dormant_days_min:
                log.debug(
                    "  %s…%s active %dd ago — too recent, skipping",
                    addr[:6], addr[-4:], dormant_days,
                )
                time.sleep(0.2)
                continue

            # Qualifies — dormant whale found
            last_date = (
                datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                if last_ts else "unknown"
            )
            seen_this_run.add(addr_lower)
            new_whales.append({
                "address":          addr,
                "chain":            chain,
                "last_active_ts":   last_ts or None,
                "last_active_date": last_date,
                "dormant_days":     dormant_days,
            })
            log.info(
                "  ✓ DORMANT WHALE: %s…%s | $%,.0f held | %s (%dd dormant)",
                addr[:8], addr[-4:],
                holder.get("usd_value", 0),
                symbol, dormant_days,
            )
            time.sleep(0.3)   # be gentle on RPC rate limits

        time.sleep(1.0)   # pause between tokens

    log.info(
        "Auto-populate complete: %d dormant whale(s) found for %s",
        len(new_whales), chain,
    )
    return new_whales


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — Trending memecoin tokens from DexScreener
# ══════════════════════════════════════════════════════════════════════════════

def _get_trending_tokens(chain: str) -> list[dict]:
    """
    Fetch recently trending tokens for the chain.
    Falls back to a 'meme' keyword search if the profiles endpoint returns nothing.
    """
    ds_chain = DEXSCREENER_CHAIN.get(chain, chain)
    tokens: list[dict] = []

    # Primary: token profiles (trending/recently boosted tokens)
    try:
        r = requests.get(DEXSCREENER_PROFILES, timeout=15)
        r.raise_for_status()
        for item in r.json():
            if item.get("chainId", "").lower() != ds_chain:
                continue
            addr = item.get("tokenAddress", "")
            if not addr:
                continue
            enriched = _enrich_token_dexscreener(addr)
            if enriched and enriched.get("price_usd", 0) > 0:
                tokens.append(enriched)
            if len(tokens) >= MAX_TOKENS_SCAN * 2:
                break
        log.info("DexScreener profiles: %d tokens for %s", len(tokens), chain)
    except requests.RequestException as e:
        log.warning("DexScreener profiles failed: %s", e)

    # Fallback: search for 'meme'
    if not tokens:
        log.info("Falling back to DexScreener meme search for %s", chain)
        tokens = _dexscreener_meme_search(ds_chain)

    # Sort by 24h volume descending — most active memecoins have the most whale interest
    tokens.sort(key=lambda t: t.get("volume_24h", 0), reverse=True)
    return tokens


def _enrich_token_dexscreener(token_address: str) -> dict | None:
    """Fetch price, supply, volume for a token from DexScreener pairs."""
    try:
        r = requests.get(DEXSCREENER_TOKEN.format(token_address), timeout=10)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        if not pairs:
            return None

        # Best pair = highest USD liquidity
        pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        price_usd = float(pair.get("priceUsd") or 0)
        fdv       = float(pair.get("fdv") or 0)
        supply    = (fdv / price_usd) if price_usd > 0 else 0

        return {
            "address":      token_address,
            "symbol":       pair.get("baseToken", {}).get("symbol", "?"),
            "name":         pair.get("baseToken", {}).get("name", ""),
            "price_usd":    price_usd,
            "total_supply": supply,
            "volume_24h":   float((pair.get("volume") or {}).get("h24") or 0),
        }
    except Exception:
        return None


def _dexscreener_meme_search(ds_chain: str) -> list[dict]:
    """Search DexScreener for 'meme' keyword as a fallback."""
    results, seen = [], set()
    try:
        r = requests.get(DEXSCREENER_SEARCH, params={"q": "meme"}, timeout=15)
        r.raise_for_status()
        for pair in r.json().get("pairs") or []:
            if pair.get("chainId", "").lower() != ds_chain:
                continue
            addr = (pair.get("baseToken") or {}).get("address", "")
            if not addr or addr in seen:
                continue
            seen.add(addr)
            price_usd = float(pair.get("priceUsd") or 0)
            fdv       = float(pair.get("fdv") or 0)
            results.append({
                "address":      addr,
                "symbol":       pair.get("baseToken", {}).get("symbol", "?"),
                "name":         pair.get("baseToken", {}).get("name", ""),
                "price_usd":    price_usd,
                "total_supply": (fdv / price_usd) if price_usd > 0 else 0,
                "volume_24h":   float((pair.get("volume") or {}).get("h24") or 0),
            })
            if len(results) >= MAX_TOKENS_SCAN * 2:
                break
    except requests.RequestException as e:
        log.error("DexScreener meme search failed: %s", e)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — Large holder lists
# ══════════════════════════════════════════════════════════════════════════════

def _get_large_holders(
    chain: str, mint: str,
    price_usd: float, total_supply: float,
    helius_api_key: str, moralis_api_key: str,
) -> list[dict]:
    """Route to the correct chain's holder fetcher."""
    if chain == "sol":
        return _sol_large_holders(mint, helius_api_key, price_usd, total_supply)
    return _evm_large_holders(mint, chain, moralis_api_key, price_usd, total_supply)


def _sol_large_holders(
    mint: str, helius_api_key: str,
    price_usd: float, total_supply: float,
) -> list[dict]:
    """
    Use Helius RPC getTokenLargestAccounts to find top Solana token holders.
    Returns token accounts — we then resolve the owner wallet address.
    """
    if not helius_api_key:
        log.warning("HELIUS_API_KEY not set — skipping SOL holder lookup")
        return []

    rpc_url = HELIUS_RPC.format(helius_api_key)

    # Get largest token accounts
    r = requests.post(rpc_url, json={
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenLargestAccounts",
        "params": [mint, {"commitment": "finalized"}],
    }, timeout=15)
    r.raise_for_status()
    accounts = (r.json().get("result") or {}).get("value") or []

    qualified = []
    for acct in accounts:
        amount    = float(acct.get("uiAmount") or 0)
        usd_value = amount * price_usd
        pct       = (amount / total_supply * 100) if total_supply > 0 else 0

        if usd_value < MIN_HOLDER_USD and pct < MIN_HOLDER_PCT:
            continue

        token_account = acct.get("address", "")
        if not token_account:
            continue

        # Resolve token account → owner wallet via getAccountInfo
        owner = _sol_resolve_token_account_owner(token_account, rpc_url)
        if not owner:
            continue

        qualified.append({
            "address":   owner,
            "usd_value": usd_value,
            "pct":       pct,
        })

    return qualified


def _sol_resolve_token_account_owner(token_account: str, rpc_url: str) -> str | None:
    """
    Resolve a Solana token account address to its owner wallet.
    Token accounts are intermediate — the owner is the actual whale wallet.
    """
    try:
        r = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [token_account, {"encoding": "jsonParsed", "commitment": "finalized"}],
        }, timeout=10)
        r.raise_for_status()
        info = r.json().get("result", {}).get("value") or {}
        parsed = info.get("data", {}).get("parsed", {})
        owner = parsed.get("info", {}).get("owner", "")
        return owner or None
    except Exception:
        return None


def _evm_large_holders(
    token_address: str, chain: str, moralis_api_key: str,
    price_usd: float, total_supply: float,
) -> list[dict]:
    """Fetch ERC-20 top holders from Moralis."""
    if not moralis_api_key:
        log.warning("MORALIS_API_KEY not set — skipping EVM holder lookup")
        return []

    chain_id = MORALIS_CHAIN_ID.get(chain, "0x1")
    r = requests.get(
        f"{MORALIS_BASE}/erc20/{token_address}/owners",
        headers={"X-API-Key": moralis_api_key},
        params={"chain": chain_id, "limit": MAX_HOLDERS_CHECK, "order": "DESC"},
        timeout=15,
    )
    if r.status_code == 429:
        log.warning("Moralis rate limit — skipping %s", token_address[:10])
        return []
    r.raise_for_status()

    qualified = []
    for h in r.json().get("result") or []:
        try:
            balance = float(h.get("balance_formatted") or h.get("balance") or 0)
        except ValueError:
            continue

        usd_value = balance * price_usd
        pct       = (balance / total_supply * 100) if total_supply > 0 else 0
        address   = h.get("owner_address", "")

        if not address:
            continue
        if usd_value >= MIN_HOLDER_USD or pct >= MIN_HOLDER_PCT:
            qualified.append({"address": address, "usd_value": usd_value, "pct": pct})

    return qualified


# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — Last activity check (the dormancy filter)
# ══════════════════════════════════════════════════════════════════════════════

def _get_last_active_ts(
    chain: str, address: str,
    helius_api_key: str, moralis_api_key: str,
) -> int:
    """
    Returns the unix timestamp of the wallet's most recent transaction.
    Returns 0 if no history found (treat as very old / dormant).
    """
    if chain == "sol":
        return _sol_last_active_ts(address, helius_api_key)
    return _evm_last_active_ts(address, chain, moralis_api_key)


def _sol_last_active_ts(address: str, helius_api_key: str) -> int:
    """
    Fetch the most recent signature for a Solana wallet.
    getSignaturesForAddress with limit=1 returns only the latest tx.
    """
    if not helius_api_key:
        return 0

    rpc_url = HELIUS_RPC.format(helius_api_key)
    try:
        r = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "getSignaturesForAddress",
            "params":  [address, {"limit": 1, "commitment": "finalized"}],
        }, timeout=10)
        r.raise_for_status()
        sigs = r.json().get("result") or []
        if not sigs:
            return 0   # no history = treat as very dormant
        return int(sigs[0].get("blockTime") or 0)
    except Exception as e:
        log.debug("SOL last_active_ts failed for %s…: %s", address[:8], e)
        return 0


def _evm_last_active_ts(address: str, chain: str, moralis_api_key: str) -> int:
    """
    Fetch the most recent ERC-20 transfer involving this wallet via Moralis.
    We check ERC-20 transfers specifically — native ETH transfers could be
    gas top-ups which don't indicate memecoin whale activity.
    """
    if not moralis_api_key:
        return 0

    chain_id = MORALIS_CHAIN_ID.get(chain, "0x1")
    try:
        r = requests.get(
            f"{MORALIS_BASE}/{address}/erc20/transfers",
            headers={"X-API-Key": moralis_api_key},
            params={"chain": chain_id, "limit": 1, "order": "DESC"},
            timeout=10,
        )
        if r.status_code == 429:
            log.warning("Moralis rate limit on last_active check for %s…", address[:8])
            return 0
        r.raise_for_status()
        txs = r.json().get("result") or []
        if not txs:
            return 0
        ts_str = txs[0].get("block_timestamp", "")
        return _parse_iso_ts(ts_str)
    except Exception as e:
        log.debug("EVM last_active_ts failed for %s…: %s", address[:8], e)
        return 0


def _parse_iso_ts(ts_str: str) -> int:
    """Parse ISO 8601 string → unix int. Returns 0 on failure."""
    if not ts_str:
        return 0
    try:
        ts_str = ts_str.rstrip("Z").split(".")[0]
        dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0
