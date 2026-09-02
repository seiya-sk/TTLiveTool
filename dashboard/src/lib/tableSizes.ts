import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { REPO_ROOT, venvPython } from "./python";
import { resolveDbPath } from "./db";

// テーブル別のディスク使用量。**リクエストの中では絶対に集計しない。**
//
// 以前は dbstat をページのレンダリング中に直接叩いていた。better-sqlite3 は
// 同期APIなので、DBが4.6GBに育った時点で Node のイベントループが39秒間
// 止まり、データ管理を開いた人だけでなく **同時に見ている全員** の画面が
// 固まっていた(実測 2026-09-02: /settings/data の処理中、通常0.7秒の / が
// 28.3秒かかった)。
//
// いまは ops/table_sizes.py が別プロセスで集計してJSONに落とし、ここは
// そのJSONを読むだけにする。古くなっていれば裏で再集計を起こすが、
// 呼び出し側は待たない -- 古い値をそのまま返す。値が一度も無い初回だけ
// null になり、その場合はUI側が「集計中」と表示する。

const CACHE_PATH = path.join(REPO_ROOT, "data", "table_sizes.json");
// 内訳の比率は日単位でしか動かないので、30分で十分に新しい。
const TTL_MS = 30 * 60 * 1000;

export type TableSizes = {
  sizes: Map<string, number>;
  computedAt: Date;
  ageMs: number;
  stale: boolean;
  freeBytes: number;
};

type CacheFile = {
  computed_at: string;
  db_bytes: number;
  free_bytes: number;
  tables: Record<string, number>;
};

let refreshInFlight = false;

/** 裏で再集計を起こす。**完了を待たない。** 多重起動もしない。 */
function refreshInBackground(): void {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const child = spawn(
      venvPython(),
      [
        path.join(REPO_ROOT, "ops", "table_sizes.py"),
        "--db-path",
        resolveDbPath(),
        "--out",
        CACHE_PATH,
      ],
      { cwd: REPO_ROOT, stdio: "ignore", detached: false },
    );
    child.on("exit", () => {
      refreshInFlight = false;
    });
    child.on("error", () => {
      refreshInFlight = false;
    });
  } catch {
    refreshInFlight = false;
  }
}

/**
 * 最後に集計された結果を返す。一度も集計されていなければ null。
 * どちらの場合も **即座に返る**(集計は待たない)。
 */
export function getTableSizes(): TableSizes | null {
  let raw: string;
  try {
    raw = fs.readFileSync(CACHE_PATH, "utf-8");
  } catch {
    refreshInBackground(); // まだ一度も集計されていない
    return null;
  }

  let parsed: CacheFile;
  try {
    parsed = JSON.parse(raw) as CacheFile;
  } catch {
    refreshInBackground(); // 壊れている(書き込み中の断片など)
    return null;
  }

  const computedAt = new Date(parsed.computed_at);
  const ageMs = Date.now() - computedAt.getTime();
  const stale = !Number.isFinite(ageMs) || ageMs > TTL_MS;
  if (stale) refreshInBackground();

  return {
    sizes: new Map(Object.entries(parsed.tables ?? {})),
    computedAt,
    ageMs,
    stale,
    freeBytes: parsed.free_bytes ?? 0,
  };
}
