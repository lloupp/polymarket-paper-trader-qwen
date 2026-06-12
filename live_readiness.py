"""Live-readiness gate: is the CURRENT configuration proven enough for real money?

Measures only trades closed after CONFIG_EPOCH (the btc TP-off deploy, i.e. the
first cycle of the present strategy configuration) against explicit criteria.
Prints a human report; --json emits machine-readable output.

Usage:
  python live_readiness.py [--json]
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent

# Start of the current configuration: btc_5m_momentum hold-to-resolution deploy.
CONFIG_EPOCH = datetime(2026, 6, 11, 0, 5, tzinfo=timezone.utc)

# Gate criteria (all must pass before building live execution).
MIN_DAYS = 14
MIN_TRADES = 500
MIN_TOTAL_PNL = 0.0          # cumulative > 0
MIN_LAST7D_PNL = 0.0         # no late-window decay
MAX_DRAWDOWN_PCT = 0.05      # of initial bankroll, on the closed-PnL curve
MIN_BTC_EXPECTANCY_PCT = 0.015  # avg pnl per trade / avg stake (room for live friction)


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def evaluate(wallet_path: Path = BASE / "wallet.json") -> dict:
    wallet = json.loads(wallet_path.read_text(encoding="utf-8"))
    initial = float(wallet.get("initial_bankroll") or 10000.0)
    history = [h for h in wallet.get("history", []) if h.get("trusted_for_pnl", True)]
    trades = sorted(
        (h for h in history if (t := _parse(h.get("closed_at"))) and t > CONFIG_EPOCH),
        key=lambda h: _parse(h.get("closed_at")),
    )

    now = datetime.now(timezone.utc)
    days = (now - CONFIG_EPOCH).total_seconds() / 86400.0
    total_pnl = sum(float(h.get("pnl", 0) or 0) for h in trades)
    last7 = [h for h in trades if (now - _parse(h.get("closed_at"))).days < 7]
    last7_pnl = sum(float(h.get("pnl", 0) or 0) for h in last7)

    # Max drawdown on the cumulative closed-PnL curve.
    peak = equity = 0.0
    max_dd = 0.0
    for h in trades:
        equity += float(h.get("pnl", 0) or 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    btc = [h for h in trades if h.get("strategy") == "btc_5m_momentum"]
    btc_cost = sum(float(h.get("cost", 0) or 0) for h in btc)
    btc_pnl = sum(float(h.get("pnl", 0) or 0) for h in btc)
    btc_expectancy = (btc_pnl / btc_cost) if btc_cost > 0 else 0.0

    by_strategy = {}
    for h in trades:
        s = by_strategy.setdefault(h.get("strategy", "?"), {"n": 0, "pnl": 0.0, "wins": 0})
        pnl = float(h.get("pnl", 0) or 0)
        s["n"] += 1
        s["pnl"] = round(s["pnl"] + pnl, 2)
        s["wins"] += 1 if pnl > 0 else 0

    criteria = {
        "dias_de_amostra": {"valor": round(days, 1), "alvo": MIN_DAYS, "ok": days >= MIN_DAYS},
        "trades_fechados": {"valor": len(trades), "alvo": MIN_TRADES, "ok": len(trades) >= MIN_TRADES},
        "pnl_acumulado": {"valor": round(total_pnl, 2), "alvo": f"> {MIN_TOTAL_PNL}", "ok": total_pnl > MIN_TOTAL_PNL},
        "pnl_ultimos_7d": {"valor": round(last7_pnl, 2), "alvo": f"> {MIN_LAST7D_PNL}", "ok": last7_pnl > MIN_LAST7D_PNL},
        "drawdown_maximo": {
            "valor": round(max_dd, 2),
            "alvo": f"< {MAX_DRAWDOWN_PCT:.0%} do bankroll (${initial * MAX_DRAWDOWN_PCT:.0f})",
            "ok": max_dd < initial * MAX_DRAWDOWN_PCT,
        },
        "expectancia_btc_por_trade": {
            "valor": f"{btc_expectancy:.2%}",
            "alvo": f">= {MIN_BTC_EXPECTANCY_PCT:.1%}",
            "ok": btc_expectancy >= MIN_BTC_EXPECTANCY_PCT,
        },
    }
    passed = sum(1 for c in criteria.values() if c["ok"])
    return {
        "config_epoch": CONFIG_EPOCH.isoformat(),
        "avaliado_em": now.isoformat(),
        "criterios": criteria,
        "aprovados": f"{passed}/{len(criteria)}",
        "pronto_para_live": passed == len(criteria),
        "por_estrategia": by_strategy,
        "bankroll": wallet.get("bankroll"),
    }


def main() -> int:
    report = evaluate()
    if "--json" in sys.argv:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    print(f"LIVE-READINESS — configuração desde {CONFIG_EPOCH:%d/%m %H:%M} UTC")
    print(f"avaliado em {datetime.now(timezone.utc):%d/%m %H:%M} UTC | bankroll ${report['bankroll']:,.2f}\n")
    for name, c in report["criterios"].items():
        mark = "PASS " if c["ok"] else "PEND."
        print(f"  [{mark}] {name:28} valor={c['valor']}  alvo={c['alvo']}")
    print(f"\ncritérios aprovados: {report['aprovados']}"
          f" -> {'PRONTO para construir execução live' if report['pronto_para_live'] else 'seguir em paper'}")
    print("\npor estratégia (desde a época da configuração):")
    for s, v in sorted(report["por_estrategia"].items(), key=lambda x: -x[1]["pnl"]):
        wr = 100 * v["wins"] / v["n"] if v["n"] else 0
        print(f"  {s:22} n={v['n']:>4} WR={wr:>3.0f}% PnL=${v['pnl']:+9.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
