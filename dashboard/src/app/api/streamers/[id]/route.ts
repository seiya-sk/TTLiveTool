import { NextResponse } from "next/server";
import { DashboardDbError } from "@/lib/db";
import {
  listStreamersForManagement,
  setStreamerArchived,
  setStreamerEnabled,
  StreamerManagementError,
} from "@/lib/streamers";

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

  // archived と enabled は別の軸なので、それぞれ独立に受ける。
  // どちらも無いリクエストは意図が読めないので弾く。
  const { archived, enabled } = (body ?? {}) as { archived?: boolean; enabled?: boolean };
  if (typeof archived !== "boolean" && typeof enabled !== "boolean") {
    return NextResponse.json(
      { error: "archived か enabled のいずれか (boolean) が必要です" },
      { status: 400 },
    );
  }

  try {
    // アーカイブから戻すときは有効な状態で戻す。無効のままアーカイブした人を
    // 復元したとき、通常タブに出てくるのに録画対象外という分かりにくい状態を
    // 避けるため。無効に戻したければ復元後に切り替えられる。
    if (typeof archived === "boolean") {
      setStreamerArchived(streamerId, archived);
      if (!archived) setStreamerEnabled(streamerId, true);
    }
    if (typeof enabled === "boolean") {
      setStreamerEnabled(streamerId, enabled);
    }
    return NextResponse.json(listStreamersForManagement());
  } catch (err) {
    if (err instanceof StreamerManagementError) {
      return NextResponse.json({ error: err.message }, { status: 404 });
    }
    if (err instanceof DashboardDbError) {
      return NextResponse.json({ error: err.message }, { status: 500 });
    }
    throw err;
  }
}
