from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from watch_tracker.config import Settings


def alembic_config(settings: Settings) -> Config:
    ini_path = settings.project_root / "alembic.ini"
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(settings.project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{settings.paths.database}")
    return config


def upgrade_to_head(settings: Settings) -> None:
    command.upgrade(alembic_config(settings), "head")


def migration_state(settings: Settings, engine: Engine) -> tuple[str | None, str]:
    config = alembic_config(settings)
    expected = ScriptDirectory.from_config(config).get_current_head()
    if not settings.paths.database.exists():
        return None, expected or ""
    with engine.connect() as connection:
        current = MigrationContext.configure(
            connection, opts={"version_table": "alembic_versions"}
        ).get_current_revision()
    return current, expected or ""


def assert_at_head(settings: Settings, engine: Engine) -> None:
    current, expected = migration_state(settings, engine)
    if current != expected:
        raise RuntimeError(
            f"Database migration is {current or 'not initialized'}; expected {expected}. "
            "Run `watch-tracker migrate` first."
        )
