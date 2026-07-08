"""Structure-aware chunking.

Splits parsed blocks into overlapping chunks while preserving citation
metadata (source_path, page, char offsets). Token counts are approximated
by whitespace words scaled to tokens (~1.3 tokens/word) to avoid a hard
tokenizer dependency; good enough for chunk sizing.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .parsers import Block

WORDS_PER_TOKEN = 0.75  # ~1.33 tokens per word


@dataclass
class Chunk:
    id: str
    source_path: str
    page: int
    char_start: int
    char_end: int
    text: str
    metadata: dict = field(default_factory=dict)


def _target_words(chunk_tokens: int) -> int:
    return max(60, int(chunk_tokens * WORDS_PER_TOKEN))


def _split_paragraphs(text: str) -> List[str]:
    # Prefer heading/blank-line boundaries.
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def chunk_blocks(path: Path, blocks: List[Block], chunk_tokens: int = 512,
                 overlap_tokens: int = 64) -> List[Chunk]:
    target = _target_words(chunk_tokens)
    overlap = _target_words(overlap_tokens)
    chunks: List[Chunk] = []
    file_hash = hashlib.sha256(str(path).encode()).hexdigest()[:12]

    for page, text in blocks:
        offset = 0
        for para in _split_paragraphs(text):
            words = para.split()
            i = 0
            while i < len(words):
                window = words[i:i + target]
                sub = " ".join(window)
                start = text.find(window[0], offset) if window else offset
                start = start if start >= 0 else offset
                end = start + len(sub)
                cid = f"{file_hash}-p{page}-{len(chunks)}"
                chunks.append(Chunk(
                    id=cid, source_path=str(path), page=page,
                    char_start=start, char_end=end, text=sub,
                    metadata={"filename": path.name},
                ))
                offset = end
                if i + target >= len(words):
                    break
                i += max(1, target - overlap)
    return chunks
