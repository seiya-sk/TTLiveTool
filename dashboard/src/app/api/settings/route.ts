import { NextResponse } from "next/server";
import { DashboardDbError } from "@/lib/db";
import { getSettings, setSetting, SETTING_KEYS, type SettingKey } from "@/lib/settings";

export async function GET() {
  try {
    return NextResponse.json(getSettings());
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

  const { key, value } = (body ?? {}) as { key?: string; value?: number };
  if (!key || !SETTING_KEYS.includes(key as SettingKey)) {
    return NextResponse.json({ error: `key must be one of: ${SETTING_KEYS.join(", ")}` }, { status: 400 });
  }
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return NextResponse.json({ error: "value must be a positive finite number" }, { status: 400 });
  }

  try {
    setSetting(key as SettingKey, value);
    return NextResponse.json(getSettings());
  } catch (err) {
    if (err instanceof DashboardDbError) {
      return NextResponse.json({ error: err.message }, { status: 500 });
    }
    throw err;
  }
}
