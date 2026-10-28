#!/usr/bin/env python3
"""Download the current gacha banner image bundles for a Yostar client (EN or
JP) directly from its asset CDN and extract them with ArknightsStudioCLI.

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

    print(f"Wrote images to {output_dir}")


if __name__ == "__main__":
    main()
