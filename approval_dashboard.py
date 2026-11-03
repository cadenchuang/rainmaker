"""Local-only, simulation-only approval dashboard. No production/auth imports."""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import secrets
from urllib.parse import parse_qs

from approval import ApprovalService, Refused, TradeProposal
from simulation import FixtureFeed, SimulationAdapter, proposal_fixture


STYLE = '''
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#182529;background:#f3f5f3}
*{box-sizing:border-box}body{margin:0}header{background:#162e2d;color:white;padding:25px 5vw}
header h1{font-size:24px;margin:8px 0}header p{color:#c3d8d4;max-width:850px}
.badge{display:inline-block;font-size:12px;font-weight:750;letter-spacing:.08em;padding:6px 9px;background:#f2cc74;color:#342808;border-radius:3px}
main{max-width:1160px;margin:30px auto;padding:0 24px}.intro{display:flex;gap:24px;justify-content:space-between;align-items:center}
h2{font-size:20px}h3{font-size:17px;margin:0 0 8px}p{line-height:1.5}.muted{color:#53635f;font-size:13px}
.card{background:white;border:1px solid #ced8d2;border-radius:8px;padding:22px;margin:18px 0}
.terms{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:18px;margin:20px 0}
.terms dt{font-size:12px;color:#53635f;margin-bottom:5px}.terms dd{margin:0;font-weight:650;overflow-wrap:anywhere}
.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}button{border:0;border-radius:4px;background:#20594d;color:white;padding:12px 18px;font:inherit;font-weight:650;cursor:pointer}
button.reject{background:#f1e4df;color:#783b2a}label{display:block;margin:10px 0;font-size:13px}input,select{font:inherit;padding:8px;border:1px solid #bdcbc4;border-radius:4px;max-width:100%}
details{margin:15px 0}summary{cursor:pointer}.status{font-size:13px;font-weight:750;color:#20594d}.reason{color:#783b2a}
code{font-size:12px;overflow-wrap:anywhere}a{color:#20594d}table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:9px;border-bottom:1px solid #e0e6e2;overflow-wrap:anywhere}
'''


