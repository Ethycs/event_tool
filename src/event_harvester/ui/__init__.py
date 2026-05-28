"""Flet web UI + terminal display.

The Flet app (``app.EventsApp``) is the primary control surface; the
launcher in ``server`` glues it to ``flet.app_async`` and ``display``
holds the ANSI helpers + per-message printer used by the CLI path.
"""
