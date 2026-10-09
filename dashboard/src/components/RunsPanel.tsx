import { formatDateTime, type Run, type Status } from "../api";
import { StatusBadge } from "./StatusBadge";

const RUN_STATUS: Record<string, Status> = {
  ok: "ok",
  partial: "degraded",
  failed: "down",
  running: "unknown",
  skipped: "disabled",
};

function duration(run: Run): string {
  if (!run.finished_at) return "--";
  const ms = new Date(run.finished_at).getTime() - new Date(run.started_at).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "--";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

export function RunsPanel({ runs }: { runs: Run[] }) {
  if (runs.length === 0) {
    return (
      <section className="card">
        <p className="empty">
          No runs recorded yet. The bot records one row per invocation, including the ones that
          fail.
        </p>
      </section>
    );
  }

  return (
    <section className="card">
      <h3 className="section-title">Run history</h3>
      <table className="data">
        <thead>
          <tr>
            <th scope="col">Started</th>
            <th scope="col">Status</th>
            <th scope="col">Trigger</th>
            <th scope="col" className="num">
              Fetched
            </th>
            <th scope="col" className="num">
              New
            </th>
            <th scope="col">Sent</th>
            <th scope="col" className="num">
              Took
            </th>
            <th scope="col">Sources</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const failed = run.sources.filter((source) => source.status !== "ok");
            return (
              <tr key={run.id}>
                <td className="meta">{formatDateTime(run.started_at)}</td>
                <td>
                  <StatusBadge status={RUN_STATUS[run.status] ?? "unknown"} label={run.status} />
                </td>
                <td className="meta">{run.trigger ?? "--"}</td>
                <td className="num">{run.items_fetched}</td>
                <td className="num">{run.items_new}</td>
                <td className="meta">{run.sent ? "yes" : "no"}</td>
                <td className="num meta">{duration(run)}</td>
                <td>
                  {failed.length === 0 ? (
                    <span className="meta">{run.sources.length} ok</span>
                  ) : (
                    <span className="meta">
                      {run.sources.length - failed.length}/{run.sources.length} ok &mdash;{" "}
                      {failed.map((source) => source.source).join(", ")}
                    </span>
                  )}
                  {run.error ? <div className="error-note">{run.error}</div> : null}
                  {run.notes && !run.error ? <div className="meta">{run.notes}</div> : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
