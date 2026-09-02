import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import { DocumentIcon, ImageIcon, PeopleIcon, PlusIcon } from "@/components/icons";
import { formatJst } from "@/lib/format";
import type { ReportRow } from "@/lib/queries";
import styles from "./AiReport.module.css";

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

export function AiReport({ report, sessionId }: { report: ReportRow | undefined; sessionId: number }) {
  if (!report) {
    return (
      <p className="empty">
        まだレポートは生成されていません。<code>python -m tiktok_monitor.generate_report {sessionId}</code> で生成できます。
      </p>
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
