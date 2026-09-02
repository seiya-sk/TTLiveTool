import Link from "next/link";
import { notFound } from "next/navigation";
import { Avatar } from "@/components/Avatar";
import { KpiCard } from "@/components/KpiCard";
import { ClockIcon, CoinIcon, PeopleIcon, PlayIcon } from "@/components/icons";
import { DashboardDbError } from "@/lib/db";
import { avatarUrl, durationMinutes, formatJst, formatMinutes, formatNumber } from "@/lib/format";
import { getSessionRankings, getStreamerByAccountId } from "@/lib/queries";
import { RankingsTables } from "@/app/sessions/RankingsTables";

// Streamer stats change as tiktok_monitor records new streams; never freeze
// this at build time.
export const dynamic = "force-dynamic";

export default async function StreamerDetailPage(props: PageProps<"/streamers/[accountId]">) {
  const { accountId } = await props.params;

  let streamer;
  let sessions;
  try {
    streamer = getStreamerByAccountId(accountId);
    if (!streamer) notFound();
    sessions = getSessionRankings(streamer.id);
  } catch (err) {
    if (err instanceof DashboardDbError) {
      return (
        <div className="container">
          <Link href="/streamers" className="back-link">
            ← ライバー一覧に戻る
          </Link>
          <p className="empty">{err.message}</p>
        </div>
      );
    }
    throw err;
  }

  // Derived from getSessionRankings' existing per-session rows -- no new
  // aggregation query needed. avgViewers here is a mean of each session's
  // own average, not a true global average across every viewer_count
  // sample; maxViewers is a true max since it's just the largest per-
  // session max.
  const avgViewersSamples = sessions.map((s) => s.avgViewers).filter((v): v is number => v !== null);
  const avgViewers = avgViewersSamples.length > 0 ? avgViewersSamples.reduce((a, b) => a + b, 0) / avgViewersSamples.length : null;
  const maxViewersSamples = sessions.map((s) => s.maxViewers).filter((v): v is number => v !== null);
  const maxViewers = maxViewersSamples.length > 0 ? Math.max(...maxViewersSamples) : null;
  const avgDurationMinutes =
    sessions.length > 0
      ? sessions.reduce((sum, s) => sum + durationMinutes(s.startedAt, s.endedAt), 0) / sessions.length
      : null;

  return (
    <div className="container">
      <Link href="/streamers" className="back-link">
        ← ライバー一覧に戻る
      </Link>

      {/* 情報表示エリア -- サマリーのみ、ここには操作系のUIを置かない */}
      <section className="streamer-detail-info">
        <div className="detail-title-row">
          <Avatar name={streamer.name} src={avatarUrl(streamer.avatarPath)} size={48} />
          <h1>{streamer.name}</h1>
        </div>
        <p className="streamer-detail-handle">@{streamer.tiktokAccountId}</p>

        <div className="kpi-grid">
          <KpiCard
            featured
            label="累計ダイヤ"
            value={`${formatNumber(streamer.totalDiamonds)} 💎`}
            icon={<CoinIcon size={16} />}
            accent="pink"
          />
          <KpiCard label="配信数" value={formatNumber(streamer.sessionCount)} icon={<PlayIcon size={16} />} accent="cyan" />
          <KpiCard label="平均同接" value={formatNumber(avgViewers)} icon={<PeopleIcon size={16} />} accent="purple" />
          <KpiCard label="最高同接" value={formatNumber(maxViewers)} icon={<PeopleIcon size={16} />} accent="cyan" />
          <KpiCard label="直近配信日" value={formatJst(streamer.lastSessionAt)} icon={<ClockIcon size={16} />} accent="muted" />
          <KpiCard label="平均配信時間" value={formatMinutes(avgDurationMinutes)} icon={<ClockIcon size={16} />} accent="muted" />
        </div>
      </section>

      {/* 操作エリア(将来追加): アクティブ/非アクティブ切替、監視対象への追加/除外など。
          アクティブ/非アクティブ切替は設定/ライバー管理(StreamerManagement.tsx)が
          既に POST /api/streamers/[id] { archived } で実装済み -- ここに追加する
          ときは同じエンドポイントをクライアントコンポーネントから叩く形で流用できる。
          今は何も表示しない(空の操作エリアを見せない)。 */}

      <section className="streamer-detail-sessions">
        <h2>配信一覧</h2>
        <RankingsTables rows={sessions} linkFrom="streamer" />
      </section>
    </div>
  );
}
