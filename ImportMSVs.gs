/**
 * Pulls processed/item_efficiency.json from the ArknightsGuideAssets repo
 * and writes one row per item into the "Data - MSV" tab, replacing whatever
 * was there before. Values are overridden per-item by whatever's present in
 * the "DATA - Override" sheet (row 1 = note, row 2 = headers, data from row 3:
 * A = item code, B = Name, C = My Value -- D/E/F are informational only,
 * ignored here). An override for an item code that isn't in the JSON at all
 * still gets added as its own row, using the override sheet's own Name
 * column since there's no JSON entry to pull a name from.
 */
const ITEM_EFFICIENCY_URL = "https://raw.githubusercontent.com/TacticalBreakfast/ArknightsGuideAssets/main/processed/item_efficiency.json";
const MSV_SHEET_NAME = "IMPORT - MSVs";
const OVERRIDE_SHEET_NAME = "DATA - Overrides";

const MSV_WARNING = "This Sheet should be updated via the ImportMSV script, do not manually edit.";
const MSV_HEADER = ["ID", "Name", "Sanity", "Override?"];

function loadOverrides() {
  const overrides = new Map(); // itemCode (string) -> {name, value}
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(OVERRIDE_SHEET_NAME);
  if (!sheet) {
    return overrides; // no override sheet yet -- nothing to apply
  }

  const lastRow = sheet.getLastRow();
  if (lastRow < 3) {
    return overrides; // note + header only, no data rows yet
  }

  const data = sheet.getRange(3, 1, lastRow - 2, 3).getValues(); // A:C
  for (const [itemCode, name, myValue] of data) {
    if (itemCode !== "" && myValue !== "") {
      // Sheets reads numeric-looking codes (e.g. 2004) back as actual numbers,
      // not strings, while the JSON's item.id is always a string -- coerce
      // both sides to string so e.g. 2004 (number) matches "2004" (string).
      overrides.set(String(itemCode), { name, value: myValue });
    }
  }
  return overrides;
}

function importItemEfficiency() {
  const response = UrlFetchApp.fetch(ITEM_EFFICIENCY_URL);
  const items = JSON.parse(response.getContentText());
  const overrides = loadOverrides();
  const matchedOverrideCodes = new Set();

  const rows = items.map((item) => {
    const override = overrides.get(item.id);
    if (override) {
      matchedOverrideCodes.add(item.id);
      return [item.id, item.enName || item.name, override.value, "X"];
    }
    return [item.id, item.enName || item.name, item.apValue, ""];
  });

  for (const [itemCode, override] of overrides) {
    if (!matchedOverrideCodes.has(itemCode)) {
      rows.push([itemCode, override.name, override.value, "X"]);
    }
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(MSV_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(MSV_SHEET_NAME);
  }
  sheet.clearContents();

  sheet.getRange(1, 1).setValue(MSV_WARNING).setFontWeight("bold");
  sheet.getRange(2, 1, 1, MSV_HEADER.length).setValues([MSV_HEADER]);
  if (rows.length > 0) {
    sheet.getRange(3, 1, rows.length, MSV_HEADER.length).setValues(rows);
  }
}
