import { NextResponse } from "next/server";
import { DashboardDbError } from "@/lib/db";
import { createGroup, listGroups, NotificationSettingsError, type GroupInput } from "@/lib/notifications";

// /api/settings には相乗りしない -- あちらは value:number 専用に型付けされて
// おり(lib/settings.ts の SettingKey / setSetting)、ルームIDや配列は通らない。
// 無理に通すとFXレート・トークン単価側の型安全性が壊れる。

export function toGroupInput(body: unknown): GroupInput {
  const b = (body ?? {}) as Partial<GroupInput>;
  return {
    name: String(b.name ?? ""),
    roomId: String(b.roomId ?? ""),
    toAccountIds: Array.isArray(b.toAccountIds) ? b.toAccountIds.map(String) : [],
    enabled: b.enabled !== false,
    sendWhenEmpty: b.sendWhenEmpty === true,
    notifyStartHour: Number(b.notifyStartHour ?? 9),
    notifyEndHour: Number(b.notifyEndHour ?? 24),
  };
}

export function handleError(err: unknown) {
  if (err instanceof NotificationSettingsError) {
    return NextResponse.json({ error: err.message }, { status: 400 });
  }
  if (err instanceof DashboardDbError) {
    return NextResponse.json({ error: err.message }, { status: 500 });
  }
  throw err;
}

export async function GET() {
  try {
    return NextResponse.json(listGroups());
  } catch (err) {
    return handleError(err);
  }
}

export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  try {
    createGroup(toGroupInput(body));
    return NextResponse.json(listGroups());
  } catch (err) {
    return handleError(err);
  }
}
