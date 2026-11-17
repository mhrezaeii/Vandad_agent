"""Local artifact storage tests: atomicity, streaming, checksums, path safety."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.storage.base import (
    ArtifactNotFoundError,
    ArtifactSizeLimitExceededError,
    InvalidStorageKeyError,
)
from app.storage.local import LocalArtifactStorage


@pytest.fixture
def storage(tmp_path) -> LocalArtifactStorage:
    return LocalArtifactStorage(tmp_path / "artifacts", max_size_bytes=1024 * 1024)


async def _byte_chunks(*payloads: bytes):
    for payload in payloads:
        yield payload


async def test_save_streams_and_returns_checksum(storage: LocalArtifactStorage):
    payload = b"hello artifact" * 100
    stored = await storage.save("executions/e1/video/one.bin", _byte_chunks(payload))
    assert stored.size_bytes == len(payload)
    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    assert await storage.exists("executions/e1/video/one.bin")


async def test_read_streams_exact_content(storage: LocalArtifactStorage):
    payload = bytes(range(256)) * 3000  # spans multiple 1 MiB read chunks
    await storage.save("executions/e1/video/big.bin", _byte_chunks(payload))

    collected = bytearray()
    async for chunk in storage.open("executions/e1/video/big.bin"):
        collected.extend(chunk)
        assert len(chunk) <= 1024 * 1024
    assert bytes(collected) == payload


async def test_write_is_atomic_no_temp_files_left(storage: LocalArtifactStorage, tmp_path: Path):
    await storage.save("executions/e1/video/ok.bin", _byte_chunks(b"data"))

    # A failing write (size limit) must leave no temporary file behind.
    with pytest.raises(ArtifactSizeLimitExceededError):
        await storage.save(
            "executions/e1/video/too-big.bin",
            _byte_chunks(b"x" * (1024 * 1024 + 1)),
        )
    artifacts_dir = tmp_path / "artifacts" / "executions" / "e1" / "video"
    leftovers = [p.name for p in artifacts_dir.iterdir() if ".tmp" in p.name]
    assert leftovers == []
    assert not (artifacts_dir / "too-big.bin").exists()
    assert (artifacts_dir / "ok.bin").is_file()


async def test_size_limit_enforced_mid_stream(storage: LocalArtifactStorage):
    async def _chunks():
        yield b"a" * 600_000
        yield b"b" * 600_000  # crosses the 1 MiB limit

    with pytest.raises(ArtifactSizeLimitExceededError):
        await storage.save("executions/e1/x.bin", _chunks())


@pytest.mark.parametrize(
    "bad_key",
    [
        "../escape.bin",
        "executions/../../escape.bin",
        "/absolute/path.bin",
        "with space.bin",
        "with\nnewline.bin",
        "",
        "a" * 600,
        "emoji-\U0001f600.bin",
    ],
)
async def test_path_traversal_and_unsafe_keys_rejected(storage: LocalArtifactStorage, bad_key: str):
    with pytest.raises(InvalidStorageKeyError):
        await storage.save(bad_key, _byte_chunks(b"x"))
    with pytest.raises(InvalidStorageKeyError):
        async for _chunk in storage.open(bad_key):
            pass
    with pytest.raises(InvalidStorageKeyError):
        await storage.delete(bad_key)
    with pytest.raises(InvalidStorageKeyError):
        await storage.stat(bad_key)


async def test_missing_key_raises_not_found(storage: LocalArtifactStorage):
    with pytest.raises(ArtifactNotFoundError):
        async for _chunk in storage.open("executions/e9/none.bin"):
            pass


async def test_open_is_async_iterator_not_awaitable(storage: LocalArtifactStorage):
    # open() must return an async generator (streaming), never a loaded buffer.
    await storage.save("executions/e1/video/k.bin", _byte_chunks(b"data"))
    stream = storage.open("executions/e1/video/k.bin")
    assert hasattr(stream, "__aiter__")
    collected = bytearray()
    async for chunk in stream:
        collected.extend(chunk)
    assert bytes(collected) == b"data"


async def test_delete_removes_file_and_is_idempotent(storage: LocalArtifactStorage):
    await storage.save("executions/e1/video/gone.bin", _byte_chunks(b"data"))
    assert await storage.exists("executions/e1/video/gone.bin")
    await storage.delete("executions/e1/video/gone.bin")
    assert not await storage.exists("executions/e1/video/gone.bin")
    await storage.delete("executions/e1/video/gone.bin")  # idempotent


async def test_persistence_across_instances(tmp_path: Path):
    """Files written by one instance are visible to a freshly constructed one
    (the Docker named-volume persistence assumption)."""
    root = tmp_path / "artifacts"
    first = LocalArtifactStorage(root, max_size_bytes=1024 * 1024)
    await first.save("executions/e1/video/persist.mp4", _byte_chunks(b"video-bytes"))

    second = LocalArtifactStorage(root, max_size_bytes=1024 * 1024)
    stat = await second.stat("executions/e1/video/persist.mp4")
    assert stat is not None and stat.size_bytes == len(b"video-bytes")
    collected = bytearray()
    async for chunk in second.open("executions/e1/video/persist.mp4"):
        collected.extend(chunk)
    assert bytes(collected) == b"video-bytes"


async def test_overwrite_same_key_replaces_content(storage: LocalArtifactStorage):
    await storage.save("executions/e1/video/same.bin", _byte_chunks(b"first"))
    await storage.save("executions/e1/video/same.bin", _byte_chunks(b"second-version"))
    collected = bytearray()
    async for chunk in storage.open("executions/e1/video/same.bin"):
        collected.extend(chunk)
    assert bytes(collected) == b"second-version"
