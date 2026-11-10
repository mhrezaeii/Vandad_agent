"""SecretValue: a string wrapper that never leaks through repr/str/format.

Passing credential material through application code as ``SecretValue``
instead of ``str`` makes accidental exposure via logs, exceptions, or dataclass
reprs structurally impossible. Only ``reveal()`` — used exclusively inside the
trusted credential-resolution boundary — returns the plaintext.
"""

from __future__ import annotations

_MASK = "***"


class SecretValue:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """Return the plaintext. Only call inside the credential boundary."""
        return object.__getattribute__(self, "_value")

    def is_empty(self) -> bool:
        return not object.__getattribute__(self, "_value")

    def __repr__(self) -> str:
        return "SecretValue(***)"

    def __str__(self) -> str:
        return _MASK

    def __format__(self, format_spec: str) -> str:
        return _MASK

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return object.__getattribute__(self, "_value") == object.__getattribute__(other, "_value")
        # Never compare equal to raw strings, so secrets can't leak through
        # equality-based assertions or dict lookups.
        return NotImplemented

    def __hash__(self) -> int:
        return hash(object.__getattribute__(self, "_value"))
