"use client";

import { useRef, useState } from "react";
import styles from "./NotificationGroups.module.css";

// CSV での往復編集。事務所規模(100人超)の初期設定や大規模な入れ替え向けで、
// 日常の小さな変更は上の一括操作(検索・全選択)で足りる。
//
// **適用の前に必ず差分を見せる。** 件数だけだと、思っていたのと違う変更が
// 混ざっていても気づけない。誰がどのグループに入る/外れるかを一覧で出す。

type DiffRow = { group: string; groupId: number; username: string; displayName: string };
type Preview = {
  added: DiffRow[];
  removed: DiffRow[];
  warnings: string[];
  streamersInCsv: number;
  groupsInCsv: number;
};

export function CsvTransfer({ onApplied }: { onApplied: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [csv, setCsv] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  function reset() {
    setCsv(null); setFileName(null); setPreview(null); setErrors([]); setDone(null);
    if (fileRef.current) fileRef.current.value = "";
  }

  async function post(text: string, apply: boolean) {
    setBusy(true); setErrors([]); setDone(null);
    try {
      const res = await fetch("/api/notifications/assignments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csv: text, apply }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setErrors(body.errors ?? [body.error ?? "処理に失敗しました。"]);
        setPreview(null);
        return null;
      }
      return body as Preview;
    } catch (err) {
      setErrors([err instanceof Error ? err.message : "通信に失敗しました。"]);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    // BOM は File.text() が UTF-8 として読んだあとも先頭に残る。Python 側で
    // 剥がすのでここでは触らない。
    const text = await file.text();
    setCsv(text); setFileName(file.name); setDone(null);
    const result = await post(text, false);
    setPreview(result);
  }

  async function applyNow() {
    if (!csv) return;
    const result = await post(csv, true);
    if (result) {
      setDone(`反映しました(追加 ${result.added.length}件 / 削除 ${result.removed.length}件)`);
      setPreview(null); setCsv(null); setFileName(null);
      if (fileRef.current) fileRef.current.value = "";
      onApplied();
    }
  }

  const nothingToDo = preview && preview.added.length === 0 && preview.removed.length === 0;

  return (
    <div className={styles.card}>
      <div className={styles.unassignedHead}>CSVで一括編集</div>
      <p className={styles.csvLead}>
        行=ライバー、列=グループのマトリクスです。割り当てありを 1、なしを 0 にします。
        未割り当ての人も含まれるので、設定漏れの確認にも使えます。
      </p>

      <div className={styles.actions}>
        <a className={styles.btn} href="/api/notifications/assignments" download>
          CSVをダウンロード
        </a>
        <button type="button" className={styles.btn} disabled={busy}
                onClick={() => fileRef.current?.click()}>
          {busy ? "確認中…" : "CSVを読み込む"}
        </button>
        <input ref={fileRef} type="file" accept=".csv,text/csv" hidden onChange={onFile} />
        {(preview || errors.length > 0) && (
          <button type="button" className={styles.btn} onClick={reset} disabled={busy}>
            やめる
          </button>
        )}
      </div>

      {fileName && <p className={styles.csvFile}>読み込んだファイル: {fileName}</p>}
      {done && <p className={styles.csvDone} role="status">{done}</p>}

      {errors.length > 0 && (
        <div className={styles.csvErrors} role="alert">
          <strong>取り込めませんでした（変更していません）</strong>
          <ul>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </div>
      )}

      {preview && (
        <div className={styles.csvPreview}>
          {preview.warnings.map((w, i) => (
            <p key={i} className={styles.csvWarning}>⚠ {w}</p>
          ))}
          <p className={styles.csvSummary}>
            CSV内: ライバー {preview.streamersInCsv}人 / グループ {preview.groupsInCsv}個
            　→　<strong>追加 {preview.added.length}件</strong>・
            <strong>削除 {preview.removed.length}件</strong>
          </p>

          {nothingToDo ? (
            <p className="empty">現在の割り当てと同じです。変更はありません。</p>
          ) : (
            <div className={styles.csvDiffGrid}>
              <DiffList title="追加される" rows={preview.added} tone="add" />
              <DiffList title="削除される" rows={preview.removed} tone="remove" />
            </div>
          )}

          <div className={styles.actions} style={{ marginTop: 12 }}>
            <button type="button" className={`${styles.btn} ${styles.btnPrimary}`}
                    disabled={busy || !!nothingToDo} onClick={applyNow}>
              {busy ? "反映中…" : "この内容で反映する"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function DiffList({ title, rows, tone }: { title: string; rows: DiffRow[]; tone: "add" | "remove" }) {
  return (
    <div>
      <div className={tone === "add" ? styles.csvAddHead : styles.csvRemoveHead}>
        {title}（{rows.length}件）
      </div>
      {rows.length === 0 ? (
        <p className="empty" style={{ fontSize: 13 }}>なし</p>
      ) : (
        <ul className={styles.csvDiffList}>
          {rows.map((r) => (
            <li key={`${r.groupId}:${r.username}`}>
              <span className={styles.chip}>{r.group}</span>
              {r.displayName || r.username}
              <span className={styles.csvAccount}>@{r.username}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
