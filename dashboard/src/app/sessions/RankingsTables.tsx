"use client";

import Link from "next/link";
import { sessionDetailHref, type SessionOrigin } from "@/lib/backLink";
import { Avatar } from "@/components/Avatar";
import { DataTable, type Column } from "@/components/DataTable";
import { RankBadge } from "@/components/RankBadge";
import { StatusBadge } from "@/components/StatusBadge";
import Tabs, { type TabDef } from "@/components/Tabs";
import { CoinIcon, PeopleIcon, UserIcon } from "@/components/icons";
import { avatarUrl, formatDuration, formatJst, formatNumber } from "@/lib/format";
import type { SessionRankingRow } from "@/lib/queries";

// One shared column set across all 3 ranking tabs (matches sample/UI's
// ライブ一覧 mock) -- only which column drives the sort (defaultSort below)
// and the rank badge (position in that sort) changes per tab. Previously
// each tab showed a different reduced column subset; this shows more, not
// less, so nothing that worked before stops working.
// 列定義は linkFrom(遷移元)に依存するので、モジュール直下の定数ではなく
// 関数にする。「詳細を見る」リンクに遷移元の印を付けるため。
const makeColumns = (linkFrom: SessionOrigin): Column<SessionRankingRow>[] => [
  {
    key: "rank",
    label: "順位",
    accessor: () => null,
    render: (_r, index) => <RankBadge index={index} />,
    width: "56px",
  },
  {
    key: "streamerName",
    label: "ライバー",
    accessor: (r) => r.streamerName,
    searchable: true,
    filterable: true,
    render: (r, index) => (
      <span className="ranking-name-cell">
        {/* Row navigates to the session (rowHref below); the name itself
            navigates to the streamer's own page instead, so it needs to
            stop the click from bubbling to the row's onClick. */}
        <Link
          href={`/streamers/${r.tiktokAccountId}`}
          className="ranking-name-link"
          onClick={(e) => e.stopPropagation()}
        >
          <Avatar name={r.streamerName} src={avatarUrl(r.avatarPath)} size={32} />
          {r.streamerName}
        </Link>
        {index === 0 && <StatusBadge label="TOP" tone="pink" />}
      </span>
    ),
  },
  {
    key: "startedAt",
    label: "配信日時",
    accessor: (r) => r.startedAt,
    render: (r) => formatJst(r.startedAt),
    width: "170px",
  },
  {
    key: "duration",
    label: "配信時間",
    accessor: (r) => (r.endedAt ? new Date(r.endedAt).getTime() - new Date(r.startedAt).getTime() : null),
    render: (r) => formatDuration(r.startedAt, r.endedAt),
    width: "110px",
  },
  {
    key: "totalDiamonds",
    label: "獲得ダイヤ",
    accessor: (r) => r.totalDiamonds,
    align: "right",
    render: (r) => <>{formatNumber(r.totalDiamonds)} 💎</>,
    width: "130px",
  },
  {
    key: "newFollowers",
    label: "新規フォロワー",
    accessor: (r) => r.newFollowers,
    align: "right",
    render: (r) => formatNumber(r.newFollowers),
    width: "130px",
  },
  {
    key: "maxViewers",
    label: "最高同接",
    accessor: (r) => r.maxViewers,
    align: "right",
    render: (r) => formatNumber(r.maxViewers),
    width: "110px",
  },
  {
    key: "action",
    label: "操作",
    accessor: () => null,
    render: (r) => (
      <Link href={sessionDetailHref(r.id, linkFrom)} className="ranking-action-link">
        詳細を見る →
      </Link>
    ),
    width: "120px",
  },
];

// #1 by whatever the active tab is currently sorted by gets a soft pink
// highlight, matching the mock's row emphasis.
const rowClassName = (_row: SessionRankingRow, index: number) => (index === 0 ? "ranking-row-top" : undefined);

export function RankingsTables({
  rows,
  // どこから開かれた一覧なのか。詳細ページはこれを見て「戻る」先を決める
  // (既定の全件一覧なら印を付けない)。lib/backLink.ts を参照。
  linkFrom = null,
}: {
  rows: SessionRankingRow[];
  linkFrom?: SessionOrigin;
}) {
  const rowHref = (r: SessionRankingRow) => sessionDetailHref(r.id, linkFrom);
  const columns = makeColumns(linkFrom);
  const emptyMessage = "配信記録がありません。";

  const tabs: TabDef[] = [
    {
      key: "diamonds",
      label: "ダイヤランキング",
      icon: <CoinIcon size={16} />,
      accent: "pink",
      content: (
        <DataTable
          rows={rows}
          columns={columns}
          defaultSort={{ key: "totalDiamonds", dir: "desc" }}
          rowHref={rowHref}
          rowClassName={rowClassName}
          emptyMessage={emptyMessage}
        />
      ),
    },
    {
      key: "followers",
      label: "新規フォロワーランキング",
      icon: <UserIcon size={16} />,
      accent: "cyan",
      content: (
        <DataTable
          rows={rows}
          columns={columns}
          defaultSort={{ key: "newFollowers", dir: "desc" }}
          rowHref={rowHref}
          rowClassName={rowClassName}
          emptyMessage={emptyMessage}
        />
      ),
    },
    {
      key: "viewers",
      label: "同接ランキング",
      icon: <PeopleIcon size={16} />,
      accent: "purple",
      content: (
        <DataTable
          rows={rows}
          columns={columns}
          defaultSort={{ key: "maxViewers", dir: "desc" }}
          rowHref={rowHref}
          rowClassName={rowClassName}
          emptyMessage={emptyMessage}
        />
      ),
    },
  ];

  return <Tabs tabs={tabs} />;
}
