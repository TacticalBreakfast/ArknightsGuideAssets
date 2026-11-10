#!/usr/bin/env python3
"""Collect CN shop bundle packs ("组合包") and their contents into
processed/shop_packs.json.

Why this exists: real-money gift package contents are never present in the
excel-*/gamedata tables (confirmed by exhaustive search -- shopGPDataDict only
ever carries goodId/displayName/condTrigPackageType, never price or items).
Since the CN client runs months ahead of EN, CN community sources give us
pack contents well before they ever appear in EN game data.

Three sources, combined (see "Combining the sources" below):
  1. prts.wiki's "采购中心/组合包" page -- the original source. Precise dates,
     but it stopped being updated in Oct 2026 (the last event it has is 昨日海).
  2. yituliu's pack API (ark.yituliu.cn/material/pack) -- kept up to date, and
     plain JSON. Its contents win where both list a pack.
  3. CN game data (shop_client_table) -- every pack the CN shop sells, with
     its sale window and usually its price, but never its contents. A pack
     neither source lists gets an earlier same-name pack's contents.
Each source can fail on its own; the run only stops if both 1 and 2 do, so a
good file is never replaced by less. Each pack records contentsSource
(wiki | yituliu | inferred); the run log lists everything worth a check.

*** FRAGILE SCRAPING WARNING ***
The wiki scrape depends on:
  1. prts.wiki's wikitext staying in its current {{充值组合包/限时|...}} template
     shape. If wiki editors change the template's parameter names (名称/价格/
     期间/内容) or restructure the page's section headers, parsing breaks.
  2. The r.jina.ai reader proxy. prts.wiki blocks non-China IPs outright --
     confirmed even robots.txt 403s from a non-China network -- so this can't
     be fetched directly from a GitHub Actions runner. r.jina.ai is a free,
     third-party, unaffiliated service being used as a workaround; it could
     rate-limit, serve a stale cache, change behavior, or disappear entirely
     without warning (it returned HTTP 422 on 2026-11-10). There is no
     first-party alternative currently known.
yituliu's API is unofficial too (see "Source 2"). If a source shows as
"SOURCE FAILED" in the run log, check those before assuming a bug here.
"""

import calendar
import difflib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# How far back to scrape packs, measured from each pack's end date. The wiki's
# "on sale/preparing" section is NOT reliably date-ordered or pruned -- it was
# observed holding entries from a year prior sitting alongside current ones,
# apparently because recurring "template" packs (same name every rotation,
# e.g. 特训意向礼包) keep a fixed row position that just gets its dates
# updated, rather than being re-sorted. This constant bounds how much of that
# lingering noise gets scraped.
#
# It does NOT bound the output: packs already in shop_packs.json are kept
# when they age out of this window (see merge_with_previous). EN runs ~5-6
# months behind CN, so a pack would otherwise vanish right around the time EN
# sells it -- and the site keeps ended packs in its data on purpose.
LOOKBACK_MONTHS = 6

WIKI_PAGE = "采购中心/组合包"
# Fetched via r.jina.ai (see module docstring) rather than directly, and via
# action=raw so we get wikitext instead of rendered HTML -- much smaller and
# far more reliably parseable than trying to scrape rendered markup. The "&"
# before action=raw must itself be percent-encoded (%26) since it's part of
# the target URL being passed to the r.jina.ai proxy, not a query separator
# for the proxy request itself -- an unencoded "&" would just end up
# terminating the URL early instead of reaching prts.wiki. safe="/" keeps the
# page's own "/" un-encoded (MediaWiki's `title=` expects a literal "/" as
# the namespace/subpage separator, not "%2F") -- encoding it was tried first
# and made r.jina.ai reject the request outright with a 422.
_WIKI_URL = f"https://prts.wiki/index.php?title={urllib.parse.quote(WIKI_PAGE, safe='/')}%26action=raw"
FETCH_URL = f"https://r.jina.ai/{_WIKI_URL}"

# Both sections get scanned, not just the "on sale/preparing" one. A pack
# that's already ended on the CN server (and so has been moved into the
# "historical record" section from CN's point of view) can still be entirely
# in the future for EN, since EN trails CN by months -- excluding it just
# because CN considers it history would defeat the whole point of sourcing
# from CN early. LOOKBACK_MONTHS is what actually bounds how much of the
# historical section is worth pulling in, not the section split itself.
SALE_SECTION_START = "==限时充值组合包（销售中/准备中）=="
HISTORY_SECTION_START = "==限时充值组合包（历史记录）=="
# The historical-record section is the last top-level section on the page as
# of writing, so it has no explicit end marker -- it's read to end-of-text.
# If the wiki ever adds a section after it, blocks from that new section
# would incorrectly get swept in too; find_pack_blocks would likely just find
# zero {{充值组合包/限时|...}} matches in unrelated content and be harmless,
# but worth knowing about if pack counts ever look inflated for no reason.

OUTPUT_PATH = Path("processed/shop_packs.json")


def fetch_wikitext() -> str:
    req = urllib.request.Request(FETCH_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")
    if len(text) < 10000:
        # A healthy fetch of this page is on the order of 250KB. Anything
        # drastically smaller almost certainly means the proxy returned an
        # error page, an empty response, or prts.wiki itself is down --
        # better to fail loudly here than parse garbage and emit a near-empty
        # (or wrong) shop_packs.json over a good previous one.
        raise RuntimeError(
            f"Fetched wikitext suspiciously short ({len(text)} chars) -- "
            "aborting rather than risk parsing a bad/error response."
        )
    return text


def extract_relevant_sections(text: str) -> list:
    sale_start = text.find(SALE_SECTION_START)
    history_start = text.find(HISTORY_SECTION_START)
    if sale_start == -1 or history_start == -1 or history_start <= sale_start:
        raise RuntimeError(
            "Could not locate the on-sale/preparing and historical-record "
            "section headers in the fetched wikitext -- the wiki page "
            "structure has likely changed and this parser needs updating "
            "(see module docstring)."
        )
    return [
        text[sale_start:history_start],  # on-sale/preparing
        text[history_start:],            # historical record, to end of page
    ]


def extract_braced_block(text: str, start: int) -> str:
    """Return the substring starting at `start` (which must point at a '{{')
    through its matching closing '}}', tracking nested template depth.

    Needed because pack blocks contain nested templates (e.g. {{道具图标|...}}
    inside 内容=, {{Hide|...}} inside 说明=) -- a naive regex can't find the
    correct end of a block, only balanced-brace matching can.
    """
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise RuntimeError(
        "Unbalanced braces while extracting a pack block -- wikitext is "
        f"malformed or truncated near: {text[start:start + 80]!r}"
    )


def find_pack_blocks(section_text: str) -> list:
    marker = "{{充值组合包/限时"
    blocks = []
    pos = 0
    while True:
        idx = section_text.find(marker, pos)
        if idx == -1:
            break
        block = extract_braced_block(section_text, idx)
        blocks.append(block)
        pos = idx + len(block)
    return blocks


def get_field(block: str, field_name: str):
    # Field values are always single-line in every pack observed on this
    # page, so `.` (which doesn't match newlines) is deliberately used to
    # stop at end-of-line rather than spilling into subsequent fields.
    m = re.search(rf"\|{re.escape(field_name)}=(.*)", block)
    return m.group(1).strip() if m else None


CN_DATETIME_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2})")


def parse_period(block: str):
    """|期间= holds both dates separated by a {{mdi|fast-forward}} template
    and full-width spaces, e.g.:
      2026/09/04 12:00　{{mdi|fast-forward}}　2026/09/25 03:59
    Extract both plain "YYYY/MM/DD HH:MM" occurrences directly rather than
    trying to parse the separator, which is more robust to the separator's
    exact markup changing."""
    period = get_field(block, "期间")
    if not period:
        return None, None
    matches = CN_DATETIME_RE.findall(period)
    if len(matches) != 2:
        return None, None
    def to_dt(parts):
        year, month, day, hour, minute = (int(x) for x in parts)
        return datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=8)))
    return to_dt(matches[0]), to_dt(matches[1])


def parse_price(block: str):
    price_str = get_field(block, "价格")
    if price_str is None or not price_str.isdigit():
        return None
    # A handful of packs are priced in a premium in-game currency (seen:
    # |价格单位=源石, i.e. Originium Prime) rather than real money -- those
    # aren't "real money packs" at all, so treat them as having no CNY price
    # and let the caller drop them rather than silently mislabeling a
    # premium-currency exchange as a ¥ price.
    if get_field(block, "价格单位"):
        return None
    return int(price_str)


