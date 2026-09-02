import { DashboardDbError } from "@/lib/db";
import { getAllReportUsage } from "@/lib/queries";
import { getSettings } from "@/lib/settings";
import { PageHeader } from "@/components/PageHeader";
import { TokenManagement } from "./TokenManagement";

// New reports can be generated at any time; never freeze this at build time.
export const dynamic = "force-dynamic";

export default function TokenManagementPage() {
  let usageRows;
  let settings;
  try {
    usageRows = getAllReportUsage();
    settings = getSettings();
  } catch (err) {
    if (err instanceof DashboardDbError) {
      return (
        <div className="container">
          <PageHeader title="トークン管理" />
          <p className="empty">{err.message}</p>
        </div>
      );
    }
    throw err;
  }

  return (
    <div className="container">
      <PageHeader title="トークン管理" description="AI(Claude API)のトークン消費量とコストを期間・ライバー・ライブ別に確認できます。" />
      <TokenManagement usageRows={usageRows} initialSettings={settings} />
    </div>
  );
}
