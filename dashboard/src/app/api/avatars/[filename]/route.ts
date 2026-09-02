import { readFile } from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";
import { resolveAvatarDir } from "@/lib/db";

// Avatars live flat in data/avatars/ (tiktok_monitor/fetch_avatars.py's
// default_avatar_path never nests subdirectories), so this only ever needs
// a single filename segment -- no catch-all route required. Mirrors
// screenshots/[filename]/route.ts's strict allowlist pattern rather than
// just stripping ".." so a value like "..%2f..%2fsecret" can't sneak past.
const SAFE_FILENAME = /^[A-Za-z0-9._-]+\.webp$/;

const AVATAR_DIR = resolveAvatarDir();

export async function GET(_request: Request, context: { params: Promise<{ filename: string }> }) {
  const { filename } = await context.params;
  if (!SAFE_FILENAME.test(filename)) {
    return NextResponse.json({ error: "invalid filename" }, { status: 400 });
  }

  const filePath = path.join(AVATAR_DIR, filename);
  try {
    const data = await readFile(filePath);
    return new NextResponse(new Uint8Array(data), {
      headers: {
        "Content-Type": "image/webp",
        "Cache-Control": "private, max-age=3600",
      },
    });
  } catch {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
}
