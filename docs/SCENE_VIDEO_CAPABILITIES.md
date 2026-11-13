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
