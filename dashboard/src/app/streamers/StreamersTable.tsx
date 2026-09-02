"use client";

import Link from "next/link";
import { Avatar } from "@/components/Avatar";
import { DataTable, type Column } from "@/components/DataTable";
import { RankBadge } from "@/components/RankBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { avatarUrl, formatJst, formatNumber } from "@/lib/format";
import type { StreamerRow } from "@/lib/queries";

const columns: Column<StreamerRow>[] = [
  {
    key: "rank",
    label: "順位",
    accessor: () => null,
    render: (_r, _index, meta) => <RankBadge rank={meta.rank} ranked={meta.ranked} />,
    width: "56px",
  },
  {
    key: "name",
    label: "ライバー名",
    accessor: (r) => r.name,
    searchable: true,
    render: (r, _index, meta) => (
      <span className="streamer-name-cell">
        <Avatar name={r.name} src={avatarUrl(r.avatarPath)} size={36} />
        <span className="streamer-name-cell-text">
          <span>
            {r.name}
            {/* ライブ一覧と同じ扱い: 順位として意味がある並びのときだけ出す */}
            {meta.ranked && meta.rank === 1 && r.totalDiamonds > 0 && (
              <StatusBadge label="TOP" tone="pink" />
            )}
          </span>
          <span className="streamer-name-handle">@{r.tiktokAccountId}</span>
        </span>
      </span>
    ),
  },
  {
    key: "sessionCount",
    label: "配信数",
    accessor: (r) => r.sessionCount,
    align: "right",
    width: "90px",
  },
  {
    key: "lastSessionAt",
    label: "直近配信日",
    accessor: (r) => r.lastSessionAt,
    render: (r) => formatJst(r.lastSessionAt),
    width: "150px",
  },
  {
    key: "totalDiamonds",
    label: "累計ダイヤ",
    accessor: (r) => r.totalDiamonds,
    align: "right",
    render: (r) => <>{formatNumber(r.totalDiamonds)} 💎</>,
    width: "130px",
  },
  {
    key: "avgDiamonds",
    label: "平均ダイヤ/配信",
    accessor: (r) => (r.sessionCount > 0 ? r.totalDiamonds / r.sessionCount : 0),
    align: "right",
    render: (r) => formatNumber(r.sessionCount > 0 ? r.totalDiamonds / r.sessionCount : 0),
    width: "140px",
  },
  {
    key: "status",
    label: "ステータス",
    // getStreamerList() already excludes archived streamers (see its
    // comment in queries.ts) -- every row reaching this table is active by
    // definition, so this is a constant label, not new per-row data.
    accessor: () => "アクティブ",
    render: () => <StatusBadge label="アクティブ" tone="success" />,
    width: "100px",
  },
  {
    key: "action",
    label: "操作",
    accessor: () => null,
    render: (r) => (
      <Link href={`/streamers/${r.tiktokAccountId}`} className="ranking-action-link">
        詳細を見る →
      </Link>
    ),
    width: "110px",
  },
];

export function StreamersTable({ rows }: { rows: StreamerRow[] }) {
  return (
    <DataTable
      rows={rows}
      columns={columns}
      defaultSort={{ key: "totalDiamonds", dir: "desc" }}
      rowHref={(r) => `/streamers/${r.tiktokAccountId}`}
      // ライブ一覧と同じ扱い: 「上位ほど良い」並びのときだけ1位を強調する
      rowClassName={(_r, _index, meta) => (meta.ranked && meta.rank === 1 ? "streamer-row-top" : undefined)}
      emptyMessage="登録されたライバーがいません。"
    />
  );
}
