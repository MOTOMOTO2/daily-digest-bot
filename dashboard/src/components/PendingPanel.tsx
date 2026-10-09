import { useEffect, useState } from "react";
import { api, formatRelative, type PendingItem } from "../api";

export function PendingPanel({ sendAfter, timezone }: { sendAfter: string; timezone: string }) {
  const [data, setData] = useState<{ count: number; items: PendingItem[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api
      .pending(150)
      .then((result) => active && setData(result))
      .catch((cause: Error) => active && setError(cause.message));
    return () => {
      active = false;
    };
  }, []);

  return (
    <section className="card">
      <h3 className="section-title">Waiting for the next digest</h3>
      <p className="meta" style={{ marginTop: 0 }}>
        Collected and deduplicated, not yet posted. These go out in the next digest after{" "}
        {sendAfter} {timezone}.
      </p>
      {error ? <div className="error-note">{error}</div> : null}
      {data === null && !error ? <p className="empty">Loading...</p> : null}
      {data && data.items.length === 0 ? (
        <p className="empty">
          Nothing queued. The next digest will say so rather than going quiet.
        </p>
      ) : null}
      {data && data.items.length > 0 ? (
        <table className="data">
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">Title</th>
              <th scope="col">Published</th>
              <th scope="col">First seen</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((item) => (
              <tr key={item.id}>
                <td className="meta">{item.source}</td>
                <td>
                  {item.url ? (
                    <a href={item.url} target="_blank" rel="noreferrer noopener">
                      {item.title}
                    </a>
                  ) : (
                    item.title
                  )}
                </td>
                <td className="meta">{formatRelative(item.published_at)}</td>
                <td className="meta">{formatRelative(item.first_seen_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {data && data.count > data.items.length ? (
        <p className="meta">
          Showing {data.items.length} of {data.count}.
        </p>
      ) : null}
    </section>
  );
}
