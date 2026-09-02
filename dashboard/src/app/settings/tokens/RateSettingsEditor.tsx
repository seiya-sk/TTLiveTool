"use client";

import { useState } from "react";
import { formatJst } from "@/lib/format";
import type { SettingKey, Settings } from "@/lib/settings";

async function postSetting(key: SettingKey, value: number): Promise<Settings> {
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, value }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error ?? "保存に失敗しました");
  return data as Settings;
}

// Pure settings editor -- no cost/token display of its own. The parent
// (TokenManagement) owns the live `settings` state and recomputes cost from
// it, so saving/fetching here just needs to report the new value back up.
export function RateSettingsEditor({
  settings,
  onUpdated,
}: {
  settings: Settings;
  onUpdated: (settings: Settings) => void;
}) {
  const [rateInput, setRateInput] = useState(String(settings.usdJpyRate));
  const [priceInputInput, setPriceInputInput] = useState(String(settings.priceInputUsdPerMillion));
  const [priceOutputInput, setPriceOutputInput] = useState(String(settings.priceOutputUsdPerMillion));
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function save(key: SettingKey, raw: string) {
    const value = Number(raw);
    if (!Number.isFinite(value) || value <= 0) {
      setError("正の数値を入力してください。");
      return;
    }
    setBusy(key);
    setError(null);
    try {
      onUpdated(await postSetting(key, value));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function fetchRate() {
    setBusy("fetch");
    setError(null);
    try {
      const res = await fetch("/api/settings/fx-rate", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "取得に失敗しました");
      onUpdated(data);
      setRateInput(String(data.usdJpyRate));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="fx-settings-block">
      <h3>単価・為替レート設定</h3>

      <div className="fx-settings-row">
        <label>為替レート ($1 =</label>
        <input
          type="number"
          step="0.01"
          value={rateInput}
          onChange={(e) => setRateInput(e.target.value)}
          className="fx-input"
        />
        <label>円)</label>
        <button type="button" onClick={fetchRate} disabled={busy === "fetch"}>
          {busy === "fetch" ? "取得中..." : "為替レートを取得"}
        </button>
        <button
          type="button"
          className="fx-save-button"
          onClick={() => save("usd_jpy_rate", rateInput)}
          disabled={busy === "usd_jpy_rate"}
        >
          変更を保存
        </button>
      </div>

      <div className="fx-settings-row">
        <label>Input単価($/百万トークン)</label>
        <input
          type="number"
          step="0.01"
          value={priceInputInput}
          onChange={(e) => setPriceInputInput(e.target.value)}
          className="fx-input"
        />
        <button
          type="button"
          className="fx-save-button"
          onClick={() => save("price_input_usd_per_million", priceInputInput)}
          disabled={busy === "price_input_usd_per_million"}
        >
          保存
        </button>
      </div>

      <div className="fx-settings-row">
        <label>Output単価($/百万トークン)</label>
        <input
          type="number"
          step="0.01"
          value={priceOutputInput}
          onChange={(e) => setPriceOutputInput(e.target.value)}
          className="fx-input"
        />
        <button
          type="button"
          className="fx-save-button"
          onClick={() => save("price_output_usd_per_million", priceOutputInput)}
          disabled={busy === "price_output_usd_per_million"}
        >
          保存
        </button>
      </div>

      {settings.usdJpyRateUpdatedAt && (
        <div className="fx-updated-at">為替レート更新: {formatJst(settings.usdJpyRateUpdatedAt)}</div>
      )}
      {error && <div className="fx-error">{error}</div>}
    </div>
  );
}
