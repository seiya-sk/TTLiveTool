"use client";

import { useState, type ReactNode } from "react";

export type TabAccent = "pink" | "cyan" | "purple";

export type TabDef = {
  key: string;
  label: string;
  content: ReactNode;
  // Both optional and unused by existing callers (e.g. L3's 6-tab detail
  // tables) -- only the active tab is ever colored, so omitting them just
  // keeps today's neutral underline look.
  icon?: ReactNode;
  accent?: TabAccent;
};

export default function Tabs({ tabs }: { tabs: TabDef[] }) {
  const [active, setActive] = useState(tabs[0]?.key);

  return (
    <div>
      <div className="tabs">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            className={`tab-button${tab.key === active ? ` active${tab.accent ? ` tab-button-${tab.accent}` : ""}` : ""}`}
            onClick={() => setActive(tab.key)}
          >
            {tab.icon && <span className="tab-button-icon">{tab.icon}</span>}
            {tab.label}
          </button>
        ))}
      </div>
      {tabs.map((tab) => (
        <div key={tab.key} style={{ display: tab.key === active ? "block" : "none" }}>
          {tab.content}
        </div>
      ))}
    </div>
  );
}
