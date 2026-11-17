# KIE.ai Grok Imagine — image-to-video (single-shot) and Video 1.5 Preview

## Configuration

KIE is an opt-in scene-video provider. Keep the API key in the local `.env`
file only; never commit it or send it to the browser.

```dotenv
SCENE_VIDEO_PROVIDER=kie
SCENE_VIDEO_KIE_API_KEY=...
# Single-shot image-to-video (default): ONE video from ONE reference image.
SCENE_VIDEO_KIE_MODEL=grok-imagine/image-to-video
SCENE_VIDEO_KIE_RESOLUTION=480p
SCENE_VIDEO_KIE_ASPECT_RATIO=9:16
SCENE_VIDEO_KIE_MODE=normal
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

Both models return an MP4 with native audio; audio cannot be disabled. KIE
prices video at 2.4 / 4.5 / 8 credits per second for 480p / 720p / 1080p, so a
20-second 480p single-shot generation costs about 48 credits. Verify current
pricing on kie.ai before relying on the estimate; the adapter's preflight
checks the live credit balance against the same per-second rates, and the
authoritative usage is the task's reported `creditsConsumed` (converted to a
USD ledger amount with `SCENE_VIDEO_KIE_PRICE_PER_CREDIT_USD` — an accounting
assumption, not a billing statement).

## Single-shot mode (one prompt, one submission)

With `generation_mode="single_shot"` (platform default via
`SCENE_VIDEO_GENERATION_MODE`, or per project via `POST /content/scene-projects`
body field `generation_mode`):

1. Platform monitoring/knowledge and GPT scenario generation run unchanged.
   Before it can reach `script_ready`, the deterministic scenario review
   requires the title, full narration, CTA and every scene's
   narration/on-screen text/subtitle to contain Persian text; a pure-English
   surface is retried as a generation failure and can never reach a paid
   provider.
2. After the operator approves the scenario, the complete multi-beat scenario
   is composed into ONE prompt (`app/services/prompt_composer.py`): the beats
   stay in order with their durations, the exact Persian narration is embedded
   verbatim with explicit "speak exactly this in Iranian Persian" rules, the
   Persian on-screen text is preserved right-to-left, and the selected
   character's identity is kept consistent across the whole video.
3. Exactly ONE `SceneVideoJob` (`scene_key="single-shot"`) is created for the
   total duration, and exactly ONE paid `createTask` is submitted. Single-shot
   mode never splits the video into multiple paid jobs and never falls back to
   per-scene generation; an out-of-range total duration (6..30 s) fails closed
   before any spend.
4. The single provider MP4 (native audio preserved, no local TTS overlay) is
   the final deliverable via the existing single-clip passthrough path.

The previous multi-scene storyboard pipeline remains the default
(`generation_mode="multi_scene"`); a prepared project refuses a silent mode
switch instead of creating additional paid jobs.

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
TTS mux is skipped for KIE so the generated Persian soundtrack is not silently
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

The composed prompt enforces Iranian Persian speech, exact narration
reproduction and RTL on-screen text, but KIE/Grok does not contractually
guarantee Persian speech quality. The Persian script remains testable in the
database (`project.scenario`, the job's `prompt`/`narration`); the audible
result of the first paid run must be accepted manually.
