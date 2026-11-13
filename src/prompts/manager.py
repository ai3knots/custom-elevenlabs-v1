"""PromptManager: renders the single system prompt (Jinja2) with the inline knowledge base."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import DynamicVariables, KBConfig
from src.prompts.kb import KnowledgeBaseProvider

logger = logging.getLogger("posivex.prompts.manager")

# The scripted opening line, spoken verbatim after pickup.
FIRST_MESSAGE_TEMPLATE = "Hi, this is {name} from Clover Posivex. {question}"


class PromptManager:
    """Loads the persona's system prompt, substitutes dynamic variables and appends the knowledge base."""

    def __init__(
        self,
        persona: Literal["amelia", "michael"] = "amelia",
        kb_config: KBConfig | None = None,
        prompts_dir: Path | None = None,
    ) -> None:
        self.persona = persona.lower()
        self.prompts_dir = prompts_dir or (Path(__file__).resolve().parent.parent.parent / "prompts")

        self.jinja_env = Environment(
            loader=FileSystemLoader(self.prompts_dir),
            autoescape=select_autoescape(disabled_extensions=("j2", "md", "txt")),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        self.kb_provider = KnowledgeBaseProvider(kb_config or KBConfig())

    @property
    def display_name(self) -> str:
        return "Michael" if self.persona == "michael" else "Amelia"

    @staticmethod
    def opening_question(variables: DynamicVariables) -> str:
        """The question that follows the greeting.

        Lead-gen writes it: a callback opens with what was agreed, anything else gets the original question. A caller
        that sends none (an older client, a manual API call) gets the original question too.
        """
        biz_name = variables.business_name or "the business"
        return (variables.opening_question or "").strip() or f"Am I speaking with the owner of {biz_name}?"

    def get_first_message(self, variables: DynamicVariables) -> str:
        """The scripted opening line (English only)."""
        return FIRST_MESSAGE_TEMPLATE.format(name=self.display_name, question=self.opening_question(variables))

    def render_system_prompt(self, variables: DynamicVariables) -> str:
        """Render the persona's system prompt with the dynamic variables."""
        template = self.jinja_env.get_template(f"{self.persona}/system.j2")
        # The prompt quotes the opener the caller already heard, so it must use the same resolved question.
        context = {**variables.model_dump(), "opening_question": self.opening_question(variables)}
        return template.render(**context)

    def render_prompt(self, variables: DynamicVariables) -> str:
        """Complete instructions for the agent: system prompt followed by the knowledge base."""
        sections = [self.render_system_prompt(variables)]
        try:
            kb_text = self.kb_provider.load_kb()
        except Exception:
            # Never let a knowledge-base problem (unreadable file, token budget) break the call.
            logger.exception("Could not load the knowledge base; continuing without it.")
            kb_text = ""
        if kb_text:
            sections.append(f"# Knowledge Base\n\n{kb_text}")
        return "\n\n".join(sections)
