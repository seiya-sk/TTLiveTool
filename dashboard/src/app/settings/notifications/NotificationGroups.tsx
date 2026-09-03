"use client";

import { useState } from "react";
import { formatJst } from "@/lib/format";
import type { GroupStreamer, NotificationGroup } from "@/lib/notifications";
import styles from "./NotificationGroups.module.css";

type Draft = {
  name: string; roomId: string; to: string;
  enabled: boolean; sendWhenEmpty: boolean;
  notifyStartHour: string; notifyEndHour: string;
};

const BLANK: Draft = {
  name: "", roomId: "", to: "", enabled: true, sendWhenEmpty: false,
  notifyStartHour: "9", notifyEndHour: "24",
};

function toDraft(g: NotificationGroup): Draft {
  return {
    name: g.name, roomId: g.roomId, to: g.toAccountIds.join(", "),
    enabled: g.enabled, sendWhenEmpty: g.sendWhenEmpty,
    notifyStartHour: String(g.notifyStartHour), notifyEndHour: String(g.notifyEndHour),
  };
}

function draftBody(d: Draft) {
  return {
    name: d.name,
    roomId: d.roomId,
    toAccountIds: d.to.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean),
    enabled: d.enabled,
    sendWhenEmpty: d.sendWhenEmpty,
    notifyStartHour: Number(d.notifyStartHour),
    notifyEndHour: Number(d.notifyEndHour),
  };
}

const STATUS_LABEL: Record<string, string> = {
  sent: "送信済み",
  skipped_empty: "配信なしのため送信なし",
  skipped_quiet_hours: "通知時間帯外のため送信なし",
  failed: "送信失敗",
};

