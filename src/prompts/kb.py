"""Knowledge base loader, token budget checker, and RAG interface abstraction."""

from __future__ import annotations

import logging
from pathlib import Path

from src.config import KBConfig

logger = logging.getLogger("posivex.prompts.kb")


def estimate_token_count(text: str) -> int:
    """Estimate token count using standard word count multiplier (~1.33 tokens/word)."""
    if not text:
        return 0
    words = text.split()
    return int(len(words) * 1.33)


class KnowledgeBaseProvider:
    """Provides knowledge base context to prompts according to configured mode (inline, rag, off)."""

    def __init__(self, config: KBConfig, base_dir: Path | None = None) -> None:
        self.config = config
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent.parent
        self._cached_content: str | None = None
        self._cached_token_count: int | None = None

    def load_kb(self) -> str:
        """Load and validate the knowledge base content."""
        if self.config.mode == "off":
            logger.info("Knowledge base mode is 'off'; returning empty content.")
            return ""

        if self.config.mode == "rag":
            logger.info("Knowledge base mode is 'rag'; returning RAG interface placeholder.")
            # Interface ready for vector retrieval in future phase
            return ""

        if self.config.mode == "inline":
            if self._cached_content is not None:
                return self._cached_content

            kb_path = Path(self.config.file_path)
            if not kb_path.is_absolute():
                kb_path = self.base_dir / kb_path

            if not kb_path.exists():
                # A missing file must never take down a live call: run without the knowledge base.
                logger.error(
                    "Knowledge base file not found at %s; continuing without a knowledge base.", kb_path
                )
                return ""

            with open(kb_path, encoding="utf-8") as f:
                content = f.read()

            token_count = estimate_token_count(content)
            self._cached_token_count = token_count

            logger.info(
                "Loaded inline knowledge base: %d characters, approx %d tokens (budget: %d)",
                len(content),
                token_count,
                self.config.max_token_budget,
            )

            if token_count > self.config.max_token_budget:
                raise ValueError(
                    f"Knowledge base token count (~{token_count} tokens) exceeds maximum configured "
                    f"token budget of {self.config.max_token_budget} tokens."
                )

            self._cached_content = content
            return self._cached_content

        raise ValueError(f"Unsupported knowledge base mode: '{self.config.mode}'")

    def get_token_count(self) -> int:
        """Return the estimated token count of the loaded knowledge base."""
        if self._cached_token_count is None:
            self.load_kb()
        return self._cached_token_count or 0

