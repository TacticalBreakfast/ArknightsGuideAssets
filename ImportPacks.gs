/**
 * Pulls processed/shop_packs.json from the ArknightsGuideAssets repo and
 * writes one row per (pack, item) into the "Import" tab, replacing whatever
 * was there before.
 *
 * Includes both pack.items (real, valued materials) and pack.otherItems
 * (cosmetic/no-value content like furniture, skins, avatar frames) -- the
 * Category column distinguishes them, since otherItems isn't part of the
 * efficiency calculations but is still useful for users to see at a glance.
 */
const SHOP_PACKS_URL = "https://raw.githubusercontent.com/TacticalBreakfast/ArknightsGuideAssets/main/processed/shop_packs.json";
const IMPORT_SHEET_NAME = "IMPORT - Packs";

const HEADER = ["Pack Name", "Price (CNY)", "Start Date", "End Date", "Event Name", "Event ID", "Item ID", "Item Name", "Item Count", "Category"];

function importShopPacks() {
  const response = UrlFetchApp.fetch(SHOP_PACKS_URL);
  const packs = JSON.parse(response.getContentText());

  const rows = [];
  for (const pack of packs) {
    // enName is null for packs that haven't shipped in EN before -- fall
    // back to the CN name so every row still has exactly one usable name.
    const packName = pack.enName || pack.name;
    const eventName = pack.event ? pack.event.name : "";
    const eventId = pack.event ? pack.event.id : "";

    for (const item of pack.items) {
      rows.push([
        packName,
        pack.priceCNY,
        new Date(pack.startDate),
        new Date(pack.endDate),
        eventName,
        eventId,
        item.id,
        item.enName || item.name,
        item.count,
        "Material",
      ]);
    }

    for (const item of pack.otherItems) {
      rows.push([
        packName,
        pack.priceCNY,
        new Date(pack.startDate),
        new Date(pack.endDate),
        eventName,
        eventId,
        item.id,
        item.enName || item.name,
        item.count ?? "",
        item.category,
      ]);
    }
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(IMPORT_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(IMPORT_SHEET_NAME);
  }
  sheet.clearContents();

  sheet.getRange(1, 1, 1, HEADER.length).setValues([HEADER]);
  if (rows.length > 0) {
    sheet.getRange(2, 1, rows.length, HEADER.length).setValues(rows);
  }
}
