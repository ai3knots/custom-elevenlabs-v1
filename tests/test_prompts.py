"""Unit tests for prompt rendering, variable substitution, and knowledge base token budget checking."""

import pytest

from src.config import DynamicVariables, KBConfig
from src.fsm.states import State
from src.prompts import KnowledgeBaseProvider, PromptManager, estimate_token_count


@pytest.fixture
def sample_variables() -> DynamicVariables:
    """Sample dynamic variables matching Posivex-lead-gen outbound payload."""
    return DynamicVariables(
        business_name="Tony's Pizza & Grill",
        business_context="Pizza restaurant in Atlanta, 4.5 stars",
        current_datetime="Tuesday, October 20, 2026 at 3:42 PM (America/New_York)",
        is_followup="true",
        followup_notes="Requested rate comparison quote",
        contact_name="Tony",
    )


def test_first_message_generation(sample_variables: DynamicVariables):
    """Verify first message renders correctly for Amelia/Michael in English and Spanish."""
    # Amelia English
    pm_amelia_en = PromptManager(persona="amelia", language="en")
    msg1 = pm_amelia_en.get_first_message(sample_variables)
    assert msg1 == "Hi, this is Amelia from Clover Posivex. Am I speaking with the owner of Tony's Pizza & Grill?"

    # Amelia Spanish
    pm_amelia_es = PromptManager(persona="amelia", language="es")
    msg2 = pm_amelia_es.get_first_message(sample_variables)
    assert msg2 == "Hola, soy Amelia de Clover Posivex. ¿Hablo con el propietario de Tony's Pizza & Grill?"

    # Michael English
    pm_michael_en = PromptManager(persona="michael", language="en")
    msg3 = pm_michael_en.get_first_message(sample_variables)
    assert msg3 == "Hi, this is Michael from Clover Posivex. Am I speaking with the owner of Tony's Pizza & Grill?"

    # Michael Spanish
    pm_michael_es = PromptManager(persona="michael", language="es")
    msg4 = pm_michael_es.get_first_message(sample_variables)
    assert msg4 == "Hola, soy Michael de Clover Posivex. ¿Hablo con el propietario de Tony's Pizza & Grill?"


def test_render_base_prompt_variable_substitution(sample_variables: DynamicVariables):
    """Verify that Jinja variables are cleanly interpolated in the global persona prompt."""
    pm = PromptManager(persona="amelia", language="en")
    base_prompt = pm.render_base_prompt(sample_variables)

    assert "You are Amelia" in base_prompt
    assert "Business: Tony's Pizza & Grill" in base_prompt
    assert "Background: Pizza restaurant in Atlanta" in base_prompt
    assert "Current date and time: Tuesday, October 20, 2026" in base_prompt
    assert "Follow-up: true" in base_prompt
    assert "Contact name: Tony" in base_prompt
    assert "# Language" in base_prompt
    assert "{{" not in base_prompt
    assert "0.10%" in base_prompt


def test_state_prompt_composition(sample_variables: DynamicVariables):
    """Verify layered prompt composition for each FSM state."""
    pm = PromptManager(persona="amelia", language="en")

    # Opener
    p_opener = pm.render_prompt(State.OPENER, sample_variables)
    assert "You are Amelia" in p_opener
    assert "Node 1: Opener & Route" in p_opener
    assert "Knowledge Base" not in p_opener

    # Reach Owner
    p_reach = pm.render_prompt(State.REACH_OWNER, sample_variables)
    assert "Node 2: Reach the Owner" in p_reach
    assert "four details" in p_reach

    # Conversation (includes inline KB)
    p_conv = pm.render_prompt(State.CONVERSATION, sample_variables)
    assert "Node 3: Conversation" in p_conv
    assert "# The five beats" in p_conv
    assert 'transition("lock_in' in p_conv
    assert "# Knowledge Base" in p_conv
    assert "Clover Posivex Knowledge Base" in p_conv

    # Lock In
    p_lock = pm.render_prompt(State.LOCK_IN, sample_variables)
    assert "Node 4: Lock In Next Step" in p_lock
    assert 'transition("conversation"' in p_lock
    assert "Perfect, you're all set." in p_lock

    # Close
    p_close = pm.render_prompt(State.CLOSE, sample_variables)
    assert "Node 5: Close" in p_close

    # DNC Close & End produce empty strings (canned speech / terminal)
    assert pm.render_prompt(State.DNC_CLOSE, sample_variables) == ""
    assert pm.render_prompt(State.END, sample_variables) == ""


