import { SkeletonBlock, SkeletonRows, LoadingAnnouncement } from "@/components/Skeleton";

export default function Loading() {
  return (
    <div className="container">
      <LoadingAnnouncement label="ライバー一覧を読み込んでいます" />
      <SkeletonBlock width="22%" height={26} style={{ marginBottom: 8 }} />
      <SkeletonBlock width="44%" height={14} style={{ marginBottom: 24 }} />
      <SkeletonRows rows={12} cols={5} />
    </div>
  );
}
