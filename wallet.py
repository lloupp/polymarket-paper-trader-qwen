"""
Standalone JSON-based simulated trading wallet for Polymarket.

Stores all state in a single JSON file (wallet.json). No SQLite, no external DB.
Implements position management, P&L tracking, cooldown enforcement,
exposure limits, and risk-based stop-loss / take-profit exits.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Default settings
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: Dict[str, Any] = {
    "stop_loss": 0.20,
    "take_profit": 0.25,
    "max_trade": 50,
    "max_exposure": 500,
    "min_trade": 10,
    "max_per_scan": 10,
    "min_edge": 0.05,
}

DEFAULT_STATE: Dict[str, Any] = {
    "bankroll": 9936.0,
    "initial_bankroll": 10000.0,
    "positions": {},       # position_id -> position dict
    "history": [],         # list of closed position dicts
    "cooldowns": {},       # market_slug -> ISO timestamp string
    "settings": DEFAULT_SETTINGS,
}

class Wallet:
    """JSON-file-backed simulated trading wallet."""

    def __init__(self, path: Optional[str] = None) -> None:
        if path is None:
            path = str(Path(__file__).resolve().parent / "wallet.json")
        self.path = path
        self.state: Dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def load(self) -> None:
        """Load wallet state from the JSON file. Create defaults if missing."""
        if os.path.exists(self.path):
            with open(self.path, "r") as fh:
                self.state = json.load(fh)
        else:
            self.state = self._deep_copy(DEFAULT_STATE)
            self.save()

    def save(self) -> None:
        """Persist current wallet state to the JSON file (atomic-ish write)."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # Status / info
    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        """Return a summary dict of wallet state."""
        positions = self.state.get("positions", {})
        total_exposure = self.get_total_exposure()
        available = self.get_available_bankroll()
        return {
            "bankroll": self.state["bankroll"],
            "initial_bankroll": self.state["initial_bankroll"],
            "open_positions": len(positions),
            "total_exposure": total_exposure,
            "available_bankroll": available,
            "history_count": len(self.state.get("history", [])),
            "cooldown_count": len(self.state.get("cooldowns", {})),
            "settings": self.state.get("settings", {}),
        }

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------
    def open_position(
        self,
        market_slug: str,
        side: str,
        price: float,
        size: float,
        edge: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Open a new position after validation checks.

        Checks:
        1. Dedup – no duplicate market+side position already open.
        2. Cooldown – market must not be on cooldown.
        3. Size bounds – min_trade <= size <= max_trade.
        4. Exposure limit – new exposure must not exceed max_exposure.
        5. Bankroll – must have sufficient available bankroll.

        Returns the new position d
ict on success, raises ValueError on failure.
        """
        settings = self.state.get("settings", DEFAULT_SETTINGS)

        # 1. Dedup check
        for pos in self.state["positions"].values():
            if pos["market_slug"] == market_slug and pos["side"] == side and pos.get("status") == "open":
                raise ValueError(f"Duplicate position: already have open {side} on {market_slug}")

        # 2. Cooldown check
        if self.is_on_cooldown(market_slug):
            raise ValueError(f"Market {market_slug} is on cooldown")

        # 3. Size bounds
        min_trade = settings.get("min_trade", DEFAULT_SETTINGS["min_trade"])
        max_trade = settings.get("max_trade", DEFAULT_SETTINGS["max_trade"])
        if size < min_trade:
            raise ValueError(f"Trade size {size} below minimum {min_trade}")
        if size > max_trade:
            raise ValueError(f"Trade size {size} above maximum {max_trade}")

        # 4. Exposure limit
        max_exposure = settings.get("max_exposure", DEFAULT_SETTINGS["max_exposure"])
        current_exposure = self.get_total_exposure()
        if current_exposure + size > max_exposure:
            raise ValueError(
                f"Trade would push exposure to {current_exposure + size}, "
                f"exceeding max_exposure {max_exposure}"
            )

        # 5. Bankroll check
        available = self.get_available_bankroll()
        if size > available:
            raise ValueError(
                f"Trade size {size} exceeds available bankroll {available}"
            )

        # Create position
        now = datetime.now(timezone.utc).isoformat()
        position_id = str(uuid.uuid4())
        position: Dict[str, Any] = {
            "id": position_id,
            "market_slug": market_slug,
            "side": side,
            "entry_price": price,
            "size": size,
            "cost": round(price * size, 6),
            "status": "open",
            "opened_at": now,
            "closed_at": None,
            "close_price": None,
            "pnl": None,
            "pnl_pct": None,
            "edge": edge,
        }
        if extra:
            position.update(extra)

        self.state["positions"][position_id] = position
        self.state["bankroll"] = round(self.state["bankroll"] - position["cost"], 6)
        self.save()
        return position

    def close_position(
        self,
        position_id: str,
        close_price: float,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Close an open position, calculate P&L, move to history, add cooldown.

        Returns the closed position dict.
        Raises KeyError if position_id not found or not open.
        """
        positions = self.state.get("positions", {})
        if position_id not in positions:
            raise KeyError(f"Position {position_id} not found")

        pos = positions[position_id]
        if pos.get("status") != "open":
            raise KeyError(f"Position {position_id} is not open (status={pos.get('status')})")

        # Calculate P&L
        pnl, pnl_pct = self.calculate_pnl(pos, close_price)

        # Update position
        now = datetime.now(timezone.utc).isoformat()
        pos["status"] = "closed"
        pos["closed_at"] = now
        pos["close_price"] = close_price
        pos["pnl"] = round(pnl, 6)
        pos["pnl_pct"] = round(pnl_pct, 6)
        pos["close_reason"] = reason or "manual"

        # Return proceeds to bankroll
        if pos["side"] == "YES":
            proceeds = pos["size"] * close_price
        else:
            # NO side: profit = cost - (size * close_price)
            proceeds = pos["cost"] + pnl
        self.state["bankroll"] = round(self.state["bankroll"] + proceeds, 6)

        # Move to history
        self.state.setdefault("history", []).append(pos)
        del self.state["positions"][position_id]

        # Add cooldown for this market
        self.add_cooldown(pos["market_slug"])

        self.save()
        return pos

    # ------------------------------------------------------------------
    # Risk management
    # ------------------------------------------------------------------
    def check_risk_exit(
        self,
        position_id: str,
        current_price: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Check if a position should be closed due to stop-loss or take-profit.

        stop_loss  = 20% adverse move  → close with reason "stop_loss"
        take_profit = 25% favorable move → close with reason "take_profit"

        Returns the closed position dict if triggered, None otherwise.
        """
        positions = self.state.get("positions", {})
        if position_id not in positions:
            return None

        pos = positions[position_id]
        if pos.get("status") != "open":
            return None

        settings = self.state.get("settings", DEFAULT_SETTINGS)
        stop_loss = settings.get("stop_loss", DEFAULT_SETTINGS["stop_loss"])
        take_profit = settings.get("take_profit", DEFAULT_SETTINGS["take_profit"])

        _, pnl_pct = self.calculate_pnl(pos, current_price)

        # pnl_pct > 0 is profit, < 0 is loss
        if pnl_pct <= -stop_loss:
            return self.close_position(position_id, current_price, reason="stop_loss")
        if pnl_pct >= take_profit:
            return self.close_position(position_id, current_price, reason="take_profit")

        return None

    # ------------------------------------------------------------------
    # P&L calculation
    # ------------------------------------------------------------------
    @staticmethod
    def calculate_pnl(position: Dict[str, Any], current_price: float) -> tuple:
        """
        Calculate P&L for a position given the current price.

        Returns (pnl_absolute, pnl_pct) tuple.

        YES side: value = size * current_price, cost = entry_price * size
        NO  side: value = size * (1 - current_price), cost = 
entry_price * size
                  but since entry_price for NO = 1 - yes_price at entry,
                  profit = cost - size * (1 - current_price) when price moves down.
        
        Simplified approach:
        - YES: pnl = size * (current_price - entry_price)
        - NO:  pnl = size * (entry_price - current_price)
        - pnl_pct = pnl / cost
        """
        size = position["size"]
        entry_price = position["entry_price"]
        cost = position["cost"]

        if position["side"] == "YES":
            pnl = size * (current_price - entry_price)
        else:  # NO
            pnl = size * (entry_price - current_price)

        pnl_pct = round(pnl / cost, 8) if cost != 0 else 0.0
        return pnl, pnl_pct

    # ------------------------------------------------------------------
    # Cooldown management (24-hour)
    # ------------------------------------------------------------------
    def is_on_cooldown(self, market_slug: str) -> bool:
        """Return True if the market is still within its 24-hour cooldown window."""
        cooldowns = self.state.get("cooldowns", {})
        if market_slug not in cooldowns:
            return False

        cooldown_start = datetime.fromisoformat(cooldowns[market_slug])
        if cooldown_start.tzinfo is None:
            cooldown_start = cooldown_start.replace(tzinfo=timezone.utc)

        expiry = cooldown_start + timedelta(hours=24)
        now = datetime.now(timezone.utc)

        if now >= expiry:
            # Cooldown expired — clean up
            del self.state["cooldowns"][market_slug]
            self.save()
            return False

        return True

    def add_cooldown(self, market_slug: str) -> None:
        """Place a 24-hour cooldown on a market (starts now, UTC)."""
        self.state.setdefault("cooldowns", {})[market_slug] = datetime.now(timezone.utc).isoformat()
        self.save()

    def get_cooldown_remaining(self, market_slug: str) -> Optional[timedelta]:
        """Ret
urn remaining timedelta for a market's cooldown, or None if not on cooldown."""
        cooldowns = self.state.get("cooldowns", {})
        if market_slug not in cooldowns:
            return None

        cooldown_start = datetime.fromisoformat(cooldowns[market_slug])
        if cooldown_start.tzinfo is None:
            cooldown_start = cooldown_start.replace(tzinfo=timezone.utc)

        expiry = cooldown_start + timedelta(hours=24)
        remaining = expiry - datetime.now(timezone.utc)
        if remaining.total_seconds() <= 0:
            return None
        return remaining

    # ------------------------------------------------------------------
    # Exposure / bankroll helpers
    # ------------------------------------------------------------------
    def get_total_exposure(self) -> float:
        """Total capital currently tied up in open positions (sum of costs)."""
        return sum(
            pos.get("cost", 0.0)
            for pos in self.state.get("positions", {}).values()
            if pos.get("status") == "open"
        )

    def get_available_bankroll(self) -> float:
        """Bankroll minus total exposure = capital available for new trades."""
        return round(self.state["bankroll"] - self.get_total_exposure(), 6)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------
    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Return a list of all currently open positions."""
        return [
            pos for pos in self.state.get("positions", {}).values()
            if pos.get("status") == "open"
        ]

    def get_position(self, position_id: str) -> Optional[Dict[str, Any]]:
        """Return a single position by ID (from open positions)."""
        return self.state.get("positions", {}).get(position_id)

    def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return recent closed-po
sition history (most recent first)."""
        hist = self.state.get("history", [])
        return hist[-limit:][::-1]

    def get_closed_pnl(self) -> float:
        """Total realized P&L across all closed positions."""
        return sum(
            pos.get("pnl", 0.0)
            for pos in self.state.get("history", [])
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _deep_copy(d: Dict[str, Any]) -> Dict[str, Any]:
        """Cheap deep copy for default state (JSON-serializable only)."""
        return json.loads(json.dumps(d))

    def __repr__(self) -> str:
        status = self.get_status()
        return (
            f"Wallet(bankroll={status['bankroll']}, "
            f"open={status['open_positions']}, "
            f"exposure={status['total_exposure']})"
        )