# --- Source 1: prts.wiki -- pack contents parsing ---------------------------
#
# Every reward in a pack's |内容= line is wrapped in a template/link that
# tells us what kind of reward it is -- this is the wiki's OWN categorization,
# not a heuristic we invented:
#   {{道具图标|Name|Count|Size}}          -- a real inventory item
#   {{名片头像|ID|Size|link=Gallery#Label}} -- a namecard/avatar frame
#   {{家具|Name|px=N}}                    -- a furniture piece
#   {{皮肤头像|CharName|Size|Variant}}     -- a character skin portrait
#   [[文件:....png|Size|link=Gallery#Label]] -- a raw illustrative image
#     (scene backgrounds, UI themes, namecards shown as plain images)
# Only 道具图标 represents something that can actually be farmed/valued;
# everything else is decorative/cosmetic and goes to otherItems instead of
# items. This was verified by hand against real pack contents, not assumed.

ITEM_ICON_RE = re.compile(r"\{\{道具图标\|([^|}]*)\|([^|}]*)\|[^}]*\}\}")
NAMECARD_AVATAR_RE = re.compile(r"\{\{名片头像\|[^|}]*\|[^|}]*\|link=([^}]*)\}\}")
FURNITURE_RE = re.compile(r"\{\{家具\|([^|}]*)\|px=[^}]*\}\}")
SKIN_PORTRAIT_RE = re.compile(r"\{\{皮肤头像\|([^|}]*)\|")
FILE_LINK_RE = re.compile(r"\[\[文件:[^|]*\|[^\]]*link=([^\]]*)\]\]")

# CN numeral shorthand used for large quantities in wikitext, e.g. "10万" =
# 100000, "1万" = 10000. Order matters: check longer suffixes first if more
# are ever added.
CN_NUMBER_SUFFIXES = {"万": 10000}

# "Operator selector" vouchers (pick one operator from an event-specific
# pool) -- these are always functionally the same item, but the wiki
# annotates each occurrence's name with a parenthetical suffix naming that
# occurrence (e.g. "（2026周年庆典）"), which the underlying game item's own
# name never includes. That mismatch is exactly why these never resolved to
# an itemId via the normal item_lookup: item_table.json only ever has the
# bare root name, and mints a brand-new itemId for it every single occurrence
# (confirmed: voucher_item_pick1803/2701/3801/5001/6101/7301 all share the
# name "周年庆典干员凭证", one new id per year, itemType VOUCHER_PICK every
# time) -- so matching on itemId could never be stable across occurrences,
# only matching on the root name (with the suffix stripped) can be.
TRAILING_PARENTHETICAL_RE = re.compile(r"[（(][^）)]*[）)]\s*$")

# Headhunting permits that have no AP value of their own and would otherwise
# be filed as "Zero-Efficiency Item" -- which would get packs holding only
# these (e.g. a banner's ¥6/¥58 headhunting packs) dropped as uninteresting.
# Banner-specific permits get a new item id and name every time (SINGLE_75_0_1_GACHA
# 确定性混沌寻访凭证, SINGLE_78_0_1_GACHA 海渊巡游寻访凭证, ...), but their
# itemType is stable, so classify by that. Recorded as category "Headhunting"
# with the itemType kept, so the site can value each kind (the plain
# 7003/7004 permits already have AP values and go to `items` as before):
#   EXCLUSIVE_TKT_GACHA(_10)  one / ten pulls on one specific banner
#   LIMITED_TKT_GACHA_10      ten pulls on a limited banner
#   LINKAGE_TKT_GACHA(_10)    collab banner pulls
#   CLASSIC_TKT_GACHA(_10)    Kernel pulls (kept for completeness; the site values them at 0)
#   VOUCHER_CGACHA            α/β/γ vouchers: one guaranteed operator from a set of three
HEADHUNTING_ITEM_TYPES = {
    "EXCLUSIVE_TKT_GACHA", "EXCLUSIVE_TKT_GACHA_10",
    "LIMITED_TKT_GACHA_10",
    "LINKAGE_TKT_GACHA", "LINKAGE_TKT_GACHA_10",
    "CLASSIC_TKT_GACHA", "CLASSIC_TKT_GACHA_10",
    "VOUCHER_CGACHA",
}

SPECIAL_SELECTOR_TRANSLATIONS = {
    "中坚高级干员调用凭证": "Kernel Selector",
    "周年庆典干员凭证": "Standard Selector",
}


def parse_count(count_str: str) -> int:
    count_str = count_str.strip()
    if not count_str:
        return 1  # e.g. {{道具图标|Name||45px}} -- empty count means qty 1
    for suffix, multiplier in CN_NUMBER_SUFFIXES.items():
        if count_str.endswith(suffix):
            return int(float(count_str[: -len(suffix)]) * multiplier)
    return int(count_str)


def anchor_label_and_category(link_value: str):
    """A `link=Gallery一览#Label` value tells us both a human-readable name
    (the part after '#') and, from which gallery it points into, what kind of
    cosmetic it is. This one function backs both 名片头像 and raw file-link
    parsing since they use the exact same link=Gallery#Label convention."""
    label = link_value.split("#", 1)[-1] if "#" in link_value else link_value
    if "头像" in link_value:
        category = "Avatar"
    elif "场景" in link_value:
        category = "Scene"
    elif "界面主题" in link_value:
        category = "UI Theme"
    elif "名片" in link_value:
        category = "Namecard"
    else:
        category = "Unknown"
    return label, category


def classify_reward(name: str, count: int, item_lookup: dict, item_efficiency: dict,
                    en_names: dict, item_id: str | None = None):
    """One reward -> ("items", entry) for a real item with an AP value, else
    ("other", entry) with a category. Shared by both content sources (the
    wiki gives a name, yituliu an id and a name), so they classify alike."""
    selector_root = TRAILING_PARENTHETICAL_RE.sub("", name)
    if selector_root in SPECIAL_SELECTOR_TRANSLATIONS:
        # Never resolves via item_lookup (see SPECIAL_SELECTOR_TRANSLATIONS
        # docstring above) and never has real farming value either way, so
        # this is checked before the normal id/apValue resolution rather
        # than as a fallback after it fails.
        return "other", {
            "name": name,
            "id": None,
            "enName": SPECIAL_SELECTOR_TRANSLATIONS[selector_root],
            "category": "Special",
            "count": count,
        }

    item_id = item_id or item_lookup.get(name)
    ap_value = item_efficiency.get(item_id) if item_id else None
    if item_id and ap_value and ap_value > 0:
        return "items", {
            "id": item_id,
            "name": name,
            "enName": en_names.get(item_id),
            "count": count,
        }
    else:
        # Present but zero-value (e.g. module upgrade components), or a
        # real itemId that's a cosmetic-collection voucher rather than a
        # farmable material -- see the classifyType/itemType check.
        if not item_id:
            # The wiki sometimes year-tags an item name the game doesn't:
            # "α类迎新寻访凭证（2026）" is item "α类迎新寻访凭证".
            item_id = item_lookup.get(TRAILING_PARENTHETICAL_RE.sub("", name).strip())
        item_type = item_lookup.get((item_id, "type")) if item_id else None
        if item_type in HEADHUNTING_ITEM_TYPES:
            category = "Headhunting"
        elif item_type == "UNI_COLLECTION":
            category = "Furniture"
        else:
            category = "Zero-Efficiency Item"
        entry = {
            "name": name,
            "id": item_id,
            "enName": en_names.get(item_id),
            "category": category,
            "count": count,
        }
        if category == "Headhunting":
            entry["itemType"] = item_type   # how the site values it; see HEADHUNTING_ITEM_TYPES
        return "other", entry


