// ライブ詳細の「戻る」先を、遷移元に応じて決める。
//
// ライブ詳細には複数の入口がある:
//   - ライバー詳細の配信一覧      -> そのライバーのページに戻したい
//   - ライブ一覧(ライバーで絞り込み中) -> 絞り込んだ一覧に戻したい
//   - ライブ一覧(全件)/ 直接URL     -> 全件の一覧へ
// 以前は入口によらず常に /sessions へ戻っており、ライバー詳細から入ると
// 全ライバーの一覧に飛ばされて、元の文脈を失っていた。
//
// **遷移元は列挙値だけを受け取り、URLは受け取らない。** クエリパラメータで
// 戻り先URLをそのまま渡すと、任意のリンク先を差し込めてしまう
// (フィッシングの踏み台になる)。ここでは "streamer" / "filtered" という
// 印だけを受け取り、実際のURLはセッション自身のデータから組み立てる。

export type SessionOrigin = "streamer" | "filtered" | null;

/** クエリパラメータを既知の遷移元に正規化する。未知の値は null(既定の戻り先)。 */
export function parseSessionOrigin(value: string | string[] | undefined): SessionOrigin {
  const raw = Array.isArray(value) ? value[0] : value;
  if (raw === "streamer" || raw === "filtered") return raw;
  return null;
}

export type SessionBackTarget = { href: string; label: string };

/**
 * 戻り先のURLと表示ラベルを組み立てる。
 *
 * 直接URLを開いた場合など遷移元が不明なときは、全件のライブ一覧に戻す。
 * ライバーのページに戻すほうが親切に見えるが、そのライバーを見に来たとは
 * 限らないので、いちばん広い一覧に戻すのが安全側。
 */
export function sessionBackTarget(
  origin: SessionOrigin,
  session: { streamerName: string; tiktokAccountId: string; streamerId?: number },
): SessionBackTarget {
  if (origin === "streamer" && session.tiktokAccountId) {
    return {
      href: `/streamers/${encodeURIComponent(session.tiktokAccountId)}`,
      label: `${session.streamerName}のページに戻る`,
    };
  }
  if (origin === "filtered" && session.streamerId !== undefined) {
    return {
      href: `/sessions?streamerId=${session.streamerId}`,
      label: `${session.streamerName}のライブ一覧に戻る`,
    };
  }
  return { href: "/sessions", label: "ライブ一覧に戻る" };
}

/** 一覧から詳細へのリンクに付ける遷移元の印。既定(全件一覧)では何も付けない。 */
export function sessionDetailHref(sessionId: number, origin: SessionOrigin): string {
  const base = `/sessions/${sessionId}`;
  return origin ? `${base}?from=${origin}` : base;
}
