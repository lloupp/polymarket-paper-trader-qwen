#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen

BASE = Path(__file__).resolve().parent
LOGS = BASE / "logs"
STATE_FILE = LOGS / "runtime_state.json"
TIMELINE_JSON = LOGS / "timeline_report.json"
TIMELINE_HTML = LOGS / "timeline_report.html"
WALLET_FILE = BASE / "wallet.json"
LAST_REPORT = LOGS / "last_report.txt"
LAST_REPORT_JSON = LOGS / "last_report.json"
ACTIVE_STRATEGY_FILE = LOGS / "active_strategy.txt"
LOOP_SECONDS_FILE = LOGS / "loop_seconds.txt"

KNOWN_STRATEGIES = [
    "btc_5m_momentum", "endgame_last_minute", "arbitrage", "value",
    "mean_reversion", "volume_spike", "smart_money", "event_countdown"
]
RECOMMENDED_MODE = "btc_5m_momentum,endgame_last_minute,smart_money,event_countdown"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_report(report: str) -> dict[str, Any]:
    opened = skipped = actionable = 0
    m = re.search(r"Trades:\*\*\s*(\d+) opened,\s*(\d+) skipped", report)
    if m:
        opened, skipped = int(m.group(1)), int(m.group(2))
    m = re.search(r"signals \((\d+) actionable\)", report)
    if m:
        actionable = int(m.group(1))
    m = re.search(r"\|\s*mode=([a-zA-Z0-9_,\-]+)", report)
    mode = m.group(1).strip() if m else "unknown"
    return {"opened": opened, "skipped": skipped, "actionable": actionable, "mode": mode}


def read_report_metrics() -> dict[str, Any]:
    report_json = load_json(LAST_REPORT_JSON, {})
    if isinstance(report_json, dict) and report_json:
        execution = report_json.get("execution", {}) if isinstance(report_json.get("execution"), dict) else {}
        scan = report_json.get("scan", {}) if isinstance(report_json.get("scan"), dict) else {}
        return {
            "opened": int(execution.get("executed", 0) or 0),
            "skipped": int(execution.get("skipped", 0) or 0),
            "actionable": int(scan.get("actionable_signals", 0) or 0),
            "mode": str(scan.get("strategy_mode") or "unknown"),
        }

    report = LAST_REPORT.read_text(encoding="utf-8", errors="ignore") if LAST_REPORT.exists() else ""
    return parse_report(report)


def choose_mode_from_history(wallet: dict[str, Any]) -> str:
    settings = wallet.get("settings", {}) if isinstance(wallet, dict) else {}
    window = int(settings.get("rot_window", 80) or 80)
    top_k = int(settings.get("rot_top_k", 4) or 4)
    w_win = float(settings.get("rot_weight_winrate", 0.7) or 0.7)
    w_pnl = float(settings.get("rot_weight_pnl", 0.3) or 0.3)

    hist = wallet.get("history", [])[-window:]
    by = defaultdict(lambda: {"wins": 0, "n": 0, "pnl": 0.0})
    for h in hist:
        s = str(h.get("strategy") or "").strip().lower()
        if s not in KNOWN_STRATEGIES:
            continue
        pnl = float(h.get("pnl", 0.0) or 0.0)
        by[s]["n"] += 1
        by[s]["pnl"] += pnl
        if pnl > 0:
            by[s]["wins"] += 1
    if not by:
        return RECOMMENDED_MODE

    scored = []
    for s, v in by.items():
        n = max(v["n"], 1)
        win = v["wins"] / n
        avg = v["pnl"] / n
        score = (win * w_win) + (max(-1.0, min(1.0, avg / 5.0)) * w_pnl)
        scored.append((score, s))
    scored.sort(reverse=True)
    selected = [s for _, s in scored[:top_k]]
    if "endgame_last_minute" not in selected:
        selected.append("endgame_last_minute")
    return ",".join(dict.fromkeys(selected))


