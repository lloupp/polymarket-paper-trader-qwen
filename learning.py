from __future__ import annotations

from typing import Any, Dict, List  # noqa: UP035

from common import now_iso as _now_iso  # noqa: F401
from common import to_dt as _to_dt


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
    size = float(closed_pos.get("size") or 0)
    pnl_pct = round(pnl / size, 6) if size > 0 else 0.0
    edge = float(closed_pos.get("edge") or 0)
    return {
        "closed_at": closed_pos.get("closed_at") or _now_iso(),
        "strategy": (closed_pos.get("strategy") or "unknown"),
        "side": (closed_pos.get("side") or "").upper(),
        "edge": edge,
        "edge_bucket": _edge_bucket(edge),
        "size": size,
        "entry_price": float(closed_pos.get("entry_price") or 0),
        "close_price": float(closed_pos.get("close_price") or 0),
        "close_reason": (closed_pos.get("close_reason") or "unknown"),
        "pnl": pnl,
        "pnl_pct": pnl_pct,
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
        sb = strategy_stats.setdefault(
            s,
            {
                "n": 0,
                "wins": 0,
                "pnl_pct_sum": 0.0,
                "stop_loss": 0,
                "_hold_sum": 0.0,
                "_hold_count": 0,
                "_yes_n": 0,
                "_yes_wins": 0,
                "_no_n": 0,
                "_no_wins": 0,
            },
        )
        win = 1 if int(f.get("win") or 0) == 1 else 0
        sb["n"] += 1
        sb["wins"] += win
        sb["pnl_pct_sum"] += float(f.get("pnl_pct") or f.get("pnl") or 0)
        if str(f.get("close_reason") or "") == "stop_loss":
            sb["stop_loss"] += 1
        if f.get("hold_minutes") is not None:
            sb["_hold_sum"] += float(f.get("hold_minutes") or 0.0)
            sb["_hold_count"] += 1
        side = str(f.get("side") or "").upper()
        if side == "YES":
            sb["_yes_n"] += 1
            sb["_yes_wins"] += win
        elif side == "NO":
            sb["_no_n"] += 1
            sb["_no_wins"] += win

        b = str(f.get("edge_bucket") or "unknown")
        eb = edge_stats.setdefault(b, {"n": 0, "wins": 0, "pnl_pct_sum": 0.0})
        eb["n"] += 1
        eb["wins"] += 1 if int(f.get("win") or 0) == 1 else 0
        eb["pnl_pct_sum"] += float(f.get("pnl_pct") or f.get("pnl") or 0)

    for s, d in strategy_stats.items():
        n = max(1, int(d["n"]))
        wins = int(d["wins"])
        # Laplace smoothing
        d["winrate"] = round((wins + 1) / (n + 2), 6)
        d["avg_pnl_pct"] = round(float(d.pop("pnl_pct_sum", 0.0)) / n, 6)
        d["stop_loss_rate"] = round(float(d["stop_loss"]) / n, 6)
        hold_count = int(d.pop("_hold_count", 0) or 0)
        hold_sum = float(d.pop("_hold_sum", 0.0) or 0.0)
        d["avg_hold_minutes"] = round(hold_sum / hold_count, 6) if hold_count else None
        # YES / NO side winrates (Laplace smoothed, only when n >= 3)
        yes_n = int(d.pop("_yes_n", 0))
        yes_w = int(d.pop("_yes_wins", 0))
        no_n = int(d.pop("_no_n", 0))
        no_w = int(d.pop("_no_wins", 0))
        d["yes_winrate"] = round((yes_w + 1) / (yes_n + 2), 6) if yes_n >= 3 else None
        d["no_winrate"] = round((no_w + 1) / (no_n + 2), 6) if no_n >= 3 else None
        d["yes_n"] = yes_n
        d["no_n"] = no_n

    for b, d in edge_stats.items():
        n = max(1, int(d["n"]))
        wins = int(d["wins"])
        d["winrate"] = round((wins + 1) / (n + 2), 6)
        d["avg_pnl_pct"] = round(float(d.pop("pnl_pct_sum", 0.0)) / n, 6)

    ls["strategy_stats"] = strategy_stats
    ls["edge_buckets_stats"] = edge_stats
    ls["last_updated_at"] = _now_iso()
    return {"strategy_stats": strategy_stats, "edge_buckets_stats": edge_stats}


def build_policy_recommendations(ls: Dict[str, Any], wallet_settings: Dict[str, Any]) -> Dict[str, Any]:
    current_min_edge = float(wallet_settings.get("min_edge", 0.05) or 0.05)
    min_samples = int(ls.get("min_samples") or 20)
    # Edge bucket decisions use half the strategy threshold — buckets fill slower than strategy totals.
    edge_min_samples = max(3, min_samples // 2)
    aggressiveness = str(ls.get("aggressiveness") or "medium")
    step = {"low": 0.005, "medium": 0.010, "high": 0.020}.get(aggressiveness, 0.010)

    reasons: List[str] = []
    conf = "low"
    eff = current_min_edge

    edge_stats = ls.get("edge_buckets_stats") or {}
    low_b = edge_stats.get("0.05_0.08", {"n": 0, "winrate": 0.5, "avg_pnl_pct": 0.0})
    high_b = edge_stats.get("0.12_0.20", {"n": 0, "winrate": 0.5, "avg_pnl_pct": 0.0})

    if int(low_b.get("n", 0)) >= edge_min_samples and float(low_b.get("avg_pnl_pct", low_b.get("avg_pnl", 0))) < 0:
        eff = min(0.12, eff + step)
        reasons.append(f"edge baixo (n={low_b['n']}) com avg_pnl_pct negativo: elevando min_edge")
    elif int(high_b.get("n", 0)) >= edge_min_samples and float(high_b.get("winrate", 0.5)) > 0.55 and float(high_b.get("avg_pnl_pct", high_b.get("avg_pnl", 0))) > 0:
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
        avg_pnl_pct = float(st.get("avg_pnl_pct", st.get("avg_pnl", 0.0)))
        if wr < 0.47 and avg_pnl_pct < 0:
            mult[strat] = 0.85
        elif wr > 0.56 and avg_pnl_pct > 0:
            mult[strat] = 1.10
        else:
            mult[strat] = 1.0

        # Apply additional YES-side penalty when YES winrate is significantly below NO
        yes_wr = st.get("yes_winrate")
        no_wr = st.get("no_winrate")
        yes_n = int(st.get("yes_n", 0))
        if yes_wr is not None and no_wr is not None and yes_n >= edge_min_samples:
            if float(yes_wr) < 0.40 and float(no_wr) > float(yes_wr) + 0.10:
                reasons.append(f"{strat}: YES winrate {yes_wr:.2f} muito abaixo de NO {no_wr:.2f} — sinal para scanner")

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
    rec = ls.get("policy_recommendations") or {}
    has_features = bool(ls.get("trades_features"))
    has_metrics = bool(ls.get("strategy_stats")) or bool(ls.get("edge_buckets_stats"))
    is_boot_policy = not rec.get("updated_at") or "boot" in list(rec.get("reasons") or [])
    needs_initial_refresh = has_features and (not has_metrics or is_boot_policy)
    if (not needs_initial_refresh) and (ls["cycle_counter"] - last) < cooldown and rec:
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
