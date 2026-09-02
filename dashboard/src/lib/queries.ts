import { getDb } from "./db";
import { groupBattleEvents, type BattleGroup } from "./battles";

export type SessionSummary = {
  id: number;
  streamerName: string;
  tiktokAccountId: string;
  title: string | null;
  startedAt: string;
  endedAt: string | null;
  status: string;
  endDetectionType: string | null;
  maxViewers: number | null;
  avgViewers: number | null;
  finalLikes: number | null;
};

export type SessionDetail = {
  id: number;
  streamerName: string;
  tiktokAccountId: string;
  title: string | null;
  startedAt: string;
  endedAt: string | null;
  status: string;
  endDetectionType: string | null;
  avatarPath: string | null;
};

export type ViewerBucketPoint = { minute: string; avgViewers: number };
export type BucketPoint = { minute: string; value: number };
export type BattleOpponentRow = { opponentId: string | null; occurredAt: string };
export type ScreenshotRow = { id: number; capturedAt: string; imagePath: string };
export type TreasureBoxRow = {
  occurredAt: string;
  senderNickname: string | null;
  coins: number | null;
  winnerHeadcount: number | null;
  openAt: number | null; // Unix seconds (envelope_info.unpack_at) -- not ISO8601, see format.ts's formatUnixSecondsJst
};
export type GiftRankingRow = {
  userId: string | null;
  nickname: string | null;
  gifterLevel: number | null;
  totalDiamonds: number;
};
export type GiftDetailRow = {
  occurredAt: string;
  nickname: string | null;
  gifterLevel: number | null;
  giftName: string | null;
  repeatCount: number | null;
  diamondCount: number | null;
};
export type JoinRow = {
  userId: string | null;
  nickname: string | null;
  gifterLevel: number | null;
  entryCount: number;
};
export type FollowRow = { occurredAt: string; nickname: string | null; gifterLevel: number | null };
export type CommentRow = {
  occurredAt: string;
  nickname: string | null;
  gifterLevel: number | null;
  memberLevel: number | null;
  comment: string | null;
};
// Mirrors tiktok_monitor/report/sections.py's REPORT_SECTIONS keys --
// next_stream_suggestions is the only array (a list of suggestion
// strings); the rest are freeform Markdown text. Kept loosely typed
// (unknown, not a strict union) since this is the AI's raw structured
// output straight from summary_json -- see report/render.py's
// summary_json = {..., sections: ai_sections, ...}.
export type ReportSections = {
  viewer_highlights?: string;
  comment_trends?: string;
  visual_feedback?: string;
  next_stream_suggestions?: string[];
};

export type ReportRow = {
  id: number;
  generatedAt: string;
  recommendationMd: string;
  // Parsed from summary_json -- null if a report predates this field being
  // read, or if summary_json didn't parse (never thrown; degrades to the
  // existing recommendationMd-only rendering instead).
  sections: ReportSections | null;
};

export type SessionStats = {
  avgViewers: number | null;
  maxViewers: number | null;
  totalUniqueViewers: number | null;
  totalDiamonds: number;
  commentCount: number;
  followCount: number;
  battleCount: number;
  uniqueVisitors: number;
  uniqueGifters: number;
  treasureBoxCount: number;
  totalTreasureBoxCoins: number;
};

// A streaking gift's repeat_count still climbs on every tick; only the
// final (streaking=false) event per combo carries the settled total, so
// summing anything else would double count. Mirrors GiftEvent.value in
// tiktok_monitor/events.py.
//
// Separately: TikTok sometimes delivers the SAME settled gift as multiple
// distinct webcast messages (confirmed on real data -- identical user,
// timestamp, gift, and diamond value, but a different common.msg_id each
// time), all sharing one log_id. A naive "GROUP BY log_id" fix is not safe
// alone though: also confirmed on real data, TikTok can *reuse* the same
// log_id for two genuinely separate gifts from the same user a few seconds
// apart (different diamond_count each). Grouping by (log_id, streaking,
// diamond_count, repeat_count) together handles both: true duplicates
// share all four and collapse to one row via MAX(); two
// reused-log_id-but-different-value gifts differ on diamond_count/
// repeat_count and land in separate groups, so both are still counted. id
// is the fallback grouping key for the never-observed-but-defend-anyway
// case where log_id is blank. Mirrors tiktok_monitor/report/data.py's
// _DEDUPED_GIFTS_SUBQUERY -- keep both in sync if this changes.
//
// log_id is read from `payload`, not raw_payload: events.py's
// normalize_gift promotes it into the curated payload specifically so
// gift queries never touch the separately-stored (and much larger)
// raw_payload table -- see tiktok_monitor/db.py's live_events comment for
// why that split exists (raw_payload averages 20-40KB/row and dragging it
// along made every query pay for reading it, even ones that never selected
// it, since SQLite's row storage isn't columnar).
const DEDUPED_GIFTS_SUBQUERY = `
  SELECT
    MAX(user_id) as user_id,
    MAX(user_nickname) as user_nickname,
    MAX(occurred_at) as occurred_at,
    MAX(CAST(json_extract(payload,'$.gifter_level') AS INTEGER)) as gifter_level,
    MAX(CASE WHEN json_extract(payload,'$.streaking') = 0
      THEN COALESCE(json_extract(payload,'$.diamond_count'),0) * COALESCE(json_extract(payload,'$.repeat_count'),1)
      ELSE 0 END) as diamond_value
  FROM live_events
  WHERE live_session_id = ? AND event_type = 'gift'
  GROUP BY
    COALESCE(NULLIF(json_extract(payload,'$.log_id'), ''), id),
    json_extract(payload,'$.streaking'),
    json_extract(payload,'$.diamond_count'),
    json_extract(payload,'$.repeat_count')
`;

