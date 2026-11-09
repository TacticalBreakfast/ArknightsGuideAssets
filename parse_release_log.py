#!/usr/bin/env python3
"""Maintain processed/release_log.csv: when each operator first appeared in the
CN and EN game data.

The game files carry no reliable release date (module and skin dates belong to
those items, not the operator), so this log records the date an operator id
first shows up in this repo's synced excel data. In practice that is within a
day of the real release, because the sync runs daily.

Columns:
  char_id        game id; the stable key.
  name           EN name if the operator is on EN, else the CN appellation.
                 Display only -- refreshed on every run, never used as a key.
  cn_first_seen  YYYY-MM-DD, "pre-log", or blank.
  en_first_seen  YYYY-MM-DD, "pre-log", or blank (= not on EN yet).
  cn_patch       CN patch key: the YYYY-MM-DD its first operator appeared.
                 Operators sharing a key are one patch; sort keys to order
                 patches. Blank if unknown. Dates rather than numbers so an
                 older patch can be filled in later without renumbering.
  source         log      dates stamped by the daily run
                 seed     CN date/patch supplied by hand, EN date stamped by this script
                 pre-log  already in the data before any history we have, no date
                 manual   dates entered by hand or from --history; NEVER changed
  note           free text, never touched by this script (except --history adding
                 where its dates came from).

"pre-log" means the operator was already in that server's data before the
earliest history available (2023-12-24, ArknightsAssets/ArknightsGamedata), so
its real date is unknown.

HAND EDITS: correct a date by editing it AND setting source to "manual" -- the
data repo isn't always the official date (e.g. Bellone and Ripresa reached EN on
2026-08-12, a day before the data did). The daily run only ever adds rows and
fills BLANK en_first_seen cells, so it never changes any existing date; but
--history corrects dates this script stamped (log rows, seed rows' EN dates),
so only "manual" keeps an edit safe from it.

Patches: when the daily run (or --history) stamps a new CN operator, it joins
the newest patch if it appears within PATCH_GAP_DAYS of that patch's first
date, otherwise it starts a new patch. This keeps late same-patch additions
(e.g. an Integrated Strategies unit a week after the event operators) in one
patch. That grouping is only for dates derived from the data: a CN date
entered by hand is its own patch key (cn_patch = the date), so set both.

Usage:
  python3 parse_release_log.py              daily update. New operators are stamped
      with the date the upstream repo our sync copies from
      (ArknightsAssets/ArknightsGamedata) last changed that server's
      character_table.json -- i.e. when the data was released, not when our
      daily sync picked it up -- in China time for CN and UTC for EN, the same
      clock --history uses. Falls back to today (UTC) if GitHub can't be
      reached. Set GITHUB_TOKEN to avoid the API's anonymous rate limit.
  python3 parse_release_log.py --history DIR
      Date "pre-log" operators from another repo's history of the same excel
      tables -- a clone of ArknightsAssets/ArknightsGamedata (history from
      2023-12-24; a blob-less partial clone is enough: git clone
      --filter=blob:none --no-checkout). Fills only "pre-log" cells; those rows
      become source "manual" (so nothing ever overwrites them) with a note
      naming the source. Operators already in that repo's first snapshot stay
      "pre-log". An operator first seen right after a long silence in that
      history (see HISTORY_MAX_GAP_DAYS) gets the first-seen date -- the
      latest it could be -- with the window in its note and, for CN, no
      cn_patch (it would form a fake patch). It also corrects dates this
      script stamped (log rows, seed rows' EN dates) where the history
      differs; "manual" rows and seed CN dates are never changed. CN dates are
      in China time (UTC+8), so they match CN patch dates; EN dates are UTC,
      like the daily run. Local only.
  (There used to be a --backfill that rebuilt dates from this repo's own
  history. --history replaced it: same upstream data, longer and without our
  sync's delay -- --backfill would have reset corrected dates to later ones.)
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

OUTPUT = Path("processed/release_log.csv")
FIELDS = ["char_id", "name", "cn_first_seen", "en_first_seen", "cn_patch", "source", "note"]
PRE_LOG = "pre-log"
PATCH_GAP_DAYS = 14

# Same rule as parse_characters.py: summons and deployables share the schema.
NON_CHARACTER_PROFESSIONS = {"TRAP", "TOKEN"}

TABLES = {
    "cn": ("excel-cn/character_table.json", "excel-cn/char_patch_table.json"),
    "en": ("excel-en/character_table.json", "excel-en/char_patch_table.json"),
}


def operators(char_table: dict, patch_table: dict | None) -> dict:
    """Return {char_id: data} for obtainable operators, including alternate
    forms (Amiya Guard/Medic), which live in char_patch_table's patchChars."""
    merged = {**char_table, **((patch_table or {}).get("patchChars") or {})}
    return {
        k: v for k, v in merged.items()
        if k.startswith("char_")
        and v.get("profession") not in NON_CHARACTER_PROFESSIONS
        and not v.get("isNotObtainable")
    }


