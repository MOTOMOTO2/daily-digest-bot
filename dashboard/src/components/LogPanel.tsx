import { useEffect, useState } from "react";
import { api, formatLogTime, type Event } from "../api";

const LEVELS = ["CRITICAL", "ERROR", "WARNING", "INFO"] as const;

const LEVEL_COLOR: Record<string, string> = {
  CRITICAL: "var(--status-critical)",
  ERROR: "var(--status-critical)",
  WARNING: "var(--status-warning)",
  INFO: "var(--text-muted)",
  DEBUG: "var(--text-muted)",
};

const LEVEL_GLYPH: Record<string, string> = {
  CRITICAL: "✕",
  ERROR: "✕",
  WARNING: "!",
  INFO: "·",
  DEBUG: "·",
};

export function LogPanel({ logFile }: { logFile: string }) {
  const [levels, setLevels] = useState<string[]>([]);
  const [events, setEvents] = useState<Event[] | null>(null);
  const [raw, setRaw] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api
      .events(200, levels)
      .then((result) => active && setEvents(result.events))
      .catch((cause: Error) => active && setError(cause.message));
    return () => {
      active = false;
    };
  }, [levels]);

  useEffect(() => {
    if (!showRaw || raw !== null) return;
    let active = true;
    api
      .logfile(400)
      .then((text) => active && setRaw(text))
      .catch((cause: Error) => active && setError(cause.message));
    return () => {
      active = false;
    };
  }, [showRaw, raw]);

  function toggle(level: string) {
    setLevels((current) =>
      current.includes(level) ? current.filter((item) => item !== level) : [...current, level],
    );
  }

  return (
    <section className="card">
      <div className="filter-row">
        <h3 className="section-title" style={{ margin: 0 }}>
          The bot&apos;s own log
        </h3>
        <span style={{ flex: 1 }} />
        {LEVELS.map((level) => (
          <button
            key={level}
            type="button"
            className="btn"
            aria-pressed={levels.includes(level)}
            onClick={() => toggle(level)}
          >
            {level}
          </button>
        ))}
        <button
          type="button"
          className="btn"
          aria-pressed={showRaw}
          onClick={() => setShowRaw((previous) => !previous)}
        >
          {showRaw ? "Hide file" : "Raw file"}
        </button>
      </div>

      <p className="meta" style={{ marginTop: 0 }}>
        Warnings and above are mirrored from <code>{logFile}</code> into the database, so a failure
        is visible here even if nobody opens the file.
      </p>

      {error ? <div className="error-note">{error}</div> : null}

      {showRaw ? (
        <pre className="logfile">{raw ?? "Loading..."}</pre>
      ) : events === null ? (
        <p className="empty">Loading...</p>
      ) : events.length === 0 ? (
        <p className="empty">
          {levels.length > 0
            ? "Nothing at the selected level(s)."
            : "No events recorded. A quiet log on a bot that has never run is expected; a quiet log on a bot that should have posted is not."}
        </p>
      ) : (
        <div>
          {events.map((event) => (
            <div className="log-line" key={event.id}>
              <span className="log-ts">{formatLogTime(event.ts)}</span>
              <span className="log-level" style={{ color: LEVEL_COLOR[event.level] ?? "inherit" }}>
                <span aria-hidden="true">{LEVEL_GLYPH[event.level] ?? "·"}</span>
                {event.level}
              </span>
              <span className="log-message">
                {event.source ? <span className="log-source">[{event.source}] </span> : null}
                {event.message}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
