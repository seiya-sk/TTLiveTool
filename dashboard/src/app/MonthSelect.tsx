"use client";

import { useRouter } from "next/navigation";

export function MonthSelect({
  options,
  value,
}: {
  options: { value: string; label: string }[];
  value: string;
}) {
  const router = useRouter();

  return (
    <select
      className="month-select"
      value={value}
      onChange={(e) => router.push(`/?month=${e.target.value}`)}
      aria-label="集計対象の月"
    >
      {options.map((opt) => (
        <option key={opt.value} value={opt.value}>
          {opt.label}
        </option>
      ))}
    </select>
  );
}
