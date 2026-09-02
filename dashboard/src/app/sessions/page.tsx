import Link from "next/link";
import { DashboardDbError } from "@/lib/db";
import { getSessionRankings, getStreamerName } from "@/lib/queries";
import { PageHeader } from "@/components/PageHeader";
import { RankingsTables } from "./RankingsTables";

// Session data changes as tiktok_monitor records new streams; never freeze
// this list at build time.
export const dynamic = "force-dynamic";

export default async function SessionsListPage(props: PageProps<"/sessions">) {
  const searchParams = await props.searchParams;
  const rawStreamerId = searchParams.streamerId;
  const streamerIdParam = Array.isArray(rawStreamerId) ? rawStreamerId[0] : rawStreamerId;
  const parsed = streamerIdParam ? Number(streamerIdParam) : undefined;
  const streamerId = parsed !== undefined && Number.isInteger(parsed) ? parsed : undefined;

  let rows;
  let streamerName: string | undefined;
  try {
    rows = getSessionRankings(streamerId);
    streamerName = streamerId !== undefined ? getStreamerName(streamerId) : undefined;
  } catch (err) {
    if (err instanceof DashboardDbError) {
      return (
        <div className="container">
          <PageHeader title="ライブ一覧" />
          <p className="empty">{err.message}</p>
        </div>
      );
    }
    throw err;
  }

  return (
    <div className="container">
      <PageHeader title="ライブ一覧" description="配信のパフォーマンスをランキングで比較できます。" />
      {streamerId !== undefined && (
        <p className="filter-banner">
          「{streamerName ?? `ライバーID ${streamerId}`}」で絞り込み中 ー <Link href="/sessions">絞り込み解除</Link>
        </p>
      )}
      {/* 絞り込み中の一覧から開いた詳細は、絞り込みを保ったまま戻す */}
      <RankingsTables rows={rows} linkFrom={streamerId !== undefined ? "filtered" : null} />
    </div>
  );
}
