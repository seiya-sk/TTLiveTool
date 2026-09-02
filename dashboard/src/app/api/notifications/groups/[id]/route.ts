import { NextResponse } from "next/server";
import { deleteGroup, listGroups, updateGroup } from "@/lib/notifications";
import { handleError, toGroupInput } from "../route";

async function groupId(context: { params: Promise<{ id: string }> }): Promise<number | null> {
  const { id } = await context.params;
  const n = Number(id);
  return Number.isInteger(n) && n > 0 ? n : null;
}

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const id = await groupId(context);
  if (id === null) return NextResponse.json({ error: "invalid group id" }, { status: 400 });

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  try {
    updateGroup(id, toGroupInput(body));
    return NextResponse.json(listGroups());
  } catch (err) {
    return handleError(err);
  }
}

export async function DELETE(_request: Request, context: { params: Promise<{ id: string }> }) {
  const id = await groupId(context);
  if (id === null) return NextResponse.json({ error: "invalid group id" }, { status: 400 });
  try {
    deleteGroup(id);
    return NextResponse.json(listGroups());
  } catch (err) {
    return handleError(err);
  }
}
