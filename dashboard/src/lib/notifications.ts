import { getDb, getWritableDb } from "./db";

// 進捗通知(系統2)のグループ設定。エラー通知(系統1)はここに出てこない --
// あちらはシステム異常専用で運用者だけが見るものなので、宛先は環境変数
// CHATWORK_ERROR_ROOM_ID 固定、UIにもDBにも設定を持たせない設計。

export type GroupStreamer = { id: number; name: string; tiktokAccountId: string };

export type NotificationGroup = {
  id: number;
  name: string;
  roomId: string;
  toAccountIds: string[];
  enabled: boolean;
  sendWhenEmpty: boolean;
  notifyStartHour: number;
  notifyEndHour: number;
  updatedAt: string;
  streamers: GroupStreamer[];
  lastSentAt: string | null;
  lastStatus: string | null;
  lastDetail: string | null;
};

export type GroupInput = {
  name: string;
  roomId: string;
  toAccountIds: string[];
  enabled: boolean;
  sendWhenEmpty: boolean;
  notifyStartHour: number;
  notifyEndHour: number;
};

export class NotificationSettingsError extends Error {}

function parseToAccountIds(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

export function listGroups(): NotificationGroup[] {
  const db = getDb();
  const groups = db
    .prepare(
      `SELECT id, name, room_id as roomId, to_account_ids as toRaw, enabled, send_when_empty as sendWhenEmpty,
              notify_start_hour as notifyStartHour, notify_end_hour as notifyEndHour, updated_at as updatedAt
       FROM notification_groups ORDER BY name`
    )
    .all() as {
    id: number; name: string; roomId: string; toRaw: string | null; enabled: number;
    sendWhenEmpty: number; notifyStartHour: number; notifyEndHour: number; updatedAt: string;
  }[];

  const members = db.prepare(
    `SELECT gs.group_id as groupId, s.id as id, s.name as name, s.tiktok_account_id as tiktokAccountId
     FROM notification_group_streamers gs
     JOIN streamers s ON s.id = gs.streamer_id
     ORDER BY s.name`
  ).all() as (GroupStreamer & { groupId: number })[];

  // 最新の送信結果(UIの「最終送信」表示用)。skipped_* も含めて直近1件を出す
  // -- 「送っていない」ことにも理由があり、それが見えないと設定ミスと
  // 平常運転の区別がつかないため。
  const last = db.prepare(
    `SELECT group_id as groupId, status, detail, sent_at as sentAt FROM notification_digest_log
     WHERE id IN (SELECT MAX(id) FROM notification_digest_log GROUP BY group_id)`
  ).all() as { groupId: number; status: string; detail: string | null; sentAt: string }[];

  return groups.map((g) => {
    const l = last.find((x) => x.groupId === g.id);
    return {
      id: g.id,
      name: g.name,
      roomId: g.roomId,
      toAccountIds: parseToAccountIds(g.toRaw),
      enabled: Boolean(g.enabled),
      sendWhenEmpty: Boolean(g.sendWhenEmpty),
      notifyStartHour: g.notifyStartHour,
      notifyEndHour: g.notifyEndHour,
      updatedAt: g.updatedAt,
      streamers: members.filter((m) => m.groupId === g.id).map(({ groupId: _g, ...rest }) => rest),
      lastSentAt: l?.sentAt ?? null,
      lastStatus: l?.status ?? null,
      lastDetail: l?.detail ?? null,
    };
  });
}

// 割り当て候補。archived は論理削除済みなので候補に出さない。
export function listAssignableStreamers(): GroupStreamer[] {
  return getDb()
    .prepare(
      `SELECT id, name, tiktok_account_id as tiktokAccountId FROM streamers
       WHERE archived = 0 ORDER BY name`
    )
    .all() as GroupStreamer[];
}

// どのグループにも属さないライバー。通知は出さず(運用管理の話であって
// システム異常ではないため)、UIでのみ見える化する。
export function listUnassignedStreamers(): GroupStreamer[] {
  return getDb()
    .prepare(
      `SELECT id, name, tiktok_account_id as tiktokAccountId FROM streamers
       WHERE archived = 0
         AND id NOT IN (SELECT streamer_id FROM notification_group_streamers)
       ORDER BY name`
    )
    .all() as GroupStreamer[];
}

function validate(input: GroupInput): GroupInput {
  const name = input.name?.trim() ?? "";
  const roomId = String(input.roomId ?? "").trim();
  if (!name) throw new NotificationSettingsError("グループ名を入力してください。");
  if (!roomId) throw new NotificationSettingsError("ルームIDを入力してください。");
  if (!/^\d+$/.test(roomId)) {
    throw new NotificationSettingsError("ルームIDは数字のみで入力してください(ルームURL末尾の数字)。");
  }
  const to = (input.toAccountIds ?? []).map((v) => String(v).trim()).filter(Boolean);
  if (to.some((v) => !/^\d+$/.test(v))) {
    throw new NotificationSettingsError("To(account_id)は数字のみで入力してください。");
  }
  const start = Number(input.notifyStartHour);
  const end = Number(input.notifyEndHour);
  for (const [label, v] of [["開始", start], ["終了", end]] as const) {
    if (!Number.isInteger(v) || v < 0 || v > 24) {
      throw new NotificationSettingsError(`通知時間帯の${label}時刻は0〜24の整数で指定してください。`);
    }
  }
  if (start === end) {
    throw new NotificationSettingsError("通知時間帯の開始と終了が同じです。24時間通知するなら 0〜24 を指定してください。");
  }
  return { ...input, name, roomId, toAccountIds: to, notifyStartHour: start, notifyEndHour: end };
}

export function createGroup(input: GroupInput): number {
  const v = validate(input);
  const db = getWritableDb();
  const now = new Date().toISOString();
  try {
    const info = db
      .prepare(
        `INSERT INTO notification_groups
           (name, room_id, to_account_ids, enabled, send_when_empty, notify_start_hour, notify_end_hour, created_at, updated_at)
         VALUES (?,?,?,?,?,?,?,?,?)`
      )
      .run(v.name, v.roomId, JSON.stringify(v.toAccountIds), v.enabled ? 1 : 0,
           v.sendWhenEmpty ? 1 : 0, v.notifyStartHour, v.notifyEndHour, now, now);
    return Number(info.lastInsertRowid);
  } catch (err) {
    if (err instanceof Error && err.message.includes("UNIQUE")) {
      throw new NotificationSettingsError(`「${v.name}」という名前のグループは既にあります。`);
    }
    throw err;
  }
}

export function updateGroup(id: number, input: GroupInput): void {
  const v = validate(input);
  const db = getWritableDb();
  try {
    const info = db
      .prepare(
        `UPDATE notification_groups SET name=?, room_id=?, to_account_ids=?, enabled=?,
           send_when_empty=?, notify_start_hour=?, notify_end_hour=?, updated_at=? WHERE id=?`
      )
      .run(v.name, v.roomId, JSON.stringify(v.toAccountIds), v.enabled ? 1 : 0,
           v.sendWhenEmpty ? 1 : 0, v.notifyStartHour, v.notifyEndHour, new Date().toISOString(), id);
    if (info.changes === 0) throw new NotificationSettingsError("対象のグループが見つかりません。");
  } catch (err) {
    if (err instanceof Error && err.message.includes("UNIQUE")) {
      throw new NotificationSettingsError(`「${v.name}」という名前のグループは既にあります。`);
    }
    throw err;
  }
}

export function deleteGroup(id: number): void {
  const db = getWritableDb();
  // 割り当ては ON DELETE CASCADE で消える(db.ts が foreign_keys = ON を
  // 立てている)。digest_log には外部キーを張っていないので、送信履歴は
  // グループを消しても監査用に残す -- ここで消すと「いつ何を送ったか」が
  // 追えなくなる。
  db.prepare(`DELETE FROM notification_groups WHERE id = ?`).run(id);
}

export function setGroupStreamers(groupId: number, streamerIds: number[]): void {
  const db = getWritableDb();
  const exists = db.prepare(`SELECT 1 FROM notification_groups WHERE id = ?`).get(groupId);
  if (!exists) throw new NotificationSettingsError("対象のグループが見つかりません。");

  const now = new Date().toISOString();
  const replace = db.transaction((ids: number[]) => {
    db.prepare(`DELETE FROM notification_group_streamers WHERE group_id = ?`).run(groupId);
    const insert = db.prepare(
      `INSERT OR IGNORE INTO notification_group_streamers (group_id, streamer_id, created_at) VALUES (?,?,?)`
    );
    for (const sid of ids) insert.run(groupId, sid, now);
  });
  replace(streamerIds.filter((n) => Number.isInteger(n)));
}