def utc(value):
    return datetime.fromtimestamp(value / 1_000_000_000, timezone.utc).isoformat(timespec='seconds')


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service, port=8765):
        self.service = service
        self.csrf_secret = secrets.token_bytes(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = f'http://127.0.0.1:{self.server_address[1]}'

    def token(self, cookie, action, identity, digest):
        body = '\0'.join((cookie, action, identity, digest)).encode()
        return hmac.new(self.csrf_secret, body, hashlib.sha256).hexdigest()


class Handler(BaseHTTPRequestHandler):
    server_version = 'RainmakerSimulation'

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, *args):
        pass  # Never log request headers, body, cookies, or CSRF material.

    def trusted(self, post=False):
        if self.client_address[0] != '127.0.0.1':
            return False
        if self.headers.get_all('Host') != [self.server.origin.removeprefix('http://')]:
            return False
        if self.headers.get('X-Forwarded-Host') or self.headers.get('Forwarded'):
            return False
        if post and self.headers.get_all('Origin') != [self.server.origin]:
            return False
        site = self.headers.get('Sec-Fetch-Site')
        return not post or site in (None, 'same-origin')

    def respond(self, status, body='', *, cookie=None, location=None):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Connection', 'close')
        if cookie is not None:
            self.send_header('Set-Cookie', f'rainmaker_session={cookie}; Path=/; HttpOnly; SameSite=Strict')
        if location:
            self.send_header('Location', location)
        payload = body.encode('utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def session(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', '')).get('rainmaker_session')
            value = cookie.value if cookie else ''
        except Exception:
            value = ''
        return value if re.fullmatch(r'[A-Za-z0-9_-]{43}', value) else None

    def form(self, cookie, action, identity, digest='', label=None):
        token = self.server.token(cookie, action, identity, digest)
        key = 'ticker' if action == 'create' else 'pid'
        fields = {'action': action, key: identity, 'csrf': token}
        if action != 'create':
            fields['digest'] = digest
        hidden = ''.join(f'<input type="hidden" name="{k}" value="{escape(v, quote=True)}">' for k, v in fields.items())
        return f'<form method="post" action="/action">{hidden}{label or ""}'

    def page(self, cookie):
        service = self.server.service
        cards = []
        for row in service.listing():
            p = TradeProposal.from_body(row['body'])
            terms = {
                'Market / office': f'{p.market_ticker} / US {p.office}',
                'Order': f'{p.side} {p.quantity} {p.outcome} contracts',
                'Exact limit': f'${p.limit_price}',
                'Max principal': f'${p.max_principal}', 'Max fees': f'${p.max_fee}',
                'Maximum cash budget': f'${p.max_principal + p.max_fee}',
                'Created (UTC)': utc(p.created_ns), 'Expires (UTC)': utc(p.expires_ns),
                'Risk / quality': f'{p.risk_status} / {p.data_quality}',
                'Source time (UTC)': utc(p.source_data_ns) if p.source_data_ns is not None else 'Missing',
                'Forecast reference': p.forecast_ref or 'None', 'Fake result scenario': p.simulation_scenario,
            }
            grid = ''.join(f'<div><dt>{escape(k)}</dt><dd>{escape(v)}</dd></div>' for k, v in terms.items())
            rules = p.rules_text
            actions = ''
            if row['state'] == 'PROPOSED':
                actions += self.form(cookie, 'approve', p.proposal_id, p.digest) + '<button>Approve these exact terms</button></form>'
                actions += self.form(cookie, 'reject', p.proposal_id, p.digest) + '<button class="reject">Reject proposal</button></form>'
            if row['state'] == 'APPROVED':
                actions += self.form(cookie, 'simulate', p.proposal_id, p.digest) + '<button>Revalidate &amp; SIMULATE approved order</button></form>'
            if row['state'] in ('RECONCILING', 'SIMULATED_ACKNOWLEDGED'):
                actions += self.form(cookie, 'reconcile', p.proposal_id, p.digest) + '<button>Reconcile fake order — no retry</button></form>'
            cards.append(f'''<article class="card"><div class="status">{escape(row['state'])} · SIMULATION</div>
                <h3>{escape(p.market_ticker)}</h3><div class="muted">Proposal {escape(p.proposal_id)}</div>
                <dl class="terms">{grid}</dl><p class="reason">{escape(row['reason'])}</p>
                <details><summary>Contract rules and approval fingerprint</summary><p>{escape(rules)}</p>
                <code>Contract: {escape(p.contract_id)}<br>Rules SHA-256: {p.rules_hash}<br>Terms SHA-256: {p.digest}</code></details>
                <div class="actions">{actions}</div></article>''')
        creators = []
        for ticker in service.feed.markets:
            inputs = '''<label>Whole contracts <input name="quantity" type="number" min="1" max="20" value="5" required></label>
                <label>Limit dollars <input name="price" value="0.55" required></label>
                <label>Maximum fee dollars <input name="max_fee" value="0.20" required></label>
                <label>Outcome <select name="outcome"><option>YES</option><option>NO</option></select></label>
                <label>Side <select name="side"><option>BUY</option><option>SELL</option></select></label>
                <label>Fake scenario <select name="scenario"><option>fill</option><option>ack</option><option>reject</option><option>timeout</option><option>ambiguous</option></select></label>
                <button>Create unapproved proposal</button></form>'''
            creators.append(self.form(cookie, 'create', ticker) + f'<h3>{escape(ticker)}</h3>' + inputs)
        known, exposure = service.exposure_status()
        exposure_text = (f'Verified simulation exposure: ${exposure} / ${service.limits.max_exposure}' if known
                         else 'Exposure unknown or awaiting reconciliation — new submissions blocked')
        orders = service.orders()
        order_html = ''.join(f"<tr><td>{escape(o['client_id'])}</td><td>{escape(o['state'])}</td><td>${escape(o['reservation'])}</td></tr>" for o in orders)
        return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
            <title>Rainmaker · Simulation approvals</title><style>{STYLE}</style></head><body>
            <header><span class="badge">PHASE A · SIMULATION ONLY</span><h1>Rainmaker approval desk</h1>
            <p>Synthetic U.S. Senate and governor contracts. No Kalshi connection, production credentials, or real orders.
            Approve exact terms first; then explicitly revalidate and simulate.</p></header><main>
            <p class="status">{escape(exposure_text)}</p>
            <div class="intro"><h2>Proposal review</h2><a href="/">Refresh status</a></div>
            <p class="muted">Synthetic quotes are generated at each read. Fees are fixture estimates, not Kalshi fees.
            Exposure reserves a conservative $1 per contract plus fees. SELL requires known simulated inventory.</p>
            {''.join(cards) or '<p>No proposals yet. Create a fixture proposal below.</p>'}
            <details class="card"><summary>Create a simulation proposal</summary><div class="terms">{''.join(creators)}</div></details>
            <h2>Simulated order ledger</h2><table><thead><tr><th>Stable client order ID</th><th>Order state</th><th>Risk reservation</th></tr></thead><tbody>{order_html}</tbody></table>
            <p class="muted">Uncertain outcomes hold reservations and block new exposure. Reconciliation queries the fake adapter only; it never resubmits.
            This local dashboard assumes a trusted operating-system account.</p></main></body></html>'''

    def do_GET(self):
        if not self.trusted():
            self.respond(403, 'Local access only')
            return
        if self.path != '/':
            self.respond(404, 'Not found')
            return
        cookie = self.session() or secrets.token_urlsafe(32)
        try:
            self.respond(200, self.page(cookie), cookie=cookie)
        except Exception:
            self.respond(503, 'Simulation ledger unavailable; no action taken')

    def do_POST(self):
        if not self.trusted(post=True) or self.path != '/action':
            self.respond(403, 'Untrusted request')
            return
        try:
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                raise Refused('Invalid request framing')
            if self.headers.get('Content-Type') != 'application/x-www-form-urlencoded':
                raise Refused('Unsupported content type')
            length = int(self.headers['Content-Length'])
            if not 0 < length <= 8192:
                raise Refused('Request body too large')
            fields = parse_qs(self.rfile.read(length).decode('utf-8'), strict_parsing=True)
            if any(len(v) != 1 for v in fields.values()):
                raise Refused('Duplicate form fields')
            data = {k: v[0] for k, v in fields.items()}
            action = data.get('action')
            if action not in ('create', 'approve', 'reject', 'simulate', 'reconcile'):
                raise Refused('Unknown action')
            creating = action == 'create'
            required = ({'action', 'ticker', 'quantity', 'price', 'max_fee', 'scenario', 'outcome', 'side', 'csrf'} if creating
                        else {'action', 'pid', 'digest', 'csrf'})
            if set(data) != required:
                raise Refused('Unexpected form fields')
            cookie = self.session()
            identity = data['ticker'] if creating else data['pid']
            digest = '' if creating else data['digest']
            if cookie is None or not hmac.compare_digest(data['csrf'], self.server.token(cookie, action, identity, digest)):
                self.respond(403, 'Invalid CSRF token')
                return
            service = self.server.service
            if creating:
                p = proposal_fixture(service.feed, identity, clock=service.clock, quantity=int(data['quantity']),
                    price=data['price'], max_fee=data['max_fee'], scenario=data['scenario'], outcome=data['outcome'], side=data['side'])
                service.create(p)
            else:
                getattr(service, action)(identity, digest)
            self.respond(303, location='/')
        except Refused as exc:
            self.respond(409, '<p>Action refused: ' + escape(str(exc)) + '</p><a href="/">Return to review</a>')
        except (ValueError, UnicodeError):
            self.respond(400, 'Invalid simulation request')
        except Exception:
            self.respond(503, 'Simulation ledger unavailable; do not retry uncertain submissions')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local SIMULATION ONLY approval dashboard')
    parser.add_argument('--db', default='approval-simulation.sqlite')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('Invalid localhost port')
    feed = FixtureFeed()
    service = ApprovalService(args.db, feed, SimulationAdapter())
    if not service.listing():
        for ticker in feed.markets:
            service.create(proposal_fixture(feed, ticker))
    server = DashboardServer(service, args.port)
    print(f'SIMULATION ONLY — {server.origin} — no production credentials or order submission')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
