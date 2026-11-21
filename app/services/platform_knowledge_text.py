"""Shared Persian text normalisation for source-grounded knowledge."""

from __future__ import annotations


def normalise_evidence_text(text: str) -> str:
    """Unicode-normalise Persian text for verbatim evidence comparison.

    Crawler text and extracted excerpts may differ in half-space (ZWNJ), bidi
    marks, Arabic/Persian digit forms, kaf/yeh variants, or whitespace without
    any of them being a paraphrase. Both sides are normalised the same way
    before comparison; this is not a weakening of the grounding check.
    """
    text = text.casefold()
    for mark in ("\u200c", "\u200d", "\u200e", "\u200f", "\ufeff"):
        text = text.replace(mark, "")
    table = str.maketrans({
        "\u064a": "\u06cc",  # arabic yeh -> persian yeh
        "\u0643": "\u06a9",  # arabic kaf -> persian kaf
        **{chr(0x06F0 + i): str(i) for i in range(10)},
        **{chr(0x0660 + i): str(i) for i in range(10)},
    })
    text = text.translate(table)
    return " ".join(text.split())
