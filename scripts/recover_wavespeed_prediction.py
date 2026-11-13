"""Recover an existing WaveSpeed prediction without submitting a new one.

This helper performs GET-only status checks against the documented
``/predictions/{id}/result`` endpoint. It never POSTs a paid generation request.
Use it when a previous run has an ambiguous outcome or when a prediction ID was
persisted outside the product database.

Examples::

    python scripts/recover_wavespeed_prediction.py 0270e66a02bf4cc996d7126f0cb76261
    python scripts/recover_wavespeed_prediction.py <id> --output artifacts/wavespeed/recovered.mp4
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a development dependency
    pass

API_BASE = os.environ.get(
    "SCENE_VIDEO_WAVESPEED_BASE_URL", "https://api.wavespeed.ai/api/v3"
).rstrip("/")
TIMEOUT_SECONDS = float(os.environ.get("SCENE_VIDEO_WAVESPEED_TIMEOUT_SECONDS", "60"))
POLL_INTERVAL_SECONDS = max(2.0, float(os.environ.get("SCENE_VIDEO_POLL_INTERVAL_SECONDS", "2")))
TERMINAL_FAILURES = {"failed", "cancelled", "canceled", "timeout", "deleted"}
MP4_BRANDS = {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash"}


class RecoveryError(RuntimeError):
    """Sanitized recovery error that never contains credentials."""


def _data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RecoveryError("WaveSpeed returned an invalid JSON object.")
    value = payload.get("data", payload)
    if not isinstance(value, dict):
        raise RecoveryError("WaveSpeed returned an invalid data object.")
    return value


def _api_key() -> str:
    key = (
        os.environ.get("SCENE_VIDEO_WAVESPEED_API_KEY")
        or os.environ.get("WAVESPEED_API_KEY")
        or ""
    ).strip()
    if not key:
        raise RecoveryError("WaveSpeed API key is not configured; nothing was requested.")
    return key


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _https_output_url(value: Any) -> str:
    if not isinstance(value, str):
        raise RecoveryError("The prediction has no output URL.")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RecoveryError("The prediction output URL is not a safe HTTPS URL.")
    if parsed.port not in (None, 443):
        raise RecoveryError("The prediction output URL uses an unexpected port.")
    return value


def _first_output_url(payload: dict[str, Any]) -> str:
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise RecoveryError("The completed prediction has no outputs.")
    first = outputs[0]
    if isinstance(first, dict):
        first = first.get("url") or first.get("video_url")
    return _https_output_url(first)


def _assert_public_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise RecoveryError("The output host is not a public address.")
        return host
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RecoveryError("The output host could not be resolved.") from exc
    if not addresses or any(
        not ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_global for item in addresses
    ):
        raise RecoveryError("The output host resolves to a non-public address.")
    return host


def fetch_result(client: httpx.Client, api_key: str, prediction_id: str) -> dict[str, Any]:
    url = f"{API_BASE}/predictions/{prediction_id}/result"
    attempts = 5
    delay = POLL_INTERVAL_SECONDS
    for attempt in range(attempts):
        try:
            response = client.get(url, headers=_headers(api_key), timeout=TIMEOUT_SECONDS)
        except httpx.TransportError as exc:
            if attempt + 1 >= attempts:
                raise RecoveryError(
                    "WaveSpeed could not be reached after bounded retries."
                ) from exc
            time.sleep(delay)
            delay = min(15.0, delay * 1.75)
            continue
        if response.status_code == 401:
            raise RecoveryError("WaveSpeed rejected the API key (HTTP 401).")
        if response.status_code == 403:
            raise RecoveryError("WaveSpeed denied this read (HTTP 403).")
        if response.status_code == 404:
            raise RecoveryError("WaveSpeed has no prediction with the supplied ID (HTTP 404).")
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            if attempt + 1 >= attempts:
                raise RecoveryError(f"WaveSpeed result read failed (HTTP {response.status_code}).")
            retry_after = response.headers.get("Retry-After")
            try:
                delay = max(2.0, min(30.0, float(retry_after))) if retry_after else min(15.0, delay * 1.75)
            except ValueError:
                delay = min(15.0, delay * 1.75)
            time.sleep(delay)
            continue
        if not 200 <= response.status_code < 300:
            raise RecoveryError(f"WaveSpeed result read failed (HTTP {response.status_code}).")
        try:
            return _data(response.json())
        except ValueError as exc:
            raise RecoveryError("WaveSpeed returned invalid JSON.") from exc
    raise RecoveryError("WaveSpeed result read was exhausted without a response.")


def download(client: httpx.Client, url: str, output: Path) -> tuple[int, str]:
    _assert_public_host(url)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".part")
    total = 0
    prefix = bytearray()
    try:
        with client.stream("GET", url, timeout=180.0, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                raise RecoveryError("The output URL redirected; refusing an unverified host.")
            if response.status_code != 200:
                raise RecoveryError(f"Video download failed (HTTP {response.status_code}).")
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not (
                content_type.startswith("video/") or "application/octet-stream" in content_type
            ):
                raise RecoveryError("The output endpoint did not return a video content type.")
            with temp.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    if len(prefix) < 32:
                        prefix.extend(chunk[: 32 - len(prefix)])
                    total += len(chunk)
                    handle.write(chunk)
        if total < 12 or bytes(prefix[4:8]) != b"ftyp":
            raise RecoveryError("The downloaded file is not a recognizable MP4 container.")
        brand = bytes(prefix[8:12])
        if brand not in MP4_BRANDS:
            # ftyp still identifies an ISO base media file; record the brand only.
            brand = brand.strip() or b"unknown"
        temp.replace(output)
        return total, brand.decode("ascii", "replace")
    finally:
        if temp.exists():
            temp.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="GET-only recovery of an existing WaveSpeed prediction.")
    parser.add_argument("prediction_id")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/wavespeed/recovered-prediction.mp4"),
    )
    args = parser.parse_args()

    prediction_id = args.prediction_id.strip()
    if not prediction_id or "/" in prediction_id:
        print("A single prediction ID is required.", file=sys.stderr)
        return 2

    try:
        api_key = _api_key()
        with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
            result = fetch_result(client, api_key, prediction_id)
            status = str(result.get("status") or "").strip().lower()
            print(f"Prediction: {prediction_id}")
            print(f"Status: {status or 'unknown'}")
            if result.get("error"):
                print(f"Provider error: {str(result.get('error'))[:500]}")
            if status == "completed":
                url = _first_output_url(result)
                host = _assert_public_host(url)
                print(f"Output host: {host}")
                size, brand = download(client, url, args.output)
                print(f"Video saved: {args.output.resolve()}")
                print(f"File size: {size:,} bytes")
                print(f"MP4 brand: {brand}")
                return 0
            if status in TERMINAL_FAILURES:
                print("The existing prediction ended in a failed terminal state; no new prediction was submitted.")
                return 1
            print("The prediction is still processing. Re-run this script later; do not submit a duplicate.")
            return 3
    except (RecoveryError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"Recovery failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
