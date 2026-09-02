import { SkeletonBlock, SkeletonCards, SkeletonRows, LoadingAnnouncement } from "@/components/Skeleton";

export default function Loading() {
  return (
    <div className="container">
      <LoadingAnnouncement label="ライバー詳細を読み込んでいます" />
      <SkeletonBlock width={140} height={14} style={{ marginBottom: 16 }} />
      <div style={{ display: "flex", gap: 16, alignItems: "center", marginBottom: 24 }}>
        <SkeletonBlock width={64} height={64} style={{ borderRadius: "50%", flexShrink: 0 }} />
        <div style={{ flex: 1 }}>
          <SkeletonBlock width="34%" height={26} style={{ marginBottom: 8 }} />
          <SkeletonBlock width="20%" height={14} />
        </div>
      </div>
      <SkeletonCards count={6} />
      <SkeletonBlock width="14%" height={18} style={{ marginBottom: 12 }} />
      <SkeletonRows rows={10} cols={5} />
    </div>
  );
}