def dynamic_interval(wallet: dict[str, Any], report_metrics: dict[str, Any]) -> int:
    base = 90
    hist = wallet.get("history", [])[-20:]
    pnls = [float(x.get("pnl", 0.0) or 0.0) for x in hist]
    if len(pnls) >= 5:
        mean = sum(pnls) / len(pnls)
        var = sum((x - mean) ** 2 for x in pnls) / len(pnls)
        vol = math.sqrt(var)
        if vol >= 8:
            return 70
        if vol <= 2:
            return 110
    if report_metrics.get("actionable", 0) == 0:
        return 120
    return base


def circuit_breaker(wallet: dict[str, Any], prev_state: dict[str, Any]) -> tuple[bool, str, int]:
    settings = wallet.get("settings", {}) if isinstance(wallet, dict) else {}
    loss_seq_threshold = int(settings.get("cb_loss_seq", 4) or 4)
    loss_sum_threshold = float(settings.get("cb_loss_sum6", -25.0) or -25.0)
    cooldown_default = int(settings.get("cb_cooldown_cycles", 2) or 2)

    hist = wallet.get("history", [])
    last = hist[-6:]
    pnls = [float(x.get("pnl", 0.0) or 0.0) for x in last]
    losses_seq = 0
    for p in reversed(pnls):
        if p < 0:
            losses_seq += 1
        else:
            break
    recent_sum = sum(pnls)
    cool = int(prev_state.get("cooldown_cycles", 0) or 0)
    if cool > 0:
        return True, "cooldown_ativo", cool - 1
    if losses_seq >= loss_seq_threshold or recent_sum <= loss_sum_threshold:
        return True, f"circuit_breaker(losses_seq={losses_seq},sum6={recent_sum:.2f})", cooldown_default
    return False, "ok", 0


