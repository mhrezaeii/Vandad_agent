# WaveSpeed LTX-2.3 scene-video provider

The `wavespeed` provider implements WaveSpeed's documented asynchronous
`POST /api/v3/{model_id}` and `GET /api/v3/predictions/{id}/result` contract
for `wavespeed-ai/ltx-2.3/text-to-video`.

## Configuration

The provider is opt-in:

```dotenv
SCENE_VIDEO_PROVIDER=wavespeed
SCENE_VIDEO_WAVESPEED_API_KEY=...
SCENE_VIDEO_WAVESPEED_MODEL=wavespeed-ai/ltx-2.3/text-to-video
SCENE_VIDEO_WAVESPEED_RESOLUTION=480p
SCENE_VIDEO_WAVESPEED_ASPECT_RATIO=9:16
SCENE_VIDEO_WAVESPEED_TIMEOUT_SECONDS=60
SCENE_VIDEO_WAVESPEED_DOWNLOAD_ALLOWED_HOSTS=exact-cdn-host.example
SCENE_VIDEO_POLL_INTERVAL_SECONDS=2
SCENE_VIDEO_MAX_POLL_ATTEMPTS=600
```

Use exact HTTPS hostnames observed in the returned output URL for
`SCENE_VIDEO_WAVESPEED_DOWNLOAD_ALLOWED_HOSTS`. The adapter rejects redirects,
userinfo, non-HTTPS URLs, unlisted hosts, and non-public resolved IP addresses.
It does not forward the API key to the media host.

Supported LTX-2.3 settings are 480p/720p/1080p, 16:9/9:16, and 5/10/15/20
seconds. Seed is optional. Reference images are rejected because this is the
text-to-video model, not the image-to-video endpoint.

## Submission safety

WaveSpeed does not document an idempotency-key contract for this endpoint.
The adapter therefore does not claim upstream idempotency and never retries a
submit POST after a timeout, dropped connection, server error, or malformed
accepted response. The scene service locks persisted scene rows while deciding
whether to submit. An ambiguous submit is marked failed/unknown, its automatic
attempt budget is exhausted, and the operator must reconcile WaveSpeed History
before any manual retry. A documented HTTP 429 is treated as an explicit
rejection and remains retryable.

Prediction IDs are stored as the provider job ID (with a `wavespeed:` prefix)
and in submission metadata. Provider state, output URL, model, timing, reported
cost (when present), and the published estimate are tracked on the scene job.

## Cost and budget accounting

Published LTX-2.3 estimates are $0.10/$0.15/$0.20 per 5 seconds for
480p/720p/1080p respectively. The adapter records an estimate using the scene's
persisted duration. It records actual cost only when the prediction response
explicitly reports a USD cost field. Estimates are not misrepresented as actual
charges. If WaveSpeed omits actual per-prediction cost, the platform's existing
budget reservation remains unknown/reserved until reconciled against provider
billing. This is intentional fail-closed accounting.

## Composition and quality review

WaveSpeed's documented LTX-2.3 text-to-video contract generates clips; its
model page instructs users to edit multiple clips together and does not define
a final-video composition or AI quality-review operation. The adapter does not
invent either endpoint.

To use the existing finalization pipeline, configure an authorised secondary
HTTP provider with explicit composition and quality endpoints:

```dotenv
SCENE_VIDEO_HTTP_BASE_URL=https://your-authorised-video-service.example
SCENE_VIDEO_HTTP_API_KEY=...
SCENE_VIDEO_HTTP_COMPOSE_PATH=/v1/compose
SCENE_VIDEO_HTTP_QUALITY_PATH=/v1/quality
SCENE_VIDEO_HTTP_STATUS_PATH=/v1/videos/{job_id}
SCENE_VIDEO_HTTP_DOWNLOAD_ALLOWED_HOSTS=exact-media-host.example
```

When both paths are configured, the WaveSpeed provider delegates final
composition and quality review to this provider while retaining WaveSpeed
for scene generation. If either capability is absent, scene generation is
available but finalization fails closed. The secondary service's request and
response shape must match the existing generic HTTP adapter contract.

## Testing

All adapter contract tests use `httpx.MockTransport`; they do not contact
WaveSpeed or spend credits. The paid smoke test is separate and opt-in.
