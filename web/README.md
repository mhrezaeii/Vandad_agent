# Vandad Agent Web

Persian-first (RTL) Next.js single-page control plane for the Vandad Agent API.

## Local development

```powershell
cd web
Copy-Item .env.example .env.local
npm install
npm run dev
```

Open http://localhost:3000. The API defaults to `http://localhost:8000/api/v1`.

## Accounts and approval

- Registration and login share one panel; Persian is the default language and
  English can be selected at any time.
- New accounts are created with `pending_approval` status. The backend rejects
  every tenant endpoint for them until an administrator approves the account;
  the UI only reflects that server decision and re-checks it periodically.
- Administrators see an **Account approvals** modal in the dashboard and can
  approve or reject pending accounts.
- Set `AUTH_BOOTSTRAP_FIRST_ADMIN=true` on a fresh single-tenant install so the
  first registered account becomes an approved administrator. Leave it `false`
  when an identity proxy supplies principals.

## Production flow

The dashboard is one page:

1. Select or create a brand profile (Vandefence, NobatVakil, NobatMoshaver,
   Vandiar, …).
2. Choose the duration, character reference and provider; the scenario is generated from platform knowledge.
3. Press **شروع فرآیند**. The UI creates a real scene project, starts the real
   scenario workflow, approves the generated scenario and starts the real video
   workflow — no mock progress or simulated completion.
4. The live timeline shows the actual workflow execution id, project id, agent
   steps, attempts, durations and errors. State survives a page reload and can
   be cancelled where the backend allows it.
5. On completion the player, output metadata, quality-review details and an
   authorized download are shown. Storage paths, credentials and provider
   secrets are never rendered.

## Implemented screens

- Splash, combined sign-in/registration, pending-approval and rejected states
- Single-page dashboard: profiles, production form, live execution timeline,
  scenario review, video result, account approvals

The UI consumes only public API contracts. It does not import backend models or
connect directly to PostgreSQL.

## Security boundary

Authorization is enforced by the backend. The browser keeps only a signed
bearer token; the server resolves the company from the account, re-reads the
approval state on every request and rejects cross-tenant access. The frontend
is not a security boundary.