def parse_contents(block: str, item_lookup: dict, item_efficiency: dict, en_names: dict):
    """Returns (items, other_items) for one pack, classifying each reward
    per the module docstring's template-to-category mapping above."""
    content = get_field(block, "内容")
    if not content:
        return [], []

    items = []
    other_items = []

    for m in ITEM_ICON_RE.finditer(content):
        name, count = m.group(1), parse_count(m.group(2))
        where, entry = classify_reward(name, count, item_lookup, item_efficiency, en_names)
        (items if where == "items" else other_items).append(entry)

    # The remaining template types (namecard/avatar, furniture, skin, and raw
    # file-link cosmetics) never resolve to a real itemId at all -- there's
    # nothing to look up, so enName is always None for these, not just
    # unresolved. Only the 道具图标 branch above can ever have a real EN name.
    for m in NAMECARD_AVATAR_RE.finditer(content):
        label, category = anchor_label_and_category(m.group(1))
        other_items.append({"name": label, "id": None, "enName": None, "category": category})

    for m in FURNITURE_RE.finditer(content):
        other_items.append({"name": m.group(1), "id": None, "enName": None, "category": "Furniture"})

    for m in SKIN_PORTRAIT_RE.finditer(content):
        other_items.append({"name": m.group(1), "id": None, "enName": None, "category": "Skin"})

    for m in FILE_LINK_RE.finditer(content):
        label, category = anchor_label_and_category(m.group(1))
        other_items.append({"name": label, "id": None, "enName": None, "category": category})

    return items, other_items


# --- Cross-referencing against existing repo data ---------------------------

def load_item_lookup():
    """CN item name -> itemId, plus (itemId, 'type') -> itemType, both pulled
    from excel-cn/item_table.json's real item catalog."""
    with Path("excel-cn/item_table.json").open(encoding="utf-8") as f:
        cn_items = json.load(f)["items"]
    lookup = {}
    for item_id, data in cn_items.items():
        lookup[data["name"]] = item_id
        lookup[(item_id, "type")] = data.get("itemType")
    return lookup


def load_en_item_names():
    with Path("excel-en/item_table.json").open(encoding="utf-8") as f:
        return {item_id: data["name"] for item_id, data in json.load(f)["items"].items()}


def load_item_efficiency():
    with Path("processed/item_efficiency.json").open(encoding="utf-8") as f:
        return {entry["id"]: entry["apValue"] for entry in json.load(f)}


def load_pack_name_translations():
    """CN pack displayName -> EN pack displayName, built from every goodId
    that has already shipped in BOTH excel-cn and excel-en's
    shop_client_table.json (shopGPDataDict). This only ever resolves for
    *recurring* packs that have appeared in EN before under the same CN name
    (verified: 579 shared goodIds -> 265 distinct name pairs, zero
    conflicts as of last check) -- brand-new seasonal/themed packs will
    correctly come back unresolved (None) until Yostar actually localizes
    them, which is expected, not a bug."""
    with Path("excel-cn/shop_client_table.json").open(encoding="utf-8") as f:
        cn_goods = json.load(f)["shopGPDataDict"]
    with Path("excel-en/shop_client_table.json").open(encoding="utf-8") as f:
        en_goods = json.load(f)["shopGPDataDict"]

    mapping = {}
    for good_id, cn_data in cn_goods.items():
        en_data = en_goods.get(good_id)
        if en_data:
            mapping[cn_data["displayName"]] = en_data["displayName"]
    return mapping


def load_en_pack_names():
    """goodId -> EN displayName, from excel-en/shop_client_table.json."""
    with Path("excel-en/shop_client_table.json").open(encoding="utf-8") as f:
        return {good_id: data["displayName"]
                for good_id, data in json.load(f)["shopGPDataDict"].items()
                if data.get("displayName")}


# --- Pack goodIds ------------------------------------------------------------
#
# Every pack has a goodId (GP_Once_423) in shop_client_table.json's
# shopGPDataDict, and the SAME goodId is used for that pack on CN and EN
# (verified: GP_Once_405-442 all map 1:1, e.g. 423 = 鸭爵的零钱包 = Duck Lord's
# Change Purse). Recording it here is what lets the site look up a pack's EN
# name, EN sale window and EN price (recommendList: showStartTs/showEndTs,
# price in cents) from EN game data once EN ships it -- none of which can be
# frozen in this file, since it's only rescraped by hand.
#
# Name alone isn't enough: recurring packs (特训意向礼包, the chip packs) get a
# new goodId every rotation under the same displayName. So among the goodIds
# carrying the pack's name, pick the one the CN shop schedule
# (recommendList/carousels) puts on sale on the pack's CN start date -- the
# same calendar-date rule load_activities uses. Picking simply the newest id
# was tried first and is wrong (it gave Duck Lord's Change Purse 506, not 423).

# Wiki-only decorations on pack names that the game's displayName lacks:
# "调用凭证组合包<br>(2026周年庆典)" -> "调用凭证组合包", and trailing
# "(2026)" / "（复刻）" (rerun) annotations.
PACK_NAME_LINEBREAK_RE = re.compile(r"<br\s*/?>.*$", re.IGNORECASE)


def pack_name_variants(name: str) -> list:
    """The wiki name, then progressively stripped of wiki-only decorations."""
    variants = [name.strip()]
    stripped = PACK_NAME_LINEBREAK_RE.sub("", name).strip()
    variants.append(stripped)
    variants.append(TRAILING_PARENTHETICAL_RE.sub("", stripped).strip())
    return list(dict.fromkeys(v for v in variants if v))


def load_pack_good_ids():
    """(displayName -> [goodId], goodId -> {CN start dates on sale},
    goodId -> {(CN start date, CNY price)}) from excel-cn/shop_client_table.json.
    Prices come only from recommendList entries (CN's are whole yuan); the
    carousels carry none."""
    with Path("excel-cn/shop_client_table.json").open(encoding="utf-8") as f:
        table = json.load(f)

    by_name = {}
    for good_id, data in table["shopGPDataDict"].items():
        by_name.setdefault((data.get("displayName") or "").strip(), []).append(good_id)

    cn_tz = timezone(timedelta(hours=8))
    sale_dates = {}
    sale_prices = {}

    def add(good_id, ts, price=None):
        # Some entries carry sentinel times (negative / far-future) rather
        # than a real date; those can't match a pack's start date anyway.
        if good_id and isinstance(ts, (int, float)) and 1e9 < ts < 4e9:
            day = datetime.fromtimestamp(ts, tz=cn_tz).date()
            sale_dates.setdefault(good_id, set()).add(day)
            if price is not None:
                sale_prices.setdefault(good_id, set()).add((day, price))

    for entry in table.get("recommendList") or []:
        gift = (entry.get("templateParam") or {}).get("normalGiftParam") or {}
        for group in entry.get("groupList") or []:
            for slot in group.get("dataList") or []:
                if slot.get("cmd") == "GIFTPACKAGE":
                    good_id = slot.get("param1")
                    price = gift.get("price") if gift.get("goodId") == good_id else None
                    add(good_id, entry.get("startDatetime"), price)
    for carousel in table.get("carousels") or []:
        for slot in carousel.get("items") or []:
            if slot.get("cmd") == "GIFTPACKAGE":
                add(slot.get("param1"), slot.get("startTime"))
    return by_name, sale_dates, sale_prices


def resolve_good_id(name: str, start_date: datetime, by_name: dict, sale_dates: dict):
    """The pack's goodId, or None if it can't be pinned to exactly one."""
    day = start_date.date()
    for variant in pack_name_variants(name):
        ids = by_name.get(variant)
        if not ids:
            continue
        on_day = [g for g in ids if day in sale_dates.get(g, ())]
        if len(on_day) == 1:
            return on_day[0]
        if len(ids) == 1 and not on_day:
            return ids[0]   # never scheduled in the table, but the name is unique
        return None         # several candidates on the day, or none of several
    # The wiki sometimes appends 礼包/组合包 to a name the game doesn't
    # (wiki 水芸仿绘礼包 = game 水芸仿绘): fall back to a pack on sale that day
    # whose game name is contained in the wiki name.
    stripped = pack_name_variants(name)[-1]
    on_day = [g for display, ids in by_name.items() if display and display in stripped
              for g in ids if day in sale_dates.get(g, ())]
    return on_day[0] if len(on_day) == 1 else None


