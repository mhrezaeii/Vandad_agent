# Staging readiness checklist

The API-side identity-proxy contract is implemented and tested, but a real
staging deployment cannot be completed from the repository alone. It requires
an operator-controlled OIDC proxy, a reachable staging host/domain, a trusted
proxy-to-API network, and deployment secrets.

## Required environment

Set these in the API environment before starting with ENVIRONMENT=staging:

- AUTH_PROXY_SHARED_SECRET: at least 32 characters; generate with
  openssl rand -hex 32.
- PLATFORM_BOOTSTRAP_COMPANY_IDS: explicit comma-separated company UUIDs;
  empty values and * are rejected in staging/production.
- CORS_ALLOWED_ORIGINS: exact trusted browser origin(s), never *.
- DATABASE_URL and REDIS_URL: staging-only services and credentials.
- CREDENTIAL_MASTER_KEYS and CREDENTIAL_ACTIVE_KEY_VERSION: staging-only
  encryption keys; never reuse development or production keys.
- ARTIFACT_STORAGE_ROOT: persistent writable storage.
- Provider credentials and company budgets only for providers approved for the
  staging test.

The same .env file is loaded by API and worker containers. Do not commit it or
put AUTH_PROXY_SHARED_SECRET in any frontend build argument.

## OIDC proxy contract

The external proxy must:

1. Authenticate users with OIDC and enforce MFA/session policy.
2. Strip browser-supplied X-Auth-* headers before injecting trusted claims.
3. Inject X-Auth-Proxy-Secret server-side.
4. Inject the stable subject, active-company membership UUIDs, and roles using
   the configured X-Auth-Request-* headers.
5. Send only the active company's roles, not the union of roles across all
   memberships.
6. Forward API requests to the API container over a private trusted network.
7. Prevent direct public access to the API container/port.

The proxy must not expose its shared secret to the browser. Serve the UI and API
on one site where possible; otherwise configure exact CORS origins and
credential support.

## Verification sequence

1. Start the API with ENVIRONMENT=staging and no proxy secret: startup must
   fail.
2. Start with AUTH_PROXY_SHARED_SECRET missing, an empty bootstrap company
   list, or wildcard *: startup must fail.
3. Send a request without the proxy secret: expect 401.
4. Send a wrong proxy secret: expect 401.
5. Send a valid proxy identity without a company membership: expect 401.
6. Select a company not present in the trusted membership claim: expect 403.
7. Verify viewer can read but cannot mutate; operator cannot change provider
   budgets; admin/owner can use the permitted control-plane endpoints.
8. Verify API, worker and agent-worker share the same staging database,
   Redis, credential keyring and artifact volume.
9. Run provider health probes first. Do not run a paid generation smoke test
   until the operator explicitly approves the provider, model, spend ceiling,
   and test payload.

## Current external blockers

A real staging sign-off still needs the deployment URL, OIDC issuer/client
configuration, proxy deployment/network, staging company UUIDs, and generated
secrets. These are intentionally not fabricated or committed in this repository.
