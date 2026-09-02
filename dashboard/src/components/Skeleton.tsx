// ローディング中に出す骨格。スピナーではなくレイアウトの形を見せる。
// 「何が来るか」が先に分かるほうが体感が速く、ページ間の移動でも
// 位置感覚が保たれる。
//
// aria-hidden にしているのは、これが視覚的な繋ぎでしかないため。
// 読み上げ環境には loading.tsx 側の role="status" が「読み込み中」を
// 一度だけ伝えるので、意味のない矩形を大量に読ませない。

export function SkeletonBlock({ width = "100%", height = 16, style }: {
  width?: string | number;
  height?: string | number;
  style?: React.CSSProperties;
}) {
  return <span className="skeleton" aria-hidden="true" style={{ width, height, ...style }} />;
}

export function SkeletonRows({ rows = 8, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div aria-hidden="true">
      {Array.from({ length: rows }).map((_, r) => (
        <div className="skeleton-row" key={r}>
          {Array.from({ length: cols }).map((_, c) => (
            <SkeletonBlock key={c} width={c === 0 ? "28%" : `${Math.floor(60 / (cols - 1))}%`} />
          ))}
        </div>
      ))}
    </div>
  );
}

export function SkeletonCards({ count = 4, height = 84 }: { count?: number; height?: number }) {
  return (
    <div className="skeleton-grid" aria-hidden="true">
      {Array.from({ length: count }).map((_, i) => (
        <SkeletonBlock key={i} height={height} />
      ))}
    </div>
  );
}

/** 画面読み上げ向けの一度きりの通知。視覚的には骨格が担う。 */
export function LoadingAnnouncement({ label }: { label: string }) {
  return (
    <span role="status" aria-live="polite" className="sr-only">
      {label}
    </span>
  );
}
