import fcntl
import json
import os
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from common import KNOWN_STRATEGIES
from learning import default_learning_state, ensure_learning_state, learning_snapshot

HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("DASHBOARD_PORT", "8090"))
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
BASE = Path(__file__).resolve().parent
WALLET = BASE / 'wallet.json'
LOG = BASE / 'logs' / 'paper_runner.log'
LAST = BASE / 'logs' / 'last_report.txt'
LAST_JSON = BASE / 'logs' / 'last_report.json'
ACTIVE_STRATEGY_FILE = BASE / 'logs' / 'active_strategy.txt'
LOOP_SECONDS_FILE = BASE / 'logs' / 'loop_seconds.txt'
PAPER_LOOP_PID_FILE = BASE / 'logs' / 'paper_loop.pid'
RUNTIME_STATE_FILE = BASE / 'logs' / 'runtime_state.json'
TIMELINE_REPORT_FILE = BASE / 'logs' / 'timeline_report.json'

def _host_is_local(host: str) -> bool:
    return host.strip().lower() in {"127.0.0.1", "localhost", "::1"}


def _require_token_when_exposed() -> None:
    if not DASHBOARD_TOKEN and not _host_is_local(HOST):
        raise SystemExit(
            "DASHBOARD_TOKEN deve ser definido quando DASHBOARD_HOST nao for localhost/127.0.0.1"
        )


@contextmanager
def wallet_file_lock(exclusive: bool):
    WALLET.parent.mkdir(parents=True, exist_ok=True)
    lock_path = WALLET.with_suffix(WALLET.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def tail(path: Path, n=200):
    if not path.exists():
        return ''
    # Read only the file tail — the runner log grows to tens of MB and this
    # runs on every dashboard request.
    size = path.stat().st_size
    window = max(262144, n * 1024)
    with path.open('rb') as fh:
        fh.seek(max(0, size - window))
        text = fh.read().decode('utf-8', errors='ignore')
    lines = text.splitlines()
    if size > window:
        lines = lines[1:]
    return '\n'.join(lines[-n:])


def parse_equity_from_log(log_text: str, limit: int = 80):
    points = []
    current_ts = None
    for line in log_text.splitlines():
        s = line.strip()
        if '===== ' in s and s.endswith(' ====='):
            current_ts = s.replace('\\n', '').replace('===== ', '').replace(' =====', '').strip()
            continue
        m = re.search(r"Wallet:\*\* \$(\d+(?:\.\d+)?)", line)
        if m and current_ts:
            points.append({"ts": current_ts, "bankroll": float(m.group(1))})
    return points[-limit:]


def cycle_signal(report_text: str):
    opened = 0
    skipped = 0
    actionable = 0

    m1 = re.search(r"Trades:\*\*\s*(\d+) opened,\s*(\d+) skipped", report_text)
    if m1:
        opened = int(m1.group(1))
        skipped = int(m1.group(2))

    m2 = re.search(r"signals \((\d+) actionable\)", report_text)
    if m2:
        actionable = int(m2.group(1))

    if opened > 0:
        return {"level": "bom", "reason": f"{opened} trade(s) aberto(s)"}
    if actionable > 0 and skipped > 0:
        return {"level": "neutro", "reason": f"{actionable} sinais, mas {skipped} pulados"}
    return {"level": "ruim", "reason": "sem execução no ciclo"}


def cycle_signal_from_json(report: dict):
    execution = report.get('execution', {}) if isinstance(report.get('execution'), dict) else {}
    scan = report.get('scan', {}) if isinstance(report.get('scan'), dict) else {}
    opened = int(execution.get('executed', 0) or 0)
    skipped = int(execution.get('skipped', 0) or 0)
    actionable = int(scan.get('actionable_signals', 0) or 0)
    if opened > 0:
        return {"level": "bom", "reason": f"{opened} trade(s) aberto(s)"}
    if actionable > 0 and skipped > 0:
        return {"level": "neutro", "reason": f"{actionable} sinais, mas {skipped} pulados"}
    return {"level": "ruim", "reason": "sem execução no ciclo"}


def parse_strategy_mode(report_text: str) -> str:
    m = re.search(r"\|\s*mode=([a-zA-Z0-9_,\-]+)", report_text)
    return m.group(1).strip() if m else "all"


def _normalize_strategy_selection(raw: str):
    value = (raw or '').strip().lower()
    if not value:
        return ["btc_5m_momentum"]
    if value == 'all':
        return KNOWN_STRATEGIES.copy()
    parts = [p.strip() for p in value.replace(';', ',').split(',') if p.strip()]
    selected = [p for p in parts if p in KNOWN_STRATEGIES]
    # remove duplicates preserving order
    out = []
    seen = set()
    for s in selected:
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out or ["btc_5m_momentum"]


def read_configured_strategy() -> str:
    try:
        if ACTIVE_STRATEGY_FILE.exists():
            value = ACTIVE_STRATEGY_FILE.read_text(errors='ignore').strip().lower()
            selected = _normalize_strategy_selection(value)
            if len(selected) == len(KNOWN_STRATEGIES):
                return 'all'
            return ','.join(selected)
    except Exception:
        pass
    return "btc_5m_momentum"


def write_configured_strategy(mode: str) -> None:
    ACTIVE_STRATEGY_FILE.parent.mkdir(parents=True, exist_ok=True)
    selected = _normalize_strategy_selection(mode)
    ACTIVE_STRATEGY_FILE.write_text('all' if len(selected) == len(KNOWN_STRATEGIES) else ','.join(selected))


def read_loop_seconds(default: int = 90) -> int:
    try:
        if LOOP_SECONDS_FILE.exists():
            v = int(LOOP_SECONDS_FILE.read_text(errors='ignore').strip())
            if 30 <= v <= 3600:
                return v
    except Exception:
        pass
    return default


def write_loop_seconds(v: int) -> None:
    LOOP_SECONDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOOP_SECONDS_FILE.write_text(str(v))


def is_paper_loop_running() -> bool:
    try:
        if not PAPER_LOOP_PID_FILE.exists():
            return False
        pid = int(PAPER_LOOP_PID_FILE.read_text(errors='ignore').strip())
        if pid <= 1:
            return False
        cmdline = Path(f"/proc/{pid}/cmdline")
        if not cmdline.exists():
            return False
        cmd = cmdline.read_bytes().replace(b'\x00', b' ').decode('utf-8', errors='ignore')
        return 'paper_loop.sh' in cmd and str(BASE) in cmd
    except Exception:
        return False


def read_wallet_state() -> dict:
    if not WALLET.exists():
        return {}
    try:
        with wallet_file_lock(exclusive=False):
            return json.loads(WALLET.read_text(errors='ignore'))
    except Exception:
        return {}


def read_last_report_json() -> dict:
    try:
        if LAST_JSON.exists():
            data = json.loads(LAST_JSON.read_text(errors='ignore'))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def write_wallet_state(state: dict) -> None:
    WALLET.parent.mkdir(parents=True, exist_ok=True)
    with wallet_file_lock(exclusive=True):
        tmp = WALLET.with_suffix(f'.json.{os.getpid()}.{threading.get_ident()}.tmp')
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))
        tmp.replace(WALLET)


def read_runtime_state() -> dict:
    try:
        if RUNTIME_STATE_FILE.exists():
            return json.loads(RUNTIME_STATE_FILE.read_text(errors='ignore'))
    except Exception:
        pass
    return {}


def read_timeline_tail(limit: int = 40) -> list:
    try:
        if TIMELINE_REPORT_FILE.exists():
            data = json.loads(TIMELINE_REPORT_FILE.read_text(errors='ignore'))
            entries = data.get('entries', []) if isinstance(data, dict) else []
            return entries[-limit:]
    except Exception:
        pass
    return []


def wallet_controls_from_state(wallet: dict) -> dict:
    settings = wallet.get('settings', {}) if isinstance(wallet, dict) else {}
    return {
        'auto_risk_enabled': bool(settings.get('auto_risk_enabled', True)),
        'max_exposure': settings.get('max_exposure'),
        'max_per_scan': settings.get('max_per_scan'),
        'max_trade': settings.get('max_trade'),
        'min_trade': settings.get('min_trade'),
        'min_edge': settings.get('min_edge'),
        'llm_enabled': bool(settings.get('llm_enabled', False)),
        'llm_mode': settings.get('llm_mode', 'fast'),
        'llm_url': settings.get('llm_url', 'http://127.0.0.1:8080/v1/chat/completions'),
        'stop_loss': settings.get('stop_loss'),
        'take_profit': settings.get('take_profit'),
        'cb_loss_seq': settings.get('cb_loss_seq', 4),
        'cb_loss_sum6': settings.get('cb_loss_sum6', -25.0),
        'cb_cooldown_cycles': settings.get('cb_cooldown_cycles', 2),
        'rot_window': settings.get('rot_window', 80),
        'rot_top_k': settings.get('rot_top_k', 4),
        'rot_weight_winrate': settings.get('rot_weight_winrate', 0.7),
        'rot_weight_pnl': settings.get('rot_weight_pnl', 0.3),
        'min_net_edge': settings.get('min_net_edge', 0.035),
        'taker_fee_estimate': settings.get('taker_fee_estimate', 0.001),
        'slippage_estimate': settings.get('slippage_estimate', 0.01),
        'smart_money_max_spread': settings.get('smart_money_max_spread', 0.03),
        'smart_money_min_liquidity': settings.get('smart_money_min_liquidity', 15000),
        'smart_money_min_vol24h': settings.get('smart_money_min_vol24h', 50000),
        'event_countdown_max_spread': settings.get('event_countdown_max_spread', 0.06),
        'event_countdown_min_liquidity': settings.get('event_countdown_min_liquidity', 15000),
        'event_countdown_min_vol24h': settings.get('event_countdown_min_vol24h', 25000),
        'btc_max_entry_price': settings.get('btc_max_entry_price', 0.82),
        'btc_min_liquidity': settings.get('btc_min_liquidity', 1000),
        'endgame_max_entry_price': settings.get('endgame_max_entry_price', 0.95),
        'endgame_min_liquidity': settings.get('endgame_min_liquidity', 1500),
        'shadow_strategies': settings.get('shadow_strategies', 'arbitrage,value,mean_reversion,volume_spike'),
    }


