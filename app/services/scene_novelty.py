"""Deterministic visual-scene fingerprinting and near-duplicate detection.

This conservative lexical guard catches exact and near-identical visual
directions before paid generation. It is not a claim of semantic AI matching.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher

_TOKEN_RE = re.compile(r"[a-z0-9\u0600-\u06ff]+", re.IGNORECASE)
_VISUAL_LINE_RE = re.compile(r"(?:^|\s)VISUAL(?: PROMPT)?:\s*(.+?)\s*$", re.IGNORECASE)


def normalize_visual_signature(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = normalized.translate(str.maketrans({"ي": "ی", "ك": "ک"}))
    normalized = re.sub(r"[\u064B-\u065F\u0670\u06D6-\u06ED]", "", normalized)
    return " ".join(_TOKEN_RE.findall(normalized))


def visual_fingerprint(value: str) -> str:
    normalized = normalize_visual_signature(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_duplicate_visual(candidate: str, previous: str) -> bool:
    left = normalize_visual_signature(candidate)
    right = normalize_visual_signature(previous)
    if not left or not right:
        return False
    if left == right:
        return True
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if min(len(left_tokens), len(right_tokens)) < 5:
        return False
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, left, right, autojunk=False).ratio()
    return jaccard >= 0.90 or (jaccard >= 0.82 and sequence >= 0.90)


def extract_visual_signatures(prompt: str) -> list[str]:
    signatures: list[str] = []
    for line in (prompt or "").splitlines():
        match = _VISUAL_LINE_RE.search(line)
        if match:
            signature = normalize_visual_signature(match.group(1))
            if signature:
                signatures.append(signature)
    return signatures


__all__ = [
    "extract_visual_signatures",
    "is_duplicate_visual",
    "normalize_visual_signature",
    "visual_fingerprint",
]
