# macOS Podcasts OPML

Export, clean up, and sync your Apple Podcasts subscriptions.

Apple's Catalyst Podcasts app has no OPML export. This tool reads directly from the SQLite database, exports in multiple formats, removes stale subscriptions, checks feed health, and syncs to Pocket Casts and Overcast.

**Requirements:** Python 3.10+ · macOS · no external dependencies

---

## Installation

```bash
chmod +x macos-podcasts-opml.py
```

The Apple Podcasts database is auto-detected at:
```
~/Library/Group Containers/*.groups.com.apple.podcasts/Documents/MTLibrary.sqlite
```

Use `--db PATH` to override (e.g. when working from a backup or Time Machine restore).

---

## Usage

### Export

```bash
# OPML to stdout (default)
./macos-podcasts-opml.py

# Write to file
./macos-podcasts-opml.py -o podcasts.opml

# JSON or CSV
./macos-podcasts-opml.py -f json -o podcasts.json
./macos-podcasts-opml.py -f csv  -o podcasts.csv

# Only active subscriptions (excludes feed-less entries)
./macos-podcasts-opml.py --subscribed-only -o podcasts.opml

# Custom OPML title
./macos-podcasts-opml.py --title "My Podcasts 2026" -o podcasts.opml
```

### Inspect

```bash
# List all podcast titles with last activity date
./macos-podcasts-opml.py --list

# Show database schema (verify date columns available)
./macos-podcasts-opml.py --schema

# Subscription statistics — count, coverage, activity histogram
./macos-podcasts-opml.py --stats

# Compare subscriptions against an existing OPML file
./macos-podcasts-opml.py --diff old-export.opml

# Detect duplicate subscriptions (http/https, trailing slash aware)
./macos-podcasts-opml.py --dupes
```

### Feed health check

Check all feed URLs via HTTP HEAD and report any that are unreachable (404, timeout, etc.):

```bash
./macos-podcasts-opml.py --broken

# Tune parallelism and timeout
./macos-podcasts-opml.py --broken --workers 20 --timeout 15
```

### Cleanup — stale podcasts

Filter and optionally remove podcasts with no recent activity.
Duration syntax: `1y` · `6m` · `90d`  
Or use an absolute date with `--since`:

```bash
# Preview stale podcasts (relative duration)
./macos-podcasts-opml.py --stale 2y --list

# Preview stale podcasts (absolute date)
./macos-podcasts-opml.py --since 2023-01-01 --list

# Export stale list for review
./macos-podcasts-opml.py --stale 2y -f json -o stale.json

# Remove stale podcasts from Apple Podcasts database (dry-run)
./macos-podcasts-opml.py --stale 2y --unsubscribe

# Actually remove (Podcasts.app must be closed)
./macos-podcasts-opml.py --stale 2y --unsubscribe --confirm
```

> A timestamped backup (`MTLibrary.backup-YYYYMMDD-HHMMSS.sqlite`) is created
> automatically before any write. Podcasts.app must be **closed** when using `--confirm`.

### Sync to Pocket Casts

Compares Apple Podcasts subscriptions with Pocket Casts and adds the missing ones.
Use `--sync-remove` to also remove from Pocket Casts what is no longer in Apple Podcasts.

```bash
# Set credentials (see "Credential sources" below for secure options)
export POCKETCASTS_EMAIL=you@example.com
export POCKETCASTS_PASSWORD=yourpassword

# Preview what would change (dry-run)
./macos-podcasts-opml.py --sync-pocketcasts

# Apply
./macos-podcasts-opml.py --sync-pocketcasts --confirm

# Full two-way sync (add new + remove deleted)
./macos-podcasts-opml.py --sync-pocketcasts --sync-remove --confirm
```

> Uses the unofficial Pocket Casts API. May break if Pocket Casts changes their API.

### Sync to Overcast

Same as Pocket Casts sync, using the Overcast web interface.

```bash
export OVERCAST_EMAIL=you@example.com
export OVERCAST_PASSWORD=yourpassword

./macos-podcasts-opml.py --sync-overcast
./macos-podcasts-opml.py --sync-overcast --sync-remove --confirm
```

> Uses the unofficial Overcast web API. May break if Overcast changes their UI.

### Sync to Castro

Castro has no public API. This generates an OPML file for manual import.

```bash
# Generate OPML and print import instructions
./macos-podcasts-opml.py --sync-castro -o castro.opml
```

Then in Castro: **Settings (gear icon) → Import Subscriptions → select the file**.

---

## Credential sources

Credentials can be provided as plain strings, but it is strongly recommended to use a
secret manager — especially for automated runs via launchd.

### Environment variables (default)

```bash
export POCKETCASTS_EMAIL=you@example.com
export POCKETCASTS_PASSWORD=mysecretpassword
```

### 1Password CLI

Use an `op://` URI. The `op` CLI must be installed and signed in.

```bash
export POCKETCASTS_PASSWORD="op://Personal/Pocket Casts/password"
export OVERCAST_PASSWORD="op://Personal/Overcast/password"

# Or via flags
./macos-podcasts-opml.py --sync-pocketcasts \
  --pc-email you@example.com \
  --pc-password "op://Personal/Pocket Casts/password"
```

