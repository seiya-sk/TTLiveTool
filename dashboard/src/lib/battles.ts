// battle_opponent only records the instant a battle/PK opponent was
// detected, not a start/end interval, and TikTok re-sends detection events
// repeatedly while a battle is ongoing -- so the raw event count wildly
// overstates the actual number of battles (session 9: 47 raw detections).
// This groups consecutive detections into battles: a gap of BATTLE_GAP_MS
// with no detection ends the current battle, and the next detection starts
// a new one. Everyone detected within one group is that battle's opponent
// (multi-opponent battles land in the same group). Used by both the L2 KPI
// card's battle count and the composite chart's battle lane so the two
// numbers never drift apart.
const BATTLE_GAP_MS = 5 * 60 * 1000;

export type BattleGroup = {
  startedAt: string;
  endedAt: string;
  opponentIds: string[];
};

export function groupBattleEvents(
  events: { opponentId: string | null; occurredAt: string }[]
): BattleGroup[] {
  const sorted = [...events].sort((a, b) => a.occurredAt.localeCompare(b.occurredAt));
  const groups: BattleGroup[] = [];
  let current: BattleGroup | null = null;
  let lastEventMs = -Infinity;

  for (const event of sorted) {
    const t = new Date(event.occurredAt).getTime();
    if (!current || t - lastEventMs > BATTLE_GAP_MS) {
      current = { startedAt: event.occurredAt, endedAt: event.occurredAt, opponentIds: [] };
      groups.push(current);
    }
    current.endedAt = event.occurredAt;
    if (event.opponentId && !current.opponentIds.includes(event.opponentId)) {
      current.opponentIds.push(event.opponentId);
    }
    lastEventMs = t;
  }

  return groups;
}
