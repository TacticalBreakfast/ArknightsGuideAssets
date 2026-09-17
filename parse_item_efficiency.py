#!/usr/bin/env python3
"""Refine gamedata-manual/item-eff.json (hand-exported from yituliu's material
value tool) by adding each item's EN readable name, looked up via itemId in
excel-en/item_table.json."""

import json
from pathlib import Path

SOURCE_PATH = Path("gamedata-manual/item-eff.json")
OUTPUT_PATH = Path("processed/item_efficiency.json")

# Known cases where yituliu's item id doesn't correspond to any real itemId,
# but a differently-named real item is actually the same thing -- e.g.
# "rare_material_issue_voucher" (yituliu, from the CN name 稀有/"rare") vs.
# "precious_material_voucher_perm" (the real itemId, matching Yostar's EN
# localization "Precious" instead). Unlike its sibling tiers (premium/
# advanced), which have a real itemId matching yituliu's "*_issue_voucher"
# naming pattern, this tier only ever had one real itemId and it doesn't
# follow that pattern, so yituliu's tooling apparently invented a name rather
# than using it.
#
# Both the original entry and this alias are kept in the output: the alias so
# anything resolving against real game itemIds (e.g. parse_shop_packs.py)
# finds a value, the original so nothing already depending on yituliu's own
# id breaks.
MANUAL_ID_ALIASES = {
    "rare_material_issue_voucher": "precious_material_voucher_perm",
}


def load_en_names() -> dict:
    with Path("excel-en/item_table.json").open(encoding="utf-8") as f:
        items = json.load(f)["items"]
    return {item_id: data["name"] for item_id, data in items.items()}


def load_cn_ids() -> set:
    with Path("excel-cn/item_table.json").open(encoding="utf-8") as f:
        return set(json.load(f)["items"])


def add_manual_aliases(items: list) -> list:
    by_id = {item["id"]: item for item in items}
    aliases = []
    for yituliu_id, real_id in MANUAL_ID_ALIASES.items():
        source = by_id.get(yituliu_id)
        if source is None:
            continue
        alias = dict(source)
        alias["id"] = real_id
        aliases.append(alias)
    return items + aliases


def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with SOURCE_PATH.open(encoding="utf-8") as f:
        items = json.load(f)

    items = add_manual_aliases(items)

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
    print(f"  {len(MANUAL_ID_ALIASES)} manual id alias(es) added (see MANUAL_ID_ALIASES)")
    print(f"  {not_yet_localized} missing EN name (in excel-cn, not yet in excel-en)")
    print(f"  {not_a_real_item} missing EN name (not a resolvable itemId in either)")


if __name__ == "__main__":
    main()
