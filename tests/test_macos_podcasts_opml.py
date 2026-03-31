import csv
import io
import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock, patch

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
    # 978307200 seconds after epoch = 2001-01-01
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
    # DB untouched
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
    # Podcasts.app "running" — but dry_run should not care
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
# Pocket Casts API helpers
# ---------------------------------------------------------------------------


def _make_urlopen_mock(response_data: Any) -> MagicMock:
    """Return a mock suitable for patching urllib.request.urlopen."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.read.return_value = json.dumps(response_data).encode("utf-8")
    return ctx


def _pc_subs_response(podcasts: List[dict[str, str]]) -> dict[str, Any]:
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
    pc_podcasts: List[dict[str, str]],
) -> MagicMock:
    """Return a side_effect list for urlopen: login → list → (subscribe/unsubscribe)*."""
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
    # Only 2 calls: login + list. No subscribe.
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
    # login + list + 2 subscribes
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
    # login + list only — nothing to add
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
    # login + list + 1 unsubscribe
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
