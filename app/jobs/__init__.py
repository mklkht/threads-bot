"""Scheduled jobs and shared helpers for pause/heartbeat files."""

from pathlib import Path

from app.config import Settings


def data_dir(settings: Settings) -> Path:
    """Directory that holds the SQLite file and control files (PAUSE, heartbeat)."""
    return Path(settings.db_path).parent


def is_paused(settings: Settings) -> bool:
    """True if the operator created data/PAUSE to suspend posting and replies."""
    return (data_dir(settings) / "PAUSE").exists()
