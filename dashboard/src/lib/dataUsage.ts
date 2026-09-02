import fs from "node:fs";
import path from "node:path";
import { getDb, resolveDbPath, resolveScreenshotDir } from "./db";
import { basenameFromPath } from "./format";

// dbstat is a read-only virtual table SQLite (and better-sqlite3's build)
// exposes for exactly this purpose: SUM(pgsize) per table name gives the
// real on-disk byte count without reading any row content, so this stays
// fast even though live_event_raw_payloads alone is ~1GB (see db.py's
// live_events comment for why that table is split out in the first place).
// Still ~1.2s to scan the whole DB's page metadata though, so callers share
// one call via getDataUsageReport() below rather than each fetching it
// separately.
function getTableSizes(): Map<string, number> {
  const db = getDb();
  const rows = db.prepare(`SELECT name, SUM(pgsize) as bytes FROM dbstat GROUP BY name`).all() as {
    name: string;
    bytes: number;
  }[];
  return new Map(rows.map((r) => [r.name, r.bytes]));
}

function getDbFileBytes(): number {
  try {
    return fs.statSync(resolveDbPath()).size;
  } catch {
    return 0;
  }
}

function listScreenshotFiles(): { name: string; bytes: number }[] {
  const dir = resolveScreenshotDir();
  let names: string[];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  return names
    .filter((n) => n.toLowerCase().endsWith(".png"))
    .map((name) => {
      try {
        return { name, bytes: fs.statSync(path.join(dir, name)).size };
      } catch {
        return { name, bytes: 0 };
      }
    });
}

export type StorageBreakdownRow = { label: string; bytes: number };

export type StorageOverview = {
  dbFileBytes: number;
  screenshotsBytes: number;
  totalBytes: number;
  breakdown: StorageBreakdownRow[];
};

function buildOverview(tableSizes: Map<string, number>): StorageOverview {
  const dbFileBytes = getDbFileBytes();
  const screenshotsBytes = listScreenshotFiles().reduce((sum, f) => sum + f.bytes, 0);

  const rawPayloadBytes = tableSizes.get("live_event_raw_payloads") ?? 0;
  const eventsBytes = tableSizes.get("live_events") ?? 0;
  const reportsBytes = tableSizes.get("live_reports") ?? 0;

  let otherBytes = 0;
  for (const [name, bytes] of tableSizes) {
    if (name !== "live_event_raw_payloads" && name !== "live_events" && name !== "live_reports") {
      otherBytes += bytes;
    }
  }

  return {
    dbFileBytes,
    screenshotsBytes,
    totalBytes: dbFileBytes + screenshotsBytes,
    breakdown: [
      { label: "スクリーンショット画像", bytes: screenshotsBytes },
      { label: "生イベントデータ(raw_payload)", bytes: rawPayloadBytes },
      { label: "整形済みイベント(live_events)", bytes: eventsBytes },
      { label: "AIレポート(live_reports)", bytes: reportsBytes },
      { label: "その他(インデックス等)", bytes: otherBytes },
    ],
  };
}

export type SessionDataUsage = {
  sessionId: number;
  streamerId: number;
  streamerName: string;
  tiktokAccountId: string;
  startedAt: string;
  eventCount: number;
  eventsPayloadBytes: number;
  rawPayloadBytesEstimate: number;
  reportsBytes: number;
  screenshotsBytes: number;
  totalBytes: number;
};

