import { DashboardDbError } from "@/lib/db";
import { listStreamersForManagement } from "@/lib/streamers";
import { PageHeader } from "@/components/PageHeader";
import { StreamerManagement } from "./StreamerManagement";

// Streamer roster changes whenever an admin adds/archives one; never
// freeze this at build time.
export const dynamic = "force-dynamic";

export default function StreamerManagementPage() {
  let rows;
  try {
    rows = listStreamersForManagement();
  } catch (err) {
    if (err instanceof DashboardDbError) {
      return (
        <div className="container">
          <PageHeader title="ライバー管理" />
          <p className="empty">{err.message}</p>
        </div>
      );
    }
    throw err;
  }

  return (
    <div className="container">
      <PageHeader title="ライバー管理" description="登録ライバーの追加・アーカイブを管理できます。" />
      <p className="token-price-note">
        削除は論理削除です。アーカイブしても過去の配信データは失われず、いつでも復元できます。
      </p>
      <StreamerManagement initialRows={rows} />
    </div>
  );
}
