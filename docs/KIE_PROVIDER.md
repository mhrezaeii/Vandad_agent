# KIE.ai Grok Imagine Video 1.5 Preview

## Configuration

KIE is an opt-in scene-video provider. Keep the API key in the local `.env`
file only; never commit it or send it to the browser.

```dotenv
SCENE_VIDEO_PROVIDER=kie
SCENE_VIDEO_KIE_API_KEY=...
SCENE_VIDEO_KIE_MODEL=grok-imagine-video-1-5-preview
SCENE_VIDEO_KIE_RESOLUTION=480p
SCENE_VIDEO_KIE_ASPECT_RATIO=9:16
SCENE_VIDEO_KIE_TIMEOUT_SECONDS=60
SCENE_VIDEO_KIE_PRICE_PER_CREDIT_USD=0.005
SCENE_VIDEO_KIE_DOWNLOAD_ALLOWED_HOSTS=
SCENE_VIDEO_POLL_INTERVAL_SECONDS=3
SCENE_VIDEO_MAX_POLL_ATTEMPTS=300
```

The provider calls the documented `POST /api/v1/jobs/createTask`,
`GET /api/v1/jobs/recordInfo?taskId=...`, and `GET /api/v1/chat/credit`
endpoints. It uses the model ID `grok-imagine-video-1-5-preview`.
The model returns an MP4 with native audio; audio cannot be disabled.
Durations are integer seconds from 1 through 15. KIE documents 2.4, 4.5,
and 8 credits per second for 480p, 720p, and 1080p respectively.

## Spend protection

Before any paid submit, the adapter checks the account credit balance and
the scene's required credits. The service checks the sum of all pending scene
credits before starting the first billable request, preventing a partial
storyboard when the current balance cannot cover all remaining scenes.

KIE does not expose a documented idempotency key for this createTask route.
The service therefore commits a durable `submitting` fence before the POST.
A timeout, dropped connection, server error, or malformed accepted response
is marked `provider_submission_unknown`; it is never automatically retried.
The operator must inspect KIE task history before a manual retry. Explicit
rate-limit responses and preflight failures are safe non-submission outcomes.

KIE's task details response reports `creditsConsumed`. The adapter records
that figure and converts it to an estimated USD ledger amount using
`SCENE_VIDEO_KIE_PRICE_PER_CREDIT_USD` (default 0.005). This conversion is
an accounting assumption; the effective USD value of credits depends on the
account's actual top-up purchase. The credit count is the authoritative
provider-reported usage.

## Media retrieval and assembly

Generated result URLs are downloaded immediately and stored in the platform's
artifact storage. Downloads require HTTPS, reject redirects, and reject
private/non-public destination IPs. If the provider's media hostname is known,
set an exact hostname in `SCENE_VIDEO_KIE_DOWNLOAD_ALLOWED_HOSTS`.

One-scene projects use the provider MP4 as the final deliverable. Multi-scene
projects are concatenated locally with FFmpeg. Native audio is preserved and
the local TTS mux is skipped for KIE so the generated soundtrack is not
silently overwritten. FFmpeg and ffprobe are installed in the project runtime
Docker image.

## Test safety

Adapter tests use `httpx.MockTransport` and do not contact KIE or consume
credits. Real generation is an explicit operator action only.
