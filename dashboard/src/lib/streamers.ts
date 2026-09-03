import { getDb, getWritableDb } from "./db";

export type StreamerManagementRow = {
  id: number;
  name: string;
  tiktokAccountId: string;
  archived: boolean;
  archivedAt: string | null;
  /** 一時的に巡回・録画の対象から外しているか。**archived とは別の軸**で、
      archived=true のときは意味を持たない(アーカイブが優先)。 */
  enabled: boolean;
  createdAt: string;
  sessionCount: number;
  lastSessionAt: string | null;
  avatarPath: string | null;
};

// Unlike getStreamerList (the public roster, archived excluded), this is
// the 設定/ライバー管理 view -- it deliberately shows every streamer,
// archived or not, so admins can find and restore one.
export function listStreamersForManagement(): StreamerManagementRow[] {
  const db = getDb();
  const rows = db
    .prepare(
      `SELECT
        s.id as id,
        s.name as name,
        s.tiktok_account_id as tiktokAccountId,
        s.archived as archived,
        s.archived_at as archivedAt,
        s.enabled as enabled,
        s.created_at as createdAt,
        s.avatar_path as avatarPath,
        (SELECT COUNT(*) FROM live_sessions WHERE streamer_id = s.id) as sessionCount,
        (SELECT MAX(started_at) FROM live_sessions WHERE streamer_id = s.id) as lastSessionAt
      FROM streamers s
      ORDER BY s.archived ASC, s.enabled DESC, s.name ASC`
    )
    .all() as (Omit<StreamerManagementRow, "archived" | "enabled"> &
      { archived: number; enabled: number })[];
  return rows.map((r) => ({
    ...r,
    archived: Boolean(r.archived),
    enabled: Boolean(r.enabled),
  }));
}

export class StreamerManagementError extends Error {}

// Mirrors tiktok_monitor/db.py's get_or_create_streamer's INSERT shape,
// but rejects re-adding an existing tiktok_account_id instead of silently
// returning the existing row -- for a management UI, "already registered"
// should be visible feedback, not a silent no-op.
export function addStreamer(tiktokAccountId: string, name: string): { id: number; tiktokAccountId: string } {
  const trimmed = tiktokAccountId.trim().replace(/^@/, "");
  if (!trimmed) {
    throw new StreamerManagementError("TikTokアカウントID(ユーザー名)を入力してください。");
  }

  const db = getWritableDb();
  const existing = db.prepare(`SELECT id FROM streamers WHERE tiktok_account_id = ?`).get(trimmed) as
    | { id: number }
    | undefined;
  if (existing) {
    throw new StreamerManagementError(`「${trimmed}」は既に登録されています。`);
  }

  const info = db
    .prepare(`INSERT INTO streamers (name, tiktok_account_id, created_at) VALUES (?, ?, ?)`)
    .run(name.trim() || trimmed, trimmed, new Date().toISOString());
  return { id: Number(info.lastInsertRowid), tiktokAccountId: trimmed };
}

// Logical delete only (design doc 7-1) -- never DELETEs the row, so every
// live_sessions/live_events row referencing this streamer stays intact and
// reachable by direct link. archived=false clears archived_at too, mirroring
// tiktok_monitor/db.py's unarchive_streamer.
/**
 * 一時的に巡回・録画の対象から外す/戻す。通常の一覧には表示され続ける。
 *
 * **アーカイブとは別の軸。** アーカイブは退所済みで専用タブへ分離するもの、
 * 無効は「今は録らないが在籍している」状態。過去データはどちらも保持する。
 */
export function setStreamerEnabled(id: number, enabled: boolean): void {
  const db = getWritableDb();
  const info = db.prepare(`UPDATE streamers SET enabled = ? WHERE id = ?`).run(enabled ? 1 : 0, id);
  if (info.changes === 0) throw new StreamerManagementError("そのライバーは見つかりません。");
}

export function setStreamerArchived(id: number, archived: boolean): void {
  const db = getWritableDb();
  db.prepare(`UPDATE streamers SET archived = ?, archived_at = ? WHERE id = ?`).run(
    archived ? 1 : 0,
    archived ? new Date().toISOString() : null,
    id
  );
}
