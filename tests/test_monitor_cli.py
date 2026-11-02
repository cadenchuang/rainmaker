"""CLI diagnostics without environment loading, key access, or network calls."""
import asyncio
from io import StringIO

import pytest
from rich.console import Console
import monitor
from market_stream import FatalStreamError


SECRET = 'synthetic-token-signature-private-key-payload'


@pytest.mark.parametrize('exception, expected', [
    (FatalStreamError(SECRET), 'check configuration and authorization'),
    (RuntimeError(SECRET), 'unexpected RuntimeError'),
    (ValueError(SECRET), 'unexpected ValueError'),
    (TypeError(SECRET), 'unexpected TypeError'),
    (OSError(SECRET), 'unexpected OSError'),
    (RecursionError(SECRET), 'unexpected RecursionError'),
])
def test_cli_distinguishes_expected_and_unexpected_errors(monkeypatch, exception, expected):
    output = StringIO()
    monkeypatch.setattr(monitor, 'CONSOLE', Console(file=output, color_system=None))
    calls = []

    def forbidden(*args, **kwargs):
        raise AssertionError('Credential or network access attempted')

    monkeypatch.setattr(monitor, 'load_dotenv', forbidden)
    monkeypatch.setattr(monitor, 'auth_headers', forbidden)
    monkeypatch.setattr(monitor.websockets, 'connect', forbidden)

    async def fail(tickers, record_db=None):
        calls.append((tickers, record_db))
        try:
            raise ValueError('chained-' + SECRET)
        except ValueError as cause:
            raise exception from cause

    monkeypatch.setattr(monitor, 'run', fail)
    with pytest.raises(SystemExit) as stopped:
        monitor.main(['A', 'B', '--record-db', 'unused.sqlite'])
    assert stopped.value.code == 1
    assert stopped.value.__suppress_context__
    text = output.getvalue()
    assert expected in text and SECRET not in text
    if not isinstance(exception, FatalStreamError):
        assert 'configuration' not in text and 'authorization' not in text
    assert calls == [(['A', 'B'], 'unused.sqlite')]


@pytest.mark.parametrize('failure', [FatalStreamError(SECRET), RuntimeError(SECRET)])
def test_signing_provider_errors_keep_their_classification(monkeypatch, failure):
    # Exercise the real run/connect boundary using a fake signing provider only.
    monkeypatch.setattr(monitor, 'load_dotenv', lambda: None)
    monkeypatch.setattr(monitor, 'CONSOLE', Console(file=StringIO(), color_system=None))

    def signing():
        raise failure

    def forbidden(*args, **kwargs):
        raise AssertionError('Network access attempted')

    monkeypatch.setattr(monitor, 'auth_headers', signing)
    monkeypatch.setattr(monitor.websockets, 'connect', forbidden)
    with pytest.raises(type(failure)) as raised:
        asyncio.run(monitor.run(['A']))
    assert raised.value is failure
