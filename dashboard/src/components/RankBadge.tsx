import styles from "./RankBadge.module.css";

// Purely positional -- reflects the row's current place in whatever the
// table is sorted by right now (see DataTable's render(row, index)), not a
// stored ranking field. Top 3 get a colored medal circle; everyone else
// just gets a plain muted number, matching the reference mockups.
export function RankBadge({ index }: { index: number }) {
  const rank = index + 1;
  if (rank > 3) {
    return <span className={styles.plain}>{rank}</span>;
  }
  return <span className={`${styles.medal} ${styles[`rank${rank}`]}`}>{rank}</span>;
}
