"""Unit tests for prompt rendering, variable substitution, and knowledge base token budget checking."""

import pytest

from src.config import DynamicVariables, KBConfig
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
    """The scripted opener names the persona and the business (English only)."""
    amelia = PromptManager(persona="amelia").get_first_message(sample_variables)
    assert amelia == "Hi, this is Amelia from Clover Posivex. Am I speaking with the owner of Tony's Pizza & Grill?"
    michael = PromptManager(persona="michael").get_first_message(sample_variables)
    assert michael == "Hi, this is Michael from Clover Posivex. Am I speaking with the owner of Tony's Pizza & Grill?"


def test_system_prompt_variable_substitution(sample_variables: DynamicVariables):
    """Jinja variables are interpolated in the single system prompt."""
    prompt = PromptManager(persona="amelia").render_system_prompt(sample_variables)

    assert "You are Amelia" in prompt
    assert "Business: Tony's Pizza & Grill" in prompt
    assert "Background: Pizza restaurant in Atlanta" in prompt
    assert "Current date and time: Tuesday, October 20, 2026" in prompt
    assert "Follow-up: true" in prompt
    assert "Contact name: Tony" in prompt
    assert "{{" not in prompt and "{%" not in prompt
    assert "0.10%" in prompt


def test_single_prompt_contains_every_step_and_the_three_tools(sample_variables: DynamicVariables):
    """The whole call flow lives in one prompt, followed by the knowledge base."""
    prompt = PromptManager(persona="amelia").render_prompt(sample_variables)

    for step in ("Step A: Voicemail check", "Step B: Who is on the line", "Step C: Staff, not the owner",
                 "Step D: The owner conversation (five beats)", "Step E: Lock in the next step",
                 "Step F: Closing the call"):
        assert step in prompt
    for tool in ("voicemail_detection", "skip_turn", "end_call"):
        assert tool in prompt
    assert "transition(" not in prompt
    assert "set_call_outcome" not in prompt
    assert "# Knowledge Base" in prompt
    assert "Clover Posivex Knowledge Base" in prompt
    assert prompt.index("# Knowledge Base") > prompt.index("# Tools")


def test_michael_prompt_differs_from_amelia_by_name_only(sample_variables: DynamicVariables):
    amelia = PromptManager(persona="amelia").render_prompt(sample_variables)
    michael = PromptManager(persona="michael").render_prompt(sample_variables)
    assert "Amelia" not in michael
    assert michael == amelia.replace("Amelia", "Michael")


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



def test_missing_knowledge_base_file_degrades_to_empty():
    cfg = KBConfig(mode="inline", file_path="does/not/exist.md")
    assert KnowledgeBaseProvider(cfg).load_kb() == ""


def test_default_knowledge_base_ships_in_a_docker_copied_path():
    """The KB must live under prompts/ so it is in the image."""
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    default_path = KBConfig().file_path
    assert default_path.startswith("prompts/")
    assert (repo / default_path).exists()
    dockerignore = (repo / ".dockerignore").read_text(encoding="utf-8").splitlines()
    ignored_roots = {line.strip().rstrip("/") for line in dockerignore if line.strip()}
    assert default_path.split("/")[0] not in ignored_roots


def test_missing_knowledge_base_does_not_break_the_prompt(sample_variables: DynamicVariables):
    pm = PromptManager(persona="amelia", kb_config=KBConfig(mode="inline", file_path="does/not/exist.md"))
    prompt = pm.render_prompt(sample_variables)
    assert "You are Amelia" in prompt
    assert "# Knowledge Base" not in prompt


def test_callback_first_message_uses_the_question_lead_gen_wrote():
    """A callback opens with what was agreed; a blank question falls back to the original opener."""
    callback = DynamicVariables(
        business_name="Tony's Pizza & Grill",
        is_callback="true",
        opening_question="Someone there asked me to call back around one to speak with Mark. Is Mark available?",
    )
    assert PromptManager(persona="amelia").get_first_message(callback) == (
        "Hi, this is Amelia from Clover Posivex. Someone there asked me to call back around one to speak with Mark. Is Mark available?"
    )
    blank = DynamicVariables(business_name="Tony's Pizza & Grill", opening_question="   ")
    assert PromptManager(persona="michael").get_first_message(blank) == (
        "Hi, this is Michael from Clover Posivex. Am I speaking with the owner of Tony's Pizza & Grill?"
    )


