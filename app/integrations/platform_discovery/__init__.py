"""Platform discovery adapters."""
from app.integrations.platform_discovery.public_sources import PublicSourceCrawler
from app.integrations.platform_discovery.types import DiscoveredPage, DiscoveryResult
__all__ = ["DiscoveredPage", "DiscoveryResult", "PublicSourceCrawler"]
