from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApplicationSettings(BaseModel):
    timezone: str = "America/Los_Angeles"
    schedule_hour: int = Field(default=0, ge=0, le=23)
    schedule_minute: int = Field(default=0, ge=0, le=59)
    discovery_window_hours: int = Field(default=48, ge=1)
    valuation_refresh_hours: int = Field(default=168, ge=1)
    sold_price_recheck_days: int = Field(default=14, ge=0)
    log_level: str = "INFO"
    user_agent: str

    @model_validator(mode="after")
    def validate_deployed_schedule(self) -> ApplicationSettings:
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(f"Unknown IANA timezone: {self.timezone}") from error
        if (self.schedule_hour, self.schedule_minute) != (0, 0):
            raise ValueError(
                "This release's LaunchAgent is fixed to daily midnight; "
                "schedule_hour and schedule_minute must both be 0"
            )
        return self


class PathSettings(BaseModel):
    database: Path
    exports: Path
    backups: Path
    evidence: Path
    logs: Path
    lock: Path


class NetworkSettings(BaseModel):
    timeout_seconds: float = Field(default=20, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_seconds: float = Field(default=1.0, ge=0)
    minimum_request_interval_seconds: float = Field(default=1.5, ge=0)


class RedditSettings(BaseModel):
    enabled: bool = True
    community: str = "Watchexchange"
    page_limit: int = Field(default=100, ge=1, le=100)
    max_requests_per_run: int = Field(default=100, ge=1)
    access_approved: bool = False
    deletion_contract_verified: bool = False
    client_id: str | None = None
    client_secret: str | None = None
    username: str | None = None


class Chrono24Settings(BaseModel):
    enabled: bool = False
    access_authorized: bool = False
    authorized_feed_path: Path | None = None
    require_verified_posted_date: bool = True


class SourceSettings(BaseModel):
    reddit: RedditSettings = RedditSettings()
    chrono24: Chrono24Settings = Chrono24Settings()


class BrandSettings(BaseModel):
    canonical: str
    aliases: list[str]


class CurrencySettings(BaseModel):
    base: str = "USD"
    provider_url: str
    cache_hours: int = Field(default=24, ge=1)


class ScoringSettings(BaseModel):
    version: str
    valuation_version: str
    strong_authenticity_risk_cap: float = 4.0
    scam_indicator_cap: float = 2.0


class RetentionSettings(BaseModel):
    daily_backups: int = Field(default=30, ge=1)
    evidence_days: int = Field(default=30, ge=0)
    log_days: int = Field(default=30, ge=1)


class Settings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    project_root: Path
    config_path: Path
    application: ApplicationSettings
    paths: PathSettings
    network: NetworkSettings
    sources: SourceSettings
    target_brands: list[BrandSettings]
    currency: CurrencySettings
    scoring: ScoringSettings
    retention: RetentionSettings

    @model_validator(mode="after")
    def validate_brand_aliases(self) -> Settings:
        aliases = [alias.casefold() for brand in self.target_brands for alias in brand.aliases]
        if len(aliases) != len(set(aliases)):
            raise ValueError("Target-brand aliases must be unique")
        return self

    def ensure_directories(self) -> None:
        private_directories = (
            self.paths.database.parent,
            self.paths.exports,
            self.paths.backups,
            self.paths.evidence,
            self.paths.logs,
            self.paths.lock.parent,
        )
        for path in private_directories:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        for private_file in (self.paths.database, self.paths.lock):
            if private_file.is_file() and not private_file.is_symlink():
                os.chmod(private_file, 0o600)
        for directory in (
            self.paths.exports,
            self.paths.backups,
            self.paths.evidence,
            self.paths.logs,
        ):
            for artifact in directory.iterdir():
                if artifact.is_file() and not artifact.is_symlink():
                    os.chmod(artifact, 0o600)


def _resolve_paths(raw: dict[str, Any], project_root: Path) -> None:
    for key, value in raw["paths"].items():
        path = Path(value).expanduser()
        raw["paths"][key] = path if path.is_absolute() else project_root / path


def _load_secure_environment(path: Path) -> None:
    if not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(
            f"Secrets file must not be accessible by group or others: {path} "
            f"(mode {mode:o}; expected 600)"
        )
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ValueError(f"Invalid secrets entry at {path}:{line_number}")
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key.startswith("WATCH_TRACKER_"):
            raise ValueError(
                f"Unsupported secrets key at {path}:{line_number}; "
                "only WATCH_TRACKER_* keys are accepted"
            )
        os.environ.setdefault(key, value.strip())


def load_settings(config_path: Path | str | None = None) -> Settings:
    configured = config_path or os.getenv("WATCH_TRACKER_CONFIG", "config/default.yaml")
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"Configuration file does not exist: {candidate}")
    _load_secure_environment(candidate.parent / "secrets.env")

    with candidate.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    project_root = candidate.parent.parent
    _resolve_paths(raw, project_root)

    env_timezone = os.getenv("WATCH_TRACKER_TIMEZONE")
    env_log_level = os.getenv("WATCH_TRACKER_LOG_LEVEL")
    env_user_agent = os.getenv("WATCH_TRACKER_USER_AGENT")
    env_database_url = os.getenv("WATCH_TRACKER_DATABASE_URL")
    if env_timezone:
        raw["application"]["timezone"] = env_timezone
    if env_log_level:
        raw["application"]["log_level"] = env_log_level
    if env_user_agent:
        raw["application"]["user_agent"] = env_user_agent
    if env_database_url:
        prefix = "sqlite:///"
        if not env_database_url.startswith(prefix):
            raise ValueError("Only sqlite:/// database URLs are supported in this release")
        database_path = Path(env_database_url.removeprefix(prefix)).expanduser()
        raw["paths"]["database"] = (
            database_path if database_path.is_absolute() else project_root / database_path
        )

    reddit = raw["sources"]["reddit"]
    reddit["enabled"] = os.getenv(
        "WATCH_TRACKER_REDDIT_ENABLED", str(reddit.get("enabled", True))
    ).casefold() in {"1", "true", "yes"}
    reddit["access_approved"] = os.getenv(
        "WATCH_TRACKER_REDDIT_ACCESS_APPROVED", str(reddit.get("access_approved", False))
    ).casefold() in {"1", "true", "yes"}
    reddit["deletion_contract_verified"] = os.getenv(
        "WATCH_TRACKER_REDDIT_DELETION_CONTRACT_VERIFIED",
        str(reddit.get("deletion_contract_verified", False)),
    ).casefold() in {"1", "true", "yes"}
    reddit["client_id"] = os.getenv("WATCH_TRACKER_REDDIT_CLIENT_ID")
    reddit["client_secret"] = os.getenv("WATCH_TRACKER_REDDIT_CLIENT_SECRET")
    reddit["username"] = os.getenv("WATCH_TRACKER_REDDIT_USERNAME")

    chrono24 = raw["sources"]["chrono24"]
    chrono24["enabled"] = os.getenv(
        "WATCH_TRACKER_CHRONO24_ENABLED", str(chrono24.get("enabled", False))
    ).casefold() in {"1", "true", "yes"}
    chrono24["access_authorized"] = os.getenv(
        "WATCH_TRACKER_CHRONO24_ACCESS_AUTHORIZED",
        str(chrono24.get("access_authorized", False)),
    ).casefold() in {"1", "true", "yes"}
    feed_value = os.getenv("WATCH_TRACKER_CHRONO24_FEED_PATH") or chrono24.get(
        "authorized_feed_path"
    )
    if feed_value:
        feed_path = Path(feed_value).expanduser()
        chrono24["authorized_feed_path"] = (
            feed_path if feed_path.is_absolute() else project_root / feed_path
        )

    settings = Settings(
        project_root=project_root,
        config_path=candidate,
        **raw,
    )
    settings.ensure_directories()
    return settings
