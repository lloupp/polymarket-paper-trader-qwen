from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_dt(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def default_learning_state() -> Dict[str, Any]:
    return {
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
    }


def ensure_learning_state(wallet_state: Dict[str, Any]) -> Dict[str, Any]:
    ls = wallet_state.get("learning_state")
    if not isinstance(ls, dict):
        ls = default_learning_state()
        wallet_state["learning_state"] = ls
        return ls

    defaults = default_learning_state()
    for k, v in defaults.items():
        if k not in ls:
            ls[k] = v
    if not isinstance(ls.get("trades_features"), list):
        ls["trades_features"] = []
    if not isinstance(ls.get("strategy_stats"), dict):
        ls["strategy_stats"] = {}
    if not isinstance(ls.get("edge_buckets_stats"), dict):
        ls["edge_buckets_stats"] = {}
    if not isinstance(ls.get("policy_recommendations"), dict):
        ls["policy_recommendations"] = defaults["policy_recommendations"]
    return ls


def _edge_bucket(edge: float) -> str:
    x = abs(edge)
    if x < 0.05:
        return "lt_0.05"
    if x < 0.08:
        return "0.05_0.08"
    if x < 0.12:
        return "0.08_0.12"
    if x < 0.20:
        return "0.12_0.20"
    return "ge_0.20"


def build_trade_feature(closed_pos: Dict[str, Any], strategy_mode: str) -> Dict[str, Any]:
    opened = _to_dt(closed_pos.get("opened_at"))
    closed = _to_dt(closed_pos.get("closed_at"))
    hold_minutes = None
    if opened and closed:
        hold_minutes = round((closed - opened).total_seconds() / 60.0, 3)

    pnl = float((closed_pos.get("realized_pnl") if closed_pos.get("realized_pnl") is not None else closed_pos.get("pnl", 0)) or 0)
    edge = float(closed_pos.get("edge") or 0)
    return {
        "closed_at": closed_pos.get("closed_at") or _now_iso(),
        "strategy": (closed_pos.get("strategy") or "unknown"),
        "side": (closed_pos.get("side") or "").upper(),
        "edge": edge,
        "edge_bucket": _edge_bucket(edge),
        "size": float(closed_pos.get("size") or 0),
        "entry_price": float(closed_pos.get("entry_price") or 0),
        "close_price": float(closed_pos.get("close_price") or 0),
        "close_reason": (closed_pos.get("close_reason") or "unknown"),
        "pnl": pnl,
        "win": 1 if pnl > 0 else 0,
        "hold_minutes": hold_minutes,
        "strategy_mode": strategy_mode,
    }


def append_trade_feature(ls: Dict[str, Any], feat: Dict[str, Any]) -> None:
    feats: List[Dict[str, Any]] = ls.setdefault("trades_features", [])
    feats.append(feat)
    max_features = int(ls.get("max_features") or 1000)
    if len(feats) > max_features:
        del feats[:-max_features]


def compute_learning_metrics(ls: Dict[str, Any]) -> Dict[str, Any]:
    feats = list(ls.get("trades_features") or [])
    window = int(ls.get("window_trades") or 400)
    feats = feats[-window:]

    strategy_stats: Dict[str, Dict[str, Any]] = {}
    edge_stats: Dict[str, Dict[str, Any]] = {}

    for f in feats:
        s = str(f.get("strategy") or "unknown")
        sb = strategy_stats.setdefault(s, {"n": 0, "wins": 0, "pnl_sum": 0.0, "stop_loss": 0, "avg_hold_minutes": 0.0})
        sb["n"] += 1
        sb["wins"] += 1 if int(f.get("win") or 0) == 1 else 0
        sb["pnl_sum"] += float(f.get("pnl") or 0)
        if str(f.get("close_reason") or "") == "stop_loss":
            sb["stop_loss"] += 1

        b = str(f.get("edge_bucket") or "unknown")
        eb = edge_stats.setdefault(b, {"n": 0, "wins": 0, "pnl_sum": 0.0})
        eb["n"] += 1
        eb["wins"] += 1 if int(f.get("win") or 0) == 1 else 0
        eb["pnl_sum"] += float(f.get("pnl") or 0)

    for s, d in strategy_stats.items():
        n = max(1, int(d["n"]))
        wins = int(d["wins"])
        # Laplace smoothing
        d["winrate"] = round((wins + 1) / (n + 2), 6)
        d["avg_pnl"] = round(float(d["pnl_sum"]) / n, 6)
        d["stop_loss_rate"] = round(float(d["stop_loss"]) / n, 6)

    for b, d in edge_stats.items():
        n = max(1, int(d["n"]))
        wins = int(d["wins"])
        d["winrate"] = round((wins + 1) / (n + 2), 6)
        d["avg_pnl"] = round(float(d["pnl_sum"]) / n, 6)

    ls["strategy_stats"] = strategy_stats
    ls["edge_buckets_stats"] = edge_stats
    ls["last_updated_at"] = _now_iso()
    return {"strategy_stats": strategy_stats, "edge_buckets_stats": edge_stats}


def build_policy_recommendations(ls: Dict[str, Any], wallet_settings: Dict[str, Any]) -> Dict[str, Any]:
    current_min_edge = float(wallet_settings.get("min_edge", 0.05) or 0.05)
    min_samples = int(ls.get("min_samples") or 20)
    aggressiveness = str(ls.get("aggressiveness") or "medium")
    step = {"low": 0.003, "medium": 0.005, "high": 0.01}.get(aggressiveness, 0.005)

    reasons: List[str] = []
    conf = "low"
    eff = current_min_edge

    edge_stats = ls.get("edge_buckets_stats") or {}
    low_b = edge_stats.get("0.05_0.08", {"n": 0, "winrate": 0.5, "avg_pnl": 0.0})
    high_b = edge_stats.get("0.12_0.20", {"n": 0, "winrate": 0.5, "avg_pnl": 0.0})

    if int(low_b.get("n", 0)) >= min_samples and float(low_b.get("avg_pnl", 0)) < 0:
        eff = min(0.12, eff + step)
        reasons.append("edge baixo com pnl medio negativo: elevando min_edge")
    elif int(high_b.get("n", 0)) >= min_samples and float(high_b.get("winrate", 0.5)) > 0.55 and float(high_b.get("avg_pnl", 0)) > 0:
        eff = max(0.03, eff - (step / 2.0))
        reasons.append("edge alto consistente: reduzindo min_edge levemente")

    mult: Dict[str, float] = {}
    strategy_stats = ls.get("strategy_stats") or {}
    qualified = 0
    for strat, st in strategy_stats.items():
        n = int(st.get("n", 0))
        if n < min_samples:
            continue
        qualified += 1
        wr = float(st.get("winrate", 0.5))
        avg_pnl = float(st.get("avg_pnl", 0.0))
        if wr < 0.47 and avg_pnl < 0:
            mult[strat] = 0.85
        elif wr > 0.56 and avg_pnl > 0:
            mult[strat] = 1.10
        else:
            mult[strat] = 1.0

    if qualified >= 3:
        conf = "high"
    elif qualified >= 1:
        conf = "medium"

    if not reasons:
        reasons.append("sem sinal forte para ajuste de min_edge")

    rec = {
        "effective_min_edge": round(max(0.03, min(0.12, eff)), 6),
        "strategy_multipliers": mult,
        "confidence": conf,
        "reasons": reasons,
        "updated_at": _now_iso(),
    }
    ls["policy_recommendations"] = rec
    return rec


def maybe_refresh_policy(ls: Dict[str, Any], wallet_settings: Dict[str, Any]) -> Dict[str, Any]:
    ls["cycle_counter"] = int(ls.get("cycle_counter") or 0) + 1
    cooldown = int(ls.get("cooldown_cycles") or 10)
    last = int(ls.get("last_policy_cycle") or 0)
    if (ls["cycle_counter"] - last) < cooldown and ls.get("policy_recommendations"):
        return ls["policy_recommendations"]

    compute_learning_metrics(ls)
    rec = build_policy_recommendations(ls, wallet_settings)
    ls["last_policy_cycle"] = int(ls.get("cycle_counter") or 0)
    return rec


def learning_snapshot(ls: Dict[str, Any]) -> Dict[str, Any]:
    rec = ls.get("policy_recommendations") or {}
    return {
        "enabled": bool(ls.get("enabled", True)),
        "shadow_mode": bool(ls.get("shadow_mode", False)),
        "min_samples": int(ls.get("min_samples") or 20),
        "aggressiveness": str(ls.get("aggressiveness") or "medium"),
        "effective_min_edge": float(rec.get("effective_min_edge", 0.05) or 0.05),
        "confidence": str(rec.get("confidence") or "low"),
        "reasons": list(rec.get("reasons") or []),
        "strategies_tracked": len(ls.get("strategy_stats") or {}),
        "features_count": len(ls.get("trades_features") or []),
        "updated_at": rec.get("updated_at") or ls.get("last_updated_at"),
    }
