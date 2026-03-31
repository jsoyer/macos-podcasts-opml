#!/usr/bin/env python3

import argparse
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, cast
from xml.dom import minidom

VERSION = "2.3.0"
POCKETCASTS_API = "https://api.pocketcasts.com"
PC_RATE_LIMIT_SECS = 0.1
DB_GLOB = (
    "Library/Group Containers/*.groups.com.apple.podcasts/Documents/MTLibrary.sqlite"
)

# Core Data stores dates as seconds since 2001-01-01 UTC (not Unix epoch).
COREDATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# Candidate date columns in priority order.
DATE_COLUMNS = ["ZLASTPUBLISHDATE", "ZLASTDOWNLOADDATE", "ZCREATIONDATE"]


@dataclass(frozen=True)
class PCPodcast:
    uuid: str
    feed_url: str
    title: str


@dataclass(frozen=True)
class Podcast:
    title: str
    feed_url: str
    website_url: str
    last_date: Optional[datetime] = None
    date_source: str = ""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def open_database(db_path: Path, writable: bool = False) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode={'rwc' if writable else 'ro'}"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except (sqlite3.OperationalError, OSError) as e:
        raise SystemExit(f"Unable to open database {db_path!r}: {e}")
    conn.row_factory = sqlite3.Row
    return conn


def resolve_database(db_arg: Optional[str]) -> Path:
    if db_arg is not None:
        path = Path(db_arg).expanduser()
        if not path.exists():
            raise SystemExit(f"Database file not found: {path!r}")
        return path
    home = Path("~").expanduser()
    found = next(home.glob(DB_GLOB), None)
    if found is None:
        raise SystemExit(
            "Unable to find Podcasts database. "
            "Try specifying the path with --db."
        )
    return found


def table_columns(connection: sqlite3.Connection, table: str) -> List[str]:
    rows = connection.execute(f"PRAGMA table_info({table})")
    return [cast(str, row["name"]) for row in rows]


def detect_date_column(columns: List[str]) -> Optional[str]:
    for candidate in DATE_COLUMNS:
        if candidate in columns:
            return candidate
    return None


