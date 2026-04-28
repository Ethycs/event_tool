"""Flet events browser — interactive UI for reviewing and acting on extracted events."""

import logging

import flet as ft

from event_harvester.event_match import find_fingerprint, save_fingerprint

logger = logging.getLogger("event_harvester.app")

_SEED_COLOR = "#6750A4"


def _status_for(event: dict) -> str:
    """Determine display status for an event."""
    if event.get("_status"):
        return event["_status"]
    return "known" if find_fingerprint(event) else "new"


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


class EventsApp:
    """Flet events browser."""

    def __init__(self, events, rejects, stats, cfg):
        self.events = events
        self.rejects = rejects or {}
        self.stats = stats or {}
        self.cfg = cfg
        self._tt = None
        self._page = None
        self._cards_column = None
        self._status_filter = None
        self._source_filter = None
        self._search_field = None
        self._header_text = None

        # Compute initial statuses
        for ev in self.events:
            if "_status" not in ev:
                ev["_status"] = _status_for(ev)

    def _get_tt(self):
        if self._tt is None:
            from event_harvester.ticktick import get_ticktick_client
            self._tt = get_ticktick_client(self.cfg.ticktick)
        return self._tt

    def _on_approve(self, event_dict):
        def handler(e):
            try:
                tt = self._get_tt()
                if tt:
                    from event_harvester.ticktick import create_ticktick_tasks
                    create_ticktick_tasks(
                        tt, [event_dict],
                        project_name=self.cfg.ticktick.project,
                    )
                save_fingerprint(event_dict)
                event_dict["_status"] = "approved"
            except Exception as ex:
                logger.error("Approve failed: %s", ex)
                event_dict["_status"] = "approved"
                save_fingerprint(event_dict)
            self._rebuild_cards()
        return handler

    def _on_decline(self, event_dict):
        def handler(e):
            save_fingerprint(event_dict)
            event_dict["_status"] = "declined"
            self._rebuild_cards()
        return handler

    def _on_approve_all(self, e):
        tt = self._get_tt()
        for ev in self.events:
            if ev.get("_status") == "new":
                try:
                    if tt:
                        from event_harvester.ticktick import create_ticktick_tasks
                        create_ticktick_tasks(
                            tt, [ev], project_name=self.cfg.ticktick.project,
                        )
                except Exception as ex:
                    logger.error("Approve failed for %s: %s", ev.get("title"), ex)
                save_fingerprint(ev)
                ev["_status"] = "approved"
        self._rebuild_cards()

    def _on_decline_all(self, e):
        for ev in self.events:
            if ev.get("_status") == "new":
                save_fingerprint(ev)
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

        # Action row
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

        self._page.update()

    def _build_reject_panels(self) -> ft.ExpansionPanelList | None:
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
                # Event-level rejects
                rows = []
                for ev in items[:50]:
                    rows.append(ft.Text(
                        f"{ev.get('title', '?')} — {ev.get('date', '?')} ({ev.get('reason', '?')})",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT,
                    ))
            else:
                # Message-level rejects
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
                content=ft.Column(rows, spacing=2),
            ))

        if not panels:
            return None
        return ft.ExpansionPanelList(panels, elevation=1)

    def main(self, page: ft.Page):
        self._page = page
        page.title = "Event Harvester"
        page.theme = ft.Theme(color_scheme_seed=_SEED_COLOR)
        page.dark_theme = ft.Theme(color_scheme_seed=_SEED_COLOR)
        page.theme_mode = ft.ThemeMode.DARK
        page.padding = 20

        # Header
        n_new = sum(1 for ev in self.events if ev.get("_status") == "new")
        n_known = sum(1 for ev in self.events if ev.get("_status") == "known")
        self._header_text = ft.Text(
            f"{len(self.events)} events · {n_new} new · {n_known} known",
            size=14,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )

        def toggle_theme(e):
            page.theme_mode = (
                ft.ThemeMode.LIGHT
                if page.theme_mode == ft.ThemeMode.DARK
                else ft.ThemeMode.DARK
            )
            page.update()

        # Filters
        self._status_filter = ft.Dropdown(
            label="Status",
            value="All",
            options=[ft.dropdown.Option(s) for s in [
                "All", "New", "Known", "Approved", "Declined",
            ]],
            width=140,
            on_change=self._on_filter_change,
        )

        sources = sorted({_source_platform(ev) for ev in self.events})
        self._source_filter = ft.Dropdown(
            label="Source",
            value="All",
            options=[ft.dropdown.Option("All")] + [
                ft.dropdown.Option(s) for s in sources
            ],
            width=140,
            on_change=self._on_filter_change,
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

        # Event cards
        self._cards_column = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO)
        for ev in self._filtered_events():
            self._cards_column.controls.append(self._build_event_card(ev))

        # Pipeline stats
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

        # App bar
        page.appbar = ft.AppBar(
            title=ft.Text("Event Harvester"),
            center_title=False,
            actions=[
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
                    on_click=toggle_theme,
                ),
            ],
        )

        # Layout
        page.add(
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
