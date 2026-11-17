# Vandad Agent Web

Persian-first, RTL Next.js control plane for the Vandad Agent API.

## Local development

```powershell
cd web
Copy-Item .env.example .env.local
npm install
npm run dev
```

Open http://localhost:3000. The API defaults to `http://localhost:8000/api/v1`.

Set `NEXT_PUBLIC_COMPANY_ID` when you want to use a specific company. It can also be configured in Settings inside the UI. When omitted, the backend's default company scope is used.

## Implemented screens

- Overview: task and runtime summary
- Agent Center: code-owned catalog and company enablement
- Task Center: create, approve, cancel, retry and inspect durable progress events
- Company Brain: platform and brand context
- Content Studio: brand profiles and approval workflow guidance
- Provider Center: provider registry, usage ledger and company budget policies
- Settings: API company scope

The UI intentionally consumes only public API contracts. It does not import backend models or connect directly to PostgreSQL.

## Security boundary

The current backend exposes a bootstrap principal and company-scope boundary. This frontend is an internal operator console, not a substitute for user authentication, membership, role-based authorization or CSRF controls. Do not expose it publicly until the backend identity and authorization phase is completed.
