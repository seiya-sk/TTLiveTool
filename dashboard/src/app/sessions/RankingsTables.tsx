"use client";

import Link from "next/link";
import { sessionDetailHref, type SessionOrigin } from "@/lib/backLink";
import { Avatar } from "@/components/Avatar";
import { DataTable, type Column, type RowRankMeta } from "@/components/DataTable";
import { RankBadge } from "@/components/RankBadge";
import { StatusBadge } from "@/components/StatusBadge";
import Tabs, { type TabDef } from "@/components/Tabs";
import { ClockIcon, CoinIcon, PeopleIcon, UserIcon } from "@/components/icons";
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
    render: (_r, _index, meta) => <RankBadge rank={meta.rank} ranked={meta.ranked} />,
    width: "56px",
  },
  {
    key: "streamerName",
    label: "ライバー",
    accessor: (r) => r.streamerName,
    searchable: true,
    filterable: true,
    render: (r, _index, meta) => (
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
        {/* 「TOP」は1位の目印。**順位として意味がある並びのときだけ**出す
            (meta.ranked)。日時順や昇順では先頭行は1位ではないので、
            付けると誤解を招く -- 実際、獲得ダイヤの昇順で0ダイヤの行に
            付いていた。 */}
        {meta.ranked && meta.rank === 1 && <StatusBadge label="TOP" tone="pink" />}
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

// 1位の行だけ淡いピンクで強調する。**「上位ほど良い」並びのときだけ**
// 出す(meta.ranked)。昇順や日時での並べ替えでは、先頭行は1位ではなく
// 単に先頭なので、強調すると誤解を招く。
const rowClassName = (_row: SessionRankingRow, _index: number, meta: RowRankMeta) =>
  meta.ranked && meta.rank === 1 ? "ranking-row-top" : undefined;

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
  const emptyMessage = "ライブの記録がありません。";

  const tabs: TabDef[] = [
    {
      // 既定タブ。ホームの「直近のライブ」から「すべて見る」で来たときに、
      // いきなりダイヤランキングに着地しないようにするための入口。
      //
      // 並び順はホームと **同じクエリ・同じ並び**(getSessionRankings の
      // ORDER BY ls.started_at DESC)にそろえる。ホームは同じ結果の先頭7件を
      // 出しているだけなので、ここが違うと「同じ名前の別物」になる。
      key: "recent",
      label: "直近のライブ",
      icon: <ClockIcon size={16} />,
      accent: "cyan",
      content: (
        <DataTable
          rows={rows}
          columns={columns}
          defaultSort={{ key: "startedAt", dir: "desc" }}
          rowHref={rowHref}
          rowClassName={rowClassName}
          emptyMessage={emptyMessage}
        />
      ),
    },
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
