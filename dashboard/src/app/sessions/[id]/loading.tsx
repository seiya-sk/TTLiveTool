import { SkeletonBlock, SkeletonCards, SkeletonRows, LoadingAnnouncement } from "@/components/Skeleton";

// ライブ詳細は KPI・グラフ・表と縦に長い。骨格を出しておくと、
// 読み込み中でも「どこに何があるか」が分かり、スクロール位置の
// 感覚も保たれる。
export default function Loading() {
  return (
    <div className="container">
      <LoadingAnnouncement label="ライブ詳細を読み込んでいます" />
      <SkeletonBlock width={160} height={14} style={{ marginBottom: 16 }} />
      <SkeletonBlock width="42%" height={26} style={{ marginBottom: 8 }} />
      <SkeletonBlock width="26%" height={14} style={{ marginBottom: 24 }} />
      <SkeletonCards count={5} />
      <SkeletonBlock height={280} style={{ marginBottom: 24 }} />
      <SkeletonBlock width="18%" height={18} style={{ marginBottom: 12 }} />
      <SkeletonRows rows={8} cols={5} />
    </div>
  );
}
