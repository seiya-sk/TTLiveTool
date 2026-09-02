import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { NextResponse } from "next/server";
import { REPO_ROOT, venvPython } from "@/lib/python";
import { resolveDbPath } from "@/lib/db";
import { getSettings } from "@/lib/settings";

const execFileAsync = promisify(execFile);

// The "取得" button shells out to tiktok_monitor.fxrate rather than
// reimplementing the fetch here, so there's exactly one place (Python)
// that talks to the exchange rate API -- this route just triggers it and
// re-reads whatever it wrote to app_settings.

export async function POST() {
  try {
    await execFileAsync(venvPython(), ["-m", "tiktok_monitor.fxrate", "--db-path", resolveDbPath()], {
      cwd: REPO_ROOT,
      timeout: 15000,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `為替レートの取得に失敗しました: ${message}` }, { status: 502 });
  }

  return NextResponse.json(getSettings());
}