export function NotificationGroups({
  initialGroups, assignable, initialUnassigned,
}: {
  initialGroups: NotificationGroup[];
  assignable: GroupStreamer[];
  initialUnassigned: GroupStreamer[];
}) {
  const [groups, setGroups] = useState(initialGroups);
  const [unassigned, setUnassigned] = useState(initialUnassigned);
  const [creating, setCreating] = useState(false);
  const [newDraft, setNewDraft] = useState<Draft>(BLANK);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState<Draft>(BLANK);
  const [pickerId, setPickerId] = useState<number | null>(null);
  const [picked, setPicked] = useState<number[]>([]);
  // ライバー名の絞り込み。ピッカーは同時に1グループしか開かないので状態は1つでよいが、
  // グループを切り替えたら必ず空に戻す(前のグループの絞り込みが残ると、
  // 「表示中を全選択」が別グループの意図で押される)。
  const [pickerSearch, setPickerSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function call(url: string, init: RequestInit): Promise<void> {
    setBusy(true); setError(null); setNotice(null);
    try {
      const res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...init });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "操作に失敗しました");
      if (Array.isArray(data)) {
        const next = data as NotificationGroup[];
        setGroups(next);
        const assignedIds = new Set(next.flatMap((g) => g.streamers.map((s) => s.id)));
        setUnassigned(assignable.filter((s) => !assignedIds.has(s.id)));
      } else if (data.message) {
        setNotice(data.message);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.wrap}>
      {error && <p className={styles.error}>{error}</p>}
      {notice && <p className={styles.ok}>{notice}</p>}

      <div className={styles.cardHead}>
        <div />
        <button className={`${styles.btn} ${styles.btnPrimary}`} disabled={busy}
                onClick={() => { setCreating((v) => !v); setNewDraft(BLANK); }}>
          {creating ? "キャンセル" : "＋ グループを作成"}
        </button>
      </div>

      {creating && (
        <div className={styles.card}>
          <div className={styles.cardTitle}>新しいグループ</div>
          <DraftForm draft={newDraft} onChange={setNewDraft} />
          <div className={styles.actions} style={{ marginTop: 14 }}>
            <button className={`${styles.btn} ${styles.btnPrimary}`} disabled={busy}
                    onClick={async () => {
                      await call("/api/notifications/groups",
                        { method: "POST", body: JSON.stringify(draftBody(newDraft)) });
                      setCreating(false);
                    }}>
              作成
            </button>
          </div>
        </div>
      )}

      {groups.length === 0 && !creating && (
        <p className="empty">グループがまだありません。「＋ グループを作成」から追加してください。</p>
      )}

      {groups.map((g) => (
        <div key={g.id} className={styles.card}>
          <div className={styles.cardHead}>
            <div className={styles.cardTitle}>
              {g.name}
              <span className={`${styles.badge} ${g.enabled ? styles.badgeOn : styles.badgeOff}`}>
                {g.enabled ? "有効" : "無効"}
              </span>
            </div>
            <div className={styles.actions}>
              <button className={styles.btn} disabled={busy}
                      onClick={() => {
                        setEditingId(editingId === g.id ? null : g.id);
                        setEditDraft(toDraft(g)); setPickerId(null); setPickerSearch("");
                      }}>
                {editingId === g.id ? "閉じる" : "編集"}
              </button>
              <button className={styles.btn} disabled={busy}
                      onClick={() => {
                        setPickerSearch("");
                        setPickerId(pickerId === g.id ? null : g.id);
                        setPicked(g.streamers.map((s) => s.id)); setEditingId(null);
                      }}>
                {pickerId === g.id ? "閉じる" : "割り当て変更"}
              </button>
              <button className={styles.btn} disabled={busy}
                      onClick={() => call(`/api/notifications/groups/${g.id}/test`, { method: "POST" })}>
                テスト送信
              </button>
              <button className={`${styles.btn} ${styles.btnDanger}`} disabled={busy}
                      onClick={() => {
                        if (!confirm(`グループ「${g.name}」を削除します。よろしいですか?\n(送信履歴は監査のため残ります)`)) return;
                        call(`/api/notifications/groups/${g.id}`, { method: "DELETE" });
                      }}>
                削除
              </button>
            </div>
          </div>

          <div className={styles.meta}>
            <div className={styles.metaItem}>ルームID<b>{g.roomId}</b></div>
            <div className={styles.metaItem}>To<b>{g.toAccountIds.join(", ") || "(なし)"}</b></div>
            <div className={styles.metaItem}>
              通知時間帯<b>{g.notifyStartHour}:00 – {g.notifyEndHour}:00 JST</b>
            </div>
            <div className={styles.metaItem}>
              配信が無い時間帯<b>{g.sendWhenEmpty ? "送る" : "送らない"}</b>
            </div>
            <div className={styles.metaItem}>
              最終送信
              <b>
                {g.lastSentAt
                  ? `${formatJst(g.lastSentAt)}　${STATUS_LABEL[g.lastStatus ?? ""] ?? g.lastStatus}${g.lastDetail ? `（${g.lastDetail}）` : ""}`
                  : "―"}
              </b>
            </div>
          </div>

          <div className={styles.members}>
            ライバー {g.streamers.length}人
            <div className={styles.chips}>
              {g.streamers.length === 0
                ? <span className={styles.chip}>(未割り当て)</span>
                : g.streamers.map((s) => <span key={s.id} className={styles.chip}>{s.name}</span>)}
            </div>
          </div>

          {editingId === g.id && (
            <>
              <DraftForm draft={editDraft} onChange={setEditDraft} />
              <div className={styles.actions} style={{ marginTop: 14 }}>
                <button className={`${styles.btn} ${styles.btnPrimary}`} disabled={busy}
                        onClick={async () => {
                          await call(`/api/notifications/groups/${g.id}`,
                            { method: "POST", body: JSON.stringify(draftBody(editDraft)) });
                          setEditingId(null);
                        }}>
                  保存
                </button>
              </div>
            </>
          )}

          {pickerId === g.id && (
            <div className={styles.picker}>
              <div style={{ fontSize: 13, color: "var(--muted)" }}>
                このグループに含めるライバー(アーカイブ済みは表示されません)
              </div>
              <PickerControls
                assignable={assignable}
                picked={picked}
                setPicked={setPicked}
                search={pickerSearch}
                setSearch={setPickerSearch}
              />
              <div className={styles.pickerGrid}>
                {filterStreamers(assignable, pickerSearch).map((s) => (
                  <label key={s.id} className={styles.pickerItem}>
                    <input type="checkbox" checked={picked.includes(s.id)}
                           onChange={(e) =>
                             setPicked((prev) =>
                               e.target.checked ? [...prev, s.id] : prev.filter((x) => x !== s.id))} />
                    {s.name}
                  </label>
                ))}
              </div>
              {filterStreamers(assignable, pickerSearch).length === 0 && (
                <p className="empty" style={{ fontSize: 13 }}>
                  「{pickerSearch}」に一致するライバーがいません。
                </p>
              )}
              <div className={styles.actions} style={{ marginTop: 14 }}>
                <button className={`${styles.btn} ${styles.btnPrimary}`} disabled={busy}
                        onClick={async () => {
                          await call(`/api/notifications/groups/${g.id}/streamers`,
                            { method: "PUT", body: JSON.stringify({ streamerIds: picked }) });
                          setPickerId(null);
                          setPickerSearch("");
                        }}>
                  割り当てを保存
                </button>
              </div>
            </div>
          )}
        </div>
      ))}

      <div className={`${styles.card} ${unassigned.length ? styles.unassigned : ""}`}>
        <div className={styles.unassignedHead} style={unassigned.length ? undefined : { color: "var(--muted)" }}>
          未割り当てのライバー {unassigned.length}名
        </div>
        <div className={styles.members}>
          {unassigned.length === 0
            ? "すべてのライバーがいずれかのグループに割り当てられています。"
            : "以下のライバーはどのグループにも属していないため、進捗通知に含まれません。"}
          {unassigned.length > 0 && (
            <div className={styles.chips}>
              {unassigned.map((s) => <span key={s.id} className={styles.chip}>{s.name}</span>)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** ライバー名での絞り込み。大文字小文字を無視し、部分一致で1文字ずつ効かせる。 */
export function filterStreamers<T extends { name: string }>(list: T[], search: string): T[] {
  const needle = search.trim().toLowerCase();
  if (!needle) return list;
  return list.filter((s) => s.name.toLowerCase().includes(needle));
}

/**
 * 一括操作。日常の小さな変更をUI上で完結させるためのもので、
 * CSVインポート/エクスポートとは用途が別。
 *
 * 「全部」と「表示中」を別のボタンに分けているのが要点。絞り込んでいる最中に
 * 「全選択」を押したら、画面に出ていない人まで選ばれる -- それが欲しい場面も
 * あるが、事故にもなる。絞り込み中だけ「表示中を…」を出し、どちらを操作して
 * いるかをラベルで明示する。
 */
function PickerControls({
  assignable, picked, setPicked, search, setSearch,
}: {
  assignable: GroupStreamer[];
  picked: number[];
  setPicked: (fn: (prev: number[]) => number[]) => void;
  search: string;
  setSearch: (v: string) => void;
}) {
  const shown = filterStreamers(assignable, search);
  const filtering = search.trim().length > 0;
  const shownIds = shown.map((s) => s.id);
  const shownSelected = shownIds.filter((id) => picked.includes(id)).length;

  return (
    <div className={styles.pickerControls}>
      <input
        type="search"
        className={styles.pickerSearch}
        placeholder="ライバー名で絞り込み"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        aria-label="ライバー名で絞り込み"
      />
      <button type="button" className={styles.btn}
              onClick={() => setPicked(() => assignable.map((s) => s.id))}>
        全選択（{assignable.length}人）
      </button>
      <button type="button" className={styles.btn}
              onClick={() => setPicked(() => [])}>
        全解除
      </button>
      {filtering && (
        <>
          <button type="button" className={styles.btn} disabled={shown.length === 0}
                  onClick={() => setPicked((prev) => [...new Set([...prev, ...shownIds])])}>
            表示中を全選択（{shown.length}人）
          </button>
          <button type="button" className={styles.btn} disabled={shown.length === 0}
                  onClick={() => setPicked((prev) => prev.filter((id) => !shownIds.includes(id)))}>
            表示中を全解除
          </button>
        </>
      )}
      <span className={styles.pickerCount}>
        選択 {picked.length} / {assignable.length}人
        {filtering && `（表示中 ${shownSelected} / ${shown.length}人）`}
      </span>
    </div>
  );
}


function DraftForm({ draft, onChange }: { draft: Draft; onChange: (d: Draft) => void }) {
  const set = (patch: Partial<Draft>) => onChange({ ...draft, ...patch });
  return (
    <div className={styles.form}>
      <div className={styles.field}>
        <label>グループ名(事務所/チーム)</label>
        <input type="text" value={draft.name} onChange={(e) => set({ name: e.target.value })}
               placeholder="〇〇事務所 Aチーム" />
      </div>
      <div className={styles.field}>
        <label>ルームID(ルームURL末尾の数字)</label>
        <input type="text" value={draft.roomId} onChange={(e) => set({ roomId: e.target.value })}
               placeholder="123456789" inputMode="numeric" />
      </div>
      <div className={styles.field}>
        <label>To(account_id、カンマ区切り・任意)</label>
        <input type="text" value={draft.to} onChange={(e) => set({ to: e.target.value })}
               placeholder="1234567, 7654321" />
      </div>
      <div className={styles.field}>
        <label>通知時間帯(JST)</label>
        <div className={styles.hourRow}>
          <input type="number" min={0} max={24} value={draft.notifyStartHour}
                 onChange={(e) => set({ notifyStartHour: e.target.value })} />
          <span>時 〜</span>
          <input type="number" min={0} max={24} value={draft.notifyEndHour}
                 onChange={(e) => set({ notifyEndHour: e.target.value })} />
          <span>時</span>
        </div>
      </div>
      <div className={styles.field}>
        <label>オプション</label>
        <label className={styles.checkRow}>
          <input type="checkbox" checked={draft.enabled}
                 onChange={(e) => set({ enabled: e.target.checked })} />
          有効(オフにすると送信しません)
        </label>
        <label className={styles.checkRow}>
          <input type="checkbox" checked={draft.sendWhenEmpty}
                 onChange={(e) => set({ sendWhenEmpty: e.target.checked })} />
          配信が無い時間帯も送る
        </label>
      </div>
    </div>
  );
}
