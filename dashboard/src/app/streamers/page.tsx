import Link from "next/link";
import { DashboardDbError } from "@/lib/db";
import { countSessions, countStreamers, getCurrentJstMonth, getMonthlyOverview, getStreamerList, listSessions, shiftJstMonth } from "@/lib/queries";
import { durationMinutes, formatNumber, jstMonthKey } from "@/lib/format";
import { PageHeader } from "@/components/PageHeader";
import { KpiCard } from "@/components/KpiCard";
import { ClockIcon, CoinIcon, PeopleIcon, PlayIcon, PlusIcon } from "@/components/icons";
import { StreamersTable } from "./StreamersTable";

// Streamer/session counts change as tiktok_monitor records new streams;
// never freeze this at build time.
export const dynamic = "force-dynamic";

function formatMinutes(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return h > 0 ? `${h}h${String(m).padStart(2, "0")}m` : `${m}分`;
}

// Average of ENDED sessions' durations within one JST month -- no new
// query, just bucketing the already-fetched listSessions() rows (mirrors
// queries.ts's getMonthlyOverview pattern of comparing to the prior month).
function averageDurationForMonth(sessions: ReturnType<typeof listSessions>, month: string): number | null {
  const ended = sessions.filter((s) => s.endedAt && jstMonthKey(s.startedAt) === month);
  if (ended.length === 0) return null;
  const total = ended.reduce((sum, s) => sum + durationMinutes(s.startedAt, s.endedAt), 0);
  return total / ended.length;
}

function changePercent(current: number, previous: number): number | null {
  if (previous <= 0) return null;
  return ((current - previous) / previous) * 100;
}

export default function StreamersPage() {
  let rows;
  let streamerCount: number;
  let sessionCount: number;
  let overview: ReturnType<typeof getMonthlyOverview>;
  let avgDurationMinutes: number | null;
  let avgDurationChangePercent: number | null;
  try {
    rows = getStreamerList();
    streamerCount = countStreamers();
    sessionCount = countSessions();
    overview = getMonthlyOverview();
    const sessions = listSessions();
    const currentMonth = getCurrentJstMonth();
    const previousMonth = shiftJstMonth(currentMonth, -1);
    avgDurationMinutes = averageDurationForMonth(sessions, currentMonth);
    const previousAvgDuration = averageDurationForMonth(sessions, previousMonth);
    avgDurationChangePercent =
      avgDurationMinutes !== null && previousAvgDuration !== null
        ? changePercent(avgDurationMinutes, previousAvgDuration)
        : null;
  } catch (err) {
    if (err instanceof DashboardDbError) {
      return (
        <div className="container">
          <PageHeader title="ライバー一覧" />
          <p className="empty">{err.message}</p>
        </div>
      );
    }
    throw err;
  }

  return (
    <div className="container">
      <PageHeader
        title="ライバー一覧"
        description="登録ライバーのパフォーマンスを一覧で確認できます。"
        actions={
          <Link href="/settings/streamers" className="page-cta">
            <PlusIcon size={16} />
            ライバーを追加
          </Link>
        }
      />

      <div className="kpi-grid">
        <KpiCard accent="cyan" icon={<PeopleIcon size={16} />} label="登録ライバー数" value={streamerCount} />
        <KpiCard
          accent="pink"
          icon={<PlayIcon size={16} />}
          label="総配信数"
          value={sessionCount}
          caption={`今月 ${overview.sessionCount}配信`}
        />
        <KpiCard
          accent="pink"
          icon={<CoinIcon size={16} />}
          label="今月の獲得ダイヤ"
          value={formatNumber(overview.totalDiamonds)}
          trend={
            overview.diamondsChangePercent !== null
              ? {
                  direction: overview.diamondsChangePercent >= 0 ? "up" : "down",
                  text: `${Math.abs(overview.diamondsChangePercent).toFixed(1)}% 先月比`,
                }
              : null
          }
        />
        <KpiCard
          accent="purple"
          icon={<ClockIcon size={16} />}
          label="平均配信時間"
          value={avgDurationMinutes !== null ? formatMinutes(avgDurationMinutes) : "-"}
          caption={
            avgDurationChangePercent !== null ? (
              <>
                先月比{" "}
                <span className={avgDurationChangePercent >= 0 ? "trend-up-inline" : "trend-down-inline"}>
                  {avgDurationChangePercent >= 0 ? "↑" : "↓"}
                  {Math.abs(avgDurationChangePercent).toFixed(1)}%
                </span>
              </>
            ) : null
          }
        />
      </div>

      <StreamersTable rows={rows} />
    </div>
  );
}
