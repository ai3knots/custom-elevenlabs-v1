# Clover Posivex voice agents v5: workflow version (Amelia and Michael)

A thin ElevenLabs workflow around the v4 agents you already like. Five nodes, with one adaptive conversation node that stays prompt-driven. v4 keeps running. v5 is built as separate agents and compared against it.

## Files

| Path | What it is |
|---|---|
| [Workflow-spec.md](Workflow-spec.md) | The graph, node settings, entry behavior, tools per node, and every edge in order |
| [Node-1-Opener-and-Route.md](Node-1-Opener-and-Route.md) | Additional prompt for node 1 |
| [Node-2-Reach-the-Owner.md](Node-2-Reach-the-Owner.md) | Additional prompt for node 2 |
| [Node-3-Conversation.md](Node-3-Conversation.md) | Additional prompt for node 3 (discovery, pitch, objections) |
| [Node-4-Lock-in-Next-Step.md](Node-4-Lock-in-Next-Step.md) | Additional prompt for node 4 |
| [Test-plan.md](Test-plan.md) | Scenarios, workflow-specific checks and the A/B comparison with v4 |
| [../agent-v5-Amelia/](../agent-v5-Amelia/) | Amelia's `Global-prompt.md`, `First-message.md`, `Knowledge-base.md` |
| [../agent-v5-Michael/](../agent-v5-Michael/) | Michael's `Global-prompt.md`, `First-message.md`, `Knowledge-base.md` |

Node 5 (Close) and the Do-Not-Call node are Say nodes. Their text is in `Workflow-spec.md`.

## How it is split

- **Global prompt (persona-specific):** personality, voice rules, goal, guardrails, a compact pricing reference, and, for Michael, his extra talking points and playbooks.
- **Node prompts (shared, persona-neutral):** only that phase's instructions. They are appended to the global prompt.
- **Knowledge base (persona-specific):** full facts, hardware specs and recommendations. Attached to node 3 only. Core prices are also inline in the global prompt so a missed retrieval can never change a quote.
- **Tools:** `end_call` is on **no** conversation node. Calls end only through an edge to an End node. `skip_turn` is on every conversation node. `voicemail_detection` is on nodes 1 and 2 only.

Approximate prompt size per turn (characters divided by four, so rough): Amelia about 2,000 tokens in nodes 1, 2 and 4 and about 3,250 in node 3. Michael about 2,900 and 4,150. v4 was about 4,200 (Amelia) and 5,800 (Michael) on every turn. The ElevenLabs guide says prompts over about 2,000 tokens increase latency and cost. Michael's global prompt carries his extra playbooks, so it stays above that line. If latency matters, move his playbooks into node 3, which can be done when you want it.

## Build checklist (dashboard)

Do one persona first. Amelia is simpler.

1. **Create a new agent** named "Amelia v5". Don't touch the v4 agent.
2. **Global system prompt:** paste `agent-v5-Amelia/Global-prompt.md`.
3. **First message:** paste `agent-v5-Amelia/First-message.md`.
4. **Dynamic variables:** `business_name`, `business_context`, `current_datetime`, `is_followup`, `followup_notes`, `contact_name`. Set a default for each, as in v4.
5. **Voice, LLM base and voice settings:** copy from the v4 agent. Keep the same voice.
6. **Knowledge base:** upload `agent-v5-Amelia/Knowledge-base.md`. Make sure it is **not** included globally. Include it in node 3 only.
7. **Global tools:** `skip_turn` and `voicemail_detection`. (Per node, enable only what `Workflow-spec.md` says.)
8. **Workflow tab:** create nodes 1 to 4 as subagents, node 5 and the DNC node as Say nodes, and an End node. Paste each node's additional prompt. Set entry behavior, LLM and turn settings per the spec table.
9. **Edges:** add them **in the order listed** in the spec. The first edge on every conversation node is the Do-Not-Call edge.
10. **Post-call analysis:** copy the data collection and evaluation criteria from the v4 agent. The app depends on the `call_outcome` field, so it must be identical.
11. **Pull the JSON** with `elevenlabs agents pull --agent "Amelia v5"` and commit it. Michael can then be created by cloning, changing the global prompt, first message, voice and knowledge base.

## Troubleshooting from the first test call

