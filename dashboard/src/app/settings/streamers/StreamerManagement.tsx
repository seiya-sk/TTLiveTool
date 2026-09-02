"use client";

import Link from "next/link";
import { useState } from "react";
import { Avatar } from "@/components/Avatar";
import { DataTable, type Column } from "@/components/DataTable";
import { StatusBadge } from "@/components/StatusBadge";
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

  async function toggleArchived(row: StreamerManagementRow) {
    setBusyId(row.id);
    setError(null);
    try {
      const updated = await postJson<StreamerManagementRow[]>(`/api/streamers/${row.id}`, {
        archived: !row.archived,
      });
      setRows(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  }

  const columns: Column<StreamerManagementRow>[] = [
    {
      key: "archived",
      label: "状態",
      accessor: (r) => (r.archived ? "アーカイブ済み" : "有効"),
      filterable: true,
      width: "120px",
      // "監視中" (the reference mock's label) would imply an active
      // watch.py connection this DB-only management view has no way to
      // know about -- 有効/アーカイブ済み is what's actually true here.
      render: (r) => <StatusBadge label={r.archived ? "アーカイブ済み" : "有効"} tone={r.archived ? "muted" : "success"} />,
    },
    {
      key: "name",
      label: "ライバー名",
      accessor: (r) => r.name,
      searchable: true,
      render: (r) => (
        <Link href={`/streamers/${r.tiktokAccountId}`} className="ranking-name-cell">
          <Avatar name={r.name} src={avatarUrl(r.avatarPath)} size={30} />
          {r.name}
        </Link>
      ),
    },
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
      width: "110px",
      render: (r) => (
        <button type="button" onClick={() => toggleArchived(r)} disabled={busyId === r.id}>
          {busyId === r.id ? "処理中..." : r.archived ? "復元" : "アーカイブ"}
        </button>
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
      <DataTable rows={rows} columns={columns} defaultSort={{ key: "name", dir: "asc" }} emptyMessage="登録されたライバーがいません。" />
    </div>
  );
}
