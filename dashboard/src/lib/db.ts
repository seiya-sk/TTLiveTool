import path from "node:path";
import Database from "better-sqlite3";

let instance: Database.Database | null = null;
let writableInstance: Database.Database | null = null;

export class DashboardDbError extends Error {}

export function resolveDbPath(): string {
  return path.resolve(
    /* turbopackIgnore: true */ process.cwd(),
    process.env.TTS_DB_PATH ?? "../data/tts_live_tool.db"
  );
}

export function resolveScreenshotDir(): string {
  return path.resolve(
    /* turbopackIgnore: true */ process.cwd(),
    process.env.TTS_SCREENSHOT_DIR ?? "../data/screenshots"
  );
}

export function resolveAvatarDir(): string {
  return path.resolve(
    /* turbopackIgnore: true */ process.cwd(),
    process.env.TTS_AVATAR_DIR ?? "../data/avatars"
  );
}

export function getDb(): Database.Database {
  if (instance) return instance;

  const dbPath = resolveDbPath();
  try {
    instance = new Database(dbPath, { readonly: true, fileMustExist: true });
  } catch (err) {
    throw new DashboardDbError(
      `SQLiteデータベースを開けませんでした (${dbPath})。tiktok_monitor をまだ一度も実行していない可能性があります: ${
        err instanceof Error ? err.message : String(err)
      }`
    );
  }

  return instance;
}

// Everything else in the dashboard is read-only by design (tiktok_monitor
// owns the data); app_settings (USD/JPY rate, token pricing -- see
// lib/settings.ts) is the one exception, since it's dashboard-editable
// configuration with no Python writer running at the same time it would be
// edited from. WAL mode (set by tiktok_monitor.db.connect) lets this
// writable connection and the readonly one above coexist safely.
export function getWritableDb(): Database.Database {
  if (writableInstance) return writableInstance;

  const dbPath = resolveDbPath();
  try {
    writableInstance = new Database(dbPath, { fileMustExist: true });
    writableInstance.pragma("journal_mode = WAL");
    // better-sqlite3 leaves foreign_keys OFF by default, unlike
    // tiktok_monitor/db.py's connect() which turns it ON. Without this the
    // ON DELETE CASCADE on notification_group_streamers silently does
    // nothing when a group is deleted from the dashboard, orphaning its
    // assignment rows. Matching Python's connection settings keeps both
    // writers behaving identically against the same file.
    writableInstance.pragma("foreign_keys = ON");
    writableInstance.exec(
      `CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
      )`
    );
  } catch (err) {
    throw new DashboardDbError(
      `SQLiteデータベースを開けませんでした (${dbPath}): ${err instanceof Error ? err.message : String(err)}`
    );
  }

  return writableInstance;
}
