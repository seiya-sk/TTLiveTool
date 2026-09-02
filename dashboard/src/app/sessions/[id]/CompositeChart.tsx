"use client";

import { useMemo, useState, type ReactNode } from "react";
import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  ReferenceDot,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  elapsedMinutes,
  durationMinutes,
  formatJst,
  formatJstHm,
  formatMinutes,
  formatNumber,
  formatUnixSecondsJst,
  basenameFromPath,
} from "@/lib/format";
import type { BattleGroup } from "@/lib/battles";
import type { BucketPoint, ScreenshotRow, SessionStats, TreasureBoxRow, ViewerBucketPoint } from "@/lib/queries";

// Each metric (viewers/comments/gifts) previously shared one overlaid chart;
// with three metrics at very different scales (tens of viewers vs hundreds
// of thousands of diamonds) that made the smaller series unreadable next to
// the larger ones. Split into three independently-scaled panels instead --
// see sample/UI/ライブ詳細.png's 配信推移 tab.
const PANEL_HEIGHT = 170;
// A lane only ever draws one row of dot markers -- 60px left a lot of dead
// vertical space above/below a single dot; this is just enough for the dot
// plus its top/bottom margin (see SHARED_MARGIN) without clipping.
const LANE_HEIGHT = 26;

// Alignment across the panels/lanes relies on every chart sharing the same X
// domain/type AND the same left Y-axis width (set explicitly below, even
// where the axis is hidden) -- recharts has no built-in "synced small
// multiples" primitive, so identical margins/axis widths is what keeps a
// given elapsed-minute lined up vertically across rows. Each of the three
// main panels now renders its own visible X-axis (mockup repeats the time
// labels under every panel), so unlike before there's no separate
// axis-only row to keep in sync -- just this one shared width constant.
//
// AXIS_WIDTH_LEFT needs enough room for the widest tick labels among the
// three panels (viewer count up to 5 digits, e.g. "10,450", and the gift
// axis's "100K" log-scale labels).
const AXIS_WIDTH_LEFT = 68;
const AXIS_WIDTH_RIGHT = 60;
const SHARED_MARGIN = { top: 6, right: 20, left: 10, bottom: 4 };

// The viewer/gift panels draw a PeakChip annotation ABOVE their peak
// point(s) -- recharts clips anything drawn outside the chart's own SVG
// canvas, so a plain top:6 margin let a tall chip get cut off right at the
// panel's top edge (confirmed on session 9's largest gift spikes). Widening
// margin.top reserves real pixel space above the plot for the chip
// regardless of where the peak sits in the Y domain; panel height grows by
// the same amount so the plot area itself doesn't shrink. 1-line chip
// (viewer) needs ~35px (23px box + 12px gap to the dot); 2-line chip
// (gift: time + amount) needs ~48px -- both rounded up with a small buffer.
const VIEWER_MARGIN = { ...SHARED_MARGIN, top: 40 };
const GIFT_MARGIN = { ...SHARED_MARGIN, top: 54 };
const VIEWER_PANEL_HEIGHT = PANEL_HEIGHT + (VIEWER_MARGIN.top - SHARED_MARGIN.top);
const GIFT_PANEL_HEIGHT = PANEL_HEIGHT + (GIFT_MARGIN.top - SHARED_MARGIN.top);

// Peaks closer together than this fraction of the whole session get
// de-duplicated (see giftPeaks below) -- otherwise session 9's large gifts,
// which cluster in bursts, produced overlapping annotation boxes.
const MIN_PEAK_GAP_RATIO = 0.05;

// Ticks are evenly spaced across the whole session so the start, the middle
// and the end are all readable. The previous whole-hour-only placement
// (ticks at 0, 60, 120, ...) collapsed to a SINGLE tick for any session
// shorter than an hour -- and most sessions are: 6 of the 8 most recent
// completed sessions ran under 60 minutes, so the axis showed nothing but
// the start time and the stream's shape couldn't be read off it at all.
//
// The step is picked from a "nice" ladder rather than dividing the duration
// by N, so labels land on round clock times (16:20, 16:30, ...) instead of
// arbitrary ones like 16:23. TICK_TARGET is an upper bound, not an exact
// count -- the first step that fits within it wins.
//
// Pairing with the XAxis's `interval={0}` below is required: recharts'
// default interval applies its own label-collision culling even to an
// explicit `ticks` array, and silently dropped the tick nearest the
// domain's left edge (confirmed empirically -- with the default interval,
// ticks=[0, 60, ...] rendered only the 60+ ticks, no DOM node for 0 at all,
// regardless of the actual pixel gap between them). interval={0} disables
// that culling so every provided tick renders.
const TICK_TARGET = 6;
const NICE_STEPS_MIN = [1, 2, 5, 10, 15, 20, 30, 60, 90, 120, 180, 240, 360, 480];

