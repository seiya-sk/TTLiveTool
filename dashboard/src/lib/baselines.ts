// Numeric baselines from criteria/winning_patterns.md's "指標・目標の目安"
// section, hardcoded because that file is free-form Markdown the user edits
// directly (see dashboard-ui-design.md real-data-findings note). If the
// user changes the source numbers there, update these to match manually --
// there is no runtime parser tying the two together.
//
// Two baselines from that section (平均視聴時間 1〜1.5分, タップスルー率
// 25%) are intentionally NOT included here: the current data model has no
// per-viewer leave-time tracking, so they cannot be computed from
// live_events. Badges for those metrics must show "計測不可", never a
// fabricated number.
export const BASELINES = {
  giftSendRate: 0.025, // ギフト送信率 目安2.5%前後 (unique_gifters / unique_visitors)
  newFollowers: 10, // 新規フォロワー 目安10人/配信
  battleIntervalMinutes: [40, 50] as [number, number], // バトル頻度 目安40〜50分に1回
};

export type BaselineStatus = "good" | "warn" | "unavailable";
export type BaselineResult = { status: BaselineStatus; label: string };

export function compareGiftSendRate(rate: number | null): BaselineResult {
  if (rate === null) return { status: "unavailable", label: "計測不可" };
  const target = BASELINES.giftSendRate;
  return rate >= target
    ? { status: "good", label: `目安${(target * 100).toFixed(1)}%以上` }
    : { status: "warn", label: `目安${(target * 100).toFixed(1)}%未達` };
}

export function compareNewFollowers(count: number): BaselineResult {
  const target = BASELINES.newFollowers;
  return count >= target
    ? { status: "good", label: `目安${target}人以上` }
    : { status: "warn", label: `目安${target}人未達` };
}

export function compareBattleInterval(durationMinutes: number, battleCount: number): BaselineResult {
  if (battleCount === 0) return { status: "unavailable", label: "バトルなし" };
  const [min, max] = BASELINES.battleIntervalMinutes;
  const intervalMinutes = durationMinutes / battleCount;
  return intervalMinutes <= max
    ? { status: "good", label: `目安${min}〜${max}分に1回` }
    : { status: "warn", label: `目安${min}〜${max}分に1回` };
}
