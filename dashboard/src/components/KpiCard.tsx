import Link from "next/link";
import type { ReactNode } from "react";
import styles from "./KpiCard.module.css";

export type KpiAccent = "pink" | "cyan" | "purple" | "success" | "warning" | "muted";

export function KpiCard({
  label,
  value,
  icon,
  accent = "cyan",
  trend,
  caption,
  illustrationSrc,
  featured = false,
  href,
}: {
  label: ReactNode;
  value: ReactNode;
  icon?: ReactNode;
  accent?: KpiAccent;
  // e.g. "先月比" alongside a +/-N% figure -- omit entirely when there's
  // nothing to compare against yet (see queries.ts's _changePercent).
  trend?: { text: string; direction: "up" | "down" } | null;
  // Plain muted caption line (e.g. "アクティブ 6名", "先月比 ↑12.3%") --
  // unlike `trend`, this never renders as a colored pill. Callers embed
  // their own inline color if a figure within it should stand out.
  caption?: ReactNode;
  illustrationSrc?: string;
  featured?: boolean;
  href?: string;
}) {
  const content = (
    <>
      <div className={styles.top}>
        {icon && <span className={`${styles.icon} ${styles[accent]}`}>{icon}</span>}
        <span className={styles.label}>{label}</span>
      </div>
      <div className={styles.value}>{value}</div>
      {trend && (
        <span className={`${styles.trend} ${styles[trend.direction]}`}>
          {trend.direction === "up" ? "↑" : "↓"} {trend.text}
        </span>
      )}
      {caption && <div className={styles.caption}>{caption}</div>}
      {illustrationSrc && (
        // Static decorative art in public/ -- next/image's optimization
        // pipeline buys nothing here.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={illustrationSrc} alt="" className={styles.illustration} aria-hidden />
      )}
    </>
  );

  const className = `${styles.card} ${featured ? styles.featured : ""} ${styles[`accent-${accent}`]}`;

  if (href) {
    return (
      <Link href={href} className={className}>
        {content}
      </Link>
    );
  }
  return <div className={className}>{content}</div>;
}
