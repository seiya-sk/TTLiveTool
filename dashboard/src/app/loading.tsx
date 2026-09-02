import { SkeletonBlock, SkeletonCards, SkeletonRows, LoadingAnnouncement } from "@/components/Skeleton";

export default function Loading() {
  return (
    <div className="container">
      <LoadingAnnouncement label="ダッシュボードを読み込んでいます" />
      <SkeletonBlock width="24%" height={26} style={{ marginBottom: 8 }} />
      <SkeletonBlock width="40%" height={14} style={{ marginBottom: 24 }} />
      <SkeletonCards count={4} />
      <SkeletonRows rows={8} cols={4} />
    </div>
  );
}
