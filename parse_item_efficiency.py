#!/usr/bin/env python3
"""Refine gamedata-manual/item-eff.json (hand-exported from yituliu's material
value tool) by adding each item's EN readable name, looked up via itemId in
excel-en/item_table.json."""

import json
from pathlib import Path

SOURCE_PATH = Path("gamedata-manual/item-eff.json")
OUTPUT_PATH = Path("processed/item_efficiency.json")


def load_en_names() -> dict:
    with Path("excel-en/item_table.json").open(encoding="utf-8") as f:
        items = json.load(f)["items"]
    return {item_id: data["name"] for item_id, data in items.items()}


def load_cn_ids() -> set:
    with Path("excel-cn/item_table.json").open(encoding="utf-8") as f:
        return set(json.load(f)["items"])


def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with SOURCE_PATH.open(encoding="utf-8") as f:
        items = json.load(f)

    en_names = load_en_names()
    cn_ids = load_cn_ids()

    not_yet_localized = 0
    not_a_real_item = 0

    for item in items:
        en_name = en_names.get(item["id"])
        item["enName"] = en_name
        if en_name is None:
            if item["id"] in cn_ids:
                # Exists in excel-cn but hasn't reached excel-en yet -- will
                # resolve on its own once EN localization catches up.
                not_yet_localized += 1
            else:
                # Not a real itemId at all (e.g. yituliu's own bucket ids like
                # "voucher_recruitR6_pick" or "theme_scene") -- this will
                # never resolve via itemId lookup, in EN or CN.
                not_a_real_item += 1

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(items)} items to {OUTPUT_PATH}")
    print(f"  {not_yet_localized} missing EN name (in excel-cn, not yet in excel-en)")
    print(f"  {not_a_real_item} missing EN name (not a resolvable itemId in either)")


if __name__ == "__main__":
    main()
