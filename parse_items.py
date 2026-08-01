#!/usr/bin/env python3
"""Parse item_table.json and export material items to CSV."""

import csv
import json
from pathlib import Path


def load_items(path: str) -> dict:
    with Path(path).open(encoding="utf-8") as f:
        return json.load(f)["items"]


def main():
    output_path = Path("processed/items.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    en_items = load_items("excel-en/item_table.json")
    cn_items = load_items("excel-cn/item_table.json")

    # excel-en is the source of truth for English names. Items that only
    # exist in excel-cn haven't been localized/released in en yet, so they
    # fall back to their Chinese name until excel-en picks them up.
    cn_only = {k: v for k, v in cn_items.items() if k not in en_items}
    merged = {**en_items, **cn_only}

    materials = {
        item_id: item_data
        for item_id, item_data in merged.items()
        if item_id.startswith("3")
    }

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["itemId", "name"])
        for item_id, item_data in materials.items():
            writer.writerow([item_data["itemId"], item_data["name"]])

    print(f"Wrote {len(materials)} items to {output_path}")


if __name__ == "__main__":
    main()
