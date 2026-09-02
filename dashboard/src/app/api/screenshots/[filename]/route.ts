import { readFile } from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";
import { resolveScreenshotDir } from "@/lib/db";

// Screenshots live flat in data/screenshots/ (tiktok_monitor/screenshot.py's
// default_screenshot_path never nests subdirectories), so this only ever
// needs a single filename segment -- no catch-all route required. The
// filename is validated against a strict allowlist pattern rather than just
// stripping ".." so a value like "..%2f..%2fsecret" can't sneak past.
const SAFE_FILENAME = /^[A-Za-z0-9._-]+\.png$/;

const SCREENSHOT_DIR = resolveScreenshotDir();

export async function GET(_request: Request, context: { params: Promise<{ filename: string }> }) {
  const { filename } = await context.params;
  if (!SAFE_FILENAME.test(filename)) {
    return NextResponse.json({ error: "invalid filename" }, { status: 400 });
  }

  const filePath = path.join(SCREENSHOT_DIR, filename);
  try {
    const data = await readFile(filePath);
    return new NextResponse(new Uint8Array(data), {
      headers: {
        "Content-Type": "image/png",
        "Cache-Control": "private, max-age=3600",
      },
    });
  } catch {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
}
