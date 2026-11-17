# WaveSpeed LTX-2.3 text-to-video smoke test

This is an opt-in, paid smoke test for
`wavespeed-ai/ltx-2.3/text-to-video`. It generates a video directly from a
text prompt: **no image, reference file, or upload is required**.

## Product boundary

This test proves one provider-generated text-to-video clip. It does not prove
the complete Vandad multi-scene production contract, storyboard composition,
editing, or AI quality review.

## Prepare

1. Create a WaveSpeed developer API key at https://wavespeed.ai/accesskey and
   top up the account.
2. Set `WAVESPEED_API_KEY` in your local environment or uncommitted `.env`.
   Never commit the real key or paste it into chat.
3. Activate the project virtual environment and install the project dev
   dependencies as described in the README.

## Run a low-cost first test

The published LTX-2.3 estimate for 480p / 5 seconds is approximately USD 0.10.
The provider's live estimate and final charge prevail.

```powershell
. .\.venv\Scripts\Activate.ps1

python .\scripts\wavespeed_smoke_test.py `
  --prompt "Vertical cinematic advertisement: a premium electric vehicle drives slowly through a modern city at blue hour, reflections on wet pavement, smooth tracking camera, realistic lighting, no logos, no captions. Audio: subtle cinematic electronic music and soft city ambience." `
  --resolution 480p `
  --aspect-ratio 9:16 `
  --duration 5
```

The script writes the MP4 to
`artifacts/wavespeed/ltx23-text-to-video.mp4` by default.

## Safety and billing

- The script sends only the text prompt and generation settings to the model.
  It does not call the media-upload endpoint.
- The task-submission POST is sent exactly once. An ambiguous connection
  failure may still mean the provider accepted and billed the task. Check
  WaveSpeed prediction history before manually retrying.
- Polling GET requests may retry transient errors. Paid generation POSTs are
  never automatically retried.
- The script validates the downloaded MP4 container before publishing the
  output file locally.

## Next production gate

Review prompt adherence, visual quality, motion, audio, output duration,
actual charge, and whether multiple scene clips can be composed into the
Vandad final-delivery workflow. Do not mark the full production pipeline
complete based on this one-clip smoke test.
