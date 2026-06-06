from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from common import now_iso, to_dt as parse_dt

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_EVENT_LOG = BASE_DIR / "logs" / "learning_events.jsonl"
DEFAULT_PENDING_FILE = BASE_DIR / "logs" / "learning_pending_signals.json"

STORE_ENABLED = os.getenv("PAPER_LEARNING_STORE_ENABLED", "1") == "1"
DEFAULT_OUTCOME_HORIZON_MINUTES = float(os.getenv("PAPER_LEARNING_OUTCOME_HORIZON_MINUTES", "30"))
DEFAULT_PENDING_MAX = int(os.getenv("PAPER_LEARNING_PENDING_MAX", "5000"))
DEFAULT_PENDING_MAX_AGE_HOURS = float(os.getenv("PAPER_LEARNING_PENDING_MAX_AGE_HOURS", "72"))


def new_cycle_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def signal_market(signal: Dict[str, Any]) -> str:
    for key in ("market_id", "event_slug", "market_slug"):
        value = str(signal.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def signal_direction(signal: Dict[str, Any]) -> str:
    side = str(signal.get("direction") or signal.get("side") or "yes").strip().lower()
    if side in {"yes", "no"}:
        return side
    return "yes"


def signal_strategy(signal: Dict[str, Any]) -> str:
    return str(signal.get("strategy") or "unknown").strip().lower() or "unknown"


def signal_key(signal: Dict[str, Any]) -> str:
    return f"{signal_strategy(signal)}|{signal_market(signal)}|{signal_direction(signal)}"


def signal_bucket_key(signal: Dict[str, Any]) -> str:
    return f"{signal_strategy(signal)}|{signal_market(signal)}"


def side_entry_price(signal: Dict[str, Any]) -> float:
    market_probability = safe_float(signal.get("market_probability"), 0.5)
    return 1.0 - market_probability if signal_direction(signal) == "no" else market_probability


def signal_snapshot(signal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "strategy": signal_strategy(signal),
        "market_id": str(signal.get("market_id") or ""),
        "event_slug": str(signal.get("event_slug") or signal.get("market_slug") or ""),
        "event_title": str(signal.get("event_title") or "")[:240],
        "direction": signal_direction(signal),
        "model_probability": round(safe_float(signal.get("model_probability"), 0.5), 6),
        "market_probability": round(safe_float(signal.get("market_probability"), 0.5), 6),
        "entry_side_price": round(side_entry_price(signal), 6),
        "edge": round(safe_float(signal.get("edge"), 0.0), 6),
        "net_edge": round(safe_float(signal.get("net_edge"), safe_float(signal.get("edge"), 0.0)), 6),
        "confidence": round(safe_float(signal.get("confidence"), 0.0), 6),
        "suggested_size": round(safe_float(signal.get("suggested_size"), 0.0), 6),
        "spread": round(safe_float(signal.get("spread"), 0.0), 6),
        "liquidity": round(safe_float(signal.get("liquidity"), 0.0), 6),
        "volume_24hr": round(safe_float(signal.get("volume_24hr"), 0.0), 6),
        "score": round(safe_float(signal.get("_learn_score"), 0.0), 6),
    }


def signal_id_for(cycle_id: str, index: int, signal: Dict[str, Any]) -> str:
    raw = f"{cycle_id}|{index}|{signal_key(signal)}|{signal.get('timestamp', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def append_event(event: Dict[str, Any], event_log: Path = DEFAULT_EVENT_LOG) -> None:
    event_log.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload.setdefault("recorded_at", now_iso())
    with event_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows[-limit:] if limit else rows


def load_pending(path: Path = DEFAULT_PENDING_FILE) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "signals": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "signals": {}}
    if not isinstance(data, dict):
        return {"version": 1, "signals": {}}
    if not isinstance(data.get("signals"), dict):
        data["signals"] = {}
    data.setdefault("version", 1)
    return data


