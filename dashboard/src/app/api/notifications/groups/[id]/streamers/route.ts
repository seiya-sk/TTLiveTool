import { NextResponse } from "next/server";
import { listGroups, setGroupStreamers } from "@/lib/notifications";
import { handleError } from "../../route";

export async function PUT(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const groupId = Number(id);
  if (!Number.isInteger(groupId)) {
    return NextResponse.json({ error: "invalid group id" }, { status: 400 });
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }

  const { streamerIds } = (body ?? {}) as { streamerIds?: unknown };
  if (!Array.isArray(streamerIds) || streamerIds.some((v) => !Number.isInteger(v))) {
    return NextResponse.json({ error: "streamerIds (number[]) is required" }, { status: 400 });
  }

  try {
    setGroupStreamers(groupId, streamerIds as number[]);
    return NextResponse.json(listGroups());
  } catch (err) {
    return handleError(err);
  }
}
