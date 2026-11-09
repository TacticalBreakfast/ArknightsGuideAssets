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
  source         log      dates stamped by this script
                 seed     pre-log operator whose CN date/patch was supplied by hand
                 pre-log  already in the data when the log started, no extra info
                 manual   hand-entered dates
  note           free text, never touched by this script.

"pre-log" means the operator was already in that server's data when the log
began (LOG_START), so its real date is unknown. Hand-editing is expected: put
real dates over "pre-log", fix a cn_patch, or set source to manual. This script
only ever ADDS rows and fills BLANK en_first_seen cells, so edits are safe.

Patches: a new CN operator joins the newest patch if it appears within
PATCH_GAP_DAYS of that patch's first date, otherwise it starts a new patch.
This keeps late same-patch additions (e.g. an Integrated Strategies unit a week
after the event operators) in one patch. Correct cn_patch by hand if it guesses
wrong.

Usage:
  python3 parse_release_log.py              daily update (stamps today, UTC)
  python3 parse_release_log.py --backfill   rebuild log dates from git history.
      Local only: needs full history, and CI checks out shallow. Preserves
      seed/manual rows and every note.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

OUTPUT = Path("processed/release_log.csv")
FIELDS = ["char_id", "name", "cn_first_seen", "en_first_seen", "cn_patch", "source", "note"]
PRE_LOG = "pre-log"
LOG_START = "2026-06-25"  # first sync of the excel data into this repo
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
        # pre-log rows first (by id), then patches oldest -> newest, so new
        # operators append at the bottom and daily diffs stay small.
        return (row["cn_patch"], row["cn_first_seen"] if row["cn_first_seen"] != PRE_LOG else "", row["char_id"])

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(rows.values(), key=key))


def assign_patch(rows: dict, day: str) -> str:
    """cn_patch for an operator first seen on `day`."""
    # Only patches that started on or before `day`: a backfilled older day
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


def git(*args: str) -> str:
    # TZ=UTC so format-local dates match the UTC dates the daily run stamps.
    env = {**os.environ, "TZ": "UTC"}
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, env=env).stdout


def history_versions(path: str) -> list[tuple[str, str]]:
    """[(commit, UTC date)] for every commit touching path, oldest first."""
    out = git("-c", "log.showSignature=false", "log", "--reverse",
              "--date=format-local:%Y-%m-%d", "--format=%H %cd", "--", path)
    return [tuple(line.split()) for line in out.splitlines()]


def backfill(rows: dict, cn: dict, en: dict) -> None:
    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        sys.exit("--backfill needs full git history (this clone is shallow).")

    # Drop dates previously stamped by this script; keep everything hand-made.
    for char_id in list(rows):
        if rows[char_id]["source"] in ("log", PRE_LOG) and not rows[char_id]["note"]:
            del rows[char_id]
        elif rows[char_id]["source"] == "log":
            rows[char_id].update(cn_first_seen="", en_first_seen="", cn_patch="")
        elif rows[char_id]["source"] == "seed":
            # The seed supplies the CN date/patch; its EN date came from this script.
            rows[char_id]["en_first_seen"] = ""

    # Merge both tables' histories into one timeline per server.
    for server, (char_path, patch_path) in TABLES.items():
        events = sorted({(d, h) for h, d in history_versions(char_path) + history_versions(patch_path)})
        first_day = events[0][0]
        for day, commit in events:
            def show(path):
                try:
                    return json.loads(git("show", f"{commit}:{path}"))
                except subprocess.CalledProcessError:
                    return None
            present = operators(show(char_path) or {}, show(patch_path))
            stamp = PRE_LOG if day == first_day else day
            for char_id in sorted(present):
                record(rows, char_id, server, stamp, cn, en)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backfill", action="store_true", help="rebuild log dates from git history")
    args = parser.parse_args()

    cn, en = load_current("cn"), load_current("en")
    rows = read_log()
    if not rows and not args.backfill:
        # Without a baseline every existing operator would be stamped today.
        sys.exit(f"{OUTPUT} is missing or empty; create it locally with --backfill.")
    before = {k: dict(v) for k, v in rows.items()}

    if args.backfill:
        backfill(rows, cn, en)

    today = datetime.now(timezone.utc).date().isoformat()
    for server, present in (("cn", cn), ("en", en)):
        for char_id in sorted(present):
            if record(rows, char_id, server, today, cn, en):
                if char_id not in before or not before[char_id][f"{server}_first_seen"]:
                    print(f"{server.upper()} new: {char_id} ({display_name(char_id, cn, en)}) -> {rows[char_id][f'{server}_first_seen']}")

    for char_id, row in rows.items():
        row["name"] = display_name(char_id, cn, en) or row["name"]

    write_log(rows)
    print(f"Wrote {len(rows)} operators to {OUTPUT}")


if __name__ == "__main__":
    main()