def test_knowledge_base_inline_token_budget():
    """Verify KnowledgeBaseProvider loads inline KB and enforces token budget."""
    cfg = KBConfig(
        mode="inline",
        file_path="prompts/knowledge/Knowledge-base.md",
        max_token_budget=4000,
    )
    provider = KnowledgeBaseProvider(cfg)
    content = provider.load_kb()

    assert len(content) > 5000
    assert "Clover Posivex Knowledge Base" in content
    token_count = provider.get_token_count()
    assert 1500 < token_count < 3500


def test_knowledge_base_token_budget_exceeded():
    """Verify ValueError is raised if the KB exceeds configured max_token_budget."""
    cfg = KBConfig(
        mode="inline",
        file_path="prompts/knowledge/Knowledge-base.md",
        max_token_budget=100,  # Intentionally tiny budget
    )
    provider = KnowledgeBaseProvider(cfg)
    with pytest.raises(ValueError, match="exceeds maximum configured token budget"):
        provider.load_kb()


def test_knowledge_base_modes():
    """Verify off and rag modes return empty string without error."""
    # Off mode
    cfg_off = KBConfig(mode="off")
    p_off = KnowledgeBaseProvider(cfg_off)
    assert p_off.load_kb() == ""

    # RAG mode
    cfg_rag = KBConfig(mode="rag")
    p_rag = KnowledgeBaseProvider(cfg_rag)
    assert p_rag.load_kb() == ""


def test_estimate_token_count_utility():
    """Verify token estimation calculation."""
    assert estimate_token_count("") == 0
    # 10 words * 1.33 = 13 tokens
    text = "one two three four five six seven eight nine ten"
    assert estimate_token_count(text) == 13



def test_node_prompts_use_tools_not_elevenlabs_workflow(sample_variables: DynamicVariables):
    """Ported prompts reference our tools and leave no ElevenLabs-only wording or raw template tags."""
    for persona in ("amelia", "michael"):
        pm = PromptManager(persona=persona, language="en")
        for state in (State.OPENER, State.REACH_OWNER, State.CONVERSATION, State.LOCK_IN, State.CLOSE):
            prompt = pm.render_prompt(state, sample_variables)
            assert "{{" not in prompt and "{%" not in prompt
            assert "voicemail detection" not in prompt
            assert "voicemail_detection" not in prompt
            assert "The workflow" not in prompt
    opener = PromptManager(persona="amelia").render_prompt(State.OPENER, sample_variables)
    assert 'set_call_outcome("voicemail"' in opener
    assert 'transition("end"' in opener
    assert "A business greeting is not an answer" in opener
    assert "Michael" in PromptManager(persona="michael").render_prompt(State.OPENER, sample_variables)


def test_missing_knowledge_base_file_degrades_to_empty():
    cfg = KBConfig(mode="inline", file_path="does/not/exist.md")
    assert KnowledgeBaseProvider(cfg).load_kb() == ""


def test_default_knowledge_base_ships_in_a_docker_copied_path():
    """The KB must live outside reference/ (excluded by .dockerignore) so it is in the image."""
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    default_path = KBConfig().file_path
    assert not default_path.startswith("reference/")
    assert (repo / default_path).exists()
    dockerignore = (repo / ".dockerignore").read_text(encoding="utf-8").splitlines()
    ignored_roots = {line.strip().rstrip("/") for line in dockerignore if line.strip()}
    assert default_path.split("/")[0] not in ignored_roots


def test_opener_prompt_does_not_treat_a_recording_notice_as_a_machine(sample_variables: DynamicVariables):
    opener = PromptManager(persona="amelia").render_prompt(State.OPENER, sample_variables)
    assert "A recording notice is NOT a machine" in opener
    assert "Google Voice or carrier screening message" not in opener


def test_wrap_up_steps_transition_in_the_same_turn(sample_variables: DynamicVariables):
    pm = PromptManager(persona="amelia")
    for state in (State.REACH_OWNER, State.LOCK_IN):
        prompt = pm.render_prompt(state, sample_variables)
        assert "in that same turn" in prompt
        assert "after their reply" not in prompt
