// Small stroke-style icon set shared across Sidebar/KpiCard/table columns.
// Deliberately plain hand-drawn SVG (not an icon library dependency) --
// each one is used in 1-2 places, matching the line-icon style in
// sample/UI's reference mockups.

type IconProps = { size?: number };

export function HomeIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
      <path d="M3 9.5 10 3l7 6.5" strokeLinecap="round" />
      <path d="M5 8.5V17h10V8.5" />
    </svg>
  );
}

export function UserIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="10" cy="7" r="3.2" />
      <path d="M3.5 17c1-3.5 4-5 6.5-5s5.5 1.5 6.5 5" strokeLinecap="round" />
    </svg>
  );
}

export function PlayIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6">
      <rect x="2.5" y="4" width="15" height="12" rx="3" />
      <path d="M8.5 7.5v5l4.5-2.5-4.5-2.5Z" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function CoinIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="10" cy="10" r="7" />
      <path d="M10 6.5v7M8 12.2c0 .9.9 1.3 2 1.3s2-.5 2-1.4-1-1.2-2-1.4-2-.5-2-1.4.9-1.4 2-1.4 1.7.3 1.9.9" strokeLinecap="round" />
    </svg>
  );
}

export function DbIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6">
      <ellipse cx="10" cy="5" rx="6.5" ry="2.3" />
      <path d="M3.5 5v10c0 1.27 2.9 2.3 6.5 2.3s6.5-1.03 6.5-2.3V5" />
      <path d="M3.5 10c0 1.27 2.9 2.3 6.5 2.3s6.5-1.03 6.5-2.3" />
    </svg>
  );
}

export function PeopleIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="7" cy="7" r="2.6" />
      <circle cx="14" cy="8" r="2.1" />
      <path d="M2.5 17c.7-2.8 2.6-4.2 4.5-4.2s3.8 1.4 4.5 4.2" strokeLinecap="round" />
      <path d="M12.5 13.2c1.6.2 2.9 1.4 3.5 3.8" strokeLinecap="round" />
    </svg>
  );
}

export function GearIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="10" cy="10" r="2.6" />
      <path
        d="M10 3.5v1.6M10 14.9v1.6M16.5 10h-1.6M5.1 10H3.5M14.6 5.4l-1.1 1.1M6.5 13.5l-1.1 1.1M14.6 14.6l-1.1-1.1M6.5 6.5 5.4 5.4"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function DiamondIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
      <path d="M4 8.5 10 17l6-8.5-3-4H7l-3 4Z" />
      <path d="M4 8.5h12M7.5 4.5 6 8.5l4 8.5M12.5 4.5 14 8.5l-4 8.5" strokeLinecap="round" />
    </svg>
  );
}

export function ClockIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="10" cy="10" r="7" />
      <path d="M10 6v4.5l3 2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function PlusIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M10 4v12M4 10h12" strokeLinecap="round" />
    </svg>
  );
}

export function DocumentIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
      <path d="M6 2.5h6l3 3V17a.5.5 0 0 1-.5.5h-9A.5.5 0 0 1 5 17V3a.5.5 0 0 1 1-.5Z" />
      <path d="M12 2.5V6h3M7.5 10h5M7.5 13h5" strokeLinecap="round" />
    </svg>
  );
}

export function ArrowDownIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.7">
      <path d="M10 3.5v11M5.5 10.5 10 15l4.5-4.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function ArrowUpIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.7">
      <path d="M10 16.5v-11M5.5 9.5 10 5l4.5 4.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function ImageIcon({ size = 18 }: IconProps) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
      <rect x="2.5" y="3.5" width="15" height="13" rx="2" />
      <circle cx="7" cy="8" r="1.4" />
      <path d="M3 15.5 8 10l3 3 2.5-2.5L17 14" strokeLinecap="round" />
    </svg>
  );
}