def learning_controls_from_state(wallet: dict) -> dict:
    if not isinstance(wallet, dict):
        return {}
    ls = ensure_learning_state(wallet)
    return learning_snapshot(ls)


def run_control_script(action: str):
    if action == 'start':
        cmd = 'cd "{base}" && ./start_all.sh'.format(base=str(BASE))
    elif action == 'stop':
        cmd = 'cd "{base}" && ./stop_all.sh'.format(base=str(BASE))
    else:
        raise ValueError('invalid action')
    return subprocess.run(['bash', '-lc', cmd], capture_output=True, text=True, timeout=120)


def trigger_restart_async() -> None:
    # Reinício assíncrono para evitar derrubar a própria conexão HTTP antes de responder.
    cmd = (
        'cd "{base}" && '
        '(./stop_all.sh; sleep 1; ./start_all.sh) '
        '> logs/restart_from_dashboard.log 2>&1 &'
    ).format(base=str(BASE))
    subprocess.Popen(['bash', '-lc', cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_one_cycle_async() -> None:
    def _job():
        mode = read_configured_strategy()
        pybin = str(BASE / '.venv' / 'bin' / 'python')
        if not Path(pybin).exists():
            pybin = 'python3'
        cmd = f'cd "{BASE}" && PAPER_STRATEGY_MODE="{mode}" "{pybin}" settlement.py full'
        started = f"\n===== {datetime.now(timezone.utc).isoformat()} =====\n[manual] cycle trigger via dashboard"
        with LOG.open('a', encoding='utf-8') as f:
            f.write(started + "\n")
        p = subprocess.run(['bash', '-lc', cmd], capture_output=True, text=True, timeout=240)
        out = (p.stdout or '') + (('\n' + p.stderr) if p.stderr else '')
        if out.strip():
            with LOG.open('a', encoding='utf-8') as f:
                f.write(out.rstrip() + "\n")
            LAST.write_text(out)

    threading.Thread(target=_job, daemon=True).start()


def parse_strategy_counts(report_text: str):
    # Exemplo: "arbitrage: 17 | smart_money: 12 | mean_reversion: 5"
    counts = {}
    allowed = set(KNOWN_STRATEGIES)
    for name, val in re.findall(r"([a-zA-Z_]+):\s*(\d+)", report_text):
        if name in allowed:
            counts[name] = int(val)
    return counts


def current_strategies(report_text: str, report_json: dict | None = None):
    scan = report_json.get('scan', {}) if isinstance(report_json, dict) and isinstance(report_json.get('scan'), dict) else {}
    mode_from_report = str(scan.get('strategy_mode') or parse_strategy_mode(report_text))
    configured_mode = read_configured_strategy()
    by_strategy = scan.get('by_strategy') if isinstance(scan.get('by_strategy'), dict) else parse_strategy_counts(report_text)
    available = KNOWN_STRATEGIES.copy()
    selected = _normalize_strategy_selection(configured_mode)
    active = selected
    mode_label = 'all' if len(selected) == len(available) else ','.join(selected)
    return {
        "mode": mode_label,
        "mode_from_report": mode_from_report,
        "available": available,
        "active": active,
        "last_scan_counts": by_strategy,
    }


def fetch_google_news(query: str = "Polymarket OR prediction market OR crypto", limit: int = 8):
    try:
        q = quote_plus(query)
        url = f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        news = []
        for item in root.findall("./channel/item")[:limit]:
            src = item.find("source")
            news.append({
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
                "source": (src.text.strip() if src is not None and src.text else ""),
            })
        return {"ok": True, "error": "", "query": query, "items": news}
    except Exception as e:
        return {"ok": False, "error": str(e), "query": query, "items": []}


class H(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        if not DASHBOARD_TOKEN:
            return True
        header_token = self.headers.get('X-Dashboard-Token', '').strip()
        query_token = ''
        if '?' in self.path:
            query = self.path.split('?', 1)[1]
            for part in query.split('&'):
                k, _, v = part.partition('=')
                if k == 'token':
                    query_token = v.strip()
                    break
        return header_token == DASHBOARD_TOKEN or query_token == DASHBOARD_TOKEN

    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # POST endpoint handlers — each returns (http_status, response_dict)
    # ------------------------------------------------------------------

    def _handle_strategy(self, data):
        mode = str(data.get('mode', '')).strip().lower()
        modes = data.get('modes')
        selected = []
        if isinstance(modes, list):
            selected = [str(x).strip().lower() for x in modes if str(x).strip()]
        if mode == 'all':
            write_configured_strategy('all')
            return 200, {"ok": True, "mode": "all", "active": KNOWN_STRATEGIES}
        if selected:
            invalid = [m for m in selected if m not in KNOWN_STRATEGIES]
            if invalid:
                return 400, {"ok": False, "error": "invalid modes", "invalid": invalid, "allowed": KNOWN_STRATEGIES + ["all"]}
            write_configured_strategy(','.join(selected))
            active = _normalize_strategy_selection(','.join(selected))
            return 200, {"ok": True, "mode": ','.join(active), "active": active}
        if mode not in KNOWN_STRATEGIES:
            return 400, {"ok": False, "error": "invalid mode", "allowed": KNOWN_STRATEGIES + ["all"]}
        write_configured_strategy(mode)
        return 200, {"ok": True, "mode": mode, "active": [mode]}

    def _handle_loop_interval(self, data):
        sec = int(data.get('interval_seconds', 0))
        if sec < 30 or sec > 3600:
            return 400, {"ok": False, "error": "interval_seconds fora do range", "min": 30, "max": 3600}
        write_loop_seconds(sec)
        return 200, {"ok": True, "interval_seconds": sec}

    def _handle_control(self, data):
        action = str(data.get('action', '')).strip().lower()
        if action == 'run_once':
            run_one_cycle_async()
            return 200, {"ok": True, "action": action, "message": "ciclo manual disparado"}
        if action == 'restart':
            trigger_restart_async()
            return 200, {"ok": True, "action": action, "message": "reinício disparado em background; dashboard pode ficar indisponível por alguns segundos"}
        if action not in {'start', 'stop'}:
            return 400, {"ok": False, "error": "invalid action", "allowed": ["start", "stop", "restart", "run_once"]}
        p = run_control_script(action)
        status = 200 if p.returncode == 0 else 500
        return status, {"ok": p.returncode == 0, "action": action, "exit_code": p.returncode, "stdout": (p.stdout or '')[-2000:], "stderr": (p.stderr or '')[-2000:]}

    def _handle_settings(self, data):
        wallet = read_wallet_state()
        if not wallet:
            return 500, {"ok": False, "error": "wallet indisponivel"}
        settings = wallet.get('settings', {})
        settings['auto_risk_enabled'] = bool(data.get('auto_risk_enabled', settings.get('auto_risk_enabled', True)))

        def _num(name, cast=float, min_v=None, max_v=None):
            if name not in data:
                return
            v = cast(data.get(name))
            if min_v is not None and v < min_v:
                raise ValueError(f'{name} abaixo do mínimo {min_v}')
            if max_v is not None and v > max_v:
                raise ValueError(f'{name} acima do máximo {max_v}')
            settings[name] = v

        _num('max_exposure', float, 1, 1000000)
        _num('max_trade', float, 0.5, 1000000)
        _num('min_trade', float, 0.1, 1000000)
        _num('max_per_scan', int, 1, 100)
        _num('min_edge', float, 0.0, 1.0)
        if 'llm_enabled' in data:
            settings['llm_enabled'] = bool(data.get('llm_enabled'))
        if 'llm_mode' in data:
            llm_mode = str(data.get('llm_mode', 'fast')).strip().lower()
            if llm_mode not in {'fast', 'balanced', 'strong'}:
                return 400, {"ok": False, "error": "llm_mode invalido (fast|balanced|strong)"}
            settings['llm_mode'] = llm_mode
        if 'llm_url' in data:
            llm_url = str(data.get('llm_url', '')).strip()
            if not llm_url.startswith(('http://', 'https://')):
                return 400, {"ok": False, "error": "llm_url deve iniciar com http:// ou https://"}
            settings['llm_url'] = llm_url
        _num('stop_loss', float, 0.01, 0.99)
        _num('take_profit', float, 0.01, 5.0)
        _num('cb_loss_seq', int, 2, 12)
        _num('cb_loss_sum6', float, -500.0, -1.0)
        _num('cb_cooldown_cycles', int, 1, 20)
        _num('rot_window', int, 20, 400)
        _num('rot_top_k', int, 1, len(KNOWN_STRATEGIES))
        _num('rot_weight_winrate', float, 0.0, 1.0)
        _num('rot_weight_pnl', float, 0.0, 1.0)
        _num('min_net_edge', float, 0.0, 1.0)
        _num('taker_fee_estimate', float, 0.0, 0.20)
        _num('slippage_estimate', float, 0.0, 0.50)
        _num('smart_money_max_spread', float, 0.0, 1.0)
        _num('smart_money_min_liquidity', float, 0.0, 10000000)
        _num('smart_money_min_vol24h', float, 0.0, 10000000)
        _num('event_countdown_max_spread', float, 0.0, 1.0)
        _num('event_countdown_min_liquidity', float, 0.0, 10000000)
        _num('event_countdown_min_vol24h', float, 0.0, 10000000)
        _num('btc_max_entry_price', float, 0.01, 0.99)
        _num('btc_min_liquidity', float, 0.0, 10000000)
        _num('endgame_max_entry_price', float, 0.01, 0.99)
        _num('endgame_min_liquidity', float, 0.0, 10000000)
        if 'shadow_strategies' in data:
            raw_shadow = str(data.get('shadow_strategies', '')).strip().lower()
            shadow = [x.strip() for x in raw_shadow.replace(';', ',').split(',') if x.strip()]
            invalid_shadow = [x for x in shadow if x not in KNOWN_STRATEGIES]
            if invalid_shadow:
                return 400, {"ok": False, "error": "shadow_strategies invalido", "invalid": invalid_shadow, "allowed": KNOWN_STRATEGIES}
            settings['shadow_strategies'] = ','.join(dict.fromkeys(shadow))

        if settings.get('min_trade', 0) > settings.get('max_trade', 0):
            return 400, {"ok": False, "error": "min_trade nao pode ser maior que max_trade"}
        if settings.get('max_trade', 0) > settings.get('max_exposure', 0):
            return 400, {"ok": False, "error": "max_trade nao pode ser maior que max_exposure"}
        rw = float(settings.get('rot_weight_winrate', 0.7) or 0.7)
        rp = float(settings.get('rot_weight_pnl', 0.3) or 0.3)
        if abs((rw + rp) - 1.0) > 0.001:
            return 400, {"ok": False, "error": "rot_weight_winrate + rot_weight_pnl deve somar 1.0"}

        wallet['settings'] = settings
        write_wallet_state(wallet)
        return 200, {'ok': True, 'settings': wallet_controls_from_state(wallet)}

    def _handle_settings_preset(self, data):
        wallet = read_wallet_state()
        if not wallet:
            return 500, {"ok": False, "error": "wallet indisponivel"}
        preset = str(data.get('preset', '')).strip().lower()
        presets = {
            'conservador': {'cb_loss_seq': 3, 'cb_loss_sum6': -15.0, 'cb_cooldown_cycles': 4, 'rot_window': 140, 'rot_top_k': 2, 'rot_weight_winrate': 0.8, 'rot_weight_pnl': 0.2, 'min_edge': 0.08, 'max_per_scan': 2},
            'balanceado':  {'cb_loss_seq': 4, 'cb_loss_sum6': -25.0, 'cb_cooldown_cycles': 2, 'rot_window': 100, 'rot_top_k': 3, 'rot_weight_winrate': 0.7, 'rot_weight_pnl': 0.3, 'min_edge': 0.06, 'max_per_scan': 3},
            'agressivo':   {'cb_loss_seq': 6, 'cb_loss_sum6': -45.0, 'cb_cooldown_cycles': 1, 'rot_window': 60,  'rot_top_k': 5, 'rot_weight_winrate': 0.55, 'rot_weight_pnl': 0.45, 'min_edge': 0.04, 'max_per_scan': 5},
        }
        if preset not in presets:
            return 400, {'ok': False, 'error': 'preset inválido', 'allowed': list(presets.keys())}
        settings = wallet.get('settings', {})
        settings.update(presets[preset])
        wallet['settings'] = settings
        write_wallet_state(wallet)
        return 200, {'ok': True, 'preset': preset, 'settings': wallet_controls_from_state(wallet)}

    def _handle_learning_settings(self, data):
        wallet = read_wallet_state()
        if not wallet:
            return 500, {"ok": False, "error": "wallet indisponivel"}
        ls = ensure_learning_state(wallet)
        if 'enabled' in data:
            ls['enabled'] = bool(data.get('enabled'))
        if 'shadow_mode' in data:
            ls['shadow_mode'] = bool(data.get('shadow_mode'))
        if 'min_samples' in data:
            v = int(data.get('min_samples'))
            if v < 5 or v > 500:
                return 400, {"ok": False, "error": "min_samples fora do range [5,500]"}
            ls['min_samples'] = v
        if 'aggressiveness' in data:
            a = str(data.get('aggressiveness', 'medium')).strip().lower()
            if a not in {'low', 'medium', 'high'}:
                return 400, {"ok": False, "error": "aggressiveness invalido (low|medium|high)"}
            ls['aggressiveness'] = a
        wallet['learning_state'] = ls
        write_wallet_state(wallet)
        return 200, {'ok': True, 'learning': learning_controls_from_state(wallet)}

    def _handle_learning_reset(self, data):
        wallet = read_wallet_state()
        if not wallet:
            return 500, {"ok": False, "error": "wallet indisponivel"}
        prev = ensure_learning_state(wallet)
        keep_cfg = {
            'enabled': bool(prev.get('enabled', True)),
            'shadow_mode': bool(prev.get('shadow_mode', False)),
            'min_samples': int(prev.get('min_samples', 20)),
            'aggressiveness': str(prev.get('aggressiveness', 'medium')),
        }
        fresh = default_learning_state()
        fresh.update(keep_cfg)
        wallet['learning_state'] = fresh
        write_wallet_state(wallet)
        return 200, {'ok': True, 'learning': learning_controls_from_state(wallet)}

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    _POST_ROUTES = {
        '/api/strategy': '_handle_strategy',
        '/api/loop_interval': '_handle_loop_interval',
        '/api/control': '_handle_control',
        '/api/settings': '_handle_settings',
        '/api/settings_preset': '_handle_settings_preset',
        '/api/learning_settings': '_handle_learning_settings',
        '/api/learning_reset': '_handle_learning_reset',
    }

    def do_POST(self):
        if not self._authorized():
            self._send(401, 'application/json; charset=utf-8', b'{"ok":false,"error":"unauthorized"}')
            return
        try:
            path_only = self.path.split('?', 1)[0]
            length = int(self.headers.get('Content-Length', '0') or '0')
            raw = self.rfile.read(length) if length > 0 else b'{}'
            data = json.loads(raw.decode('utf-8') or '{}')

            handler_name = self._POST_ROUTES.get(path_only)
            if handler_name is None:
                self._send(404, 'application/json; charset=utf-8', b'{"error":"not found"}')
                return

            status, result = getattr(self, handler_name)(data)
            self._send(status, 'application/json; charset=utf-8', json.dumps(result).encode('utf-8'))
        except Exception as e:
            payload = json.dumps({"ok": False, "error": str(e)}).encode('utf-8')
            self._send(500, 'application/json; charset=utf-8', payload)

    def do_GET(self):
        if self.path == '/health':
            self._send(200, 'application/json', b'{"ok":true}')
            return

        path_only = self.path.split('?', 1)[0]

        if not self._authorized():
            self._send(401, 'application/json; charset=utf-8', b'{"ok":false,"error":"unauthorized"}')
            return

        if path_only == '/api/status':
            wallet = read_wallet_state()
            if not wallet:
                wallet = {"error": "wallet parse failed"}

            report = LAST.read_text(errors='ignore') if LAST.exists() else ''
            report_json = read_last_report_json()
            log_full = LOG.read_text(errors='ignore') if LOG.exists() else ''
            runtime_state = read_runtime_state()
            timeline_tail = read_timeline_tail(40)
            news_payload = fetch_google_news()
            payload = {
                'now': datetime.now(timezone.utc).isoformat(),
                'wallet': wallet,
                'settings_controls': wallet_controls_from_state(wallet),
                'learning_controls': learning_controls_from_state(wallet),
                'last_report': report,
                'last_report_json': report_json,
                'log_tail': tail(LOG, 200),
                'equity': parse_equity_from_log(log_full),
                'cycle_signal': cycle_signal_from_json(report_json) if report_json else cycle_signal(report),
                'strategies': current_strategies(report, report_json),
                'controls': {
                    'loop_interval_seconds': read_loop_seconds(90),
                    'paper_loop_running': is_paper_loop_running(),
                },
                'runtime_state': runtime_state,
                'timeline_tail': timeline_tail,
                'news': news_payload.get('items', []),
                'news_status': {
                    'ok': bool(news_payload.get('ok')),
                    'error': news_payload.get('error', ''),
                    'query': news_payload.get('query', ''),
                },
            }
            self._send(200, 'application/json; charset=utf-8', json.dumps(payload).encode('utf-8'))
            return

        html = """<!doctype html>
<html lang='pt-BR'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Polymarket Paper Trader Dashboard</title>
<style>
:root{--bg:#0b1220;--panel:#111827;--panel2:#0f172a;--txt:#e5e7eb;--muted:#93a4bd;--line:#253246;--good:#22c55e;--warn:#f59e0b;--bad:#ef4444;--blue:#38bdf8}
*{box-sizing:border-box} body{margin:0;background:radial-gradient(1200px 600px at 10% -10%,#1e293b 0,#0b1220 45%);color:var(--txt);font-family:Inter,Segoe UI,Arial,sans-serif;overflow-x:hidden}
.wrap{max-width:1180px;margin:0 auto;padding:10px}.top{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.h1{font-size:22px;font-weight:800}.badge{font-size:12px;padding:5px 9px;border-radius:999px;border:1px solid var(--line);color:var(--muted);background:rgba(255,255,255,.02)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:12px;padding:10px;min-width:0}
.kpi-title{font-size:11px;color:var(--muted);margin-bottom:4px}.kpi-value{font-size:22px;font-weight:800}.kpi-sub{font-size:11px;color:var(--muted);margin-top:3px}
.bar{height:10px;background:#0a1020;border:1px solid var(--line);border-radius:999px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,var(--blue),#60a5fa)}
.section{margin-top:8px}.section h3{margin:0 0 8px 0;font-size:14px;color:#cbd5e1}.columns{display:grid;grid-template-columns:1.3fr .9fr;gap:8px}
.columns-2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px}
.table{width:100%;border-collapse:collapse;font-size:13px}.table th,.table td{padding:8px;border-bottom:1px solid var(--line);text-align:left}.table th{color:#9fb1ca;font-weight:600}
.tag{padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line);display:inline-block}.yes{background:rgba(34,197,94,.12);color:#86efac}.no{background:rgba(239,68,68,.12);color:#fca5a5}
.sem{display:inline-block;padding:6px 10px;border-radius:999px;font-weight:700;font-size:12px}.sem.bom{background:rgba(34,197,94,.15);color:#86efac;border:1px solid rgba(34,197,94,.35)}.sem.neutro{background:rgba(245,158,11,.15);color:#fcd34d;border:1px solid rgba(245,158,11,.35)}.sem.ruim{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.35)}
.strat-wrap{display:flex;flex-wrap:wrap;gap:6px}.strat-btn{padding:6px 10px;border-radius:999px;border:1px solid var(--line);background:#0b1324;color:#cbd5e1;font-size:12px;font-weight:600;cursor:pointer}
.strat-btn.active{background:rgba(34,197,94,.18);border-color:rgba(34,197,94,.35);color:#86efac}
.strat-btn.idle{background:rgba(148,163,184,.08);color:#cbd5e1}
.strat-count{opacity:.85;font-weight:500}
.fieldgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px}
.field{display:flex;flex-direction:column;gap:4px}
.field label{font-size:12px;color:var(--muted)}
.field input,.field select{background:#0b1324;color:#cbd5e1;border:1px solid #253246;border-radius:8px;padding:6px;min-width:0;width:100%}
.field.wide{grid-column:span 2}
.actions{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px}
details.card summary{cursor:pointer;font-size:14px;font-weight:700;color:#cbd5e1;list-style:none}
details.card summary::-webkit-details-marker{display:none}
details.card summary:after{content:'abrir';float:right;color:var(--muted);font-size:11px;font-weight:600}
details.card[open] summary:after{content:'fechar'}
.compact-scroll{max-height:260px;overflow:auto}
.timeline{position:relative;padding-left:18px}.timeline:before{content:'';position:absolute;left:7px;top:0;bottom:0;width:2px;background:var(--line)}
.t-item{position:relative;background:#0d1422;border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin-bottom:8px}.t-item:before{content:'';position:absolute;left:-16px;top:12px;width:10px;height:10px;border-radius:50%;background:var(--blue)}
.t-item.pnl-pos{border-color:rgba(34,197,94,.35);background:rgba(34,197,94,.08)}
.t-item.pnl-pos:before{background:#22c55e}
.t-item.pnl-neg{border-color:rgba(239,68,68,.35);background:rgba(239,68,68,.09)}
.t-item.pnl-neg:before{background:#ef4444}
.t-item.pnl-zero{border-color:rgba(148,163,184,.35);background:rgba(148,163,184,.08)}
.t-item.pnl-zero:before{background:#94a3b8}
pre{white-space:pre-wrap;word-break:break-word;background:#070d1a;border:1px solid var(--line);border-radius:10px;padding:10px;max-height:34vh;overflow:auto;font-size:12px}
canvas{width:100%;height:190px;background:#070d1a;border:1px solid var(--line);border-radius:10px}.muted{color:var(--muted)}
@media (max-width:960px){.grid{grid-template-columns:1fr 1fr}.columns,.columns-2{grid-template-columns:1fr}.fieldgrid{grid-template-columns:1fr 1fr}.field.wide{grid-column:span 1}} @media (max-width:560px){.grid,.fieldgrid{grid-template-columns:1fr}.wrap{padding:8px}.h1{font-size:19px}}
</style>
</head>
<body><div class='wrap'>
<div class='top'><div class='h1'>Polymarket Paper Trader Dashboard</div><div class='badge' id='now'>atualizando...</div></div>
<div class='grid'>
  <div class='card'><div class='kpi-title'>Bankroll atual</div><div id='bankroll' class='kpi-value'>$0</div><div id='bankrollSub' class='kpi-sub'>vs inicial</div></div>
  <div class='card'><div class='kpi-title'>Lucro/Prejuízo (P&L)</div><div id='pnl' class='kpi-value'>$0</div><div id='pnlSub' class='kpi-sub'>paper trading</div></div>
  <div class='card'><div class='kpi-title'>Posições abertas</div><div id='openCount' class='kpi-value'>0</div><div class='kpi-sub'>exposição ativa</div></div>
  <div class='card'><div class='kpi-title'>Semáforo do ciclo</div><div id='cycleSem' class='sem neutro'>NEUTRO</div><div id='cycleReason' class='kpi-sub'>-</div></div>
</div>
<div class='section card'><h3>Uso de risco</h3><div class='muted' id='riskText'>-</div><div class='bar' style='margin-top:8px'><div id='riskFill' class='fill' style='width:0%'></div></div></div>
<div class='section columns-2'>
  <div class='card'><h3>Curva de equity (bankroll ao longo do tempo)</h3><canvas id='equity' width='1100' height='260'></canvas></div>
  <div class='card'><h3>Posições por estratégia</h3><canvas id='stratChart' width='1100' height='260'></canvas></div>
</div>
<div class='section card'><h3>Estratégias</h3><div id='strategies' class='strat-wrap'><span class='muted'>Carregando estratégias...</span></div></div>
<div class='section card'>
  <h3>Controles do bot</h3>
  <div class='strat-wrap'>
    <button class='strat-btn' onclick='controlBot("start")'>Start</button>
    <button class='strat-btn' onclick='controlBot("stop")'>Stop</button>
    <button class='strat-btn' onclick='controlBot("restart")'>Restart</button>
    <button class='strat-btn' onclick='controlBot("run_once")'>Rodar ciclo agora</button>
    <span class='muted'>Intervalo (s):</span>
    <input id='loopSeconds' type='number' min='30' max='3600' step='1' style='width:100px;background:#0b1324;color:#cbd5e1;border:1px solid #253246;border-radius:8px;padding:6px'>
    <button class='strat-btn' onclick='saveLoopInterval()'>Salvar intervalo</button>
    <span id='botCtrlStatus' class='muted'></span>
  </div>
</div>
<div class='section columns-2'>
  <div class='card'>
    <h3>LLM / Rerank</h3>
    <div class='fieldgrid'>
      <div class='field'><label>Habilitar LLM</label><label class='muted'><input id='sLlmEnabled' type='checkbox'> usar rerank local</label></div>
      <div class='field'><label>Modo</label><select id='sLlmMode'><option value='fast'>fast</option><option value='balanced'>balanced</option><option value='strong'>strong</option></select></div>
      <div class='field wide'><label>URL</label><input id='sLlmUrl' type='text'></div>
    </div>
    <div class='actions'>
      <button class='strat-btn active' onclick='saveLlmSettings()'>Salvar LLM</button>
      <span id='llmStatus' class='muted'></span>
    </div>
    <div id='llmSummary' class='muted' style='margin-top:8px'>fast=0.5B, balanced=1.5B, strong=Qwen3 4B.</div>
  </div>
  <div class='card'>
    <h3>Risco e sizing</h3>
    <div class='fieldgrid'>
      <div class='field'><label>Auto-risk</label><label class='muted'><input id='autoRiskEnabled' type='checkbox'> ajustar pelo bankroll</label></div>
      <div class='field'><label>max_exposure</label><input id='sMaxExposure' type='number' min='1' step='0.1'></div>
      <div class='field'><label>max_trade</label><input id='sMaxTrade' type='number' min='0.1' step='0.1'></div>
      <div class='field'><label>min_trade</label><input id='sMinTrade' type='number' min='0.1' step='0.1'></div>
      <div class='field'><label>max_per_scan</label><input id='sMaxPerScan' type='number' min='1' max='100' step='1'></div>
      <div class='field'><label>min_edge</label><input id='sMinEdge' type='number' min='0' max='1' step='0.01'></div>
      <div class='field'><label>stop_loss</label><input id='sStopLoss' type='number' min='0.01' max='0.99' step='0.01'></div>
      <div class='field'><label>take_profit</label><input id='sTakeProfit' type='number' min='0.01' max='5' step='0.01'></div>
    </div>
  </div>
</div>
<details class='section card'>
  <summary>Política de execução</summary>
  <div class='fieldgrid'>
    <div class='field'><label>min_net_edge</label><input id='sMinNetEdge' type='number' min='0' max='1' step='0.005'></div>
    <div class='field'><label>fee_est</label><input id='sTakerFeeEstimate' type='number' min='0' max='0.2' step='0.001'></div>
    <div class='field'><label>slippage_est</label><input id='sSlippageEstimate' type='number' min='0' max='0.5' step='0.005'></div>
    <div class='field'><label>shadow strategies</label><input id='sShadowStrategies' type='text' placeholder='arbitrage,value,mean_reversion,volume_spike'></div>
    <div class='field'><label>smart spread</label><input id='sSmartMoneyMaxSpread' type='number' min='0' max='1' step='0.005'></div>
    <div class='field'><label>smart liq</label><input id='sSmartMoneyMinLiquidity' type='number' min='0' step='100'></div>
    <div class='field'><label>smart vol24h</label><input id='sSmartMoneyMinVol24h' type='number' min='0' step='100'></div>
    <div class='field'><label>countdown spread</label><input id='sEventCountdownMaxSpread' type='number' min='0' max='1' step='0.005'></div>
    <div class='field'><label>countdown liq</label><input id='sEventCountdownMinLiquidity' type='number' min='0' step='100'></div>
    <div class='field'><label>countdown vol24h</label><input id='sEventCountdownMinVol24h' type='number' min='0' step='100'></div>
    <div class='field'><label>btc max price</label><input id='sBtcMaxEntryPrice' type='number' min='0.01' max='0.99' step='0.01'></div>
    <div class='field'><label>btc liq</label><input id='sBtcMinLiquidity' type='number' min='0' step='100'></div>
    <div class='field'><label>endgame max price</label><input id='sEndgameMaxEntryPrice' type='number' min='0.01' max='0.99' step='0.01'></div>
    <div class='field'><label>endgame liq</label><input id='sEndgameMinLiquidity' type='number' min='0' step='100'></div>
  </div>
  <div class='actions'>
    <button class='strat-btn' onclick='applySettingsPreset("conservador")'>Preset conservador</button>
    <button class='strat-btn' onclick='applySettingsPreset("balanceado")'>Preset balanceado</button>
    <button class='strat-btn' onclick='applySettingsPreset("agressivo")'>Preset agressivo</button>
    <button class='strat-btn active' onclick='saveSettings()'>Salvar ajustes</button>
    <span id='presetActive' class='badge'>Preset ativo: custom</span>
    <span id='settingsStatus' class='muted'></span>
  </div>
</details>
<details class='section card'>
  <summary>Circuit breaker e rotação</summary>
  <div class='fieldgrid'>
    <div class='field'><label>cb_loss_seq</label><input id='sCbLossSeq' type='number' min='2' max='12' step='1'></div>
    <div class='field'><label>cb_loss_sum6</label><input id='sCbLossSum6' type='number' min='-500' max='-1' step='0.5'></div>
    <div class='field'><label>cb_cooldown_cycles</label><input id='sCbCooldownCycles' type='number' min='1' max='20' step='1'></div>
    <div class='field'><label>rot_window</label><input id='sRotWindow' type='number' min='20' max='400' step='1'></div>
    <div class='field'><label>rot_top_k</label><input id='sRotTopK' type='number' min='1' max='8' step='1'></div>
    <div class='field'><label>rot_weight_winrate</label><input id='sRotWeightWinrate' type='number' min='0' max='1' step='0.05'></div>
    <div class='field'><label>rot_weight_pnl</label><input id='sRotWeightPnl' type='number' min='0' max='1' step='0.05'></div>
  </div>
</details>
<div class='section card'>
  <h3>Aprendizado (auto-filtro)</h3>
  <div class='strat-wrap'>
    <label class='muted'><input id='learnEnabled' type='checkbox'> Learning habilitado</label>
    <label class='muted'><input id='learnShadow' type='checkbox'> Shadow mode (não aplica no filtro)</label>
    <span class='muted'>min_samples</span><input id='learnMinSamples' type='number' min='5' max='500' step='1' style='width:90px;background:#0b1324;color:#cbd5e1;border:1px solid #253246;border-radius:8px;padding:6px'>
    <span class='muted'>agressividade</span>
    <select id='learnAgg' style='background:#0b1324;color:#cbd5e1;border:1px solid #253246;border-radius:8px;padding:6px'>
      <option value='low'>low</option>
      <option value='medium'>medium</option>
      <option value='high'>high</option>
    </select>
    <button class='strat-btn' onclick='saveLearningSettings()'>Salvar aprendizado</button>
    <button class='strat-btn' onclick='resetLearningState()'>Reset learning state</button>
    <span id='learningStatus' class='muted'></span>
  </div>
  <div id='learningSummary' class='muted' style='margin-top:8px'>-</div>
</div>
<div class='section columns-2'>
  <div class='card'><h3>Linha do tempo operacional</h3><div id='timeline' class='timeline compact-scroll'><div class='muted'>Carregando...</div></div></div>
  <div class='card'><h3>OSINT: Google News (Polymarket/Crypto)</h3><div id='news' class='muted compact-scroll'>Carregando notícias...</div></div>
</div>
<div class='section card'>
  <h3>Trades fechados (linha do tempo visual)</h3>
  <div id='closedTimeline' class='timeline'><div class='muted'>Carregando trades fechados...</div></div>
</div>
<div class='section columns'>
  <div class='card'><h3>Posições abertas (top 12)</h3><table class='table' id='posTable'><thead><tr><th>Mercado</th><th>Lado</th><th>Preço</th><th>Tamanho</th><th>Estratégia</th></tr></thead><tbody><tr><td colspan='5' class='muted'>Carregando...</td></tr></tbody></table></div>
  <div class='card'><h3>Último relatório</h3><pre id='report'>(sem relatório)</pre></div>
</div>
<div class='section card'><h3>Log de execução (tail)</h3><pre id='log'>(sem logs)</pre></div>
</div>
<script>
const DASH_TOKEN = new URLSearchParams(window.location.search).get('token') || '';
function apiPath(path){
  if(!DASH_TOKEN) return path;
  return path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(DASH_TOKEN);
}
function apiFetch(path, options={}){
  const headers = Object.assign({}, options.headers || {});
  if(DASH_TOKEN) headers['X-Dashboard-Token'] = DASH_TOKEN;
  return fetch(apiPath(path), Object.assign({}, options, {headers}));
}
function fmtMoney(v){ if(v===undefined||v===null||Number.isNaN(Number(v))) return '$0'; return '$'+Number(v).toLocaleString('pt-BR',{maximumFractionDigits:2}); }
function safe(v,d=0){ const n=Number(v); return Number.isFinite(n)?n:d; }

function renderPositions(positions){
  const tbody=document.querySelector('#posTable tbody');
  if(!positions||!positions.length){ tbody.innerHTML="<tr><td colspan='5' class='muted'>Nenhuma posição aberta.</td></tr>"; return; }
  tbody.innerHTML=positions.slice(0,12).map(p=>{ const m=(p.market_slug||p.market_id||'sem slug').toString().slice(0,52); const side=(p.side||'-').toUpperCase(); const cls=side==='YES'?'yes':'no'; const price=safe(p.entry_price,0).toFixed(3); const size=fmtMoney(p.size); const strat=(p.strategy||p.extra?.strategy||'-').toString(); return `<tr><td title="${m}">${m}</td><td><span class='tag ${cls}'>${side}</span></td><td>${price}</td><td>${size}</td><td>${strat}</td></tr>`; }).join('');
}

function drawEquity(points){
  const c=document.getElementById('equity'); const ctx=c.getContext('2d');
  ctx.clearRect(0,0,c.width,c.height);
  ctx.fillStyle='#070d1a'; ctx.fillRect(0,0,c.width,c.height);
  if(!points || points.length<2){ ctx.fillStyle='#93a4bd'; ctx.font='14px sans-serif'; ctx.fillText('Dados insuficientes para curva (aguardando mais ciclos).',20,40); return; }
  const vals=points.map(p=>Number(p.bankroll));
  const min=Math.min(...vals), max=Math.max(...vals); const pad=28;
  const x=(i)=> pad + (i*(c.width-2*pad)/(points.length-1));
  const y=(v)=> { if(max===min) return c.height/2; return c.height-pad - ((v-min)*(c.height-2*pad)/(max-min)); };
  ctx.strokeStyle='#1f2d46'; ctx.lineWidth=1;
  for(let i=0;i<5;i++){ const yy=pad+i*((c.height-2*pad)/4); ctx.beginPath(); ctx.moveTo(pad,yy); ctx.lineTo(c.width-pad,yy); ctx.stroke(); }
  ctx.strokeStyle='#38bdf8'; ctx.lineWidth=2; ctx.beginPath(); points.forEach((p,i)=>{ const xx=x(i), yy=y(Number(p.bankroll)); if(i===0) ctx.moveTo(xx,yy); else ctx.lineTo(xx,yy); }); ctx.stroke();
  const last=points[points.length-1]; const lx=x(points.length-1), ly=y(Number(last.bankroll));
  ctx.fillStyle='#22c55e'; ctx.beginPath(); ctx.arc(lx,ly,4,0,Math.PI*2); ctx.fill();
  ctx.fillStyle='#cbd5e1'; ctx.font='12px sans-serif';
  ctx.fillText('min '+min.toFixed(2), pad, c.height-8); ctx.fillText('max '+max.toFixed(2), c.width-90, c.height-8);
}

function drawStrategyBars(positions){
  const c=document.getElementById('stratChart'); const ctx=c.getContext('2d');
  ctx.clearRect(0,0,c.width,c.height); ctx.fillStyle='#070d1a'; ctx.fillRect(0,0,c.width,c.height);
  const counts={};
  (positions||[]).forEach(p=>{ const k=(p.strategy||p.extra?.strategy||'desconhecida'); counts[k]=(counts[k]||0)+1; });
  const entries=Object.entries(counts);
  if(!entries.length){ ctx.fillStyle='#93a4bd'; ctx.font='14px sans-serif'; ctx.fillText('Sem posições abertas no momento.',20,40); return; }
  const pad=30, gap=12, w=((c.width-2*pad)-gap*(entries.length-1))/entries.length;
  const max=Math.max(...entries.map(([,v])=>Number(v)||0),1);
  entries.forEach(([name,val],i)=>{
    const h=((Number(val)||0)/max)*(c.height-90);
    const x=pad+i*(w+gap), y=c.height-40-h;
    ctx.fillStyle='#60a5fa'; ctx.fillRect(x,y,w,h);
    ctx.fillStyle='#cbd5e1'; ctx.font='11px sans-serif';
    ctx.fillText(String(val), x+2, y-6);
    ctx.fillText(name.slice(0,16), x+2, c.height-20);
  });
}

function renderTimeline(data){
  const el=document.getElementById('timeline');
  const items=[];
  items.push({t:'Base histórica', tag:'bad', title:'Incidente DNS já mitigado', txt:'Falhas de resolução ocorreram, com fallback aplicado no código.'});
  items.push({t:'Base histórica', tag:'warn', title:'Estratégia focada', txt:'Bot configurado para alternar estratégias via dashboard.'});
  const mode = data?.strategies?.mode || '-';
  const running = data?.strategies?.mode_from_report || '-';
  items.push({t:'Agora', tag:'ok', title:'Modo configurado: '+mode, txt:'Último ciclo executado em: '+running});
  const sem = data?.cycle_signal?.level || 'neutro';
  const reason = data?.cycle_signal?.reason || '-';
  const tag = sem==='bom'?'ok':(sem==='ruim'?'bad':'warn');
  items.push({t:'Último ciclo', tag, title:'Semáforo: '+sem.toUpperCase(), txt:reason});
  const report = data?.last_report || '';
  const m = report.match(/Trades:\\*\\*\\s*(\\d+) opened,\\s*(\\d+) skipped/);
  if(m){ items.push({t:'Último ciclo', tag:'ok', title:`Execução: ${m[1]} abertas, ${m[2]} puladas`, txt:'Resumo extraído do último relatório.'}); }
  el.innerHTML = items.map(i=>`<article class='t-item'><div class='muted'>${i.t}</div><div><b>${i.title}</b></div><div class='sem ${i.tag==='ok'?'bom':(i.tag==='bad'?'ruim':'neutro')}' style='margin:4px 0'>${i.tag.toUpperCase()}</div><div>${i.txt}</div></article>`).join('');
}

function renderNews(items, status){
  const el=document.getElementById('news');
  const st = status || {};
  if(!st.ok){
    const err = (st.error || 'sem detalhes').replace(/</g,'&lt;');
    el.innerHTML = `<div class='sem ruim'>OFFLINE</div><div style='margin-top:8px'>Google News RSS não respondeu neste ambiente.</div><div class='muted' style='margin-top:6px;font-size:12px'>${err}</div>`;
    return;
  }
  if(!items || !items.length){
    el.innerHTML="<div class='sem neutro'>OK</div><div style='margin-top:8px'>RSS acessível, mas sem notícias para a query atual.</div>";
    return;
  }
  el.innerHTML = `<div class='sem bom'>OK</div><div class='muted' style='margin:6px 0;font-size:12px'>${items.length} item(ns) via Google News RSS.</div><ul style='margin:0;padding-left:18px'>` + items.map(n => {
    const t = (n.title || 'sem título').replace(/</g,'&lt;');
    const s = (n.source || '').replace(/</g,'&lt;');
    const p = (n.published || '').replace(/</g,'&lt;');
    const l = n.link || '#';
    return `<li style='margin:8px 0'><a href='${l}' target='_blank' rel='noopener noreferrer' style='color:#93c5fd;text-decoration:none'>${t}</a><br><span class='muted' style='font-size:12px'>${s} ${s&&p?'·':''} ${p}</span></li>`;
  }).join('') + `</ul>`;
}

function renderClosedTradesTimeline(wallet){
  const el=document.getElementById('closedTimeline');
  const hist = Array.isArray(wallet?.history) ? wallet.history : [];
  const closed = hist.filter(x => (x?.status||'')==='closed');
  if(!closed.length){
    el.innerHTML = "<div class='muted'>Ainda não há trades fechados.</div>";
    return;
  }

  const toTs = (x) => {
    const raw = x?.closed_at || x?.timestamp || x?.opened_at || '';
    const t = Date.parse(raw);
    return Number.isFinite(t) ? t : 0;
  };
  const fmtTs = (x) => {
    const raw = x?.closed_at || x?.timestamp || x?.opened_at || '';
    const t = Date.parse(raw);
    return Number.isFinite(t) ? new Date(t).toLocaleString('pt-BR') : '-';
  };
  const fmtPnl = (v) => {
    const n = Number(v || 0);
    const s = n>=0?'+':'';
    return s + n.toLocaleString('pt-BR',{minimumFractionDigits:2, maximumFractionDigits:2});
  };

  const rows = closed.slice().sort((a,b)=>toTs(b)-toTs(a)).slice(0,20);
  el.innerHTML = rows.map(t => {
    const pnlRaw = (t?.realized_pnl ?? t?.pnl ?? 0);
    const pnl = Number(pnlRaw || 0);
    const cls = pnl>0 ? 'pnl-pos' : (pnl<0 ? 'pnl-neg' : 'pnl-zero');
    const reason = (t?.close_reason || 'unknown').toString();
    const strat = (t?.strategy || t?.extra?.strategy || 'desconhecida').toString();
    const side = (t?.side || '-').toString().toUpperCase();
    const market = (t?.market_slug || t?.market_id || 'sem mercado').toString();
    const price = Number(t?.entry_price || 0).toFixed(3);
    const size = fmtMoney(t?.size || 0);
    const pnlColor = pnl>0 ? 'var(--good)' : (pnl<0 ? 'var(--bad)' : '#cbd5e1');
    const trusted = t?.trusted_for_pnl !== false;
    const audit = trusted ? '' : ` • <span class='sem ruim'>QUARENTENA</span>`;
    return `<article class='t-item ${cls}'>
      <div class='muted'>${fmtTs(t)}</div>
      <div><b>${strat}</b> • <span class='tag ${side==='YES'?'yes':'no'}'>${side}</span>${audit}</div>
      <div style='margin-top:3px'>${market.slice(0,88)}</div>
      <div class='muted' style='margin-top:2px'>entry ${price} • size ${size} • motivo ${reason}</div>
      <div style='margin-top:4px;font-weight:700;color:${pnlColor}'>P&L ${fmtPnl(pnl)}</div>
    </article>`;
  }).join('');
}

let selectedStrategies = new Set();

function renderStrategies(strategies){
  const el=document.getElementById('strategies');
  if(!strategies || !Array.isArray(strategies.available) || !strategies.available.length){
    el.innerHTML = "<span class='muted'>Sem estratégias disponíveis.</span>";
    return;
  }
  selectedStrategies = new Set(strategies.active || []);
  const counts = strategies.last_scan_counts || {};
  const modeFromReport = strategies.mode_from_report || '-';
  const head = `<div class='muted' style='width:100%'>Selecionadas: <b>${strategies.mode}</b> | Em execução (último ciclo): <b>${modeFromReport}</b></div>`;
  const buttons = strategies.available.map(name => {
    const isActive = selectedStrategies.has(name);
    const cls = isActive ? 'active' : 'idle';
    const badge = Number.isFinite(Number(counts[name])) ? ` <span class='strat-count'>(${counts[name]})</span>` : '';
    return `<button class='strat-btn ${cls}' onclick='toggleStrategy("${name}")' title='Alternar ${name}'>${name}${badge}</button>`;
  }).join('');
  const actions = `<button class='strat-btn' onclick='selectAllStrategies()' title='Selecionar todas'>all</button><button class='strat-btn' onclick='applyStrategies()' title='Salvar seleção'>Salvar seleção</button>`;
  el.innerHTML = head + buttons + actions;
}

function toggleStrategy(name){
  if(selectedStrategies.has(name)) selectedStrategies.delete(name); else selectedStrategies.add(name);
  if(selectedStrategies.size===0) selectedStrategies.add('btc_5m_momentum');
  applyStrategies();
}

async function selectAllStrategies(){
  try{
    const r = await apiFetch('/api/strategy', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mode:'all'})});
    const d = await r.json();
    if(!r.ok || !d.ok){ alert('Falha ao ativar all: '+(d.error||r.status)); return; }
    await load();
  }catch(e){ alert('Erro ao ativar all: '+e); }
}

async function applyStrategies(){
  try{
    const modes = Array.from(selectedStrategies);
    const r = await apiFetch('/api/strategy', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({modes})});
    const d = await r.json();
    if(!r.ok || !d.ok){ alert('Falha ao salvar estratégias: '+(d.error||r.status)); return; }
    await load();
  }catch(e){
    alert('Erro ao salvar estratégias: '+e);
  }
}

async function saveLoopInterval(){
  const el = document.getElementById('loopSeconds');
  const status = document.getElementById('botCtrlStatus');
  const interval_seconds = Number(el.value || 0);
  if(!Number.isFinite(interval_seconds) || interval_seconds < 30 || interval_seconds > 3600){
    alert('Intervalo inválido. Use entre 30 e 3600 segundos.');
    return;
  }
  status.textContent = 'Salvando...';
  try{
    const r = await apiFetch('/api/loop_interval', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({interval_seconds})});
    const d = await r.json();
    if(!r.ok || !d.ok){ throw new Error(d.error || r.status); }
    status.textContent = `Intervalo salvo: ${d.interval_seconds}s (vale no próximo ciclo).`;
    await load();
  }catch(e){
    status.textContent = 'Erro ao salvar intervalo: '+e;
  }
}

async function controlBot(action){
  const status = document.getElementById('botCtrlStatus');
  status.textContent = 'Executando '+action+'...';
  try{
    const r = await apiFetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action})});
    const d = await r.json();
    if(!r.ok || !d.ok){ throw new Error((d.error||'falha') + (d.stderr ? ' | '+d.stderr : '')); }
    status.textContent = d.message || ('OK: '+action);
    await load();
  }catch(e){
    status.textContent = 'Erro em '+action+': '+e;
  }
}

function fillSettingsForm(s){
  if(!s) return;
  const setNum=(id,val)=>{ const el=document.getElementById(id); if(el && Number.isFinite(Number(val))) el.value=Number(val); };
  const auto=document.getElementById('autoRiskEnabled'); if(auto) auto.checked=!!s.auto_risk_enabled;
  setNum('sMaxExposure', s.max_exposure);
  setNum('sMaxTrade', s.max_trade);
  setNum('sMinTrade', s.min_trade);
  setNum('sMaxPerScan', s.max_per_scan);
  setNum('sMinEdge', s.min_edge);
  const llmEnabled=document.getElementById('sLlmEnabled'); if(llmEnabled) llmEnabled.checked=!!s.llm_enabled;
  const llmMode=document.getElementById('sLlmMode'); if(llmMode && s.llm_mode) llmMode.value=String(s.llm_mode);
  const llmUrl=document.getElementById('sLlmUrl'); if(llmUrl) llmUrl.value=s.llm_url || 'http://127.0.0.1:8080/v1/chat/completions';
  const llmSummary=document.getElementById('llmSummary');
  if(llmSummary){
    const on = s.llm_enabled ? 'ligado' : 'desligado';
    llmSummary.textContent = `LLM ${on} | modo=${s.llm_mode || 'fast'} | fast=0.5B, balanced=1.5B, strong=Qwen3 4B`;
  }
  setNum('sStopLoss', s.stop_loss);
  setNum('sTakeProfit', s.take_profit);
  setNum('sCbLossSeq', s.cb_loss_seq);
  setNum('sCbLossSum6', s.cb_loss_sum6);
  setNum('sCbCooldownCycles', s.cb_cooldown_cycles);
  setNum('sRotWindow', s.rot_window);
  setNum('sRotTopK', s.rot_top_k);
  setNum('sRotWeightWinrate', s.rot_weight_winrate);
  setNum('sRotWeightPnl', s.rot_weight_pnl);
  setNum('sMinNetEdge', s.min_net_edge);
  setNum('sTakerFeeEstimate', s.taker_fee_estimate);
  setNum('sSlippageEstimate', s.slippage_estimate);
  setNum('sSmartMoneyMaxSpread', s.smart_money_max_spread);
  setNum('sSmartMoneyMinLiquidity', s.smart_money_min_liquidity);
  setNum('sSmartMoneyMinVol24h', s.smart_money_min_vol24h);
  setNum('sEventCountdownMaxSpread', s.event_countdown_max_spread);
  setNum('sEventCountdownMinLiquidity', s.event_countdown_min_liquidity);
  setNum('sEventCountdownMinVol24h', s.event_countdown_min_vol24h);
  setNum('sBtcMaxEntryPrice', s.btc_max_entry_price);
  setNum('sBtcMinLiquidity', s.btc_min_liquidity);
  setNum('sEndgameMaxEntryPrice', s.endgame_max_entry_price);
  setNum('sEndgameMinLiquidity', s.endgame_min_liquidity);
  const shadow=document.getElementById('sShadowStrategies'); if(shadow) shadow.value=s.shadow_strategies || '';
}

function detectPresetFromSettings(s){
  if(!s) return 'custom';
  const same=(a,b)=>Math.abs(Number(a)-Number(b))<1e-9;
  const isConservador = Number(s.cb_loss_seq)===3 && same(s.cb_loss_sum6,-15) && Number(s.cb_cooldown_cycles)===4 && Number(s.rot_window)===140 && Number(s.rot_top_k)===2 && same(s.rot_weight_winrate,0.8) && same(s.rot_weight_pnl,0.2) && same(s.min_edge,0.08) && Number(s.max_per_scan)===2;
  if(isConservador) return 'conservador';
  const isBalanceado = Number(s.cb_loss_seq)===4 && same(s.cb_loss_sum6,-25) && Number(s.cb_cooldown_cycles)===2 && Number(s.rot_window)===100 && Number(s.rot_top_k)===3 && same(s.rot_weight_winrate,0.7) && same(s.rot_weight_pnl,0.3) && same(s.min_edge,0.06) && Number(s.max_per_scan)===3;
  if(isBalanceado) return 'balanceado';
  const isAgressivo = Number(s.cb_loss_seq)===6 && same(s.cb_loss_sum6,-45) && Number(s.cb_cooldown_cycles)===1 && Number(s.rot_window)===60 && Number(s.rot_top_k)===5 && same(s.rot_weight_winrate,0.55) && same(s.rot_weight_pnl,0.45) && same(s.min_edge,0.04) && Number(s.max_per_scan)===5;
  if(isAgressivo) return 'agressivo';
  return 'custom';
}

function updatePresetBadge(s){
  const el=document.getElementById('presetActive');
  if(!el) return;
  const p=detectPresetFromSettings(s);
  el.textContent='Preset ativo: '+p;
}

function fillLearningForm(l){
  if(!l) return;
  const en=document.getElementById('learnEnabled'); if(en) en.checked=!!l.enabled;
  const sh=document.getElementById('learnShadow'); if(sh) sh.checked=!!l.shadow_mode;
  const ms=document.getElementById('learnMinSamples'); if(ms && Number.isFinite(Number(l.min_samples))) ms.value=Number(l.min_samples);
  const ag=document.getElementById('learnAgg'); if(ag && l.aggressiveness) ag.value=String(l.aggressiveness);
  const sum=document.getElementById('learningSummary');
  if(sum){
    const r = Array.isArray(l.reasons) ? l.reasons.join(' | ') : '-';
    sum.textContent = `effective_min_edge=${Number(l.effective_min_edge||0).toFixed(3)} | confiança=${l.confidence||'-'} | features=${Number(l.features_count||0)} | estratégias=${Number(l.strategies_tracked||0)} | motivo=${r}`;
  }
}

async function saveSettings(){
  const status = document.getElementById('settingsStatus');
  const payload = {
    auto_risk_enabled: !!document.getElementById('autoRiskEnabled')?.checked,
    max_exposure: Number(document.getElementById('sMaxExposure')?.value || 0),
    max_trade: Number(document.getElementById('sMaxTrade')?.value || 0),
    min_trade: Number(document.getElementById('sMinTrade')?.value || 0),
    max_per_scan: Number(document.getElementById('sMaxPerScan')?.value || 0),
    min_edge: Number(document.getElementById('sMinEdge')?.value || 0),
    llm_enabled: !!document.getElementById('sLlmEnabled')?.checked,
    llm_mode: String(document.getElementById('sLlmMode')?.value || 'fast'),
    llm_url: String(document.getElementById('sLlmUrl')?.value || 'http://127.0.0.1:8080/v1/chat/completions'),
    stop_loss: Number(document.getElementById('sStopLoss')?.value || 0),
    take_profit: Number(document.getElementById('sTakeProfit')?.value || 0),
    cb_loss_seq: Number(document.getElementById('sCbLossSeq')?.value || 4),
    cb_loss_sum6: Number(document.getElementById('sCbLossSum6')?.value || -25),
    cb_cooldown_cycles: Number(document.getElementById('sCbCooldownCycles')?.value || 2),
    rot_window: Number(document.getElementById('sRotWindow')?.value || 80),
    rot_top_k: Number(document.getElementById('sRotTopK')?.value || 4),
    rot_weight_winrate: Number(document.getElementById('sRotWeightWinrate')?.value || 0.7),
    rot_weight_pnl: Number(document.getElementById('sRotWeightPnl')?.value || 0.3),
    min_net_edge: Number(document.getElementById('sMinNetEdge')?.value || 0.035),
    taker_fee_estimate: Number(document.getElementById('sTakerFeeEstimate')?.value || 0.001),
    slippage_estimate: Number(document.getElementById('sSlippageEstimate')?.value || 0.01),
    smart_money_max_spread: Number(document.getElementById('sSmartMoneyMaxSpread')?.value || 0.03),
    smart_money_min_liquidity: Number(document.getElementById('sSmartMoneyMinLiquidity')?.value || 15000),
    smart_money_min_vol24h: Number(document.getElementById('sSmartMoneyMinVol24h')?.value || 50000),
    event_countdown_max_spread: Number(document.getElementById('sEventCountdownMaxSpread')?.value || 0.06),
    event_countdown_min_liquidity: Number(document.getElementById('sEventCountdownMinLiquidity')?.value || 15000),
    event_countdown_min_vol24h: Number(document.getElementById('sEventCountdownMinVol24h')?.value || 25000),
    btc_max_entry_price: Number(document.getElementById('sBtcMaxEntryPrice')?.value || 0.82),
    btc_min_liquidity: Number(document.getElementById('sBtcMinLiquidity')?.value || 1000),
    endgame_max_entry_price: Number(document.getElementById('sEndgameMaxEntryPrice')?.value || 0.95),
    endgame_min_liquidity: Number(document.getElementById('sEndgameMinLiquidity')?.value || 1500),
    shadow_strategies: String(document.getElementById('sShadowStrategies')?.value || ''),
  };
  status.textContent = 'Salvando ajustes...';
  try{
    const r = await apiFetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    const d = await r.json();
    if(!r.ok || !d.ok) throw new Error(d.error || r.status);
    status.textContent = 'Ajustes salvos com sucesso.';
    fillSettingsForm(d.settings || {});
    await load();
  }catch(e){
    status.textContent = 'Erro ao salvar ajustes: '+e;
  }
}

async function saveLlmSettings(){
  const status = document.getElementById('llmStatus');
  const payload = {
    llm_enabled: !!document.getElementById('sLlmEnabled')?.checked,
    llm_mode: String(document.getElementById('sLlmMode')?.value || 'fast'),
    llm_url: String(document.getElementById('sLlmUrl')?.value || 'http://127.0.0.1:8080/v1/chat/completions'),
  };
  status.textContent = 'Salvando LLM...';
  try{
    const r = await apiFetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    const d = await r.json();
    if(!r.ok || !d.ok) throw new Error(d.error || r.status);
    fillSettingsForm(d.settings || {});
    status.textContent = `LLM ${payload.llm_enabled ? 'habilitado' : 'desabilitado'} (${payload.llm_mode}).`;
    await load();
  }catch(e){
    status.textContent = 'Erro ao salvar LLM: '+e;
  }
}

async function applySettingsPreset(preset){
  const status = document.getElementById('settingsStatus');
  status.textContent = 'Aplicando preset '+preset+'...';
  try{
    const r = await apiFetch('/api/settings_preset', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({preset})});
    const d = await r.json();
    if(!r.ok || !d.ok) throw new Error(d.error || r.status);
    fillSettingsForm(d.settings || {});
    status.textContent = 'Preset aplicado: '+preset;
    await load();
  }catch(e){
    status.textContent = 'Erro ao aplicar preset: '+e;
  }
}

async function saveLearningSettings(){
  const status = document.getElementById('learningStatus');
  const payload = {
    enabled: !!document.getElementById('learnEnabled')?.checked,
    shadow_mode: !!document.getElementById('learnShadow')?.checked,
    min_samples: Number(document.getElementById('learnMinSamples')?.value || 20),
    aggressiveness: String(document.getElementById('learnAgg')?.value || 'medium'),
  };
  status.textContent = 'Salvando aprendizado...';
  try{
    const r = await apiFetch('/api/learning_settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    const d = await r.json();
    if(!r.ok || !d.ok) throw new Error(d.error || r.status);
    status.textContent = 'Aprendizado salvo com sucesso.';
    fillLearningForm(d.learning || {});
    await load();
  }catch(e){
    status.textContent = 'Erro ao salvar aprendizado: '+e;
  }
}

async function resetLearningState(){
  const status = document.getElementById('learningStatus');
  status.textContent = 'Resetando learning state...';
  try{
    const r = await apiFetch('/api/learning_reset', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({})});
    const d = await r.json();
    if(!r.ok || !d.ok) throw new Error(d.error || r.status);
    status.textContent = 'Learning state resetado.';
    fillLearningForm(d.learning || {});
    await load();
  }catch(e){
    status.textContent = 'Erro no reset do learning: '+e;
  }
}

async function load(){
  try{
    const r=await apiFetch('/api/status'); const d=await r.json(); const w=d.wallet||{}; const settings=w.settings||{};
    const positionsRaw=w.positions||[];
    const positions=Array.isArray(positionsRaw)?positionsRaw:Object.values(positionsRaw);
    const bankroll=safe(w.bankroll,0), initial=safe(w.initial_bankroll,0), pnl=bankroll-initial;
    const hist=Array.isArray(w.history)?w.history:[];
    const trustedHist=hist.filter(t => t?.trusted_for_pnl !== false);
    const quarantined=hist.length-trustedHist.length;
    const trustedPnl=trustedHist.reduce((acc,t)=>acc+safe(t.realized_pnl ?? t.pnl,0),0);
    const exposureByPos=positions.reduce((acc,p)=>acc+safe(p.cost,safe(p.size,0)),0); const maxExposure=safe(settings.max_exposure,0);
    const riskPct=maxExposure>0?Math.min(100,(exposureByPos/maxExposure)*100):0;

    document.getElementById('now').textContent='Atualizado: '+new Date().toLocaleString('pt-BR');
    document.getElementById('bankroll').textContent=fmtMoney(bankroll); document.getElementById('bankrollSub').textContent='Inicial: '+fmtMoney(initial);
    const pnlEl=document.getElementById('pnl'); pnlEl.textContent=(pnl>=0?'+':'')+fmtMoney(pnl); pnlEl.style.color=pnl>=0?'var(--good)':'var(--bad)';
    document.getElementById('pnlSub').textContent=`Confiável: ${(trustedPnl>=0?'+':'')+fmtMoney(trustedPnl)} | Quarentena: ${quarantined}`;
    document.getElementById('openCount').textContent=String(positions.length);

    const sem=d.cycle_signal||{level:'neutro',reason:'-'}; const semEl=document.getElementById('cycleSem');
    semEl.className='sem '+(sem.level||'neutro'); semEl.textContent=(sem.level||'neutro').toUpperCase();
    document.getElementById('cycleReason').textContent=sem.reason||'-';

    document.getElementById('riskText').textContent=`Exposição ${fmtMoney(exposureByPos)} / ${fmtMoney(maxExposure)} (${riskPct.toFixed(1)}%)`;
    const fill=document.getElementById('riskFill'); fill.style.width=riskPct.toFixed(1)+'%';
    fill.style.background=riskPct<60?'linear-gradient(90deg,#22c55e,#16a34a)':riskPct<85?'linear-gradient(90deg,#f59e0b,#f97316)':'linear-gradient(90deg,#ef4444,#dc2626)';

    renderPositions(positions); drawEquity(d.equity||[]); drawStrategyBars(positions); renderTimeline(d); renderNews(d.news||[], d.news_status||{}); renderStrategies(d.strategies||{}); renderClosedTradesTimeline(w);
    const ctrl = d.controls || {};
    const loopInput = document.getElementById('loopSeconds');
    if(loopInput && Number.isFinite(Number(ctrl.loop_interval_seconds))){ loopInput.value = Number(ctrl.loop_interval_seconds); }
    const st = document.getElementById('botCtrlStatus');
    if(st){ st.textContent = `loop ${ctrl.paper_loop_running ? 'ativo' : 'parado'} | intervalo ${Number(ctrl.loop_interval_seconds||300)}s`; }
    fillSettingsForm(d.settings_controls || settings || {});
    updatePresetBadge(d.settings_controls || settings || {});
    fillLearningForm(d.learning_controls || {});
    document.getElementById('report').textContent=d.last_report||'(sem relatório ainda)';
    document.getElementById('log').textContent=d.log_tail||'(sem logs ainda)';
  }catch(e){ document.getElementById('now').textContent='Erro de atualização: '+e; }
}
load(); setInterval(load,10000);
</script>
</body></html>"""
        self._send(200, 'text/html; charset=utf-8', html.encode('utf-8'))


if __name__ == '__main__':
    _require_token_when_exposed()
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
