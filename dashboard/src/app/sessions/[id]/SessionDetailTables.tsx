"use client";

import { Avatar } from "@/components/Avatar";
import { formatJst } from "@/lib/format";
import type {
  BattleOpponentRow,
  CommentRow,
  FollowRow,
  GiftDetailRow,
  GiftRankingRow,
  JoinRow,
} from "@/lib/queries";
import { DataTable, type Column } from "@/components/DataTable";
import Tabs, { type TabDef } from "@/components/Tabs";

function NameCell({ name }: { name: string | null }) {
  if (!name) return <>-</>;
  return (
    <span className="ranking-name-cell">
      <Avatar name={name} size={26} />
      {name}
    </span>
  );
}

const battleColumns: Column<BattleOpponentRow>[] = [
  {
    key: "occurredAt",
    label: "検知時刻",
    accessor: (r) => r.occurredAt,
    render: (r) => formatJst(r.occurredAt),
    width: "180px",
  },
  {
    key: "opponentId",
    label: "相手ID",
    accessor: (r) => r.opponentId,
    searchable: true,
    filterable: true,
    render: (r) => <NameCell name={r.opponentId} />,
  },
];

const giftRankingColumns: Column<GiftRankingRow>[] = [
  {
    key: "nickname",
    label: "ユーザー名",
    accessor: (r) => r.nickname ?? r.userId,
    searchable: true,
    filterable: true,
    render: (r) => <NameCell name={r.nickname ?? r.userId} />,
  },
  { key: "gifterLevel", label: "ギフレベ", accessor: (r) => r.gifterLevel, filterable: true, width: "100px" },
  {
    key: "totalDiamonds",
    label: "ダイヤ合計",
    accessor: (r) => r.totalDiamonds,
    align: "right",
    render: (r) => r.totalDiamonds.toLocaleString("ja-JP"),
    width: "140px",
  },
];

const giftDetailColumns: Column<GiftDetailRow>[] = [
  {
    key: "occurredAt",
    label: "時刻",
    accessor: (r) => r.occurredAt,
    render: (r) => formatJst(r.occurredAt),
    width: "180px",
  },
  {
    key: "nickname",
    label: "ユーザー名",
    accessor: (r) => r.nickname,
    searchable: true,
    filterable: true,
    render: (r) => <NameCell name={r.nickname} />,
  },
  { key: "gifterLevel", label: "ギフレベ", accessor: (r) => r.gifterLevel, filterable: true, width: "100px" },
  {
    key: "giftName",
    label: "ギフト名",
    accessor: (r) => r.giftName,
    searchable: true,
    filterable: true,
    width: "180px",
  },
  { key: "repeatCount", label: "個数", accessor: (r) => r.repeatCount, align: "right", width: "90px" },
];

const joinColumns: Column<JoinRow>[] = [
  {
    key: "nickname",
    label: "ユーザー名",
    accessor: (r) => r.nickname ?? r.userId,
    searchable: true,
    filterable: true,
    render: (r) => <NameCell name={r.nickname ?? r.userId} />,
  },
  { key: "gifterLevel", label: "ギフレベ", accessor: (r) => r.gifterLevel, filterable: true, width: "100px" },
  { key: "entryCount", label: "入室回数", accessor: (r) => r.entryCount, align: "right", width: "120px" },
];

const followColumns: Column<FollowRow>[] = [
  {
    key: "occurredAt",
    label: "時刻",
    accessor: (r) => r.occurredAt,
    render: (r) => formatJst(r.occurredAt),
    width: "180px",
  },
  {
    key: "nickname",
    label: "ユーザー名",
    accessor: (r) => r.nickname,
    searchable: true,
    filterable: true,
    render: (r) => <NameCell name={r.nickname} />,
  },
  { key: "gifterLevel", label: "ギフレベ", accessor: (r) => r.gifterLevel, filterable: true, width: "100px" },
];

const commentColumns: Column<CommentRow>[] = [
  {
    key: "occurredAt",
    label: "時刻",
    accessor: (r) => r.occurredAt,
    render: (r) => formatJst(r.occurredAt),
    width: "170px",
  },
  {
    key: "nickname",
    label: "ユーザー名",
    accessor: (r) => r.nickname,
    searchable: true,
    filterable: true,
    width: "180px",
    render: (r) => <NameCell name={r.nickname} />,
  },
  { key: "gifterLevel", label: "ギフレベ", accessor: (r) => r.gifterLevel, filterable: true, width: "90px" },
  { key: "memberLevel", label: "メンバーレベル", accessor: (r) => r.memberLevel, filterable: true, width: "110px" },
  { key: "comment", label: "コメント", accessor: (r) => r.comment, searchable: true },
];

export function SessionDetailTables({
  battleOpponents,
  giftRanking,
  giftDetail,
  joins,
  follows,
  comments,
}: {
  battleOpponents: BattleOpponentRow[];
  giftRanking: GiftRankingRow[];
  giftDetail: GiftDetailRow[];
  joins: JoinRow[];
  follows: FollowRow[];
  comments: CommentRow[];
}) {
  const tabs: TabDef[] = [
    {
      key: "battle",
      label: "バトル相手",
      content: (
        <DataTable rows={battleOpponents} columns={battleColumns} emptyMessage="検知されたバトルはありません。" />
      ),
    },
    {
      key: "gift-ranking",
      label: "ギフトランキング",
      content: <DataTable rows={giftRanking} columns={giftRankingColumns} emptyMessage="ギフトはありません。" />,
    },
    {
      key: "gift-detail",
      label: "ギフト明細",
      content: <DataTable rows={giftDetail} columns={giftDetailColumns} emptyMessage="ギフトはありません。" />,
    },
    {
      key: "joins",
      label: "入室管理",
      content: <DataTable rows={joins} columns={joinColumns} emptyMessage="入室記録はありません。" />,
    },
    {
      key: "follows",
      label: "フォロー",
      content: <DataTable rows={follows} columns={followColumns} emptyMessage="フォローはありません。" />,
    },
    {
      key: "comments",
      label: "コメント",
      content: <DataTable rows={comments} columns={commentColumns} emptyMessage="コメントはありません。" />,
    },
  ];

  return <Tabs tabs={tabs} />;
}
