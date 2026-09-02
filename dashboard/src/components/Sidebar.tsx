"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { CoinIcon, DbIcon, GearIcon, HomeIcon, PeopleIcon, PlayIcon, UserIcon } from "./icons";
import styles from "./Sidebar.module.css";

type NavItem = { href: string; label: string; icon: ReactNode };

const MAIN_ITEMS: NavItem[] = [
  { href: "/", label: "ホーム", icon: <HomeIcon /> },
  { href: "/streamers", label: "ライバー一覧", icon: <UserIcon /> },
  { href: "/sessions", label: "ライブ一覧", icon: <PlayIcon /> },
];

// "ランキング" appears in the design mock's sidebar but has no distinct
// screen of its own yet (its content today lives inside ライブ一覧's ranking
// tabs) -- omitted here rather than pointing two nav items at the same
// route, which would highlight both simultaneously. Add it back once it has
// its own page.
// No standalone "設定" hub item -- it only ever linked out to these three
// (see the 2026-08-28 redesign discussion), so it was removed rather than
// kept as a redundant extra click.
const MANAGE_ITEMS: NavItem[] = [
  { href: "/settings/tokens", label: "トークン管理", icon: <CoinIcon /> },
  { href: "/settings/data", label: "データ管理", icon: <DbIcon /> },
  { href: "/settings/streamers", label: "ライバー管理", icon: <PeopleIcon /> },
  { href: "/settings/notifications", label: "通知設定", icon: <GearIcon /> },
];

export function Sidebar() {
  const pathname = usePathname();

  const isActive = (href: string) => {
    if (href === "/") return pathname === href;
    return pathname.startsWith(href);
  };

  return (
    <aside className={styles.sidebar}>
      <div>
        <Link href="/" className={styles.brandRow}>
          {/* Placeholder only -- see design-brief follow-up: an original
              mark still needs to be designed. Never fill this with TikTok's
              own logo (trademark risk + the docx's explicit "not a copy of
              official TikTok UI" rule). */}
          <span className={styles.logoBox} aria-hidden />
          <span className={styles.brand}>
            TikTok <span className={styles.brandAccent}>LIVE</span>
            <span className={styles.brandSub}>ANALYTICS</span>
          </span>
        </Link>

        <nav className={styles.nav}>
          {MAIN_ITEMS.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`${styles.navLink} ${isActive(item.href) ? styles.active : ""}`}
            >
              <span className={styles.navIcon}>{item.icon}</span>
              {item.label}
            </Link>
          ))}
        </nav>

        <div className={styles.sectionLabel}>設定・管理</div>
        <nav className={styles.nav}>
          {MANAGE_ITEMS.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`${styles.navLink} ${isActive(item.href) ? styles.active : ""}`}
            >
              <span className={styles.navIcon}>{item.icon}</span>
              {item.label}
            </Link>
          ))}
        </nav>
      </div>

      <div className={styles.promo}>
        <div className={styles.promoIllustration} aria-hidden />
        <p>データで、配信をもっと楽しく。もっと強く。</p>
      </div>
    </aside>
  );
}