export function listSessions(): SessionSummary[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        ls.id as id,
        s.name as streamerName,
        s.tiktok_account_id as tiktokAccountId,
        ls.title as title,
        ls.started_at as startedAt,
        ls.ended_at as endedAt,
        ls.status as status,
        ls.end_detection_type as endDetectionType,
        (SELECT MAX(CAST(json_extract(payload,'$.viewer_count') AS INTEGER))
           FROM live_events WHERE live_session_id = ls.id AND event_type = 'viewer_count') as maxViewers,
        (SELECT AVG(CAST(json_extract(payload,'$.viewer_count') AS INTEGER))
           FROM live_events WHERE live_session_id = ls.id AND event_type = 'viewer_count') as avgViewers,
        (SELECT CAST(json_extract(payload,'$.total_likes') AS INTEGER)
           FROM live_events WHERE live_session_id = ls.id AND event_type = 'like'
           ORDER BY occurred_at DESC LIMIT 1) as finalLikes
      FROM live_sessions ls
      JOIN streamers s ON s.id = ls.streamer_id
      ORDER BY ls.started_at DESC`
    )
    .all() as SessionSummary[];
}

export type SessionRankingRow = {
  id: number;
  streamerName: string;
  tiktokAccountId: string;
  startedAt: string;
  endedAt: string | null;
  totalDiamonds: number;
  newFollowers: number;
  maxViewers: number | null;
  avgViewers: number | null;
  avatarPath: string | null;
};

// Same dedup rule as DEDUPED_GIFTS_SUBQUERY (see its comment for why), just
// correlated against the outer ls.id instead of taking a `?` bind param --
// needed here because it runs once per session row rather than once for a
// single session.
const _DEDUPED_GIFTS_CORRELATED_SUBQUERY = `
  SELECT
    MAX(CASE WHEN json_extract(payload,'$.streaking') = 0
      THEN COALESCE(json_extract(payload,'$.diamond_count'),0) * COALESCE(json_extract(payload,'$.repeat_count'),1)
      ELSE 0 END) as diamond_value
  FROM live_events
  WHERE live_session_id = ls.id AND event_type = 'gift'
  GROUP BY
    COALESCE(NULLIF(json_extract(payload,'$.log_id'), ''), id),
    json_extract(payload,'$.streaking'),
    json_extract(payload,'$.diamond_count'),
    json_extract(payload,'$.repeat_count')
`;

// Backs the /sessions ranking tabs (ダイヤ/新規フォロワー/同接) -- one row
// shape covers all three; each tab just sorts/highlights a different column
// (see RankingsTables.tsx).
export function getSessionRankings(streamerId?: number): SessionRankingRow[] {
  const db = getDb();
  const where = streamerId ? "WHERE ls.streamer_id = ?" : "";
  const rows = db
    .prepare(
      `SELECT
        ls.id as id,
        s.name as streamerName,
        s.tiktok_account_id as tiktokAccountId,
        ls.started_at as startedAt,
        ls.ended_at as endedAt,
        COALESCE((SELECT SUM(diamond_value) FROM (${_DEDUPED_GIFTS_CORRELATED_SUBQUERY})), 0) as totalDiamonds,
        (SELECT COUNT(*) FROM live_events WHERE live_session_id = ls.id AND event_type = 'follow') as newFollowers,
        (SELECT MAX(CAST(json_extract(payload,'$.viewer_count') AS INTEGER))
           FROM live_events WHERE live_session_id = ls.id AND event_type = 'viewer_count') as maxViewers,
        (SELECT AVG(CAST(json_extract(payload,'$.viewer_count') AS INTEGER))
           FROM live_events WHERE live_session_id = ls.id AND event_type = 'viewer_count') as avgViewers,
        s.avatar_path as avatarPath
      FROM live_sessions ls
      JOIN streamers s ON s.id = ls.streamer_id
      ${where}
      ORDER BY ls.started_at DESC`
    )
    .all(...(streamerId ? [streamerId] : [])) as SessionRankingRow[];
  return rows;
}

export type StreamerRow = {
  id: number;
  name: string;
  tiktokAccountId: string;
  sessionCount: number;
  lastSessionAt: string | null;
  totalDiamonds: number;
  avatarPath: string | null;
};

// Same dedup rule as DEDUPED_GIFTS_SUBQUERY, correlated against the outer
// streamer id and joined across all of that streamer's sessions -- backs
// ライバー一覧's 累計ダイヤ column (docs/dashboard-navigation-design.md
// step 3).
const _DEDUPED_GIFTS_BY_STREAMER_SUBQUERY = `
  SELECT
    MAX(CASE WHEN json_extract(le.payload,'$.streaking') = 0
      THEN COALESCE(json_extract(le.payload,'$.diamond_count'),0) * COALESCE(json_extract(le.payload,'$.repeat_count'),1)
      ELSE 0 END) as diamond_value
  FROM live_events le
  JOIN live_sessions ls ON ls.id = le.live_session_id
  WHERE ls.streamer_id = s.id AND le.event_type = 'gift'
  GROUP BY
    COALESCE(NULLIF(json_extract(le.payload,'$.log_id'), ''), le.id),
    json_extract(le.payload,'$.streaking'),
    json_extract(le.payload,'$.diamond_count'),
    json_extract(le.payload,'$.repeat_count')
`;

// Archived streamers (設定/ライバー管理's logical delete) are excluded here
// -- this is the public roster, not the management view. Their past
// sessions stay fully reachable by direct link/URL; only the roster entry
// is hidden (design doc 7-1).
export function getStreamerList(): StreamerRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        s.id as id,
        s.name as name,
        s.tiktok_account_id as tiktokAccountId,
        (SELECT COUNT(*) FROM live_sessions WHERE streamer_id = s.id) as sessionCount,
        (SELECT MAX(started_at) FROM live_sessions WHERE streamer_id = s.id) as lastSessionAt,
        COALESCE((SELECT SUM(diamond_value) FROM (${_DEDUPED_GIFTS_BY_STREAMER_SUBQUERY})), 0) as totalDiamonds,
        s.avatar_path as avatarPath
      FROM streamers s
      WHERE s.archived = 0
      ORDER BY s.name`
    )
    .all() as StreamerRow[];
}