function timeTicks(durationMin: number): number[] {
  const span = Math.max(1, Math.ceil(durationMin));
  const step =
    NICE_STEPS_MIN.find((candidate) => span / candidate <= TICK_TARGET - 1) ??
    Math.ceil(span / (TICK_TARGET - 1));

  const ticks: number[] = [];
  for (let m = 0; m <= span; m += step) ticks.push(m);

  // The nice-step ladder rarely divides the duration exactly, so the last
  // tick usually sits short of the end -- add the true end so the session's
  // finish time is always readable, unless doing so would crowd the tick
  // before it (< half a step apart, where the two labels would collide).
  const last = ticks[ticks.length - 1];
  if (span - last >= step / 2) ticks.push(span);

  return ticks;
}

// Every tick shows the wall-clock time it corresponds to. The axis used to
// label elapsed hours ("1時間", "2時間"), which answers "how long in" but
// not "when" -- and "when" is what a tick is being read for when lining an
// event up against a chat log or a recording. Elapsed time is still shown
// in every tooltip via formatMinutes, so nothing is lost.
function formatAxisTick(minutes: number, startedAt: string): string {
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return "-";
  return formatJstHm(new Date(start + minutes * 60000).toISOString());
}

// Battle/screenshot/treasure-box markers all render as the same dot shape,
// distinguished only by color -- a shared visual language across lanes
// instead of a different glyph per lane.
function LaneDot({
  cx,
  cy,
  color,
  onClick,
}: {
  cx?: number;
  cy?: number;
  color: string;
  onClick?: () => void;
}) {
  if (cx === undefined || cy === undefined) return null;
  return (
    <circle
      cx={cx}
      cy={cy}
      r={5}
      fill={color}
      stroke="#fff"
      strokeWidth={1.5}
      style={onClick ? { cursor: "pointer" } : undefined}
      onClick={onClick}
    />
  );
}

// A Scatter point carries two tooltip payload entries by default -- one for
// its x value, one for its y value -- so a `formatter` prop (which runs
// once per entry) renders whatever it returns twice. A custom `content`
// renderer sidesteps that entirely: it gets called once per hover and we
// pull the original data point straight off `payload[0].payload`.
function LaneTooltip<T>({
  active,
  label,
  payload,
  render,
}: {
  active?: boolean;
  label?: number | string;
  payload?: readonly { payload?: T }[];
  render: (point: T) => ReactNode;
}) {
  const point = payload?.[0]?.payload;
  if (!active || point === undefined) return null;
  return (
    <div className="lane-tooltip">
      <div className="lane-tooltip-time">{formatMinutes(Number(label))}</div>
      {render(point)}
    </div>
  );
}

function ScreenshotMarker(
  props: { cx?: number; cy?: number; payload?: { screenshot?: ScreenshotRow } },
  onSelect: (s: ScreenshotRow) => void
) {
  const { cx, cy, payload } = props;
  if (!payload?.screenshot) return null;
  return <LaneDot cx={cx} cy={cy} color={SCREENSHOT_COLOR} onClick={() => onSelect(payload.screenshot!)} />;
}

// Small floating annotation box used to call out the viewer-count peak and
// the top gift spikes (see sample/UI/ライブ詳細.png) -- recharts' Reference*
// components only draw plain text via their built-in `label`, so this is a
// custom SVG chip (rounded rect + centered text lines) rendered through
// that same `label` slot. Recharts clones the element and injects `viewBox`
// (the dot's pixel position) alongside whatever props were already set.
function PeakChip({
  viewBox,
  x,
  y,
  lines,
  color,
}: {
  viewBox?: { x?: number; y?: number };
  x?: number;
  y?: number;
  lines: string[];
  color: string;
}) {
  const cx = viewBox?.x ?? x;
  const cy = viewBox?.y ?? y;
  if (cx === undefined || cy === undefined) return null;
  const width = Math.max(...lines.map((l) => l.length)) * 6.4 + 14;
  const height = lines.length * 13 + 10;
  const boxX = cx - width / 2;
  const boxY = cy - height - 12;
  return (
    <g>
      <rect x={boxX} y={boxY} width={width} height={height} rx={6} fill="#11161fe6" stroke={color} strokeWidth={1} />
      {lines.map((line, i) => (
        <text
          key={i}
          x={cx}
          y={boxY + 13 + i * 13}
          textAnchor="middle"
          fontSize={10}
          fontWeight={i === lines.length - 1 ? 700 : 500}
          fill={i === lines.length - 1 ? color : "var(--muted)"}
        >
          {line}
        </text>
      ))}
    </g>
  );
}

