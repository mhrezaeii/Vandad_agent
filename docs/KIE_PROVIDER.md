# KIE.ai Grok Imagine — sole video provider and native-audio delivery

## Configuration

KIE is the only active video provider in this release, selected by product decision. WaveSpeed and other video providers were intentionally removed from the runtime scope; there is no automatic provider fallback. Keep the API key in the local `.env`
file only; never commit it or send it to the browser.

```dotenv
SCENE_VIDEO_PROVIDER=kie
SCENE_VIDEO_KIE_API_KEY=...
# Single-shot image-to-video (default): ONE video from ONE reference image.
SCENE_VIDEO_KIE_MODEL=grok-imagine/image-to-video
SCENE_VIDEO_KIE_RESOLUTION=480p
SCENE_VIDEO_KIE_ASPECT_RATIO=9:16
SCENE_VIDEO_KIE_MODE=normal
SCENE_VIDEO_KIE_SEND_DURATION=true
SCENE_VIDEO_KIE_TIMEOUT_SECONDS=60
SCENE_VIDEO_KIE_PRICE_PER_CREDIT_USD=0.005
SCENE_VIDEO_KIE_DOWNLOAD_ALLOWED_HOSTS=
# Single-shot pipeline mode + character reference assets.
SCENE_VIDEO_GENERATION_MODE=single_shot
PLATFORM_CHARACTERS_ROOT=assets/Paltform_charactar
# Polling (documented createTask/recordInfo contract).
SCENE_VIDEO_POLL_INTERVAL_SECONDS=3
SCENE_VIDEO_MAX_POLL_ATTEMPTS=300
```

Mandatory vs optional settings:

- **Mandatory for the `kie` provider:** `SCENE_VIDEO_KIE_API_KEY` and
  `SCENE_VIDEO_PROVIDER=kie`. A single-shot project additionally requires a
  character reference image (`character_key` or `character_image_url`).
- **Optional (documented defaults):** every other `SCENE_VIDEO_KIE_*` variable,
  the polling interval/attempt settings, and the upload host/path override.
  The upload host defaults to the documented file service
  `https://kieai.redpandaai.co` (`SCENE_VIDEO_KIE_UPLOAD_BASE_URL`); it is a
  different origin from the jobs API, which answers the upload path with 404.

Two models are supported by the adapter:

| Model | Input mode | Duration | Reference image | Prompt limit |
|---|---|---|---|---|
| `grok-imagine/image-to-video` (default) | image-to-video | 6..30 s, whole seconds, serialized as a string (`"20"`) | mandatory (exactly one) | 5000 chars |
| `grok-imagine-video-1-5-preview` (legacy per-scene text-to-video) | text-to-video | 1..15 s (integer or fractional number) | optional | 4096 chars |

`SCENE_VIDEO_KIE_SEND_DURATION=true` is the production default: the
image-to-video payload sends the documented `duration`/`resolution`/`mode`
fields. Two live paid probes on 2026-11-25 showed that prompt-only timing is not
honoured: a prompt asking for 30 seconds still produced a 6.04 s clip, while an
explicit `"30"` produced a 30.0 s clip. Set the flag to `false` only to
experiment with model-controlled timing; that path is accepted by the API but
does not follow the requested duration, and the adapter then validates the
deliverable only against a broad 45 s safety bound because the provider status
does not report a duration.

### Measured spoken-word rate

Two explicit 30 s probes with real character reference images produced 132 and
99 spoken words (Whisper `fa` transcription), averaging ~116 words per 30
seconds (232 wpm). The scenario writer is calibrated to this measured rate so
the script fills the clip; a short script leaves silence that the model fills
with repeated sentences or improvised scenes. Provider speech is synthesised
live and is not word-perfect, so the review gate enforces a budget near the
measured average rather than a slower manual pace. The scenario writer uses the
same budget helper as the gate and performs one counted corrective regeneration
if the first draft is over or under budget, before the workflow-level retry.

Both supported KIE models return an MP4 with provider-native audio; audio cannot be disabled. This native-audio output is the chosen audio path for the initial release: Vandad validates and preserves the returned audio and does not add a local TTS/lip-sync replacement. The image-to-video model prices at 2.4 / 4.5 / 8 credits per second for
480p / 720p / 1080p, so a 20-second 480p single-shot generation costs about
48 credits. These rates do not describe the separate Grok Imagine Video 1.5
Preview text-to-video model. Verify current
pricing on kie.ai before relying on the estimate; the adapter's preflight
checks the live credit balance against the same per-second rates, and the
authoritative usage is the task's reported `creditsConsumed` (converted to a
USD ledger amount with `SCENE_VIDEO_KIE_PRICE_PER_CREDIT_USD` — an accounting
assumption, not a billing statement).