def resolve_good_id_by_price(pack: dict, sale_prices: dict, claimed: set):
    """Last resort when the name matches nothing usable: the one pack the CN
    shop puts on sale on the same day at the same CNY price, among goodIds
    no other pack has claimed.

    Added for the 2026-10-09 ¥45 funding box, which the wiki lists under the
    old name 沉甸甸的资金箱 while the game sells 港都之礼资金箱 (GP_Once_498)
    that day. Whether that's a wiki copy-paste or a renamed pack is unknown.
    THIS IS A WEAK MATCH -- price tiers are shared by many packs, so it only
    works when a single unclaimed pack has that price that day. Packs matched
    this way carry "goodIdMatchedBy": "price" so they can be checked; suspect
    it first if a pack's EN name/price/window ever looks wrong."""
    day = datetime.fromisoformat(pack["startDate"]).date()
    candidates = [g for g, entries in sale_prices.items()
                  if g not in claimed and (day, pack["priceCNY"]) in entries]
    return candidates[0] if len(candidates) == 1 else None


def add_good_ids(packs: list, by_name: dict, sale_dates: dict, sale_prices: dict,
                 en_pack_names: dict, name_translations: dict):
    """Fill goodId (and enName, from it) on every pack that lacks them,
    including packs kept from an earlier run. Name matching runs for every
    pack first, so the price fallback only picks among goodIds nobody else
    matched. Returns (names left without a goodId, names matched by price)."""
    for pack in packs:
        if not pack.get("goodId"):
            start = datetime.fromisoformat(pack["startDate"])
            pack["goodId"] = resolve_good_id(pack["name"], start, by_name, sale_dates)
    claimed = {p["goodId"] for p in packs if p.get("goodId")}
    unresolved, by_price = [], []
    for pack in packs:
        if not pack["goodId"] and pack.get("priceCNY") is not None:
            pack["goodId"] = resolve_good_id_by_price(pack, sale_prices, claimed)
            if pack["goodId"]:
                pack["goodIdMatchedBy"] = "price"
                claimed.add(pack["goodId"])
        if pack.get("goodIdMatchedBy") == "price":
            by_price.append(pack["name"])
        if not pack["goodId"]:
            unresolved.append(pack["name"])
        if not pack.get("enName"):
            pack["enName"] = (en_pack_names.get(pack["goodId"]) if pack["goodId"] else None) \
                or name_translations.get(pack["name"])
    return unresolved, by_price


def pack_key(pack: dict):
    return (pack["name"], pack["startDate"])


def merge_with_previous(scraped: list, previous: list):
    """Scraped packs replace their earlier copies (fresh data wins, so parser
    fixes apply to everything still listed within LOOKBACK_MONTHS); earlier
    packs no longer scraped are kept unchanged. An earlier copy is the same
    name and start, or -- since sources can name and date one pack slightly
    differently -- the same goodId starting within MATCH_DAYS. The fresh copy
    keeps the earlier one's name and start, so the pack stays the same pack
    (and keeps its translation). Returns (merged, new_count, kept_count)."""
    remaining = list(previous)
    new_count = 0
    for pack in scraped:
        match = next((p for p in remaining if pack_key(p) == pack_key(pack)), None) or \
            next((p for p in remaining if pack.get("goodId") and p.get("goodId") == pack["goodId"]
                  and abs((datetime.fromisoformat(p["startDate"]) -
                           datetime.fromisoformat(pack["startDate"])).days) <= MATCH_DAYS), None)
        if match is None:
            new_count += 1
            continue
        remaining.remove(match)
        pack["name"], pack["startDate"] = match["name"], match["startDate"]
        if match.get("mtlName") and not pack.get("mtlName"):
            pack["mtlName"] = match["mtlName"]
    return scraped + remaining, new_count, len(remaining)


def load_previous_output() -> list:
    if not OUTPUT_PATH.exists():
        return []
    with OUTPUT_PATH.open(encoding="utf-8") as f:
        return json.load(f)

# --- Source 2: yituliu --------------------------------------------------------
#
# ark.yituliu.cn/material/pack (the site whose material values we already use)
# keeps its own CN pack list, served as plain JSON by its backend -- no wiki
# markup, no proxy (it doesn't block non-China IPs), and it was kept up to date
# when prts.wiki stopped (Oct 2026). It's an unofficial API for their own page,
# so it can change without notice (note the v5); a failure here is reported,
# not fatal, as long as the wiki still works (and vice versa).
#
# How it differs from the wiki, all handled below:
#   - Originite Prime / Orundum / permits are fields (originium, orundum,
#     gachaTicket, tenGachaTicket), the rest a packContent list with item ids.
#   - Banner-only permits are recorded as plain permits; only the free-text
#     note says "仅…可用" (only usable on ...). See banner_permits().
#   - Some ids are its own inventions: cosmetics (avatar_special, theme_scene,
#     skin_18, "0", ...), selectors (voucher_item_pick6), and a few aliases
#     shared with its material-value export (YITULIU_ID_ALIASES).
#   - Packs bought with Originite Prime have price 0 (skipped, as the wiki's
#     non-CNY packs are); permanent chip packs are dated 2099 (kept only as
#     templates for gap filling, below); old packs have start == end.
#   - It skips some reruns (several runs of the skill/chip packs), and dates
#     are sometimes a few days off -- matching allows for that.
YITULIU_URL = "https://backend.yituliu.cn/item/v5/store/pack"
YITULIU_SALE_TYPES = {"activity", "chip"}
YITULIU_ID_ALIASES = {"rare_material_issue_voucher": "precious_material_voucher_perm"}
YITULIU_SELECTORS = {"voucher_item_pick6": "Standard Selector", "voucher_class_pick6": "Kernel Selector"}
YITULIU_COSMETICS = {"avatar_special": "Avatar", "business_card_theme": "Namecard",
                     "theme_scene": "Scene", "theme_scene+UI_Theme": "Scene", "UI_Theme": "UI Theme",
                     "0": "Unknown"}   # "0": furniture, avatars etc. it doesn't model
YITULIU_FIELDS = (("originium", "4002"), ("orundum", "4003"),
                  ("gachaTicket", "7003"), ("tenGachaTicket", "7004"))
YITULIU_NAME_JUNK_RE = re.compile(r"^[\s\[\]【】]+|[\s\[\]【】]+$")   # "技巧集综合礼包]"
CN_TZ = timezone(timedelta(hours=8))


def fetch_yituliu() -> list:
    req = urllib.request.Request(YITULIU_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    data = payload.get("data")
    if not isinstance(data, list) or len(data) < 100:
        # ~550 packs as of writing; a short or odd reply is an error page.
        raise RuntimeError(f"yituliu returned {type(data).__name__} of {len(data or [])} packs")
    return data


def load_gp_data() -> dict:
    """goodId -> shopGPDataDict entry (displayName etc.), CN."""
    with Path("excel-cn/shop_client_table.json").open(encoding="utf-8") as f:
        return json.load(f)["shopGPDataDict"]


def load_cn_items() -> dict:
    with Path("excel-cn/item_table.json").open(encoding="utf-8") as f:
        return json.load(f)["items"]


def banner_permits(cn_items: dict) -> list:
    """[(banner name, single-pull permit id, ten-pull permit id)] for every
    banner-only permit in the CN item table. CN names them after the banner
    (海渊巡游寻访凭证 / 海渊巡游十连寻访凭证), which is what yituliu's note names."""
    singles, tens = {}, {}
    for item_id, d in cn_items.items():
        name = d.get("name") or ""
        if d.get("itemType") == "EXCLUSIVE_TKT_GACHA" and name.endswith("寻访凭证"):
            singles[name[:-len("寻访凭证")]] = item_id
        elif d.get("itemType") == "EXCLUSIVE_TKT_GACHA_10" and name.endswith("十连寻访凭证"):
            tens[name[:-len("十连寻访凭证")]] = item_id
    return [(b, singles.get(b), tens.get(b)) for b in sorted(set(singles) | set(tens), key=len, reverse=True)]


BANNER_PACK_SUFFIX_RE = re.compile(r"(十连)?寻访(组合)?包$")


def is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def find_banner(name: str, note: str, banners: list):
    """The banner whose own permits a yituliu pack holds, if any. Named in the
    note ("仅可用于【海渊巡游】寻访"), or in the pack name -- which may drop a
    character of the banner's (pack 确定混沌寻访包, permit 确定性混沌寻访凭证;
    that pack's note says nothing about the restriction)."""
    for banner in banners:
        # The note only counts when it says the permits are restricted (仅,
        # "only"): 调用凭证组合包's note names 辟路之人 just to describe the
        # selector's pool, and that pack's permit is a normal one.
        if banner[0] in name or ("仅" in note and banner[0] in note):
            return banner
    root = BANNER_PACK_SUFFIX_RE.sub("", name)
    if len(root) >= 3 and root != name:
        matches = [b for b in banners if is_subsequence(root, b[0])]
        if len(matches) == 1:
            return matches[0]
    return None


def ms_to_dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=CN_TZ) if ms else None


