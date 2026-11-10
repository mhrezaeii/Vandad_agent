# Platform Discovery

The platform core now has two source-discovery modes.

## Public sources (default)
The crawler reads the configured homepage, robots.txt, sitemap.xml (including
Sitemap declarations), and bounded same-origin HTML pages. It prioritises
documentation, guides, help, product/features and changelog links. It respects
robots.txt, limits pages/response size/redirect count, rejects private network
destinations and does not bypass login, paywalls or access controls.

## Authenticated browser mode (optional)
Install:
```bash
pip install -e ".[browser]"
playwright install chromium
```
Create a platform connection with provider `browser_playwright` and non-secret
configuration:
```json
{"browser":{"login":{"username_selector":"input[name='username']","password_selector":"input[name='password']","submit_selector":"button[type='submit']","success_url_contains":"/dashboard"}}}
```
Attach an encrypted `basic_auth` credential whose value is a JSON object with
`username` and `password`. The existing credential service resolves it only
at scan time. Use only accounts authorised for automated access.

## API
- `POST /api/v1/platforms/{platform_id}/discover`
  - public: `{"mode":"public","max_pages":25}`
  - browser: `{"mode":"browser","max_pages":15,"connection_id":"..."}`
- `GET /api/v1/platforms/{platform_id}/sources?limit=50&offset=0`

Discovery results are persisted per company and platform. Repeated scans
classify pages as created, changed or unchanged. Scans are synchronous and
bounded; scheduling/background orchestration is a later step.
