"""Configuration loading and validation.

Config is TOML. Secrets never live in it: any ``${VAR}`` in a string is
substituted from the environment (or a local ``.env``), and a missing variable
is a hard error rather than a silently empty webhook URL.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

SENSITIVE_HEADERS = {"authorization", "x-api-key", "cookie", "proxy-authorization"}


class ConfigError(Exception):
    """Raised when the config file is missing, unparseable or invalid."""


# --------------------------------------------------------------------------
# .env + ${VAR} interpolation
# --------------------------------------------------------------------------


def load_dotenv(path: Path) -> dict[str, str]:
    """Read a simple KEY=value file. The real environment wins over the file."""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip("\"'")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def interpolate(value: Any, *, path: str = "") -> Any:
    """Recursively replace ${VAR} and ${VAR:-default} inside strings."""
    if isinstance(value, str):

        def _sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            env = os.environ.get(name)
            if env:
                return env
            if default is not None:
                return default
            raise ConfigError(
                f"{path or 'config'}: environment variable ${{{name}}} is not set. "
                "Export it or add it to .env (see .env.example)."
            )

        return ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: interpolate(v, path=f"{path}.{k}" if path else k) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, path=f"{path}[{i}]") for i, v in enumerate(value)]
    return value


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class Strict(BaseModel):
    """Base model that rejects unknown keys, so a typo is an error not a no-op."""

    model_config = ConfigDict(extra="forbid")


class _CommonSource(Strict):
    name: str
    url: str
    enabled: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=20.0, gt=0)
    max_items: int | None = Field(default=None, ge=1)
    # Case-insensitive substring filters applied to "title summary".
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    # Optional display name used in the digest instead of `name`.
    label: str | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source name must not be blank")
        return value.strip()

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"url must start with http:// or https://, got {value!r}")
        return value

    @property
    def display_name(self) -> str:
        return self.label or self.name


class RssSource(_CommonSource):
    kind: Literal["rss"] = "rss"


class ApiSource(_CommonSource):
    kind: Literal["api"] = "api"
    method: Literal["GET", "POST"] = "GET"
    params: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    # Dotted path to the list of items in the response.
    # "" means the response body is itself the list.
    items_path: str = ""
    # Maps digest item fields -> dotted paths within each API item.
    fields: dict[str, str] = Field(default_factory=lambda: {"title": "title", "url": "url"})

    @field_validator("fields")
    @classmethod
    def _check_fields(cls, value: dict[str, str]) -> dict[str, str]:
        if "title" not in value:
            raise ValueError("fields must map a 'title'")
        allowed = {"title", "url", "summary", "author", "published_at", "id"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(
                f"unsupported field name(s) {unknown}; allowed: {sorted(allowed)}"
            )
        return value


class ScrapeSource(_CommonSource):
    kind: Literal["scrape"] = "scrape"
    # CSS selectors. "item" picks each row; the others select inside a row.
    # Append "@attr" to read an attribute instead of text, e.g. "a@href".
    selectors: dict[str, str]

    @field_validator("selectors")
    @classmethod
    def _check_selectors(cls, value: dict[str, str]) -> dict[str, str]:
        for required in ("item", "title"):
            if required not in value:
                raise ValueError(f"selectors must include {required!r}")
        allowed = {"item", "title", "url", "summary", "author", "published_at"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(
                f"unsupported selector name(s) {unknown}; allowed: {sorted(allowed)}"
            )
        return value


SourceConfig = Annotated[RssSource | ApiSource | ScrapeSource, Field(discriminator="kind")]

ChannelKind = Literal["slack", "discord", "webhook", "file", "stdout"]
WEBHOOK_KINDS = {"slack", "discord", "webhook"}


class DeliveryConfig(Strict):
    kind: ChannelKind = "stdout"
    webhook_url: str | None = None
    # Human label for the destination, shown in the dashboard and logs.
    channel: str = "local"
    timeout_seconds: float = Field(default=20.0, gt=0)
    # Used when kind = "file".
    path: str = "data/digests"
    username: str | None = None

    @model_validator(mode="after")
    def _webhook_required(self) -> DeliveryConfig:
        if self.kind in WEBHOOK_KINDS and not self.webhook_url:
            raise ValueError(f"delivery.kind={self.kind!r} requires delivery.webhook_url")
        return self


class AlertConfig(Strict):
    """Secondary channel, used only to shout when the digest itself cannot go out."""

    enabled: bool = False
    kind: ChannelKind = "stdout"
    webhook_url: str | None = None
    channel: str = "alerts"
    timeout_seconds: float = Field(default=20.0, gt=0)
    path: str = "data/alerts"
    username: str | None = None

    @model_validator(mode="after")
    def _webhook_required(self) -> AlertConfig:
        if self.enabled and self.kind in WEBHOOK_KINDS and not self.webhook_url:
            raise ValueError("alerts.webhook_url is required when alerts.enabled = true")
        return self


class BotConfig(Strict):
    timezone: str = "UTC"
    # Earliest local time the day's digest may be sent, "HH:MM".
    send_after: str = "08:00"
    title: str = "Daily Digest"
    max_items_per_digest: int = Field(default=30, ge=1)
    max_items_per_source: int = Field(default=10, ge=1)
    # Items published longer ago than this are recorded but never surfaced.
    lookback_hours: int = Field(default=72, ge=1)
    # Post even with nothing new, so a quiet channel always means a broken bot.
    post_when_empty: bool = True
    # Health thresholds, also used by the `healthcheck` command.
    max_silence_hours: int = Field(default=26, ge=1)
    unhealthy_after_consecutive_failures: int = Field(default=3, ge=1)
    dedupe_by: list[Literal["url", "title"]] = Field(default_factory=lambda: ["url", "title"])
    user_agent: str = "daily-digest-bot/1.0 (+https://github.com/MOTOMOTO2/daily-digest-bot)"

    @field_validator("send_after")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError(f"send_after must look like HH:MM, got {value!r}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"send_after must be a real time of day, got {value!r}")
        return f"{hour:02d}:{minute:02d}"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"unknown timezone {value!r}; use an IANA name like America/Toronto ({exc})"
            ) from exc
        return value

    @field_validator("dedupe_by")
    @classmethod
    def _non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("dedupe_by must list at least one of 'url' or 'title'")
        return value


class PathsConfig(Strict):
    database: str = "data/digest.db"
    log_file: str = "logs/digest-bot.log"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_max_bytes: int = Field(default=5_000_000, ge=10_000)
    log_backups: int = Field(default=5, ge=0)


class Config(Strict):
    bot: BotConfig = Field(default_factory=BotConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    sources: list[SourceConfig] = Field(default_factory=list)

    # Absolute path of the file this came from; not part of the TOML.
    config_path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _validate_sources(self) -> Config:
        if not self.sources:
            raise ValueError(
                "no [[sources]] configured, so the bot would have nothing to collect; "
                "copy config.example.toml and add at least one source"
            )
        names = [source.name for source in self.sources]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate source name(s) {duplicates}; names must be unique")
        if not any(source.enabled for source in self.sources):
            raise ValueError("every source is disabled, so nothing would ever be collected")
        return self

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [source for source in self.sources if source.enabled]

    def source_by_name(self, name: str) -> SourceConfig | None:
        for source in self.sources:
            if source.name == name:
                return source
        return None

    def redacted(self) -> dict[str, Any]:
        """Config as plain data with secrets masked, for the dashboard."""
        data = self.model_dump(mode="json")
        for section in ("delivery", "alerts"):
            url = data.get(section, {}).get("webhook_url")
            if url:
                data[section]["webhook_url"] = mask(url)
        for source in data.get("sources", []):
            headers = source.get("headers") or {}
            for key in list(headers):
                if key.lower() in SENSITIVE_HEADERS:
                    headers[key] = "***"
        return data


def mask(secret: str) -> str:
    if len(secret) <= 16:
        return "***"
    return f"{secret[:16]}...{secret[-4:]}"


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_config(
    path: str | os.PathLike[str],
    *,
    dotenv: str | os.PathLike[str] | None = ".env",
) -> Config:
    """Load, interpolate and validate the config file.

    Raises ConfigError with an actionable message on any problem.
    """
    config_path = Path(path).resolve()
    if dotenv is not None:
        dotenv_path = Path(dotenv)
        if not dotenv_path.is_absolute():
            dotenv_path = config_path.parent / dotenv_path
        load_dotenv(dotenv_path)

    if not config_path.exists():
        raise ConfigError(
            f"config file not found: {config_path}\n"
            "Copy config.example.toml to config.toml and edit it."
        )
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc

    raw = interpolate(raw)
    try:
        config = Config.model_validate(raw)
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"{config_path} is invalid:\n{format_errors(exc)}") from exc
    config.config_path = config_path
    return config


def format_errors(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    lines = []
    for error in errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
        lines.append(f"  - {location}: {error.get('msg')}")
    return "\n".join(lines)