def yituliu_pack(raw: dict, ctx: dict, warnings: list):
    """One yituliu entry -> a pack in this file's shape (without goodId/event),
    or None if it isn't a real-money limited pack."""
    name = YITULIU_NAME_JUNK_RE.sub("", raw.get("officialName") or raw.get("displayName") or "")
    start, end = ms_to_dt(raw.get("start")), ms_to_dt(raw.get("end"))
    if not name or not start or not end or raw.get("saleType") not in YITULIU_SALE_TYPES:
        return None
    if not raw.get("price"):
        return None   # bought with Originite Prime, not money
    if name[-1] in "αβγ" or name.startswith("新人"):
        # α/β/γ: one "choose one of three" pack, which yituliu lists three
        # times (the wiki once, as the site values it). 新人: newcomer-only
        # packs, which the site lists by hand (Starter Packs).
        return None
    note = raw.get("note") or ""
    rewards = []   # (name, count, item_id)
    for field, item_id in YITULIU_FIELDS:
        if raw.get(field):
            rewards.append((ctx["cn_items"][item_id]["name"], raw[field], item_id))
    # Banner-only permits: swap the plain permits for the banner's own.
    if raw.get("gachaTicket") or raw.get("tenGachaTicket"):
        banner = find_banner(name, note, ctx["banners"])
        if banner:
            swap = {"7003": banner[1], "7004": banner[2]}
            rewards = [(ctx["cn_items"][swap[i]]["name"], n, swap[i]) if swap.get(i) else (r, n, i)
                       for r, n, i in rewards]
        elif "仅" in note:
            warnings.append(f"{name}: note says the permits are limited ({note[:40]}) but no banner permit matches")
    items, other = [], []
    for it in raw.get("packContent") or []:
        item_id, item_name, count = str(it.get("itemId")), it.get("itemName") or "", it.get("quantity") or 0
        if not count:
            continue
        item_id = YITULIU_ID_ALIASES.get(item_id, item_id)
        if item_id in YITULIU_SELECTORS:
            other.append({"name": item_name, "id": None, "enName": YITULIU_SELECTORS[item_id],
                          "category": "Special", "count": count})
        elif item_id in YITULIU_COSMETICS or item_id.startswith("skin_"):
            other.append({"name": item_name, "id": None, "enName": None, "count": count,
                          "category": YITULIU_COSMETICS.get(item_id, "Skin")})
        else:
            rewards.append((item_name, count, item_id if item_id in ctx["cn_items"] else None))
    for reward_name, count, item_id in rewards:
        where, entry = classify_reward(reward_name, count, ctx["item_lookup"], ctx["item_efficiency"],
                                       ctx["en_item_names"], item_id=item_id)
        (items if where == "items" else other).append(entry)
    return {
        "name": name, "enName": None, "goodId": None, "priceCNY": int(raw["price"]),
        "startDate": start.isoformat(), "endDate": end.isoformat(), "event": None,
        "items": items, "otherItems": other, "contentsSource": "yituliu",
        "_permanent": end.year >= 2099,
    }


def load_yituliu(ctx: dict):
    """Every usable yituliu pack (no date cut -- old ones serve as templates),
    plus warnings."""
    warnings = []
    packs = [p for p in (yituliu_pack(raw, ctx, warnings) for raw in fetch_yituliu()) if p]
    return packs, warnings


def worth_keeping(pack: dict):
    """None if the pack is worth keeping, else why not (the wiki's own filters)."""
    if not pack["items"] and not pack["otherItems"]:
        return "empty"
    if not pack["items"] and all(o["category"] == "Zero-Efficiency Item" for o in pack["otherItems"]):
        return "uninteresting"
    return None


# --- Combining the sources ----------------------------------------------------
#
# The wiki and yituliu are matched pack-for-pack: same goodId (or, failing
# that, the same name) and CN start dates within MATCH_DAYS. A pack both list
# takes the wiki's name and dates (minute precision; keeps it the same pack as
# in earlier runs of this file) and yituliu's contents -- in a comparison of 82
# packs both had, the only disagreement over anything of value was a wiki
# error. Disagreements are still reported ("contentsConflict") so they can be
# checked. Each pack records contentsSource: yituliu | wiki | inferred.
MATCH_DAYS = 3


def valued_contents(pack: dict) -> dict:
    """What a pack holds that has value -- for spotting disagreements."""
    out = {}
    for i in pack["items"]:
        out[i["id"]] = out.get(i["id"], 0) + i["count"]
    for o in pack["otherItems"]:
        if o["category"] in ("Headhunting", "Special"):
            key = o.get("itemType") or o.get("enName") or o["name"]
            out[key] = out.get(key, 0) + o["count"]
    return out


def same_pack(a: dict, b: dict) -> bool:
    days = abs((datetime.fromisoformat(a["startDate"]) - datetime.fromisoformat(b["startDate"])).days)
    if days > MATCH_DAYS:
        return False
    if a.get("goodId") and b.get("goodId"):
        return a["goodId"] == b["goodId"]
    if pack_name_variants(a["name"])[-1] == pack_name_variants(b["name"])[-1]:
        return True
    # A renamed pack (the wiki's 沉甸甸的资金箱 is the game's 港都之礼资金箱):
    # same day, same price, same valued contents.
    return days == 0 and a.get("priceCNY") == b.get("priceCNY") and valued_contents(a) == valued_contents(b)


SIMILAR_NAME_RATIO = 0.7


def resolve_good_id_by_similar_name(pack: dict, schedule: dict, gp_names: dict):
    """yituliu sometimes misspells a name (技巧综合礼包 for 技巧集综合礼包,
    定向甄选组合包 for 定向甄选寻访组合包), so the exact-name match finds no
    goodId. Fall back to the CN pack on sale within a day at the same price
    whose name is closest, if it's close enough and the only one that is."""
    start = datetime.fromisoformat(pack["startDate"])
    scored = []
    for good_id, windows in schedule.items():
        if any(abs((w[0].date() - start.date()).days) <= 1 and w[2] in (None, pack.get("priceCNY"))
               for w in windows):
            ratio = difflib.SequenceMatcher(None, pack["name"], gp_names.get(good_id, "")).ratio()
            if ratio >= SIMILAR_NAME_RATIO:
                scored.append((ratio, good_id))
    scored.sort(reverse=True)
    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def combine_sources(wiki: list, yituliu: list, by_name: dict, sale_dates: dict,
                    schedule: dict, gp_names: dict):
    """Returns (packs, report lines)."""
    report = []
    for p in wiki + yituliu:
        p["goodId"] = resolve_good_id(p["name"], datetime.fromisoformat(p["startDate"]), by_name, sale_dates)
    for p in yituliu:
        if not p["goodId"]:
            p["goodId"] = resolve_good_id_by_similar_name(p, schedule, gp_names)
            if p["goodId"]:
                report.append(f"{p['name']} ({p['startDate'][:10]}): yituliu's name matched to "
                              f"{gp_names[p['goodId']]} [{p['goodId']}] by similarity")
    packs, used = [], set()
    for w in wiki:
        y = next((y for i, y in enumerate(yituliu) if i not in used and same_pack(w, y)), None)
        if y is None:
            w["contentsSource"] = "wiki"
            packs.append(w)
            continue
        used.add(yituliu.index(y))
        merged = dict(w, items=y["items"], otherItems=y["otherItems"], contentsSource="yituliu",
                      goodId=w["goodId"] or y["goodId"], _paired=True)
        if valued_contents(w) != valued_contents(y):
            merged["contentsConflict"] = True
            a, b = valued_contents(w), valued_contents(y)
            diff = {k: (a.get(k), b.get(k)) for k in set(a) | set(b) if a.get(k) != b.get(k)}
            report.append(f"{w['name']} ({w['startDate'][:10]}): wiki and yituliu disagree, using yituliu "
                          f"-- (wiki, yituliu): {diff}")
        if w.get("priceCNY") != y.get("priceCNY"):
            report.append(f"{w['name']} ({w['startDate'][:10]}): price ¥{w.get('priceCNY')} on the wiki, "
                          f"¥{y.get('priceCNY')} on yituliu -- using the wiki's")
        packs.append(merged)
    packs += [y for i, y in enumerate(yituliu) if i not in used]
    return packs, report


