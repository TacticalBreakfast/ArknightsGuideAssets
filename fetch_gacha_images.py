#!/usr/bin/env python3
"""Download the current gacha banner image bundles for a Yostar client (EN or
JP) directly from its asset CDN, extract their sprites with
ArknightsStudioCLI, and write each banner prefab's UI layout to layout.json
alongside its sprites.

Usage: fetch_gacha_images.py --server {en,jp}
"""

import argparse
import io
import json
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

import requests
import UnityPy
from arkprts.assets.bundle import decompress_lz4ak
from UnityPy.enums.BundleFile import CompressionFlags
from UnityPy.helpers import CompressionHelper

# Arknights bundles use a modified LZ4 ("LZ4AK") under the LZHAM flag.
CompressionHelper.DECOMPRESSION_MAP[CompressionFlags.LZHAM] = decompress_lz4ak

NETWORK_CONFIG_URLS = {
    "en": "https://ak-conf.arknights.global/config/prod/official/network_config",
    "jp": "https://ak-conf.arknights.jp/config/prod/official/network_config",
}
PLATFORM = "Android"

# thesadru/AssetStudio publishes the Linux build; aelurum/AssetStudio (same
# project, different release host) publishes Windows. We only ever run on
# GitHub's ubuntu-latest runners, so only the Linux build is needed here.
STUDIO_CLI_URL = "https://github.com/thesadru/AssetStudio/releases/download/ak-v1.2.1/ArknightsStudioCLI-net6-linux64.v1.2.1.zip"


def get_asset_host(server: str) -> str:
    resp = requests.get(NETWORK_CONFIG_URLS[server], timeout=30)
    resp.raise_for_status()
    content = json.loads(resp.json()["content"])
    network = content["configs"][content["funcVer"]]["network"]
    return network["hu"]


def get_res_version(asset_host: str) -> str:
    resp = requests.get(f"{asset_host}/{PLATFORM}/version", timeout=30)
    resp.raise_for_status()
    return resp.json()["resVersion"]


def get_gacha_bundles(asset_host: str, res_version: str) -> list:
    url = f"{asset_host}/{PLATFORM}/assets/{res_version}/hot_update_list.json"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    # Covers both the shared "ui/gacha/..." bundles (e.g. boot art, the
    # limit-time banner object) and the server-exclusive "[[jp]]/ui/gacha/..."
    # or "[[en]]/ui/gacha/..." ones - "ui/gacha/" appears in all of them.
    return [ab for ab in resp.json()["abInfos"] if "ui/gacha/" in ab["name"]]


def flattened_dat_name(bundle_name: str) -> str:
    return bundle_name.replace("/", "_").rsplit(".", 1)[0] + ".dat"


def download_bundles(asset_host: str, res_version: str, bundles: list, dest_dir: Path) -> None:
    base_url = f"{asset_host}/{PLATFORM}/assets/{res_version}"
    for ab in bundles:
        dat_name = flattened_dat_name(ab["name"])
        resp = requests.get(f"{base_url}/{dat_name}", timeout=60)
        resp.raise_for_status()
        # Each .dat is a zip containing the original .ab under its real path.
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            z.extractall(dest_dir)


def ensure_studio_cli(tools_dir: Path) -> Path:
    cli_path = tools_dir / "ArknightsStudioCLI"
    if cli_path.exists():
        return cli_path

    resp = requests.get(STUDIO_CLI_URL, timeout=120)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        z.extractall(tools_dir)

    cli_path.chmod(cli_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return cli_path


def extract_images(cli_path: Path, bundles_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(cli_path),
            str(bundles_dir),
            "-g", "containerFull",
            "-t", "Sprite",
            "--log-level", "warning",
            "-o", str(output_dir),
        ],
        check=True,
    )


def _xy(v: dict) -> list:
    # + 0.0 turns Unity's -0.0 into 0.0 so it doesn't churn diffs.
    return [round(v["x"], 2) + 0.0, round(v["y"], 2) + 0.0]


def _layout_node(transform, objects: dict) -> dict:
    tf = transform.read_typetree()
    go = objects[tf["m_GameObject"]["m_PathID"]].read_typetree()

    node = {"name": go["m_Name"], "active": bool(go["m_IsActive"])}
    for comp in go["m_Component"]:
        comp_obj = objects.get(comp["component"]["m_PathID"])
        if comp_obj is None or comp_obj.type.name != "MonoBehaviour":
            continue
        sprite_ref = comp_obj.read_typetree().get("m_Sprite")
        if sprite_ref and sprite_ref["m_FileID"] == 0 and sprite_ref["m_PathID"] in objects:
            node["sprite"] = objects[sprite_ref["m_PathID"]].read().m_Name

    if transform.type.name == "RectTransform":
        for key, field in [
            ("anchoredPosition", "m_AnchoredPosition"),
            ("sizeDelta", "m_SizeDelta"),
            ("anchorMin", "m_AnchorMin"),
            ("anchorMax", "m_AnchorMax"),
            ("pivot", "m_Pivot"),
        ]:
            node[key] = _xy(tf[field])
    else:
        node["localPosition"] = _xy(tf["m_LocalPosition"])
    node["scale"] = _xy(tf["m_LocalScale"])

    children = [_layout_node(objects[c["m_PathID"]], objects) for c in tf["m_Children"]]
    if children:
        node["children"] = children
    return node


def write_layouts(bundles_dir: Path, output_dir: Path) -> None:
    for bundle_path in bundles_dir.rglob("*.ab"):
        env = UnityPy.load(str(bundle_path))
        objects = {o.path_id: o for o in env.objects}
        for container_path, obj in env.container.items():
            if obj.type.name != "GameObject" or not container_path.endswith(".prefab"):
                continue
            go = obj.read_typetree()
            transform = next(
                objects[c["component"]["m_PathID"]]
                for c in go["m_Component"]
                if objects[c["component"]["m_PathID"]].type.name in ("RectTransform", "Transform")
            )
            layout_path = output_dir / container_path.removesuffix(".prefab") / "layout.json"
            layout_path.parent.mkdir(parents=True, exist_ok=True)
            layout_path.write_text(
                json.dumps(_layout_node(transform, objects), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True, choices=sorted(NETWORK_CONFIG_URLS))
    server = parser.parse_args().server
    output_dir = Path(f"gacha-{server}")

    asset_host = get_asset_host(server)
    res_version = get_res_version(asset_host)
    print(f"{server.upper()} resVersion: {res_version}")

    bundles = get_gacha_bundles(asset_host, res_version)
    print(f"Found {len(bundles)} gacha bundles")
    if not bundles:
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        bundles_dir = tmp_path / "bundles"
        bundles_dir.mkdir()
        download_bundles(asset_host, res_version, bundles, bundles_dir)

        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        cli_path = ensure_studio_cli(tools_dir)

        if output_dir.exists():
            shutil.rmtree(output_dir)
        extract_images(cli_path, bundles_dir, output_dir)
        write_layouts(bundles_dir, output_dir)

    print(f"Wrote images and layouts to {output_dir}")


if __name__ == "__main__":
    main()
