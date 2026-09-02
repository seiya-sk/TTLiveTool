import Link from "next/link";
import { notFound } from "next/navigation";
import {
  getBattleGroups,
  getBattleOpponents,
  getComments,
  getFollows,
  getGiftDetail,
  getGiftRanking,
  getJoins,
  getLatestReport,
  getScreenshots,
  getSession,
  getSessionStats,
  getTreasureBoxes,
  getViewerSeries,
  getCommentSeries,
  getGiftDiamondSeries,
} from "@/lib/queries";
import { avatarUrl, formatDuration, formatJst } from "@/lib/format";
import { Avatar } from "@/components/Avatar";
import { parseSessionOrigin, sessionBackTarget } from "@/lib/backLink";
import { StatusBadge, type BadgeTone } from "@/components/StatusBadge";
import Tabs, { type TabDef } from "@/components/Tabs";
import { AiReport } from "./AiReport";
import { CompositeChart } from "./CompositeChart";
import { KpiCards } from "./KpiCards";
import { SessionDetailTables } from "./SessionDetailTables";

// A session still marked 'live' keeps gaining events; always read fresh.
export const dynamic = "force-dynamic";

const STATUS_LABEL: Record<string, string> = {
  live: "配信中",
  ended: "終了",
  error: "エラー中断",
};

const STATUS_TONE: Record<string, BadgeTone> = {
  live: "pink",
  ended: "success",
  error: "warning",
};

export default async function SessionDetailPage(props: PageProps<"/sessions/[id]">) {
  const { id } = await props.params;
  const sessionId = Number(id);
  if (!Number.isInteger(sessionId)) notFound();

  const session = getSession(sessionId);
  if (!session) notFound();

  // 遷移元に応じて「戻る」先を変える。直接URLを開いた場合など不明なときは
  // 全件のライブ一覧に戻す(lib/backLink.ts の docstring を参照)。
  const searchParams = await props.searchParams;
  const backTarget = sessionBackTarget(parseSessionOrigin(searchParams.from), session);

  const viewerSeries = getViewerSeries(sessionId);
  const commentSeries = getCommentSeries(sessionId);
  const giftDiamondSeries = getGiftDiamondSeries(sessionId);
  const stats = getSessionStats(sessionId);
  const screenshots = getScreenshots(sessionId);
  const treasureBoxes = getTreasureBoxes(sessionId);

  const report = getLatestReport(sessionId);

  const battleOpponents = getBattleOpponents(sessionId);
  const battleGroups = getBattleGroups(sessionId);
  const giftRanking = getGiftRanking(sessionId);
  const giftDetail = getGiftDetail(sessionId);
  const joins = getJoins(sessionId);
  const follows = getFollows(sessionId);
  const comments = getComments(sessionId);

  const tabs: TabDef[] = [
    {
      key: "overview",
      label: "概要",
      accent: "pink",
      content: <KpiCards stats={stats} startedAt={session.startedAt} endedAt={session.endedAt} />,
    },
    {
      key: "trend",
      label: "配信推移",
      accent: "cyan",
      content: (
        <CompositeChart
          startedAt={session.startedAt}
          endedAt={session.endedAt}
          viewerSeries={viewerSeries}
          commentSeries={commentSeries}
          giftSeries={giftDiamondSeries}
          battleGroups={battleGroups}
          screenshots={screenshots}
          treasureBoxes={treasureBoxes}
          stats={stats}
        />
      ),
    },
    {
      key: "ai-report",
      label: "AI分析",
      accent: "purple",
      content: <AiReport report={report} sessionId={session.id} />,
    },
    {
      key: "detail",
      label: "詳細データ",
      content: (
        <SessionDetailTables
          battleOpponents={battleOpponents}
          giftRanking={giftRanking}
          giftDetail={giftDetail}
          joins={joins}
          follows={follows}
          comments={comments}
        />
      ),
    },
  ];

  return (
    <div className="container">
      <Link href={backTarget.href} className="back-link">
        ← {backTarget.label}
      </Link>
      <div className="detail-title-row">
        <Link href={`/streamers/${session.tiktokAccountId}`} className="detail-title-link">
          <Avatar name={session.streamerName} src={avatarUrl(session.avatarPath)} size={48} />
          <h1>{session.streamerName}</h1>
        </Link>
        <StatusBadge
          label={`${STATUS_LABEL[session.status] ?? session.status}${session.endDetectionType ? `(${session.endDetectionType})` : ""}`}
          tone={STATUS_TONE[session.status] ?? "muted"}
        />
      </div>
      <div className="session-header">
        <span>開始: {formatJst(session.startedAt)}</span>
        <span>終了: {formatJst(session.endedAt)}</span>
        <span>配信時間: {formatDuration(session.startedAt, session.endedAt)}</span>
      </div>

      <Tabs tabs={tabs} />
    </div>
  );
}