# --- Source 3: CN game data, for packs neither source has ----------------------
#
# The CN shop table lists every pack's goodId, name and sale window (and the
# price, for most), but never its contents. So a pack the CN shop sells that
# neither source lists gets the contents of an earlier pack with the same name
# (and price, when known) -- recurring packs (chip packs, Duck Lord's Change
# Purse, the training packs) come back unchanged. Such packs carry
# contentsSource "inferred" and contentsInferredFrom. Ones with no earlier
# pack to copy are only reported: their contents are unknown.
# Only one-off packs (GP_Once_*) are considered, as only those are limited-time
# sales; monthly cards, permanent and returning-player packs aren't.
GAME_DATA_PREFIX = "GP_Once_"
LIMITED_SALE_MAX_DAYS = 60   # longer "sales" are permanent packs (newcomer packs etc.)


def load_cn_schedule() -> dict:
    """goodId -> [(start, end, CNY price or None)], merged per start day."""
    with Path("excel-cn/shop_client_table.json").open(encoding="utf-8") as f:
        table = json.load(f)
    out = {}

    def add(good_id, start, end, price=None):
        if not (good_id and isinstance(start, (int, float)) and 1e9 < start < 4e9
                and isinstance(end, (int, float)) and 1e9 < end < 4e9):
            return
        s, e = datetime.fromtimestamp(start, CN_TZ), datetime.fromtimestamp(end, CN_TZ)
        windows = out.setdefault(good_id, [])
        for i, (ws, we, wp) in enumerate(windows):
            if ws.date() == s.date():
                windows[i] = (max(ws, s), max(we, e), wp if wp is not None else price)
                return
        windows.append((s, e, price))

    for entry in table.get("recommendList") or []:
        gift = (entry.get("templateParam") or {}).get("normalGiftParam") or {}
        for group in entry.get("groupList") or []:
            for slot in group.get("dataList") or []:
                if slot.get("cmd") == "GIFTPACKAGE":
                    good_id = slot.get("param1")
                    if gift.get("goodId") == good_id:   # the shown time: when it really goes on sale
                        add(good_id, gift.get("showStartTs"), gift.get("showEndTs"), gift.get("price"))
                    else:
                        add(good_id, entry.get("startDatetime"), entry.get("endDatetime"))
    for carousel in table.get("carousels") or []:
        for slot in carousel.get("items") or []:
            if slot.get("cmd") == "GIFTPACKAGE":
                add(slot.get("param1"), slot.get("startTime"), slot.get("endTime"))
    return out


SNAP_DAYS = 14


def snap_to_schedule(packs: list, schedule: dict, activities: dict) -> list:
    """yituliu's dates are sometimes off by days (全能开发者主题包: 11-01 there,
    11-08 in the game). For packs only yituliu lists, take the sale window
    from the CN schedule when it has one within SNAP_DAYS. Returns what moved."""
    moved = []
    for p in packs:
        if p.get("contentsSource") != "yituliu" or p.get("_paired") or not p.get("goodId"):
            continue
        start = datetime.fromisoformat(p["startDate"])
        windows = [w for w in schedule.get(p["goodId"], []) if abs((w[0] - start).days) <= SNAP_DAYS]
        if not windows:
            continue
        w = min(windows, key=lambda w: abs((w[0] - start).total_seconds()))
        if w[0].date() != start.date():
            moved.append(f"{p['name']} {start.date()} -> {w[0].date()}")
        p["startDate"], p["endDate"] = w[0].isoformat(), w[1].isoformat()
        p["event"] = activities.get(w[0].date())
    return moved


def fill_from_game_data(packs: list, schedule: dict, gp_names: dict, cutoff: datetime,
                        templates: list, activities: dict):
    """Returns (new packs, packs left without contents)."""
    def covered(good_id, start):
        return any(p.get("goodId") == good_id and
                   abs((datetime.fromisoformat(p["startDate"]) - start).days) <= MATCH_DAYS for p in packs)

    by_name = {}
    # Later ones win; on the same date, ours (wiki-backed, with the cosmetics
    # yituliu leaves out) win over yituliu's. Never copy a copy.
    for t in sorted(templates, key=lambda t: (t["startDate"][:10], t.get("contentsSource") != "yituliu")):
        if (t["items"] or t["otherItems"]) and t.get("contentsSource") != "inferred":
            by_name.setdefault(pack_name_variants(t["name"])[-1], []).append(t)
    new, unknown = [], []
    for good_id, windows in schedule.items():
        if not good_id.startswith(GAME_DATA_PREFIX):
            continue
        name = gp_names.get(good_id, "")
        for start, end, price in windows:
            if end < cutoff or covered(good_id, start) or (end - start).days > LIMITED_SALE_MAX_DAYS:
                continue
            candidates = by_name.get(pack_name_variants(name)[-1], [])
            if price is not None:
                candidates = [t for t in candidates if t.get("priceCNY") == price]
            if not candidates:
                unknown.append(f"{name} [{good_id}] {start.date()}" + (f" ¥{price}" if price else ""))
                continue
            t = candidates[-1]
            new.append({
                "name": name, "enName": None, "goodId": good_id,
                "priceCNY": price if price is not None else t["priceCNY"],
                "startDate": start.isoformat(), "endDate": end.isoformat(),
                "event": activities.get(start.date()),
                "items": t["items"], "otherItems": t["otherItems"],
                "contentsSource": "inferred",
                "contentsInferredFrom": f"{t['name']} {t['startDate'][:10]}",
            })
    return new, unknown


# --- Machine translation of pack names --------------------------------------
#
# Brand-new seasonal packs have no EN name until EN ships them, months later,
# and "黑流树海特训组合" means nothing to the site's readers. So each pack that
# still lacks an enName gets a machine translation (mtlName) from the Claude
# API, ONCE: the result is stored here and carried over on every later run
# (see carry_over_translations), so a name is never re-translated or changes
# under the reader. The site prefers a real EN name whenever one exists and
# marks MTL names as such.
#
# The key comes from the ANTHROPIC_API_KEY repository secret (Settings ->
# Secrets and variables -> Actions). Failure is never fatal: no key, an API
# error or an unparseable reply just leaves the names untranslated, and the
# next run tries again. Spending is bounded three ways: one request per run,
# at most MTL_MAX_PER_RUN names in it, and MTL_MAX_TOKENS of reply -- on Haiku
# that's a fraction of a cent. The account also has a monthly spend limit.
#
# The prompt carries context so names come out the way EN would write them:
# real CN -> EN pack name pairs already in this file (house style: "组合包"
# is "Pack", "资金箱" "Funding Box", ...), the EN names of operators named in
# the pack names (安洁莉娜 -> Angelina), and each pack's event and contents.
MTL_MODEL = "claude-haiku-5-5"
MTL_MAX_PER_RUN = 50
MTL_MAX_TOKENS = 2000
MTL_EXAMPLES = 60
MTL_API_URL = "https://api.anthropic.com/v1/messages"


def carry_over_translations(packs: list, previous: list):
    """Scraped packs replace their earlier copies wholesale, so put back the
    mtlName each one already had."""
    earlier = {pack_key(p): p["mtlName"] for p in previous if p.get("mtlName")}
    for pack in packs:
        if not pack.get("mtlName") and pack_key(pack) in earlier:
            pack["mtlName"] = earlier[pack_key(pack)]


def load_operator_names():
    """CN operator name -> EN name, for every operator EN has."""
    with Path("excel-cn/character_table.json").open(encoding="utf-8") as f:
        cn = json.load(f)
    with Path("excel-en/character_table.json").open(encoding="utf-8") as f:
        en = json.load(f)
    return {cn[c]["name"]: en[c]["name"] for c in cn
            if c.startswith("char_") and c in en and cn[c].get("name") and en[c].get("name")
            and cn[c]["name"] != en[c]["name"]}