// Unlike getStreamerList, intentionally not filtered by archived -- ライバー
// 詳細's direct-link URL (/streamers/[accountId]) must keep working for an
// archived streamer the same way a session's URL does (design doc 7-1:
// archiving hides the roster entry, never the underlying data). Keyed by
// tiktok_account_id (not the internal numeric id) so the URL is something a
// human can recognize/type -- see StreamerDetailPage.
export function getStreamerByAccountId(tiktokAccountId: string): StreamerRow | undefined {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        s.id as id,
        s.name as name,
        s.tiktok_account_id as tiktokAccountId,
        (SELECT COUNT(*) FROM live_sessions WHERE streamer_id = s.id) as sessionCount,
        (SELECT MAX(started_at) FROM live_sessions WHERE streamer_id = s.id) as lastSessionAt,
        COALESCE((SELECT SUM(diamond_value) FROM (${_DEDUPED_GIFTS_BY_STREAMER_SUBQUERY})), 0) as totalDiamonds,
        s.avatar_path as avatarPath
      FROM streamers s
      WHERE s.tiktok_account_id = ?`
    )
    .get(tiktokAccountId) as StreamerRow | undefined;
}

export function getStreamerName(id: number): string | undefined {
  const db = getDb();
  const row = db.prepare(`SELECT name FROM streamers WHERE id = ?`).get(id) as { name: string } | undefined;
  return row?.name;
}

export function getSession(id: number): SessionDetail | undefined {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        ls.id as id,
        s.name as streamerName,
        s.tiktok_account_id as tiktokAccountId,
        ls.title as title,
        ls.started_at as startedAt,
        ls.ended_at as endedAt,
        ls.status as status,
        ls.end_detection_type as endDetectionType,
        s.avatar_path as avatarPath
      FROM live_sessions ls
      JOIN streamers s ON s.id = ls.streamer_id
      WHERE ls.id = ?`
    )
    .get(id) as SessionDetail | undefined;
}

