# Workflow spec (shared by Amelia and Michael)

The graph, node settings, and edges to build in the ElevenLabs **Workflow** tab. Both personas use this same workflow. Only the global prompt, first message, voice and knowledge base differ.

## Confirmed behavior (from the ElevenLabs agent, to re-check in your first test)

| Question | Answer |
|---|---|
| Node prompt vs global prompt | A subagent node's `additional_prompt` is **appended** to the global system prompt. |
| Conversation history | The full history carries across node transitions in the same call. |
| Speaking on entry | Subagent nodes have `entry_behavior`: `generate_immediately`, `wait_for_user`, or `auto`. Say nodes speak their own message on entry. The global first message only starts the call. |
| Limits | No hard low limit on nodes and edges. **Each node can have only one unconditional outgoing edge.** |
| Edge evaluation | Conditional edges (llm, result, expression) are **evaluated in `edge_order`, sequentially**. |
| Turn settings per node | Subagent nodes support `conversation_config` overrides, including turn eagerness and timeouts. |
| Voicemail detection | A system tool. Tool availability can be toggled per node. |

Because edges run in order, **the first edge on every node must be the Do-Not-Call edge**, so a "stop calling me" is never swallowed by another condition.

## The graph

```
Start ──▶ [1 Opener & Route]
             │ edge: DNC ──────────────▶ [DNC Say] ──▶ [End]
             │ edge: no live human ────▶ [End]
             │ edge: employee ─────────▶ [2 Reach the Owner]
             │ edge: owner / handles it ▶ [3 Conversation]
             ▼
         [2 Reach the Owner]
             │ edge: DNC ──────────────▶ [DNC Say] ──▶ [End]
             │ edge: owner on the line ▶ [3 Conversation]
             │ edge: details / refusal ▶ [5 Close]
             ▼
         [3 Conversation]  ◀── loop back from 4
             │ edge: DNC ──────────────▶ [DNC Say] ──▶ [End]
             │ edge: goodbye / can't continue ▶ [5 Close]
             │ edge: agreed to a next step ───▶ [4 Lock In Next Step]
             ▼
         [4 Lock In Next Step]
             │ edge: DNC ──────────────▶ [DNC Say] ──▶ [End]
             │ edge: hesitates / question ────▶ [3 Conversation]
             │ edge: confirmed / declines ────▶ [5 Close]
             ▼
         [5 Close (Say, prompt mode)] ──(unconditional)──▶ [End]
```

`end_call` is not attached to any conversation node. A call can only end by reaching an End node through an edge. This is what makes a premature hang-up structurally impossible.

## Nodes

| # | Node | Type | Entry behavior | LLM (starting point) | Turn settings | Tools enabled |
|---|---|---|---|---|---|---|
| 1 | Opener & Route | Subagent | `wait_for_user` (the global first message already spoke) | Fast, cheap model | Normal eagerness, turn timeout about 6 s | `voicemail_detection`, `skip_turn` |
| 2 | Reach the Owner | Subagent | `generate_immediately` | Fast, cheap model | Normal eagerness, turn timeout about 8 s | `voicemail_detection`, `skip_turn` |
| 3 | Conversation | Subagent | `generate_immediately` | Stronger model | Normal eagerness, turn timeout about 5 to 6 s | `skip_turn` |
| 4 | Lock In Next Step | Subagent | `generate_immediately` | Mid model | **Patient** eagerness, turn timeout about 10 to 12 s | `skip_turn` |
| 5 | Close | Say node, **prompt mode** | n/a | Fast | n/a | none |
| DNC | Do-Not-Call | Say node, **literal** | n/a | n/a | n/a | none |
| End | End | End | n/a | n/a | n/a | n/a |

Notes:

