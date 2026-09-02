export function formatJst(iso: string | null | undefined): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

// Hour:minute only, no seconds -- for compact chart labels (e.g. the
// composite chart's "配信開始時刻" tick at the time axis's left edge).
export function formatJstHm(iso: string | null | undefined): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatJstShort(iso: string | null | undefined): string {
  if (!iso) return "-";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function formatDuration(startedAt: string, endedAt: string | null): string {
  if (!endedAt) return "配信中";
  const start = new Date(startedAt).getTime();
  const end = new Date(endedAt).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return "-";
  const totalSeconds = Math.max(0, Math.round((end - start) / 1000));
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  return `${h}時間${m}分${s}秒`;
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return Math.round(value).toLocaleString("ja-JP");
}

// Numeric duration for KPI/baseline math (formatDuration above is
// display-only text). A still-live session (endedAt=null) measures against
// "now" so in-progress KPI cards show a sensible running duration.
export function durationMinutes(startedAt: string, endedAt: string | null): number {
  const start = new Date(startedAt).getTime();
  const end = endedAt ? new Date(endedAt).getTime() : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end)) return 0;
  return Math.max(0, (end - start) / 60000);
}

// Renders a plain minute count (e.g. an average across sessions) as
// "6時間52分" -- same wording as formatDuration above, but that one needs
// two timestamps; this is for a number already computed elsewhere.
export function formatMinutes(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || Number.isNaN(minutes)) return "-";
  const rounded = Math.max(0, Math.round(minutes));
  const h = Math.floor(rounded / 60);
  const m = rounded % 60;
  return h > 0 ? `${h}時間${m}分` : `${m}分`;
}

// "YYYY-MM" for the JST calendar month `iso` falls in -- mirrors
// queries.ts's strftime('%Y-%m', ..., '+9 hours') convention, so
// already-fetched rows (e.g. listSessions()) can be bucketed by month in
// TypeScript instead of a new SQL query per page.
export function jstMonthKey(iso: string): string {
  const jst = new Date(new Date(iso).getTime() + 9 * 60 * 60 * 1000);
  return `${jst.getUTCFullYear()}-${String(jst.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function elapsedMinutes(occurredAt: string, startedAt: string): number {
  const start = new Date(startedAt).getTime();
  const t = new Date(occurredAt).getTime();
  if (Number.isNaN(start) || Number.isNaN(t)) return 0;
  return (t - start) / 60000;
}

// tiktok_monitor's default_screenshot_path/fetch_avatars.default_avatar_path
// both join with os.path.join, which is backslash on Windows -- a stored
// path can mix "/" and "\" (e.g. "data/screenshots\\session9_....png").
// This just wants the trailing filename regardless of which separator
// produced it.
export function basenameFromPath(storedPath: string): string {
  const parts = storedPath.split(/[\\/]/);
  return parts[parts.length - 1] ?? storedPath;
}

// avatarPath is whatever fetch_avatars.py wrote to streamers.avatar_path
// (a local filesystem path, see its module docstring for why it's never a
// live CDN URL) -- null until a fetch has succeeded for that streamer.
export function avatarUrl(avatarPath: string | null | undefined): string | undefined {
  return avatarPath ? `/api/avatars/${basenameFromPath(avatarPath)}` : undefined;
}

// Treasure Box's open_at (envelope_info.unpack_at) is Unix seconds, unlike
// every other timestamp in this codebase (ISO8601 strings) -- see
// tiktok_monitor/events.py's normalize_treasure_box_envelope.
export function formatUnixSecondsJst(unixSeconds: number | null | undefined): string {
  if (unixSeconds === null || unixSeconds === undefined || Number.isNaN(unixSeconds)) return "-";
  return formatJstShort(new Date(unixSeconds * 1000).toISOString());
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "-";
  if (bytes < 1024) return `${bytes}B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value < 10 ? 2 : 1)}${units[unitIndex]}`;
}