// Long sessions can carry 10k+ raw viewer_count snapshots (session 9: 7hrs /
// 10,450 points); drawing every point makes the chart heavy and noisy, so
// this buckets to a per-minute average, mirroring report/data.py's
// _get_timeseries bucketing.
export function getViewerSeries(id: number): ViewerBucketPoint[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT strftime('%Y-%m-%dT%H:%M:00Z', occurred_at) as minute,
        CAST(ROUND(AVG(CAST(json_extract(payload,'$.viewer_count') AS INTEGER))) AS INTEGER) as avgViewers
       FROM live_events WHERE live_session_id = ? AND event_type = 'viewer_count'
       GROUP BY minute ORDER BY minute`
    )
    .all(id) as ViewerBucketPoint[];
}

export function getScreenshots(id: number): ScreenshotRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT id as id, captured_at as capturedAt, image_path as imagePath
       FROM live_screenshots WHERE live_session_id = ?
       ORDER BY captured_at`
    )
    .all(id) as ScreenshotRow[];
}

// Only rows classified as Treasure Box (not TikTok's unrelated Red
// Envelope feature) ever reach event_type='treasure_box' -- see
// tiktok_monitor/events.py's is_treasure_box_envelope.
export function getTreasureBoxes(id: number): TreasureBoxRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT occurred_at as occurredAt, user_nickname as senderNickname,
        CAST(json_extract(payload,'$.coins') AS INTEGER) as coins,
        CAST(json_extract(payload,'$.winner_headcount') AS INTEGER) as winnerHeadcount,
        CAST(json_extract(payload,'$.open_at') AS INTEGER) as openAt
       FROM live_events WHERE live_session_id = ? AND event_type = 'treasure_box'
       ORDER BY occurred_at`
    )
    .all(id) as TreasureBoxRow[];
}

export function getCommentSeries(id: number): BucketPoint[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT strftime('%Y-%m-%dT%H:%M:00Z', occurred_at) as minute, COUNT(*) as value
       FROM live_events WHERE live_session_id = ? AND event_type = 'comment'
       GROUP BY minute ORDER BY minute`
    )
    .all(id) as BucketPoint[];
}

export function getGiftDiamondSeries(id: number): BucketPoint[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT strftime('%Y-%m-%dT%H:%M:00Z', occurred_at) as minute, SUM(diamond_value) as value
       FROM (${DEDUPED_GIFTS_SUBQUERY})
       GROUP BY minute ORDER BY minute`
    )
    .all(id) as BucketPoint[];
}

export function getBattleOpponents(id: number): BattleOpponentRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT user_id as opponentId, occurred_at as occurredAt
       FROM live_events WHERE live_session_id = ? AND event_type = 'battle_opponent'
       ORDER BY occurred_at`
    )
    .all(id) as BattleOpponentRow[];
}

// battle_opponent fires repeatedly for the same ongoing battle, so raw rows
// wildly overstate battle count (session 9: 47 raw rows -> 14 actual
// battles). See lib/battles.ts's groupBattleEvents docstring for the
// grouping rule. Both the KPI card's battle count (getSessionStats) and the
// composite chart's battle lane use this, not the raw rows, so they always
// agree. The L3 "バトル相手" detail tab intentionally keeps showing raw
// detections -- that tab is the drill-down into the underlying events.
export function getBattleGroups(id: number): BattleGroup[] {
  return groupBattleEvents(getBattleOpponents(id));
}

export function getGiftRanking(id: number): GiftRankingRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT user_id as userId, user_nickname as nickname,
        MAX(gifter_level) as gifterLevel,
        SUM(diamond_value) as totalDiamonds
       FROM (${DEDUPED_GIFTS_SUBQUERY})
       GROUP BY user_id ORDER BY totalDiamonds DESC`
    )
    .all(id) as GiftRankingRow[];
}

export function getGiftDetail(id: number): GiftDetailRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT occurred_at as occurredAt, user_nickname as nickname,
        CAST(json_extract(payload,'$.gifter_level') AS INTEGER) as gifterLevel,
        json_extract(payload,'$.gift_name') as giftName,
        CAST(json_extract(payload,'$.repeat_count') AS INTEGER) as repeatCount,
        CAST(json_extract(payload,'$.diamond_count') AS INTEGER) as diamondCount
       FROM live_events WHERE live_session_id = ? AND event_type = 'gift'
       ORDER BY occurred_at DESC`
    )
    .all(id) as GiftDetailRow[];
}

