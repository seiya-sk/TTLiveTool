import { execFile } from "node:child_process";
import { NextResponse } from "next/server";
import { REPO_ROOT, venvPython } from "@/lib/python";
import { DashboardDbError, resolveDbPath } from "@/lib/db";
import { addStreamer, listStreamersForManagement, StreamerManagementError } from "@/lib/streamers";

// Mirrors settings/fx-rate/route.ts's shell-out to tiktok_monitor -- this
// is the one place that talks to TikTok's avatar-lookup API.

// Fired without awaiting: fetching+downloading an avatar is a multi-second
// network round trip to TikTok, and a streamer registration must succeed
// (and respond) regardless of whether that lookup works, is slow, or the
// account has no avatar. The icon simply appears on avatarPath's next read
// once this finishes -- StreamerManagement.tsx's row won't show it until
// the table is reloaded. Safe as fire-and-forget only because this runs as
// a long-lived `next dev`/`next start` process, not a serverless function
// that could be torn down before the child process exits.
function fetchAvatarInBackground(tiktokAccountId: string): void {
  execFile(
    venvPython(),
    ["-m", "tiktok_monitor.fetch_avatars", "--db-path", resolveDbPath(), tiktokAccountId],
    { cwd: REPO_ROOT, timeout: 30000 },
    (err) => {
      if (err) {
        console.error(`avatar fetch failed for @${tiktokAccountId}:`, err.message);
      }
    }
  );
}

export async function GET() {
  try {
    return NextResponse.json(listStreamersForManagement());
  } catch (err) {
    if (err instanceof DashboardDbError) {
      return NextResponse.json({ error: err.message }, { status: 500 });
    }
    throw err;
  }
}

export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }

  const { tiktokAccountId, name } = (body ?? {}) as { tiktokAccountId?: string; name?: string };
  if (typeof tiktokAccountId !== "string") {
    return NextResponse.json({ error: "tiktokAccountId is required" }, { status: 400 });
  }

  try {
    const created = addStreamer(tiktokAccountId, typeof name === "string" ? name : "");
    fetchAvatarInBackground(created.tiktokAccountId);
    return NextResponse.json(listStreamersForManagement());
  } catch (err) {
    if (err instanceof StreamerManagementError) {
      return NextResponse.json({ error: err.message }, { status: 400 });
    }
    if (err instanceof DashboardDbError) {
      return NextResponse.json({ error: err.message }, { status: 500 });
    }
    throw err;
  }
}
