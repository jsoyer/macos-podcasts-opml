import csv
import io
import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock, call, patch

import pytest

import macos_podcasts_opml as m

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

Row = Tuple[Optional[str], Optional[str], Optional[str], Optional[float]]

COREDATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def dt_to_coredata(dt: datetime) -> float:
    return (dt - COREDATA_EPOCH).total_seconds()


def make_db(rows: List[Row]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE ZMTPODCAST "
        "(ZTITLE TEXT, ZFEEDURL TEXT, ZWEBPAGEURL TEXT, ZLASTPUBLISHDATE REAL)"
    )
    conn.executemany("INSERT INTO ZMTPODCAST VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    return conn


def make_db_no_date(
    rows: List[Tuple[Optional[str], Optional[str], Optional[str]]]
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE ZMTPODCAST (ZTITLE TEXT, ZFEEDURL TEXT, ZWEBPAGEURL TEXT)")
    conn.executemany("INSERT INTO ZMTPODCAST VALUES (?, ?, ?)", rows)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Podcast dataclass
# ---------------------------------------------------------------------------


def test_podcast_is_immutable() -> None:
    p = m.Podcast(title="Test", feed_url="https://feed.example", website_url="")
    with pytest.raises(Exception):
        p.title = "Other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# coredata_to_datetime
# ---------------------------------------------------------------------------


def test_coredata_to_datetime_known_value() -> None:
    result = m.coredata_to_datetime(0.0)
    assert result is not None
    assert result.year == 2001
    assert result.month == 1
    assert result.day == 1


def test_coredata_to_datetime_none() -> None:
    assert m.coredata_to_datetime(None) is None


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


def test_parse_duration_years() -> None:
    assert m.parse_duration("1y") == timedelta(days=365)
    assert m.parse_duration("2y") == timedelta(days=730)


def test_parse_duration_months() -> None:
    assert m.parse_duration("6m") == timedelta(days=180)


def test_parse_duration_days() -> None:
    assert m.parse_duration("90d") == timedelta(days=90)


def test_parse_duration_invalid() -> None:
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        m.parse_duration("2w")

    with pytest.raises(argparse.ArgumentTypeError):
        m.parse_duration("abc")


# ---------------------------------------------------------------------------
# table_columns / detect_date_column
# ---------------------------------------------------------------------------


def test_table_columns_returns_names() -> None:
    conn = make_db([])
    cols = m.table_columns(conn, "ZMTPODCAST")
    assert "ZTITLE" in cols
    assert "ZLASTPUBLISHDATE" in cols


def test_detect_date_column_finds_first_candidate() -> None:
    assert m.detect_date_column(["ZTITLE", "ZLASTPUBLISHDATE", "ZCREATIONDATE"]) == "ZLASTPUBLISHDATE"


def test_detect_date_column_fallback() -> None:
    assert m.detect_date_column(["ZTITLE", "ZLASTDOWNLOADDATE"]) == "ZLASTDOWNLOADDATE"


def test_detect_date_column_none_when_absent() -> None:
    assert m.detect_date_column(["ZTITLE", "ZFEEDURL"]) is None


# ---------------------------------------------------------------------------
# get_podcasts
# ---------------------------------------------------------------------------


def test_get_podcasts_returns_all_rows() -> None:
    conn = make_db(
        [
            ("Pod A", "https://a.example/feed", "https://a.example", None),
            ("Pod B", "https://b.example/feed", "https://b.example", None),
        ]
    )
    result = list(m.get_podcasts(conn))
    assert len(result) == 2
    assert result[0].title == "Pod A"
    assert result[1].feed_url == "https://b.example/feed"


def test_get_podcasts_converts_none_to_empty_string() -> None:
    conn = make_db([(None, None, None, None)])
    result = list(m.get_podcasts(conn))
    assert result[0].title == ""
    assert result[0].feed_url == ""
    assert result[0].website_url == ""


def test_get_podcasts_subscribed_only_filters_missing_feed_url() -> None:
    conn = make_db(
        [
            ("Pod A", "https://a.example/feed", "https://a.example", None),
            ("Pod B", None, "https://b.example", None),
            ("Pod C", "", "https://c.example", None),
        ]
    )
    result = list(m.get_podcasts(conn, subscribed_only=True))
    assert len(result) == 1
    assert result[0].title == "Pod A"


def test_get_podcasts_includes_date_when_column_provided() -> None:
    pub = datetime(2022, 6, 1, tzinfo=timezone.utc)
    conn = make_db([("Pod A", "https://a.example/feed", "", dt_to_coredata(pub))])
    result = list(m.get_podcasts(conn, date_column="ZLASTPUBLISHDATE"))
    assert result[0].last_date is not None
    assert result[0].last_date.year == 2022
    assert result[0].last_date.month == 6
    assert result[0].date_source == "ZLASTPUBLISHDATE"


def test_get_podcasts_raises_on_missing_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with pytest.raises(SystemExit, match="schema may have changed"):
        list(m.get_podcasts(conn))


# ---------------------------------------------------------------------------
# filter_stale
# ---------------------------------------------------------------------------


def test_filter_stale_returns_only_old_podcasts() -> None:
    now = datetime.now(tz=timezone.utc)
    podcasts: Iterator[m.Podcast] = iter(
        [
            m.Podcast("Old", "https://old.example/feed", "", last_date=now - timedelta(days=800)),
            m.Podcast("Recent", "https://recent.example/feed", "", last_date=now - timedelta(days=10)),
        ]
    )
    cutoff = now - timedelta(days=365)
    result = list(m.filter_stale(podcasts, cutoff))
    assert len(result) == 1
    assert result[0].title == "Old"


def test_filter_stale_skips_unknown_date() -> None:
    now = datetime.now(tz=timezone.utc)
    podcasts: Iterator[m.Podcast] = iter(
        [m.Podcast("No date", "https://x.example/feed", "", last_date=None)]
    )
    result = list(m.filter_stale(podcasts, now - timedelta(days=365)))
    assert result == []


# ---------------------------------------------------------------------------
# format_as_opml
# ---------------------------------------------------------------------------


def _parse_opml(xml_str: str) -> ET.Element:
    return ET.fromstring(xml_str)


def test_format_as_opml_structure() -> None:
    podcasts: Iterator[m.Podcast] = iter(
        [m.Podcast(title="Test Pod", feed_url="https://feed.test", website_url="https://test.com")]
    )
    result = m.format_as_opml(podcasts)
    root = _parse_opml(result)
    assert root.tag == "opml"
    assert root.attrib["version"] == "1.0"
    head = root.find("head")
    assert head is not None
    title_el = head.find("title")
    assert title_el is not None
    assert title_el.text == "MacOS Podcasts"
    body = root.find("body")
    assert body is not None
    outlines = body.findall("outline")
    assert len(outlines) == 1
    assert outlines[0].attrib["type"] == "rss"
    assert outlines[0].attrib["text"] == "Test Pod"
    assert outlines[0].attrib["xmlUrl"] == "https://feed.test"
    assert outlines[0].attrib["htmlUrl"] == "https://test.com"


def test_format_as_opml_empty_input() -> None:
    result = m.format_as_opml(iter([]))
    body = _parse_opml(result).find("body")
    assert body is not None
    assert list(body) == []


def test_format_as_opml_skips_empty_feed_url(capsys: pytest.CaptureFixture[str]) -> None:
    podcasts: Iterator[m.Podcast] = iter(
        [
            m.Podcast(title="No Feed", feed_url="", website_url=""),
            m.Podcast(title="Has Feed", feed_url="https://feed.ok", website_url=""),
        ]
    )
    result = m.format_as_opml(podcasts)
    body = _parse_opml(result).find("body")
    assert body is not None
    outlines = body.findall("outline")
    assert len(outlines) == 1
    assert outlines[0].attrib["text"] == "Has Feed"
    assert "No Feed" in capsys.readouterr().err


def test_format_as_opml_custom_title() -> None:
    result = m.format_as_opml(iter([]), title="My Export")
    head = _parse_opml(result).find("head")
    assert head is not None
    title_el = head.find("title")
    assert title_el is not None
    assert title_el.text == "My Export"


# ---------------------------------------------------------------------------
# format_as_json
# ---------------------------------------------------------------------------


def test_format_as_json_output() -> None:
    podcasts: Iterator[m.Podcast] = iter(
        [m.Podcast(title="Pod A", feed_url="https://a.example/feed", website_url="https://a.example")]
    )
    data = json.loads(m.format_as_json(podcasts))
    assert len(data) == 1
    assert data[0]["title"] == "Pod A"
    assert data[0]["feedUrl"] == "https://a.example/feed"
    assert data[0]["websiteUrl"] == "https://a.example"


def test_format_as_json_includes_date_when_requested() -> None:
    dt = datetime(2022, 3, 15, tzinfo=timezone.utc)
    podcasts: Iterator[m.Podcast] = iter(
        [m.Podcast("Pod", "https://feed.x", "", last_date=dt, date_source="ZLASTPUBLISHDATE")]
    )
    data = json.loads(m.format_as_json(podcasts, include_date=True))
    assert data[0]["lastDate"] == "2022-03-15"
    assert data[0]["dateSource"] == "ZLASTPUBLISHDATE"


def test_format_as_json_skips_empty_feed_url() -> None:
    podcasts: Iterator[m.Podcast] = iter(
        [
            m.Podcast(title="No Feed", feed_url="", website_url=""),
            m.Podcast(title="Has Feed", feed_url="https://feed.ok", website_url=""),
        ]
    )
    data = json.loads(m.format_as_json(podcasts))
    assert len(data) == 1
    assert data[0]["title"] == "Has Feed"


# ---------------------------------------------------------------------------
# format_as_csv
# ---------------------------------------------------------------------------


def test_format_as_csv_output() -> None:
    podcasts: Iterator[m.Podcast] = iter(
        [m.Podcast(title="Pod A", feed_url="https://a.example/feed", website_url="https://a.example")]
    )
    rows = list(csv.reader(io.StringIO(m.format_as_csv(podcasts))))
    assert rows[0] == ["title", "feedUrl", "websiteUrl"]
    assert rows[1] == ["Pod A", "https://a.example/feed", "https://a.example"]


def test_format_as_csv_includes_date_when_requested() -> None:
    dt = datetime(2022, 3, 15, tzinfo=timezone.utc)
    podcasts: Iterator[m.Podcast] = iter(
        [m.Podcast("Pod", "https://feed.x", "", last_date=dt, date_source="ZLASTPUBLISHDATE")]
    )
    rows = list(csv.reader(io.StringIO(m.format_as_csv(podcasts, include_date=True))))
    assert rows[0] == ["title", "feedUrl", "websiteUrl", "lastDate", "dateSource"]
    assert rows[1][3] == "2022-03-15"
    assert rows[1][4] == "ZLASTPUBLISHDATE"


def test_format_as_csv_skips_empty_feed_url() -> None:
    podcasts: Iterator[m.Podcast] = iter(
        [
            m.Podcast(title="No Feed", feed_url="", website_url=""),
            m.Podcast(title="Has Feed", feed_url="https://feed.ok", website_url=""),
        ]
    )
    rows = list(csv.reader(io.StringIO(m.format_as_csv(podcasts))))
    assert len(rows) == 2  # header + 1 data row
    assert rows[1][0] == "Has Feed"


# ---------------------------------------------------------------------------
# backup_database
# ---------------------------------------------------------------------------


def test_backup_database_creates_copy(tmp_path: Path) -> None:
    db_file = tmp_path / "MTLibrary.sqlite"
    db_file.write_bytes(b"fake sqlite content")
    backup = m.backup_database(db_file)
    assert backup.exists()
    assert backup != db_file
    assert backup.read_bytes() == b"fake sqlite content"
    assert "backup-" in backup.name


# ---------------------------------------------------------------------------
# unsubscribe_from_db
# ---------------------------------------------------------------------------


def make_file_db(path: Path, rows: List[Tuple[Optional[str], Optional[str], Optional[str]]]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE ZMTPODCAST (ZTITLE TEXT, ZFEEDURL TEXT, ZWEBPAGEURL TEXT)")
    conn.executemany("INSERT INTO ZMTPODCAST VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


def test_unsubscribe_dry_run_does_not_modify(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    make_file_db(db, [
        ("Pod A", "https://a.example/feed", ""),
        ("Pod B", "https://b.example/feed", ""),
    ])
    count = m.unsubscribe_from_db(db, ["https://a.example/feed"], dry_run=True)
    assert count == 1
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT COUNT(*) FROM ZMTPODCAST").fetchone()
    conn.close()
    assert rows[0] == 2


def test_unsubscribe_confirm_deletes_rows(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    make_file_db(db, [
        ("Pod A", "https://a.example/feed", ""),
        ("Pod B", "https://b.example/feed", ""),
        ("Pod C", "https://c.example/feed", ""),
    ])
    removed = m.unsubscribe_from_db(
        db, ["https://a.example/feed", "https://c.example/feed"], dry_run=False
    )
    assert removed == 2
    conn = sqlite3.connect(str(db))
    remaining = [r[0] for r in conn.execute("SELECT ZFEEDURL FROM ZMTPODCAST")]
    conn.close()
    assert remaining == ["https://b.example/feed"]


def test_unsubscribe_confirm_creates_backup(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    make_file_db(db, [("Pod A", "https://a.example/feed", "")])
    before = set(tmp_path.iterdir())
    m.unsubscribe_from_db(db, ["https://a.example/feed"], dry_run=False)
    after = set(tmp_path.iterdir())
    new_files = after - before
    assert len(new_files) == 1
    assert "backup-" in list(new_files)[0].name


def test_unsubscribe_empty_list_returns_zero(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    make_file_db(db, [("Pod A", "https://a.example/feed", "")])
    assert m.unsubscribe_from_db(db, [], dry_run=True) == 0
    assert m.unsubscribe_from_db(db, [], dry_run=False) == 0


# ---------------------------------------------------------------------------
# is_podcasts_running
# ---------------------------------------------------------------------------


def test_is_podcasts_running_false_when_not_found() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        assert m.is_podcasts_running() is False


def test_is_podcasts_running_true_when_found() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        assert m.is_podcasts_running() is True


def test_is_podcasts_running_false_when_pgrep_missing() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert m.is_podcasts_running() is False


# ---------------------------------------------------------------------------
# cmd_unsubscribe
# ---------------------------------------------------------------------------


def test_cmd_unsubscribe_dry_run_does_not_require_app_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "db.sqlite"
    make_file_db(db, [("Pod A", "https://a.example/feed", "")])
    podcasts = [m.Podcast("Pod A", "https://a.example/feed", "")]
    with patch("macos_podcasts_opml.is_podcasts_running", return_value=True):
        m.cmd_unsubscribe(db, podcasts, confirm=False)
    captured = capsys.readouterr()
    assert "Dry-run" in captured.err
    assert "--confirm" in captured.err


def test_cmd_unsubscribe_confirm_blocked_when_app_running(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    make_file_db(db, [("Pod A", "https://a.example/feed", "")])
    podcasts = [m.Podcast("Pod A", "https://a.example/feed", "")]
    with patch("macos_podcasts_opml.is_podcasts_running", return_value=True):
        with pytest.raises(SystemExit, match="Podcasts.app is currently running"):
            m.cmd_unsubscribe(db, podcasts, confirm=True)


def test_cmd_unsubscribe_confirm_executes_when_app_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "db.sqlite"
    make_file_db(db, [
        ("Pod A", "https://a.example/feed", ""),
        ("Pod B", "https://b.example/feed", ""),
    ])
    podcasts = [m.Podcast("Pod A", "https://a.example/feed", "")]
    with patch("macos_podcasts_opml.is_podcasts_running", return_value=False):
        m.cmd_unsubscribe(db, podcasts, confirm=True)
    captured = capsys.readouterr()
    assert "1 podcast(s) removed" in captured.err
    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM ZMTPODCAST").fetchone()[0]
    conn.close()
    assert count == 1


# ---------------------------------------------------------------------------
# check_feed_url
# ---------------------------------------------------------------------------


def _make_http_resp_mock(status: int, body: bytes = b"") -> MagicMock:
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.status = status
    ctx.read.return_value = body
    return ctx


def test_check_feed_url_ok_returns_none() -> None:
    resp = _make_http_resp_mock(200)
    with patch("urllib.request.urlopen", return_value=resp):
        assert m.check_feed_url("https://good.example/feed") is None


def test_check_feed_url_404_returns_error() -> None:
    import urllib.error as _ue

    err = _ue.HTTPError("https://bad.example/feed", 404, "Not Found", {}, None)  # type: ignore[arg-type]
    with patch("urllib.request.urlopen", side_effect=err):
        result = m.check_feed_url("https://bad.example/feed")
    assert result == "HTTP 404"


def test_check_feed_url_timeout_returns_error() -> None:
    import urllib.error as _ue

    err = _ue.URLError("timed out")
    with patch("urllib.request.urlopen", side_effect=err):
        result = m.check_feed_url("https://slow.example/feed")
    assert result is not None
    assert "URLError" in result


# ---------------------------------------------------------------------------
# cmd_broken
# ---------------------------------------------------------------------------


def test_cmd_broken_reports_broken_feeds(capsys: pytest.CaptureFixture[str]) -> None:
    podcasts = [
        m.Podcast("Good Pod", "https://good.example/feed", ""),
        m.Podcast("Bad Pod", "https://bad.example/feed", ""),
    ]

    def mock_check(url: str, timeout: int = 10) -> Optional[str]:
        return "HTTP 404" if "bad" in url else None

    with patch("macos_podcasts_opml.check_feed_url", side_effect=mock_check):
        m.cmd_broken(podcasts, workers=2)

    captured = capsys.readouterr()
    assert "HTTP 404" in captured.out
    assert "Bad Pod" in captured.out
    assert "1 broken" in captured.out


def test_cmd_broken_all_ok(capsys: pytest.CaptureFixture[str]) -> None:
    podcasts = [m.Podcast("Good", "https://good.example/feed", "")]

    with patch("macos_podcasts_opml.check_feed_url", return_value=None):
        m.cmd_broken(podcasts, workers=1)

    assert "reachable" in capsys.readouterr().err


def test_cmd_broken_skips_no_feed_url(capsys: pytest.CaptureFixture[str]) -> None:
    podcasts = [m.Podcast("No URL", "", "")]
    with patch("macos_podcasts_opml.check_feed_url") as mock_check:
        m.cmd_broken(podcasts)
    mock_check.assert_not_called()
    assert "No podcasts" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_stats
# ---------------------------------------------------------------------------


def test_cmd_stats_basic(capsys: pytest.CaptureFixture[str]) -> None:
    now = datetime.now(tz=timezone.utc)
    podcasts = [
        m.Podcast("Recent", "https://a.example/feed", "", last_date=now - timedelta(days=10)),
        m.Podcast("Old", "https://b.example/feed", "", last_date=now - timedelta(days=800)),
        m.Podcast("NoDate", "https://c.example/feed", ""),
    ]
    m.cmd_stats(podcasts, "ZLASTPUBLISHDATE")
    out = capsys.readouterr().out
    assert "Total podcasts  : 3" in out
    assert "No date" in out


def test_cmd_stats_empty(capsys: pytest.CaptureFixture[str]) -> None:
    m.cmd_stats([], None)
    assert "No podcasts" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# parse_opml_feeds
# ---------------------------------------------------------------------------

SAMPLE_OPML = """\
<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head><title>Test</title></head>
  <body>
    <outline type="rss" text="Pod A" xmlUrl="https://a.example/feed" htmlUrl="https://a.example"/>
    <outline type="rss" text="Pod B" xmlUrl="https://b.example/feed" htmlUrl="https://b.example"/>
  </body>
</opml>
"""


def test_parse_opml_feeds_returns_mapping(tmp_path: Path) -> None:
    opml_file = tmp_path / "test.opml"
    opml_file.write_text(SAMPLE_OPML, encoding="utf-8")
    feeds = m.parse_opml_feeds(opml_file)
    assert len(feeds) == 2
    assert feeds["https://a.example/feed"] == "Pod A"
    assert feeds["https://b.example/feed"] == "Pod B"


def test_parse_opml_feeds_invalid_raises(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.opml"
    bad_file.write_text("not xml at all", encoding="utf-8")
    with pytest.raises(SystemExit, match="Failed to parse"):
        m.parse_opml_feeds(bad_file)


# ---------------------------------------------------------------------------
# cmd_diff
# ---------------------------------------------------------------------------


def test_cmd_diff_identical(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    opml_file = tmp_path / "test.opml"
    opml_file.write_text(SAMPLE_OPML, encoding="utf-8")
    podcasts = [
        m.Podcast("Pod A", "https://a.example/feed", ""),
        m.Podcast("Pod B", "https://b.example/feed", ""),
    ]
    m.cmd_diff(podcasts, opml_file)
    assert "No differences" in capsys.readouterr().out


def test_cmd_diff_only_in_apple(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    opml_file = tmp_path / "test.opml"
    opml_file.write_text(SAMPLE_OPML, encoding="utf-8")
    podcasts = [
        m.Podcast("Pod A", "https://a.example/feed", ""),
        m.Podcast("Pod B", "https://b.example/feed", ""),
        m.Podcast("Pod C", "https://c.example/feed", ""),
    ]
    m.cmd_diff(podcasts, opml_file)
    out = capsys.readouterr().out
    assert "Only in Apple Podcasts" in out
    assert "Pod C" in out


def test_cmd_diff_only_in_opml(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    opml_file = tmp_path / "test.opml"
    opml_file.write_text(SAMPLE_OPML, encoding="utf-8")
    podcasts = [m.Podcast("Pod A", "https://a.example/feed", "")]
    m.cmd_diff(podcasts, opml_file)
    out = capsys.readouterr().out
    assert "Only in OPML" in out
    assert "Pod B" in out


# ---------------------------------------------------------------------------
# Pocket Casts API helpers
# ---------------------------------------------------------------------------


def _make_urlopen_mock(response_data: Any) -> MagicMock:
    """Return a mock suitable for patching urllib.request.urlopen."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.read.return_value = json.dumps(response_data).encode("utf-8")
    return ctx


def _pc_subs_response(podcasts: List[Dict[str, str]]) -> Dict[str, Any]:
    return {"podcasts": podcasts}


# ---------------------------------------------------------------------------
# pc_login
# ---------------------------------------------------------------------------


def test_pc_login_returns_token() -> None:
    mock_resp = _make_urlopen_mock({"token": "abc123"})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        token = m.pc_login("user@example.com", "secret")
    assert token == "abc123"


def test_pc_login_raises_on_missing_token() -> None:
    mock_resp = _make_urlopen_mock({"error": "invalid credentials"})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        with pytest.raises(SystemExit, match="login failed"):
            m.pc_login("user@example.com", "wrong")


# ---------------------------------------------------------------------------
# pc_list_subscriptions
# ---------------------------------------------------------------------------


def test_pc_list_subscriptions_returns_podcasts() -> None:
    mock_resp = _make_urlopen_mock(_pc_subs_response([
        {"uuid": "uuid-1", "url": "https://a.example/feed", "title": "Pod A"},
        {"uuid": "uuid-2", "url": "https://b.example/feed", "title": "Pod B"},
    ]))
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = m.pc_list_subscriptions("token")
    assert len(result) == 2
    assert result[0].uuid == "uuid-1"
    assert result[0].feed_url == "https://a.example/feed"
    assert result[1].title == "Pod B"


def test_pc_list_subscriptions_skips_incomplete_rows() -> None:
    mock_resp = _make_urlopen_mock(_pc_subs_response([
        {"uuid": "", "url": "https://a.example/feed", "title": "No UUID"},
        {"uuid": "uuid-2", "url": "", "title": "No URL"},
        {"uuid": "uuid-3", "url": "https://c.example/feed", "title": "Valid"},
    ]))
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = m.pc_list_subscriptions("token")
    assert len(result) == 1
    assert result[0].uuid == "uuid-3"


def test_pc_list_subscriptions_empty() -> None:
    mock_resp = _make_urlopen_mock({"podcasts": []})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert m.pc_list_subscriptions("token") == []


# ---------------------------------------------------------------------------
# cmd_sync_pocketcasts
# ---------------------------------------------------------------------------


def _setup_sync_mocks(
    pc_podcasts: List[Dict[str, str]],
) -> MagicMock:
    login_resp = _make_urlopen_mock({"token": "test-token"})
    list_resp = _make_urlopen_mock(_pc_subs_response(pc_podcasts))
    subscribe_resp = _make_urlopen_mock({})
    mock = MagicMock(side_effect=[login_resp, list_resp] + [subscribe_resp] * 20)
    return mock


def test_sync_dry_run_does_not_call_subscribe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock = _setup_sync_mocks([])
    with patch("urllib.request.urlopen", mock):
        m.cmd_sync_pocketcasts(
            apple_feeds=["https://a.example/feed"],
            email="u@example.com",
            password="pw",
            sync_remove=False,
            confirm=False,
        )
    assert mock.call_count == 2
    captured = capsys.readouterr()
    assert "Dry-run" in captured.err
    assert "https://a.example/feed" in captured.err


def test_sync_confirm_calls_subscribe_for_new_feeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock = _setup_sync_mocks([])
    with patch("urllib.request.urlopen", mock):
        with patch("time.sleep"):
            m.cmd_sync_pocketcasts(
                apple_feeds=["https://a.example/feed", "https://b.example/feed"],
                email="u@example.com",
                password="pw",
                sync_remove=False,
                confirm=True,
            )
    assert mock.call_count == 4
    captured = capsys.readouterr()
    assert "2 added" in captured.err


def test_sync_skips_already_subscribed_feeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock = _setup_sync_mocks([
        {"uuid": "uuid-1", "url": "https://a.example/feed", "title": "Pod A"},
    ])
    with patch("urllib.request.urlopen", mock):
        with patch("time.sleep"):
            m.cmd_sync_pocketcasts(
                apple_feeds=["https://a.example/feed"],
                email="u@example.com",
                password="pw",
                sync_remove=False,
                confirm=True,
            )
    assert mock.call_count == 2
    assert "Already in sync" in capsys.readouterr().err


def test_sync_remove_unsubscribes_missing_feeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock = _setup_sync_mocks([
        {"uuid": "uuid-1", "url": "https://a.example/feed", "title": "Pod A"},
        {"uuid": "uuid-old", "url": "https://old.example/feed", "title": "Old Pod"},
    ])
    with patch("urllib.request.urlopen", mock):
        with patch("time.sleep"):
            m.cmd_sync_pocketcasts(
                apple_feeds=["https://a.example/feed"],
                email="u@example.com",
                password="pw",
                sync_remove=True,
                confirm=True,
            )
    assert mock.call_count == 3
    captured = capsys.readouterr()
    assert "0 added, 1 removed" in captured.err


def test_sync_remove_dry_run_does_not_unsubscribe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock = _setup_sync_mocks([
        {"uuid": "uuid-old", "url": "https://old.example/feed", "title": "Old Pod"},
    ])
    with patch("urllib.request.urlopen", mock):
        m.cmd_sync_pocketcasts(
            apple_feeds=[],
            email="u@example.com",
            password="pw",
            sync_remove=True,
            confirm=False,
        )
    assert mock.call_count == 2
    assert "Dry-run" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Overcast helpers
# ---------------------------------------------------------------------------

OVERCAST_LOGIN_HTML = """
<html><body>
<form action="/login" method="post">
  <input type="hidden" name="csrf_token" value="csrf-abc">
  <input type="email" name="email">
  <input type="password" name="password">
</form>
</body></html>
"""

OVERCAST_ACCOUNT_HTML = """
<html><body>
<form action="/account" method="post">
  <input type="hidden" name="csrf_token" value="csrf-xyz">
</form>
</body></html>
"""

OVERCAST_ACCOUNT_LOGGED_IN = """
<html><body>
<div class="account-content">Logged in as user@example.com</div>
<form action="/account" method="post">
  <input type="hidden" name="csrf_token" value="csrf-xyz">
</form>
</body></html>
"""

OVERCAST_OPML = """\
<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <body>
    <outline type="rss" text="Pod A" xmlUrl="https://a.example/feed"
             htmlUrl="https://overcast.fm/itunes111111"/>
    <outline type="rss" text="Pod B" xmlUrl="https://b.example/feed"
             htmlUrl="https://overcast.fm/itunes222222"/>
  </body>
</opml>
"""


def _make_oc_opener_mock(*html_responses: str) -> MagicMock:
    """Mock opener whose .open() returns successive HTML responses."""
    opener = MagicMock()

    def make_ctx(content: str) -> MagicMock:
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read.return_value = content.encode("utf-8")
        return ctx

    opener.open.side_effect = [make_ctx(r) for r in html_responses]
    return opener


# ---------------------------------------------------------------------------
# _oc_extract_form_fields
# ---------------------------------------------------------------------------


def test_oc_extract_form_fields_finds_hidden() -> None:
    fields = m._oc_extract_form_fields(OVERCAST_LOGIN_HTML)
    assert fields.get("csrf_token") == "csrf-abc"


def test_oc_extract_form_fields_ignores_non_hidden() -> None:
    fields = m._oc_extract_form_fields(OVERCAST_LOGIN_HTML)
    assert "email" not in fields
    assert "password" not in fields


# ---------------------------------------------------------------------------
# oc_login
# ---------------------------------------------------------------------------


def test_oc_login_success() -> None:
    opener = _make_oc_opener_mock(
        OVERCAST_LOGIN_HTML,       # GET /login
        OVERCAST_ACCOUNT_LOGGED_IN,  # POST /login response
        OVERCAST_ACCOUNT_HTML,      # GET /account for CSRF
    )
    with patch("macos_podcasts_opml._oc_build_opener", return_value=opener):
        result_opener, csrf_fields = m.oc_login("u@example.com", "pw")
    assert csrf_fields.get("csrf_token") == "csrf-xyz"


def test_oc_login_fails_on_rejected_credentials() -> None:
    # Server still shows the login form → credentials rejected
    opener = _make_oc_opener_mock(
        OVERCAST_LOGIN_HTML,   # GET /login
        OVERCAST_LOGIN_HTML,   # POST /login → still shows login form
    )
    with patch("macos_podcasts_opml._oc_build_opener", return_value=opener):
        with pytest.raises(SystemExit, match="login failed"):
            m.oc_login("u@example.com", "wrong")


# ---------------------------------------------------------------------------
# oc_list_subscriptions
# ---------------------------------------------------------------------------


def test_oc_list_subscriptions_parses_opml() -> None:
    opener = _make_oc_opener_mock(OVERCAST_OPML)
    result = m.oc_list_subscriptions(opener)
    assert len(result) == 2
    assert result[0].feed_url == "https://a.example/feed"
    assert result[0].title == "Pod A"
    assert result[0].overcast_id == "itunes111111"
    assert result[1].overcast_id == "itunes222222"


def test_oc_list_subscriptions_invalid_opml() -> None:
    opener = _make_oc_opener_mock("not xml")
    with pytest.raises(SystemExit, match="parse Overcast OPML"):
        m.oc_list_subscriptions(opener)


# ---------------------------------------------------------------------------
# cmd_sync_overcast
# ---------------------------------------------------------------------------


def test_oc_sync_dry_run_no_subscribes(capsys: pytest.CaptureFixture[str]) -> None:
    opener = _make_oc_opener_mock(
        OVERCAST_LOGIN_HTML,        # GET /login
        OVERCAST_ACCOUNT_LOGGED_IN, # POST /login
        OVERCAST_ACCOUNT_HTML,      # GET /account for CSRF
        OVERCAST_OPML,              # GET /account/export_opml
    )
    with patch("macos_podcasts_opml._oc_build_opener", return_value=opener):
        m.cmd_sync_overcast(
            apple_feeds=["https://new.example/feed"],
            email="u@example.com",
            password="pw",
            sync_remove=False,
            confirm=False,
        )
    captured = capsys.readouterr()
    assert "Dry-run" in captured.err
    assert "https://new.example/feed" in captured.err
    # No extra calls beyond login + list
    assert opener.open.call_count == 4


def test_oc_sync_already_in_sync(capsys: pytest.CaptureFixture[str]) -> None:
    opener = _make_oc_opener_mock(
        OVERCAST_LOGIN_HTML,
        OVERCAST_ACCOUNT_LOGGED_IN,
        OVERCAST_ACCOUNT_HTML,
        OVERCAST_OPML,
    )
    with patch("macos_podcasts_opml._oc_build_opener", return_value=opener):
        m.cmd_sync_overcast(
            apple_feeds=["https://a.example/feed", "https://b.example/feed"],
            email="u@example.com",
            password="pw",
            sync_remove=False,
            confirm=True,
        )
    assert "Already in sync" in capsys.readouterr().err


def test_oc_sync_confirm_subscribes(capsys: pytest.CaptureFixture[str]) -> None:
    opener = _make_oc_opener_mock(
        OVERCAST_LOGIN_HTML,
        OVERCAST_ACCOUNT_LOGGED_IN,
        OVERCAST_ACCOUNT_HTML,
        OVERCAST_OPML,
        "<html/>",  # POST subscribe response
    )
    with patch("macos_podcasts_opml._oc_build_opener", return_value=opener):
        with patch("time.sleep"):
            m.cmd_sync_overcast(
                apple_feeds=["https://new.example/feed"],
                email="u@example.com",
                password="pw",
                sync_remove=False,
                confirm=True,
            )
    captured = capsys.readouterr()
    assert "1 added" in captured.err


# ---------------------------------------------------------------------------
# cmd_sync_castro
# ---------------------------------------------------------------------------


def test_cmd_sync_castro_writes_opml_to_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    podcasts = [
        m.Podcast("Pod A", "https://a.example/feed", "https://a.example"),
    ]
    out_file = tmp_path / "castro.opml"
    m.cmd_sync_castro(podcasts, out_file, title="My Podcasts")
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "https://a.example/feed" in content
    captured = capsys.readouterr()
    assert "Castro" in captured.err
    assert "Import Subscriptions" in captured.err


def test_cmd_sync_castro_prints_to_stdout_without_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    podcasts = [m.Podcast("Pod A", "https://a.example/feed", "")]
    m.cmd_sync_castro(podcasts, None, title="Export")
    out = capsys.readouterr().out
    assert "https://a.example/feed" in out
