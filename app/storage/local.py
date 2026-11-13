"""Local filesystem artifact storage (MVP backend).

- All keys are validated and resolved inside a configurable storage root;
  traversal (`..`, absolute paths, encoded separators) is rejected.
- Writes are atomic: content streams into ``<key>.<uuid>.tmp`` in the same
  directory, is fsynced, then moved into place with ``os.replace``. Partial
  files are removed on any failure, including size-limit violations.
- Reads stream in bounded chunks via a thread pool, so multi-GB videos never
  load into memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from app.storage.base import (
    ArtifactNotFoundError,
    ArtifactSizeLimitExceededError,
    ArtifactStat,
    ArtifactStorage,
    ArtifactStorageError,
    InvalidStorageKeyError,
    StoredArtifact,
)

READ_CHUNK_BYTES = 1024 * 1024  # 1 MiB
_SAFE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class LocalArtifactStorage(ArtifactStorage):
    def __init__(self, root: str | Path, *, max_size_bytes: int) -> None:
        self._root = Path(root).resolve()
        self._max_size_bytes = max_size_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def _resolve(self, key: str) -> Path:
        """Resolve a validated key to a path strictly inside the storage root."""
        if not key or len(key) > 512:
            raise InvalidStorageKeyError("storage key is missing or too long")
        if not _SAFE_KEY_PATTERN.fullmatch(key):
            raise InvalidStorageKeyError("storage key contains unsupported characters")
        if ".." in key or key.startswith("/"):
            raise InvalidStorageKeyError("storage key must be a relative path inside the storage root")
        candidate = (self._root / key).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise InvalidStorageKeyError("storage key escapes the storage root") from exc
        return candidate

    # ------------------------------------------------------------------- save
    async def save(self, key: str, chunks: AsyncIterator[bytes]) -> StoredArtifact:
        final_path = self._resolve(key)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = final_path.with_name(f"{final_path.name}.{uuid.uuid4().hex}.tmp")

        digest = hashlib.sha256()
        size = 0
        try:
            with tmp_path.open("wb") as handle:
                async for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise ArtifactStorageError("storage chunks must be bytes")
                    size += len(chunk)
                    if size > self._max_size_bytes:
                        raise ArtifactSizeLimitExceededError(
                            f"artifact exceeds the configured size limit of {self._max_size_bytes} bytes"
                        )
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
                await asyncio.to_thread(handle.flush)
                await asyncio.to_thread(os.fsync, handle.fileno())
            os.replace(tmp_path, final_path)  # atomic within the same directory
        except BaseException:
            # Never leave incomplete temporary files behind.
            tmp_path.unlink(missing_ok=True)
            raise
        return StoredArtifact(key=key, size_bytes=size, sha256=digest.hexdigest())

    # ------------------------------------------------------------------- read
    async def open(self, key: str) -> AsyncIterator[bytes]:
        path = self._resolve(key)
        if not path.is_file():
            raise ArtifactNotFoundError(f"artifact {key!r} does not exist")

        def _read_chunk(offset: int, length: int) -> bytes:
            with path.open("rb") as handle:
                handle.seek(offset)
                return handle.read(length)

        offset = 0
        while True:
            chunk = await asyncio.to_thread(_read_chunk, offset, READ_CHUNK_BYTES)
            if not chunk:
                return
            offset += len(chunk)
            yield chunk

    # ------------------------------------------------------------------- misc
    async def stat(self, key: str) -> ArtifactStat | None:
        path = self._resolve(key)
        if not path.is_file():
            return None
        return ArtifactStat(key=key, size_bytes=path.stat().st_size)

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        path.unlink(missing_ok=True)

    async def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def local_path(self, key: str) -> Path | None:
        path = self._resolve(key)
        return path if path.is_file() else None
