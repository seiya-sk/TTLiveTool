import { DashboardDbError } from "@/lib/db";
import { listAssignableStreamers, listGroups, listUnassignedStreamers } from "@/lib/notifications";
import { PageHeader } from "@/components/PageHeader";
import { NotificationGroups } from "./NotificationGroups";

// グループ構成も送信履歴も tiktok_monitor 側の稼働に応じて変わるので、
// ビルド時に固めない。
export const dynamic = "force-dynamic";

export default async function NotificationsSettingsPage() {
  try {
    const groups = listGroups();
    const assignable = listAssignableStreamers();
    const unassigned = listUnassignedStreamers();
    return (
      <div className="container">
        <PageHeader
          title="通知設定"
          description="ライバーの進捗を、事務所/チームごとのChatworkルームへ1時間ごとに通知します。システム異常のエラー通知は運用者専用のため、この画面には表示されません。"
        />
        <NotificationGroups initialGroups={groups} assignable={assignable} initialUnassigned={unassigned} />
      </div>
    );
  } catch (err) {
    if (err instanceof DashboardDbError) {
      return (
        <div className="container">
          <PageHeader title="通知設定" />
          <p className="empty">{err.message}</p>
        </div>
      );
    }
    throw err;
  }
}