### Budget planning at the configured credit price

With the example `SCENE_VIDEO_KIE_PRICE_PER_CREDIT_USD=0.005`, the image-to-video
credit schedule implies these planning estimates:

| Resolution | Credits/second | Estimated USD/second | 30 seconds | 60 seconds |
|---|---:|---:|---:|---:|
| 480p | 2.4 | $0.0120 | $0.36 | $0.72 |
| 720p | 4.5 | $0.0225 | $0.675 | $1.35 |
| 1080p | 8.0 | $0.0400 | $1.20 | $2.40 |

The sub-$1 target for a 30–60 second final video is therefore compatible with
480p generation on raw estimated credits only; 720p/1080p do not meet that
target for a 60-second output. These estimates exclude retries and any
supplier-side charges not represented by `creditsConsumed`. The application
checks the estimated project cost against the company's per-operation budget
before reservation, and settles the ledger from reported consumed credits.

**Duration caveat:** the initial production default is single-shot, which
submits exactly one image-to-video task and supports at most 30 seconds. The
60-second figure above is a planning estimate only; it requires multi-scene
generation and local assembly. Multi-scene mode is not part of the initial
accepted production path until its separate end-to-end quality and audio
acceptance is completed. Do not interpret the $0.72 estimate as evidence that
60-second production has already passed acceptance.

## Single-shot mode (one prompt, one submission)

With `generation_mode="single_shot"` (platform default via
`SCENE_VIDEO_GENERATION_MODE`, or per project via `POST /content/scene-projects`
body field `generation_mode`):

1. Platform monitoring/knowledge and GPT scenario generation run unchanged.
   Before it can reach `script_ready`, the deterministic scenario review
   requires the title, full narration, CTA and every scene's narration to
   contain Persian text. Optional on-screen text is always cleared before the
   paid request; no subtitle or caption surface is generated.
2. After the operator approves the scenario, the complete multi-beat scenario
   is composed into ONE prompt (`app/services/prompt_composer.py`): the beats
   stay in order with their durations, the exact Persian narration is embedded
   verbatim with explicit "speak exactly this in Iranian Persian" rules,
   on-screen text and subtitles are forbidden, and the selected character's
   identity is kept consistent across the whole video.
3. Exactly ONE `SceneVideoJob` (`scene_key="single-shot"`) is created for the
   total duration, and exactly ONE paid `createTask` is submitted. Single-shot
   mode never splits the video into multiple paid jobs and never falls back to
   per-scene generation; an out-of-range total duration (6..30 s) fails closed
   before any spend.
4. The single provider MP4 (native audio preserved, no local TTS overlay) is
   the final deliverable via the existing single-clip passthrough path.

The initial release defaults to `generation_mode="single_shot"`. Multi-scene mode is available only when explicitly selected with `generation_mode="multi_scene"`; it creates one paid KIE job per storyboard scene and assembles the returned clips locally while preserving their native audio tracks. It is not the default path. Because the default image-to-video model accepts at most 30 seconds per job, a 60-second deliverable requires multi-scene mode with every scene kept within the model-specific 6–30 second bounds. The multi-scene code path has mock-based tests, but credentialed end-to-end audio/quality acceptance remains a release gate.

## Character reference image

Character images are operator-managed files under `PLATFORM_CHARACTERS_ROOT`
(default `assets/Paltform_charactar`), organised as
`<Platform>/<character name>.png|jpg|jpeg|webp` — for example the Vandiyar set
in `assets/Paltform_charactar/Vandiyar/`. `GET /api/v1/content/character-images`
lists the available keys and validates each image. Two sources are supported:

- `character_key` (recommended): a local registry image. Before the paid
  submission the file is validated (JPEG/PNG/WEBP magic bytes, 1KB..10MB) and
  uploaded through KIE's documented base64 file-upload endpoint
  (`POST https://kieai.redpandaai.co/api/file-base64-upload`, free) which
  returns a public
  `downloadUrl` that the generation task fetches. The image bytes are really
  transmitted to KIE — the character is never "just mentioned in the prompt".
  The adapter then fetches the returned URL once before the credit check to
  confirm it is publicly accessible, is actually an image and is within the
  10MB limit. A storyboard resolves and uploads the character only once.
