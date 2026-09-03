// 指標の算出ルール。**DBに触れないモジュールにする。**
//
// ここに置く理由: この定数はクライアントコンポーネント(StreamersTable)からも
// 参照する。queries.ts に置くと、値のインポートを通じて better-sqlite3 が
// クライアントバンドルに引き込まれ、ビルドが落ちる(2026-09-03 に実際に発生)。
// 型だけのインポートは消えるので気づきにくく、値を1つ足した瞬間に壊れる。

/**
 * ダイヤ/時を算出する最低配信時間(分)。
 *
 * 30分にした根拠: セッションの中央値が77.5分なので、その半分未満の実績から
 * 時間あたりの率を語るのは無理がある。実測(2026-09-03)では77人中12人(16%)が
 * ここにかかり、そのうち最上位は11.1分1本で 59,814💎/時 -- 中央値1,247の48倍で
 * 首位に立っていた。
 */
export const MIN_MINUTES_FOR_RATE = 30;

/** 配信時間が下限未満なら null(「-」表示)。 */
export function diamondsPerHour(totalDiamonds: number, totalMinutes: number): number | null {
  if (!totalMinutes || totalMinutes < MIN_MINUTES_FOR_RATE) return null;
  return Math.round(totalDiamonds / (totalMinutes / 60));
}
