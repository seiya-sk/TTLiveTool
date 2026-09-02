import { SkeletonBlock, SkeletonRows, LoadingAnnouncement } from "@/components/Skeleton";

export default function Loading() {
  return (
    <div className="container">
      <LoadingAnnouncement label="通知設定を読み込んでいます" />
      <SkeletonBlock width="24%" height={26} style={{ marginBottom: 8 }} />
      <SkeletonBlock width="48%" height={14} style={{ marginBottom: 24 }} />
      <SkeletonRows rows={8} cols={4} />
    </div>
  );
}
