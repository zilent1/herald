import sys
import os
from unittest.mock import MagicMock

# Insert nodes/ into sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'nodes'))

# Inject stubs before importing agent
for mod in [
    'undetected_chromedriver',
    'selenium',
    'selenium.webdriver',
    'selenium.webdriver.common',
    'selenium.webdriver.common.by',
    'selenium.webdriver.chrome',
    'selenium.webdriver.chrome.options',
]:
    sys.modules[mod] = MagicMock()


import agent


def _write_cookie_file(tmp_path, content):
    f = tmp_path / "auth.txt"
    f.write_text(content)
    return str(f)


class TestLoadNetscapeCookies:
    def test_normal_line(self, tmp_path):
        """Normal line with secure=TRUE and expiry > 0."""
        content = ".google.com\tTRUE\t/\tTRUE\t1999999999\tSID\tABC123\n"
        path = _write_cookie_file(tmp_path, content)
        result = agent.load_netscape_cookies(path)
        assert len(result) == 1
        c = result[0]
        assert c['domain'] == '.google.com'
        assert c['path'] == '/'
        assert c['secure'] is True
        assert c['httpOnly'] is False
        assert c['name'] == 'SID'
        assert c['value'] == 'ABC123'
        assert c['expires'] == 1999999999

    def test_httponly_line(self, tmp_path):
        """#HttpOnly_ prefix -> httpOnly=True, domain kept."""
        content = "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1999999999\tHSID\tXYZ\n"
        path = _write_cookie_file(tmp_path, content)
        result = agent.load_netscape_cookies(path)
        assert len(result) == 1
        c = result[0]
        assert c['domain'] == '.google.com'
        assert c['httpOnly'] is True
        assert c['name'] == 'HSID'

    def test_comment_ignored(self, tmp_path):
        """Plain # comment lines are skipped."""
        content = "# This is a comment\n.google.com\tTRUE\t/\tFALSE\t0\tname\tval\n"
        path = _write_cookie_file(tmp_path, content)
        result = agent.load_netscape_cookies(path)
        assert len(result) == 1
        assert result[0]['name'] == 'name'

    def test_short_line_ignored(self, tmp_path):
        """Lines with fewer than 7 fields are skipped."""
        content = "only\tthree\tfields\n.google.com\tTRUE\t/\tFALSE\t0\tname\tval\n"
        path = _write_cookie_file(tmp_path, content)
        result = agent.load_netscape_cookies(path)
        assert len(result) == 1
        assert result[0]['name'] == 'name'

    def test_expiry_zero_no_expires_key(self, tmp_path):
        """expiry=0 means no 'expires' key in dict."""
        content = ".google.com\tFALSE\t/\tFALSE\t0\tsession\ttok\n"
        path = _write_cookie_file(tmp_path, content)
        result = agent.load_netscape_cookies(path)
        assert len(result) == 1
        assert 'expires' not in result[0]
        assert result[0]['secure'] is False

    def test_empty_value(self, tmp_path):
        """Empty value is accepted."""
        content = ".google.com\tFALSE\t/\tFALSE\t0\tempty\t\n"
        path = _write_cookie_file(tmp_path, content)
        result = agent.load_netscape_cookies(path)
        assert len(result) == 1
        assert result[0]['value'] == ''

    def test_missing_file(self, tmp_path):
        """Non-existent file returns []."""
        result = agent.load_netscape_cookies(str(tmp_path / "nonexistent.txt"))
        assert result == []


class TestInjectCookies:
    def test_all_success(self):
        driver = MagicMock()
        cookies = [
            {'domain': '.a.com', 'path': '/', 'secure': True, 'httpOnly': False, 'name': 'n1', 'value': 'v1'},
            {'domain': '.b.com', 'path': '/', 'secure': False, 'httpOnly': True, 'name': 'n2', 'value': 'v2'},
        ]
        count = agent.inject_cookies(driver, cookies)
        assert count == 2
        # Network.enable called
        driver.execute_cdp_cmd.assert_any_call('Network.enable', {})
        # Network.setCookie called for each
        set_cookie_calls = [
            call for call in driver.execute_cdp_cmd.call_args_list
            if call[0][0] == 'Network.setCookie'
        ]
        assert len(set_cookie_calls) == 2

    def test_partial_failure(self):
        """One cookie raises; count reflects only successes, all cookies attempted."""
        driver = MagicMock()
        cookies = [
            {'domain': '.a.com', 'path': '/', 'secure': True, 'httpOnly': False, 'name': 'ok', 'value': 'v'},
            {'domain': '.b.com', 'path': '/', 'secure': False, 'httpOnly': False, 'name': 'bad', 'value': 'v'},
            {'domain': '.c.com', 'path': '/', 'secure': False, 'httpOnly': False, 'name': 'ok2', 'value': 'v'},
        ]

        def side_effect(cmd, params):
            if cmd == 'Network.setCookie' and params.get('name') == 'bad':
                raise Exception("cookie rejected")

        driver.execute_cdp_cmd.side_effect = side_effect
        count = agent.inject_cookies(driver, cookies)
        assert count == 2
        # All 3 cookies were attempted
        set_cookie_calls = [
            call for call in driver.execute_cdp_cmd.call_args_list
            if call[0][0] == 'Network.setCookie'
        ]
        assert len(set_cookie_calls) == 3


