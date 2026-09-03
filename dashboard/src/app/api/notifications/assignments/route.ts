import { execFile } from "node:child_process";
import { NextResponse } from "next/server";
import { REPO_ROOT, venvPython } from "@/lib/python";
import { resolveDbPath } from "@/lib/db";

// グループ割り当ての CSV 往復。解析・検証・適用はすべて Python 側
// (tiktok_monitor.notify.group_csv)に任せ、ここは受け渡しだけを行う。
//
// Python 側に置いた理由:
//   - CSV の解析は標準ライブラリの csv に任せたい。グループ名にカンマ・
//     改行・引用符が入りうるので、自前の分割では壊れる。ダッシュボード側には
//     CSV パーサが依存に無く(推移的にも無い)、追加が必要だった。
//   - 検証規則が多く、1つ緩むと「設定したつもりが反映されていない」という
//     気づきにくい壊れ方をする。テスト基盤のある側に置きたかった
//     (Python は pytest がある。ダッシュボードには JS のテスト基盤が無い)。

const TIMEOUT_MS = 60_000;
// 1000行を想定。1行あたり数百バイトなので余裕を持って上限を置く。
const MAX_CSV_BYTES = 5 * 1024 * 1024;

function runPython(command: "export" | "preview" | "apply", stdin?: string) {
  return new Promise<{ code: number; stdout: string; stderr: string }>((resolve, reject) => {
    const child = execFile(
      venvPython(),
      ["-m", "tiktok_monitor.notify.group_csv", command, "--db-path", resolveDbPath()],
      { cwd: REPO_ROOT, timeout: TIMEOUT_MS, maxBuffer: 20 * 1024 * 1024 },
      (err, stdout, stderr) => {
        // 検証エラーは終了コード1で返る(異常ではない)ので、reject せずに
        // そのまま渡す。呼び出し側が stdout の JSON を読む。
        const code = (err as { code?: number } | null)?.code ?? 0;
        if (err && code !== 1) return reject(err);
        resolve({ code, stdout, stderr });
      },
    );
    if (stdin !== undefined) {
      child.stdin?.end(stdin);
    }
  });
}

export async function GET() {
  try {
    const { stdout } = await runPython("export");
    return new NextResponse(stdout, {
      headers: {
        // charset=utf-8 を明示し、本文は BOM 付きで返す(Python 側が付ける)。
        // Excel は BOM が無いと日本語を Shift_JIS と誤認して文字化けする。
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition":
          `attachment; filename="notification-groups.csv"; ` +
          `filename*=UTF-8''${encodeURIComponent("通知グループ割り当て.csv")}`,
        "Cache-Control": "no-store",
      },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `CSVの書き出しに失敗しました: ${message}` }, { status: 500 });
  }
}

export async function POST(request: Request) {
  let body: { csv?: string; apply?: boolean };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "リクエストの形式が不正です。" }, { status: 400 });
  }
  const csv = body.csv;
  if (typeof csv !== "string" || !csv.trim()) {
    return NextResponse.json({ error: "CSVが空です。" }, { status: 400 });
  }
  if (Buffer.byteLength(csv, "utf-8") > MAX_CSV_BYTES) {
    return NextResponse.json({ error: "CSVが大きすぎます。" }, { status: 413 });
  }

  try {
    const { code, stdout } = await runPython(body.apply ? "apply" : "preview", csv);
    const parsed = JSON.parse(stdout || "{}");
    if (code === 1) {
      // 検証に落ちた。**DBは変更されていない。**
      return NextResponse.json({ errors: parsed.errors ?? ["検証に失敗しました。"] }, { status: 422 });
    }
    return NextResponse.json(parsed);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `CSVの処理に失敗しました: ${message}` }, { status: 500 });
  }
}
