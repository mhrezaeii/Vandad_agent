from app.integrations.providers import build_default_registry
from app.integrations.providers.browser_playwright import BrowserPlaywrightProvider
from app.integrations.providers.base import ProviderExecutionContext
import uuid
import pytest

def test_browser_playwright_provider_is_registered():
    adapter = build_default_registry().get("browser_playwright")
    assert isinstance(adapter, BrowserPlaywrightProvider)

async def test_browser_provider_validates_selector_contract():
    adapter = BrowserPlaywrightProvider()
    context = ProviderExecutionContext(provider_id="browser_playwright", connection_id=uuid.uuid4(),
        company_id=uuid.uuid4(), platform_id=uuid.uuid4(),
        configuration={"browser":{"login":{"username_selector":"#user","password_selector":"#pass","submit_selector":"button"}}},
        credential=None, credential_type=None, secret_reference=None, timeout_seconds=3)
    assert (await adapter.validate_connection(context)).success

async def test_browser_provider_rejects_partial_login_selectors():
    adapter = BrowserPlaywrightProvider()
    context = ProviderExecutionContext(provider_id="browser_playwright", connection_id=uuid.uuid4(),
        company_id=uuid.uuid4(), platform_id=uuid.uuid4(),
        configuration={"browser":{"login":{"username_selector":"#user"}}},
        credential=None, credential_type=None, secret_reference=None, timeout_seconds=3)
    from app.integrations.providers.base import ProviderPermanentError
    with pytest.raises(ProviderPermanentError):
        await adapter.validate_connection(context)
