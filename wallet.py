"""
Standalone JSON-based simulated trading wallet for Polymarket.

Stores all state in a single JSON file (wallet.json). No SQLite, no external DB.
Implements position management, P&L tracking, cooldown enforcement,
exposure limits, and risk-based stop-loss / take-profit exits.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from learning import append_trade_feature, build_trade_feature, ensure_learning_state

# ---------------------------------------------------------------------------
# Default settings
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: Dict[str, Any] = {
    "stop_loss": 0.10,
    "take_profit": 0.25,
    "max_trade": 50,
    "max_exposure": 500,
    "min_trade": 10,
    "max_per_scan": 10,
    "min_edge": 0.05,
    "llm_enabled": False,
    "llm_mode": "fast",
    "llm_url": "http://127.0.0.1:8080/v1/chat/completions",
    "min_net_edge": 0.035,
    "taker_fee_estimate": 0.001,
    "slippage_estimate": 0.01,
    "smart_money_max_spread": 0.03,
    "smart_money_min_liquidity": 15000,
    "smart_money_min_vol24h": 50000,
    "event_countdown_max_spread": 0.06,
    "event_countdown_min_liquidity": 15000,
    "event_countdown_min_vol24h": 25000,
    "btc_max_entry_price": 0.82,
    "btc_min_liquidity": 1000,
    "endgame_max_entry_price": 0.90,
    "endgame_min_liquidity": 1500,
    "shadow_strategies": "arbitrage,value,mean_reversion,volume_spike,weather_forecast",
}

DEFAULT_STATE: Dict[str, Any] = {
    "bankroll": 9936.0,
    "initial_bankroll": 10000.0,
    "positions": {},       # position_id -> position dict
    "history": [],         # list of closed position dicts
    "cooldowns": {},       # market_slug -> ISO timestamp string
    "settings": DEFAULT_SETTINGS,
    "learning_state": {
        "version": 1,
        "enabled": True,
        "shadow_mode": False,
        "min_samples": 20,
        "aggressiveness": "medium",
        "max_features": 1000,
        "window_trades": 400,
        "cooldown_cycles": 10,
        "last_policy_cycle": 0,
        "cycle_counter": 0,
        "trades_features": [],
        "strategy_stats": {},
        "edge_buckets_stats": {},
        "policy_recommendations": {
            "effective_min_edge": 0.05,
            "strategy_multipliers": {},
            "confidence": "low",
            "reasons": ["boot"],
            "updated_at": None,
        },
        "last_updated_at": None,
    },
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
    @contextmanager
    def _file_lock(self, exclusive: bool):
        lock_path = self.path + ".lock"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def load(self) -> None:
        """Load wallet state from the JSON file. Create defaults if missing."""
        if os.path.exists(self.path):
            with self._file_lock(exclusive=False):
                with open(self.path, "r") as fh:
                    self.state = json.load(fh)
        else:
            self.state = self._deep_copy(DEFAULT_STATE)
            self.save()

        # Keep risk settings proportional to bankroll when enabled.
        ensure_learning_state(self.state)
        if self._apply_auto_risk_settings():
            self.save()

    def save(self) -> None:
        """Persist current wallet state to the JSON file (atomic-ish write)."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with self._file_lock(exclusive=True):
            with open(tmp, "w") as fh:
                json.dump(self.state, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)

    def _apply_auto_risk_settings(self) -> bool:
        """Auto-scale risk settings from bankroll. Enable with PAPER_AUTO_RISK=1 (default)."""
        settings = dict(self.state.get("settings", {}))
        auto_flag = settings.get("auto_risk_enabled")
        if auto_flag is None:
            auto_flag = os.getenv("PAPER_AUTO_RISK", "1") == "1"
            settings["auto_risk_enabled"] = bool(auto_flag)
            self.state["settings"] = settings
        if not bool(auto_flag):
            return False

        bankroll = float(self.state.get("bankroll", 0.0) or 0.0)
        if bankroll <= 0:
            return False

        settings = dict(self.state.get("settings", {}))

        max_exposure = round(min(bankroll, max(10.0, bankroll * 0.25)), 2)
        max_trade = round(min(max_exposure, max(2.0, bankroll * 0.02)), 2)
        min_trade = round(min(max_trade, max(1.0, bankroll * 0.005)), 2)
        max_per_scan = int(max(1, min(3, bankroll // 100 or 1)))

        new_values = {
            "max_exposure": max_exposure,
            "max_trade": max_trade,
            "min_trade": min_trade,
            "max_per_scan": max_per_scan,
        }

        changed = False
        for k, v in new_values.items():
            if settings.get(k) != v:
                settings[k] = v
                changed = True

        if changed:
            self.state["settings"] = settings
        return changed

    # ------------------------------------------------------------------
    # Status / info
    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        """Return a summary dict of wallet state."""
        positions = self.state.get("positions", {})
        history = self.state.get("history", [])
        trusted_history = [pos for pos in history if pos.get("trusted_for_pnl", True)]
        quarantined_history = [pos for pos in history if not pos.get("trusted_for_pnl", True)]
        total_exposure = self.get_total_exposure()
        available = self.get_available_bankroll()
        return {
            "bankroll": self.state["bankroll"],
            "initial_bankroll": self.state["initial_bankroll"],
            "open_positions": len(positions),
            "total_exposure": total_exposure,
            "available_bankroll": available,
            "history_count": len(history),
            "trusted_history_count": len(trusted_history),
            "quarantined_history_count": len(quarantined_history),
            "trusted_closed_pnl": round(self.get_closed_pnl(), 6),
            "raw_closed_pnl": round(self.get_raw_closed_pnl(), 6),
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
        3. Stake bounds – min_trade <= size <= max_trade.
        4. Exposure limit – new stake must not exceed max_exposure.
        5. Bankroll – must have sufficient available cash.

        Returns the new position dict on success, raises ValueError on failure.
        """
        if self._apply_auto_risk_settings():
            self.save()
        settings = self.state.get("settings", DEFAULT_SETTINGS)

        # 1. Dedup check
        for pos in self.state["positions"].values():
            if pos["market_slug"] == market_slug and pos["side"] == side and pos.get("status") == "open":
                raise ValueError(f"Duplicate position: already have open {side} on {market_slug}")

        # 2. Cooldown check
        if self.is_on_cooldown(market_slug):
            raise ValueError(f"Market {market_slug} is on cooldown")

        if price <= 0 or price > 1:
            raise ValueError(f"Invalid entry price {price}; expected 0 < price <= 1")

        # 3. Stake bounds. Public callers pass size as dollar stake, not shares.
        min_trade = settings.get("min_trade", DEFAULT_SETTINGS["min_trade"])
        max_trade = settings.get("max_trade", DEFAULT_SETTINGS["max_trade"])
        if size < min_trade:
            raise ValueError(f"Trade size {size} below minimum {min_trade}")
        if size > max_trade:
            raise ValueError(f"Trade size {size} above maximum {max_trade}")

        # 4. Exposure limit
        max_exposure = settings.get("max_exposure", DEFAULT_SETTINGS["max_exposure"])
        current_exposure = self.get_total_exposure()
        cost = round(size, 6)
        shares = round(cost / price, 8)
        if current_exposure + cost > max_exposure:
            raise ValueError(
                f"Trade would push exposure to {current_exposure + cost}, "
                f"exceeding max_exposure {max_exposure}"
            )

        # 5. Bankroll check
        available = self.get_available_bankroll()
        if cost > available:
            raise ValueError(
                f"Trade size {cost} exceeds available bankroll {available}"
            )

        # Create position
        now = datetime.now(timezone.utc).isoformat()
        position_id = str(uuid.uuid4())
        position: Dict[str, Any] = {
            "id": position_id,
            "market_slug": market_slug,
            "side": side,
            "entry_price": price,
            "size": cost,
            "shares": shares,
            "cost": cost,
            "status": "open",
            "opened_at": now,
            "closed_at": None,
            "close_price": None,
            "pnl": None,
            "pnl_pct": None,
            "edge": edge,
            "trusted_for_pnl": True,
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

        # Return side-price proceeds to cash. For legacy positions without
        # shares, size was the share quantity; new positions persist shares.
        shares = float(pos.get("shares", pos.get("size", 0.0)) or 0.0)
        proceeds = shares * close_price
        self.state["bankroll"] = round(self.state["bankroll"] + proceeds, 6)

        # Move to history
        self.state.setdefault("history", []).append(pos)

        if pos.get("trusted_for_pnl", True):
            # Learning must only consume trades priced with trusted execution data.
            ls = ensure_learning_state(self.state)
            feat = build_trade_feature(pos, strategy_mode=str(self.state.get("last_strategy_mode") or "unknown"))
            append_trade_feature(ls, feat)

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

        current_price is always the side price for the position:
        YES positions receive yes_price, NO positions receive no_price.

        New positions store:
        - size: dollar stake/cost
        - shares: outcome shares bought

        Legacy positions did not store shares, so size is used as a fallback
        share quantity.

        Formula for both sides:
        - pnl = shares * (current_side_price - entry_side_price)
        - pnl_pct = pnl / cost
        """
        shares = float(position.get("shares", position.get("size", 0.0)) or 0.0)
        entry_price = float(position["entry_price"])
        cost = float(position["cost"])

        pnl = shares * (current_price - entry_price)
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
        """Cash available for new trades.

        Bankroll is already reduced by position cost when a trade opens, so
        subtracting exposure here would double-count committed capital.
        """
        return round(self.state["bankroll"], 6)

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
        """Total trusted realized P&L across all closed positions."""
        return sum(
            pos.get("pnl", 0.0)
            for pos in self.state.get("history", [])
            if pos.get("trusted_for_pnl", True)
        )

    def get_raw_closed_pnl(self) -> float:
        """Total realized P&L across all closed positions, including quarantined trades."""
        return sum(pos.get("pnl", 0.0) for pos in self.state.get("history", []))

    def get_learning_state(self) -> Dict[str, Any]:
        """Return learning state, guaranteeing defaults exist."""
        return ensure_learning_state(self.state)

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
