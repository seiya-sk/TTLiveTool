import fs from "node:fs";
import path from "node:path";

// Routes that shell out to tiktok_monitor (fx-rate, avatar fetch, progress
// test-send) all need the repo's venv interpreter. The path differs by
// platform -- .venv/Scripts/python.exe on Windows, .venv/bin/python
// everywhere else -- and this project is developed on Windows but deployed
// to a Linux VPS, so hardcoding either one silently breaks the other.
// Probing the filesystem covers both without a build-time switch.
export const REPO_ROOT = path.resolve(/* turbopackIgnore: true */ process.cwd(), "..");

const CANDIDATES = [
  path.join(REPO_ROOT, ".venv", "bin", "python"),
  path.join(REPO_ROOT, ".venv", "Scripts", "python.exe"),
];

export function venvPython(): string {
  for (const candidate of CANDIDATES) {
    try {
      if (fs.existsSync(candidate)) return candidate;
    } catch {
      // ignore -- fall through to the platform default
    }
  }
  return process.platform === "win32" ? CANDIDATES[1] : CANDIDATES[0];
}