def save_pending(data: Dict[str, Any], path: Path = DEFAULT_PENDING_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _map_by_keys(items: Iterable[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    full: Dict[str, Dict[str, Any]] = {}
    bucket: Dict[str, Dict[str, Any]] = {}
    for item in items:
        full.setdefault(signal_key(item), item)
        bucket.setdefault(signal_bucket_key(item), item)
    return full, bucket


def _lookup_decision_item(
    signal: Dict[str, Any],
    full: Dict[str, Dict[str, Any]],
    bucket: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    return full.get(signal_key(signal)) or bucket.get(signal_bucket_key(signal))


def _decision_from_signal(
    signal: Dict[str, Any],
    *,
    selected_strategies: set[str],
    effective_min_edge: float,
    accepted_full: Dict[str, Dict[str, Any]],
    accepted_bucket: Dict[str, Dict[str, Any]],
    rejected_full: Dict[str, Dict[str, Any]],
    rejected_bucket: Dict[str, Dict[str, Any]],
    top_full: Dict[str, Dict[str, Any]],
    top_bucket: Dict[str, Dict[str, Any]],
    executed_full: Dict[str, Dict[str, Any]],
    executed_bucket: Dict[str, Dict[str, Any]],
    skipped_full: Dict[str, Dict[str, Any]],
    skipped_bucket: Dict[str, Dict[str, Any]],
    entries_paused: bool,
) -> Tuple[str, str, str, Dict[str, Any]]:
    if entries_paused:
        return "rejected", "circuit_breaker", "entries paused by circuit breaker", signal

    if signal_strategy(signal) not in selected_strategies:
        return "rejected", "strategy_filter", "strategy not selected", signal

    if abs(safe_float(signal.get("edge"), 0.0)) < effective_min_edge:
        return "rejected", "learning_min_edge", f"edge below effective_min_edge {effective_min_edge:.3f}", signal

    rejected = _lookup_decision_item(signal, rejected_full, rejected_bucket)
    if rejected:
        return "rejected", "execution_policy", str(rejected.get("reason") or "policy rejected"), signal

    executed = _lookup_decision_item(signal, executed_full, executed_bucket)
    if executed:
        merged = dict(signal)
        merged.update(executed)
        return "executed", "execution", "opened paper position", merged

    skipped = _lookup_decision_item(signal, skipped_full, skipped_bucket)
    if skipped:
        return "skipped", "execution", str(skipped.get("reason") or "execution skipped"), signal

    top = _lookup_decision_item(signal, top_full, top_bucket)
    if top:
        merged = dict(signal)
        merged.update(top)
        return "selected", "ranking", "selected for execution", merged

    accepted = _lookup_decision_item(signal, accepted_full, accepted_bucket)
    if accepted:
        merged = dict(signal)
        merged.update(accepted)
        return "not_selected", "ranking", "passed policy but not selected", merged

    return "unknown", "unknown", "decision not classified", signal


def _trim_pending(data: Dict[str, Any], max_pending: int) -> None:
    signals = data.setdefault("signals", {})
    if len(signals) <= max_pending:
        return
    ordered = sorted(
        signals.items(),
        key=lambda kv: parse_dt(kv[1].get("observed_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    data["signals"] = dict(ordered[:max_pending])


def _expire_pending(
    data: Dict[str, Any],
    *,
    event_log: Path,
    now: datetime,
    max_age_hours: float,
) -> int:
    cutoff = now - timedelta(hours=max_age_hours)
    signals = data.setdefault("signals", {})
    expired = 0
    for sid, pending in list(signals.items()):
        observed_at = parse_dt(pending.get("observed_at"))
        if observed_at and observed_at < cutoff:
            append_event(
                {
                    "event_type": "signal_outcome_expired",
                    "signal_id": sid,
                    "cycle_id": pending.get("cycle_id"),
                    "signal_key": pending.get("signal_key"),
                    "decision": pending.get("decision"),
                    "reason": "pending signal exceeded max age",
                },
                event_log=event_log,
            )
            del signals[sid]
            expired += 1
    return expired


def record_signal_decisions(
    *,
    cycle_id: str,
    signals: List[Dict[str, Any]],
    selected_strategies: set[str],
    effective_min_edge: float,
    accepted_signals: List[Dict[str, Any]],
    policy_rejected: List[Dict[str, Any]],
    top_signals: List[Dict[str, Any]],
    executed: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    entries_paused: bool = False,
    event_log: Path = DEFAULT_EVENT_LOG,
    pending_file: Path = DEFAULT_PENDING_FILE,
    now: Optional[datetime] = None,
    outcome_horizon_minutes: float = DEFAULT_OUTCOME_HORIZON_MINUTES,
    max_pending: int = DEFAULT_PENDING_MAX,
) -> Dict[str, Any]:
    if not STORE_ENABLED:
        return {"enabled": False, "decision_events": 0, "pending": 0}

    observed_at = now or datetime.now(timezone.utc)
    due_at = observed_at + timedelta(minutes=outcome_horizon_minutes)

    accepted_full, accepted_bucket = _map_by_keys(accepted_signals)
    rejected_full, rejected_bucket = _map_by_keys(policy_rejected)
    top_full, top_bucket = _map_by_keys(top_signals)
    executed_full, executed_bucket = _map_by_keys(executed)
    skipped_full, skipped_bucket = _map_by_keys(skipped)

    pending = load_pending(pending_file)
    decision_counts: Dict[str, int] = {}

    for idx, signal in enumerate(signals):
        decision, stage, reason, enriched_signal = _decision_from_signal(
            signal,
            selected_strategies=selected_strategies,
            effective_min_edge=effective_min_edge,
            accepted_full=accepted_full,
            accepted_bucket=accepted_bucket,
            rejected_full=rejected_full,
            rejected_bucket=rejected_bucket,
            top_full=top_full,
            top_bucket=top_bucket,
            executed_full=executed_full,
            executed_bucket=executed_bucket,
            skipped_full=skipped_full,
            skipped_bucket=skipped_bucket,
            entries_paused=entries_paused,
        )
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        sid = signal_id_for(cycle_id, idx, signal)
        snap = signal_snapshot(enriched_signal)
        event = {
            "event_type": "signal_decision",
            "signal_id": sid,
            "signal_key": signal_key(signal),
            "cycle_id": cycle_id,
            "observed_at": observed_at.isoformat(),
            "decision": decision,
            "stage": stage,
            "reason": reason,
            "is_counterfactual": decision != "executed",
            "selected_strategy_mode": sorted(selected_strategies),
            "effective_min_edge": round(effective_min_edge, 6),
            "signal": snap,
        }
        append_event(event, event_log=event_log)
        pending["signals"][sid] = {
            "signal_id": sid,
            "signal_key": signal_key(signal),
            "cycle_id": cycle_id,
            "observed_at": observed_at.isoformat(),
            "outcome_due_at": due_at.isoformat(),
            "decision": decision,
            "stage": stage,
            "reason": reason,
            "entry_side_price": snap["entry_side_price"],
            "signal": snap,
        }

    _trim_pending(pending, max_pending)
    save_pending(pending, pending_file)
    return {
        "enabled": True,
        "decision_events": len(signals),
        "decision_counts": decision_counts,
        "pending": len(pending.get("signals", {})),
    }


def observe_signal_outcomes_from_signals(
    signals: List[Dict[str, Any]],
    *,
    event_log: Path = DEFAULT_EVENT_LOG,
    pending_file: Path = DEFAULT_PENDING_FILE,
    now: Optional[datetime] = None,
    max_age_hours: float = DEFAULT_PENDING_MAX_AGE_HOURS,
) -> Dict[str, Any]:
    if not STORE_ENABLED:
        return {"enabled": False, "outcome_events": 0, "pending": 0}

    observed_at = now or datetime.now(timezone.utc)
    pending = load_pending(pending_file)
    current_by_key = {signal_key(s): s for s in signals}
    outcome_events = 0
    expired = _expire_pending(pending, event_log=event_log, now=observed_at, max_age_hours=max_age_hours)

    for sid, item in list(pending.get("signals", {}).items()):
        due_at = parse_dt(item.get("outcome_due_at"))
        if due_at and observed_at < due_at:
            continue
        current = current_by_key.get(str(item.get("signal_key") or ""))
        if not current:
            continue

        entry_price = safe_float(item.get("entry_side_price"), 0.0)
        observed_price = side_entry_price(current)
        pnl_per_share = observed_price - entry_price
        return_pct = (pnl_per_share / entry_price) if entry_price > 0 else 0.0
        original_at = parse_dt(item.get("observed_at"))
        horizon_minutes = None
        if original_at:
            horizon_minutes = round((observed_at - original_at).total_seconds() / 60.0, 3)

        append_event(
            {
                "event_type": "signal_outcome",
                "signal_id": sid,
                "signal_key": item.get("signal_key"),
                "cycle_id": item.get("cycle_id"),
                "observed_at": observed_at.isoformat(),
                "original_observed_at": item.get("observed_at"),
                "horizon_minutes": horizon_minutes,
                "decision": item.get("decision"),
                "stage": item.get("stage"),
                "entry_side_price": round(entry_price, 6),
                "observed_side_price": round(observed_price, 6),
                "paper_pnl_per_share": round(pnl_per_share, 6),
                "paper_return_pct": round(return_pct, 6),
                "win": 1 if pnl_per_share > 0 else 0,
                "original_signal": item.get("signal", {}),
                "observed_signal": signal_snapshot(current),
            },
            event_log=event_log,
        )
        del pending["signals"][sid]
        outcome_events += 1

    save_pending(pending, pending_file)
    return {
        "enabled": True,
        "outcome_events": outcome_events,
        "expired": expired,
        "pending": len(pending.get("signals", {})),
    }


def summarize_learning_events(event_log: Path = DEFAULT_EVENT_LOG, limit: int = 1000) -> Dict[str, Any]:
    rows = read_jsonl(event_log, limit=limit)
    decisions = [r for r in rows if r.get("event_type") == "signal_decision"]
    outcomes = [r for r in rows if r.get("event_type") == "signal_outcome"]

    by_decision: Dict[str, int] = {}
    by_strategy: Dict[str, Dict[str, Any]] = {}

    for row in decisions:
        decision = str(row.get("decision") or "unknown")
        by_decision[decision] = by_decision.get(decision, 0) + 1

    for row in outcomes:
        sig = row.get("original_signal", {}) if isinstance(row.get("original_signal"), dict) else {}
        strategy = str(sig.get("strategy") or "unknown")
        bucket = by_strategy.setdefault(strategy, {"n": 0, "wins": 0, "return_sum": 0.0})
        bucket["n"] += 1
        bucket["wins"] += 1 if safe_int(row.get("win"), 0) == 1 else 0
        bucket["return_sum"] += safe_float(row.get("paper_return_pct"), 0.0)

    for stats in by_strategy.values():
        n = max(1, int(stats["n"]))
        stats["winrate"] = round((int(stats["wins"]) + 1) / (n + 2), 6)
        stats["avg_return_pct"] = round(float(stats["return_sum"]) / n, 6)
        del stats["return_sum"]

    return {
        "event_log": str(event_log),
        "total_recent_events": len(rows),
        "decision_events": len(decisions),
        "outcome_events": len(outcomes),
        "decision_counts": by_decision,
        "outcome_by_strategy": by_strategy,
    }