Install the 1Password CLI: https://developer.1password.com/docs/cli/

### macOS Keychain

Use a `keychain:SERVICE` or `keychain:SERVICE:ACCOUNT` reference.

```bash
# Store the password first
security add-generic-password -s PocketCasts -a you@example.com -w

# Then reference it
export POCKETCASTS_EMAIL=you@example.com
export POCKETCASTS_PASSWORD="keychain:PocketCasts:you@example.com"
```

---

## Recommended workflow

```bash
# 1. Check for broken feeds and duplicates
./macos-podcasts-opml.py --broken
./macos-podcasts-opml.py --dupes

# 2. Review subscription stats and stale list
./macos-podcasts-opml.py --stats
./macos-podcasts-opml.py --stale 2y --list

# 3. Remove stale from Apple Podcasts (close the app first)
./macos-podcasts-opml.py --stale 2y --unsubscribe --confirm

# 4. Sync the cleaned-up list to Pocket Casts
./macos-podcasts-opml.py --sync-pocketcasts --sync-remove --confirm

# 5. Sync to Overcast
./macos-podcasts-opml.py --sync-overcast --sync-remove --confirm
```

---

## Automation with launchd

A launchd plist template for daily sync is provided at
`launchd/com.github.macos-podcasts-opml.sync.plist`.

```bash
# Edit the plist, then install it
cp launchd/com.github.macos-podcasts-opml.sync.plist \
   ~/Library/LaunchAgents/

# Edit REPLACE_WITH_* placeholders, then load
launchctl load ~/Library/LaunchAgents/com.github.macos-podcasts-opml.sync.plist

# Logs
tail -f ~/Library/Logs/macos-podcasts-opml-sync.log
```

A macOS notification is sent after each sync completes (on macOS only).

---

## All options

```
usage: macos-podcasts-opml [-h] [--db PATH] [-o FILE] [-f {opml,json,csv}]
                            [--title TITLE] [--list] [--subscribed-only]
                            [--stale DURATION | --since DATE]
                            [--stats] [--broken] [--workers N] [--timeout SECS]
                            [--diff OPML_FILE] [--dupes]
                            [--sync-remove] [--confirm]
                            [--sync-pocketcasts] [--pc-email EMAIL] [--pc-password PASSWORD]
                            [--sync-overcast] [--oc-email EMAIL] [--oc-password PASSWORD]
                            [--sync-castro]
                            [--unsubscribe] [--schema] [--version]

options:
  --db PATH               Path to the Podcasts SQLite database (auto-detected)
  -o, --output FILE       Write output to FILE instead of stdout
  -f, --format            Output format: opml (default), json, csv
  --title TITLE           OPML <head> title (default: 'MacOS Podcasts')
  --list                  List podcast titles without generating output
  --subscribed-only       Only include podcasts with a valid feed URL
  --stale DURATION        Filter to podcasts inactive for the given duration (1y / 6m / 90d)
  --since DATE            Filter to podcasts inactive since DATE (e.g. 2024-01-01)
  --stats                 Print subscription statistics
  --broken                Check all feed URLs via HTTP HEAD and report broken ones
  --workers N             Parallel workers for --broken (default: 10)
  --timeout SECS          HTTP timeout for --broken (default: 10)
  --diff OPML_FILE        Compare subscriptions against an existing OPML file
  --dupes                 Report podcasts subscribed more than once
  --schema                Print ZMTPODCAST table schema and exit
  --unsubscribe           Remove matched podcasts from the Apple Podcasts database
  --confirm               Execute --unsubscribe / --sync-* (dry-run by default)
  --sync-remove           Remove from target app what is no longer in Apple Podcasts
  --version               Show version and exit

Pocket Casts sync:
  --sync-pocketcasts      Sync to Pocket Casts (dry-run by default)
  --pc-email EMAIL        Pocket Casts email (or POCKETCASTS_EMAIL env var)
  --pc-password PASSWORD  Pocket Casts password (or POCKETCASTS_PASSWORD env var)

Overcast sync:
  --sync-overcast         Sync to Overcast (dry-run by default)
  --oc-email EMAIL        Overcast email (or OVERCAST_EMAIL env var)
  --oc-password PASSWORD  Overcast password (or OVERCAST_PASSWORD env var)

Castro sync:
  --sync-castro           Generate OPML for Castro import (--output to save to file)
```

Credentials for `--pc-password`, `--oc-password` and their env var equivalents support:
- Literal value
- `op://vault/item/field` — 1Password CLI
- `keychain:SERVICE` or `keychain:SERVICE:ACCOUNT` — macOS Keychain

---

## Development

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run tests
python -m pytest tests/ -v

# Type checking
mypy macos-podcasts-opml.py

# Coverage
pytest tests/ --cov=. --cov-report=term-missing
```

CI runs automatically on push via GitHub Actions (`.github/workflows/ci.yml`).