- `character_image_url`: an operator-supplied public HTTPS URL (JPEG/PNG/WEBP,
  standard port, no query/userinfo, non-private host). The adapter re-validates
  it before submission and fetches it before the credit check; private/reserved
  hosts and redirects to non-public hosts are rejected (SSRF-safe).

A single-shot project without any character source fails with a clear
validation error BEFORE budget reservation and BEFORE any paid request. For
single-image input KIE derives the output geometry from the image itself, so
the adapter intentionally omits `aspect_ratio` in that mode.

## Spend protection

Before any paid submit, the adapter checks the account credit balance and the
required credits (per-second rate x duration), and the service checks the sum
of all pending scene credits before the first billable request — a partial
storyboard can never happen when the balance is too low. Artifact storage and
FFmpeg/ffprobe availability are preflighted before multi-scene spends.

KIE does not expose a documented idempotency key for the createTask route. The
service therefore commits a durable `submitting` fence before the POST. A
timeout, dropped connection, server error, or malformed accepted response is
marked `provider_submission_unknown`; it is never automatically retried. The
operator must inspect KIE task history before a manual retry. Explicit
rate-limit responses and preflight failures are safe non-submission outcomes.

Documented API code 505 is treated as "Feature Disabled" for every route except
a `recordInfo` response that still carries a structured task payload (the
legacy form), so a disabled createTask can never be mistaken for success.
Reference-image accessibility, content type and size are verified before the
credit preflight; a broken image can never consume credits.

## Media retrieval and assembly

Generated result URLs are downloaded immediately and stored in the platform's
artifact storage. Downloads require HTTPS, reject redirects, and reject
private/non-public destination IPs. If the provider's media hostname is known,
set an exact hostname in `SCENE_VIDEO_KIE_DOWNLOAD_ALLOWED_HOSTS`.

One-job projects (single-shot or single-scene) use the provider MP4 as the
final deliverable. Before the project is marked complete for KIE, the stored
MP4 is probed with ffprobe: a video stream, provider-native audio stream,
valid dimensions/codec and a duration matching the paid request (within a
small tolerance) are required, otherwise finalization fails closed. The probe
facts are stored in `assembly_metadata.output_validation`. Multi-scene projects
are concatenated locally with FFmpeg. Native audio is preserved and the local
The KIE-native audio track is preserved so the generated Persian soundtrack is not silently
overwritten. FFmpeg and ffprobe are installed in the project runtime Docker
image.

## Test safety

Adapter, composer, character-registry and pipeline tests use `httpx.MockTransport`
or capability-matched mock providers and do not contact KIE or consume credits.
Real generation is an explicit operator action only. For the first manual paid
acceptance test: create a single-shot project with one character key and a 20s
target (about 48 credits at 480p — confirm the account balance first), approve
the scenario, start video generation, and inspect the KIE task history page
alongside the project's job status.

## Persian-language notes

The composed prompt enforces Iranian Persian speech and exact narration
reproduction, and explicitly forbids rendered text or subtitles, but KIE/Grok
does not contractually guarantee Persian speech quality. The Persian script
remains testable in the database (`project.scenario`, the job's
`prompt`/`narration`); the audible result of the first paid run must be accepted
manually.


## Product decision: KIE-only video and provider-native audio

The initial release intentionally uses KIE.ai as the sole video provider and
uses the audio returned by KIE as part of the generated MP4. This is a product
decision, not an accidental consequence of missing adapters. WaveSpeed has
been removed from the active runtime, registry, configuration and deployment
path. Do not restore it as a fallback or silently route a failed KIE request
to another vendor.

The KIE adapter supports these distinct model contracts:

- `grok-imagine-video-1-5-preview`: text-to-video, 1–15 seconds, native
  audio; the supplier-published 480p price reviewed for this option is
  approximately USD 0.012/second.
- `grok-imagine/image-to-video`: image-to-video, one required reference
  image, 6–30 seconds, native audio; its credit-based pricing is documented
  separately above.

The code default remains `grok-imagine/image-to-video` unless
`SCENE_VIDEO_KIE_MODEL` is explicitly set. Do not describe the 1.5 Preview
price as the price of the image-to-video model. A real credentialed run is
still required to validate the selected model's output quality, Persian
speech, duration, actual credits consumed and final MP4 audio stream.