def build_mtl_prompt(todo: list, packs: list, operator_names: dict) -> str:
    examples, seen = [], set()
    for p in reversed(packs):   # newest first: closest to current naming
        # Single-word EN names are placeholders, not style (重装芯片礼包 => "Defender").
        if p.get("enName") and " " in p["enName"] and not p.get("mtlName") and p["name"] not in seen:
            seen.add(p["name"])
            examples.append(f"{p['name']} => {p['enName']}")
        if len(examples) >= MTL_EXAMPLES:
            break
    operators = sorted({f"{cn} => {en}" for cn, en in operator_names.items()
                        if len(cn) > 1 and any(cn in p["name"] for p in todo)})
    lines = []
    for i, p in enumerate(todo):
        contents = [it.get("enName") or it["name"] for it in p.get("items", [])]
        contents += [it.get("enName") or it["name"] for it in p.get("otherItems", [])]
        lines.append(json.dumps({"n": i, "name": p["name"],
                                 "event": (p.get("event") or {}).get("name"),
                                 "contents": contents[:8]}, ensure_ascii=False))
    return (
        "You translate names of paid shop packs in the mobile game Arknights from the "
        "Chinese server into English, in the style the English server would use.\n\n"
        "Official CN => EN pack names, for style (\"组合包\" is usually \"Pack\"):\n"
        + "\n".join(examples) + "\n\n"
        + ("Official EN names of operators mentioned:\n" + "\n".join(operators) + "\n\n" if operators else "")
        + "Translate each pack below. \"event\" (the Chinese event it ships with) and "
        "\"contents\" are context only. Keep it short, title case, no quotes, no notes. "
        "Drop wiki year tags such as \"（2026）\".\n\n"
        + "\n".join(lines) + "\n\n"
        "Reply with only a JSON object mapping each n (as a string) to its English name, "
        "e.g. {\"0\": \"...\", \"1\": \"...\"}."
    )


