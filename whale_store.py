"""
whale_store.py
--------------
Simple JSON-backed storage for whale wallet addresses.
Thread-safe via a reentrant lock.

Schema of whales.json:
{
  "whales": [
    {
      "address":         "0xAbc...",
      "chain":           "eth",          // sol | eth | bsc
      "added_at":        "2025-01-01T00:00:00Z",
      "last_active_ts":  1700000000,     // unix timestamp of last known tx
      "last_active_date": "2024-11-15",  // human-readable
      "dormant_days":    45              // computed on read
    },
    ...
  ]
}
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class WhaleStore:
    def __init__(self, path: str = "whales.json"):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._ensure_file()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_file(self):
        if not self.path.exists():
            self.path.write_text(json.dumps({"whales": []}, indent=2))

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {"whales": []}

    def _write(self, data: dict):
        self.path.write_text(json.dumps(data, indent=2))

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _compute_dormant_days(self, whale: dict) -> int:
        ts = whale.get("last_active_ts")
        if not ts:
            return -1  # unknown
        now = datetime.now(timezone.utc).timestamp()
        return int((now - ts) / 86400)

    # ── Public API ────────────────────────────────────────────────────────────

    def add_whale(self, address: str, chain: str):
        """Add a whale if not already present."""
        with self._lock:
            data = self._read()
            addresses = [w["address"].lower() for w in data["whales"]]
            if address.lower() in addresses:
                log.info("Whale already tracked: %s", address)
                return
            data["whales"].append({
                "address": address,
                "chain": chain,
                "added_at": self._now_iso(),
                "last_active_ts": None,
                "last_active_date": None,
                "dormant_days": -1,
            })
            self._write(data)
            log.info("Whale added: %s (%s)", address, chain)

    def remove_whale(self, address: str) -> bool:
        """Remove a whale by address. Returns True if found and removed."""
        with self._lock:
            data = self._read()
            original = len(data["whales"])
            data["whales"] = [
                w for w in data["whales"]
                if w["address"].lower() != address.lower()
            ]
            if len(data["whales"]) < original:
                self._write(data)
                return True
            return False

    def list_whales(self) -> list[dict]:
        """Return all whales with up-to-date dormant_days computed."""
        with self._lock:
            data = self._read()
            for w in data["whales"]:
                w["dormant_days"] = self._compute_dormant_days(w)
            return data["whales"]

    def get_whale(self, address: str) -> dict | None:
        """Find a single whale by address (case-insensitive)."""
        for w in self.list_whales():
            if w["address"].lower() == address.lower():
                return w
        return None

    def update_last_active(self, address: str, timestamp: int):
        """
        Update the last-known-active timestamp for a whale.
        Called whenever we confirm a tx from this address.
        """
        with self._lock:
            data = self._read()
            for w in data["whales"]:
                if w["address"].lower() == address.lower():
                    w["last_active_ts"] = timestamp
                    w["last_active_date"] = datetime.fromtimestamp(
                        timestamp, tz=timezone.utc
                    ).strftime("%Y-%m-%d")
                    w["dormant_days"] = self._compute_dormant_days(w)
                    break
            self._write(data)

    def clear_all(self):
        """Remove all tracked whales from the store."""
        with self._lock:
            self._write({"whales": []})
            log.info("Whale store cleared")
