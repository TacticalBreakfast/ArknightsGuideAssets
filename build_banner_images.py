#!/usr/bin/env python3
"""Compose a banner image for each EN double_en_* gacha banner from its
extracted sprites, placed according to the banner's layout.json.

Only the background, the two six-star illustrations (char1/char2) and the
name plates (name1-name5) are drawn. The output is cropped to the
background's rect and written to banner-images/<banner>.png.
"""

import json
from pathlib import Path

from PIL import Image

# Built from parts rather than globbed, since "[[en]]" would be read as a
# glob character class.
GACHA_DIR = Path("gacha-en") / "dyn" / "[[en]]" / "ui" / "gacha"
OUTPUT_DIR = Path("banner-images")
BACKGROUND_NODE = "back_image"
FOREGROUND_SPRITES = {"char1", "char2", "name1", "name2", "name3", "name4", "name5"}


def collect_draws(node: dict, parent_rect: tuple, parent_origin: tuple, parent_scale: tuple, draws: list) -> None:
    """Walk the RectTransform tree in draw order, resolving each node's rect
    into root space (y up), and record the active sprite nodes."""
    if not node["active"]:
        return  # inactive parents hide their whole subtree

    px, py, pw, ph = parent_rect
    amin, amax = node["anchorMin"], node["anchorMax"]
    pivot, pos, size_delta = node["pivot"], node["anchoredPosition"], node["sizeDelta"]
    scale = node["scale"]

    size = ((amax[0] - amin[0]) * pw + size_delta[0], (amax[1] - amin[1]) * ph + size_delta[1])
    anchor_min_pt = (px + amin[0] * pw, py + amin[1] * ph)
    anchor_max_pt = (px + amax[0] * pw, py + amax[1] * ph)
    pivot_pt = (
        anchor_min_pt[0] + (anchor_max_pt[0] - anchor_min_pt[0]) * pivot[0] + pos[0],
        anchor_min_pt[1] + (anchor_max_pt[1] - anchor_min_pt[1]) * pivot[1] + pos[1],
    )

    origin = (parent_origin[0] + parent_scale[0] * pivot_pt[0], parent_origin[1] + parent_scale[1] * pivot_pt[1])
    world_scale = (parent_scale[0] * scale[0], parent_scale[1] * scale[1])
    local_rect = (-pivot[0] * size[0], -pivot[1] * size[1], size[0], size[1])

    sprite = node.get("sprite")
    if sprite and (node["name"] == BACKGROUND_NODE or sprite in FOREGROUND_SPRITES):
        draws.append({
            "node": node["name"],
            "sprite": sprite,
            "x": origin[0] + world_scale[0] * local_rect[0],
            "y": origin[1] + world_scale[1] * local_rect[1],
            "w": world_scale[0] * size[0],
            "h": world_scale[1] * size[1],
        })

    for child in node.get("children", []):
        collect_draws(child, local_rect, origin, world_scale, draws)


def build_banner(banner_dir: Path, output_path: Path) -> bool:
    layout = json.loads((banner_dir / "layout.json").read_text(encoding="utf-8"))

    # The root's own anchors refer to the gacha screen it's mounted in, which
    # isn't in the bundle - treat its sizeDelta as its rect around its pivot.
    root_size = layout["sizeDelta"]
    root_rect = (-layout["pivot"][0] * root_size[0], -layout["pivot"][1] * root_size[1], root_size[0], root_size[1])
    draws = []
    for child in layout.get("children", []):
        collect_draws(child, root_rect, (0.0, 0.0), tuple(layout["scale"]), draws)

    background = next((d for d in draws if d["node"] == BACKGROUND_NODE), None)
    if background is None:
        print(f"Skipping {banner_dir.name}: no background sprite in layout")
        return False

    canvas = Image.new("RGBA", (round(background["w"]), round(background["h"])), (0, 0, 0, 0))
    for d in draws:
        sprite_path = banner_dir / f"{d['sprite']}.png"
        if not sprite_path.exists():
            print(f"Skipping {banner_dir.name}: missing {sprite_path.name}")
            return False
        img = Image.open(sprite_path).convert("RGBA")
        size = (round(d["w"]), round(d["h"]))
        if img.size != size:
            img = img.resize(size, Image.LANCZOS)
        # Root space is y-up; the canvas is y-down with its top-left at the
        # background's top-left corner.
        left = round(d["x"] - background["x"])
        top = round((background["y"] + background["h"]) - (d["y"] + d["h"]))
        _composite_clipped(canvas, img, left, top)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)
    return True


def _composite_clipped(canvas: Image.Image, img: Image.Image, left: int, top: int) -> None:
    # alpha_composite rejects negative destinations and sprites that run past
    # the canvas, so crop each sprite to the part that lands on the canvas.
    box = (max(0, -left), max(0, -top), min(img.width, canvas.width - left), min(img.height, canvas.height - top))
    if box[0] >= box[2] or box[1] >= box[3]:
        return
    canvas.alpha_composite(img.crop(box), dest=(left + box[0], top + box[1]))


def main():
    if not GACHA_DIR.is_dir():
        print(f"{GACHA_DIR} not found. Nothing to build.")
        return

    built = 0
    for banner_dir in sorted(GACHA_DIR.iterdir()):
        if not banner_dir.is_dir() or not banner_dir.name.startswith("double_"):
            continue
        if not (banner_dir / "layout.json").exists():
            print(f"Skipping {banner_dir.name}: no layout.json")
            continue
        if build_banner(banner_dir, OUTPUT_DIR / f"{banner_dir.name}.png"):
            built += 1

    print(f"Built {built} banner images in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