export function getJoins(id: number): JoinRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT user_id as userId, user_nickname as nickname,
        MAX(CAST(json_extract(payload,'$.gifter_level') AS INTEGER)) as gifterLevel,
        COUNT(*) as entryCount
       FROM live_events WHERE live_session_id = ? AND event_type = 'room_enter'
       GROUP BY user_id ORDER BY entryCount DESC`
    )
    .all(id) as JoinRow[];
}

export function getFollows(id: number): FollowRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT occurred_at as occurredAt, user_nickname as nickname,
        CAST(json_extract(payload,'$.gifter_level') AS INTEGER) as gifterLevel
       FROM live_events WHERE live_session_id = ? AND event_type = 'follow'
       ORDER BY occurred_at DESC`
    )
    .all(id) as FollowRow[];
}

export function getComments(id: number): CommentRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT occurred_at as occurredAt, user_nickname as nickname,
        CAST(json_extract(payload,'$.gifter_level') AS INTEGER) as gifterLevel,
        CAST(json_extract(payload,'$.member_level') AS INTEGER) as memberLevel,
        json_extract(payload,'$.comment') as comment
       FROM live_events WHERE live_session_id = ? AND event_type = 'comment'
       ORDER BY occurred_at DESC`
    )
    .all(id) as CommentRow[];
}

// KPI-card aggregates in one place. total_diamonds reuses the same dedup
// subquery as getGiftDiamondSeries/getGiftRanking (see comment above
// DEDUPED_GIFTS_SUBQUERY) so the KPI card and the graph/ranking always agree.
export function getSessionStats(id: number): SessionStats {
  const db = getDb();
  const viewerRow = db
    .prepare(
      `SELECT
        AVG(CAST(json_extract(payload,'$.viewer_count') AS INTEGER)) as avgViewers,
        MAX(CAST(json_extract(payload,'$.viewer_count') AS INTEGER)) as maxViewers,
        MAX(CAST(json_extract(payload,'$.total_unique_viewers') AS INTEGER)) as totalUniqueViewers
       FROM live_events WHERE live_session_id = ? AND event_type = 'viewer_count'`
    )
    .get(id) as { avgViewers: number | null; maxViewers: number | null; totalUniqueViewers: number | null };

  const diamondsRow = db
    .prepare(`SELECT COALESCE(SUM(diamond_value), 0) as totalDiamonds FROM (${DEDUPED_GIFTS_SUBQUERY})`)
    .get(id) as { totalDiamonds: number };

  const gifterRow = db
    .prepare(`SELECT COUNT(DISTINCT user_id) as uniqueGifters FROM (${DEDUPED_GIFTS_SUBQUERY})`)
    .get(id) as { uniqueGifters: number };

  const countRow = db
    .prepare(
      `SELECT
        (SELECT COUNT(*) FROM live_events WHERE live_session_id = ? AND event_type = 'comment') as commentCount,
        (SELECT COUNT(*) FROM live_events WHERE live_session_id = ? AND event_type = 'follow') as followCount,
        (SELECT COUNT(DISTINCT user_id) FROM live_events WHERE live_session_id = ? AND event_type = 'room_enter') as uniqueVisitors`
    )
    .get(id, id, id) as {
    commentCount: number;
    followCount: number;
    uniqueVisitors: number;
  };

  const treasureBoxRow = db
    .prepare(
      `SELECT
        COUNT(*) as treasureBoxCount,
        COALESCE(SUM(CAST(json_extract(payload,'$.coins') AS INTEGER)), 0) as totalTreasureBoxCoins
       FROM live_events WHERE live_session_id = ? AND event_type = 'treasure_box'`
    )
    .get(id) as { treasureBoxCount: number; totalTreasureBoxCoins: number };

  return {
    avgViewers: viewerRow.avgViewers,
    maxViewers: viewerRow.maxViewers,
    totalUniqueViewers: viewerRow.totalUniqueViewers,
    totalDiamonds: diamondsRow.totalDiamonds,
    commentCount: countRow.commentCount,
    followCount: countRow.followCount,
    battleCount: getBattleGroups(id).length,
    uniqueVisitors: countRow.uniqueVisitors,
    uniqueGifters: gifterRow.uniqueGifters,
    treasureBoxCount: treasureBoxRow.treasureBoxCount,
    totalTreasureBoxCoins: treasureBoxRow.totalTreasureBoxCoins,
  };
}

export function getLatestReport(id: number): ReportRow | undefined {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT id as id, generated_at as generatedAt, recommendation_md as recommendationMd, summary_json as summaryJson
       FROM live_reports WHERE live_session_id = ?
       ORDER BY generated_at DESC LIMIT 1`
    )
    .get(id) as (Omit<ReportRow, "sections"> & { summaryJson: string }) | undefined;
  if (!row) return undefined;

  let sections: ReportSections | null = null;
  try {
    sections = JSON.parse(row.summaryJson)?.sections ?? null;
  } catch {
    // A malformed summary_json shouldn't take down the page -- the existing
    // recommendationMd rendering still works without the structured cards.
  }
  return { id: row.id, generatedAt: row.generatedAt, recommendationMd: row.recommendationMd, sections };
}

