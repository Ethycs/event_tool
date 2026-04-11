"""Allow `python -m event_harvester.cli` to invoke the CLI."""

from event_harvester.cli.dispatch import main_sync

if __name__ == "__main__":
    main_sync()
