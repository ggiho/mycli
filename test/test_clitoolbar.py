"""Tests for the enhanced toolbar."""

from unittest.mock import MagicMock, PropertyMock, patch

from mycli.clitoolbar import (
    _build_connection_info,
    _build_server_info,
    _build_ssl_indicator,
    create_toolbar_tokens_func,
)


def _make_mock_mycli(
    user="root",
    host="localhost",
    port=3306,
    dbname="testdb",
    server_info_str="MySQL 8.0.35",
    ssl=None,
    multi_line=False,
    toolbar_error_message=None,
):
    mycli = MagicMock()
    sqle = MagicMock()
    sqle.user = user
    sqle.host = host
    sqle.port = port
    sqle.dbname = dbname
    sqle.ssl = ssl

    si = MagicMock()
    si.__str__ = MagicMock(return_value=server_info_str)
    sqle.server_info = si

    mycli.sqlexecute = sqle
    mycli.multi_line = multi_line
    mycli.toolbar_error_message = toolbar_error_message
    mycli.completion_refresher.is_refreshing.return_value = False
    return mycli


class TestBuildConnectionInfo:
    def test_standard_connection(self):
        mycli = _make_mock_mycli()
        assert _build_connection_info(mycli) == "root@localhost/testdb"

    def test_non_default_port(self):
        mycli = _make_mock_mycli(port=3307)
        assert _build_connection_info(mycli) == "root@localhost:3307/testdb"

    def test_no_user(self):
        mycli = _make_mock_mycli(user="")
        assert _build_connection_info(mycli) == "localhost/testdb"

    def test_no_database(self):
        mycli = _make_mock_mycli(dbname=None)
        assert _build_connection_info(mycli) == "root@localhost/(none)"

    def test_no_sqlexecute(self):
        mycli = MagicMock()
        mycli.sqlexecute = None
        assert _build_connection_info(mycli) == ""

    def test_custom_host_and_port(self):
        mycli = _make_mock_mycli(host="db.example.com", port=13306)
        assert _build_connection_info(mycli) == "root@db.example.com:13306/testdb"


class TestBuildServerInfo:
    def test_with_server_info(self):
        mycli = _make_mock_mycli(server_info_str="MariaDB 10.6.12")
        assert _build_server_info(mycli) == "MariaDB 10.6.12"

    def test_no_sqlexecute(self):
        mycli = MagicMock()
        mycli.sqlexecute = None
        assert _build_server_info(mycli) == ""

    def test_no_server_info(self):
        mycli = _make_mock_mycli()
        mycli.sqlexecute.server_info = None
        assert _build_server_info(mycli) == ""


class TestBuildSSLIndicator:
    def test_ssl_enabled(self):
        mycli = _make_mock_mycli(ssl={"mode": "on"})
        assert _build_ssl_indicator(mycli) == "SSL"

    def test_ssl_disabled(self):
        mycli = _make_mock_mycli(ssl=None)
        assert _build_ssl_indicator(mycli) == ""

    def test_no_sqlexecute(self):
        mycli = MagicMock()
        mycli.sqlexecute = None
        assert _build_ssl_indicator(mycli) == ""


class TestCreateToolbarTokensFunc:
    def test_toolbar_contains_connection_info(self):
        mycli = _make_mock_mycli()
        mycli.prompt_app = None
        get_tokens = create_toolbar_tokens_func(mycli, lambda: False)
        tokens = get_tokens()
        text = "".join(t[1] for t in tokens)
        assert "root@localhost/testdb" in text

    def test_toolbar_contains_server_info(self):
        mycli = _make_mock_mycli()
        mycli.prompt_app = None
        get_tokens = create_toolbar_tokens_func(mycli, lambda: False)
        tokens = get_tokens()
        text = "".join(t[1] for t in tokens)
        assert "MySQL 8.0.35" in text

    def test_toolbar_multiline_on(self):
        mycli = _make_mock_mycli(multi_line=True)
        mycli.prompt_app = None
        get_tokens = create_toolbar_tokens_func(mycli, lambda: False)
        tokens = get_tokens()
        text = "".join(t[1] for t in tokens)
        assert "Multi:ON" in text

    def test_toolbar_multiline_off(self):
        mycli = _make_mock_mycli(multi_line=False)
        mycli.prompt_app = None
        get_tokens = create_toolbar_tokens_func(mycli, lambda: False)
        tokens = get_tokens()
        text = "".join(t[1] for t in tokens)
        assert "Multi:OFF" in text

    def test_toolbar_shows_error_message(self):
        mycli = _make_mock_mycli(toolbar_error_message="Parse error")
        mycli.prompt_app = None
        get_tokens = create_toolbar_tokens_func(mycli, lambda: False)
        tokens = get_tokens()
        text = "".join(t[1] for t in tokens)
        assert "Parse error" in text
        # Error should be cleared after display
        assert mycli.toolbar_error_message is None

    def test_toolbar_shows_refresh_indicator(self):
        mycli = _make_mock_mycli()
        mycli.prompt_app = None
        mycli.completion_refresher.is_refreshing.return_value = True
        mycli.completion_refresher.current_refresher = "tables"
        get_tokens = create_toolbar_tokens_func(mycli, lambda: False)
        tokens = get_tokens()
        text = "".join(t[1] for t in tokens)
        assert "tables" in text

    def test_toolbar_ssl_indicator(self):
        mycli = _make_mock_mycli(ssl={"mode": "on"})
        mycli.prompt_app = None
        get_tokens = create_toolbar_tokens_func(mycli, lambda: False)
        tokens = get_tokens()
        text = "".join(t[1] for t in tokens)
        assert "SSL" in text

    def test_toolbar_fish_help(self):
        mycli = _make_mock_mycli()
        mycli.prompt_app = None
        get_tokens = create_toolbar_tokens_func(mycli, lambda: True)
        tokens = get_tokens()
        text = "".join(t[1] for t in tokens)
        assert "complete" in text