def load_current(server: str) -> dict:
    char_path, patch_path = TABLES[server]
    with open(char_path, encoding="utf-8") as f:
        chars = json.load(f)
    with open(patch_path, encoding="utf-8") as f:
        patch = json.load(f)
    return operators(chars, patch)


def display_name(char_id: str, cn: dict, en: dict) -> str:
    if char_id in en:
        return en[char_id].get("name") or ""
    data = cn.get(char_id, {})
    return data.get("appellation") or data.get("name") or ""


def read_log() -> dict:
    if not OUTPUT.exists():
        return {}
    with OUTPUT.open(encoding="utf-8", newline="") as f:
        return {row["char_id"]: {k: row.get(k, "") for k in FIELDS} for row in csv.DictReader(f)}


def write_log(rows: dict) -> None:
    def key(row):
        # By CN release date, oldest first (easiest to review), ties by id so
        # the order is stable. Rows without a CN date ("pre-log") come first.
        # New operators have the newest dates, so they append at the bottom and
        # daily diffs stay small.
        cn_date = row["cn_first_seen"] if row["cn_first_seen"][:1].isdigit() else ""
        return (cn_date, row["char_id"])

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(rows.values(), key=key))


def assign_patch(rows: dict, day: str) -> str:
    """cn_patch for an operator first seen on `day`."""
    # Only patches that started on or before `day`: an older day from --history
    # must not join a newer patch.
    earlier = [r["cn_patch"] for r in rows.values() if r["cn_patch"] and r["cn_patch"] <= day]
    if earlier:
        latest = max(earlier)
        if (date.fromisoformat(day) - date.fromisoformat(latest)).days < PATCH_GAP_DAYS:
            return latest
    return day


def record(rows: dict, char_id: str, server: str, day: str, cn: dict, en: dict) -> bool:
    """Note that char_id is present on `server` as of `day`. Returns True if
    anything changed."""
    row = rows.get(char_id)
    if row is None:
        row = dict.fromkeys(FIELDS, "")
        row.update(char_id=char_id, source="log")
        if server == "cn":
            row["cn_first_seen"] = day
            row["cn_patch"] = "" if day == PRE_LOG else assign_patch(rows, day)
        else:
            row["en_first_seen"] = day
        if day == PRE_LOG:
            row["source"] = PRE_LOG
        rows[char_id] = row
        return True
    column = f"{server}_first_seen"
    if not row[column]:
        row[column] = day
        if server == "cn" and not row["cn_patch"] and day != PRE_LOG:
            row["cn_patch"] = assign_patch(rows, day)
        return True
    return False


HISTORY_PATHS = {
    "cn": ("cn/gamedata/excel/character_table.json", "cn/gamedata/excel/char_patch_table.json"),
    "en": ("en/gamedata/excel/character_table.json", "en/gamedata/excel/char_patch_table.json"),
}
HISTORY_TZ = {"cn": 8, "en": 0}  # hours from UTC used to turn commit times into dates
HISTORY_NOTE = "dated from ArknightsAssets/ArknightsGamedata history"
# A first appearance right after a longer silence in the history can't be
# trusted: the operator arrived somewhere in the silence. (Real patches are at
# most ~5 weeks apart; CN data, for one, wasn't updated 2025-03-20 to 07-30.)
HISTORY_MAX_GAP_DAYS = 40


UPSTREAM_REPO = "ArknightsAssets/ArknightsGamedata"  # what sparse_sync_jsons.yml copies from


def upstream_day(server: str) -> str | None:
    """Date (server clock, see HISTORY_TZ) the upstream repo last changed this
    server's character_table.json, or None if GitHub can't be asked."""
    query = urllib.parse.urlencode({"path": HISTORY_PATHS[server][0], "per_page": 1})
    request = urllib.request.Request(f"https://api.github.com/repos/{UPSTREAM_REPO}/commits?{query}",
                                     headers={"Accept": "application/vnd.github+json"})
    if os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            committed = json.load(response)[0]["commit"]["committer"]["date"]
    except Exception as error:  # network, rate limit, unexpected payload
        print(f"WARNING: couldn't read the upstream {server.upper()} date ({error}); using today", file=sys.stderr)
        return None
    moment = datetime.fromisoformat(committed.replace("Z", "+00:00"))
    return moment.astimezone(timezone(timedelta(hours=HISTORY_TZ[server]))).date().isoformat()


