import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import { DocumentIcon, ImageIcon, PeopleIcon, PlusIcon } from "@/components/icons";
import { formatJst } from "@/lib/format";
import type { ReportRow } from "@/lib/queries";
import styles from "./AiReport.module.css";
import { GenerateReportButton } from "./GenerateReportButton";

const SECTION_DEFS: {
  key: "viewer_highlights" | "comment_trends" | "visual_feedback";
  title: string;
  icon: ReactNode;
  accent: "cyan" | "pink" | "purple";
}[] = [
  { key: "viewer_highlights", title: "視聴者の傾向", icon: <PeopleIcon size={16} />, accent: "cyan" },
  { key: "comment_trends", title: "コメントの傾向", icon: <DocumentIcon size={16} />, accent: "pink" },
  { key: "visual_feedback", title: "配信画面の見え方", icon: <ImageIcon size={16} />, accent: "purple" },
];

export function AiReport({
  report,
  sessionId,
  isLive = false,
}: {
  report: ReportRow | undefined;
  sessionId: number;
  /** 配信中はレポートを作らせない。途中経過で作ると内容が不完全なうえ、
      「生成済み」になって作り直せなくなる。 */
  isLive?: boolean;
}) {
  if (!report) {
    if (isLive) {
      return (
        <p className="empty">
          配信中はレポートを生成できません。配信が終了すると生成できるようになります。
        </p>
      );
    }
    return (
      <div>
        <p className="empty">まだレポートは生成されていません。</p>
        <GenerateReportButton sessionId={sessionId} />
      </div>
    );
  }

  const sections = report.sections;

  return (
    <div>
      <p className="report-generated-at">生成日時: {formatJst(report.generatedAt)}</p>

      {sections ? (
        <>
          <div className={styles.grid}>
            {SECTION_DEFS.map(({ key, title, icon, accent }) => {
              const body = sections[key];
              if (!body) return null;
              return (
                <div key={key} className={styles.card}>
                  <div className={styles.cardHeader}>
                    <span className={`${styles.icon} ${styles[accent]}`}>{icon}</span>
                    {title}
                  </div>
                  <div className={`markdown-body ${styles.body}`}>
                    <ReactMarkdown>{body}</ReactMarkdown>
                  </div>
                </div>
              );
            })}
            {sections.next_stream_suggestions && sections.next_stream_suggestions.length > 0 && (
              <div className={styles.card}>
                <div className={styles.cardHeader}>
                  <span className={`${styles.icon} ${styles.success}`}>
                    <PlusIcon size={16} />
                  </span>
                  次回配信への提案
                </div>
                <ol className={styles.suggestionList}>
                  {sections.next_stream_suggestions.map((s, i) => (
                    <li key={i}>{s}</li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        </>
      ) : (
        <div className="markdown-body">
          <ReactMarkdown>{report.recommendationMd}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}