// --- Home page summary -----------------------------------------------

export function countStreamers(): number {
  const db = getDb();
  return (db.prepare(`SELECT COUNT(*) as c FROM streamers WHERE archived = 0`).get() as { c: number }).c;
}

export function countSessions(): number {
  const db = getDb();
  return (db.prepare(`SELECT COUNT(*) as c FROM live_sessions`).get() as { c: number }).c;
}

// Same dedup rule as DEDUPED_GIFTS_SUBQUERY (see its comment), scoped to
// this JST calendar month's gift events across every streamer/session --
// backs the home page's 今月のダイヤ card. Not correlated/joined to a
// specific session like the other dedup variants since this deliberately
// spans all of them for the month.
const _DEDUPED_GIFTS_THIS_MONTH_SUBQUERY = `
  SELECT
    MAX(CASE WHEN json_extract(payload,'$.streaking') = 0
      THEN COALESCE(json_extract(payload,'$.diamond_count'),0) * COALESCE(json_extract(payload,'$.repeat_count'),1)
      ELSE 0 END) as diamond_value
  FROM live_events
  WHERE event_type = 'gift'
    AND strftime('%Y-%m', occurred_at, '+9 hours') = strftime('%Y-%m', 'now', '+9 hours')
  GROUP BY
    COALESCE(NULLIF(json_extract(payload,'$.log_id'), ''), id),
    json_extract(payload,'$.streaking'),
    json_extract(payload,'$.diamond_count'),
    json_extract(payload,'$.repeat_count')
`;

export function getMonthlyDiamonds(): number {
  const db = getDb();
  const row = db
    .prepare(`SELECT COALESCE(SUM(diamond_value), 0) as total FROM (${_DEDUPED_GIFTS_THIS_MONTH_SUBQUERY})`)
    .get() as { total: number };
  return row.total;
}

// --- New home-page overview (2026 dark/neon redesign) ----------------------
//
// Additive only -- none of the functions above are touched. Everything
// below is scoped to an arbitrary JST calendar month ("YYYY-MM") instead of
// hardcoding "this month" like getMonthlyDiamonds, so the home page's period
// selector can move between months. Diamonds are scoped by event
// occurred_at's JST month (mirrors _DEDUPED_GIFTS_THIS_MONTH_SUBQUERY's own
// convention above) -- a session that started in the prior month but
// received a gift after the month rolled over would count that gift in the
// later month, consistent with how getMonthlyDiamonds already behaves.

// This JST calendar month as "YYYY-MM", e.g. "2026-08".
export function getCurrentJstMonth(): string {
  const db = getDb();
  const row = db.prepare(`SELECT strftime('%Y-%m', 'now', '+9 hours') as month`).get() as { month: string };
  return row.month;
}

// Exported so page-level code can bucket already-fetched rows (e.g.
// listSessions()) by JST month without a new query -- see streamers/page.tsx.
export function shiftJstMonth(month: string, delta: number): string {
  const [y, m] = month.split("-").map(Number);
  const total = y * 12 + (m - 1) + delta;
  const year = Math.floor(total / 12);
  const monthIndex = ((total % 12) + 12) % 12;
  return `${year}-${String(monthIndex + 1).padStart(2, "0")}`;
}

const _DEDUPED_GIFTS_FOR_MONTH_SUBQUERY = `
  SELECT
    MAX(CASE WHEN json_extract(payload,'$.streaking') = 0
      THEN COALESCE(json_extract(payload,'$.diamond_count'),0) * COALESCE(json_extract(payload,'$.repeat_count'),1)
      ELSE 0 END) as diamond_value
  FROM live_events
  WHERE event_type = 'gift' AND strftime('%Y-%m', occurred_at, '+9 hours') = ?
  GROUP BY
    COALESCE(NULLIF(json_extract(payload,'$.log_id'), ''), id),
    json_extract(payload,'$.streaking'),
    json_extract(payload,'$.diamond_count'),
    json_extract(payload,'$.repeat_count')
`;