def history_first_seen(repo: Path, server: str) -> tuple[dict, set, dict]:
    """From repo's history: ({char_id: first date seen}, ids already in the first
    snapshot, {char_id: (last date before the gap, first date seen)} for
    operators first seen right after a gap of more than HISTORY_MAX_GAP_DAYS)."""
    def run(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout

    tz = timezone(timedelta(hours=HISTORY_TZ[server]))
    commits = {}
    for path in HISTORY_PATHS[server]:
        for line in run("log", "--format=%H %ct", "--", path).splitlines():
            sha, ts = line.split()
            commits[sha] = int(ts)
    first_seen, baseline, uncertain, previous = {}, None, {}, None
    for sha, ts in sorted(commits.items(), key=lambda kv: kv[1]):
        def show(path):
            try:
                return json.loads(run("show", f"{sha}:{path}"))
            except subprocess.CalledProcessError:
                return None   # file not present in this commit
        char_path, patch_path = HISTORY_PATHS[server]
        present = set(operators(show(char_path) or {}, show(patch_path)))
        day = datetime.fromtimestamp(ts, tz).date().isoformat()
        if baseline is None:
            baseline, previous = present, day
            continue
        gap = (date.fromisoformat(day) - date.fromisoformat(previous)).days
        for char_id in present - baseline:
            if char_id not in first_seen:
                first_seen[char_id] = day
                if gap > HISTORY_MAX_GAP_DAYS:
                    uncertain[char_id] = (previous, day)
        previous = day
    return first_seen, baseline or set(), uncertain


def history_backfill(rows: dict, repo: Path) -> None:
    found = {server: history_first_seen(repo, server) for server in HISTORY_PATHS}
    filled = {"cn": [], "en": []}
    unsure = {"cn": [], "en": []}
    differences = []
    for server, (first_seen, _, uncertain) in found.items():
        column = f"{server}_first_seen"
        for char_id, day in sorted(first_seen.items(), key=lambda kv: kv[1]):
            row = rows.get(char_id)
            if row is None:
                continue  # no longer an obtainable operator
            if row[column] == PRE_LOG and char_id in uncertain:
                # The history was silent before this, so the real date is
                # somewhere in that window: keep the first-seen date as the
                # latest possible one, say so, and don't put it in a CN patch.
                after, by = uncertain[char_id]
                row[column] = day
                row["source"] = "manual"
                window = (f"{server.upper()} released between {after} and {by}, date shown is the latest "
                          f"possible (gap in ArknightsGamedata history)")
                note = row["note"] or HISTORY_NOTE
                row["note"] = note if window in note else f"{note}; {window}"
                unsure[server].append(char_id)
            elif row[column] == PRE_LOG:
                row[column] = day
                if server == "cn":
                    row["cn_patch"] = assign_patch(rows, day)
                row["source"] = "manual"
                row["note"] = row["note"] or HISTORY_NOTE
                filled[server].append(char_id)
            elif row[column][:1].isdigit() and row[column] != day:
                gap = (date.fromisoformat(row[column]) - date.fromisoformat(day)).days
                # Dates this script stamped from our own sync (log rows, and the
                # EN dates of seed rows) lag the real release by the sync delay:
                # the upstream history is more accurate, so take it. Hand-entered
                # dates (seed CN dates, manual rows) are never changed.
                stamped = row["source"] == "log" or (row["source"] == "seed" and server == "en")
                corrected = stamped and char_id not in uncertain
                differences.append((char_id, server, row[column], day, gap,
                                    row["source"] + (", corrected" if corrected else ", kept")))
                if corrected:
                    row[column] = day
    for server in filled:
        print(f"{server.upper()}: dated {len(filled[server]) + len(unsure[server])} pre-log operators, "
              f"of them {len(unsure[server])} only to within a window (first seen after a gap; see the note); "
              f"{len(found[server][1])} were already in the history's first snapshot")
    if differences:
        print("Dates that differed from the history:")
        for char_id, server, ours, theirs, gap, source in differences:
            print(f"  {char_id} {server.upper()}: log {ours} ({source}), history {theirs} ({gap:+d} days)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history", type=Path, metavar="DIR",
                        help="date pre-log operators from a clone of ArknightsAssets/ArknightsGamedata")
    args = parser.parse_args()

    cn, en = load_current("cn"), load_current("en")
    rows = read_log()
    if not rows:
        # Without a baseline every existing operator would be stamped today.
        sys.exit(f"{OUTPUT} is missing or empty; restore it from git -- it holds hand-entered dates.")
    before = {k: dict(v) for k, v in rows.items()}

    if args.history:
        history_backfill(rows, args.history)

    today = datetime.now(timezone.utc).date().isoformat()
    for server, present in (("cn", cn), ("en", en)):
        # Only ask GitHub when there's something new to stamp.
        new = [c for c in present if c not in rows or not rows[c][f"{server}_first_seen"]]
        day = (upstream_day(server) or today) if new else today
        for char_id in sorted(present):
            if record(rows, char_id, server, day, cn, en):
                if char_id not in before or not before[char_id][f"{server}_first_seen"]:
                    print(f"{server.upper()} new: {char_id} ({display_name(char_id, cn, en)}) -> {rows[char_id][f'{server}_first_seen']}")

    for char_id, row in rows.items():
        row["name"] = display_name(char_id, cn, en) or row["name"]

    write_log(rows)
    print(f"Wrote {len(rows)} operators to {OUTPUT}")


if __name__ == "__main__":
    main()
