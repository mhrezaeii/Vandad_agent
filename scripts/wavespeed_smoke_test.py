"""Paid, opt-in WaveSpeed LTX-2 image-to-video smoke test.

This is deliberately an isolated provider smoke test, not a production
Video Production Agent adapter: LTX-2 image-to-video returns a scene clip and
does not document provider-side storyboard composition or AI quality review.
The generation POST is sent exactly once because an ambiguous timeout may
still have created a billable prediction.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

API_BASE = "https://api.wavespeed.ai/api/v3"
MODEL_ID = "wavespeed-ai/ltx-2-19b/image-to-video"
MAX_IMAGE_BYTES = 200 * 1024 * 1024
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


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def upload_image(client: httpx.Client, api_key: str, image_path: Path) -> str:
    if not image_path.is_file():
        raise WaveSpeedError(f"Reference image does not exist: {image_path}")
    size = image_path.stat().st_size
    if size <= 0 or size > MAX_IMAGE_BYTES:
        raise WaveSpeedError("Reference image must be between 1 byte and 200 MB.")
    content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    ticket_response = client.post(
        f"{API_BASE}/media/uploads",
        headers={**_auth_headers(api_key), "Content-Type": "application/json"},
        json={"filename": image_path.name, "size": size, "content_type": content_type},
    )
    if ticket_response.status_code != 200:
        if ticket_response.status_code == 401:
            raise WaveSpeedError("WaveSpeed rejected the API key (HTTP 401).")
        if ticket_response.status_code == 429:
            raise WaveSpeedError("WaveSpeed image-upload quota/rate limit reached (HTTP 429).")
        raise WaveSpeedError(f"WaveSpeed image-upload ticket failed (HTTP {ticket_response.status_code}).")
    ticket = _data(ticket_response.json())
    upload = ticket.get("upload")
    if not isinstance(upload, dict):
        raise WaveSpeedError("WaveSpeed upload ticket is missing upload instructions.")
    upload_url = _https_url(upload.get("url"), label="temporary upload")
    upload_headers = upload.get("headers")
    if not isinstance(upload_headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in upload_headers.items()
    ):
        raise WaveSpeedError("WaveSpeed upload ticket contains invalid headers.")
    # Important: the temporary storage URL receives only the ticket headers,
    # never the WaveSpeed API key.
    with image_path.open("rb") as image_file:
        upload_response = client.put(
            upload_url,
            headers=upload_headers,
            content=image_file,
            timeout=300.0,
            follow_redirects=False,
        )
    if not 200 <= upload_response.status_code < 300:
        raise WaveSpeedError(f"WaveSpeed image upload failed (HTTP {upload_response.status_code}).")
    return _https_url(ticket.get("download_url"), label="uploaded image")


def submit_prediction(
    client: httpx.Client,
    api_key: str,
    *,
    image_url: str,
    prompt: str,
    resolution: str,
    duration: int,
    seed: int | None = None,
) -> str:
    body: dict[str, Any] = {
        "image": _https_url(image_url, label="reference image"),
        "prompt": prompt,
        "resolution": resolution,
        "duration": duration,
    }
    if seed is not None:
        body["seed"] = seed
    try:
        response = client.post(
            f"{API_BASE}/{MODEL_ID}",
            headers={**_auth_headers(api_key), "Content-Type": "application/json"},
            json=body,
            timeout=60.0,
        )
    except httpx.TransportError as exc:
        # Never automatically repeat a paid submit: the provider may have
        # accepted the request before the connection was lost.
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
    return _prediction_id(response.json())


def poll_prediction(
    client: httpx.Client,
    api_key: str,
    prediction_id: str,
    *,
    max_wait_seconds: int = 900,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    result_url = f"{API_BASE}/predictions/{prediction_id}/result"
    deadline = time.monotonic() + max_wait_seconds
    delay = poll_interval_seconds
    while time.monotonic() < deadline:
        try:
            response = client.get(result_url, headers=_auth_headers(api_key), timeout=60.0)
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
            # Do not include raw provider payloads: they may contain private URLs.
            raise WaveSpeedError(f"WaveSpeed prediction ended with terminal status: {status}.")
        time.sleep(delay)
        delay = min(10.0, delay * 1.25)
    raise WaveSpeedError(
        f"Timed out waiting for prediction {prediction_id}; it may still be running. "
        "Check WaveSpeed history before starting another paid prediction."
    )


def download_video(client: httpx.Client, video_url: str, output_path: Path) -> int:
    url = _https_url(video_url, label="video output")
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
    parser = argparse.ArgumentParser(description="Generate one paid WaveSpeed LTX-2 image-to-video test clip.")
    parser.add_argument("--image", required=True, type=Path, help="Local JPG/PNG/WebP reference image.")
    parser.add_argument("--prompt", required=True, help="Describe one simple motion/action.")
    parser.add_argument("--resolution", choices=("480p", "720p", "1080p"), default="480p")
    parser.add_argument("--duration", choices=(5, 10, 15, 20), type=int, default=5)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--output", type=Path, default=Path("artifacts/wavespeed/ltx2-smoke.mp4"))
    parser.add_argument("--max-wait-seconds", type=int, default=900)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    api_key = os.environ.get("WAVESPEED_API_KEY", "").strip()
    if not api_key:
        print("WAVESPEED_API_KEY is not set. Add it to your local .env; do not commit or paste it into chat.", file=sys.stderr)
        return 2
    if not args.prompt.strip():
        print("Prompt must not be empty.", file=sys.stderr)
        return 2
    if args.max_wait_seconds < 30:
        print("--max-wait-seconds must be at least 30.", file=sys.stderr)
        return 2

    rates = {"480p": 0.06, "720p": 0.08, "1080p": 0.12}
    estimate = rates[args.resolution] * (args.duration / 5)
    print(f"Model: {MODEL_ID}")
    print(f"Requested output: {args.resolution}, {args.duration}s")
    print(f"Reference upload + generation estimate: about ${estimate:.2f} (provider charge prevails).")
    print("The paid generation request will be submitted exactly once.")

    try:
        with httpx.Client(timeout=60.0, follow_redirects=False) as client:
            image_url = upload_image(client, api_key, args.image)
            print("Reference image uploaded.")
            prediction_id = submit_prediction(
                client, api_key, image_url=image_url, prompt=args.prompt.strip(),
                resolution=args.resolution, duration=args.duration, seed=args.seed,
            )
            print(f"Prediction accepted: {prediction_id}")
            result = poll_prediction(
                client, api_key, prediction_id, max_wait_seconds=args.max_wait_seconds
            )
            video_url = _video_url({"data": result})
            size = download_video(client, video_url, args.output)
        print(f"Video saved: {args.output.resolve()}")
        print(f"File size: {size:,} bytes")
        print(f"Provider prediction: {prediction_id}")
        return 0
    except (WaveSpeedError, httpx.HTTPError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"WaveSpeed smoke test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
