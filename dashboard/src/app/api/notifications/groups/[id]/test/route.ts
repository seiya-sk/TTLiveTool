import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { NextResponse } from "next/server";
import { resolveDbPath } from "@/lib/db";
import { REPO_ROOT, venvPython } from "@/lib/python";

const execFileAsync = promisify(execFile);

// テスト送信は Python の progress_notifier.py --test-send を呼ぶ。
// ここでTS側に集計を再実装しないのは、ギフトの重複排除(streaking /
// log_id重複 / log_id再利用)が繊細で、実装が増えるほど通知の数字と
// ダッシュボードの数字が食い違う危険が上がるため。--test-send は
// 通知時間帯と二重送信チェックを無視し、digest_log にも記録しない。
export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const groupId = Number(id);
  if (!Number.isInteger(groupId) || groupId <= 0) {
    return NextResponse.json({ error: "invalid group id" }, { status: 400 });
  }

  try {
    const { stdout, stderr } = await execFileAsync(
      venvPython(),
      ["ops/progress_notifier.py", "--db-path", resolveDbPath(), "--group-id", String(groupId), "--test-send"],
      { cwd: REPO_ROOT, timeout: 60000 }
    );
    const output = `${stdout}\n${stderr}`;
    if (/送信失敗|Traceback/.test(output)) {
      return NextResponse.json(
        { error: `テスト送信に失敗しました: ${output.trim().split("\n").slice(-3).join(" / ")}` },
        { status: 502 }
      );
    }
    return NextResponse.json({ ok: true, message: "テスト送信しました。Chatworkのルームを確認してください。" });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `テスト送信に失敗しました: ${message}` }, { status: 502 });
  }
}