type SeriesToggle = {
  viewers: boolean;
  comments: boolean;
  gifts: boolean;
  battles: boolean;
  screenshots: boolean;
  treasureBoxes: boolean;
};

// Battle/screenshot/treasure-box each get a clearly distinct hue -- design
// tokens from globals.css so the chart matches the dark/neon theme.
const VIEWERS_COLOR = "var(--cyan)";
const COMMENTS_COLOR = "var(--success)";
const GIFTS_COLOR = "var(--pink)";
const BATTLE_COLOR = "var(--warning)";
const SCREENSHOT_COLOR = "var(--cyan)";
const TREASURE_BOX_COLOR = "var(--purple)";

const TOGGLE_LABELS: { key: keyof SeriesToggle; label: string; color: string }[] = [
  { key: "viewers", label: "視聴者数", color: VIEWERS_COLOR },
  { key: "comments", label: "コメント数", color: COMMENTS_COLOR },
  { key: "gifts", label: "ギフト(ダイヤ・対数)", color: GIFTS_COLOR },
  { key: "battles", label: "バトル", color: BATTLE_COLOR },
  { key: "screenshots", label: "スクショ", color: SCREENSHOT_COLOR },
  { key: "treasureBoxes", label: "宝箱", color: TREASURE_BOX_COLOR },
];

// How many gift buckets get a peak-amount callout (matches the mockup's ~5
// labeled spikes).
const GIFT_PEAK_COUNT = 5;