function _diamondsForMonth(month: string): number {
  const db = getDb();
  const row = db
    .prepare(`SELECT COALESCE(SUM(diamond_value), 0) as total FROM (${_DEDUPED_GIFTS_FOR_MONTH_SUBQUERY})`)
    .get(month) as { total: number };
  return row.total;
}

// Per-session average first, then averaged across sessions, so a
// short-but-densely-sampled session doesn't outweigh a long one.
function _avgConcurrentViewersForMonth(month: string): number | null {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT AVG(session_avg) as avgViewers FROM (
        SELECT AVG(CAST(json_extract(le.payload,'$.viewer_count') AS INTEGER)) as session_avg
        FROM live_events le
        WHERE le.event_type = 'viewer_count' AND strftime('%Y-%m', le.occurred_at, '+9 hours') = ?
        GROUP BY le.live_session_id
      )`
    )
    .get(month) as { avgViewers: number | null };
  return row.avgViewers === null ? null : Math.round(row.avgViewers);
}

function _totalCommentsForMonth(month: string): number {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT COUNT(*) as c FROM live_events
       WHERE event_type = 'comment' AND strftime('%Y-%m', occurred_at, '+9 hours') = ?`
    )
    .get(month) as { c: number };
  return row.c;
}

export type TopSessionOfMonth = {
  sessionId: number;
  streamerName: string;
  totalDiamonds: number;
  percentOfMonthTotal: number;
};

function _topSessionForMonth(month: string, monthTotalDiamonds: number): TopSessionOfMonth | null {
  const db = getDb();
  const row = db
    .prepare(
      `SELECT g.live_session_id as sessionId, s.name as streamerName,
          COALESCE(SUM(g.diamond_value), 0) as totalDiamonds
        FROM (
          SELECT le.live_session_id as live_session_id,
            MAX(CASE WHEN json_extract(le.payload,'$.streaking') = 0
              THEN COALESCE(json_extract(le.payload,'$.diamond_count'),0) * COALESCE(json_extract(le.payload,'$.repeat_count'),1)
              ELSE 0 END) as diamond_value
          FROM live_events le
          WHERE le.event_type = 'gift' AND strftime('%Y-%m', le.occurred_at, '+9 hours') = ?
          GROUP BY le.live_session_id,
            COALESCE(NULLIF(json_extract(le.payload,'$.log_id'), ''), le.id),
            json_extract(le.payload,'$.streaking'),
            json_extract(le.payload,'$.diamond_count'),
            json_extract(le.payload,'$.repeat_count')
        ) g
        JOIN live_sessions ls ON ls.id = g.live_session_id
        JOIN streamers s ON s.id = ls.streamer_id
        GROUP BY g.live_session_id
        ORDER BY totalDiamonds DESC
        LIMIT 1`
    )
    .get(month) as { sessionId: number; streamerName: string; totalDiamonds: number } | undefined;
  if (!row || row.totalDiamonds <= 0) return null;
  return {
    ...row,
    percentOfMonthTotal: monthTotalDiamonds > 0 ? (row.totalDiamonds / monthTotalDiamonds) * 100 : 0,
  };
}

// null means "no data last month to compare against" -- the UI should omit
// the change indicator rather than showing a misleading "+∞%"/"-100%".
function _changePercent(current: number, previous: number): number | null {
  if (previous <= 0) return null;
  return ((current - previous) / previous) * 100;
}

function _sessionCountForMonth(month: string): number {
  const db = getDb();
  const row = db
    .prepare(`SELECT COUNT(*) as c FROM live_sessions WHERE strftime('%Y-%m', started_at, '+9 hours') = ?`)
    .get(month) as { c: number };
  return row.c;
}

export type MonthlyOverview = {
  month: string;
  sessionCount: number;
  totalDiamonds: number;
  diamondsChangePercent: number | null;
  avgConcurrentViewers: number | null;
  avgConcurrentChangePercent: number | null;
  totalComments: number;
  totalCommentsChangePercent: number | null;
  topSession: TopSessionOfMonth | null;
};

