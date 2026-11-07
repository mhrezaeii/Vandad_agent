"""External service integrations (n8n, AI providers, platform providers).

Phase 2 introduces the provider adapter architecture under
``app.integrations.providers``. The n8n orchestration client arrives in a
later phase and belongs here as well.
"""

from app.integrations.providers import build_default_registry

__all__ = ["build_default_registry"]
