"""Provider adapter for authorised Playwright-backed platform sessions."""
from __future__ import annotations
from app.integrations.providers.adapter import ProviderAdapter, ProviderCredentialRequirement
from app.integrations.providers.base import ProviderCapability, ProviderExecutionContext, ProviderPermanentError, ProviderResult

class BrowserPlaywrightProvider(ProviderAdapter):
    provider_id = "browser_playwright"
    display_name = "Playwright Browser"
    description = "Validates configuration for authorised browser-based platform discovery."
    capabilities = frozenset({ProviderCapability.CONNECTION_VALIDATION, ProviderCapability.HEALTH_CHECK})
    credential_requirements = (ProviderCredentialRequirement("basic_auth", required=False),)

    async def validate_connection(self, context: ProviderExecutionContext) -> ProviderResult:
        browser = context.configuration.get("browser", {})
        if not isinstance(browser, dict):
            raise ProviderPermanentError("Browser configuration must be an object.", code="invalid_configuration")
        login = browser.get("login", {})
        if login and not all(login.get(key) for key in ("username_selector", "password_selector", "submit_selector")):
            raise ProviderPermanentError("Browser login requires username, password and submit selectors.",
                code="invalid_configuration")
        return ProviderResult(provider_id=self.provider_id, success=True,
            message="Browser discovery configuration verified; live navigation occurs during discovery.")
