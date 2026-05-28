"""Shim so litellm proxy's config can find ``uc_handler``.

The proxy resolves ``custom_handler`` dotted paths by treating them as
file paths relative to the config file's directory, not as regular
Python imports. Our real module is at
``src/event_harvester/llm_providers/uc.py`` which the proxy's loader
can't reach that way, so we re-export the handler from this file
(which sits next to ``litellm.config.yaml``).

Referenced from ``litellm.config.yaml`` as
``custom_handler: uc_provider_loader.uc_handler``.
"""

from event_harvester.llm_providers.uc import uc_handler  # noqa: F401
