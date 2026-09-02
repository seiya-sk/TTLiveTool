"use client";

import { useMemo, useState } from "react";
import { Avatar } from "@/components/Avatar";
import { DataTable, type Column } from "@/components/DataTable";
import { KpiCard } from "@/components/KpiCard";
import { ArrowDownIcon, ArrowUpIcon, CoinIcon, DocumentIcon } from "@/components/icons";
import { avatarUrl, formatJst, formatNumber } from "@/lib/format";
import type { ReportUsageRow } from "@/lib/queries";
import type { Settings } from "@/lib/settings";
import { RateSettingsEditor } from "./RateSettingsEditor";

type Period = "all" | "thisMonth" | "lastMonth" | "custom";

// Formats as YYYY-MM-DD in JST regardless of the browser's local timezone
// -- "en-CA" is a well-known trick for getting Intl.DateTimeFormat to
// produce that exact ordering without manual string surgery.
function toJstDateString(isoUtc: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Tokyo" }).format(new Date(isoUtc));
}

function jstYearMonth(isoUtc: string): string {
  return toJstDateString(isoUtc).slice(0, 7);
}

function shiftMonth(yearMonth: string, delta: number): string {
  const [y, m] = yearMonth.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + delta, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

function calcCost(inputTokens: number, outputTokens: number, settings: Settings) {
  const usd =
    (inputTokens / 1_000_000) * settings.priceInputUsdPerMillion +
    (outputTokens / 1_000_000) * settings.priceOutputUsdPerMillion;
  return { usd, jpy: usd * settings.usdJpyRate };
}

function formatUsd(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

function formatJpy(jpy: number): string {
  return `¥${Math.round(jpy).toLocaleString("ja-JP")}`;
}

type StreamerUsage = {
  streamerId: number;
  streamerName: string;
  tiktokAccountId: string;
  reportCount: number;
  inputTokens: number;
  outputTokens: number;
  avatarPath: string | null;
};

type SessionUsage = {
  sessionId: number;
  streamerName: string;
  sessionStartedAt: string;
  reportCount: number;
  inputTokens: number;
  outputTokens: number;
};

export function TokenManagement({
  usageRows,
  initialSettings,
}: {
  usageRows: ReportUsageRow[];
  initialSettings: Settings;
}) {
  // Owned here (not just passed through) so editing the rate/price via
  // RateSettingsEditor below immediately recomputes every cost figure on
  // this page -- no page reload needed.
  const [settings, setSettings] = useState(initialSettings);
  const nowJstYearMonth = useMemo(() => jstYearMonth(new Date().toISOString()), []);
  const [period, setPeriod] = useState<Period>("thisMonth");
  const [customFrom, setCustomFrom] = useState(() => `${nowJstYearMonth}-01`);
  const [customTo, setCustomTo] = useState(() => toJstDateString(new Date().toISOString()));

  const filtered = useMemo(() => {
    if (period === "all") return usageRows;
    if (period === "thisMonth") {
      return usageRows.filter((r) => jstYearMonth(r.generatedAt) === nowJstYearMonth);
    }
    if (period === "lastMonth") {
      const target = shiftMonth(nowJstYearMonth, -1);
      return usageRows.filter((r) => jstYearMonth(r.generatedAt) === target);
    }
    // custom
    return usageRows.filter((r) => {
      const d = toJstDateString(r.generatedAt);
      return d >= customFrom && d <= customTo;
    });
  }, [usageRows, period, nowJstYearMonth, customFrom, customTo]);

  const totals = useMemo(
    () =>
      filtered.reduce(
        (acc, r) => {
          acc.inputTokens += r.inputTokens;
          acc.outputTokens += r.outputTokens;
          return acc;
        },
        { inputTokens: 0, outputTokens: 0 }
      ),
    [filtered]
  );
  const totalCost = calcCost(totals.inputTokens, totals.outputTokens, settings);

  const byStreamer = useMemo(() => {
    const map = new Map<number, StreamerUsage>();
    for (const r of filtered) {
      const entry = map.get(r.streamerId) ?? {
        streamerId: r.streamerId,
        streamerName: r.streamerName,
        tiktokAccountId: r.tiktokAccountId,
        reportCount: 0,
        inputTokens: 0,
        outputTokens: 0,
        avatarPath: r.avatarPath,
      };
      entry.reportCount += 1;
      entry.inputTokens += r.inputTokens;
      entry.outputTokens += r.outputTokens;
      map.set(r.streamerId, entry);
    }
    return Array.from(map.values());
  }, [filtered]);

  const bySession = useMemo(() => {
    const map = new Map<number, SessionUsage>();
    for (const r of filtered) {
      const entry = map.get(r.sessionId) ?? {
        sessionId: r.sessionId,
        streamerName: r.streamerName,
        sessionStartedAt: r.sessionStartedAt,
        reportCount: 0,
        inputTokens: 0,
        outputTokens: 0,
      };
      entry.reportCount += 1;
      entry.inputTokens += r.inputTokens;
      entry.outputTokens += r.outputTokens;
      map.set(r.sessionId, entry);
    }
    return Array.from(map.values());
  }, [filtered]);

  const streamerColumns: Column<StreamerUsage>[] = [
    {
      key: "streamerName",
      label: "ライバー",
      accessor: (r) => r.streamerName,
      searchable: true,
      render: (r) => (
        <span className="ranking-name-cell">
          <Avatar name={r.streamerName} src={avatarUrl(r.avatarPath)} size={28} />
          {r.streamerName}
        </span>
      ),
    },
    { key: "reportCount", label: "レポート数", accessor: (r) => r.reportCount, align: "right", width: "110px" },
    {
      key: "inputTokens",
      label: "inputトークン",
      accessor: (r) => r.inputTokens,
      align: "right",
      render: (r) => formatNumber(r.inputTokens),
      width: "130px",
    },
    {
      key: "outputTokens",
      label: "outputトークン",
      accessor: (r) => r.outputTokens,
      align: "right",
      render: (r) => formatNumber(r.outputTokens),
      width: "130px",
    },
    {
      key: "costJpy",
      label: "コスト(円)",
      accessor: (r) => calcCost(r.inputTokens, r.outputTokens, settings).jpy,
      align: "right",
      render: (r) => formatJpy(calcCost(r.inputTokens, r.outputTokens, settings).jpy),
      width: "120px",
    },
    {
      key: "costUsd",
      label: "コスト($)",
      accessor: (r) => calcCost(r.inputTokens, r.outputTokens, settings).usd,
      align: "right",
      render: (r) => formatUsd(calcCost(r.inputTokens, r.outputTokens, settings).usd),
      width: "110px",
    },
  ];

  const sessionColumns: Column<SessionUsage>[] = [
    {
      key: "sessionStartedAt",
      label: "日付",
      accessor: (r) => r.sessionStartedAt,
      render: (r) => formatJst(r.sessionStartedAt),
      width: "180px",
    },
    { key: "streamerName", label: "ライバー名", accessor: (r) => r.streamerName, searchable: true, filterable: true },
    { key: "reportCount", label: "レポート数", accessor: (r) => r.reportCount, align: "right", width: "110px" },
    {
      key: "inputTokens",
      label: "inputトークン",
      accessor: (r) => r.inputTokens,
      align: "right",
      render: (r) => formatNumber(r.inputTokens),
      width: "130px",
    },
    {
      key: "outputTokens",
      label: "outputトークン",
      accessor: (r) => r.outputTokens,
      align: "right",
      render: (r) => formatNumber(r.outputTokens),
      width: "130px",
    },
    {
      key: "costJpy",
      label: "コスト(円)",
      accessor: (r) => calcCost(r.inputTokens, r.outputTokens, settings).jpy,
      align: "right",
      render: (r) => formatJpy(calcCost(r.inputTokens, r.outputTokens, settings).jpy),
      width: "120px",
    },
    {
      key: "costUsd",
      label: "コスト($)",
      accessor: (r) => calcCost(r.inputTokens, r.outputTokens, settings).usd,
      align: "right",
      render: (r) => formatUsd(calcCost(r.inputTokens, r.outputTokens, settings).usd),
      width: "110px",
    },
  ];

  return (
    <div>
      <div className="token-period-controls">
        <div className="token-period-presets">
          {(
            [
              ["thisMonth", "今月"],
              ["lastMonth", "先月"],
              ["all", "全期間"],
              ["custom", "期間指定"],
            ] as [Period, string][]
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={`tab-button${period === value ? " active" : ""}`}
              onClick={() => setPeriod(value)}
            >
              {label}
            </button>
          ))}
        </div>
        {period === "custom" && (
          <div className="token-period-custom">
            <input type="date" value={customFrom} onChange={(e) => setCustomFrom(e.target.value)} />
            <span>〜</span>
            <input type="date" value={customTo} onChange={(e) => setCustomTo(e.target.value)} />
          </div>
        )}
      </div>

      <div className="kpi-grid">
        <KpiCard accent="cyan" icon={<DocumentIcon size={16} />} label="対象レポート数" value={`${filtered.length}件`} />
        <KpiCard
          accent="cyan"
          icon={<ArrowDownIcon size={16} />}
          label="Inputトークン合計"
          value={<>{formatNumber(totals.inputTokens)} <span className="kpi-unit">tokens</span></>}
        />
        <KpiCard
          accent="purple"
          icon={<ArrowUpIcon size={16} />}
          label="Outputトークン合計"
          value={<>{formatNumber(totals.outputTokens)} <span className="kpi-unit">tokens</span></>}
        />
        <KpiCard
          accent="pink"
          icon={<CoinIcon size={16} />}
          label="合計コスト"
          value={formatJpy(totalCost.jpy)}
          caption={formatUsd(totalCost.usd)}
        />
      </div>
      <p className="token-price-note">
        単価: input ${settings.priceInputUsdPerMillion}/output ${settings.priceOutputUsdPerMillion}
        (百万トークンあたり) ・ 為替レート $1 = ¥{settings.usdJpyRate}
        {settings.usdJpyRateUpdatedAt && <> (更新: {formatJst(settings.usdJpyRateUpdatedAt)})</>}
      </p>

      <RateSettingsEditor settings={settings} onUpdated={setSettings} />

      <h2>ライバー別</h2>
      <DataTable
        rows={byStreamer}
        columns={streamerColumns}
        defaultSort={{ key: "costJpy", dir: "desc" }}
        rowHref={(r) => `/streamers/${r.tiktokAccountId}`}
        emptyMessage="この期間のレポートがありません。"
      />

      <h2>ライブ別</h2>
      <DataTable
        rows={bySession}
        columns={sessionColumns}
        defaultSort={{ key: "sessionStartedAt", dir: "desc" }}
        rowHref={(r) => `/sessions/${r.sessionId}`}
        emptyMessage="この期間のレポートがありません。"
      />
    </div>
  );
}
