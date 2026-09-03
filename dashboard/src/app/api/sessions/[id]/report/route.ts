import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { NextResponse } from "next/server";
import { REPO_ROOT, venvPython } from "@/lib/python";
import { getDb, resolveDbPath } from "@/lib/db";
import { getLatestReport } from "@/lib/queries";

const execFileAsync = promisify(execFile);

// AIレポートの生成は **1枚あたり約7.5円の原価** がかかる。意図しない生成が
// 起きない作りにするため、UI側のボタン制御だけに頼らず、サーバ側でも同じ
// 条件を検査する。画面が古いまま押された、タブが2つ開いている、といった
// 場合にUIの状態は当てにならない。
//
// 二重生成を防ぐ3段:
//   1. 生成済みなら断る(live_reports に行がある)
//   2. 配信中なら断る(不完全なレポートができ、しかも作り直せなくなる)
//   3. 同じセッションの生成が走っている間は断る(下の inFlight)
//
// 失敗時にレポート行は残らない -- generate_report() は Claude API の応答を
// 得たあとに insert_report() を呼ぶので、API エラーやタイムアウトなら
// 行は作られない。結果として「生成済み」扱いにならず、ボタンが復活する。

// 生成中のセッションid。ダッシュボードは単一プロセスなのでこれで足りる。
const inFlight = new Set<number>();

// レポート生成は Claude API の応答待ちを含む。CLI 実行の実測に基づいて
// 決めるべき値だが、未計測のため、途中で切らないことを優先して長めに取る。
// ここで切ると API の課金は発生したのにレポートが残らない -- いちばん損な
// 失敗の仕方になる。
const GENERATE_TIMEOUT_MS = 300_000;

export async function POST(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  const sessionId = Number(id);
  if (!Number.isInteger(sessionId) || sessionId <= 0) {
    return NextResponse.json({ error: "セッションIDが不正です。" }, { status: 400 });
  }

  const db = getDb();
  const session = db
    .prepare("SELECT status FROM live_sessions WHERE id = ?")
    .get(sessionId) as { status: string } | undefined;
  if (!session) {
    return NextResponse.json({ error: "そのライブは見つかりません。" }, { status: 404 });
  }
  if (session.status === "live") {
    return NextResponse.json(
      { error: "配信中のライブではレポートを生成できません。終了後にお試しください。" },
      { status: 409 },
    );
  }
  if (getLatestReport(sessionId)) {
    return NextResponse.json(
      { error: "このライブのレポートはすでに生成されています。" },
      { status: 409 },
    );
  }
  if (inFlight.has(sessionId)) {
    return NextResponse.json(
      { error: "このライブのレポートを生成中です。しばらくお待ちください。" },
      { status: 409 },
    );
  }

  inFlight.add(sessionId);
  const startedAt = Date.now();
  try {
    await execFileAsync(
      venvPython(),
      ["-m", "tiktok_monitor.generate_report", String(sessionId), "--db-path", resolveDbPath()],
      { cwd: REPO_ROOT, timeout: GENERATE_TIMEOUT_MS, maxBuffer: 10 * 1024 * 1024 },
    );
  } catch (err) {
    // generate_report は失敗理由を stderr に日本語で出す(APIキー未設定など)。
    // 汎用の "Command failed" だけ返すと原因が分からないので、拾って返す。
    const e = err as { stderr?: string; message?: string; killed?: boolean };
    const detail = (e.stderr || e.message || "").trim().split("\n").filter(Boolean).pop();
    const message = e.killed
      ? `生成がタイムアウトしました(${GENERATE_TIMEOUT_MS / 1000}秒)。`
      : `レポートの生成に失敗しました: ${detail || "原因不明"}`;
    return NextResponse.json({ error: message }, { status: 502 });
  } finally {
    inFlight.delete(sessionId);
  }

  const report = getLatestReport(sessionId);
  if (!report) {
    // コマンドは成功したのに行が無い = 想定外。成功扱いにするとUIが
    // 「生成済み」に変わってしまい、二度と作れなくなる。
    return NextResponse.json(
      { error: "生成は完了しましたが、レポートを読み出せませんでした。" },
      { status: 500 },
    );
  }
  return NextResponse.json({ report, elapsedMs: Date.now() - startedAt });
}
