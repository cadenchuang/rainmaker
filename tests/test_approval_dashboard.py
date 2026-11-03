"""HTTP tests bind to localhost only. No production credentials or APIs."""
from contextlib import contextmanager
import html
from http.client import HTTPConnection
import re
import threading
from urllib.parse import urlencode

import pytest
from approval import ApprovalService
from approval_dashboard import DashboardServer
from simulation import FixtureFeed, SimulationAdapter, proposal_fixture
from test_approval import Clock, AdvancingClock


@contextmanager
def dashboard(tmp_path, clock=None):
    clock = clock or Clock()
    feed = FixtureFeed(clock=clock)
    adapter = SimulationAdapter()
    service = ApprovalService(tmp_path / 'ui.sqlite', feed, adapter, clock=clock)
    service.create(proposal_fixture(feed, 'SIM-KXSENATE-PA', clock=clock))
    server = DashboardServer(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, service, adapter
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def request(server, method='GET', path='/', data=None, cookie=None, origin=True, host=None, content_type=None):
    connection = HTTPConnection(*server.server_address, timeout=3)
    headers = {}
    if host is not None:
        headers['Host'] = host
    if cookie:
        headers['Cookie'] = cookie
    if method == 'POST':
        headers['Content-Type'] = content_type or 'application/x-www-form-urlencoded'
        if origin:
            headers['Origin'] = server.origin if origin is True else origin
    body = urlencode(data) if data is not None else None
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        connection.close()


def action_form(page, action):
    for fields in re.findall(r'<form.*?</form>', page, re.S):
        values = dict((name, html.unescape(value)) for name, value in
                      re.findall(r'name="([^"]+)" value="([^"]*)"', fields))
        if values.get('action') == action:
            return values
    raise AssertionError('Missing action ' + action)


def test_deliberate_approval_and_simulation_ui(tmp_path):
    with dashboard(tmp_path) as (server, service, adapter):
        assert server.server_address[0] == '127.0.0.1'
        status, headers, page = request(server)
        assert status == 200 and 'SIMULATION ONLY' in page
        assert '$2.75' in page and 'Expires (UTC)' in page
        assert 'HttpOnly' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
        assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
        assert headers['Cache-Control'] == 'no-store'
        cookie = headers['Set-Cookie'].split(';')[0]
        approve = action_form(page, 'approve')
        assert request(server, 'POST', '/action', approve, cookie)[0] == 303
        assert not adapter.submissions
        page = request(server, cookie=cookie)[2]
        assert 'APPROVED' in page
        simulate = action_form(page, 'simulate')
        assert request(server, 'POST', '/action', simulate, cookie)[0] == 303
        assert request(server, 'POST', '/action', simulate, cookie)[0] == 303
        assert len(adapter.submissions) == 1
        assert 'SIMULATED_FILLED' in request(server, cookie=cookie)[2]


@pytest.mark.parametrize('attack', ['missing_token', 'wrong_token', 'missing_cookie', 'foreign_origin', 'missing_origin', 'wrong_host', 'wrong_action', 'changed_digest'])
def test_csrf_rebinding_and_tampered_action_denied(tmp_path, attack):
    with dashboard(tmp_path) as (server, service, adapter):
        _, headers, page = request(server)
        cookie = headers['Set-Cookie'].split(';')[0]
        form = action_form(page, 'approve')
        origin, host = True, None
        if attack == 'missing_token':
            del form['csrf']
        if attack == 'wrong_token':
            form['csrf'] = '0' * 64
        if attack == 'missing_cookie':
            cookie = None
        if attack == 'foreign_origin':
            origin = 'https://attacker.invalid'
        if attack == 'missing_origin':
            origin = False
        if attack == 'wrong_host':
            host = 'attacker.invalid'
        if attack == 'wrong_action':
            form['action'] = 'simulate'
        if attack == 'changed_digest':
            form['digest'] = '0' * 64
        status = request(server, 'POST', '/action', form, cookie, origin, host)[0]
        assert status in (403, 409)
        assert service.listing()[0]['state'] == 'PROPOSED'
        assert not adapter.submissions


def test_reject_action_and_no_get_side_effects(tmp_path):
    with dashboard(tmp_path) as (server, service, adapter):
        _, headers, page = request(server)
        cookie = headers['Set-Cookie'].split(';')[0]
        assert request(server, 'GET', '/action?approve=all')[0] == 404
        assert request(server, 'POST', '/action', action_form(page, 'reject'), cookie)[0] == 303
        assert service.listing()[0]['state'] == 'REJECTED'
        assert not adapter.submissions


def test_dashboard_dependency_graph_excludes_production_access():
    import ast
    from pathlib import Path
    allowed_roots = {'contextlib', 'dataclasses', 'decimal', 'hashlib', 'hmac', 'json', 'pathlib',
                     'sqlite3', 'time', 'uuid', 'threading', 'argparse', 'datetime', 'html',
                     'http', 're', 'secrets', 'urllib', 'approval', 'simulation'}
    for name in ('approval.py', 'simulation.py', 'approval_dashboard.py'):
        tree = ast.parse(Path(name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split('.')[0] in allowed_roots for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module.split('.')[0] in allowed_roots
        text = Path(name).read_text()
        assert 'load_dotenv' not in text and 'auth_headers' not in text
        assert '/portfolio/orders' not in text


def test_dashboard_displays_original_rules_not_mutated_feed_rules(tmp_path):
    from dataclasses import replace
    with dashboard(tmp_path) as (server, service, adapter):
        row = service.listing()[0]
        ticker = 'SIM-KXSENATE-PA'
        original = service.feed.markets[ticker].rules
        service.feed.markets[ticker] = replace(service.feed.markets[ticker], rules='Unapproved replacement rules')
        _, headers, page = request(server)
        assert html.escape(original) in page
        assert 'Unapproved replacement rules' not in page
        cookie = headers['Set-Cookie'].split(';')[0]
        assert request(server, 'POST', '/action', action_form(page, 'approve'), cookie)[0] == 303
        page = request(server, cookie=cookie)[2]
        assert request(server, 'POST', '/action', action_form(page, 'simulate'), cookie)[0] == 303
        assert service.listing()[0]['state'] == 'BLOCKED'
        assert not adapter.submissions


def test_dashboard_approval_and_submission_with_advancing_clock(tmp_path):
    with dashboard(tmp_path, clock=AdvancingClock()) as (server, service, adapter):
        status, headers, page = request(server)
        assert status == 200
        cookie = headers['Set-Cookie'].split(';')[0]
        assert request(server, 'POST', '/action', action_form(page, 'approve'), cookie)[0] == 303
        assert not adapter.submissions
        page = request(server, cookie=cookie)[2]
        assert 'APPROVED' in page
        assert request(server, 'POST', '/action', action_form(page, 'simulate'), cookie)[0] == 303
        assert 'SIMULATED_FILLED' in request(server, cookie=cookie)[2]
        assert len(adapter.submissions) == 1
