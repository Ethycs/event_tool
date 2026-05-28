"""MCP tool registrations for Grok control.

Exposes 11 tools that delegate to a process-singleton GrokClient.
The client is created lazily on first call and reused thereafter; an
``atexit`` hook closes the underlying browser on shutdown.

Tools:
    Core:      grok_send_message, grok_read_conversation,
               grok_list_conversations, grok_delete_conversation
    Lifecycle: grok_new_chat, grok_rename_conversation,
               grok_archive_conversation, grok_regenerate,
               grok_stop_generation
    Models:    grok_get_models, grok_switch_model
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from event_harvester.sites.grok import GrokAuthRequired, get_grok_client

logger = logging.getLogger("event_harvester.servers.mcp_grok_tools")


def _wrap_auth(fn):
    """Decorator that converts GrokAuthRequired into a plain dict result.

    FastMCP serializes return values; raising would be ugly in the
    client. We instead return ``{"ok": False, "error": "...", ...}``.
    """

    def _inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GrokAuthRequired as exc:
            return {"ok": False, "error": "auth_required", "message": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Grok tool failed: %s", fn.__name__)
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}

    _inner.__name__ = fn.__name__
    _inner.__doc__ = fn.__doc__
    return _inner


def register_grok_tools(server) -> None:
    """Attach all Grok MCP tools to the given FastMCP ``server``."""

    @server.tool()
    @_wrap_auth
    def grok_send_message(
        message: str,
        conversation_url: Optional[str] = None,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        """Send a message to Grok and return the assistant's response.

        If ``conversation_url`` is None, posts to https://grok.com/ which
        creates a new conversation. Returns ``{ok, response, url,
        conversation_id}``.
        """
        result = get_grok_client().send(
            message,
            conversation_url=conversation_url,
            timeout_s=timeout_s,
        )
        return {"ok": True, **result}

    @server.tool()
    @_wrap_auth
    def grok_read_conversation(conversation_url: str) -> dict[str, Any]:
        """Return the full transcript of a Grok conversation.

        Result: ``{ok, url, conversation_id, messages: [{role, text}, ...]}``.
        """
        result = get_grok_client().read(conversation_url)
        return {"ok": True, **result}

    @server.tool()
    @_wrap_auth
    def grok_list_conversations() -> dict[str, Any]:
        """List all conversations visible in the Grok sidebar.

        Result: ``{ok, conversations: [{id, title, url}, ...]}``.
        """
        items = get_grok_client().list_conversations()
        return {"ok": True, "conversations": items, "count": len(items)}

    @server.tool()
    @_wrap_auth
    def grok_delete_conversation(conversation_url: str) -> dict[str, Any]:
        """Delete a Grok conversation by URL or id.

        Result: ``{ok, deleted}`` where ``deleted`` reflects whether the
        sidebar row disappeared after the confirm-modal click.
        """
        ok = get_grok_client().delete(conversation_url)
        return {"ok": True, "deleted": ok}

    @server.tool()
    @_wrap_auth
    def grok_new_chat() -> dict[str, Any]:
        """Return the URL of the Grok new-chat surface.

        Grok creates a real conversation id only after the first message
        is sent. To materialize a conversation, follow this with
        ``grok_send_message`` (no ``conversation_url`` arg).
        """
        result = get_grok_client().new_chat()
        return {"ok": True, **result}

    @server.tool()
    @_wrap_auth
    def grok_rename_conversation(
        conversation_url: str,
        new_title: str,
    ) -> dict[str, Any]:
        """Rename a conversation. Result: ``{ok, renamed}``."""
        renamed = get_grok_client().rename(conversation_url, new_title)
        return {"ok": True, "renamed": renamed}

    @server.tool()
    @_wrap_auth
    def grok_archive_conversation(conversation_url: str) -> dict[str, Any]:
        """Archive a conversation. Result: ``{ok, archived}``.

        Note: Grok may not expose an explicit archive — recipe falls back
        to whatever "Archive" menu item is present. Returns ``False`` if
        no such item exists.
        """
        archived = get_grok_client().archive(conversation_url)
        return {"ok": True, "archived": archived}

    @server.tool()
    @_wrap_auth
    def grok_regenerate(
        conversation_url: str,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        """Regenerate the last assistant response. Returns ``{ok, response}``."""
        return get_grok_client().regenerate(conversation_url, timeout_s=timeout_s)

    @server.tool()
    @_wrap_auth
    def grok_stop_generation(conversation_url: str) -> dict[str, Any]:
        """Stop the assistant if it's currently generating. Returns ``{ok, stopped}``."""
        stopped = get_grok_client().stop(conversation_url)
        return {"ok": True, "stopped": stopped}

    @server.tool()
    @_wrap_auth
    def grok_get_models() -> dict[str, Any]:
        """Open the model picker and return available model names.

        Result: ``{ok, models: [...]}``.
        """
        models = get_grok_client().get_models()
        return {"ok": True, "models": models}

    @server.tool()
    @_wrap_auth
    def grok_switch_model(model_name: str) -> dict[str, Any]:
        """Open the model picker and select ``model_name``. Returns ``{ok, switched}``."""
        switched = get_grok_client().switch_model(model_name)
        return {"ok": True, "switched": switched}