// Backs the home page's big 今月の獲得ダイヤ card and 今月のハイライト panel.
// month defaults to the current JST month; pass an explicit "YYYY-MM" for
// the period selector.
export function getMonthlyOverview(month?: string): MonthlyOverview {
  const targetMonth = month ?? getCurrentJstMonth();
  const previousMonth = shiftJstMonth(targetMonth, -1);

  const totalDiamonds = _diamondsForMonth(targetMonth);
  const previousDiamonds = _diamondsForMonth(previousMonth);

  const avgConcurrentViewers = _avgConcurrentViewersForMonth(targetMonth);
  const previousAvgConcurrent = _avgConcurrentViewersForMonth(previousMonth);

  const totalComments = _totalCommentsForMonth(targetMonth);
  const previousComments = _totalCommentsForMonth(previousMonth);

  return {
    month: targetMonth,
    sessionCount: _sessionCountForMonth(targetMonth),
    totalDiamonds,
    diamondsChangePercent: _changePercent(totalDiamonds, previousDiamonds),
    avgConcurrentViewers,
    avgConcurrentChangePercent:
      avgConcurrentViewers !== null && previousAvgConcurrent !== null
        ? _changePercent(avgConcurrentViewers, previousAvgConcurrent)
        : null,
    totalComments,
    totalCommentsChangePercent: _changePercent(totalComments, previousComments),
    topSession: _topSessionForMonth(targetMonth, totalDiamonds),
  };
}

export type TokenUsageSummary = { inputTokens: number; outputTokens: number };

// live_reports.summary_json.usage already holds input_tokens/output_tokens
// per generated report (tiktok_monitor/report/claude.py) -- no new data
// collection needed, just parsing (see docs/dashboard-navigation-design.md
// 5-1). Shared by the home page's monthly figure and 設定/トークン管理's
// full per-report breakdown so both agree on what "usage" means for a row.
function _parseUsage(summaryJson: string): TokenUsageSummary {
  try {
    const usage = JSON.parse(summaryJson)?.usage;
    if (usage) {
      return {
        inputTokens: Number(usage.input_tokens) || 0,
        outputTokens: Number(usage.output_tokens) || 0,
      };
    }
  } catch {
    // A malformed summary_json row shouldn't take down the whole page --
    // treat it as 0 usage rather than throwing.
  }
  return { inputTokens: 0, outputTokens: 0 };
}

// This JST calendar month's worth, for the home page's KPI card.
export function getMonthlyTokenUsage(): TokenUsageSummary {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT summary_json FROM live_reports
       WHERE strftime('%Y-%m', generated_at, '+9 hours') = strftime('%Y-%m', 'now', '+9 hours')`
    )
    .all() as { summary_json: string }[];

  return rows.reduce(
    (acc, row) => {
      const usage = _parseUsage(row.summary_json);
      acc.inputTokens += usage.inputTokens;
      acc.outputTokens += usage.outputTokens;
      return acc;
    },
    { inputTokens: 0, outputTokens: 0 }
  );
}

export type ReportUsageRow = {
  reportId: number;
  sessionId: number;
  streamerId: number;
  streamerName: string;
  tiktokAccountId: string;
  generatedAt: string;
  sessionStartedAt: string;
  inputTokens: number;
  outputTokens: number;
  avatarPath: string | null;
};

// One row per generated report, every report, un-filtered -- 設定/トーク
// ン管理 does period/streamer/session filtering and aggregation entirely
// client-side (report counts here are small; see TokenManagement.tsx)
// rather than re-querying per filter change.
export function getAllReportUsage(): ReportUsageRow[] {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT
        lr.id as reportId,
        lr.live_session_id as sessionId,
        s.id as streamerId,
        s.name as streamerName,
        s.tiktok_account_id as tiktokAccountId,
        lr.generated_at as generatedAt,
        ls.started_at as sessionStartedAt,
        lr.summary_json as summaryJson,
        s.avatar_path as avatarPath
      FROM live_reports lr
      JOIN live_sessions ls ON ls.id = lr.live_session_id
      JOIN streamers s ON s.id = ls.streamer_id
      ORDER BY lr.generated_at DESC`
    )
    .all() as {
    reportId: number;
    sessionId: number;
    streamerId: number;
    streamerName: string;
    tiktokAccountId: string;
    generatedAt: string;
    sessionStartedAt: string;
    summaryJson: string;
    avatarPath: string | null;
  }[];

  return rows.map((r) => {
    const usage = _parseUsage(r.summaryJson);
    return {
      reportId: r.reportId,
      sessionId: r.sessionId,
      streamerId: r.streamerId,
      streamerName: r.streamerName,
      tiktokAccountId: r.tiktokAccountId,
      generatedAt: r.generatedAt,
      sessionStartedAt: r.sessionStartedAt,
      inputTokens: usage.inputTokens,
      outputTokens: usage.outputTokens,
      avatarPath: r.avatarPath,
    };
  });
}
