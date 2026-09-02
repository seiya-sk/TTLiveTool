import { getDb, getWritableDb } from "./db";

export type Settings = {
  usdJpyRate: number;
  usdJpyRateUpdatedAt: string | null;
  priceInputUsdPerMillion: number;
  priceOutputUsdPerMillion: number;
};

// Claude Sonnet 5 pricing (USD per million tokens) as of 2026-08; cache
// token pricing isn't tracked separately since measured cache usage on
// this project's reports has been 0 so far -- see live_reports.summary_json
// .usage. All three of these are user-editable in the dashboard (prices and
// FX rates both drift over time), these are just the seed defaults for a
// fresh app_settings row.
const DEFAULTS = {
  usd_jpy_rate: 150,
  price_input_usd_per_million: 2,
  price_output_usd_per_million: 10,
} as const;

export type SettingKey = keyof typeof DEFAULTS;

export const SETTING_KEYS = Object.keys(DEFAULTS) as SettingKey[];

function readSetting(key: SettingKey): { value: number; updatedAt: string | null } {
  const db = getDb();
  let row: { value: string; updated_at: string } | undefined;
  try {
    row = db.prepare(`SELECT value, updated_at FROM app_settings WHERE key = ?`).get(key) as
      | { value: string; updated_at: string }
      | undefined;
  } catch {
    // app_settings may not exist yet if a tiktok_monitor entrypoint hasn't
    // re-run init_schema since this table was added -- fall back to the
    // default rather than failing the whole page.
    row = undefined;
  }
  const parsed = row ? Number(row.value) : NaN;
  return { value: Number.isFinite(parsed) ? parsed : DEFAULTS[key], updatedAt: row?.updated_at ?? null };
}

export function getSettings(): Settings {
  const rate = readSetting("usd_jpy_rate");
  const priceInput = readSetting("price_input_usd_per_million");
  const priceOutput = readSetting("price_output_usd_per_million");
  return {
    usdJpyRate: rate.value,
    usdJpyRateUpdatedAt: rate.updatedAt,
    priceInputUsdPerMillion: priceInput.value,
    priceOutputUsdPerMillion: priceOutput.value,
  };
}

export function setSetting(key: SettingKey, value: number): void {
  const db = getWritableDb();
  db.prepare(
    `INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
     ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
  ).run(key, String(value), new Date().toISOString());
}
