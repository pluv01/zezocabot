"""
helius_webhook.py
-----------------
Helpers for:
  1. Registering Solana wallet addresses with Helius Enhanced Webhooks.
  2. Verifying the HMAC signature Helius includes in webhook requests.

Helius docs: https://docs.helius.dev/webhooks-and-websockets/webhooks
"""

import hashlib
import hmac
import logging
import os

import requests

log = logging.getLogger(__name__)

HELIUS_WEBHOOK_API = "https://api.helius.xyz/v0/webhooks"


def register_address_with_helius(address: str, api_key: str) -> bool:
    """
    Register (or update) a Solana address on the existing Helius webhook
    so we receive push notifications for its transactions.

    Strategy:
      1. GET all existing webhooks.
      2. Find the one whose webhookURL matches our server (or the first one).
      3. PATCH it to add the new address.
      4. If no webhook exists yet, CREATE one.

    The webhook URL is read from the HELIUS_WEBHOOK_URL env var.
    Example: https://your-app.railway.app/helius-webhook
    """
    webhook_url = os.environ.get("HELIUS_WEBHOOK_URL", "")
    if not webhook_url:
        log.warning("HELIUS_WEBHOOK_URL not set — skipping Helius registration")
        return False

    try:
        # 1. Fetch existing webhooks
        r = requests.get(HELIUS_WEBHOOK_API, params={"api-key": api_key}, timeout=10)
        r.raise_for_status()
        webhooks = r.json()

        existing = None
        for wh in webhooks:
            if wh.get("webhookURL") == webhook_url:
                existing = wh
                break

        if existing:
            # 2. PATCH to add address
            wh_id = existing["webhookID"]
            current_addresses = existing.get("accountAddresses", [])
            if address in current_addresses:
                log.info("Address %s already on Helius webhook", address)
                return True

            updated_addresses = current_addresses + [address]
            patch_r = requests.patch(
                f"{HELIUS_WEBHOOK_API}/{wh_id}",
                params={"api-key": api_key},
                json={"accountAddresses": updated_addresses},
                timeout=10,
            )
            patch_r.raise_for_status()
            log.info("Patched Helius webhook to add %s", address)
            return True

        else:
            # 3. CREATE a new webhook
            payload = {
                "webhookURL": webhook_url,
                "transactionTypes": ["ANY"],
                "accountAddresses": [address],
                "webhookType": "enhanced",  # enriched with token metadata
            }
            # Optionally add HMAC auth secret
            secret = os.environ.get("HELIUS_WEBHOOK_SECRET", "")
            if secret:
                payload["authHeader"] = secret

            create_r = requests.post(
                HELIUS_WEBHOOK_API,
                params={"api-key": api_key},
                json=payload,
                timeout=10,
            )
            create_r.raise_for_status()
            log.info("Created new Helius webhook for %s", address)
            return True

    except requests.RequestException as e:
        log.error("Helius webhook registration failed: %s", e)
        return False


def verify_helius_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """
    Verify the HMAC-SHA256 signature that Helius sends in the
    'Helius-Signature' header when an authHeader / secret is configured.

    Returns True if valid (or if no secret is configured — open mode).
    """
    if not secret:
        return True  # No secret configured → accept all

    expected = hmac.new(
        key=secret.encode(),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)