class TestIsLoggedIn:
    def test_accounts_google(self):
        assert agent.is_logged_out('https://accounts.google.com/v3/signin/identifier') is True

    def test_account_about(self):
        assert agent.is_logged_out('https://www.google.com/account/about/') is True

    def test_meet_url(self):
        assert agent.is_logged_out('https://meet.google.com/calling/') is False

    def test_none(self):
        assert agent.is_logged_out(None) is False


class TestEnsureSession:
    def test_retries_then_succeeds(self, monkeypatch):
        from unittest.mock import PropertyMock
        driver = MagicMock()
        type(driver).current_url = PropertyMock(side_effect=[
            'https://accounts.google.com/v3/signin/identifier',
            'https://meet.google.com/calling/'
        ])
        monkeypatch.setattr(agent.time, 'sleep', lambda s: None)
        monkeypatch.setattr(agent, 'COOKIE_FILE', None)
        agent.ensure_session(driver)
        assert driver.get.call_count == 2


import time
import pytest

@pytest.fixture
def reset_state():
    saved = dict(agent.STATE)
    yield
    agent.STATE.clear()
    agent.STATE.update(saved)


class TestSetState:
    def test_set_state_since(self, reset_state):
        agent.STATE['state'] = 'pre_x1'
        agent.set_state('x1')
        since1 = agent.STATE['since']
        time.sleep(0.01)
        agent.set_state('x1')
        assert agent.STATE['since'] == since1


class TestEnsureSessionTransitions:
    def test_ensure_session_transitions(self, monkeypatch, reset_state):
        class FakeDriver:
            def __init__(self):
                self.urls = ['https://accounts.google.com/signin', 'https://meet.google.com/calling/']
            def get(self, url):
                pass
            @property
            def current_url(self):
                url = self.urls[0]
                if len(self.urls) > 1:
                    self.urls.pop(0)
                return url

        monkeypatch.setattr(agent.time, 'sleep', lambda s: None)
        monkeypatch.setattr(agent, 'COOKIE_FILE', None)

        recorded = []
        original_set_state = agent.set_state
        def recorder(name):
            recorded.append(name)
            original_set_state(name)
        monkeypatch.setattr(agent, 'set_state', recorder)

        agent.ensure_session(FakeDriver())
        assert agent.STATE['state'] == 'idle'
        assert agent.STATE['login_failures'] == 0
        assert agent.STATE['last_login_ok'] is not None
        assert recorded == ['logging_in', 'logged_out', 'logging_in', 'idle']


class TestMaybeRecheckSession:
    def test_recheck_skips_when_not_idle(self, reset_state):
        agent.STATE['state'] = 'logged_out'
        driver = MagicMock()
        result = agent.maybe_recheck_session(driver)
        assert result is False
        driver.refresh.assert_not_called()

    def test_recheck_skips_when_recent(self, monkeypatch, reset_state):
        agent.STATE['state'] = 'idle'
        agent.STATE['last_session_check'] = time.time() - 10
        driver = MagicMock()
        result = agent.maybe_recheck_session(driver)
        assert result is False
        driver.refresh.assert_not_called()

    def test_recheck_detects_logout(self, monkeypatch, reset_state):
        agent.STATE['state'] = 'idle'
        agent.STATE['last_session_check'] = None
        driver = MagicMock(current_url='https://accounts.google.com/x')
        monkeypatch.setattr(agent.time, 'sleep', lambda s: None)
        monkeypatch.setattr(agent, 'ensure_session', MagicMock())
        result = agent.maybe_recheck_session(driver)
        assert result is True
        driver.refresh.assert_called_once()
        agent.ensure_session.assert_called_with(driver)
        assert agent.STATE['state'] == 'logged_out'

    def test_recheck_ok(self, monkeypatch, reset_state):
        agent.STATE['state'] = 'idle'
        agent.STATE['last_session_check'] = None
        driver = MagicMock(current_url='https://meet.google.com/calling/')
        monkeypatch.setattr(agent.time, 'sleep', lambda s: None)
        result = agent.maybe_recheck_session(driver)
        assert result is False
        assert agent.STATE['last_session_check'] is not None


