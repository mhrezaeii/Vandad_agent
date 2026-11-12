# WaveSpeed LTX-2 image-to-video smoke test

This is an opt-in, paid smoke test for the WaveSpeedAI model
`wavespeed-ai/ltx-2-19b/image-to-video`. It is intentionally separate from
the production Video Production Agent.

## Product boundary

WaveSpeed documents image-conditioned generation of a 5–20 second clip with
synchronized audio. The documented model API does **not** expose storyboard
composition or final-video AI quality-review endpoints. Therefore this test
proves one reference-image-to-video clip only; it does not prove the complete
multi-scene Vandad delivery contract and must not be advertised as production
pipeline acceptance.

## Prepare

1. Create an API key at https://wavespeed.ai/accesskey and top up the account.
2. Add the key to the local `.env` only:

   ```dotenv
   WAVESPEED_API_KEY=your-real-key
   ```

3. Never commit `.env`, paste the key into chat, or send it to the temporary
   upload URL. The script reads the key from the process environment.
4. Activate the project virtual environment and install the project dev
   dependencies as described in the README.

## Run a low-cost first test

Use a clear JPG, PNG or WebP image and describe one simple movement:

```powershell
. .\.venv\Scripts\Activate.ps1
$env:WAVESPEED_API_KEY = (Get-Content .env | Where-Object { $_ -match '^WAVESPEED_API_KEY=' } | Select-Object -First 1).Split('=',2)[1].Trim().Trim('"')
python .\scripts\wavespeed_smoke_test.py --image .\path\to\reference.png --prompt "Slow cinematic camera push-in; subtle natural movement; preserve the original composition." --resolution 480p --duration 5
```

Expected list-price estimate for the first generation is approximately USD
0.06 at 480p/5s, before any account-specific taxes or pricing changes. The
provider's final charge is authoritative.

## Safety and billing

- The script uploads the local image using WaveSpeed's signed upload-ticket
  flow. The API key is sent only to `api.wavespeed.ai`; it is never sent to
  the temporary object-storage URL.
- The task-submission POST is sent exactly once. An ambiguous connection
  failure may still mean the provider accepted and billed the task. Check
  WaveSpeed prediction history before manually retrying.
- Polling GET requests may retry transient errors. The script does not retry
  paid generation POSTs.
- The script downloads the completed MP4 to
  `artifacts/wavespeed/ltx2-smoke.mp4` by default.
- Uploaded input media is subject to WaveSpeed's retention policy; the
  documented upload flow currently retains inputs for seven days.

## Next production gate

After reviewing the clip's visual quality, motion fidelity, output resolution,
audio and actual charge, decide whether WaveSpeed remains a scene-generation
provider or whether a separate AI provider is needed for provider-side
composition and quality review. Do not enable WaveSpeed as a production
final-delivery provider until those required capabilities are implemented and
verified.
