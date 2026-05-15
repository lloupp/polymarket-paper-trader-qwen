import json
import re
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse, parse_qs
from urllib.request import urlopen, Request
import xml.etree.ElementTree as ET

HOST = "0.0.0.0"
PORT = int(os.getenv("DASHBOARD_PORT", "8090"))
BASE = Path(__file__).resolve().parent
WALLET = BASE / 'wallet.json'
LOG = BASE / 'logs' / 'paper_runner.log'
LAST = BASE / 'logs' / 'last_report.txt'
CONTROL_TOKEN = os.getenv("CONTROL_TOKEN", "")


def tail(path: Path, n=200):
    if not path.exists():
        return ''
    lines = path.read_text(errors='ignore').splitlines()
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
        return news
    except Exception:
        return []


class H(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self, query):
        if not CONTROL_TOKEN:
            return True
        token = query.get('token', [''])[0]
        return token == CONTROL_TOKEN

    def _run_control(self, cmd):
        try:
            out = subprocess.check_output(cmd, cwd=str(BASE), stderr=subprocess.STDOUT, text=True, timeout=20)
            return {"ok": True, "output": out[-4000:]}
        except subprocess.CalledProcessError as e:
            return {"ok": False, "output": (e.output or '')[-4000:], "code": e.returncode}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == '/health':
            self._send(200, 'application/json', b'{"ok":true}')
            return

        if path == '/api/control/status':
            if not self._check_token(query):
                self._send(403, 'application/json; charset=utf-8', b'{"ok":false,"error":"forbidden"}')
                return
            payload = self._run_control(["bash", "status.sh"])
            self._send(200, 'application/json; charset=utf-8', json.dumps(payload).encode('utf-8'))
            return

        if path == '/api/control/start':
            if not self._check_token(query):
                self._send(403, 'application/json; charset=utf-8', b'{"ok":false,"error":"forbidden"}')
                return
            payload = self._run_control(["bash", "start_all.sh"])
            self._send(200, 'application/json; charset=utf-8', json.dumps(payload).encode('utf-8'))
            return

        if path == '/api/control/stop':
            if not self._check_token(query):
                self._send(403, 'application/json; charset=utf-8', b'{"ok":false,"error":"forbidden"}')
                return
            payload = self._run_control(["bash", "stop_all.sh"])
            self._send(200, 'application/json; charset=utf-8', json.dumps(payload).encode('utf-8'))
            return

        if path == '/api/status':
            wallet = {}
            if WALLET.exists():
                try:
                    wallet = json.loads(WALLET.read_text())
                except Exception:
                    wallet = {"error": "wallet parse failed"}

            report = LAST.read_text(errors='ignore') if LAST.exists() else ''
            log_full = LOG.read_text(errors='ignore') if LOG.exists() else ''
            payload = {
                'now': datetime.now(timezone.utc).isoformat(),
                'wallet': wallet,
                'last_report': report,
                'log_tail': tail(LOG, 200),
                'equity': parse_equity_from_log(log_full),
                'cycle_signal': cycle_signal(report),
                'news': fetch_google_news(),
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
*{box-sizing:border-box} body{margin:0;background:radial-gradient(1200px 600px at 10% -10%,#1e293b 0,#0b1220 45%);color:var(--txt);font-family:Inter,Segoe UI,Arial,sans-serif}
.wrap{max-width:1220px;margin:0 auto;padding:18px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.h1{font-size:24px;font-weight:800}.badge{font-size:12px;padding:6px 10px;border-radius:999px;border:1px solid var(--line);color:var(--muted);background:rgba(255,255,255,.02)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:14px;padding:12px}
.kpi-title{font-size:12px;color:var(--muted);margin-bottom:6px}.kpi-value{font-size:26px;font-weight:800}.kpi-sub{font-size:12px;color:var(--muted);margin-top:4px}
.bar{height:10px;background:#0a1020;border:1px solid var(--line);border-radius:999px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,var(--blue),#60a5fa)}
.section{margin-top:12px}.section h3{margin:0 0 8px 0;font-size:15px;color:#cbd5e1}.columns{display:grid;grid-template-columns:1.3fr .9fr;gap:10px}
.table{width:100%;border-collapse:collapse;font-size:13px}.table th,.table td{padding:8px;border-bottom:1px solid var(--line);text-align:left}.table th{color:#9fb1ca;font-weight:600}
.tag{padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line);display:inline-block}.yes{background:rgba(34,197,94,.12);color:#86efac}.no{background:rgba(239,68,68,.12);color:#fca5a5}
.sem{display:inline-block;padding:6px 10px;border-radius:999px;font-weight:700;font-size:12px}.sem.bom{background:rgba(34,197,94,.15);color:#86efac;border:1px solid rgba(34,197,94,.35)}.sem.neutro{background:rgba(245,158,11,.15);color:#fcd34d;border:1px solid rgba(245,158,11,.35)}.sem.ruim{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.35)}
pre{white-space:pre-wrap;word-break:break-word;background:#070d1a;border:1px solid var(--line);border-radius:10px;padding:10px;max-height:34vh;overflow:auto;font-size:12px}
canvas{width:100%;height:240px;background:#070d1a;border:1px solid var(--line);border-radius:10px}.muted{color:var(--muted)}
@media (max-width:960px){.grid{grid-template-columns:1fr 1fr}.columns{grid-template-columns:1fr}} @media (max-width:560px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body><div class='wrap'>
<div class='top'><div class='h1'>Polymarket Paper Trader Dashboard</div><div class='badge' id='now'>atualizando...</div></div>
<div class='grid'>
  <div class='card'><div class='kpi-title'>Bankroll atual</div><div id='bankroll' class='kpi-value'>$0</div><div id='bankrollSub' class='kpi-sub'>vs inicial</div></div>
  <div class='card'><div class='kpi-title'>Lucro/Prejuízo (P&L)</div><div id='pnl' class='kpi-value'>$0</div><div class='kpi-sub'>paper trading</div></div>
  <div class='card'><div class='kpi-title'>Posições abertas</div><div id='openCount' class='kpi-value'>0</div><div class='kpi-sub'>exposição ativa</div></div>
  <div class='card'><div class='kpi-title'>Semáforo do ciclo</div><div id='cycleSem' class='sem neutro'>NEUTRO</div><div id='cycleReason' class='kpi-sub'>-</div></div>
</div>
<div class='section card'><h3>Uso de risco</h3><div class='muted' id='riskText'>-</div><div class='bar' style='margin-top:8px'><div id='riskFill' class='fill' style='width:0%'></div></div></div>
<div class='section card'><h3>Curva de equity (bankroll ao longo do tempo)</h3><canvas id='equity' width='1100' height='260'></canvas></div>
<div class='section card'><h3>OSINT: Google News (Polymarket/Crypto)</h3><div id='news' class='muted'>Carregando notícias...</div></div>
<div class='section columns'>
  <div class='card'><h3>Posições abertas (top 12)</h3><table class='table' id='posTable'><thead><tr><th>Mercado</th><th>Lado</th><th>Preço</th><th>Tamanho</th><th>Estratégia</th></tr></thead><tbody><tr><td colspan='5' class='muted'>Carregando...</td></tr></tbody></table></div>
  <div class='card'><h3>Último relatório</h3><pre id='report'>(sem relatório)</pre></div>
</div>
<div class='section card'><h3>Log de execução (tail)</h3><pre id='log'>(sem logs)</pre></div>
</div>
<script>
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

function renderNews(items){
  const el=document.getElementById('news');
  if(!items || !items.length){ el.innerHTML="<span class='muted'>Sem notícias no momento.</span>"; return; }
  el.innerHTML = `<ul style='margin:0;padding-left:18px'>` + items.map(n => {
    const t = (n.title || 'sem título').replace(/</g,'&lt;');
    const s = (n.source || '').replace(/</g,'&lt;');
    const p = (n.published || '').replace(/</g,'&lt;');
    const l = n.link || '#';
    return `<li style='margin:8px 0'><a href='${l}' target='_blank' rel='noopener noreferrer' style='color:#93c5fd;text-decoration:none'>${t}</a><br><span class='muted' style='font-size:12px'>${s} ${s&&p?'·':''} ${p}</span></li>`;
  }).join('') + `</ul>`;
}

async function load(){
  try{
    const r=await fetch('/api/status'); const d=await r.json(); const w=d.wallet||{}; const settings=w.settings||{};
    const positionsRaw=w.positions||[];
    const positions=Array.isArray(positionsRaw)?positionsRaw:Object.values(positionsRaw);
    const bankroll=safe(w.bankroll,0), initial=safe(w.initial_bankroll,0), pnl=bankroll-initial;
    const exposureByPos=positions.reduce((acc,p)=>acc+safe(p.cost,safe(p.size,0)),0); const maxExposure=safe(settings.max_exposure,0);
    const riskPct=maxExposure>0?Math.min(100,(exposureByPos/maxExposure)*100):0;

    document.getElementById('now').textContent='Atualizado: '+new Date().toLocaleString('pt-BR');
    document.getElementById('bankroll').textContent=fmtMoney(bankroll); document.getElementById('bankrollSub').textContent='Inicial: '+fmtMoney(initial);
    const pnlEl=document.getElementById('pnl'); pnlEl.textContent=(pnl>=0?'+':'')+fmtMoney(pnl); pnlEl.style.color=pnl>=0?'var(--good)':'var(--bad)';
    document.getElementById('openCount').textContent=String(positions.length);

    const sem=d.cycle_signal||{level:'neutro',reason:'-'}; const semEl=document.getElementById('cycleSem');
    semEl.className='sem '+(sem.level||'neutro'); semEl.textContent=(sem.level||'neutro').toUpperCase();
    document.getElementById('cycleReason').textContent=sem.reason||'-';

    document.getElementById('riskText').textContent=`Exposição ${fmtMoney(exposureByPos)} / ${fmtMoney(maxExposure)} (${riskPct.toFixed(1)}%)`;
    const fill=document.getElementById('riskFill'); fill.style.width=riskPct.toFixed(1)+'%';
    fill.style.background=riskPct<60?'linear-gradient(90deg,#22c55e,#16a34a)':riskPct<85?'linear-gradient(90deg,#f59e0b,#f97316)':'linear-gradient(90deg,#ef4444,#dc2626)';

    renderPositions(positions); drawEquity(d.equity||[]); renderNews(d.news||[]);
    document.getElementById('report').textContent=d.last_report||'(sem relatório ainda)';
    document.getElementById('log').textContent=d.log_tail||'(sem logs ainda)';
  }catch(e){ document.getElementById('now').textContent='Erro de atualização: '+e; }
}
load(); setInterval(load,10000);
</script>
</body></html>"""
        self._send(200, 'text/html; charset=utf-8', html.encode('utf-8'))


if __name__ == '__main__':
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
