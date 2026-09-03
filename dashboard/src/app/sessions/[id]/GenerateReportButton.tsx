"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import styles from "./AiReport.module.css";

// レポート1枚あたり約7.5円の原価がかかる。押した本人が意図した1回だけが
// 走るようにする:
//   - 生成中はボタンを無効化する(二重クリックで2枚作らない)
//   - 配信中はそもそも表示しない(不完全なレポートができ、しかも
//     「生成済み」になって作り直せなくなる)
//   - 失敗したらボタンを戻す。失敗を「生成済み」にすると、そのライブは
//     永久にレポートを作れなくなる
// 同じ判定はサーバ側(api/sessions/[id]/report)にも置いてある。画面が
// 古いまま押された場合、UIの状態は当てにならないため。

export function GenerateReportButton({ sessionId }: { sessionId: number }) {
  const router = useRouter();
  const [state, setState] = useState<"idle" | "running">("idle");
  const [error, setError] = useState<string | null>(null);

  async function run() {
    if (state === "running") return;
    setState("running");
    setError(null);
    try {
      const res = await fetch(`/api/sessions/${sessionId}/report`, { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(body.error ?? "レポートの生成に失敗しました。");
        return;      // ボタンは残る -- 作り直せる状態を保つ
      }
      // 生成できたのでサーバコンポーネントを取り直す。ここで初めて
      // レポート本体が描画され、ボタンは消える。
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "通信に失敗しました。");
    } finally {
      setState("idle");
    }
  }

  return (
    <div className={styles.generate}>
      <button
        type="button"
        className={styles.generateButton}
        onClick={run}
        disabled={state === "running"}
        aria-busy={state === "running"}
      >
        {state === "running" ? "生成中…" : "AIレポートを生成"}
      </button>
      {state === "running" && (
        <p className={styles.generateNote} role="status" aria-live="polite">
          Claude がこのライブを分析しています。数十秒かかることがあります。
          このまま開いたままお待ちください。
        </p>
      )}
      {state === "idle" && !error && (
        <p className={styles.generateNote}>
          1枚あたり約7.5円の費用がかかります。生成できるのは1回だけです。
        </p>
      )}
      {error && (
        <p className={styles.generateError} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
