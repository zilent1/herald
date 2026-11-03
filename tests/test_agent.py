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
