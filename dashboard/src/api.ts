/** Types and fetch helpers for the read-only bot API. */

export type Status = "ok" | "degraded" | "serious" | "down" | "unknown" | "disabled";

export interface Check {
  name: string;
  status: Status;
  detail: string;
}

export interface SourceHealth {
  source: string;
  label: string;
  kind: string;
  url: string;
  enabled: boolean;
  status: Status;
  last_status: string | null;
  last_checked_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_error_type: string | null;
  last_duration_ms: number | null;
  consecutive_failures: number;
  checks: number;
  successful_checks: number;
  items_new_total: number;
  items_sent_total: number;
}

export interface Digest {
  id: number;
  digest_date: string;
  status: "pending" | "sent" | "failed" | "skipped";
  item_count: number;
  channel: string | null;
  title: string | null;
  body: string | null;
  created_at: string;
  sent_at: string | null;
  attempts: number;
  last_error: string | null;
}

export interface DigestItem {
  id: number;
  source: string;
  title: string;
  url: string | null;
  summary: string | null;
  author: string | null;
  published_at: string | null;
}

export interface SourceResult {
  id: number;
  run_id: number;
  source: string;
  kind: string | null;
  status: string;
  items_fetched: number;
  items_new: number;
  duration_ms: number;
  error: string | null;
  error_type: string | null;
  created_at: string;
}

export interface Run {
  id: number;
  started_at: string;
  finished_at: string | null;
  status: string;
  trigger: string | null;
  items_fetched: number;
  items_new: number;
  digest_id: number | null;
  sent: number;
  error: string | null;
  notes: string | null;
  sources: SourceResult[];
}

export interface Event {
  id: number;
  ts: string;
  level: string;
  source: string | null;
  event: string | null;
  message: string;
  detail: Record<string, unknown> | null;
  run_id: number | null;
}

export interface PendingItem {
  id: number;
  source: string;
  title: string;
  url: string | null;
  published_at: string | null;
  first_seen_at: string | null;
}

export interface Health {
  status: Status;
  healthy: boolean;
  hours_since_digest: number | null;
  checks: Check[];
  sources: SourceHealth[];
  last_digest: Digest | null;
  last_run: Run | null;
  stats: {
    database: string;
    items_total: number;
    items_pending: number;
    items_expired: number;
    items_sent: number;
    digests_total: number;
    digests_sent: number;
    digests_failed: number;
    runs_total: number;
    events_total: number;
  };
  generated_at: string;
  version: string;
  config: {
    timezone: string;
    send_after: string;
    channel: string;
    delivery_kind: string;
    alerts_enabled: boolean;
    max_silence_hours: number;
    post_when_empty: boolean;
    log_file: string;
    database: string;
    source_count: number;
    enabled_source_count: number;
  };
}

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "ApiError";
  }
}

async function get<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" } });
  } catch (cause) {
    // The API being unreachable is itself a finding worth showing, not a
    // blank screen.
    throw new ApiError(
      `cannot reach the bot API at ${path}. Is \`digest-bot serve\` running?`,
    );
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      /* keep the status line */
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => get<Health>("/api/health"),
  digests: (limit = 30) => get<{ digests: Digest[] }>(`/api/digests?limit=${limit}`),
  digest: (id: number) => get<{ digest: Digest; items: DigestItem[] }>(`/api/digests/${id}`),
  runs: (limit = 25) => get<{ runs: Run[] }>(`/api/runs?limit=${limit}`),
  events: (limit = 150, levels: string[] = []) => {
    const query = levels.map((level) => `&level=${encodeURIComponent(level)}`).join("");
    return get<{ events: Event[] }>(`/api/events?limit=${limit}${query}`);
  },
  pending: (limit = 100) => get<{ count: number; items: PendingItem[] }>(`/api/pending?limit=${limit}`),
  logfile: async (lines = 300): Promise<string> => {
    const response = await fetch(`/api/logfile?lines=${lines}`);
    if (!response.ok) throw new ApiError(`${response.status} ${response.statusText}`);
    return response.text();
  },
};

/* -------------------------------------------------------------- formatting */

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Compact stamp for log lines, where the year is just noise. */
export function formatLogTime(value: string | null | undefined): string {
  if (!value) return "--";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const seconds = (Date.now() - parsed.getTime()) / 1000;
  if (seconds < 60) return "just now";
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 48) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function compact(value: number): string {
  if (Math.abs(value) < 1000) return String(value);
  if (Math.abs(value) < 1_000_000) return `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)}K`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}
