import { NextResponse } from "next/server";
import { DashboardDbError } from "@/lib/db";
import { listStreamersForManagement, setStreamerArchived } from "@/lib/streamers";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const streamerId = Number(id);
  if (!Number.isInteger(streamerId)) {
    return NextResponse.json({ error: "invalid streamer id" }, { status: 400 });
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }

  const { archived } = (body ?? {}) as { archived?: boolean };
  if (typeof archived !== "boolean") {
    return NextResponse.json({ error: "archived (boolean) is required" }, { status: 400 });
  }

  try {
    setStreamerArchived(streamerId, archived);
    return NextResponse.json(listStreamersForManagement());
  } catch (err) {
    if (err instanceof DashboardDbError) {
      return NextResponse.json({ error: err.message }, { status: 500 });
    }
    throw err;
  }
}
