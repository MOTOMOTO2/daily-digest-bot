import { compact, formatRelative, type Health } from "../api";
import { StatusBadge, statusClass } from "./StatusBadge";

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {note ? <div className="tile-note">{note}</div> : null}
    </div>
  );
}

/**
 * The one hero figure in the view: how long since a digest actually landed.
 * That is the number that tells you whether the bot has gone quiet.
 */
function SilenceFigure({ health }: { health: Health }) {
  const hours = health.hours_since_digest;
  const limit = health.config.max_silence_hours;
  const never = health.last_digest === null || hours === null;

  const figure = never ? "--" : hours < 1 ? "<1h" : `${Math.round(hours)}h`;
  const caption = never
    ? "no digest has ever been delivered"
    : `since the last digest (limit ${limit}h)`;

  return (
    <div>
      <div className="hero-figure">{figure}</div>
      <div className="hero-label">{caption}</div>
    </div>
  );
}

export function HealthPanel({ health }: { health: Health }) {
  const { stats, config } = health;
  const healthySources = health.sources.filter((source) => source.status === "ok").length;

  return (
    <div className="stack">
      <section className={`card ${statusClass(health.status)}`}>
        <div className="hero">
          <SilenceFigure health={health} />
          <div className="hero-body">
            <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 10 }}>
              <StatusBadge status={health.status} />
              <span className="meta">
                {config.delivery_kind}:{config.channel} &middot; posts after {config.send_after}{" "}
                {config.timezone}
              </span>
            </div>
            <ul className="checks">
              {health.checks.map((check) => (
                <li key={check.name} className={statusClass(check.status)}>
                  <StatusBadge status={check.status} label={check.name.replace(/_/g, " ")} />
                  <span className="check-detail">{check.detail}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      <div className="grid kpi">
        <Tile
          label="Waiting to be sent"
          value={compact(stats.items_pending)}
          note="collected, not yet in a digest"
        />
        <Tile
          label="Items posted"
          value={compact(stats.items_sent)}
          note={`${compact(stats.items_total)} ever seen`}
        />
        <Tile
          label="Digests sent"
          value={compact(stats.digests_sent)}
          note={
            stats.digests_failed > 0
              ? `${stats.digests_failed} failed to deliver`
              : "none failed"
          }
        />
        <Tile
          label="Sources healthy"
          value={`${healthySources}/${config.enabled_source_count}`}
          note={config.alerts_enabled ? "alert channel configured" : "no alert channel"}
        />
        <Tile
          label="Last run"
          value={health.last_run ? formatRelative(health.last_run.started_at) : "never"}
          note={health.last_run ? `${health.last_run.status} (${health.last_run.trigger ?? "?"})` : undefined}
        />
      </div>
    </div>
  );
}
