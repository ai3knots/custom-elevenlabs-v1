"""PromptManager rendering Jinja2 templates, state-specific node prompts, and inline KB."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import DynamicVariables, KBConfig
from src.fsm.states import STATE_REGISTRY, State
from src.prompts.kb import KnowledgeBaseProvider

logger = logging.getLogger("posivex.prompts.manager")


class PromptManager:
    """Manages prompt template loading, variable substitution, and dynamic state composition."""

    def __init__(
        self,
        persona: Literal["amelia", "michael"] = "amelia",
        language: Literal["en", "es"] = "en",
        kb_config: KBConfig | None = None,
        prompts_dir: Path | None = None,
    ) -> None:
        self.persona = persona.lower()
        self.language = language.lower()
        self.prompts_dir = prompts_dir or (Path(__file__).resolve().parent.parent.parent / "prompts")

        self.jinja_env = Environment(
            loader=FileSystemLoader(self.prompts_dir),
            autoescape=select_autoescape(disabled_extensions=("j2", "md", "txt")),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        self.kb_provider = KnowledgeBaseProvider(kb_config or KBConfig())
        self._cached_node_templates: dict[str, str] = {}

    def get_first_message(self, variables: DynamicVariables) -> str:
        """Generate the persona- and language-appropriate opening utterance."""
        display_name = "Michael" if self.persona == "michael" else "Amelia"
        biz_name = variables.business_name or "the business"

        if self.language == "es":
            return f"Hola, soy {display_name} de Clover Posivex. ¿Hablo con el propietario de {biz_name}?"
        return f"Hi, this is {display_name} from Clover Posivex. Am I speaking with the owner of {biz_name}?"

    def render_base_prompt(self, variables: DynamicVariables) -> str:
        """Render the global persona base prompt with dynamic variables."""
        template_name = f"{self.persona}/global.j2"
        template = self.jinja_env.get_template(template_name)
        context = variables.model_dump()
        return template.render(**context)

    def _load_and_render_fragment(self, fragment_rel_path: str, variables: DynamicVariables) -> str:
        """Load and render an individual node markdown prompt fragment."""
        template = self.jinja_env.get_template(fragment_rel_path)
        context = variables.model_dump()
        return template.render(**context)

    def render_prompt(self, state: State, variables: DynamicVariables) -> str:
        """Compose the complete prompt for the specified conversational state."""
        # Terminal states do not invoke LLM prompt generation
        if state in (State.DNC_CLOSE, State.END):
            return ""

        meta = STATE_REGISTRY[state]
        base_prompt = self.render_base_prompt(variables)

        # If the state has no additional node prompt fragment, return base prompt
        if not meta.prompt_fragment_file:
            return base_prompt

        node_prompt = self._load_and_render_fragment(meta.prompt_fragment_file, variables)
        sections = [base_prompt, node_prompt]

        # In CONVERSATION state, attach inline knowledge base if enabled
        if state == State.CONVERSATION:
            try:
                kb_text = self.kb_provider.load_kb()
            except Exception:
                # Never let a knowledge-base problem (unreadable file, token budget) break the call.
                logger.exception("Could not load the knowledge base; continuing without it.")
                kb_text = ""
            if kb_text:
                sections.append(f"# Knowledge Base\n\n{kb_text}")

        return "\n\n".join(sections)

