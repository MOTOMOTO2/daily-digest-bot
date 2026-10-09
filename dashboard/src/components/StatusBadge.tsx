import type { Status } from "../api";

/**
 * Status is never carried by color alone: every badge ships a glyph and a
 * written label alongside the swatch.
 */
const LABELS: Record<Status, { text: string; glyph: string }> = {
  ok: { text: "Healthy", glyph: "✓" },
  degraded: { text: "Degraded", glyph: "!" },
  serious: { text: "Serious", glyph: "!" },
  down: { text: "Down", glyph: "✕" },
  unknown: { text: "Unknown", glyph: "?" },
  disabled: { text: "Disabled", glyph: "–" },
};

export function statusClass(status: Status): string {
  return `status-${status in LABELS ? status : "unknown"}`;
}

export function StatusBadge({ status, label }: { status: Status; label?: string }) {
  const meta = LABELS[status] ?? LABELS.unknown;
  return (
    <span className={`badge ${statusClass(status)}`}>
      <span className="dot" aria-hidden="true" />
      <span className="glyph" aria-hidden="true">
        {meta.glyph}
      </span>
      {label ?? meta.text}
    </span>
  );
}
