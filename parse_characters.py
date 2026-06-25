#!/usr/bin/env python3
"""Parse character_table.json and export selected attributes to YAML."""

import json
import yaml
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# ATTRIBUTES TO EXTRACT
#
# Each entry is a tuple of (output_key, json_path) where json_path is a
# dot-separated path into the character object. Use list indices with [n]
# or [-1] to index into arrays.
#
# Special paths:
#   phases[-1].attributesKeyFrames[-1].data.<stat>  → max-elite max-level stat
#   phases[0].attributesKeyFrames[0].data.<stat>    → E0 level-1 stat
# ─────────────────────────────────────────────────────────────────────────────
ATTRIBUTES = [
    ("id",               None),           # special: the top-level key itself
    ("name",             "name"),
    ("rarity",           "rarity"),
    ("profession",       "profession"),
    ("sub_profession",   "subProfessionId"),
#    ("position",         "position"),
#    ("tags",             "tagList"),
#    ("description",      "description"),
#    ("nation",           "nationId"),
#    ("is_obtainable",    "isNotObtainable"),  # note: inverted below
#    ("is_sp_char",       "isSpChar"),
#    # Max-elite, max-level base stats
#    ("max_hp",           "phases[-1].attributesKeyFrames[-1].data.maxHp"),
#    ("max_atk",          "phases[-1].attributesKeyFrames[-1].data.atk"),
#    ("max_def",          "phases[-1].attributesKeyFrames[-1].data.def"),
#    ("max_res",          "phases[-1].attributesKeyFrames[-1].data.magicResistance"),
#    ("deploy_cost",      "phases[-1].attributesKeyFrames[-1].data.cost"),
#    ("block_count",      "phases[-1].attributesKeyFrames[-1].data.blockCnt"),
#    ("redeploy_time",    "phases[-1].attributesKeyFrames[-1].data.respawnTime"),
#    ("attack_interval",  "phases[-1].attributesKeyFrames[-1].data.baseAttackTime"),
]


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


def load_sub_prof_names() -> dict:
    """Return a mapping of subProfessionId -> subProfessionName from uniequip_table.json."""
    path = Path("excel-en/uniequip_table.json")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {k: v["subProfessionName"] for k, v in data["subProfDict"].items()}


def get_nested(obj, path: str):
    """Walk a dot-separated path (supporting [n] array indexing) into obj."""
    parts = path.replace("]", "").replace("[", ".").split(".")
    for part in parts:
        if obj is None:
            return None
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def extract_character(char_id: str, char_data: dict, sub_prof_names: dict, use_appellation: bool = False) -> dict:
    result = {}
    for output_key, path in ATTRIBUTES:
        if path is None:
            result[output_key] = char_id
        else:
            result[output_key] = get_nested(char_data, path)

    if use_appellation and "name" in result:
        result["name"] = char_data.get("appellation")

    # Invert isNotObtainable so the flag reads more intuitively
    if "is_obtainable" in result:
        val = result["is_obtainable"]
        result["is_obtainable"] = not val if isinstance(val, bool) else val

    # Enrich rarity with a star string
    if "rarity" in result:
        tier = result["rarity"]
        if tier and tier.startswith("TIER_") and tier[5:].isdigit():
            result["stars"] = "★" * int(tier[5:])

    # Enrich profession with its display name
    if "profession" in result:
        result["profession_name"] = PROFESSION_NAMES.get(result["profession"])

    # Enrich sub_profession with its display name from uniequip_table
    if "sub_profession" in result:
        sub_id = result["sub_profession"]
        result["sub_profession_name"] = sub_prof_names.get(sub_id, "unknown")

    return result


def main():
    output_path = Path("processed/characters.yml")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sub_prof_names = load_sub_prof_names()

    with Path("excel-en/character_table.json").open(encoding="utf-8") as f:
        en_data = json.load(f)

    with Path("excel-cn/character_table.json").open(encoding="utf-8") as f:
        cn_data = json.load(f)

    cn_only = {k: v for k, v in cn_data.items() if k not in en_data}

    characters = [
        extract_character(char_id, char_data, sub_prof_names)
        for char_id, char_data in en_data.items()
        if char_data.get("profession") != "TRAP"
    ] + [
        extract_character(char_id, char_data, sub_prof_names, use_appellation=True)
        for char_id, char_data in cn_only.items()
        if char_data.get("profession") != "TRAP"
    ]

    with output_path.open("w", encoding="utf-8") as f:
        yaml.dump(characters, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    print(f"Wrote {len(characters)} characters to {output_path}")


if __name__ == "__main__":
    main()
