# Production identity proxy contract

Vandad Agent does not issue local passwords or mint its own browser sessions. In production, place the UI and API behind a trusted OIDC-capable identity proxy (for example, an OIDC reverse proxy). The proxy owns login, MFA, session cookies and identity-provider integration; the API verifies the proxy-to-backend trust boundary and authorizes company scope and roles.

## Required API environment

Set these values in the API and agent-worker environment:

- \`ENVIRONMENT=production\`
- \`AUTH_PROXY_SHARED_SECRET\`: high-entropy shared secret, unique to this deployment
- \`AUTH_PROXY_USER_HEADER=X-Auth-Request-User\`
- \`AUTH_PROXY_COMPANY_IDS_HEADER=X-Auth-Request-Company-IDs\`
- \`AUTH_PROXY_ROLES_HEADER=X-Auth-Request-Roles\`
- \`PLATFORM_BOOTSTRAP_COMPANY_IDS\`: explicit UUID list; wildcard is forbidden in production

The API expects these headers on every authenticated business request:

| Header | Required value |
|---|---|
| \`X-Auth-Proxy-Secret\` | Exact shared secret configured in \`AUTH_PROXY_SHARED_SECRET\` |
| \`X-Auth-Request-User\` | Stable authenticated subject, maximum 255 characters |
| \`X-Auth-Request-Company-IDs\` | Comma-separated UUID memberships |
| \`X-Auth-Request-Roles\` | Comma-separated roles: \`owner\`, \`admin\`, \`operator\`, \`viewer\` |

If a user belongs to multiple companies, the browser must send \`X-Company-ID\` to select the active company.

## Reverse-proxy requirements

1. Authenticate the browser with OIDC before serving the UI or forwarding API requests.
2. Strip every inbound \`X-Auth-*\` header from the browser request.
3. Inject the four trusted headers above only after successful authentication.
4. Inject \`X-Auth-Proxy-Secret\` server-side. Never expose the secret to browser JavaScript, HTML, source maps or frontend environment variables.
5. Restrict direct network access to the API so callers cannot bypass the proxy.
6. Use HTTPS between browser and proxy; prefer private networking or TLS between proxy and API. Set the proxy session cookie to `HttpOnly`, `Secure` and `SameSite=Lax` (or stricter where deployment topology allows).
7. Do not pass user-controlled company IDs or roles through unchanged. Resolve them from the identity provider or a trusted membership service.

The frontend sends same-site session cookies with API and SSE requests. Keep the UI and API on the same site where possible. If cross-origin deployment is required, configure exact CORS origins and credential support; never use wildcard origins with credentials.

## Baseline role policy

- \`viewer\`: read-only API access.
- \`operator\`: task execution, cancellation, content operations and other operator actions.
- \`admin\`: operator permissions plus agent activation and provider budget/reconciliation controls.
- \`owner\`: same baseline control-plane permissions as admin.

The identity proxy is responsible for mapping identity-provider groups to these roles and company memberships. For multi-company users, `X-Auth-Request-Roles` must contain the roles for the active company selected by `X-Company-ID`; do not send the union of a user's roles across all companies. The API does not trust role claims supplied directly by the browser.

## Local development

Outside production, the explicit bootstrap principal remains available for local development and tests. Do not copy the local wildcard configuration into production.
