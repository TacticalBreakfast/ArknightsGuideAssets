#!/usr/bin/env python3
"""Scrape CN shop bundle packs ("组合包") from prts.wiki and export their
contents to processed/shop_packs.json.

Why this exists: real-money gift package contents are never present in the
excel-*/gamedata tables (confirmed by exhaustive search -- shopGPDataDict only
ever carries goodId/displayName/condTrigPackageType, never price or items).
The CN wiki's "采购中心/组合包" page is maintained by the community and, since
the CN client runs months ahead of EN, gives us pack contents well before they
ever appear in EN game data.

*** FRAGILE SCRAPING WARNING ***
This entire script depends on:
  1. prts.wiki's wikitext staying in its current {{充值组合包/限时|...}} template
     shape. If wiki editors change the template's parameter names (名称/价格/
     期间/内容) or restructure the page's section headers, parsing breaks.
  2. The r.jina.ai reader proxy. prts.wiki blocks non-China IPs outright --
     confirmed even robots.txt 403s from a non-China network -- so this can't
     be fetched directly from a GitHub Actions runner. r.jina.ai is a free,
     third-party, unaffiliated service being used as a workaround; it could
     rate-limit, serve a stale cache, change behavior, or disappear entirely
     without warning. There is no first-party alternative currently known.
If this script starts failing, check both of those before assuming a bug here.
"""

import calendar
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# How far back to keep packs, measured from each pack's end date. The wiki's
# "on sale/preparing" section is NOT reliably date-ordered or pruned -- it was
# observed holding entries from a year prior sitting alongside current ones,
# apparently because recurring "template" packs (same name every rotation,
# e.g. 特训意向礼包) keep a fixed row position that just gets its dates
# updated, rather than being re-sorted. This constant exists to bound how much
# of that lingering noise makes it into the output; unused for anything else,
# so it's safe to change at will if 6 months ever stops being enough.
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


# --- Pack contents parsing -------------------------------------------------
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


def parse_contents(block: str, item_lookup: dict, item_efficiency: dict, en_names: dict):
    """Returns (items, other_items) for one pack, classifying each reward
    per the module docstring's template-to-category mapping above."""
    content = get_field(block, "内容")
    if not content:
        return [], []

    items = []
    other_items = []

    for m in ITEM_ICON_RE.finditer(content):
        name, count_str = m.group(1), m.group(2)
        count = parse_count(count_str)

        selector_root = TRAILING_PARENTHETICAL_RE.sub("", name)
        if selector_root in SPECIAL_SELECTOR_TRANSLATIONS:
            # Never resolves via item_lookup (see SPECIAL_SELECTOR_TRANSLATIONS
            # docstring above) and never has real farming value either way, so
            # this is checked before the normal id/apValue resolution rather
            # than as a fallback after it fails.
            other_items.append({
                "name": name,
                "id": None,
                "enName": SPECIAL_SELECTOR_TRANSLATIONS[selector_root],
                "category": "Special",
            })
            continue

        item_id = item_lookup.get(name)
        ap_value = item_efficiency.get(item_id) if item_id else None
        if item_id and ap_value and ap_value > 0:
            items.append({
                "id": item_id,
                "name": name,
                "enName": en_names.get(item_id),
                "count": count,
            })
        else:
            # Present but zero-value (e.g. module upgrade components), or a
            # real itemId that's a cosmetic-collection voucher rather than a
            # farmable material -- see the classifyType/itemType check.
            item_type = item_lookup.get((item_id, "type")) if item_id else None
            category = "Furniture" if item_type == "UNI_COLLECTION" else "Zero-Efficiency Item"
            other_items.append({"name": name, "id": item_id, "enName": en_names.get(item_id), "category": category})

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


def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    wikitext = fetch_wikitext()
    sections = extract_relevant_sections(wikitext)
    blocks = [block for section in sections for block in find_pack_blocks(section)]
    if not blocks:
        raise RuntimeError(
            "Found both sections but zero pack blocks inside them -- the "
            "{{充值组合包/限时|...}} template shape has likely changed and "
            "this parser needs updating."
        )

    item_lookup = load_item_lookup()
    en_item_names = load_en_item_names()
    item_efficiency = load_item_efficiency()
    pack_name_translations = load_pack_name_translations()
    activities_by_start_date = load_activities()

    cutoff = subtract_months(datetime.now(timezone.utc), LOOKBACK_MONTHS)

    packs = []
    skipped_stale = 0
    skipped_non_cny = 0
    skipped_empty = 0
    skipped_uninteresting = 0

    for block in blocks:
        name = get_field(block, "名称")
        start_date, end_date = parse_period(block)
        if not name or not start_date or not end_date:
            continue  # Not a real limited-time sale entry (e.g. malformed/edge block)

        if end_date < cutoff:
            skipped_stale += 1
            continue

        price_cny = parse_price(block)
        if price_cny is None and get_field(block, "价格单位"):
            skipped_non_cny += 1
            continue

        items, other_items = parse_contents(block, item_lookup, item_efficiency, en_item_names)
        if not items and not other_items:
            skipped_empty += 1
            continue
        # A pack with no valued items AND nothing genuinely browsable either
        # (no furniture/avatar/skin/scene/theme/namecard -- just real items
        # that happen to have no computed AP value, e.g. a rename card) has
        # nothing worth showing at all. Distinct from e.g. a pure-furniture
        # bundle, which has no `items` but real cosmetic content worth
        # surfacing in `otherItems`, so that case is deliberately kept.
        if not items and all(oi["category"] == "Zero-Efficiency Item" for oi in other_items):
            skipped_uninteresting += 1
            continue

        event = activities_by_start_date.get(start_date.date())

        packs.append({
            "name": name,
            "enName": pack_name_translations.get(name),
            "priceCNY": price_cny,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "event": event,
            "items": items,
            "otherItems": other_items,
        })

    packs.sort(key=lambda p: p["startDate"])

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(packs, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(packs)} packs to {OUTPUT_PATH}")
    print(f"  {skipped_stale} skipped (older than {LOOKBACK_MONTHS} months)")
    print(f"  {skipped_non_cny} skipped (priced in a non-CNY currency, not a real-money pack)")
    print(f"  {skipped_empty} skipped (no items or otherItems resolved at all)")
    print(f"  {skipped_uninteresting} skipped (no valued items and nothing browsable either)")


if __name__ == "__main__":
    main()
