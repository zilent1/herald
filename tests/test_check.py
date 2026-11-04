import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import herald_check as hc
import pytest


def make_status(state='idle', stream_ok=True, missing=None, unreachable=False, tts=True):
    agent = (
        {'ok': False, 'error': 'boom', 'unreachable': True}
        if unreachable
        else {
            'ok': state in ('idle', 'in_call') and not missing,
            'state': state,
            'login_failures': 0,
            'cookies': {'days_to_expiry': 12.3},
            'missing_sinks': missing or [],
        }
    )
    return {
        'nodes': {
            'Node01': {
                'ok': state in ('idle', 'in_call') and stream_ok and not missing,
                'stream_ok': stream_ok,
                'stream_age_s': 0.4,
                'busy': False,
                'agent': agent,
            }
        },
        'engines': {'tts': tts, 'whisper': True, 'speaker_id': False},
    }


def _fake_ok(data):
    def fake(method, url, body=None, timeout=10):
        return 200, data
    return fake


class TestCheckStatus:
    def test_all_ok(self):
        code, lines, data = hc.check_status('http://c', http=_fake_ok(make_status()))
        assert code == hc.OK
        assert any('agent=idle' in l and 'cookies=12.3d' in l for l in lines)

    def test_logged_out(self):
        code, lines, _ = hc.check_status('http://c', http=_fake_ok(make_status(state='logged_out')))
        assert code == hc.SESSION_FAIL
        assert any('agent=logged_out' in l for l in lines)

    def test_unreachable(self):
        code, lines, _ = hc.check_status('http://c', http=_fake_ok(make_status(unreachable=True)))
        assert code == hc.SESSION_FAIL
        assert any('agent=UNREACHABLE' in l for l in lines)

    def test_stream_bad(self):
        code, _, _ = hc.check_status('http://c', http=_fake_ok(make_status(stream_ok=False)))
        assert code == hc.PIPELINE_FAIL

    def test_missing_sinks(self):
        code, lines, _ = hc.check_status('http://c', http=_fake_ok(make_status(missing=['SelftestSink'])))
        assert code == hc.PIPELINE_FAIL
        assert any('missing:SelftestSink' in l for l in lines)

    def test_logged_out_and_stream_bad(self):
        code, _, _ = hc.check_status('http://c', http=_fake_ok(make_status(state='logged_out', stream_ok=False)))
        assert code == hc.SESSION_FAIL

    def test_tts_off(self):
        code, _, _ = hc.check_status('http://c', http=_fake_ok(make_status(tts=False)))
        assert code == hc.PIPELINE_FAIL

    def test_connection_error(self):
        def fake(method, url, body=None, timeout=10):
            raise ConnectionError('refused')
        code, lines, data = hc.check_status('http://c', http=fake)
        assert code == hc.UNREACHABLE
        assert data is None


class TestRunSelftests:
    def test_pass(self):
        data = {'ok': True, 'node': 'Node01', 'duration_ms': 12300,
                'stages': {'wake': {'ok': True, 't_ms': 1234, 'detail': 'ok'}}}
        code, lines, results = hc.run_selftests('http://c', ['Node01'], http=_fake_ok(data))
        assert code == hc.OK
        assert any('PASS (12.3s)' in l for l in lines)

    def test_fail(self):
        data = {'ok': False, 'node': 'Node01', 'duration_ms': 12300,
                'stages': {'wake': {'ok': False, 't_ms': 1234, 'detail': 'no event'}}}
        code, lines, _ = hc.run_selftests('http://c', ['Node01'], http=_fake_ok(data))
        assert code == hc.PIPELINE_FAIL
        assert any('wake' in l and 'FAIL' in l for l in lines)

    def test_busy(self):
        code, lines, _ = hc.run_selftests('http://c', ['Node01'], http=_fake_409)
        assert code == hc.OK
        assert any('SKIPPED' in l for l in lines)

    def test_404(self):
        code, lines, _ = hc.run_selftests('http://c', ['Node01'], http=_fake_404)
        assert code == hc.PIPELINE_FAIL

    def test_timeout_in_body(self):
        captured = {}
        def fake(method, url, body=None, timeout=10):
            captured.update(body or {})
            return 200, {'ok': True, 'node': 'Node01', 'duration_ms': 0, 'stages': {}}
        hc.run_selftests('http://c', ['Node01'], timeout=150, http=fake)
        assert captured['timeout'] == 120


def _fake_409(method, url, body=None, timeout=10):
    return 409, {'error': 'busy'}


def _fake_404(method, url, body=None, timeout=10):
    return 404, {'error': 'unknown node'}


class TestWorst:
    def test_ordering(self):
        assert hc.worst(hc.OK, hc.PIPELINE_FAIL, hc.SESSION_FAIL) == hc.SESSION_FAIL


class TestMain:
    def test_selftest_fail_calls_webhook(self, monkeypatch):
        calls = []
        def fake(method, url, body=None, timeout=10):
            calls.append((method, url, body))
            if url.endswith('/status'):
                return 200, make_status()
            if url.endswith('/selftest'):
                return 500, {'ok': False, 'node': 'Node01', 'duration_ms': 100,
                             'stages': {'wake': {'ok': False, 't_ms': 0, 'detail': 'no'}}}
            return 200, None
        monkeypatch.setattr(hc, 'http_json', fake)
        code = hc.main(['--url', 'http://c', '--selftest', '--webhook', 'http://hook'])
        assert code == hc.PIPELINE_FAIL
        hook_calls = [c for c in calls if c[1] == 'http://hook']
        assert len(hook_calls) == 1
        assert 'text' in hook_calls[0][2]

    def test_selftest_pass_no_webhook(self, monkeypatch):
        calls = []
        def fake(method, url, body=None, timeout=10):
            calls.append((method, url, body))
            if url.endswith('/status'):
                return 200, make_status()
            if url.endswith('/selftest'):
                return 200, {'ok': True, 'node': 'Node01', 'duration_ms': 100,
                             'stages': {'wake': {'ok': True, 't_ms': 0, 'detail': 'ok'}}}
            return 200, None
        monkeypatch.setattr(hc, 'http_json', fake)
        code = hc.main(['--url', 'http://c', '--selftest', '--webhook', 'http://hook'])
        assert code == hc.OK
        hook_calls = [c for c in calls if c[1] == 'http://hook']
        assert len(hook_calls) == 0