The first Amelia v5 test call (Tony's Pizza & Grill) failed with "SAY node generated a response after progressing... no outgoing edges". The analysis:

| Problem | Cause | Fix |
|---|---|---|
| Call marked failed, dead end | The Close Say node had no unconditional edge to End | Add the edge. See "Build check: no dead ends" in [Workflow-spec.md](Workflow-spec.md). Do the same for the DNC Say node. |
| The prospect said "Okay, sure, why not" to the demo and the agent closed with no name, email or time | The flow went from node 3 straight to Close and skipped node 4. Most likely the edge to node 4 was missing or below the farewell edge, or the farewell condition was too loose. | Edge order and conditions were tightened in the spec: node 4 first, and a strict farewell condition. Check your edges. |
| The agent never confirmed it was talking to the owner | The reply "Hello, how can I help you?" counted as confirmation, so the flow jumped to equipment questions | The node 1 to 3 condition and the node 1 prompt now say a business greeting is not an answer |
| Silence after "Mm-hmm", then "Hello? Are you there?" | A short sound after a check question was not treated as a yes, so the agent waited | Node 3 now treats "mm-hmm", "okay", "sure" as yes. Also check the node 3 turn timeout and eagerness (see below). |
| The agent repeated beat 1 almost word for word | The connection sounded bad and the agent restated the whole beat | Node 3 now confirms it's there in a few words and continues |
| Transcript shows "[Warmly]", "[Patiently]" | Unknown: possibly added by the voice model | Listen to the recording. If the tags are not spoken, ignore them. If they are spoken, check the voice model and whether an expressive mode is on. |

Settings to check on node 3: turn timeout about 5 to 6 seconds (the spec said 8) so a "Mm-hmm" followed by silence is answered sooner, and compare the turn timestamps in the transcript to see how long the agent stayed silent.

## Live agent fixes (applied through the API)

The fixes below were tested on a temporary copy (since deleted) and then applied to the live agent. A full backup of the live config from just before the change is in `live-agent-backup-before-fixes.json` in this folder. Items 1 to 6 were applied. Items 7 and the node 3 turn timeout are not changed.

1. **Close and Do-Not-Call Say nodes have no edge to End.** Calls still fail with the dead-end warning. Add the unconditional edges.
2. **Edge order and conditions.** Node 3 still has a farewell edge that ends calls on "Not interested", and its next-step edge is last. Use the edges in [Workflow-spec.md](Workflow-spec.md). Add the backward condition on the 3 to 4 edge. Put Do-Not-Call first on nodes 1 and 2.
3. **Paste the current prompts.** The live nodes 1 and 3 still have the older prompts. Also paste the updated Node 2, Node 4, Close message and both global prompts.
4. **Turn off `end_call` and the keypad tool**, and set nodes 1, 2 and 4 to `gemini-3.5-flash`.
5. **Add the missing variable defaults** `followup_notes` and `contact_name`.
6. **Turn-taking:** node 3 turn timeout is still inherited (7 s).
7. **Tags such as [Warmly] and [Patiently]** come from Expressive mode on the `eleven_v3_conversational` voice model. They are not a bug in the prompts.

## Rollout

1. Run the [Test-plan.md](Test-plan.md) scenarios, workflow checks first.
2. Run Amelia v5 on a small separate batch (same lead types and hours as a v4 batch) for a fair comparison.
3. Compare against v4: reached an owner, next steps secured, calls ending within 10 seconds, premature endings, time to first response. Use the node analytics to see where calls drop.
4. Only when v5 matches or beats v4, build Michael v5 and repeat.
5. Keep v4 as the fallback. Rolling back is switching the campaign's agent.

## Still open

1. **Pricing is now the same for both personas** (resolved). Both follow the sales manual: credit is interchange plus as low as 0.10%, debit is interchange plus 5 cents, and the surcharge program is 3%. NRS pricing ($150 per month, $3,500 buyout) applies to any NRS model.
2. **Per-node turn settings** are starting values. Tune them from test calls.
3. **Models per node** are starting points from the ElevenLabs guide. Benchmark them.
4. **Voicemail and silent pickup** still hang up with no words, as in v4.
5. **Inbound** is separate. The inbound agents in `agent-v4-*-Inbound` are not part of this workflow, and still say "Posivex" and "Consultant call".
6. **The demo ask** (resolved). The pitch asks for a ten-minute live demo over Zoom or Google Meet. The Consultant calls and meets the lead whenever the lead prefers, so there are no fixed Consultant hours in the prompts. A booked demo still maps to the `consultant_call_scheduled` outcome, so the app needs no change.
7. **Still unpriced:** the other Clover models (Station Solo, Flex Pocket, Compact, Go, Kiosk) and NRS add-ons. The agent has the Consultant confirm these.
