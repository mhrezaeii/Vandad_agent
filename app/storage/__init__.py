"""Artifact storage package.

`app.storage.base.ArtifactStorage` is the backend-agnostic contract;
`app.storage.local.LocalArtifactStorage` is the MVP filesystem backend. An
S3-compatible backend later implements the same interface.
"""

from app.storage.base import (
    ArtifactNotFoundError,
    ArtifactSizeLimitExceededError,
    ArtifactStat,
    ArtifactStorage,
    ArtifactStorageError,
    InvalidStorageKeyError,
    StoredArtifact,
)
from app.storage.local import LocalArtifactStorage

__all__ = [
    "ArtifactNotFoundError",
    "ArtifactSizeLimitExceededError",
    "ArtifactStat",
    "ArtifactStorage",
    "ArtifactStorageError",
    "InvalidStorageKeyError",
    "LocalArtifactStorage",
    "StoredArtifact",
]
