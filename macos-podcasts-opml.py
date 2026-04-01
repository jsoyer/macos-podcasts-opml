#!/usr/bin/env python3

import argparse
import concurrent.futures
import csv
import html.parser
import http.cookiejar
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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, cast
from xml.dom import minidom

VERSION = "3.0.0"
POCKETCASTS_API = "https://api.pocketcasts.com"
PC_RATE_LIMIT_SECS = 0.1
OVERCAST_BASE = "https://overcast.fm"
OC_RATE_LIMIT_SECS = 0.5
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
class OCPodcast:
    overcast_id: str  # e.g. "itunes123456789" extracted from htmlUrl
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


def parse_since(s: str) -> datetime:
    """Parse an ISO date string like '2024-01-01' into a timezone-aware datetime."""
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid date {s!r}. Use ISO format, e.g. '2024-01-01'."
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
    return dom.toprettyxml(indent="  ")


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
# Feed health check
# ---------------------------------------------------------------------------


def check_feed_url(url: str, timeout: int = 10) -> Optional[str]:
    """Return None if reachable, error description otherwise."""
    req = urllib.request.Request(url, method="HEAD")
    req.add_header("User-Agent", f"macos-podcasts-opml/{VERSION} feed-checker")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = cast(int, resp.status)
            if status >= 400:
                return f"HTTP {status}"
            return None
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return f"URLError: {e.reason}"
    except OSError as e:
        return f"Error: {e}"


