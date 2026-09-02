import Link from "next/link";
import { DashboardDbError } from "@/lib/db";
import { countSessions, countStreamers, getCurrentJstMonth, getMonthlyOverview, getSessionRankings } from "@/lib/queries";
import { listUnassignedStreamers } from "@/lib/notifications";
import { avatarUrl, formatDuration, formatJst, formatNumber } from "@/lib/format";
import { PageHeader } from "@/components/PageHeader";
import { KpiCard } from "@/components/KpiCard";
import { Avatar } from "@/components/Avatar";
import { StatusBadge } from "@/components/StatusBadge";
import { MonthSelect } from "./MonthSelect";

// Session/streamer counts change as tiktok_monitor records new streams;
// never freeze this at build time.
export const dynamic = "force-dynamic";

const RECENT_SESSIONS_LIMIT = 7;
const MONTH_OPTIONS_COUNT = 6;

function monthOptions(currentMonth: string): { value: string; label: string }[] {
  const [y, m] = currentMonth.split("-").map(Number);
  const options: { value: string; label: string }[] = [];
  for (let i = 0; i < MONTH_OPTIONS_COUNT; i++) {
    const total = y * 12 + (m - 1) - i;
    const year = Math.floor(total / 12);
    const monthIndex = ((total % 12) + 12) % 12;
    const value = `${year}-${String(monthIndex + 1).padStart(2, "0")}`;
    const label = i === 0 ? `今月(${year}年${monthIndex + 1}月)` : `${year}年${monthIndex + 1}月`;
    options.push({ value, label });
  }
  return options;
}

function formatSignedPercent(percent: number): string {
  return `${Math.abs(percent).toFixed(1)}%`;
}

