"""Read-only HTTP API behind the React dashboard.

Read-only on purpose: the dashboard's job is to show whether the bot is
working, and a browser tab should not be able to fire off a digest or edit the
local record. Collection and delivery stay in the CLI, where cron drives them.

Each request opens its own SQLite connection so the API never holds a write
lock against a concurrent ``digest-bot run``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Config, ConfigError, load_config
from .health import assess
from .store import Store, row_to_dict

DEFAULT_CONFIG_PATH = os.environ.get("DIGEST_BOT_CONFIG", "config.toml")
# Dev server origins for `npm run dev`.
DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]


def create_app(config_path: str | None = None) -> FastAPI:
    resolved_path = config_path or DEFAULT_CONFIG_PATH

    app = FastAPI(
        title="daily-digest-bot",
        version=__version__,
        description="Read-only view of the bot's local record, health and log.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    def get_config() -> Config:
        try:
            return load_config(resolved_path)
        except ConfigError as exc:
            raise HTTPException(status_code=500, detail=f"configuration error: {exc}") from exc

    def get_store(config: Config = Depends(get_config)) -> Iterator[Store]:
        store = Store(config.paths.database)
        try:
            yield store
        finally:
            store.close()

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    @app.get("/api/health")
    def health(
        config: Config = Depends(get_config), store: Store = Depends(get_store)
    ) -> dict[str, Any]:
        report = assess(config, store)
        return {**report.as_dict(), "version": __version__}

    @app.get("/api/sources")
    def sources(
        config: Config = Depends(get_config), store: Store = Depends(get_store)
    ) -> dict[str, Any]:
        return {"sources": assess(config, store).sources}

    @app.get("/api/digests")
    def digests(
        limit: int = Query(30, ge=1, le=365), store: Store = Depends(get_store)
    ) -> dict[str, Any]:
        return {"digests": [row_to_dict(row) for row in store.recent_digests(limit)]}

    @app.get("/api/digests/{digest_id}")
    def digest_detail(digest_id: int, store: Store = Depends(get_store)) -> dict[str, Any]:
        row = row_to_dict(store.get_digest(digest_id))
        if row is None:
            raise HTTPException(status_code=404, detail=f"no digest with id {digest_id}")
        items = [
            {
                "id": item.id,
                "source": item.source,
                "title": item.title,
                "url": item.url,
                "summary": item.summary,
                "author": item.author,
                "published_at": item.published_at.isoformat() if item.published_at else None,
            }
            for item in store.items_for_digest(digest_id)
        ]
        return {"digest": row, "items": items}

    @app.get("/api/runs")
    def runs(
        limit: int = Query(25, ge=1, le=200), store: Store = Depends(get_store)
    ) -> dict[str, Any]:
        out = []
        for row in store.recent_runs(limit):
            run = row_to_dict(row) or {}
            run["sources"] = [row_to_dict(r) for r in store.source_results_for_run(int(run["id"]))]
            out.append(run)
        return {"runs": out}

    @app.get("/api/events")
    def events(
        limit: int = Query(100, ge=1, le=1000),
        level: list[str] | None = Query(None),
        store: Store = Depends(get_store),
    ) -> dict[str, Any]:
        return {"events": [row_to_dict(row) for row in store.recent_events(limit=limit, levels=level)]}

    @app.get("/api/pending")
    def pending(
        limit: int = Query(50, ge=1, le=500), store: Store = Depends(get_store)
    ) -> dict[str, Any]:
        items = store.pending_items(limit=limit)
        return {
            "count": store.count_pending(),
            "items": [
                {
                    "id": item.id,
                    "source": item.source,
                    "title": item.title,
                    "url": item.url,
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                    "first_seen_at": item.first_seen_at.isoformat() if item.first_seen_at else None,
                }
                for item in items
            ],
        }

    @app.get("/api/config")
    def config_view(config: Config = Depends(get_config)) -> dict[str, Any]:
        return {"config": config.redacted(), "config_path": str(config.config_path)}

    @app.get("/api/logfile", response_class=PlainTextResponse)
    def logfile(lines: int = Query(200, ge=1, le=5000), config: Config = Depends(get_config)) -> str:
        """Tail the bot's own log file, so the dashboard can show the real thing."""
        path = Path(config.paths.log_file)
        if not path.exists():
            return f"(no log file at {path} yet)"
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            tail = handle.readlines()[-lines:]
        return "".join(tail)

    # ------------------------------------------------------------------
    # built dashboard, when it exists
    # ------------------------------------------------------------------

    dist = Path(__file__).resolve().parent.parent / "dashboard" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(dist / "index.html")

    else:

        @app.get("/", response_class=PlainTextResponse)
        def no_dashboard() -> str:
            return (
                "daily-digest-bot API is up. The dashboard has not been built yet:\n"
                "  cd dashboard && npm install && npm run build\n\n"
                "API: /api/health /api/sources /api/digests /api/runs /api/events /api/pending\n"
            )

    return app


app = create_app()
