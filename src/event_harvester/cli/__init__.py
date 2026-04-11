"""Event Harvester CLI package.

The console_script entry point in pyproject.toml points at
``event_harvester.cli:main_sync``. This re-export keeps that working
even though the implementation lives in submodules.
"""

from event_harvester.cli.dispatch import main_sync

__all__ = ["main_sync"]
