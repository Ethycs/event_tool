"""Flet events browser — interactive UI for reviewing and acting on extracted events.

The UI runs in two phases:

1. **Progress view** (initial): a stage checklist + live log panel updates as
   the pipeline runs in a background task. The user sees what's happening
   the moment the page loads.
2. **Events view**: when the pipeline calls `set_events()`, the UI swaps to
   the cards-and-filters review screen.

If `pipeline_runner` is not provided, phase 1 is skipped and the events view
is rendered directly (used when events are already extracted out-of-band).
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

import flet as ft

from event_harvester.event_match import find_acted_fingerprint, save_fingerprint

logger = logging.getLogger("event_harvester.app")

_SEED_COLOR = "#6750A4"
_LOG_LINE_LIMIT = 250

_PIPELINE_STAGES: list[tuple[str, str]] = [
    ("harvest", "Harvest messages"),
    ("filter", "Filter read/sent"),
    ("extract", "Extract events (LLM)"),
    ("match", "Match fingerprints"),
    ("done", "Done"),
]


def _status_for(event: dict) -> str:
    """Determine display status for an event.

    Only fingerprints the user has explicitly acted on count as "seen".
    Auto-saved entries (e.g. from the web link dedup pass) leave the
    event as "new" so the user gets to review it.
    """
    if event.get("_status"):
        return event["_status"]
    fp = find_acted_fingerprint(event)
    if fp is None:
        return "new"
    return fp.get("status") or "new"


def _status_icon(status: str) -> ft.Icon:
    icons = {
        "new": ft.Icon(ft.Icons.CIRCLE, color=ft.Colors.GREEN, size=14),
        "known": ft.Icon(ft.Icons.CIRCLE, color=ft.Colors.GREY, size=14),
        "approved": ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.GREEN, size=14),
        "declined": ft.Icon(ft.Icons.CANCEL, color=ft.Colors.RED, size=14),
    }
    return icons.get(status, icons["new"])


def _source_platform(event: dict) -> str:
    """Extract platform from source string like '@author in Telegram / channel'."""
    source = event.get("source", "")
    for plat in ("Discord", "Telegram", "Gmail", "Signal"):
        if plat.lower() in source.lower():
            return plat
    if event.get("_platform"):
        return event["_platform"]
    return "Web"


PipelineRunner = Callable[["ProgressReporter", "EventsApp"], Awaitable[None]]


class ProgressReporter:
    """Bridges a background pipeline task to the Flet progress UI.

    The pipeline calls `update_stage()` at major transitions and `log()`
    for free-form lines. A `_UILogHandler` (installed by EventsApp) also
    forwards `event_harvester.*` log records here automatically, so the
    pipeline gets observability without explicit reporter calls.
    """

    def __init__(self, app: "EventsApp"):
        self.app = app
        self._current_stage: Optional[str] = None

    def update_stage(self, stage_id: str, label: Optional[str] = None) -> None:
        self._current_stage = stage_id
        try:
            idx = next(i for i, (sid, _) in enumerate(_PIPELINE_STAGES) if sid == stage_id)
        except StopIteration:
            idx = -1
        for i, (sid, _) in enumerate(_PIPELINE_STAGES):
            if i < idx:
                self.app._mark_stage(sid, "done")
            elif i == idx:
                self.app._mark_stage(sid, "active")
            else:
                self.app._mark_stage(sid, "pending")
        if label:
            self.app._set_stage_label(label)
        else:
            display = next((lbl for sid, lbl in _PIPELINE_STAGES if sid == stage_id), stage_id)
            self.app._set_stage_label(display)
        self.app._refresh_page()

    def log(self, line: str, color=None) -> None:
        self.app._append_log(line, color=color)

    def error(self, message: str) -> None:
        self.app._append_log(f"ERROR: {message}", color=ft.Colors.RED)
        if self._current_stage:
            self.app._mark_stage(self._current_stage, "error")
        self.app._refresh_page()

    async def checkpoint(self) -> None:
        """Honor a Pause request from the UI.

        Pipeline runners should call this between stages so the user can
        pause the pipeline at well-defined points. If the pause flag is
        cleared, this awaits until the user clicks Resume; otherwise it
        returns immediately.
        """
        ev = self.app._pause_event
        if ev is None:
            return
        if not ev.is_set():
            self.app._set_stage_label("Paused — click Resume to continue")
            self.app._refresh_page()
            await ev.wait()


class _UILogHandler(logging.Handler):
    """Forward event_harvester log records to the Flet progress panel."""

    def __init__(self, reporter: ProgressReporter):
        super().__init__()
        self.reporter = reporter
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            color = ft.Colors.RED if record.levelno >= logging.ERROR else None
            self.reporter.log(self.format(record), color=color)
        except Exception:
            self.handleError(record)


class EventsApp:
    """Flet events browser with a leading pipeline-progress view."""

    def __init__(
        self,
        cfg,
        *,
        events: Optional[list[dict]] = None,
        rejects: Optional[dict] = None,
        stats: Optional[dict] = None,
        pipeline_runner: Optional[PipelineRunner] = None,
    ):
        self.cfg = cfg
        self.pipeline_runner = pipeline_runner
        self.events: list[dict] = events or []
        self.rejects: dict = rejects or {}
        self.stats: dict = stats or {}

        self._tt = None
        self._page: Optional[ft.Page] = None
        self._pipeline_done = False

        # Pipeline lifecycle: idle | running | paused | done
        self._state: str = "idle"
        self._pipeline_task: Optional[asyncio.Task] = None
        # Set means "go"; cleared means "wait at next checkpoint".
        self._pause_event: Optional[asyncio.Event] = None

        # Progress view controls
        self._stage_label: Optional[ft.Text] = None
        self._stage_rows: dict[str, ft.Row] = {}
        self._log_column = None
        self._cap_fields: dict[str, ft.TextField] = {}
        self._source_checkboxes: dict[str, ft.Checkbox] = {}
        self._days_field: Optional[ft.TextField] = None
        self._behavior_checkboxes: dict[str, ft.Checkbox] = {}
        self._action_bar: Optional[ft.Row] = None

        # Events view controls
        self._cards_column: Optional[ft.Column] = None
        self._status_filter: Optional[ft.Dropdown] = None
        self._source_filter: Optional[ft.Dropdown] = None
        self._search_field: Optional[ft.TextField] = None
        self._header_text: Optional[ft.Text] = None

        for ev in self.events:
            if "_status" not in ev:
                ev["_status"] = _status_for(ev)

    def set_events(
        self,
        events: list[dict],
        rejects: Optional[dict] = None,
        stats: Optional[dict] = None,
    ) -> None:
        """Called by the pipeline when extraction is complete; swaps views."""
        self.events = events
        if rejects is not None:
            self.rejects = rejects
        if stats is not None:
            self.stats = stats
        for ev in self.events:
            if "_status" not in ev:
                ev["_status"] = _status_for(ev)
        self._pipeline_done = True
        if self._page is not None:
            self._render_events_view()

    def _get_tt(self):
        if self._tt is None:
            from event_harvester.ticktick import get_ticktick_client
            self._tt = get_ticktick_client(self.cfg.ticktick)
        return self._tt

    def _on_approve(self, event_dict):
        def handler(e):
            try:
                if not self.cfg.dry_run:
                    tt = self._get_tt()
                    if tt:
                        from event_harvester.ticktick import create_ticktick_tasks
                        create_ticktick_tasks(
                            tt, [event_dict],
                            project_name=self.cfg.ticktick.project,
                        )
                else:
                    logger.info("Dry-run: not creating TickTick task for %r",
                                event_dict.get("title"))
                save_fingerprint(event_dict, status="approved")
                event_dict["_status"] = "approved"
            except Exception as ex:
                logger.error("Approve failed: %s", ex)
                event_dict["_status"] = "approved"
                save_fingerprint(event_dict, status="approved")
            self._rebuild_cards()
        return handler

    def _on_decline(self, event_dict):
        def handler(e):
            save_fingerprint(event_dict, status="declined")
            event_dict["_status"] = "declined"
            self._rebuild_cards()
        return handler

    def _on_approve_all(self, e):
        tt = None if self.cfg.dry_run else self._get_tt()
        for ev in self.events:
            if ev.get("_status") == "new":
                try:
                    if tt:
                        from event_harvester.ticktick import create_ticktick_tasks
                        create_ticktick_tasks(
                            tt, [ev], project_name=self.cfg.ticktick.project,
                        )
                    elif self.cfg.dry_run:
                        logger.info("Dry-run: skipping TickTick for %r",
                                    ev.get("title"))
                except Exception as ex:
                    logger.error("Approve failed for %s: %s", ev.get("title"), ex)
                save_fingerprint(ev, status="approved")
                ev["_status"] = "approved"
        self._rebuild_cards()

    def _on_decline_all(self, e):
        for ev in self.events:
            if ev.get("_status") == "new":
                save_fingerprint(ev, status="declined")
                ev["_status"] = "declined"
        self._rebuild_cards()

    def _on_filter_change(self, e):
        self._rebuild_cards()

    def _on_search_change(self, e):
        self._rebuild_cards()

    def _filtered_events(self) -> list[dict]:
        status = self._status_filter.value if self._status_filter else "All"
        source = self._source_filter.value if self._source_filter else "All"
        query = (self._search_field.value or "").lower().strip() if self._search_field else ""

        result = []
        for ev in self.events:
            if status != "All" and ev.get("_status", "new") != status.lower():
                continue
            if source != "All" and _source_platform(ev) != source:
                continue
            if query:
                searchable = " ".join([
                    ev.get("title", ""),
                    ev.get("notes", ""),
                    ev.get("source", ""),
                    ev.get("location", "") or "",
                ]).lower()
                if query not in searchable:
                    continue
            result.append(ev)
        return result

    def _build_event_card(self, ev: dict) -> ft.Card:
        status = ev.get("_status", "new")
        is_acted = status in ("approved", "declined")

        title_text = ev.get("title") or "Untitled"
        date_str = ev.get("date") or ""
        time_str = ev.get("time") or ""
        location = ev.get("location") or ""
        source = ev.get("source") or ""
        notes = ev.get("notes") or ""
        link = ev.get("link") or ""

        subtitle_parts = []
        if date_str:
            subtitle_parts.append(date_str)
        if time_str:
            subtitle_parts.append(time_str)
        if location:
            subtitle_parts.append(location)

        card_content = [
            ft.ListTile(
                leading=_status_icon(status),
                title=ft.Text(title_text, weight=ft.FontWeight.BOLD, size=16),
                subtitle=ft.Text(" · ".join(subtitle_parts)) if subtitle_parts else None,
            ),
        ]

        if notes:
            card_content.append(
                ft.Container(
                    ft.Text(notes[:300], size=13, color=ft.Colors.ON_SURFACE_VARIANT),
                    padding=ft.padding.only(left=16, right=16, bottom=4),
                )
            )

        if source:
            card_content.append(
                ft.Container(
                    ft.Text(source, size=12, italic=True, color=ft.Colors.OUTLINE),
                    padding=ft.padding.only(left=16, right=16, bottom=4),
                )
            )

        actions = []
        if link:
            actions.append(
                ft.TextButton(
                    "Open link",
                    url=link,
                    icon=ft.Icons.OPEN_IN_NEW,
                )
            )
        if not is_acted:
            actions.extend([
                ft.FilledButton(
                    "Approve",
                    icon=ft.Icons.CHECK,
                    on_click=self._on_approve(ev),
                ),
                ft.OutlinedButton(
                    "Decline",
                    icon=ft.Icons.CLOSE,
                    on_click=self._on_decline(ev),
                ),
            ])
        else:
            actions.append(
                ft.Text(
                    status.capitalize(),
                    color=ft.Colors.GREEN if status == "approved" else ft.Colors.RED,
                    weight=ft.FontWeight.BOLD,
                    size=13,
                )
            )

        card_content.append(
            ft.Container(
                ft.Row(actions, alignment=ft.MainAxisAlignment.END),
                padding=ft.padding.only(right=8, bottom=8),
            )
        )

        return ft.Card(
            ft.Column(card_content, spacing=0),
            elevation=2 if not is_acted else 0,
        )

    def _rebuild_cards(self):
        if not self._cards_column or not self._page:
            return

        visible = self._filtered_events()
        self._cards_column.controls = [self._build_event_card(ev) for ev in visible]

        n_new = sum(1 for ev in self.events if ev.get("_status") == "new")
        n_known = sum(1 for ev in self.events if ev.get("_status") == "known")
        n_approved = sum(1 for ev in self.events if ev.get("_status") == "approved")
        n_declined = sum(1 for ev in self.events if ev.get("_status") == "declined")
        self._header_text.value = (
            f"{len(self.events)} events · "
            f"{n_new} new · {n_known} known · "
            f"{n_approved} approved · {n_declined} declined"
        )

        self._refresh_page()

    def _build_reject_panels(self) -> "ft.ExpansionPanelList | None":
        stage_labels = {
            "classifier": "Classifier",
            "reranker": "Reranker",
            "caps": "Per-source caps",
            "llm_past": "LLM (past events)",
            "llm_no_events": "LLM (no events)",
        }
        panels = []
        for stage in ("classifier", "reranker", "caps", "llm_past", "llm_no_events"):
            items = self.rejects.get(stage, [])
            if not items:
                continue
            label = stage_labels.get(stage, stage)

            if stage == "llm_past":
                rows = []
                for ev in items[:50]:
                    rows.append(ft.Text(
                        f"{ev.get('title', '?')} — {ev.get('date', '?')} ({ev.get('reason', '?')})",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                    ))
            else:
                rows = []
                for m in items[:50]:
                    snippet = (m.get("content") or "")[:100].replace("\n", " ")
                    rows.append(ft.Text(
                        f"{m.get('author', '?')}: {snippet}",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                    ))
                if len(items) > 50:
                    rows.append(ft.Text(
                        f"... and {len(items) - 50} more",
                        size=12, italic=True,
                    ))

            panels.append(ft.ExpansionPanel(
                header=ft.ListTile(
                    title=ft.Text(f"{label} ({len(items)} rejected)"),
                ),
                content=ft.Container(
                    content=ft.ListView(controls=rows, spacing=2, padding=4),
                    height=260,
                    padding=ft.padding.symmetric(horizontal=12, vertical=4),
                ),
            ))

        if not panels:
            return None
        return ft.ExpansionPanelList(panels, elevation=1)

    # ── Progress view helpers ───────────────────────────────────────────

    def _mark_stage(self, stage_id: str, state: str) -> None:
        row = self._stage_rows.get(stage_id)
        if row is None:
            return
        icon, text = row.controls
        if state == "active":
            icon.name = ft.Icons.RADIO_BUTTON_CHECKED
            icon.color = ft.Colors.PRIMARY
            text.weight = ft.FontWeight.BOLD
        elif state == "done":
            icon.name = ft.Icons.CHECK_CIRCLE
            icon.color = ft.Colors.GREEN
            text.weight = ft.FontWeight.NORMAL
        elif state == "error":
            icon.name = ft.Icons.ERROR
            icon.color = ft.Colors.RED
            text.weight = ft.FontWeight.BOLD
        else:
            icon.name = ft.Icons.CIRCLE_OUTLINED
            icon.color = ft.Colors.OUTLINE
            text.weight = ft.FontWeight.NORMAL

    def _set_stage_label(self, label: str) -> None:
        if self._stage_label:
            self._stage_label.value = label

    def _append_log(self, line: str, color=None) -> None:
        if self._log_column is None:
            return
        text = ft.Text(
            line,
            size=11,
            color=color or ft.Colors.ON_SURFACE_VARIANT,
            font_family="monospace",
            selectable=True,
        )
        self._log_column.controls.append(text)
        if len(self._log_column.controls) > _LOG_LINE_LIMIT:
            self._log_column.controls = self._log_column.controls[-_LOG_LINE_LIMIT:]
        self._refresh_page()

    def _refresh_page(self) -> None:
        if self._page is None:
            return
        try:
            self._page.update()
        except Exception:
            pass

    # ── View rendering ──────────────────────────────────────────────────

    def _build_config_section(self) -> ft.Container:
        """Build the per-source caps + days/sources/behavior config card.

        Rebuilt fresh each time it's mounted so the same EventsApp can
        show it in both the progress view and as a collapsible header
        in the events view.
        """
        # Cap fields — one numeric field per category + total
        self._cap_fields = {}
        cap_specs = [
            ("discord", "Discord"),
            ("telegram", "Telegram"),
            ("gmail", "Gmail"),
            ("signal", "Signal"),
            ("web", "Web"),
            ("total", "Total"),
        ]
        cap_field_controls = []
        for key, label in cap_specs:
            current = getattr(self.cfg.caps, key, 0)
            field = ft.TextField(
                label=label,
                value=str(current),
                width=110,
                text_align=ft.TextAlign.CENTER,
                dense=True,
            )
            self._cap_fields[key] = field
            cap_field_controls.append(field)

        self._days_field = ft.TextField(
            label="Days back",
            value=str(self.cfg.days_back),
            width=120,
            text_align=ft.TextAlign.CENTER,
            dense=True,
        )

        self._source_checkboxes = {}
        source_specs = [
            ("discord", "Discord"),
            ("telegram", "Telegram"),
            ("gmail", "Gmail"),
            ("signal", "Signal"),
            ("web", "Web"),
        ]
        source_cb_controls = []
        for key, label in source_specs:
            cb = ft.Checkbox(label=label, value=getattr(self.cfg.sources, key, True))
            self._source_checkboxes[key] = cb
            source_cb_controls.append(cb)

        self._behavior_checkboxes = {}
        behavior_specs = [
            ("no_cooldown", "Skip web cooldown", self.cfg.web.no_cooldown),
            ("skip_analyze", "Skip LLM analysis", self.cfg.skip_analyze),
            ("dry_run", "Dry-run TickTick", self.cfg.dry_run),
        ]
        behavior_cb_controls = []
        for key, label, default in behavior_specs:
            cb = ft.Checkbox(label=label, value=default)
            self._behavior_checkboxes[key] = cb
            behavior_cb_controls.append(cb)

        # All toggles on one left-to-right row (horizontal scroll if it
        # overflows the container). Order: days input, then a "Sources:"
        # cluster of checkboxes, a small spacer, then a "Behavior:"
        # cluster.
        toggles_row = ft.Row(
            [
                self._days_field,
                ft.VerticalDivider(width=12),
                ft.Text("Sources:", color=ft.Colors.OUTLINE, size=12),
                *source_cb_controls,
                ft.VerticalDivider(width=12),
                ft.Text("Behavior:", color=ft.Colors.OUTLINE, size=12),
                *behavior_cb_controls,
            ],
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        return ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        "Pipeline config",
                        size=12,
                        weight=ft.FontWeight.BOLD,
                        color=ft.Colors.OUTLINE,
                    ),
                    toggles_row,
                    ft.Divider(height=1),
                    ft.Text(
                        "Per-source caps",
                        size=12,
                        weight=ft.FontWeight.BOLD,
                        color=ft.Colors.OUTLINE,
                    ),
                    ft.Row(
                        cap_field_controls,
                        spacing=8,
                        scroll=ft.ScrollMode.AUTO,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=8,
            ),
            border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
            padding=12,
        )

    def _render_progress_view(self) -> None:
        page = self._page
        page.controls.clear()
        page.appbar = ft.AppBar(
            title=ft.Text("Event Harvester"),
            center_title=False,
            actions=[
                ft.IconButton(
                    ft.Icons.BRIGHTNESS_6,
                    tooltip="Toggle dark mode",
                    on_click=self._toggle_theme,
                ),
            ],
        )

        self._stage_label = ft.Text(
            "Ready", size=20, weight=ft.FontWeight.BOLD,
        )

        self._stage_rows = {}
        stage_rows = []
        for sid, label in _PIPELINE_STAGES:
            icon = ft.Icon(ft.Icons.CIRCLE_OUTLINED, color=ft.Colors.OUTLINE, size=18)
            text = ft.Text(label, size=14)
            row = ft.Row([icon, text], spacing=10)
            self._stage_rows[sid] = row
            stage_rows.append(row)

        config_section = self._build_config_section()

        # Action bar — Start / Pause / Resume / Stop, in that fixed order
        # so _update_action_bar can index by position.
        start_btn = ft.FilledButton(
            "Start", icon=ft.Icons.PLAY_ARROW, on_click=self._start_clicked,
        )
        pause_btn = ft.OutlinedButton(
            "Pause", icon=ft.Icons.PAUSE, on_click=self._pause_clicked,
        )
        resume_btn = ft.OutlinedButton(
            "Resume", icon=ft.Icons.PLAY_ARROW, on_click=self._resume_clicked,
        )
        stop_btn = ft.OutlinedButton(
            "Stop", icon=ft.Icons.STOP, on_click=self._stop_clicked,
        )
        self._action_bar = ft.Row(
            [start_btn, pause_btn, resume_btn, stop_btn],
            spacing=8,
        )

        self._log_column = ft.ListView(spacing=2, padding=4, auto_scroll=True)
        log_panel = ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        "Live log",
                        size=12,
                        weight=ft.FontWeight.BOLD,
                        color=ft.Colors.OUTLINE,
                    ),
                    ft.Container(
                        content=self._log_column,
                        expand=True,
                    ),
                ],
                expand=True,
                spacing=6,
            ),
            border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
            padding=12,
            expand=True,
        )

        page.add(
            ft.Row(
                [
                    ft.Container(self._stage_label, expand=True),
                    self._action_bar,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            ft.ProgressBar(),
            config_section,
            ft.Container(
                content=ft.Column(stage_rows, spacing=8),
                padding=ft.padding.symmetric(vertical=12),
            ),
            ft.Divider(),
            log_panel,
        )
        # Reflect current state on the freshly-rendered controls.
        self._update_action_bar()
        page.update()

    def _render_events_view(self) -> None:
        page = self._page
        page.controls.clear()

        n_new = sum(1 for ev in self.events if ev.get("_status") == "new")
        n_known = sum(1 for ev in self.events if ev.get("_status") == "known")
        self._header_text = ft.Text(
            f"{len(self.events)} events · {n_new} new · {n_known} known",
            size=14,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )

        self._status_filter = ft.Dropdown(
            label="Status",
            value="All",
            options=[ft.dropdown.Option(s) for s in [
                "All", "New", "Known", "Approved", "Declined",
            ]],
            width=140,
            on_select=self._on_filter_change,
        )

        sources = sorted({_source_platform(ev) for ev in self.events})
        self._source_filter = ft.Dropdown(
            label="Source",
            value="All",
            options=[ft.dropdown.Option("All")] + [
                ft.dropdown.Option(s) for s in sources
            ],
            width=140,
            on_select=self._on_filter_change,
        )

        self._search_field = ft.TextField(
            label="Search",
            prefix_icon=ft.Icons.SEARCH,
            on_change=self._on_search_change,
            expand=True,
        )

        filter_row = ft.Row([
            self._status_filter,
            self._source_filter,
            self._search_field,
        ], spacing=10)

        self._cards_column = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO)
        for ev in self._filtered_events():
            self._cards_column.controls.append(self._build_event_card(ev))

        stats_text = ""
        if self.stats:
            parts = []
            if "total_messages" in self.stats:
                parts.append(f"{self.stats['total_messages']} msgs")
            if "actionable" in self.stats:
                parts.append(f"{self.stats['actionable']} actionable")
            if "events" in self.stats:
                parts.append(f"{self.stats['events']} events")
            stats_text = " → ".join(parts)

        reject_panels = self._build_reject_panels()

        pipeline_section = ft.Column(spacing=4)
        if stats_text:
            pipeline_section.controls.append(
                ft.Text(f"Pipeline: {stats_text}", size=13, color=ft.Colors.OUTLINE)
            )
        if reject_panels:
            pipeline_section.controls.append(reject_panels)

        # Keep the live log accessible via an expander
        if self._log_column is not None and self._log_column.controls:
            log_panel = ft.ExpansionTile(
                title=ft.Text("Pipeline log", size=13),
                controls=[
                    ft.Container(
                        content=self._log_column,
                        height=240,
                        padding=10,
                    ),
                ],
            )
            pipeline_section.controls.append(log_panel)

        page.appbar = ft.AppBar(
            title=ft.Text("Event Harvester"),
            center_title=False,
            actions=[
                ft.IconButton(
                    ft.Icons.REFRESH,
                    tooltip="Re-run pipeline",
                    on_click=self._rerun_clicked,
                ),
                ft.IconButton(
                    ft.Icons.CHECK_CIRCLE_OUTLINE,
                    tooltip="Approve all new",
                    on_click=self._on_approve_all,
                ),
                ft.IconButton(
                    ft.Icons.CANCEL_OUTLINED,
                    tooltip="Decline all",
                    on_click=self._on_decline_all,
                ),
                ft.IconButton(
                    ft.Icons.BRIGHTNESS_6,
                    tooltip="Toggle dark mode",
                    on_click=self._toggle_theme,
                ),
            ],
        )

        # Surface the pipeline config + action bar at the top of the
        # events view so caps, source toggles, days-back, and Start /
        # Stop / Pause / Re-run remain reachable after a run completes.
        config_section = self._build_config_section()
        start_btn = ft.FilledButton(
            "Start", icon=ft.Icons.PLAY_ARROW, on_click=self._start_clicked,
        )
        pause_btn = ft.OutlinedButton(
            "Pause", icon=ft.Icons.PAUSE, on_click=self._pause_clicked,
        )
        resume_btn = ft.OutlinedButton(
            "Resume", icon=ft.Icons.PLAY_ARROW, on_click=self._resume_clicked,
        )
        stop_btn = ft.OutlinedButton(
            "Stop", icon=ft.Icons.STOP, on_click=self._stop_clicked,
        )
        self._action_bar = ft.Row(
            [start_btn, pause_btn, resume_btn, stop_btn], spacing=8,
        )
        controls_panel = ft.ExpansionTile(
            title=ft.Text("Pipeline controls", size=13),
            controls=[
                ft.Container(
                    content=ft.Column(
                        [self._action_bar, config_section],
                        spacing=8,
                    ),
                    padding=10,
                ),
            ],
        )

        page.add(
            controls_panel,
            self._header_text,
            filter_row,
            ft.Divider(),
            ft.Container(
                self._cards_column,
                expand=True,
            ),
            ft.Divider(),
            pipeline_section,
        )
        # Action bar buttons reflect current pipeline state.
        self._update_action_bar()
        page.update()

    def _toggle_theme(self, e):
        page = self._page
        if page is None:
            return
        page.theme_mode = (
            ft.ThemeMode.LIGHT
            if page.theme_mode == ft.ThemeMode.DARK
            else ft.ThemeMode.DARK
        )
        page.update()

    # ── Pipeline lifecycle ──────────────────────────────────────────────

    def _read_config_from_fields(self) -> None:
        """Pull current UI-control values into cfg.

        Covers: per-source caps, days_back, source enable flags, web
        cooldown bypass, skip-analyze, dry-run. Called on Start so the
        next pipeline run honors any UI edits.
        """
        for key, field in self._cap_fields.items():
            try:
                value = int((field.value or "").strip())
            except (TypeError, ValueError):
                continue
            if value < 0:
                continue
            setattr(self.cfg.caps, key, value)

        if self._days_field is not None:
            try:
                d = int((self._days_field.value or "").strip())
                if d > 0:
                    self.cfg.days_back = d
            except (TypeError, ValueError):
                pass

        for key, cb in self._source_checkboxes.items():
            setattr(self.cfg.sources, key, bool(cb.value))

        for key, cb in self._behavior_checkboxes.items():
            if key == "no_cooldown":
                self.cfg.web.no_cooldown = bool(cb.value)
            elif key == "skip_analyze":
                self.cfg.skip_analyze = bool(cb.value)
            elif key == "dry_run":
                self.cfg.dry_run = bool(cb.value)

    def _start_clicked(self, e) -> None:
        if self._state in ("running", "paused"):
            return
        if self._pipeline_task is not None and not self._pipeline_task.done():
            return
        if self.pipeline_runner is None:
            return
        # Cap fields might exist from a prior progress-view render; pull
        # the latest user edits into cfg before re-rendering.
        if self._cap_fields:
            self._read_config_from_fields()
        # Reset progress view so a re-run gets a fresh log + checklist.
        self._pipeline_done = False
        self._render_progress_view()
        self._state = "running"
        self._pipeline_task = self._page.run_task(self._run_pipeline)
        self._update_action_bar()

    def _rerun_clicked(self, e) -> None:
        """Re-run pipeline from the events view — back to progress view."""
        self._start_clicked(e)

    def _stop_clicked(self, e) -> None:
        task = self._pipeline_task
        if task is not None and not task.done():
            task.cancel()
        # If currently paused, also wake the waiter so cancellation propagates.
        if self._pause_event is not None and not self._pause_event.is_set():
            self._pause_event.set()
        self._state = "idle"
        self._set_stage_label("Stopped")
        self._mark_all_stages_idle()
        self._update_action_bar()
        self._refresh_page()

    def _pause_clicked(self, e) -> None:
        if self._state != "running":
            return
        if self._pause_event is None:
            return
        self._pause_event.clear()
        self._state = "paused"
        # Stage label flips to "Paused" the next time the runner hits a
        # checkpoint, but we update the buttons immediately.
        self._update_action_bar()
        self._refresh_page()

    def _resume_clicked(self, e) -> None:
        if self._state != "paused":
            return
        if self._pause_event is None:
            return
        self._pause_event.set()
        self._state = "running"
        self._update_action_bar()
        self._refresh_page()

    def _mark_all_stages_idle(self) -> None:
        for sid, _ in _PIPELINE_STAGES:
            self._mark_stage(sid, "pending")

    def _update_action_bar(self) -> None:
        if self._action_bar is None:
            return
        running = self._state == "running"
        paused = self._state == "paused"
        idle_or_done = self._state in ("idle", "done")

        # Buttons live in self._action_bar.controls in this order:
        # [Start, Pause, Resume, Stop]
        start_btn, pause_btn, resume_btn, stop_btn = self._action_bar.controls
        start_btn.disabled = not idle_or_done
        pause_btn.disabled = not running
        resume_btn.disabled = not paused
        stop_btn.disabled = not (running or paused)
        # Config controls are editable only when not actively running.
        frozen = running or paused
        for f in self._cap_fields.values():
            f.disabled = frozen
        for cb in self._source_checkboxes.values():
            cb.disabled = frozen
        for cb in self._behavior_checkboxes.values():
            cb.disabled = frozen
        if self._days_field is not None:
            self._days_field.disabled = frozen
        self._refresh_page()

    # ── Entry point ─────────────────────────────────────────────────────

    def main(self, page: ft.Page):
        self._page = page
        page.title = "Event Harvester"
        page.theme = ft.Theme(color_scheme_seed=_SEED_COLOR)
        page.dark_theme = ft.Theme(color_scheme_seed=_SEED_COLOR)
        page.theme_mode = ft.ThemeMode.DARK
        page.padding = 20

        if self.pipeline_runner is not None and not self._pipeline_done:
            self._render_progress_view()
            # Auto-start on first load. The user can Stop/Pause/Restart from
            # the action bar after that.
            self._state = "running"
            self._pipeline_task = page.run_task(self._run_pipeline)
            self._update_action_bar()
        else:
            self._render_events_view()

    async def _run_pipeline(self) -> None:
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # not paused
        reporter = ProgressReporter(self)
        handler = _UILogHandler(reporter)
        handler.setLevel(logging.INFO)
        root = logging.getLogger("event_harvester")
        root.addHandler(handler)
        try:
            reporter.update_stage("harvest")
            reporter.log("Pipeline starting…")
            await self.pipeline_runner(reporter, self)
            if not self._pipeline_done:
                reporter.update_stage("done", "Pipeline finished — no events to review")
                reporter.log("No events were extracted.")
            if self._state != "idle":
                self._state = "done"
        except asyncio.CancelledError:
            reporter.log("Pipeline cancelled by user.", color=ft.Colors.OUTLINE)
            self._state = "idle"
            raise
        except Exception as ex:
            logger.exception("Pipeline failed")
            reporter.error(f"{type(ex).__name__}: {ex}")
            self._state = "idle"
        finally:
            root.removeHandler(handler)
            self._update_action_bar()
