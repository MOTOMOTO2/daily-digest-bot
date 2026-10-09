import { useEffect, useState } from "react";
import { api, formatDateTime, type Digest, type DigestItem } from "../api";
import { ItemsChart } from "./ItemsChart";
import { StatusBadge } from "./StatusBadge";

const DIGEST_STATUS = {
  sent: "ok",
  pending: "unknown",
  failed: "down",
  skipped: "disabled",
} as const;

function DigestDetail({ id, onClose }: { id: number; onClose: () => void }) {
  const [data, setData] = useState<{ digest: Digest; items: DigestItem[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setData(null);
    setError(null);
    api
      .digest(id)
      .then((result) => active && setData(result))
      .catch((cause: Error) => active && setError(cause.message));
    return () => {
      active = false;
    };
  }, [id]);

  return (
    <section className="card">
      <div className="filter-row">
        <h3 className="section-title" style={{ margin: 0, flex: 1 }}>
          {data ? data.digest.title ?? data.digest.digest_date : `Digest ${id}`}
        </h3>
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
      </div>
      {error ? <p className="empty">{error}</p> : null}
      {!data && !error ? <p className="empty">Loading...</p> : null}
      {data ? (
        <>
          <p className="meta" style={{ marginTop: 0 }}>
            {data.digest.status} &middot; {data.digest.item_count} item
            {data.digest.item_count === 1 ? "" : "s"} &middot; {data.digest.channel} &middot;{" "}
            {data.digest.sent_at ? `sent ${formatDateTime(data.digest.sent_at)}` : "never sent"}
            {data.digest.attempts > 1 ? ` after ${data.digest.attempts} attempts` : ""}
          </p>
          {data.digest.last_error ? (
            <div className="error-note" style={{ marginBottom: 12 }}>
              {data.digest.last_error}
            </div>
          ) : null}
          <pre className="digest-body">{data.digest.body ?? "(no body recorded)"}</pre>
        </>
      ) : null}
    </section>
  );
}

export function DigestsPanel({ digests }: { digests: Digest[] }) {
  const [selected, setSelected] = useState<number | null>(null);

  return (
    <div className="stack">
      <section className="card">
        <ItemsChart digests={digests} />
      </section>

      {selected !== null ? (
        <DigestDetail id={selected} onClose={() => setSelected(null)} />
      ) : null}

      <section className="card">
        <h3 className="section-title">Digest history</h3>
        {digests.length === 0 ? (
          <p className="empty">Nothing recorded yet.</p>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">Status</th>
                <th scope="col" className="num">
                  Items
                </th>
                <th scope="col">Channel</th>
                <th scope="col">Sent</th>
                <th scope="col"></th>
              </tr>
            </thead>
            <tbody>
              {digests.map((digest) => (
                <tr key={digest.id}>
                  <td style={{ fontVariantNumeric: "tabular-nums" }}>{digest.digest_date}</td>
                  <td>
                    <StatusBadge
                      status={DIGEST_STATUS[digest.status] ?? "unknown"}
                      label={digest.status}
                    />
                  </td>
                  <td className="num">{digest.item_count}</td>
                  <td className="meta">{digest.channel}</td>
                  <td className="meta">
                    {digest.sent_at ? formatDateTime(digest.sent_at) : digest.last_error ?? "--"}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="row-button"
                      onClick={() => setSelected(digest.id)}
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
