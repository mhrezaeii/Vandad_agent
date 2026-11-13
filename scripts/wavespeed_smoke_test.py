"""Paid, opt-in WaveSpeed LTX-2.3 text-to-video smoke test.

Generates a video directly from a text prompt. No image, file upload, or
reference media is required. The paid generation POST is sent exactly once:
an ambiguous timeout may still have created a billable prediction.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # The application settings loader still reads .env; this standalone script
    # also works with explicitly exported environment variables.
    pass

API_BASE = os.environ.get("SCENE_VIDEO_WAVESPEED_BASE_URL", "https://api.wavespeed.ai/api/v3").rstrip("/")
MODEL_ID = os.environ.get("SCENE_VIDEO_WAVESPEED_MODEL", "wavespeed-ai/ltx-2.3/text-to-video")
TIMEOUT_SECONDS = float(os.environ.get("SCENE_VIDEO_WAVESPEED_TIMEOUT_SECONDS", "60"))
POLL_INTERVAL_SECONDS = max(2.0, float(os.environ.get("SCENE_VIDEO_POLL_INTERVAL_SECONDS", "2")))
TERMINAL_FAILURES = {"failed", "cancelled", "timeout", "deleted"}


class WaveSpeedError(RuntimeError):
    """Sanitized WaveSpeed smoke-test error."""


def _data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise WaveSpeedError("WaveSpeed returned an invalid JSON object.")
    value = payload.get("data", payload)
    if not isinstance(value, dict):
        raise WaveSpeedError("WaveSpeed returned an invalid data object.")
    return value


def _https_url(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise WaveSpeedError(f"WaveSpeed did not return a valid {label} URL.")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise WaveSpeedError(f"WaveSpeed returned an unsafe {label} URL.")
    return value


def _prediction_id(payload: Any) -> str:
    value = _data(payload).get("id")
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise WaveSpeedError("WaveSpeed submission response did not contain a prediction ID.")
    return str(value)


def _video_url(payload: Any) -> str:
    outputs = _data(payload).get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise WaveSpeedError("Completed WaveSpeed prediction has no output URL.")
    first = outputs[0]
    if isinstance(first, dict):
        first = first.get("url") or first.get("video_url")
    return _https_url(first, label="video output")


def build_payload(
    *, prompt: str, resolution: str, aspect_ratio: str, duration: int, seed: int | None = None
) -> dict[str, Any]:
    if not prompt.strip():
        raise WaveSpeedError("Prompt must not be empty.")
    if resolution not in {"480p", "720p", "1080p"}:
        raise WaveSpeedError("Unsupported resolution.")
    if aspect_ratio not in {"16:9", "9:16"}:
        raise WaveSpeedError("Unsupported aspect ratio.")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 5 <= duration <= 20:
        raise WaveSpeedError("Duration must be an integer from 5 through 20 seconds.")
    payload: dict[str, Any] = {
        "prompt": prompt.strip(),
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "duration": duration,
    }
    if seed is not None:
        payload["seed"] = seed
    return payload


def estimate_cost_usd(resolution: str, duration: int) -> float:
    rates = {"480p": 0.10, "720p": 0.15, "1080p": 0.20}
    if resolution not in rates or isinstance(duration, bool) or not isinstance(duration, int) or not 5 <= duration <= 20:
        raise WaveSpeedError("Cannot estimate cost for the selected model settings.")
    return rates[resolution] * duration / 5


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def submit_prediction(
    client: httpx.Client,
    api_key: str,
    *,
    prompt: str,
    resolution: str,
    aspect_ratio: str,
    duration: int,
    seed: int | None = None,
) -> str:
    body = build_payload(
        prompt=prompt, resolution=resolution, aspect_ratio=aspect_ratio, duration=duration, seed=seed
    )
    try:
        response = client.post(
            f"{API_BASE}/{MODEL_ID}",
            headers={**_auth_headers(api_key), "Content-Type": "application/json"},
            json=body,
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        raise WaveSpeedError(
            "Submission outcome is uncertain. Check WaveSpeed prediction history before retrying."
        ) from exc
    if response.status_code == 401:
        raise WaveSpeedError("WaveSpeed rejected the API key (HTTP 401).")
    if response.status_code == 403:
        raise WaveSpeedError("WaveSpeed denied this request (account or API-key IP allowlist).")
    if response.status_code == 402:
        raise WaveSpeedError("WaveSpeed reports insufficient account balance.")
    if response.status_code == 429:
        raise WaveSpeedError("WaveSpeed rate limit reached; the paid submit was not retried.")
    if not 200 <= response.status_code < 300:
        raise WaveSpeedError(f"WaveSpeed task submission failed (HTTP {response.status_code}).")
    try:
        payload = response.json()
    except ValueError as exc:
        raise WaveSpeedError("WaveSpeed submission returned invalid JSON; check prediction history before retrying.") from exc
    return _prediction_id(payload)

def poll_prediction(
    client: httpx.Client,
    api_key: str,
    prediction_id: str,
    *,
    max_wait_seconds: int = 900,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    result_url = f"{API_BASE}/predictions/{prediction_id}/result"
    deadline = time.monotonic() + max_wait_seconds
    delay = poll_interval_seconds
    while time.monotonic() < deadline:
        try:
            response = client.get(result_url, headers=_auth_headers(api_key), timeout=TIMEOUT_SECONDS)
        except httpx.TransportError:
            time.sleep(delay)
            delay = min(10.0, delay * 1.5)
            continue
        if response.status_code == 401:
            raise WaveSpeedError("WaveSpeed rejected the API key while polling (HTTP 401).")
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            retry_after = response.headers.get("Retry-After")
            try:
                delay = min(30.0, max(2.0, float(retry_after))) if retry_after else min(10.0, delay * 1.5)
            except ValueError:
                delay = min(10.0, delay * 1.5)
            time.sleep(delay)
            continue
        if not 200 <= response.status_code < 300:
            raise WaveSpeedError(f"WaveSpeed result query failed (HTTP {response.status_code}).")
        result = _data(response.json())
        status = str(result.get("status") or "").lower()
        if status == "completed":
            return result
        if status in TERMINAL_FAILURES:
            raise WaveSpeedError(f"WaveSpeed prediction ended with terminal status: {status}.")
        time.sleep(delay)
        delay = min(10.0, delay * 1.25)
    raise WaveSpeedError(
        f"Timed out waiting for prediction {prediction_id}; it may still be running. "
        "Check WaveSpeed history before starting another paid prediction."
    )


def download_video(
    client: httpx.Client, video_url: str, output_path: Path, *, allowed_hosts: set[str] | None = None
) -> int:
    url = _https_url(video_url, label="video output")
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if allowed_hosts and host not in {item.lower().rstrip(".") for item in allowed_hosts}:
        raise WaveSpeedError("Video output host is not in the configured download allowlist.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".part")
    try:
        with client.stream("GET", url, timeout=180.0, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                raise WaveSpeedError("WaveSpeed output URL redirected; refusing an unverified download host.")
            if response.status_code != 200:
                raise WaveSpeedError(f"Video download failed (HTTP {response.status_code}).")
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not (
                content_type.startswith("video/") or "octet-stream" in content_type
            ):
                raise WaveSpeedError("WaveSpeed output URL did not return a video content type.")
            total = 0
            prefix = bytearray()
            with temp_path.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    if len(prefix) < 32:
                        prefix.extend(chunk[: 32 - len(prefix)])
                    total += len(chunk)
                    output.write(chunk)
        if total < 12 or bytes(prefix[4:8]) != b"ftyp":
            raise WaveSpeedError("Downloaded output is not a recognizable MP4 file.")
        temp_path.replace(output_path)
        return total
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate one paid WaveSpeed LTX-2.3 text-to-video test clip (no image input)."
    )
    parser.add_argument("--prompt", required=True, help="Describe the complete scene and motion.")
    parser.add_argument("--resolution", choices=("480p", "720p", "1080p"), default=os.environ.get("SCENE_VIDEO_WAVESPEED_RESOLUTION", "480p"))
    parser.add_argument("--aspect-ratio", choices=("16:9", "9:16"), default=os.environ.get("SCENE_VIDEO_WAVESPEED_ASPECT_RATIO", "9:16"))
    parser.add_argument("--duration", choices=tuple(range(5, 21)), type=int, default=int(os.environ.get("SCENE_VIDEO_WAVESPEED_DURATION_SECONDS", "5")))
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--output", type=Path, default=Path("artifacts/wavespeed/ltx23-text-to-video.mp4"))
    parser.add_argument("--max-wait-seconds", type=int, default=int(os.environ.get("SCENE_VIDEO_WAVESPEED_MAX_WAIT_SECONDS", "900")))
    parser.add_argument("--max-cost-usd", type=float, default=float(os.environ.get("SCENE_VIDEO_WAVESPEED_MAX_COST_USD", "0.10")))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    api_key = (
        os.environ.get("SCENE_VIDEO_WAVESPEED_API_KEY")
        or os.environ.get("WAVESPEED_API_KEY", "")
    ).strip()
    if not api_key:
        print(
            "WAVESPEED_API_KEY is not set. "
            "Add it to your local .env; do not commit or paste it into chat.",
            file=sys.stderr,
        )
        return 2

    allowed_hosts = {
        item.strip().lower().rstrip(".")
        for item in os.environ.get("SCENE_VIDEO_WAVESPEED_DOWNLOAD_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    } or None

    if args.max_wait_seconds < 30:
        print("--max-wait-seconds must be at least 30.", file=sys.stderr)
        return 2

    if not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0:
        print("--max-cost-usd must be a positive finite amount.", file=sys.stderr)
        return 2

    estimate = estimate_cost_usd(args.resolution, args.duration)
    if estimate > args.max_cost_usd:
        print(
            f"Estimated charge ${estimate:.2f} exceeds the approved cap "
            f"${args.max_cost_usd:.2f}; no prediction was submitted.",
            file=sys.stderr,
        )
        return 2

    print(f"Model: {MODEL_ID}")
    print(
        f"Requested output: {args.resolution}, "
        f"{args.aspect_ratio}, {args.duration}s"
    )
    print(
        f"Text-only generation estimate: about ${estimate:.2f}; "
        f"approved estimated cap: ${args.max_cost_usd:.2f} (provider charge prevails)."
    )
    print("No image will be uploaded.")
    print("The paid generation request will be submitted exactly once.")

    try:
        with httpx.Client(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            prediction_id = submit_prediction(
                client,
                api_key,
                prompt=args.prompt,
                resolution=args.resolution,
                aspect_ratio=args.aspect_ratio,
                duration=args.duration,
                seed=args.seed,
            )

            print(f"Prediction accepted: {prediction_id}")

            result = poll_prediction(
                client,
                api_key,
                prediction_id,
                max_wait_seconds=args.max_wait_seconds,
            )

            video_url = _video_url({"data": result})
            size = download_video(client, video_url, args.output, allowed_hosts=allowed_hosts)

        print(f"Video saved: {args.output.resolve()}")
        print(f"File size: {size:,} bytes")
        print(f"Provider prediction: {prediction_id}")
        return 0

    except (
        WaveSpeedError,
        httpx.HTTPError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"WaveSpeed smoke test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