// Per-session/streamer raw_payload size is an ESTIMATE, not an exact
// figure: dbstat only gives whole-table byte totals, and re-deriving exact
// per-session bytes would mean SUM(LENGTH(raw_payload)) over that session's
// rows -- exactly the "read every row's ~20-40KB blob" cost the raw_payload
// split exists to avoid (measured: ~12s for all 8 sessions vs ~20ms for
// this estimate). Spreading the table's real total evenly per row is a
// reasonable proxy since it's only used for "which session/streamer is
// heaviest" comparisons, not billing-grade precision.
function buildSessionUsage(tableSizes: Map<string, number>): SessionDataUsage[] {
  const db = getDb();

  const totalEventRows = (db.prepare(`SELECT COUNT(*) as c FROM live_events`).get() as { c: number }).c;
  const rawPayloadTotalBytes = tableSizes.get("live_event_raw_payloads") ?? 0;
  const avgRawPayloadBytesPerRow = totalEventRows > 0 ? rawPayloadTotalBytes / totalEventRows : 0;

  const rows = db
    .prepare(
      `SELECT
        ls.id as sessionId,
        s.id as streamerId,
        s.name as streamerName,
        s.tiktok_account_id as tiktokAccountId,
        ls.started_at as startedAt,
        (SELECT COUNT(*) FROM live_events WHERE live_session_id = ls.id) as eventCount,
        COALESCE((SELECT SUM(LENGTH(payload)) FROM live_events WHERE live_session_id = ls.id), 0) as eventsPayloadBytes,
        COALESCE(
          (SELECT SUM(LENGTH(summary_json) + LENGTH(recommendation_md)) FROM live_reports WHERE live_session_id = ls.id),
          0
        ) as reportsBytes
      FROM live_sessions ls
      JOIN streamers s ON s.id = ls.streamer_id
      ORDER BY ls.started_at DESC`
    )
    .all() as {
    sessionId: number;
    streamerId: number;
    streamerName: string;
    tiktokAccountId: string;
    startedAt: string;
    eventCount: number;
    eventsPayloadBytes: number;
    reportsBytes: number;
  }[];

  const screenshotRows = db
    .prepare(`SELECT live_session_id as sessionId, image_path as imagePath FROM live_screenshots`)
    .all() as { sessionId: number; imagePath: string }[];
  const screenshotDir = resolveScreenshotDir();
  const screenshotBytesBySession = new Map<number, number>();
  for (const s of screenshotRows) {
    let bytes = 0;
    try {
      bytes = fs.statSync(path.join(screenshotDir, basenameFromPath(s.imagePath))).size;
    } catch {
      // Screenshot file missing/moved -- don't let a stale DB row crash the page.
    }
    screenshotBytesBySession.set(s.sessionId, (screenshotBytesBySession.get(s.sessionId) ?? 0) + bytes);
  }

  return rows.map((r) => {
    const rawPayloadBytesEstimate = Math.round(r.eventCount * avgRawPayloadBytesPerRow);
    const screenshotsBytes = screenshotBytesBySession.get(r.sessionId) ?? 0;
    return {
      sessionId: r.sessionId,
      streamerId: r.streamerId,
      streamerName: r.streamerName,
      tiktokAccountId: r.tiktokAccountId,
      startedAt: r.startedAt,
      eventCount: r.eventCount,
      eventsPayloadBytes: r.eventsPayloadBytes,
      rawPayloadBytesEstimate,
      reportsBytes: r.reportsBytes,
      screenshotsBytes,
      totalBytes: r.eventsPayloadBytes + rawPayloadBytesEstimate + r.reportsBytes + screenshotsBytes,
    };
  });
}

export type StreamerDataUsage = {
  streamerId: number;
  streamerName: string;
  tiktokAccountId: string;
  eventCount: number;
  eventsPayloadBytes: number;
  rawPayloadBytesEstimate: number;
  reportsBytes: number;
  screenshotsBytes: number;
  totalBytes: number;
};

// Aggregated from sessionUsage in memory (not a separate query), so the
// per-session and per-streamer views can never drift apart.
function aggregateByStreamer(sessionUsage: SessionDataUsage[]): StreamerDataUsage[] {
  const map = new Map<number, StreamerDataUsage>();
  for (const s of sessionUsage) {
    const entry = map.get(s.streamerId) ?? {
      streamerId: s.streamerId,
      streamerName: s.streamerName,
      tiktokAccountId: s.tiktokAccountId,
      eventCount: 0,
      eventsPayloadBytes: 0,
      rawPayloadBytesEstimate: 0,
      reportsBytes: 0,
      screenshotsBytes: 0,
      totalBytes: 0,
    };
    entry.eventCount += s.eventCount;
    entry.eventsPayloadBytes += s.eventsPayloadBytes;
    entry.rawPayloadBytesEstimate += s.rawPayloadBytesEstimate;
    entry.reportsBytes += s.reportsBytes;
    entry.screenshotsBytes += s.screenshotsBytes;
    entry.totalBytes += s.totalBytes;
    map.set(s.streamerId, entry);
  }
  return Array.from(map.values());
}

export type DataUsageReport = {
  overview: StorageOverview;
  sessionUsage: SessionDataUsage[];
  streamerUsage: StreamerDataUsage[];
};

// Single entry point for 設定/データ管理 (docs/dashboard-navigation-design.md
// step 5): computes the whole-DB overview and the per-session/per-streamer
// breakdowns off exactly one dbstat scan, rather than each view paying that
// ~1.2s cost separately.
export function getDataUsageReport(): DataUsageReport {
  const tableSizes = getTableSizes();
  const overview = buildOverview(tableSizes);
  const sessionUsage = buildSessionUsage(tableSizes);
  const streamerUsage = aggregateByStreamer(sessionUsage);
  return { overview, sessionUsage, streamerUsage };
}