def cmd_broken(
    podcasts: List[Podcast],
    timeout: int = 10,
    workers: int = 10,
) -> None:
    """Check all feed URLs concurrently and report unreachable ones."""
    feeds = [(p, p.feed_url) for p in podcasts if p.feed_url]
    if not feeds:
        print("No podcasts with a feed URL to check.", file=sys.stderr)
        return

    total = len(feeds)
    print(f"Checking {total} feed URLs (workers={workers}, timeout={timeout}s)…",
          file=sys.stderr)

    broken: List[Tuple[Podcast, str]] = []
    checked = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_p: Dict[concurrent.futures.Future[Optional[str]], Podcast] = {
            executor.submit(check_feed_url, url, timeout): p
            for p, url in feeds
        }
        for future in concurrent.futures.as_completed(future_to_p):
            p = future_to_p[future]
            try:
                error = future.result()
            except Exception as exc:
                error = f"Exception: {exc}"
            if error:
                broken.append((p, error))
            checked += 1
            if checked % 20 == 0 or checked == total:
                print(f"  {checked}/{total} checked…", file=sys.stderr)

    ok = total - len(broken)
    if not broken:
        print(f"\nAll {ok} feeds reachable.", file=sys.stderr)
        return

    print(f"\n{len(broken)} broken feed(s) out of {total}:")
    for p, error in sorted(broken, key=lambda x: (x[0].title or "").lower()):
        print(f"  [{error:<14}] {p.title or '(no title)'}")
        print(f"                 {p.feed_url}")


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def cmd_stats(podcasts: List[Podcast], date_column: Optional[str]) -> None:
    total = len(podcasts)
    if total == 0:
        print("No podcasts found.")
        return

    with_feed = sum(1 for p in podcasts if p.feed_url)
    with_date = sum(1 for p in podcasts if p.last_date)

    now = datetime.now(tz=timezone.utc)

    buckets: Dict[str, int] = {
        "< 1 month": 0,
        "1-3 months": 0,
        "3-6 months": 0,
        "6-12 months": 0,
        "1-2 years": 0,
        "> 2 years": 0,
        "No date": 0,
    }

    for p in podcasts:
        if not p.last_date:
            buckets["No date"] += 1
            continue
        age = now - p.last_date
        if age < timedelta(days=30):
            buckets["< 1 month"] += 1
        elif age < timedelta(days=90):
            buckets["1-3 months"] += 1
        elif age < timedelta(days=180):
            buckets["3-6 months"] += 1
        elif age < timedelta(days=365):
            buckets["6-12 months"] += 1
        elif age < timedelta(days=730):
            buckets["1-2 years"] += 1
        else:
            buckets["> 2 years"] += 1

    bar_width = 25
    max_count = max(buckets.values(), default=1) or 1

    print(f"Total podcasts  : {total}")
    print(f"With feed URL   : {with_feed}  ({with_feed * 100 // total}%)")
    print(f"With date data  : {with_date}  (column: {date_column or 'none'})")
    print()
    print("Activity (last episode date):")
    for label, count in buckets.items():
        bar = "#" * (count * bar_width // max_count)
        pct = count * 100 // total if total else 0
        print(f"  {label:<14} {count:>4}  ({pct:>2}%)  {bar}")


# ---------------------------------------------------------------------------
# Diff against OPML
# ---------------------------------------------------------------------------


def parse_opml_feeds(opml_path: Path) -> Dict[str, str]:
    """Parse OPML file and return {feed_url: title}."""
    try:
        root = ET.parse(str(opml_path)).getroot()
    except ET.ParseError as e:
        raise SystemExit(f"Failed to parse OPML {opml_path}: {e}")
    except OSError as e:
        raise SystemExit(f"Cannot read OPML file {opml_path}: {e}")

    feeds: Dict[str, str] = {}
    for outline in root.findall(".//outline"):
        url = outline.get("xmlUrl") or outline.get("url")
        title = outline.get("text") or outline.get("title") or ""
        if url:
            feeds[url] = title
    return feeds


def cmd_diff(apple_podcasts: List[Podcast], opml_path: Path) -> None:
    """Compare Apple Podcasts DB against an OPML file."""
    opml_feeds = parse_opml_feeds(opml_path)
    apple_feeds: Dict[str, str] = {
        p.feed_url: p.title for p in apple_podcasts if p.feed_url
    }

    only_apple = {u: t for u, t in apple_feeds.items() if u not in opml_feeds}
    only_opml = {u: t for u, t in opml_feeds.items() if u not in apple_feeds}
    in_both = sum(1 for u in apple_feeds if u in opml_feeds)

    print(f"Apple Podcasts DB   : {len(apple_feeds)}")
    print(f"OPML ({opml_path.name:<18}): {len(opml_feeds)}")
    print(f"In common           : {in_both}")

    if only_apple:
        print(f"\nOnly in Apple Podcasts ({len(only_apple)}):")
        for url, title in sorted(only_apple.items(), key=lambda x: x[1].lower()):
            print(f"  + {title or url}")

    if only_opml:
        print(f"\nOnly in OPML ({len(only_opml)}):")
        for url, title in sorted(only_opml.items(), key=lambda x: x[1].lower()):
            print(f"  - {title or url}")

    if not only_apple and not only_opml:
        print("\nNo differences — perfectly in sync.")


# ---------------------------------------------------------------------------
# Credential resolution (literal, 1Password CLI, macOS Keychain)
# ---------------------------------------------------------------------------


def _resolve_credential(value: str) -> str:
    """Resolve a credential value — may be a literal, op:// URI, or keychain: ref.

    Supported formats:
      - Literal string (default, returned as-is)
      - op://vault/item/field   — read from 1Password CLI (requires `op` in PATH)
      - keychain:SERVICE        — macOS Keychain, no specific account
      - keychain:SERVICE:ACCOUNT — macOS Keychain with an explicit account name
    """
    if not value:
        return value

    if value.startswith("op://"):
        try:
            result = subprocess.run(
                ["op", "read", "--no-newline", value],
                capture_output=True, text=True, check=True,
            )
            return result.stdout
        except FileNotFoundError:
            raise SystemExit(
                "1Password CLI (op) not found. "
                "Install it from: https://developer.1password.com/docs/cli/"
            )
        except subprocess.CalledProcessError as e:
            raise SystemExit(
                f"1Password read failed for {value!r}: {e.stderr.strip()}"
            )

    if value.startswith("keychain:"):
        rest = value[9:]
        parts = rest.split(":", 1)
        service = parts[0]
        account = parts[1] if len(parts) > 1 else ""
        cmd = ["security", "find-generic-password", "-s", service, "-w"]
        if account:
            cmd += ["-a", account]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout.rstrip("\n")
        except FileNotFoundError:
            raise SystemExit("'security' command not found — Keychain is macOS only.")
        except subprocess.CalledProcessError:
            hint = f"security add-generic-password -s '{service}'"
            if account:
                hint += f" -a '{account}'"
            hint += " -w"
            raise SystemExit(
                f"Keychain lookup failed for service={service!r}"
                + (f", account={account!r}" if account else "")
                + f"\nAdd with: {hint}"
            )

    return value


# ---------------------------------------------------------------------------
# Notifications (macOS only, silent no-op elsewhere)
# ---------------------------------------------------------------------------


def _notify(title: str, message: str) -> None:
    """Send a macOS notification. Silent no-op on non-macOS or missing osascript."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            [
                "osascript", "-e",
                f"display notification {json.dumps(message)} "
                f"with title {json.dumps(title)}",
            ],
            capture_output=True,
        )
    except FileNotFoundError:
        pass


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
    print("Logging in to Pocket Casts…", file=sys.stderr)
    token = pc_login(email, password)

    print("Fetching Pocket Casts subscriptions…", file=sys.stderr)
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
    _notify("Pocket Casts Sync", f"{len(to_add)} added, {len(to_remove)} removed.")


# ---------------------------------------------------------------------------
# Overcast API (unofficial — web-based, may break on UI changes)
# ---------------------------------------------------------------------------


class _FormFieldExtractor(html.parser.HTMLParser):
    """Extract hidden input fields from an HTML form (for CSRF tokens)."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: Dict[str, str] = {}

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        if tag == "input":
            d = dict(attrs)
            name = d.get("name")
            if d.get("type") == "hidden" and name:
                self.fields[name] = d.get("value") or ""


def _oc_extract_form_fields(html_content: str) -> Dict[str, str]:
    extractor = _FormFieldExtractor()
    extractor.feed(html_content)
    return extractor.fields


def _oc_build_opener() -> urllib.request.OpenerDirector:
    jar: http.cookiejar.CookieJar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _oc_get(opener: urllib.request.OpenerDirector, path: str) -> str:
    req = urllib.request.Request(
        f"{OVERCAST_BASE}{path}",
        headers={"User-Agent": f"macos-podcasts-opml/{VERSION}"},
    )
    try:
        with opener.open(req, timeout=15) as resp:
            return cast(bytes, resp.read()).decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Overcast GET {path} returned HTTP {e.code}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error reaching Overcast: {e.reason}")


def _oc_post(
    opener: urllib.request.OpenerDirector,
    path: str,
    data: Dict[str, str],
) -> str:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        f"{OVERCAST_BASE}{path}",
        data=encoded,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": f"macos-podcasts-opml/{VERSION}",
        },
        method="POST",
    )
    try:
        with opener.open(req, timeout=15) as resp:
            return cast(bytes, resp.read()).decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Overcast POST {path} returned {e.code}: {body[:200]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error reaching Overcast: {e.reason}")


def oc_login(
    email: str, password: str
) -> Tuple[urllib.request.OpenerDirector, Dict[str, str]]:
    """Login to Overcast. Returns (opener, csrf_fields) for subsequent POSTs."""
    opener = _oc_build_opener()

    login_html = _oc_get(opener, "/login")
    form_fields = _oc_extract_form_fields(login_html)
    post_data = {"email": email, "password": password}
    post_data.update(form_fields)

    resp_html = _oc_post(opener, "/login", post_data)

    # Heuristic: still seeing a login form means credentials were rejected
    if 'action="/login"' in resp_html:
        raise SystemExit("Overcast login failed — check your credentials.")

    # Fetch account page to get fresh CSRF fields for subsequent POSTs
    account_html = _oc_get(opener, "/account")
    csrf_fields = _oc_extract_form_fields(account_html)
    return opener, csrf_fields


def oc_list_subscriptions(opener: urllib.request.OpenerDirector) -> List[OCPodcast]:
    """Export subscriptions as OPML and parse them."""
    opml_content = _oc_get(opener, "/account/export_opml")
    try:
        root = ET.fromstring(opml_content)
    except ET.ParseError as e:
        raise SystemExit(f"Failed to parse Overcast OPML export: {e}")

    result: List[OCPodcast] = []
    for outline in root.findall(".//outline[@type='rss']"):
        feed_url = outline.get("xmlUrl") or ""
        title = outline.get("text") or outline.get("title") or ""
        html_url = outline.get("htmlUrl") or ""
        # Extract overcast_id from htmlUrl e.g. "https://overcast.fm/itunes123456"
        overcast_id = html_url.rstrip("/").split("/")[-1] if html_url else ""
        if feed_url:
            result.append(
                OCPodcast(overcast_id=overcast_id, feed_url=feed_url, title=title)
            )
    return result


def oc_subscribe(
    opener: urllib.request.OpenerDirector,
    feed_url: str,
    csrf_fields: Dict[str, str],
) -> None:
    post_data = {"feedUrl": feed_url}
    post_data.update(csrf_fields)
    _oc_post(opener, "/account/add_podcast_by_feed_url", post_data)


def oc_unsubscribe(
    opener: urllib.request.OpenerDirector,
    overcast_id: str,
    csrf_fields: Dict[str, str],
) -> None:
    _oc_post(opener, f"/{overcast_id}/delete", dict(csrf_fields))


def cmd_sync_overcast(
    apple_feeds: List[str],
    email: str,
    password: str,
    sync_remove: bool,
    confirm: bool,
) -> None:
    print("Logging in to Overcast…", file=sys.stderr)
    opener, csrf_fields = oc_login(email, password)

    print("Fetching Overcast subscriptions…", file=sys.stderr)
    oc_subs = oc_list_subscriptions(opener)
    oc_feed_urls = {p.feed_url for p in oc_subs}
    oc_by_url: Dict[str, OCPodcast] = {p.feed_url: p for p in oc_subs}

    apple_feed_set = {url for url in apple_feeds if url}

    to_add = sorted(apple_feed_set - oc_feed_urls)
    to_remove = sorted(oc_feed_urls - apple_feed_set) if sync_remove else []

    print(f"\nApple Podcasts : {len(apple_feed_set)} subscriptions", file=sys.stderr)
    print(f"Overcast       : {len(oc_feed_urls)} subscriptions", file=sys.stderr)
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
            p = oc_by_url[url]
            print(f"  - {p.title or url}", file=sys.stderr)

    if not confirm:
        print("\nDry-run — add --confirm to apply.", file=sys.stderr)
        return

    for url in to_add:
        oc_subscribe(opener, url, csrf_fields)
        time.sleep(OC_RATE_LIMIT_SECS)

    removed = 0
    for url in to_remove:
        p = oc_by_url[url]
        if not p.overcast_id:
            print(f"  Skipping {p.title or url} — no Overcast ID in export.",
                  file=sys.stderr)
            continue
        oc_unsubscribe(opener, p.overcast_id, csrf_fields)
        removed += 1
        time.sleep(OC_RATE_LIMIT_SECS)

    print(
        f"\nDone: {len(to_add)} added, {removed} removed.",
        file=sys.stderr,
    )
    _notify("Overcast Sync", f"{len(to_add)} added, {removed} removed.")


# ---------------------------------------------------------------------------
# Castro sync (OPML-based — no public API)
# ---------------------------------------------------------------------------


def cmd_sync_castro(
    podcasts: List[Podcast],
    output_path: Optional[Path],
    title: str,
) -> None:
    """Generate an OPML file for Castro import.

    Castro has no public API. The only way to sync is to import an OPML file
    manually via Castro → Settings → Import Subscriptions.
    """
    opml_content = format_as_opml(iter(podcasts), title=title)

    if output_path:
        try:
            output_path.write_text(opml_content, encoding="utf-8")
        except OSError as e:
            raise SystemExit(f"Unable to write to {output_path!r}: {e}")
        print(f"OPML saved to: {output_path}", file=sys.stderr)
    else:
        print(opml_content, end="")

    print(
        "\nTo import in Castro:\n"
        "  1. Transfer the OPML file to your iPhone/iPad\n"
        "     (AirDrop, iCloud Drive, Files app, etc.)\n"
        "  2. Open Castro\n"
        "  3. Tap the gear icon → Import Subscriptions\n"
        "  4. Select the OPML file\n"
        "\nNote: Castro has no public API — OPML import is the only sync method.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def _normalize_feed_url(url: str) -> str:
    """Normalize a feed URL for duplicate comparison (lowercase, https, no trailing slash)."""
    url = url.strip().lower()
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url.rstrip("/")


def cmd_dupes(podcasts: List[Podcast]) -> None:
    """Report podcasts that appear to be subscribed more than once."""
    from collections import defaultdict

    groups: Dict[str, List[Podcast]] = defaultdict(list)
    for p in podcasts:
        if p.feed_url:
            groups[_normalize_feed_url(p.feed_url)].append(p)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}

    if not dupes:
        print(f"No duplicate subscriptions found ({len(podcasts)} checked).")
        return

    print(f"{len(dupes)} potential duplicate group(s):\n")
    for pods in sorted(dupes.values(), key=lambda v: (v[0].title or "").lower()):
        print(f"  {pods[0].title}")
        for p in pods:
            print(f"    {p.feed_url}")


# ---------------------------------------------------------------------------
# Schema inspection
# ---------------------------------------------------------------------------


def cmd_schema(connection: sqlite3.Connection) -> None:
    cols = table_columns(connection, "ZMTPODCAST")
    print(f"ZMTPODCAST — {len(cols)} column(s):\n")
    date_col = detect_date_column(cols)
    for col in cols:
        tag = " <- stale detection" if col == date_col else ""
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
    cutoff_group = parser.add_mutually_exclusive_group()
    cutoff_group.add_argument(
        "--stale",
        metavar="DURATION",
        type=parse_duration,
        help="Filter to podcasts inactive for the given duration (e.g. 1y, 6m, 90d).",
    )
    cutoff_group.add_argument(
        "--since",
        metavar="DATE",
        type=parse_since,
        help="Filter to podcasts with no activity since DATE (ISO format: 2024-01-01).",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print subscription statistics and exit.",
    )
    parser.add_argument(
        "--broken",
        action="store_true",
        help="Check all feed URLs via HTTP HEAD and report unreachable ones.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        metavar="N",
        help="Parallel workers for --broken (default: 10).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        metavar="SECS",
        help="HTTP timeout in seconds for --broken (default: 10).",
    )
    parser.add_argument(
        "--diff",
        metavar="OPML_FILE",
        help="Compare Apple Podcasts subscriptions against an existing OPML file.",
    )

    # Shared sync flag
    parser.add_argument(
        "--sync-remove",
        action="store_true",
        help=(
            "Also remove from the target app any podcast no longer in Apple Podcasts. "
            "Applies to --sync-pocketcasts and --sync-overcast."
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Actually execute destructive operations: --unsubscribe, "
            "--sync-pocketcasts, --sync-overcast."
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

    # Overcast sync
    oc_group = parser.add_argument_group(
        "Overcast sync",
        "Uses the unofficial Overcast web interface — may break on UI changes.",
    )
    oc_group.add_argument(
        "--sync-overcast",
        action="store_true",
        help=(
            "Sync Apple Podcasts subscriptions to Overcast. "
            "Dry-run by default; add --confirm to apply."
        ),
    )
    oc_group.add_argument(
        "--oc-email",
        metavar="EMAIL",
        help="Overcast email (or set OVERCAST_EMAIL env var).",
    )
    oc_group.add_argument(
        "--oc-password",
        metavar="PASSWORD",
        help="Overcast password (or set OVERCAST_PASSWORD env var).",
    )

    # Castro sync
    castro_group = parser.add_argument_group(
        "Castro sync",
        "Castro has no public API — generates an OPML file for manual import.",
    )
    castro_group.add_argument(
        "--sync-castro",
        action="store_true",
        help=(
            "Generate an OPML file for Castro import. "
            "Use --output to save to a file."
        ),
    )

    parser.add_argument(
        "--dupes",
        action="store_true",
        help="Report podcasts subscribed more than once (http/https and trailing-slash aware).",
    )
    parser.add_argument(
        "--unsubscribe",
        action="store_true",
        help=(
            "Remove matched podcasts from the Apple Podcasts database. "
            "Combine with --stale or --since. Dry-run by default; add --confirm to apply."
        ),
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
        since_date: Optional[datetime] = cast(Optional[datetime], args.since)
        subscribed_only: bool = cast(bool, args.subscribed_only)

        # Unified cutoff: either --stale (relative) or --since (absolute)
        cutoff: Optional[datetime] = None
        if stale_delta is not None:
            cutoff = datetime.now(tz=timezone.utc) - stale_delta
        elif since_date is not None:
            cutoff = since_date

        include_date = cutoff is not None or date_column is not None

        if cutoff is not None and date_column is None:
            raise SystemExit(
                f"--stale/--since requires a date column in the database, "
                f"but none of {DATE_COLUMNS} were found. "
                f"Run --schema to inspect the table."
            )

        if cast(bool, args.stats):
            all_podcasts = list(
                get_podcasts(connection, subscribed_only=False, date_column=date_column)
            )
            cmd_stats(all_podcasts, date_column)
            return

        if cast(bool, args.broken):
            all_podcasts = list(get_podcasts(connection, subscribed_only=True))
            cmd_broken(
                all_podcasts,
                timeout=cast(int, args.timeout),
                workers=cast(int, args.workers),
            )
            return

        if cast(Optional[str], args.diff):
            all_podcasts = list(get_podcasts(connection, subscribed_only=True))
            cmd_diff(all_podcasts, Path(cast(str, args.diff)))
            return

        if cast(bool, args.dupes):
            all_podcasts = list(get_podcasts(connection, subscribed_only=True))
            cmd_dupes(all_podcasts)
            return

        if cast(bool, args.sync_castro):
            all_podcasts = list(get_podcasts(connection, subscribed_only=True))
            out_path = (
                Path(cast(str, args.output)) if cast(Optional[str], args.output) else None
            )
            cmd_sync_castro(all_podcasts, out_path, title=cast(str, args.title))
            return

        if cast(bool, args.sync_pocketcasts):
            email: str = _resolve_credential(
                os.environ.get("POCKETCASTS_EMAIL") or cast(str, args.pc_email or "")
            )
            password: str = _resolve_credential(
                os.environ.get("POCKETCASTS_PASSWORD") or cast(str, args.pc_password or "")
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

        if cast(bool, args.sync_overcast):
            oc_email: str = _resolve_credential(
                os.environ.get("OVERCAST_EMAIL") or cast(str, args.oc_email or "")
            )
            oc_password: str = _resolve_credential(
                os.environ.get("OVERCAST_PASSWORD") or cast(str, args.oc_password or "")
            )
            if not oc_email or not oc_password:
                raise SystemExit(
                    "Overcast credentials required.\n"
                    "Set OVERCAST_EMAIL / OVERCAST_PASSWORD env vars "
                    "or use --oc-email / --oc-password."
                )
            apple_feeds_oc = [
                p.feed_url
                for p in get_podcasts(connection, subscribed_only=True)
                if p.feed_url
            ]
            cmd_sync_overcast(
                apple_feeds=apple_feeds_oc,
                email=oc_email,
                password=oc_password,
                sync_remove=cast(bool, args.sync_remove),
                confirm=cast(bool, args.confirm),
            )
            return

        podcast_iter: Iterator[Podcast] = get_podcasts(
            connection,
            subscribed_only=subscribed_only,
            date_column=date_column,
        )

        if cutoff is not None:
            podcast_iter = filter_stale(podcast_iter, cutoff)
            print(
                f"Podcasts with no activity since "
                f"{cutoff.strftime('%Y-%m-%d')} "
                f"(field: {date_column}):",
                file=sys.stderr,
            )

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