def coredata_to_datetime(value: object) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return COREDATA_EPOCH + timedelta(seconds=float(cast(float, value)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------


def get_podcasts(
    connection: sqlite3.Connection,
    subscribed_only: bool = False,
    date_column: Optional[str] = None,
) -> Iterator[Podcast]:
    if date_column:
        select = f"SELECT ZTITLE, ZFEEDURL, ZWEBPAGEURL, {date_column} FROM ZMTPODCAST"
    else:
        select = "SELECT ZTITLE, ZFEEDURL, ZWEBPAGEURL FROM ZMTPODCAST"

    if subscribed_only:
        select += " WHERE ZFEEDURL IS NOT NULL AND ZFEEDURL != ''"

    cursor = connection.cursor()
    try:
        rows = cursor.execute(select)
    except sqlite3.OperationalError as e:
        raise SystemExit(
            f"Failed to query podcasts — the schema may have changed "
            f"in this macOS version: {e}"
        )
    for row in rows:
        raw_date = row[date_column] if date_column else None
        yield Podcast(
            title=row["ZTITLE"] or "",
            feed_url=row["ZFEEDURL"] or "",
            website_url=row["ZWEBPAGEURL"] or "",
            last_date=coredata_to_datetime(raw_date),
            date_source=date_column or "",
        )


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------


def parse_duration(s: str) -> timedelta:
    """Parse a duration string like '1y', '6m', '90d' into a timedelta."""
    match = re.fullmatch(r"(\d+)([ymd])", s.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(
            f"Invalid duration {s!r}. Use e.g. '1y', '6m', '90d'."
        )
    value, unit = int(match.group(1)), match.group(2)
    if unit == "y":
        return timedelta(days=value * 365)
    if unit == "m":
        return timedelta(days=value * 30)
    return timedelta(days=value)


def filter_stale(
    podcasts: Iterator[Podcast], cutoff: datetime
) -> Iterator[Podcast]:
    for p in podcasts:
        if p.last_date is None:
            continue
        if p.last_date < cutoff:
            yield p


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def format_as_opml(podcasts: Iterator[Podcast], title: str = "MacOS Podcasts") -> str:
    root = ET.fromstring('<opml version="1.0"></opml>')
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = title
    body = ET.SubElement(root, "body")

    for p in podcasts:
        if not p.feed_url:
            print(f"Warning: skipping '{p.title}' — no feed URL", file=sys.stderr)
            continue
        ET.SubElement(
            body,
            "outline",
            {
                "type": "rss",
                "text": p.title,
                "xmlUrl": p.feed_url,
                "htmlUrl": p.website_url,
            },
        )

    dom = minidom.parseString(ET.tostring(root, method="xml"))
    return cast(str, dom.toprettyxml(indent="  "))


def format_as_json(podcasts: Iterator[Podcast], include_date: bool = False) -> str:
    data: List[Dict[str, str]] = []
    for p in podcasts:
        if not p.feed_url:
            continue
        entry: Dict[str, str] = {
            "title": p.title,
            "feedUrl": p.feed_url,
            "websiteUrl": p.website_url,
        }
        if include_date and p.last_date:
            entry["lastDate"] = p.last_date.strftime("%Y-%m-%d")
            entry["dateSource"] = p.date_source
        data.append(entry)
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_as_csv(podcasts: Iterator[Podcast], include_date: bool = False) -> str:
    buf = io.StringIO()
    header = ["title", "feedUrl", "websiteUrl"]
    if include_date:
        header += ["lastDate", "dateSource"]
    writer = csv.writer(buf)
    writer.writerow(header)
    for p in podcasts:
        if not p.feed_url:
            continue
        row = [p.title, p.feed_url, p.website_url]
        if include_date:
            row += [
                p.last_date.strftime("%Y-%m-%d") if p.last_date else "",
                p.date_source,
            ]
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Unsubscribe (direct DB write)
# ---------------------------------------------------------------------------


def is_podcasts_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Podcasts"],
            capture_output=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def backup_database(db_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}.backup-{timestamp}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def unsubscribe_from_db(
    db_path: Path,
    feed_urls: List[str],
    dry_run: bool = True,
) -> int:
    """Delete podcasts by feed URL. Returns the number of affected rows.

    When dry_run=True the database is not modified — only the row count is returned.
    """
    if not feed_urls:
        return 0

    placeholders = ",".join("?" * len(feed_urls))

    if dry_run:
        with closing(open_database(db_path)) as conn:
            cursor = conn.execute(
                f"SELECT COUNT(*) FROM ZMTPODCAST WHERE ZFEEDURL IN ({placeholders})",
                feed_urls,
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    backup = backup_database(db_path)
    print(f"Backup created: {backup}", file=sys.stderr)

    with closing(open_database(db_path, writable=True)) as conn:
        cursor = conn.execute(
            f"DELETE FROM ZMTPODCAST WHERE ZFEEDURL IN ({placeholders})",
            feed_urls,
        )
        conn.commit()
        return cursor.rowcount


def cmd_unsubscribe(
    db_path: Path,
    podcasts: List[Podcast],
    confirm: bool,
) -> None:
    feed_urls = [p.feed_url for p in podcasts if p.feed_url]

    if not feed_urls:
        print("No podcasts with a valid feed URL to unsubscribe.", file=sys.stderr)
        return

    print(f"\nPodcasts to remove ({len(feed_urls)}):", file=sys.stderr)
    for p in podcasts:
        date_str = f"  [last: {p.last_date.strftime('%Y-%m-%d')}]" if p.last_date else ""
        print(f"  - {p.title or p.feed_url}{date_str}", file=sys.stderr)

    if not confirm:
        count = unsubscribe_from_db(db_path, feed_urls, dry_run=True)
        print(
            f"\nDry-run: {count} row(s) would be deleted from ZMTPODCAST.\n"
            f"Re-run with --confirm to apply.",
            file=sys.stderr,
        )
        return

    if is_podcasts_running():
        raise SystemExit(
            "Podcasts.app is currently running. "
            "Close it before using --confirm to avoid database corruption."
        )

    removed = unsubscribe_from_db(db_path, feed_urls, dry_run=False)
    print(f"\nDone: {removed} podcast(s) removed from the database.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Pocket Casts API
# ---------------------------------------------------------------------------


def _pc_post(
    token: Optional[str], path: str, payload: Dict[str, object]
) -> Dict[str, object]:
    url = f"{POCKETCASTS_API}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return cast(Dict[str, object], json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Pocket Casts API {path} returned {e.code}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error reaching Pocket Casts: {e.reason}")


def pc_login(email: str, password: str) -> str:
    resp = _pc_post(
        None,
        "/user/login",
        {"email": email, "password": password, "scope": "webplayer"},
    )
    token = resp.get("token")
    if not isinstance(token, str) or not token:
        raise SystemExit("Pocket Casts login failed — check your credentials.")
    return token


def pc_list_subscriptions(token: str) -> List[PCPodcast]:
    resp = _pc_post(token, "/user/podcast/list", {"v": 1})
    raw = resp.get("podcasts", [])
    if not isinstance(raw, list):
        return []
    result: List[PCPodcast] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        uuid = item.get("uuid", "")
        url = item.get("url", "")
        title = item.get("title", "")
        if uuid and url:
            result.append(
                PCPodcast(
                    uuid=cast(str, uuid),
                    feed_url=cast(str, url),
                    title=cast(str, title),
                )
            )
    return result


def pc_subscribe(token: str, feed_url: str) -> None:
    _pc_post(token, "/user/podcast/subscribe", {"url": feed_url})


def pc_unsubscribe(token: str, uuid: str) -> None:
    _pc_post(token, "/user/podcast/unsubscribe", {"uuid": uuid})


def cmd_sync_pocketcasts(
    apple_feeds: List[str],
    email: str,
    password: str,
    sync_remove: bool,
    confirm: bool,
) -> None:
    print("Logging in to Pocket Casts...", file=sys.stderr)
    token = pc_login(email, password)

    print("Fetching Pocket Casts subscriptions...", file=sys.stderr)
    pc_subs = pc_list_subscriptions(token)
    pc_feed_urls = {p.feed_url for p in pc_subs}
    pc_by_url: Dict[str, PCPodcast] = {p.feed_url: p for p in pc_subs}

    apple_feed_set = {url for url in apple_feeds if url}

    to_add = sorted(apple_feed_set - pc_feed_urls)
    to_remove = sorted(pc_feed_urls - apple_feed_set) if sync_remove else []

    print(f"\nApple Podcasts : {len(apple_feed_set)} subscriptions", file=sys.stderr)
    print(f"Pocket Casts   : {len(pc_feed_urls)} subscriptions", file=sys.stderr)
    print(f"To subscribe   : {len(to_add)}", file=sys.stderr)
    if sync_remove:
        print(f"To remove      : {len(to_remove)}", file=sys.stderr)

    if not to_add and not to_remove:
        print("\nAlready in sync.", file=sys.stderr)
        return

    if to_add:
        label = "Subscribing" if confirm else "Would subscribe"
        print(f"\n{label}:", file=sys.stderr)
        for url in to_add:
            print(f"  + {url}", file=sys.stderr)

    if to_remove:
        label = "Unsubscribing" if confirm else "Would unsubscribe"
        print(f"\n{label}:", file=sys.stderr)
        for url in to_remove:
            title = pc_by_url[url].title or url
            print(f"  - {title}", file=sys.stderr)

    if not confirm:
        print("\nDry-run — add --confirm to apply.", file=sys.stderr)
        return

    for url in to_add:
        pc_subscribe(token, url)
        time.sleep(PC_RATE_LIMIT_SECS)

    for url in to_remove:
        pc_unsubscribe(token, pc_by_url[url].uuid)
        time.sleep(PC_RATE_LIMIT_SECS)

    print(
        f"\nDone: {len(to_add)} added, {len(to_remove)} removed.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Schema inspection
# ---------------------------------------------------------------------------


def cmd_schema(connection: sqlite3.Connection) -> None:
    cols = table_columns(connection, "ZMTPODCAST")
    print(f"ZMTPODCAST — {len(cols)} column(s):\n")
    date_col = detect_date_column(cols)
    for col in cols:
        tag = " ← stale detection" if col == date_col else ""
        print(f"  {col}{tag}")
    if date_col is None:
        print(
            "\nWarning: none of the expected date columns found "
            f"({', '.join(DATE_COLUMNS)}).",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="macos-podcasts-opml",
        description="Export macOS Podcasts subscriptions to OPML, JSON, or CSV.",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="Path to the Podcasts SQLite database (auto-detected if omitted).",
    )
    parser.add_argument(
        "--output",
        "-o",
        metavar="FILE",
        help="Write output to FILE instead of stdout.",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["opml", "json", "csv"],
        default="opml",
        help="Output format (default: opml).",
    )
    parser.add_argument(
        "--title",
        default="MacOS Podcasts",
        help="Title for the OPML <head> element (default: 'MacOS Podcasts').",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="List podcast titles without generating output.",
    )
    parser.add_argument(
        "--subscribed-only",
        action="store_true",
        help="Only export podcasts that have a valid feed URL.",
    )
    parser.add_argument(
        "--stale",
        metavar="DURATION",
        type=parse_duration,
        help=(
            "Filter to podcasts with no activity for the given duration. "
            "Examples: 1y, 6m, 90d. "
            "Combines with --format to export or review the stale list."
        ),
    )
    # Pocket Casts sync
    pc_group = parser.add_argument_group("Pocket Casts sync")
    pc_group.add_argument(
        "--sync-pocketcasts",
        action="store_true",
        help=(
            "Sync Apple Podcasts subscriptions to Pocket Casts. "
            "Dry-run by default; add --confirm to apply."
        ),
    )
    pc_group.add_argument(
        "--pc-email",
        metavar="EMAIL",
        help="Pocket Casts email (or set POCKETCASTS_EMAIL env var).",
    )
    pc_group.add_argument(
        "--pc-password",
        metavar="PASSWORD",
        help="Pocket Casts password (or set POCKETCASTS_PASSWORD env var).",
    )
    pc_group.add_argument(
        "--sync-remove",
        action="store_true",
        help=(
            "Also remove from Pocket Casts any podcast no longer in Apple Podcasts. "
            "Requires --sync-pocketcasts."
        ),
    )

    parser.add_argument(
        "--unsubscribe",
        action="store_true",
        help=(
            "Remove matched podcasts from the database. "
            "Combine with --stale to target inactive ones. "
            "Dry-run by default; add --confirm to apply."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually execute --unsubscribe (requires Podcasts.app to be closed).",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Print the ZMTPODCAST table schema and exit.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = resolve_database(cast(Optional[str], args.db))

    with closing(open_database(db_path)) as connection:
        if cast(bool, args.schema):
            cmd_schema(connection)
            return

        cols = table_columns(connection, "ZMTPODCAST")
        date_column = detect_date_column(cols)
        stale_delta: Optional[timedelta] = cast(Optional[timedelta], args.stale)
        subscribed_only: bool = cast(bool, args.subscribed_only)
        include_date = stale_delta is not None or date_column is not None

        if stale_delta is not None and date_column is None:
            raise SystemExit(
                f"--stale requires a date column in the database, "
                f"but none of {DATE_COLUMNS} were found. "
                f"Run --schema to inspect the table."
            )

        podcast_iter: Iterator[Podcast] = get_podcasts(
            connection,
            subscribed_only=subscribed_only,
            date_column=date_column,
        )

        if stale_delta is not None:
            cutoff = datetime.now(tz=timezone.utc) - stale_delta
            podcast_iter = filter_stale(podcast_iter, cutoff)
            print(
                f"Podcasts with no activity since "
                f"{cutoff.strftime('%Y-%m-%d')} "
                f"(field: {date_column}):",
                file=sys.stderr,
            )

        if cast(bool, args.sync_pocketcasts):
            email: str = (
                os.environ.get("POCKETCASTS_EMAIL")
                or cast(str, args.pc_email or "")
            )
            password: str = (
                os.environ.get("POCKETCASTS_PASSWORD")
                or cast(str, args.pc_password or "")
            )
            if not email or not password:
                raise SystemExit(
                    "Pocket Casts credentials required.\n"
                    "Set POCKETCASTS_EMAIL / POCKETCASTS_PASSWORD env vars "
                    "or use --pc-email / --pc-password."
                )
            apple_feeds = [
                p.feed_url
                for p in get_podcasts(connection, subscribed_only=True)
                if p.feed_url
            ]
            cmd_sync_pocketcasts(
                apple_feeds=apple_feeds,
                email=email,
                password=password,
                sync_remove=cast(bool, args.sync_remove),
                confirm=cast(bool, args.confirm),
            )
            return

        if cast(bool, args.unsubscribe):
            matched = list(podcast_iter)
            cmd_unsubscribe(db_path, matched, confirm=cast(bool, args.confirm))
            return

        if cast(bool, args.list_only):
            count = 0
            for p in podcast_iter:
                date_str = (
                    f"  [{p.last_date.strftime('%Y-%m-%d')}]"
                    if p.last_date
                    else ""
                )
                print(f"{p.title or '(no title)'}{date_str}")
                count += 1
            print(f"\n{count} podcast(s) found.", file=sys.stderr)
            return

        fmt: str = cast(str, args.format)
        if fmt == "opml":
            output = format_as_opml(podcast_iter, title=cast(str, args.title))
        elif fmt == "json":
            output = format_as_json(podcast_iter, include_date=include_date)
        else:
            output = format_as_csv(podcast_iter, include_date=include_date)

    out_file: Optional[str] = cast(Optional[str], args.output)
    if out_file:
        try:
            Path(out_file).write_text(output, encoding="utf-8")
        except OSError as e:
            raise SystemExit(f"Unable to write to {out_file!r}: {e}")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