import types
import json
import urllib.request
import urllib.error


class TestCookieSummary:
    def test_cookie_summary_key_cookie(self, tmp_path):
        content = ('.google.com\tTRUE\t/\tTRUE\t' + str(int(time.time() + 10 * 86400)) + '\tSID\tX\n'
                   '.google.com\tTRUE\t/\tTRUE\t' + str(int(time.time() + 99 * 86400)) + '\tNID\tY\n')
        path = _write_cookie_file(tmp_path, content)
        result = agent.cookie_summary(path)
        assert result['exists'] is True
        assert 9.9 <= result['days_to_expiry'] <= 10.1
        assert result['key_cookies'] == ['SID']
        assert result['age_days'] >= 0

    def test_cookie_summary_none(self):
        result = agent.cookie_summary(None)
        assert result['exists'] is False
        assert result['key_cookies'] == []


class TestAudioSinks:
    def test_audio_sinks_parse(self, monkeypatch):
        monkeypatch.setattr(agent.subprocess, 'run',
            lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout='0\tSpeakerOutput\tmodule-null-sink.c\ts16le 2ch 44100Hz\tIDLE\n1\tVirtualMicSink\tmodule-null-sink.c\ts16le 2ch 44100Hz\tIDLE\n'))
        assert agent.audio_sinks() == ['SpeakerOutput', 'VirtualMicSink']
        monkeypatch.setattr(agent.subprocess, 'run',
            lambda *a, **kw: types.SimpleNamespace(returncode=1, stdout=''))
        assert agent.audio_sinks() == []

        def _raise(*a, **kw):
            raise FileNotFoundError
        monkeypatch.setattr(agent.subprocess, 'run', _raise)
        assert agent.audio_sinks() == []


class TestBuildStatus:
    def test_build_status(self, monkeypatch, reset_state):
        monkeypatch.setattr(agent, 'audio_sinks', lambda: ['SpeakerOutput', 'VirtualMicSink'])
        agent.STATE['state'] = 'idle'
        s = agent.build_status()
        assert s['missing_sinks'] == ['SelftestSink']
        assert s['audio_ok'] is False
        assert s['session_ok'] is True
        assert s['ok'] is False
        monkeypatch.setattr(agent, 'audio_sinks', lambda: ['SpeakerOutput', 'VirtualMicSink', 'SelftestSink'])
        s = agent.build_status()
        assert s['ok'] is True
        agent.STATE['state'] = 'logged_out'
        s = agent.build_status()
        assert s['ok'] is False


class TestStatusHttp:
    def test_status_http(self, monkeypatch):
        monkeypatch.setattr(agent, 'build_status', lambda: {'node': 'NodeT', 'audio_ok': False})
        server = agent.start_status_server(port=0)
        port = server.server_address[1]
        try:
            resp = urllib.request.urlopen('http://127.0.0.1:%d/status' % port)
            assert resp.status == 200
            assert json.loads(resp.read())['node'] == 'NodeT'
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen('http://127.0.0.1:%d/health' % port)
            assert exc_info.value.code == 503
            with pytest.raises(urllib.error.HTTPError) as exc_info2:
                urllib.request.urlopen('http://127.0.0.1:%d/nope' % port)
            assert exc_info2.value.code == 404
        finally:
            server.shutdown()
            server.server_close()


class TestChromeMajorVersion:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv('CHROME_VERSION_MAIN', '156')
        assert agent.chrome_major_version() == 156

    def test_parse_version_string(self, monkeypatch):
        monkeypatch.delenv('CHROME_VERSION_MAIN', raising=False)
        monkeypatch.setattr(agent.subprocess, 'run',
            lambda *a, **kw: types.SimpleNamespace(stdout='Google Chrome 156.0.8078.79\n'))
        assert agent.chrome_major_version() == 156

    def test_exception_returns_none(self, monkeypatch):
        monkeypatch.delenv('CHROME_VERSION_MAIN', raising=False)
        def _raise(*a, **kw):
            raise FileNotFoundError
        monkeypatch.setattr(agent.subprocess, 'run', _raise)
        assert agent.chrome_major_version() is None
