import styles from "./Avatar.module.css";

// No profile-image pipeline exists yet (see the design-brief doc's "アバター"
// discussion) -- this generates a deterministic color + initial from the
// username so every avatar is stable across renders/reloads without storing
// anything. `src` is accepted now so a future real-photo fetch can slot in
// without touching any call site: pass a URL and it renders instead.
const PALETTE = ["#fe2c55", "#25f4ee", "#8b5cf6", "#20c997", "#f59e0b", "#fb7185", "#38bdf8", "#a78bfa"];

function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i++) {
    hash = (Math.imul(hash, 31) + value.charCodeAt(i)) >>> 0;
  }
  return hash;
}

// name.charAt(0)/slice(0,1) split on UTF-16 code units, not user-perceived
// characters -- a surrogate-pair emoji (e.g. "💝") gets cut in half into an
// unpaired surrogate, which then gets sanitized differently between the
// server-rendered payload and the client's own render, causing a hydration
// mismatch (confirmed on real streamer names like "💝あんちゃん💝"). Segmenting
// by grapheme cluster keeps any single emoji intact, including multi-
// codepoint ones (flags, skin-tone modifiers, ZWJ sequences) that a plain
// Array.from() codepoint split would still break.
function firstGrapheme(value: string): string {
  const segments = new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(value);
  const first = segments[Symbol.iterator]().next();
  return first.done ? "" : first.value.segment;
}

export function Avatar({
  name,
  src,
  size = 40,
}: {
  name: string;
  src?: string | null;
  size?: number;
}) {
  if (src) {
    // External/local profile photos of arbitrary, not-yet-known origin;
    // next/image's domain allowlisting isn't worth wiring up until this
    // path is real.
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={src}
        alt=""
        width={size}
        height={size}
        className={styles.avatarImg}
        style={{ width: size, height: size }}
      />
    );
  }

  const initial = firstGrapheme(name.trim()).toUpperCase() || "?";
  const color = PALETTE[hashString(name) % PALETTE.length];

  return (
    <span
      className={styles.avatar}
      style={{ width: size, height: size, fontSize: size * 0.42, background: color }}
      aria-hidden
    >
      {initial}
    </span>
  );
}