export function CompositeChart({
  startedAt,
  endedAt,
  viewerSeries,
  commentSeries,
  giftSeries,
  battleGroups,
  screenshots,
  treasureBoxes,
  stats,
}: {
  startedAt: string;
  endedAt: string | null;
  viewerSeries: ViewerBucketPoint[];
  commentSeries: BucketPoint[];
  giftSeries: BucketPoint[];
  battleGroups: BattleGroup[];
  screenshots: ScreenshotRow[];
  treasureBoxes: TreasureBoxRow[];
  stats: SessionStats;
}) {
  const [show, setShow] = useState<SeriesToggle>({
    viewers: true,
    comments: true,
    gifts: true,
    battles: true,
    screenshots: true,
    treasureBoxes: true,
  });
  const [selected, setSelected] = useState<ScreenshotRow | null>(null);

  const durationMin = durationMinutes(startedAt, endedAt);
  const xDomain: [number, number] = [0, Math.max(1, Math.ceil(durationMin))];
  const xTicks = useMemo(() => timeTicks(durationMin), [durationMin]);
  const xAxisProps = {
    dataKey: "elapsedMin" as const,
    type: "number" as const,
    domain: xDomain,
    ticks: xTicks,
    interval: 0 as const,
    tickFormatter: (m: number) => formatAxisTick(m, startedAt),
  };

  const mergedData = useMemo(() => {
    const viewerMap = new Map(viewerSeries.map((d) => [d.minute, d.avgViewers]));
    const commentMap = new Map(commentSeries.map((d) => [d.minute, d.value]));
    const giftMap = new Map(giftSeries.map((d) => [d.minute, d.value]));
    const minutes = new Set<string>([...viewerMap.keys(), ...commentMap.keys(), ...giftMap.keys()]);
    return Array.from(minutes)
      .sort()
      .map((minute) => {
        const gift = giftMap.get(minute) ?? 0;
        return {
          minute,
          elapsedMin: elapsedMinutes(minute, startedAt),
          viewers: viewerMap.get(minute) ?? null,
          comments: commentMap.get(minute) ?? 0,
          diamonds: gift > 0 ? gift : null,
        };
      });
  }, [viewerSeries, commentSeries, giftSeries, startedAt]);

  // Peak of the *charted* per-minute-average series -- deliberately NOT
  // labeled "最高同接" (that KPI is the true instantaneous max from raw
  // events, a different number from this bucketed average; see
  // CompositeChart's caller for why the two aren't interchangeable).
  const viewerPeak = useMemo(() => {
    return mergedData.reduce<{ elapsedMin: number; viewers: number } | null>((best, d) => {
      if (d.viewers === null) return best;
      if (!best || d.viewers > best.viewers) return { elapsedMin: d.elapsedMin, viewers: d.viewers };
      return best;
    }, null);
  }, [mergedData]);

  // Top N one-minute gift totals -- a bucket, not a single gift, so the
  // callout is time + diamond total for that minute rather than "a gift
  // worth X". Candidates are walked largest-first and a candidate is
  // skipped if it falls within MIN_PEAK_GAP_RATIO of the session's total
  // duration of an already-picked (larger) peak, so a burst of big gifts a
  // few minutes apart doesn't produce overlapping annotation boxes -- this
  // can leave fewer than GIFT_PEAK_COUNT peaks when spikes cluster tightly.
  const giftPeaks = useMemo(() => {
    const minGapMinutes = durationMin * MIN_PEAK_GAP_RATIO;
    const candidates = mergedData
      .filter((d): d is typeof mergedData[number] & { diamonds: number } => d.diamonds !== null)
      .sort((a, b) => b.diamonds - a.diamonds);
    const picked: typeof candidates = [];
    for (const candidate of candidates) {
      if (picked.length >= GIFT_PEAK_COUNT) break;
      if (picked.some((p) => Math.abs(p.elapsedMin - candidate.elapsedMin) < minGapMinutes)) continue;
      picked.push(candidate);
    }
    return picked;
  }, [mergedData, durationMin]);

  // One marker per grouped battle (see lib/battles.ts), positioned at the
  // battle's start -- not one per raw detection event, which over-counts
  // (session 9: 47 raw detections collapse to far fewer actual battles).
  const battleData = useMemo(
    () =>
      battleGroups.map((g) => ({
        elapsedMin: elapsedMinutes(g.startedAt, startedAt),
        y: 0.5,
        opponentIds: g.opponentIds,
      })),
    [battleGroups, startedAt]
  );

  const screenshotData = useMemo(
    () =>
      screenshots.map((s) => ({
        elapsedMin: elapsedMinutes(s.capturedAt, startedAt),
        y: 0.5,
        screenshot: s,
      })),
    [screenshots, startedAt]
  );

  // Plotted at send time (occurredAt); open_at (when it becomes openable) is
  // surfaced in the tooltip instead of a second marker -- see
  // tiktok_monitor/events.py's Treasure Box comment for why both timestamps
  // exist.
  const treasureBoxData = useMemo(
    () =>
      treasureBoxes.map((t) => ({
        elapsedMin: elapsedMinutes(t.occurredAt, startedAt),
        y: 0.5,
        treasureBox: t,
      })),
    [treasureBoxes, startedAt]
  );

  if (mergedData.length === 0) {
    return <p className="empty">データがありません。</p>;
  }

  return (
    <div>
      <div className="series-toggle">
        {TOGGLE_LABELS.map(({ key, label, color }) => (
          <label key={key} className="series-toggle-item">
            <input
              type="checkbox"
              checked={show[key]}
              onChange={(e) => setShow((prev) => ({ ...prev, [key]: e.target.checked }))}
            />
            <span className="series-toggle-swatch" style={{ background: color }} />
            {label}
          </label>
        ))}
      </div>

      {show.viewers && (
        <div className="chart-panel">
          <div className="chart-panel-stats">
            <span className="chart-panel-dot" style={{ background: VIEWERS_COLOR }} />
            <div className="chart-panel-title">視聴者数</div>
            <div className="chart-panel-metric">平均{formatNumber(stats.avgViewers)}人</div>
            <div className="chart-panel-metric-strong" style={{ color: VIEWERS_COLOR }}>
              最高同接{formatNumber(stats.maxViewers)}人
            </div>
          </div>
          <div className="chart-panel-plot">
            <ResponsiveContainer width="100%" height={VIEWER_PANEL_HEIGHT}>
              <ComposedChart data={mergedData} margin={VIEWER_MARGIN}>
                <defs>
                  <linearGradient id="viewerFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor={VIEWERS_COLOR} stopOpacity={0.35} />
                    <stop offset="95%" stopColor={VIEWERS_COLOR} stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
                <XAxis {...xAxisProps} />
                <YAxis allowDecimals={false} width={AXIS_WIDTH_LEFT} />
                <Tooltip
                  labelFormatter={(v) => formatMinutes(Number(v))}
                  formatter={(v) => [`${formatNumber(Number(v))}人`, "視聴者数"]}
                />
                <Area
                  type="monotone"
                  dataKey="viewers"
                  stroke={VIEWERS_COLOR}
                  strokeWidth={2}
                  fill="url(#viewerFill)"
                  dot={false}
                  name="視聴者数"
                  connectNulls
                />
                {viewerPeak && (
                  <ReferenceDot
                    x={viewerPeak.elapsedMin}
                    y={viewerPeak.viewers}
                    r={4}
                    fill={VIEWERS_COLOR}
                    stroke="#fff"
                    strokeWidth={1.5}
                    label={<PeakChip lines={[`ピーク${formatNumber(viewerPeak.viewers)}人`]} color={VIEWERS_COLOR} />}
                  />
                )}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {show.comments && (
        <div className="chart-panel">
          <div className="chart-panel-stats">
            <span className="chart-panel-dot" style={{ background: COMMENTS_COLOR }} />
            <div className="chart-panel-title">コメント数</div>
            <div className="chart-panel-metric-strong" style={{ color: COMMENTS_COLOR }}>
              合計{formatNumber(stats.commentCount)}件
            </div>
          </div>
          <div className="chart-panel-plot">
            <ResponsiveContainer width="100%" height={PANEL_HEIGHT}>
              <ComposedChart data={mergedData} margin={SHARED_MARGIN}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
                <XAxis {...xAxisProps} />
                <YAxis allowDecimals={false} domain={[0, "auto"]} width={AXIS_WIDTH_LEFT} />
                <Tooltip
                  labelFormatter={(v) => formatMinutes(Number(v))}
                  formatter={(v) => [`${formatNumber(Number(v))}件`, "コメント数"]}
                />
                <Bar dataKey="comments" fill={COMMENTS_COLOR} fillOpacity={0.75} name="コメント数" barSize={3} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {show.gifts && (
        <div className="chart-panel">
          <div className="chart-panel-stats">
            <span className="chart-panel-dot" style={{ background: GIFTS_COLOR }} />
            <div className="chart-panel-title">ギフト(ダイヤ)</div>
            <div className="chart-panel-metric-strong" style={{ color: GIFTS_COLOR }}>
              合計{formatNumber(stats.totalDiamonds)}ダイヤ
            </div>
          </div>
          <div className="chart-panel-plot">
            <ResponsiveContainer width="100%" height={GIFT_PANEL_HEIGHT}>
              <ComposedChart data={mergedData} margin={GIFT_MARGIN}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.25} />
                <XAxis {...xAxisProps} />
                <YAxis scale="log" domain={[1, "auto"]} allowDataOverflow width={AXIS_WIDTH_LEFT} />
                <Tooltip
                  labelFormatter={(v) => formatMinutes(Number(v))}
                  formatter={(v) => [`${formatNumber(Number(v))}ダイヤ`, "ギフト"]}
                />
                <Bar dataKey="diamonds" fill={GIFTS_COLOR} name="ギフト(ダイヤ)" barSize={2} />
                <Scatter
                  data={mergedData.filter((d) => d.diamonds !== null)}
                  dataKey="diamonds"
                  shape={(props: { cx?: number; cy?: number }) => (
                    <circle cx={props.cx} cy={props.cy} r={2.5} fill={GIFTS_COLOR} />
                  )}
                  name="ギフト(ダイヤ)"
                  legendType="none"
                />
                {giftPeaks.map((p) => (
                  <ReferenceDot
                    key={p.minute}
                    x={p.elapsedMin}
                    y={p.diamonds}
                    r={0}
                    label={<PeakChip lines={[formatJstHm(p.minute), `${formatNumber(p.diamonds)}ダイヤ`]} color={GIFTS_COLOR} />}
                  />
                ))}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {show.battles && (
        <div className="lane-block lane-block-battle">
          <div className="lane-label">バトル検知({battleGroups.length}件)</div>
          <ResponsiveContainer width="100%" height={LANE_HEIGHT}>
            <ComposedChart data={battleData} margin={SHARED_MARGIN}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
              <XAxis dataKey="elapsedMin" type="number" domain={xDomain} hide />
              <YAxis hide domain={[0, 1]} width={AXIS_WIDTH_LEFT} />
              <YAxis yAxisId="right" orientation="right" hide domain={[0, 1]} width={AXIS_WIDTH_RIGHT} />
              <Tooltip
                content={(props: {
                  active?: boolean;
                  label?: number | string;
                  payload?: readonly { payload?: { opponentIds: string[] } }[];
                }) => (
                  <LaneTooltip
                    {...props}
                    render={(point) => (
                      <>
                        <div className="lane-tooltip-label">相手:</div>
                        {point.opponentIds.length > 0 ? (
                          <ul className="lane-tooltip-list">
                            {point.opponentIds.map((id) => (
                              <li key={id}>{id}</li>
                            ))}
                          </ul>
                        ) : (
                          <div>不明</div>
                        )}
                      </>
                    )}
                  />
                )}
              />
              <Scatter
                data={battleData}
                dataKey="y"
                shape={(props: { cx?: number; cy?: number }) => <LaneDot {...props} color={BATTLE_COLOR} />}
                name="バトル検知"
                legendType="none"
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}

      {show.screenshots && (
        <div className="lane-block lane-block-screenshot">
          <div className="lane-label">スクショ撮影({screenshots.length}件)</div>
          <ResponsiveContainer width="100%" height={LANE_HEIGHT}>
            <ComposedChart data={screenshotData} margin={SHARED_MARGIN}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
              <XAxis dataKey="elapsedMin" type="number" domain={xDomain} hide />
              <YAxis hide domain={[0, 1]} width={AXIS_WIDTH_LEFT} />
              <YAxis yAxisId="right" orientation="right" hide domain={[0, 1]} width={AXIS_WIDTH_RIGHT} />
              <Tooltip
                content={(props: {
                  active?: boolean;
                  label?: number | string;
                  payload?: readonly { payload?: unknown }[];
                }) => (
                  <LaneTooltip {...props} render={() => <div className="lane-tooltip-label">クリックでプレビュー</div>} />
                )}
              />
              <Scatter
                data={screenshotData}
                dataKey="y"
                shape={(props: { cx?: number; cy?: number; payload?: { screenshot?: ScreenshotRow } }) =>
                  ScreenshotMarker(props, setSelected)
                }
                name="スクショ"
                legendType="none"
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}

      {show.treasureBoxes && (
        <div className="lane-block lane-block-treasure-box">
          <div className="lane-label">宝箱({treasureBoxes.length}件)</div>
          <ResponsiveContainer width="100%" height={LANE_HEIGHT}>
            <ComposedChart data={treasureBoxData} margin={SHARED_MARGIN}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
              <XAxis dataKey="elapsedMin" type="number" domain={xDomain} hide />
              <YAxis hide domain={[0, 1]} width={AXIS_WIDTH_LEFT} />
              <YAxis yAxisId="right" orientation="right" hide domain={[0, 1]} width={AXIS_WIDTH_RIGHT} />
              <Tooltip
                content={(props: {
                  active?: boolean;
                  label?: number | string;
                  payload?: readonly { payload?: { treasureBox: TreasureBoxRow } }[];
                }) => (
                  <LaneTooltip
                    {...props}
                    render={(point) => (
                      <>
                        <div>コイン数: {point.treasureBox.coins ?? "-"}</div>
                        <div>開封可能人数: {point.treasureBox.winnerHeadcount ?? "-"}</div>
                        <div>送信者: {point.treasureBox.senderNickname ?? "不明"}</div>
                        <div>開封予定時刻: {formatUnixSecondsJst(point.treasureBox.openAt)}</div>
                      </>
                    )}
                  />
                )}
              />
              <Scatter
                data={treasureBoxData}
                dataKey="y"
                shape={(props: { cx?: number; cy?: number }) => <LaneDot {...props} color={TREASURE_BOX_COLOR} />}
                name="宝箱"
                legendType="none"
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}

      {selected && (
        <div className="screenshot-preview">
          <div className="screenshot-preview-header">
            <span>撮影時刻: {formatJst(selected.capturedAt)}</span>
            <button type="button" onClick={() => setSelected(null)}>
              閉じる
            </button>
          </div>
          {/* eslint-disable-next-line @next/next/no-img-element -- filesystem image served via a route handler, not a static/public asset next/image can optimize */}
          <img src={`/api/screenshots/${basenameFromPath(selected.imagePath)}`} alt="配信スクリーンショット" />
        </div>
      )}
    </div>
  );
}
