import type { ReactNode } from "react";
import { KpiCard, type KpiAccent } from "@/components/KpiCard";
import { StatusBadge, type BadgeTone } from "@/components/StatusBadge";
import { ClockIcon, CoinIcon, DocumentIcon, PeopleIcon, PlayIcon, UserIcon } from "@/components/icons";
import { compareBattleInterval, compareGiftSendRate, compareNewFollowers, type BaselineResult } from "@/lib/baselines";
import { durationMinutes, formatNumber } from "@/lib/format";
import type { SessionStats } from "@/lib/queries";

const BASELINE_TONE: Record<BaselineResult["status"], BadgeTone> = {
  good: "success",
  warn: "warning",
  unavailable: "muted",
};

type CardDef = {
  label: string;
  value: string;
  icon?: ReactNode;
  accent?: KpiAccent;
  badge?: BaselineResult;
  unavailableLabel?: string;
};

export function KpiCards({
  stats,
  startedAt,
  endedAt,
}: {
  stats: SessionStats;
  startedAt: string;
  endedAt: string | null;
}) {
  const giftSendRate = stats.uniqueVisitors > 0 ? stats.uniqueGifters / stats.uniqueVisitors : null;
  const duration = durationMinutes(startedAt, endedAt);

  // 主要4枚(大きめ)+補助8枚の2階層(sample/UI/ライブ詳細.png、docx section 9)
  // -- 12枚を同格で並べると何を最初に見ればいいか埋もれるための区分け。
  const primary: CardDef[] = [
    { label: "総ギフト(ダイヤ)", value: formatNumber(stats.totalDiamonds), icon: <CoinIcon size={16} />, accent: "pink" },
    { label: "最高同接", value: formatNumber(stats.maxViewers), icon: <PeopleIcon size={16} />, accent: "cyan" },
    {
      label: "新規フォロー",
      value: formatNumber(stats.followCount),
      icon: <UserIcon size={16} />,
      accent: "success",
      badge: compareNewFollowers(stats.followCount),
    },
    { label: "コメント数", value: formatNumber(stats.commentCount), icon: <DocumentIcon size={16} />, accent: "purple" },
  ];

  const secondary: CardDef[] = [
    { label: "平均視聴者数", value: formatNumber(stats.avgViewers), icon: <PeopleIcon size={16} />, accent: "cyan" },
    { label: "総視聴者数", value: formatNumber(stats.totalUniqueViewers), icon: <PeopleIcon size={16} />, accent: "cyan" },
    {
      label: "バトル回数",
      value: formatNumber(stats.battleCount),
      icon: <PlayIcon size={16} />,
      accent: "warning",
      badge: compareBattleInterval(duration, stats.battleCount),
    },
    {
      label: "ギフト送信率",
      value: giftSendRate === null ? "-" : `${(giftSendRate * 100).toFixed(1)}%`,
      icon: <CoinIcon size={16} />,
      accent: "pink",
      badge: compareGiftSendRate(giftSendRate),
    },
    { label: "宝箱出現数", value: formatNumber(stats.treasureBoxCount), accent: "purple" },
    { label: "宝箱コイン合計", value: formatNumber(stats.totalTreasureBoxCoins), accent: "purple" },
    { label: "平均視聴時間", value: "-", icon: <ClockIcon size={16} />, accent: "muted", unavailableLabel: "計測不可" },
    { label: "タップスルー率", value: "-", accent: "muted", unavailableLabel: "計測不可" },
  ];

  return (
    <>
      <div className="kpi-grid">
        {primary.map((c) => (
          <KpiCard
            key={c.label}
            label={c.label}
            value={c.value}
            icon={c.icon}
            accent={c.accent}
            featured
            caption={c.badge ? <StatusBadge label={c.badge.label} tone={BASELINE_TONE[c.badge.status]} /> : undefined}
          />
        ))}
      </div>
      <div className="section-label">その他の指標</div>
      <div className="session-kpi-secondary">
        {secondary.map((c) => (
          <KpiCard
            key={c.label}
            label={c.label}
            value={c.value}
            icon={c.icon}
            accent={c.accent}
            caption={
              c.badge ? (
                <StatusBadge label={c.badge.label} tone={BASELINE_TONE[c.badge.status]} />
              ) : c.unavailableLabel ? (
                <StatusBadge label={c.unavailableLabel} tone="muted" />
              ) : undefined
            }
          />
        ))}
      </div>
    </>
  );
}
