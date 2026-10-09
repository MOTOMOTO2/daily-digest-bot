import { useCallback, useEffect, useState } from "react";
import { api, formatRelative, type Digest, type Health, type Run } from "./api";
import { DigestsPanel } from "./components/DigestsPanel";
import { HealthPanel } from "./components/HealthPanel";
import { LogPanel } from "./components/LogPanel";
import { PendingPanel } from "./components/PendingPanel";
import { RunsPanel } from "./components/RunsPanel";
import { SourceCards } from "./components/SourceCards";

type Tab = "overview" | "digests" | "runs" | "log" | "pending";

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "digests", label: "Digests" },
  { id: "runs", label: "Runs" },
  { id: "log", label: "Log" },
  { id: "pending", label: "Queue" },
];

const REFRESH_MS = 30_000;

const TAB_IDS = TABS.map((entry) => entry.id);

function tabFromHash(): Tab {
  const hash = window.location.hash.replace(/^#/, "") as Tab;
  return TAB_IDS.includes(hash) ? hash : "overview";
}

/** Keeps the open tab in the URL, so a view can be bookmarked or shared. */
function useHashTab() {
  const [tab, setTab] = useState<Tab>(tabFromHash);

  useEffect(() => {
    const onHashChange = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const select = useCallback((next: Tab) => {
    window.location.hash = next;
    setTab(next);
  }, []);

  return { tab, select };
}

function useTheme() {
  const [theme, setTheme] = useState<"system" | "light" | "dark">(
    () => (localStorage.getItem("digestbot-theme") as "system" | "light" | "dark") ?? "system",
  );

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    localStorage.setItem("digestbot-theme", theme);
  }, [theme]);

  return { theme, setTheme };
}

export default function App() {
  const { tab, select } = useHashTab();
  const [health, setHealth] = useState<Health | null>(null);
  const [digests, setDigests] = useState<Digest[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loadedAt, setLoadedAt] = useState<string | null>(null);
  const { theme, setTheme } = useTheme();

  const load = useCallback(async () => {
    try {
      const [healthResult, digestResult, runResult] = await Promise.all([
        api.health(),
        api.digests(40),
        api.runs(30),
      ]);
      setHealth(healthResult);
      setDigests(digestResult.digests);
      setRuns(runResult.runs);
      setLoadedAt(new Date().toISOString());
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  return (
    <div className="shell">
      <header className="topbar">
        <h1>daily-digest-bot</h1>
        {health ? <span className="meta">v{health.version}</span> : null}
        <span className="spacer" />
        {loadedAt ? <span className="meta">updated {formatRelative(loadedAt)}</span> : null}
        <button type="button" className="btn" onClick={() => void load()}>
          Refresh
        </button>
        <button
          type="button"
          className="btn"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          aria-label="Toggle colour theme"
        >
          {theme === "dark" ? "Light" : "Dark"}
        </button>
      </header>

      {error ? (
        <div className="banner">
          <strong>Cannot read the bot&apos;s record.</strong> {error}
          <div className="meta" style={{ marginTop: 6 }}>
            Start the API with <code>digest-bot serve</code>. This dashboard is read-only: it
            never collects or posts anything itself.
          </div>
        </div>
      ) : null}

      {health === null && !error ? <p className="empty">Loading...</p> : null}

      {health ? (
        <>
          <nav className="tabs" role="tablist" aria-label="Sections">
            {TABS.map((entry) => (
              <button
                key={entry.id}
                type="button"
                className="tab"
                role="tab"
                aria-selected={tab === entry.id}
                onClick={() => select(entry.id)}
              >
                {entry.label}
                {entry.id === "digests" && digests.length > 0 ? (
                  <span className="count"> {digests.length}</span>
                ) : null}
                {entry.id === "pending" && health.stats.items_pending > 0 ? (
                  <span className="count"> {health.stats.items_pending}</span>
                ) : null}
              </button>
            ))}
          </nav>

          {tab === "overview" ? (
            <div className="stack">
              <HealthPanel health={health} />
              <div>
                <h3 className="section-title">Sources</h3>
                <SourceCards sources={health.sources} />
              </div>
            </div>
          ) : null}

          {tab === "digests" ? <DigestsPanel digests={digests} /> : null}
          {tab === "runs" ? <RunsPanel runs={runs} /> : null}
          {tab === "log" ? <LogPanel logFile={health.config.log_file} /> : null}
          {tab === "pending" ? (
            <PendingPanel
              sendAfter={health.config.send_after}
              timezone={health.config.timezone}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}
