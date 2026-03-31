# macOS Podcasts OPML

Export, cleanup, and sync your Apple Podcasts subscriptions.

Apple's Catalyst Podcasts app has no OPML export. This tool reads directly from the SQLite database, exports in multiple formats, removes stale subscriptions, and syncs to Pocket Casts.

**Requirements:** Python 3.9+ · macOS · no external dependencies

---

## Installation

```bash
chmod +x macos-podcasts-opml.py
```

The database is auto-detected at:
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
# List all podcast titles
./macos-podcasts-opml.py --list

# Show database schema (useful to verify date columns)
./macos-podcasts-opml.py --schema
```

### Cleanup — stale podcasts

Identifies podcasts with no activity for a configurable duration.
Duration syntax: `1y` (1 year) · `6m` (6 months) · `90d` (90 days)

```bash
# Preview stale podcasts (dry-run)
./macos-podcasts-opml.py --stale 2y --list

# Export stale list as OPML for review
./macos-podcasts-opml.py --stale 2y -f opml -o stale.opml

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
# Set credentials via environment variables (recommended)
export POCKETCASTS_EMAIL=you@example.com
export POCKETCASTS_PASSWORD=yourpassword

# Preview what would change (dry-run)
./macos-podcasts-opml.py --sync-pocketcasts

# Apply — subscribe to new podcasts in Pocket Casts
./macos-podcasts-opml.py --sync-pocketcasts --confirm

# Full two-way sync (add new + remove deleted)
./macos-podcasts-opml.py --sync-pocketcasts --sync-remove --confirm

# Credentials via flags (less safe — visible in shell history)
./macos-podcasts-opml.py --sync-pocketcasts \
  --pc-email you@example.com \
  --pc-password yourpassword \
  --confirm
```

> Uses the unofficial Pocket Casts API. May break if Pocket Casts changes their API.

---

## Recommended workflow

```bash
# 1. Review inactive podcasts
./macos-podcasts-opml.py --stale 2y --list

# 2. Remove them from Apple Podcasts (close the app first)
./macos-podcasts-opml.py --stale 2y --unsubscribe --confirm

# 3. Sync the cleaned-up list to Pocket Casts
./macos-podcasts-opml.py --sync-pocketcasts --sync-remove --confirm
```

---

## All options

```
usage: macos-podcasts-opml [-h] [--db PATH] [--output FILE] [--format {opml,json,csv}]
                            [--title TITLE] [--list] [--subscribed-only]
                            [--stale DURATION] [--schema]
                            [--sync-pocketcasts] [--pc-email EMAIL]
                            [--pc-password PASSWORD] [--sync-remove]
                            [--unsubscribe] [--confirm] [--version]

options:
  --db PATH               Path to the Podcasts SQLite database (auto-detected if omitted)
  -o, --output FILE       Write output to FILE instead of stdout
  -f, --format            Output format: opml (default), json, csv
  --title TITLE           OPML <head> title (default: 'MacOS Podcasts')
  --list                  List podcast titles without generating output
  --subscribed-only       Only include podcasts with a valid feed URL
  --stale DURATION        Filter to podcasts inactive for the given duration (1y / 6m / 90d)
  --schema                Print ZMTPODCAST table schema and exit

Pocket Casts sync:
  --sync-pocketcasts      Sync Apple Podcasts subscriptions to Pocket Casts
  --pc-email EMAIL        Pocket Casts email (or POCKETCASTS_EMAIL env var)
  --pc-password PASSWORD  Pocket Casts password (or POCKETCASTS_PASSWORD env var)
  --sync-remove           Also remove from Pocket Casts what is not in Apple Podcasts

  --unsubscribe           Remove matched podcasts from the Apple Podcasts database
  --confirm               Execute --unsubscribe or --sync-pocketcasts (dry-run otherwise)
  --version               Show version and exit
```

---

## Development

```bash
# Run tests
pip install pytest
python -m pytest tests/ -v

# Type checking
pip install mypy
mypy macos-podcasts-opml.py
```
