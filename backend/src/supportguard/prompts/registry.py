from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

PROMPT_ROOT = Path(__file__).parent


@dataclass(frozen=True)
class PromptAsset:
    prompt_id: str
    version: str
    content: str
    content_hash: str


def load_prompt(prompt_id: str, version: str = "v1") -> PromptAsset:
    path = PROMPT_ROOT / f"{prompt_id}.{version}.md"
    content = path.read_text(encoding="utf-8")
    return PromptAsset(
        prompt_id=prompt_id,
        version=version,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