def translate_pack_names(packs: list) -> str:
    """Fill mtlName on packs with neither an enName nor an mtlName yet.
    Returns a one-line report for the run log; never raises."""
    todo = [p for p in packs if not p.get("enName") and not p.get("mtlName")]
    if not todo:
        return "MTL: nothing to translate"
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return f"MTL: ANTHROPIC_API_KEY not set -- {len(todo)} name(s) left untranslated"
    batch = todo[:MTL_MAX_PER_RUN]
    try:
        prompt = build_mtl_prompt(batch, packs, load_operator_names())
        request = urllib.request.Request(
            MTL_API_URL,
            data=json.dumps({"model": MTL_MODEL, "max_tokens": MTL_MAX_TOKENS,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            reply = json.load(response)
        text = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
        match = re.search(r"\{.*\}", text, re.DOTALL)
        names = json.loads(match.group(0)) if match else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        return f"MTL: API error {e.code} -- {len(todo)} name(s) left untranslated: {body}"
    except Exception as e:   # network, timeout, bad JSON: try again next run
        return f"MTL: failed ({type(e).__name__}: {e}) -- {len(todo)} name(s) left untranslated"
    done = []
    for i, pack in enumerate(batch):
        name = names.get(str(i))
        if isinstance(name, str) and name.strip():
            pack["mtlName"] = name.strip()
            done.append(f"{pack['name']} => {pack['mtlName']}")
    report = f"MTL: translated {len(done)} of {len(todo)} name(s)"
    if len(todo) > len(batch):
        report += f" (capped at {MTL_MAX_PER_RUN} per run; the rest next run)"
    usage = reply.get("usage") or {}
    report += f" [{usage.get('input_tokens')} in / {usage.get('output_tokens')} out tokens]"
    return "\n    ".join([report] + done)


# Activity types that are companion/auxiliary features riding along with a
# real content release (a check-in calendar, a login-reward campaign, a
# prayer-wall gimmick) rather than the release itself. These very often start
# on the exact same CN calendar date as the actual event, so when several
# activities share a pack's start date, one of these should lose to whatever
# non-auxiliary activity is also on that date. Every "_ONLY"-suffixed type
# observed in the data is one of these (checked exhaustively against every
# distinct `type` value in activity_table.json at the time this was written);
# a few more (CHECKIN_VIDEO/CHECKIN_ACCESS/CHECKIN_VS/ENEMY_DUEL) don't follow
# that suffix but are the same kind of companion feature.
AUXILIARY_ACTIVITY_TYPE_SUFFIX = "_ONLY"
AUXILIARY_ACTIVITY_TYPES = {"CHECKIN_VIDEO", "CHECKIN_ACCESS", "CHECKIN_VS", "ENEMY_DUEL"}


def is_auxiliary_activity_type(activity_type: str) -> bool:
    return activity_type.endswith(AUXILIARY_ACTIVITY_TYPE_SUFFIX) or activity_type in AUXILIARY_ACTIVITY_TYPES


def _add_candidate(candidates_by_date: dict, date_key, activity_id: str, name: str, auxiliary: bool):
    candidates_by_date.setdefault(date_key, []).append({
        "id": activity_id, "name": name, "auxiliary": auxiliary,
    })


def _collect_activity_table_candidates(candidates_by_date: dict):
    """The general activity catalog -- side stories, main story chapters,
    check-in campaigns, etc. Does NOT include special standalone game modes
    like Contingency Contract or Reclamation Algorithm; those are tracked in
    their own dedicated tables entirely (see the other two collectors below).
    This was discovered the hard way: two pack dates came back with no event
    at all under calendar-date matching, and both turned out to belong to
    modes this table simply doesn't carry season/rerun dates for."""
    with Path("excel-cn/activity_table.json").open(encoding="utf-8") as f:
        basic_info = json.load(f)["basicInfo"]

    for activity_id, data in basic_info.items():
        name = data.get("name")
        start_ts = data.get("startTime")
        if not name or not start_ts:
            continue
        date_key = datetime.fromtimestamp(start_ts, tz=timezone(timedelta(hours=8))).date()
        _add_candidate(candidates_by_date, date_key, activity_id, name,
                       auxiliary=is_auxiliary_activity_type(data.get("type", "")))


def _collect_crisis_contract_candidates(candidates_by_date: dict):
    """Contingency Contract (危机合约) seasons -- excel-cn/crisis_v2_table.json,
    not activity_table.json. Always treated as non-auxiliary: a Contingency
    Contract season is itself the main content release for that slot, never
    a companion feature riding along with something else."""
    with Path("excel-cn/crisis_v2_table.json").open(encoding="utf-8") as f:
        seasons = json.load(f)["seasonInfoDataMap"]

    for season_id, data in seasons.items():
        name = data.get("name")
        start_ts = data.get("startTs")
        if not name or not start_ts:
            continue
        date_key = datetime.fromtimestamp(start_ts, tz=timezone(timedelta(hours=8))).date()
        _add_candidate(candidates_by_date, date_key, season_id, name, auxiliary=False)


def _collect_reclamation_algorithm_candidates(candidates_by_date: dict):
    """Reclamation Algorithm (生息演算) topics -- excel-cn/sandbox_perm_table.json,
    not activity_table.json. A topic can rerun multiple times; each rerun's
    own window lives in that topic's `homeEntryDisplayData` list rather than
    the topic having a single start date, so every entry in there is a
    separate candidate (keyed by its own start date), not just the topic's
    original `topicStartTime`."""
    with Path("excel-cn/sandbox_perm_table.json").open(encoding="utf-8") as f:
        topics = json.load(f)["basicInfo"]

    for topic_id, topic in topics.items():
        name = topic.get("topicName")
        if not name:
            continue
        for entry in topic.get("homeEntryDisplayData") or []:
            start_ts = entry.get("startTs")
            if not start_ts:
                continue
            date_key = datetime.fromtimestamp(start_ts, tz=timezone(timedelta(hours=8))).date()
            _add_candidate(candidates_by_date, date_key, topic_id, name, auxiliary=False)


def load_activities():
    """Events indexed by their start date, for matching a pack's start date
    back to the content patch/event it shipped with. Pulls from three
    separate, unrelated tables (see each collector's docstring) -- there is
    no single unified "all events" table in the CN game data, which was not
    obvious going in and is the main reason this function exists in three
    parts rather than one.

    Matching is by CALENDAR DATE ONLY (CN local, time-of-day ignored), not by
    exact timestamp. Two things were tried and rejected before landing here:
    exact end-timestamp matching (activity end times are always stored ending
    in ':59' seconds while the wiki only gives minute precision -- fixable,
    but the deeper problem is pack sale *lengths* vary throughout the year in
    ways that don't consistently line up with the tied activity's own end
    date at all, e.g. a month-long anniversary sale window against a 2-week
    story chapter); and exact start-timestamp matching (packs commonly open a
    few hours after the activity itself starts, and that gap isn't constant
    either). The calendar date of the start, though, was consistently
    reliable across every batch checked.

    Multiple candidates routinely share a start date (a story chapter, its
    companion check-in campaign, and a login-reward drive can all begin the
    same day) -- see AUXILIARY_ACTIVITY_TYPES above for how that's resolved.
    If a date has more than one non-auxiliary candidate (not observed as of
    writing, but not provably impossible), the choice between them is
    arbitrary (whichever collector ran first, then whichever came first in
    that table's key order) -- worth knowing if an event ever looks wrong for
    a date with a genuinely packed schedule.

    If a pack's date ever again resolves to no event, check whether it
    belongs to yet another standalone mode (Integrated Strategies? Tower
    Defense?) with its own dedicated table not covered here yet, before
    assuming the pack simply isn't tied to anything."""
    candidates_by_date = {}
    _collect_activity_table_candidates(candidates_by_date)
    _collect_crisis_contract_candidates(candidates_by_date)
    _collect_reclamation_algorithm_candidates(candidates_by_date)

    by_start_date = {}
    for date_key, candidates in candidates_by_date.items():
        non_auxiliary = [c for c in candidates if not c["auxiliary"]]
        chosen = (non_auxiliary or candidates)[0]
        by_start_date[date_key] = {"id": chosen["id"], "name": chosen["name"]}
    return by_start_date


def subtract_months(dt: datetime, months: int) -> datetime:
    month_index = dt.month - 1 - months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def scrape_wiki(ctx: dict, cutoff: datetime):
    """Source 1: the packs on prts.wiki's page. Returns (packs, skip counts)."""
    wikitext = fetch_wikitext()
    sections = extract_relevant_sections(wikitext)
    blocks = [block for section in sections for block in find_pack_blocks(section)]
    if not blocks:
        raise RuntimeError(
            "Found both sections but zero pack blocks inside them -- the "
            "{{充值组合包/限时|...}} template shape has likely changed and "
            "this parser needs updating."
        )
    item_lookup, item_efficiency = ctx["item_lookup"], ctx["item_efficiency"]
    en_item_names, activities_by_start_date = ctx["en_item_names"], ctx["activities"]
    packs = []
    skipped = {"stale": 0, "non_cny": 0, "empty": 0, "uninteresting": 0}
    for block in blocks:
        name = get_field(block, "名称")
        start_date, end_date = parse_period(block)
        if not name or not start_date or not end_date:
            continue  # Not a real limited-time sale entry (e.g. malformed/edge block)

        if end_date < cutoff:
            skipped["stale"] += 1
            continue

        price_cny = parse_price(block)
        if price_cny is None and get_field(block, "价格单位"):
            skipped["non_cny"] += 1
            continue

        items, other_items = parse_contents(block, item_lookup, item_efficiency, en_item_names)
        if not items and not other_items:
            skipped["empty"] += 1
            continue
        # A pack with no valued items AND nothing genuinely browsable either
        # (no furniture/avatar/skin/scene/theme/namecard -- just real items
        # that happen to have no computed AP value, e.g. a rename card) has
        # nothing worth showing at all. Distinct from e.g. a pure-furniture
        # bundle, which has no `items` but real cosmetic content worth
        # surfacing in `otherItems`, so that case is deliberately kept.
        if not items and all(oi["category"] == "Zero-Efficiency Item" for oi in other_items):
            skipped["uninteresting"] += 1
            continue

        event = activities_by_start_date.get(start_date.date())

        packs.append({
            "name": name,
            "enName": None,     # filled by add_good_ids, from the goodId when possible
            "goodId": None,
            "priceCNY": price_cny,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "event": event,
            "items": items,
            "otherItems": other_items,
        })

    return packs, skipped


def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    cn_items = load_cn_items()
    ctx = {
        "item_lookup": load_item_lookup(), "en_item_names": load_en_item_names(),
        "item_efficiency": load_item_efficiency(), "activities": load_activities(),
        "cn_items": cn_items, "banners": banner_permits(cn_items),
    }
    pack_name_translations = load_pack_name_translations()
    en_pack_names = load_en_pack_names()
    good_ids_by_name, sale_dates, sale_prices = load_pack_good_ids()
    cutoff = subtract_months(datetime.now(timezone.utc), LOOKBACK_MONTHS)

    # Each source may fail on its own (a proxy or an API going away); only
    # both failing stops the run, so a good file is never replaced by less.
    errors = []
    try:
        wiki_packs, skipped = scrape_wiki(ctx, cutoff)
    except Exception as e:
        wiki_packs, skipped = [], None
        errors.append(f"prts.wiki: {type(e).__name__}: {e}")
    try:
        yituliu_all, yituliu_warnings = load_yituliu(ctx)
    except Exception as e:
        yituliu_all, yituliu_warnings = [], []
        errors.append(f"yituliu: {type(e).__name__}: {e}")
    if not wiki_packs and not yituliu_all:
        raise RuntimeError("Both pack sources failed:\n  " + "\n  ".join(errors))

    yituliu_packs = []
    for p in yituliu_all:
        if p["_permanent"] or datetime.fromisoformat(p["endDate"]) < cutoff or worth_keeping(p):
            continue
        p = {k: v for k, v in p.items() if k != "_permanent"}
        p["event"] = ctx["activities"].get(datetime.fromisoformat(p["startDate"]).date())
        yituliu_packs.append(p)

    schedule = load_cn_schedule()
    gp_names = {g: (d.get("displayName") or "").strip() for g, d in load_gp_data().items()}
    packs, conflicts = combine_sources(wiki_packs, yituliu_packs, good_ids_by_name, sale_dates,
                                       schedule, gp_names)
    moved = snap_to_schedule(packs, schedule, ctx["activities"])
    for p in packs:
        p.pop("_paired", None)
    previous = load_previous_output()
    inferred, unknown = fill_from_game_data(packs, schedule, gp_names, cutoff,
                                            yituliu_all + previous + packs, ctx["activities"])
    packs += inferred

    scraped_count = len(packs)
    packs, new_count, kept_count = merge_with_previous(packs, previous)
    unresolved, matched_by_price = add_good_ids(packs, good_ids_by_name, sale_dates, sale_prices,
                                                en_pack_names, pack_name_translations)
    packs.sort(key=lambda p: (p["startDate"], p["name"]))
    mtl_report = translate_pack_names(packs)

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(packs, f, indent=2, ensure_ascii=False)

    sources = {}
    for p in packs:
        sources[p.get("contentsSource", "wiki")] = sources.get(p.get("contentsSource", "wiki"), 0) + 1
    print(f"Wrote {len(packs)} packs to {OUTPUT_PATH}")
    print(f"  {scraped_count} found ({new_count} new), {kept_count} kept from the previous file")
    print(f"  sources: {len(wiki_packs)} on prts.wiki, {len(yituliu_packs)} on yituliu, "
          f"{len(inferred)} inferred from game data; contents by source: {sources}")
    for e in errors:
        print(f"  SOURCE FAILED -- {e}")
    if moved:
        print(f"  {len(moved)} yituliu date(s) corrected from the CN schedule: {', '.join(moved)}")
    for line in conflicts + yituliu_warnings:
        print(f"  CHECK: {line}")
    if inferred:
        print(f"  {len(inferred)} pack(s) on the CN shop that no source lists, contents copied from an "
              f"earlier pack: " + ", ".join(f"{p['name']} ({p['startDate'][:10]})" for p in inferred))
    if unknown:
        print(f"  {len(unknown)} pack(s) on the CN shop with no contents anywhere (left out): {', '.join(unknown)}")
    if matched_by_price:
        # Weak matches (same day + same price only) -- see resolve_good_id_by_price.
        print(f"  {len(matched_by_price)} goodId(s) matched by price only, worth checking: "
              f"{', '.join(matched_by_price)}")
    if unresolved:
        # Not fatal: the site can't match these to EN game data automatically
        # (EN name/sale window/price) and falls back to its own overrides.
        print(f"  {len(unresolved)} without a goodId: {', '.join(unresolved)}")
    if skipped:
        print(f"  wiki: {skipped['stale']} skipped (older than {LOOKBACK_MONTHS} months), "
              f"{skipped['non_cny']} (not priced in CNY), {skipped['empty']} (empty), "
              f"{skipped['uninteresting']} (nothing of value or browsable)")
    print(f"  {mtl_report}")


if __name__ == "__main__":
    main()