export default async function HomePage(props: PageProps<"/">) {
  const searchParams = await props.searchParams;
  const rawMonth = searchParams.month;
  const monthParam = Array.isArray(rawMonth) ? rawMonth[0] : rawMonth;
  const currentMonth = getCurrentJstMonth();
  const month = monthParam && /^\d{4}-\d{2}$/.test(monthParam) ? monthParam : currentMonth;

  let streamerCount: number;
  let sessionCount: number;
  let overview: ReturnType<typeof getMonthlyOverview>;
  let recentSessions: ReturnType<typeof getSessionRankings>;
  let unassigned: ReturnType<typeof listUnassignedStreamers>;
  try {
    streamerCount = countStreamers();
    sessionCount = countSessions();
    overview = getMonthlyOverview(month);
    recentSessions = getSessionRankings().slice(0, RECENT_SESSIONS_LIMIT);
    unassigned = listUnassignedStreamers();
  } catch (err) {
    if (err instanceof DashboardDbError) {
      return (
        <div className="container">
          <PageHeader title="ホーム" />
          <p className="empty">{err.message}</p>
        </div>
      );
    }
    throw err;
  }

  const lastUpdated = formatJst(new Date().toISOString());

  return (
    <div className="container">
      <PageHeader
        title={<>おかえりなさい！<span aria-hidden>👋</span></>}
        description="今日も素敵なLIVEを分析しましょう。"
        actions={
          <>
            <MonthSelect options={monthOptions(currentMonth)} value={month} />
            <span className="last-updated">
              最終更新
              <br />
              {lastUpdated}
            </span>
          </>
        }
      />

      {/* 平常時は何も出さない -- 未割り当てが1人でもいる時だけ出すことで、
          「静かなら問題なし」が成立するようにしている。割り当て漏れは
          システム異常ではないのでエラー通知には送らず、ここで気づかせる。 */}
      {unassigned.length > 0 && (
        <Link href="/settings/notifications" className="home-unassigned-banner">
          <strong>未割り当てのライバーが{unassigned.length}名います。</strong>
          <span>
            {unassigned.slice(0, 5).map((s) => s.name).join("、")}
            {unassigned.length > 5 ? ` ほか${unassigned.length - 5}名` : ""}
            ― どのグループにも属していないため、進捗通知に含まれません。
          </span>
          <span className="home-unassigned-cta">通知設定へ →</span>
        </Link>
      )}

      <div className="home-kpi-row">
        <KpiCard
          featured
          accent="pink"
          label="今月の獲得ダイヤ"
          value={formatNumber(overview.totalDiamonds)}
          trend={
            overview.diamondsChangePercent !== null
              ? {
                  direction: overview.diamondsChangePercent >= 0 ? "up" : "down",
                  text: `${formatSignedPercent(overview.diamondsChangePercent)} 先月比`,
                }
              : null
          }
          illustrationSrc="/images/diamond.png"
        />
        <KpiCard
          accent="cyan"
          label="登録ライバー数"
          value={streamerCount}
          caption={`アクティブ ${streamerCount}名`}
          href="/streamers"
        />
        <KpiCard
          accent="pink"
          label="総ライブ数"
          value={sessionCount}
          caption={`今月 ${overview.sessionCount}件`}
          href="/sessions"
        />
      </div>

      <div className="home-main-row">
        <section className="home-panel">
          <div className="home-panel-header">
            <h2 className="home-panel-title">直近のライブ</h2>
            <Link href="/sessions" className="home-panel-link">
              すべて見る →
            </Link>
          </div>
          {recentSessions.length === 0 ? (
            <p className="empty">まだ記録されたライブがありません。</p>
          ) : (
            <table className="recent-sessions-table">
              <thead>
                <tr>
                  <th>ライバー</th>
                  <th>配信日時</th>
                  <th>配信時間</th>
                  <th className="col-right">獲得ダイヤ</th>
                  <th>ステータス</th>
                </tr>
              </thead>
              <tbody>
                {recentSessions.map((s) => (
                  <tr key={s.id}>
                    <td>
                      {/* Streamer name goes to the streamer's own page; the
                          date below is this row's link to the session
                          itself (see sessions/RankingsTables.tsx for the
                          same split). */}
                      <Link href={`/streamers/${s.tiktokAccountId}`} className="recent-session-name">
                        <Avatar name={s.streamerName} src={avatarUrl(s.avatarPath)} size={32} />
                        {s.streamerName}
                      </Link>
                    </td>
                    <td>
                      <Link href={`/sessions/${s.id}`} className="recent-session-date">
                        {formatJst(s.startedAt)}
                      </Link>
                    </td>
                    <td>{formatDuration(s.startedAt, s.endedAt)}</td>
                    <td className="col-right">{formatNumber(s.totalDiamonds)} 💎</td>
                    <td>
                      <StatusBadge label={s.endedAt ? "終了" : "アクティブ"} tone={s.endedAt ? "muted" : "success"} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="home-panel home-highlight">
          <div className="home-panel-header">
            <h2 className="home-panel-title">🔥 今月のハイライト</h2>
          </div>
          {overview.topSession ? (
            <>
              <div className="home-highlight-card">
                <StatusBadge label="TOP LIVE" tone="pink" />
                <div className="home-highlight-name">{overview.topSession.streamerName}</div>
                <div className="home-highlight-label">獲得ダイヤ</div>
                <div className="home-highlight-value">{formatNumber(overview.topSession.totalDiamonds)} 💎</div>
                <p className="home-highlight-note">
                  今月の{overview.topSession.percentOfMonthTotal.toFixed(1)}%を占めています！
                </p>
                {/* eslint-disable-next-line @next/next/no-img-element -- static decorative art */}
                <img src="/images/trophy.png" alt="" className="home-highlight-illustration" aria-hidden />
              </div>

              <div className="home-highlight-stats">
                <KpiCard
                  accent="cyan"
                  label="平均同接"
                  value={overview.avgConcurrentViewers !== null ? formatNumber(overview.avgConcurrentViewers) : "-"}
                  caption={
                    overview.avgConcurrentChangePercent !== null ? (
                      <>
                        先月比{" "}
                        <span className={overview.avgConcurrentChangePercent >= 0 ? "trend-up-inline" : "trend-down-inline"}>
                          {overview.avgConcurrentChangePercent >= 0 ? "↑" : "↓"}
                          {formatSignedPercent(overview.avgConcurrentChangePercent)}
                        </span>
                      </>
                    ) : null
                  }
                />
                <KpiCard
                  accent="purple"
                  label="総コメント数"
                  value={formatNumber(overview.totalComments)}
                  caption={
                    overview.totalCommentsChangePercent !== null ? (
                      <>
                        先月比{" "}
                        <span className={overview.totalCommentsChangePercent >= 0 ? "trend-up-inline" : "trend-down-inline"}>
                          {overview.totalCommentsChangePercent >= 0 ? "↑" : "↓"}
                          {formatSignedPercent(overview.totalCommentsChangePercent)}
                        </span>
                      </>
                    ) : null
                  }
                />
              </div>

              <Link href="/sessions" className="home-highlight-cta">
                ランキングを見る →
              </Link>
            </>
          ) : (
            <p className="empty">この期間はまだハイライトがありません。</p>
          )}
        </section>
      </div>
    </div>
  );
}