def test_prompt_is_statement_first_and_carries_the_callback_line():
    variables = DynamicVariables(
        business_name="Tony's Pizza & Grill",
        is_callback="true",
        callback_confirm_line="Thanks for taking my call. You asked me to call back at this time. Do you have a couple of minutes?",
        opening_question="I'm calling back as arranged. Am I speaking with Tony?",
    )
    prompt = PromptManager(persona="amelia").render_prompt(variables)
    assert "Thanks for taking my call. You asked me to call back at this time." in prompt
    assert "Beat 5, ask for the statement." in prompt
    assert "Beat 5, ask for the demo" not in prompt
    assert "suportposivex@gmail.com" in prompt
    assert "qualify for our wholesale pricing tiers" not in prompt
    assert "{{" not in prompt


def test_prompt_quotes_the_same_opener_that_is_spoken_when_no_question_is_sent():
    """A blank question must not leave the prompt claiming the opener ended after "Clover Posivex."."""
    variables = DynamicVariables(business_name="Tony's Pizza & Grill")
    for persona in ("amelia", "michael"):
        manager = PromptManager(persona=persona)
        spoken = manager.get_first_message(variables)
        assert spoken.endswith("Am I speaking with the owner of Tony's Pizza & Grill?")
        assert f'"{spoken}"' in manager.render_system_prompt(variables)


@pytest.mark.parametrize("persona", ["amelia", "michael"])
def test_the_prompt_promises_no_transfer_unless_the_agent_has_the_transfer_tool(persona: str) -> None:
    """Off: the agent cannot transfer and says so. On: it transfers, only when the owner asks, with the tool."""
    variables = DynamicVariables(business_name="Tony's Pizza & Grill")
    manager = PromptManager(persona=persona)  # type: ignore[arg-type]

    off = manager.render_system_prompt(variables)
    assert "You cannot transfer or connect a call." in off
    assert "transfer_call" not in off and "{%" not in off

    on = manager.render_system_prompt(variables, transfer_enabled=True)
    assert "You cannot transfer or connect a call." not in on
    assert "transfer_call" in on and "{%" not in on
    assert "only when the owner or the person who makes the decision asks to talk to a person" in on
    assert "never transfer staff, a machine or a menu" in on
    assert "two two nine, two zero two, eight five zero zero" in on  # the team line stays the fallback
    # The personas are kept in step: the same transfer wording in both.
    other = "michael" if persona == "amelia" else "amelia"
    other_on = PromptManager(persona=other).render_system_prompt(variables, transfer_enabled=True)  # type: ignore[arg-type]
    assert [line for line in on.splitlines() if "transfer_call" in line] == [
        line for line in other_on.splitlines() if "transfer_call" in line
    ]


@pytest.mark.parametrize("persona", ["amelia", "michael"])
def test_the_transfer_rules_carry_the_trigger_and_the_exclusions_of_the_live_elevenlabs_agent(persona: str) -> None:
    """The live ElevenLabs agent decides a transfer with an 'asks for a human' condition; those words are now the tool's rules."""
    on = PromptManager(persona=persona).render_system_prompt(  # type: ignore[arg-type]
        DynamicVariables(business_name="Tony's Pizza & Grill"), transfer_enabled=True
    )
    for phrase in (
        "no AI, someone personal",  # a request, in the words callers use
        "put me through to someone",
        "Use it even when you have just answered an AI question",
        "when they only ask whether you are an AI or a real person",  # not a request: answer it first
        "offers to get their own owner or manager",  # staff fetching their own boss is not a request
        "press two",  # a menu offering a representative is a machine, not the prospect
        "Do not speak before the tool",  # the tool says the hand-over line itself
        "never try to end the call; the transfer ends it",
        "If staff ask for a person, give the team line instead",
    ):
        assert phrase in on, phrase