- **Models.** The ElevenLabs prompting guide recommends GLM 5.2 or GPT-6 Luna as a balanced starting point, an ultra-low-latency model for triage and simple confirmations, and Claude Sonnet 5.5 for complex reasoning. These are starting points. Benchmark two or three candidates on your own prompts for latency, accuracy and tool-call success. Use the cheapest model that passes the test scenarios for nodes 1, 2, 4 and 5, and spend on node 3.
- **Knowledge base.** Include the persona's knowledge base in **node 3 only**. Node 4 needs nothing from it: the Consultant meets the lead whenever the lead prefers, so there are no hours to look up. Exact core prices are also in the global prompt so a missed retrieval can never change what the agent quotes.
- **Disable `end_call` and the keypad tool explicitly.** Creating an agent ignores `null` tool entries, so a copy keeps `end_call` on. Disable it with an update (`PATCH`) that sets `built_in_tools.end_call` to `null`. With `end_call` on, the agent ended calls itself on the second decline. With it off, calls end only through End nodes or when the caller hangs up. The live agent has `end_call` and the keypad tool on.
- **Model per node.** Tests were run with `gemini-3.5-flash` on nodes 1, 2, 3 and 4. The edge decisions are made by the node's model, and the lighter global model (`gemini-3.5-flash-lite`) misrouted more often.
- **Voicemail detection** is only on in nodes 1 and 2. A "please leave a message" greeting can only appear at the start or after someone goes to get the owner. Turning it off in 3 and 4 avoids false triggers mid-conversation.
- **Entry behavior on loops.** Node 3 uses `generate_immediately`. When the flow comes back from node 4 because the prospect asked something, node 3 must answer that question first. The node 3 prompt covers this.

## Edges (in `edge_order`)

Labels are display-only. The condition is what the LLM evaluates. Keep conditions broad. Narrow conditions recreate the rigidity of v1 and v2.

### From 1 Opener & Route

| Order | To | Condition |
|---|---|---|
| 0 | DNC Say | The person explicitly asks not to be contacted again (for example "don't call again", "take us off your list", "stop calling", "remove our number"). Not a request to call back later or at another time. |
| 1 | End | The call was answered by a machine, not a live person: a voicemail greeting, answering machine, recorded or carrier message, IVR menu, call screener, a beep or tone, or text such as "leave a message", "after the tone", "you've reached", "not available", "press 1". Also if the line stayed silent after one "Hello?" and about five seconds. |
| 2 | 2 Reach the Owner | The person says or shows they are an employee, receptionist or staff, say the owner is not available, or they are not the owner or the person who handles card processing. |
| 3 | 3 Conversation | The person has said yes to "are you the owner", or has explicitly said they are the owner or a manager, or that they handle the point-of-sale or card processing. Do NOT take this edge for a greeting or question such as "Hello", "Tony's", "how can I help you?", "who is this?", or for a "no". If it is unclear who they are, do not take any edge. |

### From 2 Reach the Owner

| Order | To | Condition |
|---|---|---|
| 0 | DNC Say | Same as above. |
| 1 | 3 Conversation | Someone new picked up and is the owner or manager, or the person says they make that decision or handle processing. Not a bare "Hello?". |
| 2 | 5 Close | The agent has said "Perfect, I've got everything noted for the owner" (its wrap-up line) and the employee has replied or the line went quiet. Or the person cannot continue in English and a better time was offered. Do NOT take this edge just because the employee gave a name, time or number, or because the agent has asked a question and is waiting for the answer. Never take it while a detail is still being collected or corrected. |

### From 3 Conversation

| Order | To | Condition |
|---|---|---|
| 0 | DNC Say | Same as above. |
| 1 | 4 Lock In Next Step | Take this edge only when ALL of these are true: (1) an earlier agent message explicitly offered a ten-minute live demo, an emailed rate comparison, an information email, or a callback; (2) the prospect has spoken AFTER that offer, and their latest message is a direct reply to it; (3) that reply agrees, including a casual yes ("okay", "sure, why not", "yeah, send it"), or picks a day or time. If the agent's offer is the most recent message in the conversation, meaning the prospect has not replied to it yet, do NOT take this edge, no matter what the prospect said earlier. An earlier "okay" or "mm-hmm" given to a different question does not count. Also take this edge if the prospect asks for a callback or says to call them later. Do NOT take it when the prospect only answers a general check question such as "does that sound useful?" or "is that something you'd want to look at?", or when they decline or ask a question. |

