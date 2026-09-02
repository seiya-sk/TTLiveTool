"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState, type ReactNode } from "react";

export type Column<T> = {
  key: string;
  label: string;
  accessor: (row: T) => string | number | null;
  // index is the row's position in the current sorted+filtered view (0 =
  // top) -- lets a ranking column render "current rank", not a stored
  // field. Existing render(row) callbacks stay valid (extra param ignored).
  render?: (row: T, index: number) => ReactNode;
  align?: "left" | "right";
  // Participates in the free-text search box.
  searchable?: boolean;
  // Adds a dedicated exact-match dropdown for this column, options derived
  // from the distinct values actually present in `rows`.
  filterable?: boolean;
  // Fixed column width (e.g. "120px", "20%"); paired with table-layout:
  // fixed via <colgroup> so sorting/filtering never reflows column widths.
  // Columns without a width share the remaining space equally.
  width?: string;
};

type SortDir = "asc" | "desc";
type Sort = { key: string; dir: SortDir };

const PAGE_SIZE = 50;

function compareValues(a: string | number | null, b: string | number | null): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), "ja");
}

export function DataTable<T>({
  rows,
  columns,
  emptyMessage = "データがありません。",
  defaultSort,
  rowHref,
  rowClassName,
}: {
  rows: T[];
  columns: Column<T>[];
  emptyMessage?: string;
  // Seeds initial sort (e.g. a ranking tab that should already read
  // "biggest first" without the user clicking a header).
  defaultSort?: Sort;
  // When provided, each row navigates there on click (ranking/list tables
  // linking out to a detail page). Omit for tables that are just data, not
  // navigation, e.g. L3's raw detail tabs.
  rowHref?: (row: T) => string;
  // Extra class(es) for one row (e.g. highlighting the #1 rank) -- appended
  // to the existing row-link class, never replaces it.
  rowClassName?: (row: T, index: number) => string | undefined;
}) {
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [sort, setSort] = useState<Sort | null>(defaultSort ?? null);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);

  const searchableColumns = useMemo(() => columns.filter((c) => c.searchable), [columns]);
  const filterableColumns = useMemo(() => columns.filter((c) => c.filterable), [columns]);

  const filterOptions = useMemo(() => {
    const options: Record<string, string[]> = {};
    for (const col of filterableColumns) {
      // Keep the raw (possibly numeric) value through sorting so "2" sorts
      // before "10" instead of lexicographically after it; stringify only
      // for the <option> value/label once the order is decided.
      const seen = new Map<string, string | number>();
      for (const row of rows) {
        const v = col.accessor(row);
        if (v !== null && v !== "") seen.set(String(v), v);
      }
      options[col.key] = Array.from(seen.values())
        .sort((a, b) => compareValues(a, b))
        .map((v) => String(v));
    }
    return options;
  }, [filterableColumns, rows]);

  const filtered = useMemo(() => {
    let result = rows;

    for (const col of filterableColumns) {
      const active = filters[col.key];
      if (active) {
        result = result.filter((row) => String(col.accessor(row) ?? "") === active);
      }
    }

    if (search.trim() && searchableColumns.length > 0) {
      const needle = search.trim().toLowerCase();
      result = result.filter((row) =>
        searchableColumns.some((col) => String(col.accessor(row) ?? "").toLowerCase().includes(needle))
      );
    }

    return result;
  }, [rows, filters, filterableColumns, search, searchableColumns]);

  const sorted = useMemo(() => {
    if (!sort) return filtered;
    const col = columns.find((c) => c.key === sort.key);
    if (!col) return filtered;
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => dir * compareValues(col.accessor(a), col.accessor(b)));
  }, [filtered, sort, columns]);

  // A new search/filter/sort result invalidates how far the user had paged
  // into the previous result set, so start back at the top page. Adjusting
  // state during render (React's documented pattern for this, rather than
  // an effect) avoids the extra commit-then-rerender cascade.
  const resetSignature = `${search}|${JSON.stringify(filters)}|${sort?.key ?? ""}|${sort?.dir ?? ""}`;
  const [prevResetSignature, setPrevResetSignature] = useState(resetSignature);
  if (resetSignature !== prevResetSignature) {
    setPrevResetSignature(resetSignature);
    setVisibleCount(PAGE_SIZE);
  }

  const visible = sorted.slice(0, visibleCount);

  // Cycle: none -> desc -> asc -> none. Descending first because these
  // tables are mostly time/count/level data where "biggest or most recent
  // first" is the more common thing to want to see immediately.
  function toggleSort(key: string) {
    setSort((prev) => {
      if (!prev || prev.key !== key) return { key, dir: "desc" };
      if (prev.dir === "desc") return { key, dir: "asc" };
      return null;
    });
  }

  function clearAll() {
    setSearch("");
    setFilters({});
    setSort(defaultSort ?? null);
  }

  const hasActiveControls =
    search.trim() !== "" ||
    Object.values(filters).some(Boolean) ||
    (defaultSort ? sort?.key !== defaultSort.key || sort?.dir !== defaultSort.dir : sort !== null);

  if (rows.length === 0) {
    return <p className="empty">{emptyMessage}</p>;
  }

  return (
    <div className="data-table">
      <div className="data-table-controls">
        {searchableColumns.length > 0 && (
          <input
            type="text"
            className="data-table-search"
            placeholder="検索..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        )}
        {filterableColumns.map((col) => (
          <select
            key={col.key}
            value={filters[col.key] ?? ""}
            onChange={(e) => setFilters((prev) => ({ ...prev, [col.key]: e.target.value }))}
          >
            <option value="">{col.label}: すべて</option>
            {filterOptions[col.key]?.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        ))}
        <span className="data-table-count">
          {filtered.length.toLocaleString("ja-JP")}件中 {visible.length.toLocaleString("ja-JP")}件表示
        </span>
        <button type="button" className="data-table-clear" onClick={clearAll} disabled={!hasActiveControls}>
          クリア
        </button>
      </div>

      {sorted.length === 0 ? (
        <p className="empty">条件に一致するデータがありません。</p>
      ) : (
        <>
          <table>
            <colgroup>
              {columns.map((col) => (
                <col key={col.key} style={col.width ? { width: col.width } : undefined} />
              ))}
            </colgroup>
            <thead>
              <tr>
                {columns.map((col) => (
                  <th
                    key={col.key}
                    className={col.align === "right" ? "col-right" : undefined}
                    onClick={() => toggleSort(col.key)}
                    style={{ cursor: "pointer", userSelect: "none" }}
                  >
                    {col.label}
                    {sort?.key === col.key ? (sort.dir === "desc" ? " ▼" : " ▲") : ""}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visible.map((row, i) => {
                const href = rowHref?.(row);
                const extraClass = rowClassName?.(row, i);
                const className = [href ? "data-table-row-link" : undefined, extraClass].filter(Boolean).join(" ") || undefined;
                return (
                  <tr key={i} className={className} onClick={href ? () => router.push(href) : undefined}>
                    {columns.map((col) => (
                      <td key={col.key} className={col.align === "right" ? "col-right" : undefined}>
                        {col.render ? col.render(row, i) : (col.accessor(row) ?? "-")}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
          {visibleCount < sorted.length && (
            <button type="button" className="data-table-more" onClick={() => setVisibleCount((v) => v + PAGE_SIZE)}>
              もっと見る(残り{(sorted.length - visibleCount).toLocaleString("ja-JP")}件)
            </button>
          )}
        </>
      )}
    </div>
  );
}
