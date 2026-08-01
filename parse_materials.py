#!/usr/bin/env python3
"""Parse excel-cn/character_table.json and export Elite 2 promotion and skill
mastery material costs to CSV."""

import csv
from pathlib import Path
import json

LOW_RARITIES = {"TIER_1", "TIER_2", "TIER_3"}

PROFESSION_NAMES = {
    "WARRIOR":  "Guard",
    "SNIPER":   "Sniper",
    "TANK":     "Defender",
    "MEDIC":    "Medic",
    "SUPPORT":  "Supporter",
    "CASTER":   "Caster",
    "SPECIAL":  "Specialist",
    "PIONEER":  "Vanguard",
}


def rarity_number(tier: str):
    if tier and tier.startswith("TIER_") and tier[5:].isdigit():
        return int(tier[5:])
    return None


def cost_pair(costs: list) -> list:
    """Return exactly 2 (id, qty) pairs as 4 CSV values, blank-padded if
    fewer than 2 costs are present."""
    row = []
    for i in range(2):
        if i < len(costs):
            row.append(costs[i]["id"])
            row.append(str(costs[i]["count"]))
        else:
            row.append("")
            row.append("")
    return row


def evolve_cost_row(char_data: dict) -> list:
    phases = char_data.get("phases") or []
    if not phases:
        return cost_pair([])
    # The last entry in evolveCost's list is always a rarity-tagged token
    # shared by many characters (e.g. an elite promotion certificate); only
    # the materials after it vary per character, so it's skipped.
    costs = (phases[-1].get("evolveCost") or [])[1:]
    return cost_pair(costs)


def skill_mastery_row(char_data: dict) -> list:
    skills = char_data.get("skills") or []
    row = []
    for skill_idx in range(3):
        skill = skills[skill_idx] if skill_idx < len(skills) else {}
        mastery_conds = skill.get("levelUpCostCond") or []
        for mastery_idx in range(3):
            if mastery_idx < len(mastery_conds):
                # First entry is always a Skill Summary token shared across
                # characters; only the materials after it vary.
                costs = (mastery_conds[mastery_idx].get("levelUpCost") or [])[1:]
            else:
                costs = []
            row.extend(cost_pair(costs))
    return row


def build_header() -> list:
    header = ["Code_Name", "EN_Name", "Rarity", "E2_Mat_1", "E2_Mat_1_Qty", "E2_Mat_2", "E2_Mat_2_Qty"]
    for skill_idx in range(1, 4):
        for mastery_idx in range(1, 4):
            prefix = f"S{skill_idx}M{mastery_idx}"
            header += [f"{prefix}_Mat_1", f"{prefix}_Mat_1_Qty", f"{prefix}_Mat_2", f"{prefix}_Mat_2_Qty"]
    return header


def load_existing_order(output_path: Path) -> list:
    """Return the Code_Name order from a previous run's output, if any, so
    that re-runs keep existing rows in place and only append new characters
    at the bottom instead of reshuffling the whole sheet."""
    if not output_path.exists():
        return []
    with output_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["Code_Name"] for row in reader if row.get("Code_Name")]


def main():
    output_path = Path("processed/materials.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with Path("excel-cn/character_table.json").open(encoding="utf-8") as f:
        cn_data = json.load(f)

    # Alternate forms (e.g. Amiya's Guard/Medic modes) aren't in character_table.json
    # at all -- they live in char_patch_table.json's patchChars instead, with the
    # same per-character schema, so they merge in directly.
    with Path("excel-cn/char_patch_table.json").open(encoding="utf-8") as f:
        patch_data = json.load(f)
    patch_char_ids = set(patch_data["patchChars"])
    cn_data = {**cn_data, **patch_data["patchChars"]}

    # Operators recruited and promoted only within a specific Integrated
    # Strategies run (e.g. Mechanist, Raidian). They carry leftover/unused
    # cost data that doesn't apply to normal permanent-roster promotion.
    with Path("excel-cn/special_operator_table.json").open(encoding="utf-8") as f:
        special_operator_ids = set(json.load(f)["operatorBasicData"])

    eligible = {}
    for char_id, char_data in cn_data.items():
        if not char_id.startswith("char_"):
            continue
        if char_data.get("rarity") in LOW_RARITIES:
            continue
        if char_data.get("isNotObtainable"):
            # Reserve Operators and other Integrated Strategies-exclusive
            # fixed units -- not recruitable, so they have no promotion
            # or mastery costs at all.
            continue
        if char_id in special_operator_ids:
            continue
        eligible[char_id] = char_data

    # Keep previously-output characters in their existing position, and only
    # append newly-added ones at the end, so the sheet doesn't reshuffle.
    existing_order = load_existing_order(output_path)
    ordered_ids = [char_id for char_id in existing_order if char_id in eligible]
    ordered_ids += [char_id for char_id in eligible if char_id not in existing_order]

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(build_header())

        count = 0
        for char_id in ordered_ids:
            char_data = eligible[char_id]
            try:
                en_name = char_data.get("appellation")
                if char_id in patch_char_ids:
                    # Alternate forms share their base character's appellation
                    # (e.g. both Amiya forms are just "Amiya"), so disambiguate
                    # with the profession, matching the existing sheet's convention.
                    profession_name = PROFESSION_NAMES.get(char_data.get("profession"), char_data.get("profession"))
                    en_name = f"{en_name} ({profession_name})"

                row = [char_id, en_name, rarity_number(char_data.get("rarity"))]
                row += evolve_cost_row(char_data)
                row += skill_mastery_row(char_data)
                writer.writerow(row)
                count += 1
            except Exception as e:
                print(f"Error processing character {char_id}: {e}")

    print(f"Wrote {count} characters to {output_path}")


if __name__ == "__main__":
    main()