There is **no farewell edge from node 3**. In testing, a farewell edge fired on "Not interested" and ended calls on the first objection. Callers who say goodbye after a decline are handled by node 3 (it says a short goodbye), and the call ends when they hang up. If node 3 is routed to node 4 on an "okay, bye", node 4 acknowledges and the flow reaches Close.

### From 4 Lock In Next Step

Edge 1 is the **backward condition of the same edge as 3 to 4**. The workflow rejects a second edge between the same pair of nodes ("Duplicate edge found"), so the loop back to node 3 is set as the `backward_condition` of the 3 to 4 edge.

| Order | To | Condition |
|---|---|---|
| 0 | DNC Say | Same as above. |
| 1 | 3 Conversation (backward condition) | The prospect raises a new objection, asks a question that needs more than one sentence to answer (pricing, equipment, what they would be getting), or says they no longer want the next step. Do NOT take this edge for ambiguous sounds such as "hmm", "okay", "uh-huh", or for ordinary answers to the details being collected. |
| 2 | 5 Close | The agent has said "Perfect, you're all set" (its final confirmation) and the prospect has replied. Or the prospect clearly declines to give the details after a lighter step was offered, or says goodbye. Do not take this edge before the agent has said "Perfect, you're all set", unless the prospect declined or said goodbye. |

Node 4's prompt tells the agent to say "Perfect, you're all set" only once the step is fully confirmed, so the edge is anchored on the agent's own words. Without that anchor the classifier closed the call after only the email, skipping the read-back, platform, number and time.

### Unconditional edges

- 5 Close to End.
- DNC Say to End.

Each node has at most one unconditional edge, and these are the only ones.

**Every Say node must have this unconditional edge to an End node.** Without it the workflow reaches a dead end after the Say node speaks, and ElevenLabs marks the conversation as failed with the warning "The workflow reached a dead end... make sure to have one unconditional edge going out of Node". This happened on the first test call.

### Build check: no dead ends

Before testing, open the Workflow tab and confirm for **every** node:

- 5 Close has one unconditional edge to End.
- DNC Say has one unconditional edge to End.
- Node 3 has an edge to node 4 (agreed to a next step) **above** its edge to node 5 (farewell).
- Node 1 has an edge to node 3 and an edge to node 2.
- Every edge has a condition, except the two unconditional ones above.
- The conditional edges on each node are in the order listed above.

## Say nodes

**DNC Say (literal):**

```
Understood, my apologies. I'll make sure we don't call this number again. Take care.
```

**5 Close (prompt mode).** Description for the LLM:

```
Give one short, warm closing line and nothing else. Recap only what was actually confirmed in this call, if anything: a ten-minute live demo (Zoom or Google Meet) at a specific day and time that was agreed, with the invite coming by email, an email a team member will send right after the call, or a callback at a specific time. Never say or imply a demo or callback is booked if no day and time were agreed. If nothing was confirmed, thank them and leave the door open. Do not pitch. Do not ask a new question.
```

## Variables

No Update state node is needed in this version. The six dynamic variables come from the app, exactly as in v4: `business_name`, `business_context`, `current_datetime`, `is_followup`, `followup_notes`, `contact_name`. Set a default for each, as the ElevenLabs docs recommend.

## What to check in the node analytics

The Workflow tab shows entries, time in node, terminations and edge distribution. After a batch, look at:

- How many reach node 3 as a share of node 1 entries (reach rate).
- How many reach node 4 as a share of node 3 (conversion to an agreed step).
- How often 4 loops back to 3, and how often 3 goes straight to 5 (lost without an ask).
- DNC edge counts per node.
- Time in node 1. Long times there point to slow pickup handling.
