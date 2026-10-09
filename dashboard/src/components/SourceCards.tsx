import { formatRelative, type SourceHealth } from "../api";
import { StatusBadge, statusClass } from "./StatusBadge";

function SourceCard({ source }: { source: SourceHealth }) {
  const reliability =
    source.checks > 0 ? Math.round((source.successful_checks / source.checks) * 100) : null;

  return (
    <article className={`source-card ${statusClass(source.status)}`}>
      <header className="source-head">
        <span className="source-name" title={source.label}>
          {source.label}
        </span>
        <span className="kind-tag">{source.kind}</span>
        <span style={{ flex: 1 }} />
        <StatusBadge status={source.status} />
      </header>
      <div className="source-url" title={source.url}>
        {source.url}
      </div>
      <dl className="source-stats">
        <div>
          <dt>Items posted</dt>
          <dd>{source.items_sent_total}</dd>
        </div>
        <div>
          <dt>Checks ok</dt>
          <dd>{reliability === null ? "--" : `${reliability}%`}</dd>
        </div>
        <div>
          <dt>Last checked</dt>
          <dd style={{ fontWeight: 500 }}>{formatRelative(source.last_checked_at)}</dd>
        </div>
      </dl>
      {source.last_error && source.status !== "ok" ? (
        <div className="error-note">
          <strong>{source.last_error_type ?? "error"}</strong>
          {source.consecutive_failures > 1 ? ` (${source.consecutive_failures} in a row)` : ""}:{" "}
          {source.last_error}
        </div>
      ) : null}
    </article>
  );
}

export function SourceCards({ sources }: { sources: SourceHealth[] }) {
  if (sources.length === 0) {
    return <p className="empty">No sources configured.</p>;
  }
  // Worst first: the thing that needs attention is the thing you see.
  const order: Record<string, number> = { down: 0, serious: 1, degraded: 2, unknown: 3, ok: 4, disabled: 5 };
  const sorted = [...sources].sort(
    (a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || a.source.localeCompare(b.source),
  );

  return (
    <div className="grid sources">
      {sorted.map((source) => (
        <SourceCard key={source.source} source={source} />
      ))}
    </div>
  );
}