def build_timeline_entry(wallet: dict[str, Any], metrics: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    bankroll = float(wallet.get("bankroll", 0.0) or 0.0)
    initial = float(wallet.get("initial_bankroll", 0.0) or 0.0)
    hist = wallet.get("history", [])
    last10 = hist[-10:]
    wins = sum(1 for h in last10 if float(h.get("pnl", 0.0) or 0.0) > 0)
    closed_pnl_10 = sum(float(h.get("pnl", 0.0) or 0.0) for h in last10)
    return {
        "ts": now_iso(),
        "bankroll": bankroll,
        "pnl_total": round(bankroll - initial, 4),
        "open_positions": len(wallet.get("positions", {})),
        "history_count": len(hist),
        "mode": metrics.get("mode"),
        "opened": metrics.get("opened", 0),
        "skipped": metrics.get("skipped", 0),
        "actionable": metrics.get("actionable", 0),
        "kpi_window": {
            "closed_10": len(last10),
            "wins_10": wins,
            "winrate_10": round((wins / len(last10)) if last10 else 0.0, 4),
            "realized_pnl_10": round(closed_pnl_10, 4),
        },
        "alerts": state.get("alerts", []),
        "controls": {
            "entries_paused": bool(state.get("entries_paused", False)),
            "pause_reason": state.get("pause_reason", ""),
            "loop_interval_seconds": int(state.get("loop_interval_seconds", 90)),
            "strategy_mode": state.get("strategy_mode", ""),
        },
    }


def render_html(timeline: list[dict[str, Any]], wallet: dict[str, Any]) -> str:
    view = timeline[-120:]
    rows = []

    last = view[-1] if view else {}
    k_last = last.get("kpi_window", {}) if isinstance(last, dict) else {}
    c_last = last.get("controls", {}) if isinstance(last, dict) else {}

    def cls_pnl(v: float) -> str:
        return "pos" if v > 0 else ("neg" if v < 0 else "neu")

    def cls_result(v: float) -> str:
        if v > 0:
            return "rwin"
        if v < 0:
            return "rloss"
        return "rflat"

    for x in reversed(view):
        a = " | ".join(x.get("alerts", [])) if x.get("alerts") else "-"
        k = x.get("kpi_window", {})
        c = x.get("controls", {})
        pnl_total = float(x.get("pnl_total", 0) or 0)
        pnl10 = float(k.get("realized_pnl_10", 0) or 0)
        paused = bool(c.get("entries_paused", False))
        status = "PAUSADO" if paused else "ATIVO"
        status_cls = "warn" if paused else "ok"
        rows.append(
            f"<tr>"
            f"<td>{x.get('ts','')}</td>"
            f"<td>{x.get('mode','')}</td>"
            f"<td><span class='pill {status_cls}'>{status}</span></td>"
            f"<td>{x.get('opened',0)}/{x.get('skipped',0)}</td>"
            f"<td>{x.get('actionable',0)}</td>"
            f"<td>{x.get('bankroll',0):.2f}</td>"
            f"<td class='{cls_pnl(pnl_total)}'>{pnl_total:+.2f}</td>"
            f"<td>{k.get('wins_10',0)}/{k.get('closed_10',0)} ({100*k.get('winrate_10',0):.1f}%)</td>"
            f"<td class='{cls_pnl(pnl10)}'>{pnl10:+.2f}</td>"
            f"<td>{c.get('loop_interval_seconds', 90)}s</td>"
            f"<td>{c.get('pause_reason','-')}</td>"
            f"<td>{a}</td>"
            f"</tr>"
        )

    history = wallet.get("history", []) if isinstance(wallet, dict) else []
    resolved_rows = []
    recent_resolved = history[-30:]
    wins = losses = flats = 0
    for i, h in enumerate(reversed(recent_resolved), 1):
        pnl = float(h.get("pnl", 0.0) or 0.0)
        if pnl > 0:
            wins += 1
            tag = "GREEN"
        elif pnl < 0:
            losses += 1
            tag = "RED"
        else:
            flats += 1
            tag = "YELLOW"
        resolved_rows.append(
            f"<tr class='{cls_result(pnl)}'>"
            f"<td>{i}</td>"
            f"<td><span class='pill {cls_result(pnl)}'>{tag}</span></td>"
            f"<td>{h.get('strategy','-')}</td>"
            f"<td>{h.get('side','-')}</td>"
            f"<td>{h.get('market_slug') or h.get('market') or '-'}</td>"
            f"<td>{float(h.get('edge',0) or 0):.4f}</td>"
            f"<td class='{cls_pnl(pnl)}'>{pnl:+.4f}</td>"
            f"</tr>"
        )

    last_pnl = float(last.get("pnl_total", 0) or 0) if last else 0.0
    last_wr = float(k_last.get("winrate_10", 0) or 0)
    last_rpnl = float(k_last.get("realized_pnl_10", 0) or 0)
    last_bankroll = float(last.get("bankroll", 0) or 0) if last else 0.0
    last_open = int(last.get("open_positions", 0) or 0) if last else 0
    last_interval = int(c_last.get("loop_interval_seconds", 90) or 90) if last else 90
    last_reason = c_last.get("pause_reason", "-") if last else "-"
    total = len(view)
    paused_count = sum(1 for e in view if bool((e.get("controls", {}) or {}).get("entries_paused", False)))

    return f"""<!doctype html><html><head><meta charset='utf-8'><title>Timeline Report</title>
<style>
:root{{--bg:#0b1220;--panel:#0f172a;--panel2:#111827;--line:#253246;--txt:#e5e7eb;--muted:#93a4bd;--green:#22c55e;--red:#ef4444;--amber:#f59e0b;}}
*{{box-sizing:border-box}} body{{font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--txt);padding:16px;margin:0}}
.wrap{{max-width:1600px;margin:0 auto}}
.grid{{display:grid;grid-template-columns:repeat(6,minmax(140px,1fr));gap:10px;margin:10px 0 14px}}
.card{{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:12px;padding:10px}}
.k{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}}
.v{{font-size:20px;font-weight:700;margin-top:4px}}
.pos{{color:var(--green)}} .neg{{color:var(--red)}} .neu{{color:#cbd5e1}}
.pill{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line)}}
.pill.ok{{background:rgba(34,197,94,.15);color:#86efac;border-color:rgba(34,197,94,.35)}}
.pill.warn{{background:rgba(245,158,11,.15);color:#fcd34d;border-color:rgba(245,158,11,.35)}}
.pill.rwin{{background:rgba(34,197,94,.20);color:#86efac;border-color:rgba(34,197,94,.45)}}
.pill.rloss{{background:rgba(239,68,68,.20);color:#fca5a5;border-color:rgba(239,68,68,.45)}}
.pill.rflat{{background:rgba(245,158,11,.20);color:#fcd34d;border-color:rgba(245,158,11,.45)}}
.flt-btn{{background:#13233f;color:#dbe7ff;border:1px solid #304564;border-radius:8px;padding:4px 8px;font-size:11px;cursor:pointer;margin-right:4px}}
.flt-btn:hover{{background:#1a2f52}}
.meta{{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-bottom:12px}}
.tbl{{overflow:auto;border:1px solid var(--line);border-radius:12px}}
table{{width:100%;border-collapse:collapse;min-width:1200px}}
th,td{{border-bottom:1px solid var(--line);padding:8px;font-size:12px;vertical-align:top}}
th{{background:#0d1628;color:#dbe7ff;position:sticky;top:0;z-index:1;text-align:left}}
tr:hover td{{background:#0f1b31}}
small{{color:var(--muted)}}
.rwin td{{background:rgba(34,197,94,.06)}} .rloss td{{background:rgba(239,68,68,.06)}} .rflat td{{background:rgba(245,158,11,.06)}}
</style>
</head><body><div class='wrap'>
<h2 style='margin:0 0 6px'>Timeline Operacional</h2>
<div class='meta'>
  <span>Gerado automaticamente</span>
  <span>Entradas exibidas: {total}</span>
  <span>Último timestamp: {last.get('ts', '-')}</span>
</div>
<div class='grid'>
  <div class='card'><div class='k'>Bankroll</div><div class='v'>{last_bankroll:.2f}</div></div>
  <div class='card'><div class='k'>PnL Total</div><div class='v {cls_pnl(last_pnl)}'>{last_pnl:+.2f}</div></div>
  <div class='card'><div class='k'>Winrate (10)</div><div class='v'>{last_wr*100:.1f}%</div></div>
  <div class='card'><div class='k'>Realized PnL (10)</div><div class='v {cls_pnl(last_rpnl)}'>{last_rpnl:+.2f}</div></div>
  <div class='card'><div class='k'>Posições Abertas</div><div class='v'>{last_open}</div></div>
  <div class='card'><div class='k'>Loop / Pausas</div><div class='v'>{last_interval}s <small>| pausado {paused_count}x</small></div></div>
</div>
<div class='meta'><span><b>Motivo atual:</b> {last_reason}</span></div>

<h3 style='margin:18px 0 8px'>Ordens resolvidas (últimas 30)</h3>
<div class='meta'>
  <span><span class='pill rwin'>GREEN</span> {wins}</span>
  <span><span class='pill rloss'>RED</span> {losses}</span>
  <span><span class='pill rflat'>YELLOW</span> {flats}</span>
  <span style='margin-left:8px'>
    <button class='flt-btn' onclick="setResolvedFilter('all')">Todos</button>
    <button class='flt-btn' onclick="setResolvedFilter('rwin')">GREEN</button>
    <button class='flt-btn' onclick="setResolvedFilter('rloss')">RED</button>
    <button class='flt-btn' onclick="setResolvedFilter('rflat')">YELLOW</button>
  </span>
  <span style='margin-left:8px'>
    <button class='flt-btn' onclick="sortResolved('pnl_desc')">Maior ganho</button>
    <button class='flt-btn' onclick="sortResolved('pnl_asc')">Maior perda</button>
    <button class='flt-btn' onclick="sortResolved('recent')">Mais recente</button>
  </span>
</div>
<div class='tbl'>
<table><thead><tr>
<th>#</th><th>Resultado</th><th>Estratégia</th><th>Lado</th><th>Mercado</th><th>Edge</th><th>PnL</th>
</tr></thead><tbody id='resolvedBody'>{''.join(resolved_rows) if resolved_rows else '<tr><td colspan="7">Sem ordens resolvidas ainda.</td></tr>'}</tbody></table></div>
<script>
(function(){{
  const body = document.getElementById('resolvedBody');
  if(!body) return;
  const rows = Array.from(body.querySelectorAll('tr')).filter(r => r.querySelectorAll('td').length >= 7);
  rows.forEach((r, i) => {{
    const pnlCell = r.querySelector('td:last-child');
    const pnl = pnlCell ? Number((pnlCell.textContent || '0').replace(/[^0-9+.-]/g,'')) : 0;
    r.dataset.pnl = String(Number.isFinite(pnl) ? pnl : 0);
    r.dataset.order = String(i + 1);
  }});

  window.setResolvedFilter = function(kind){{
    rows.forEach(r => {{
      const show = (kind === 'all') || r.classList.contains(kind);
      r.style.display = show ? '' : 'none';
    }});
  }}

  window.sortResolved = function(mode){{
    const sorted = [...rows];
    if(mode === 'pnl_desc') sorted.sort((a,b)=> Number(b.dataset.pnl) - Number(a.dataset.pnl));
    else if(mode === 'pnl_asc') sorted.sort((a,b)=> Number(a.dataset.pnl) - Number(b.dataset.pnl));
    else sorted.sort((a,b)=> Number(a.dataset.order) - Number(b.dataset.order));
    sorted.forEach(r => body.appendChild(r));
  }}
}})();
</script>

<h3 style='margin:18px 0 8px'>Timeline de ciclos</h3>
<div class='tbl'>
<table><thead><tr>
<th>Timestamp</th><th>Mode</th><th>Status</th><th>Opened/Skipped</th><th>Actionable</th><th>Bankroll</th><th>PnL Total</th><th>Winrate(10)</th><th>Realized PnL(10)</th><th>Loop</th><th>Pause reason</th><th>Alerts</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</div></body></html>"""


def health_ok(url: str) -> bool:
    try:
        with urlopen(url, timeout=2) as r:
            return 200 <= int(getattr(r, 'status', 200)) < 300
    except Exception:
        return False


def cmd_precycle() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    wallet = load_json(WALLET_FILE, {})
    state = load_json(STATE_FILE, {})
    metrics = read_report_metrics()

    mode = choose_mode_from_history(wallet)
    interval = dynamic_interval(wallet, metrics)
    paused, reason, next_cool = circuit_breaker(wallet, state)

    alerts = []
    llm_ok = health_ok("http://127.0.0.1:8080/health")
    dash_ok = health_ok("http://127.0.0.1:8090/health")
    fail_streak = int(state.get("health_fail_streak", 0) or 0)
    if llm_ok and dash_ok:
        fail_streak = 0
    else:
        fail_streak += 1
    if fail_streak >= 3:
        alerts.append(f"health_falhando_{fail_streak}x")

    ACTIVE_STRATEGY_FILE.write_text(mode, encoding="utf-8")
    LOOP_SECONDS_FILE.write_text(str(interval), encoding="utf-8")

    out = {
        "ts": now_iso(),
        "strategy_mode": mode,
        "loop_interval_seconds": interval,
        "entries_paused": paused,
        "pause_reason": reason,
        "cooldown_cycles": next_cool,
        "health_fail_streak": fail_streak,
        "alerts": alerts,
    }
    save_json(STATE_FILE, out)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_postcycle() -> int:
    wallet = load_json(WALLET_FILE, {})
    state = load_json(STATE_FILE, {})
    metrics = read_report_metrics()

    timeline = load_json(TIMELINE_JSON, {"version": 1, "entries": []})
    entries = timeline.get("entries", [])
    entries.append(build_timeline_entry(wallet, metrics, state))
    timeline["updated_at"] = now_iso()
    timeline["entries"] = entries[-1500:]
    save_json(TIMELINE_JSON, timeline)
    TIMELINE_HTML.write_text(render_html(timeline["entries"], wallet), encoding="utf-8")
    print(json.dumps({"ok": True, "entries": len(timeline['entries'])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "precycle":
        raise SystemExit(cmd_precycle())
    if cmd == "postcycle":
        raise SystemExit(cmd_postcycle())
    print("usage: ops_runtime.py [precycle|postcycle]")
    raise SystemExit(2)
