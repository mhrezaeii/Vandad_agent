# Scene-video health and model capability catalogue

The API endpoint `GET /api/v1/providers/scene-video/models` returns the
configured model capability contract for each registered scene-video provider.
It is a runtime catalogue, not a claim that every provider model or account
entitlement has been discovered dynamically.

Each entry exposes:

- `input_modalities` and `supported_modalities` (inputs vs generated output)
- supported languages and aspect ratios, when known
- supported resolutions, when known
- seed support
- maximum prompt length and reference-image count, when known
- minimum and maximum duration
- composition, quality-review, cancellation, and health-probe support

A `null` capability limit means the adapter does not have a verified value; it
does not mean unlimited support. For the generic HTTP adapter, declare
`supported_resolutions` as dimension strings such as `1080x1920`,
`supports_seed` explicitly, and optionally set `max_prompt_characters`.
Seed and dimension requests outside that declared contract fail before the
provider request is sent.

## Ordered scene-video provider selection

For a scene project, the requested provider is tried first, followed by the
explicit provider_fallbacks list. Before dispatch, the registry checks each
candidate in order for the project's required capabilities and a live health
probe. The selected provider and remaining route order are persisted with the
project's scene options and budget metadata.

A project is pinned to the selected provider once generation jobs are accepted.
The service does not silently mix vendors midway through a storyboard or
composition; that would mix job IDs, polling semantics, media retrieval and
billing contracts. The LLM execution path has its own safe request-level
fallback rules documented in PROVIDER_ROUTING.md.

## APIMaster

The APIMaster adapter declares the limits implemented by this adapter:

- Input: text and one reference image
- Output: video
- Resolutions: 480P, 720P, 1080P
- Seed: supported
- Prompt: up to 5,000 characters
- Duration: 2–30 seconds
- Aspect ratios: route-dependent; the native route additionally supports 21:9

Its live health probe performs an authenticated, read-only
`GET /v1/models` request with a short timeout. This checks that the API
credential and model-catalogue endpoint are reachable without creating a paid
video job. It does **not** guarantee remaining quota or video-generation
entitlement for the selected model; the first actual generation remains subject
to the provider's own authorization and quota checks.

The adapter currently sends at most one reference image even if the upstream
service supports more, so the catalogue reports `max_reference_images: 1`.


## WaveSpeed LTX-2.3

The `wavespeed` adapter is opt-in and implements text-to-video scene
generation only. It supports 480p/720p/1080p, 16:9/9:16, optional seed, and
5–20 second clips (integer duration). It performs a read-only authenticated
`GET /api/v3/balance` health probe.

WaveSpeed's documented model contract does not provide final storyboard
composition or AI quality review. The adapter can delegate those two stages
to an explicitly configured `generic_http` provider only when both
`SCENE_VIDEO_HTTP_COMPOSE_PATH` and `SCENE_VIDEO_HTTP_QUALITY_PATH` are
present. Otherwise, generation remains available while finalization fails
closed. See [WAVESPEED_PROVIDER.md](WAVESPEED_PROVIDER.md).

The adapter stores prediction IDs, provider status, reported USD cost when
available, and a separately labelled published-price estimate. Missing actual
cost never becomes zero: the existing project budget reservation remains
unknown until reconciled.
