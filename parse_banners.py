#!/usr/bin/env python3
"""Parse excel-jp/gacha_table.json and export banner open/close windows to CSV."""

import calendar
import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SOURCE_PATH = Path("excel-jp/gacha_table.json")
OUTPUT_PATH = Path("processed/banners.csv")
FIELDNAMES = ["PoolID", "PoolType", "openTime", "closeTime", "new", "dateAdded"]


def format_date(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%m/%d/%Y").date()


def add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def epoch_to_jp_date(epoch: int) -> date:
    # openTime/endTime are stored as raw UTC epoch seconds, but the JP client
    # runs far enough ahead of that stored time that the correct calendar
    # date is always the day after the raw UTC date.
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date() + timedelta(days=1)


def split_pool_id(gacha_pool_id: str) -> tuple:
    """Split e.g. 'CLASSIC_DOUBLE_JP_41_0_5' into ('CLASSIC_DOUBLE', '41_0_5').
    PoolType can be more than one word (e.g. CLASSIC_DOUBLE, CLASSIC_ATTAIN),
    so split on the JP language token rather than the first underscore."""
    parts = gacha_pool_id.split("_")
    idx = parts.index("JP")
    return "_".join(parts[:idx]), "_".join(parts[idx + 1:])


def pool_id_sort_key(pool_id: str) -> tuple:
    return tuple(int(part) for part in pool_id.split("_"))


def load_existing_rows() -> list:
    if not OUTPUT_PATH.exists():
        return []
    with OUTPUT_PATH.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()

    with SOURCE_PATH.open(encoding="utf-8") as f:
        pools = json.load(f)["gachaPoolClient"]

    current = {}
    for pool in pools:
        pool_type, pool_id = split_pool_id(pool["gachaPoolId"])
        current[(pool_id, pool_type)] = {
            "openTime": format_date(epoch_to_jp_date(pool["openTime"])),
            "closeTime": format_date(epoch_to_jp_date(pool["endTime"])),
        }

    existing_rows = load_existing_rows()

    rows = []
    seen_keys = set()
    appended = []

    for row in existing_rows:
        key = (row["PoolID"], row["PoolType"])
        seen_keys.add(key)
        latest = current.get(key)

        if latest and (latest["openTime"] != row["openTime"] or latest["closeTime"] != row["closeTime"]):
            row["openTime"] = latest["openTime"]
            row["closeTime"] = latest["closeTime"]
            row["new"] = "MODIFIED"
            row["dateAdded"] = format_date(today)
            appended.append(row)
            continue

        if row["new"] and today >= add_months(parse_date(row["dateAdded"]), 1):
            row["new"] = ""
        rows.append(row)

    for (pool_id, pool_type), latest in current.items():
        if (pool_id, pool_type) in seen_keys:
            continue
        appended.append({
            "PoolID": pool_id,
            "PoolType": pool_type,
            "openTime": latest["openTime"],
            "closeTime": latest["closeTime"],
            "new": "NEW",
            "dateAdded": format_date(today),
        })

    # New/modified banners always land at the bottom, ordered by patch/sub/
    # banner number rather than by JSON order (which is not guaranteed to be
    # meaningful, and would otherwise make this non-deterministic run to run).
    appended.sort(key=lambda r: pool_id_sort_key(r["PoolID"]))
    rows.extend(appended)

    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} banners to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
