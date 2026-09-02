import styles from "./RankBadge.module.css";

// 表示中の並びに対する順位。DataTable が算出した meta.rank を受け取る
// (同値は同順位。1, 1, 3 のように次が飛ぶ)。
//
// ranked は「上位ほど良い並びか」。数値列を降順に並べているときだけ true で、
// そのときだけ上位3位にメダルを出す。
// 昇順や名前・日時での並べ替えでは順位の意味が変わるため、装飾しない --
// 最高同接の昇順で **最下位の行に金メダルが付く** という誤解を招く表示に
// 実際になっていた(2026-09-02 に確認)。
export function RankBadge({ rank, ranked = true }: { rank: number; ranked?: boolean }) {
  if (!ranked || rank > 3) {
    return <span className={styles.plain}>{rank}</span>;
  }
  return <span className={`${styles.medal} ${styles[`rank${rank}`]}`}>{rank}</span>;
}
