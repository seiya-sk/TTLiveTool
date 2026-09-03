"use client";

import Link from "next/link";
import { useState } from "react";
import { Avatar } from "@/components/Avatar";
import { DataTable, type Column } from "@/components/DataTable";
import { StatusBadge } from "@/components/StatusBadge";
import Tabs, { type TabDef } from "@/components/Tabs";
import { avatarUrl, formatJst } from "@/lib/format";
import type { StreamerManagementRow } from "@/lib/streamers";

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error ?? "リクエストに失敗しました");
  return data as T;
}

export function StreamerManagement({ initialRows }: { initialRows: StreamerManagementRow[] }) {
  const [rows, setRows] = useState(initialRows);
  const [tiktokAccountId, setTiktokAccountId] = useState("");
  const [name, setName] = useState("");
  const [busyId, setBusyId] = useState<number | "add" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    setBusyId("add");
    setError(null);
    try {
      const updated = await postJson<StreamerManagementRow[]>("/api/streamers", { tiktokAccountId, name });
      setRows(updated);
      setTiktokAccountId("");
      setName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }

  async function patch(row: StreamerManagementRow, body: Record<string, boolean>) {
    setBusyId(row.id);
    setError(null);
    try {
      setRows(await postJson<StreamerManagementRow[]>(`/api/streamers/${row.id}`, body));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }

  // 状態は3つ。archived が立っていれば enabled は意味を持たない(アーカイブが優先)。
  const stateOf = (r: StreamerManagementRow) =>
    r.archived ? "アーカイブ済み" : r.enabled ? "有効" : "無効";

  const nameColumn: Column<StreamerManagementRow> = {
    key: "name",
    label: "ライバー名",
    accessor: (r) => r.name,
    searchable: true,
    render: (r) => (
      // アーカイブ済みでも過去のライブは見られる。詳細への導線は必ず残す。
      <Link href={`/streamers/${r.tiktokAccountId}`} className="ranking-name-cell">
        <Avatar name={r.name} src={avatarUrl(r.avatarPath)} size={30} />
        {r.name}
      </Link>
    ),
  };

  const activeColumns: Column<StreamerManagementRow>[] = [
    {
      key: "state",
      label: "状態",
      accessor: stateOf,
      filterable: true,
      width: "100px",
      // "監視中" (the reference mock's label) would imply an active
      // watch.py connection this DB-only management view has no way to
      // know about -- 有効/無効 is what's actually true here.
      render: (r) => (
        <StatusBadge label={stateOf(r)} tone={r.enabled ? "success" : "warning"} />
      ),
    },
    nameColumn,
    { key: "tiktokAccountId", label: "TikTokアカウントID", accessor: (r) => r.tiktokAccountId, searchable: true },
    { key: "sessionCount", label: "配信数", accessor: (r) => r.sessionCount, align: "right", width: "90px" },
    {
      key: "createdAt",
      label: "登録日時",
      accessor: (r) => r.createdAt,
      render: (r) => formatJst(r.createdAt),
      width: "170px",
    },
    {
      key: "lastSessionAt",
      label: "直近配信日",
      accessor: (r) => r.lastSessionAt,
      render: (r) => formatJst(r.lastSessionAt),
      width: "170px",
    },
    {
      key: "actions",
      label: "操作",
      accessor: () => null,
      width: "190px",
      render: (r) => (
        <span className="streamer-actions">
          <button type="button" onClick={() => patch(r, { enabled: !r.enabled })}
                  disabled={busyId === r.id}>
            {busyId === r.id ? "処理中..." : r.enabled ? "無効にする" : "有効にする"}
          </button>
          <button type="button" onClick={() => patch(r, { archived: true })}
                  disabled={busyId === r.id}>
            アーカイブ
          </button>
        </span>
      ),
    },
  ];

  // アーカイブタブは一覧性だけあればよい。通常タブと同じ情報量は要らないが、
  // **有効/無効に戻す導線は必ず残す**(誤操作の復旧、再契約)。
  const archivedColumns: Column<StreamerManagementRow>[] = [
    nameColumn,
    { key: "tiktokAccountId", label: "TikTokアカウントID", accessor: (r) => r.tiktokAccountId, searchable: true },
    { key: "sessionCount", label: "配信数", accessor: (r) => r.sessionCount, align: "right", width: "90px" },
    {
      key: "archivedAt",
      label: "アーカイブ日時",
      accessor: (r) => r.archivedAt,
      render: (r) => formatJst(r.archivedAt),
      width: "170px",
    },
    {
      key: "actions",
      label: "操作",
      accessor: () => null,
      width: "120px",
      render: (r) => (
        <button type="button" onClick={() => patch(r, { archived: false })}
                disabled={busyId === r.id}>
          {busyId === r.id ? "処理中..." : "元に戻す"}
        </button>
      ),
    },
  ];

  const active = rows.filter((r) => !r.archived);
  const archived = rows.filter((r) => r.archived);

  const tabs: TabDef[] = [
    {
      key: "active",
      label: `通常（${active.length}人）`,
      accent: "cyan",
      content: (
        <DataTable rows={active} columns={activeColumns}
                   defaultSort={{ key: "name", dir: "asc" }}
                   emptyMessage="登録されたライバーがいません。" />
      ),
    },
    {
      key: "archived",
      label: `アーカイブ（${archived.length}人）`,
      accent: "purple",
      content: (
        <DataTable rows={archived} columns={archivedColumns}
                   defaultSort={{ key: "archivedAt", dir: "desc" }}
                   emptyMessage="アーカイブ済みのライバーはいません。" />
      ),
    },
  ];

  return (
    <div>
      <form className="streamer-add-form" onSubmit={handleAdd}>
        <h3>ライバーを追加</h3>
        <div className="fx-settings-row">
          <label>TikTokアカウントID(@なし)</label>
          <input
            type="text"
            value={tiktokAccountId}
            onChange={(e) => setTiktokAccountId(e.target.value)}
            placeholder="username"
            required
          />
        </div>
        <div className="fx-settings-row">
          <label>表示名(任意)</label>
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="未入力ならIDを使用" />
          <button type="submit" disabled={busyId === "add"}>
            {busyId === "add" ? "追加中..." : "追加"}
          </button>
        </div>
        {error && <div className="fx-error">{error}</div>}
      </form>

      <h2>登録ライバー一覧</h2>
      <Tabs tabs={tabs} />
    </div>
  );
}
