# Test plan: workflow agents

Test Amelia v5 first. Run the workflow checks (W) before the scenarios. Change one thing at a time and re-run the affected tests.

## W: workflow mechanics

| # | Check | Pass if |
|---|---|---|
| W1 | Call as an owner ("Yes, I'm the owner") | Flow goes node 1 to node 3 and the agent asks one easy setup question. No double greeting. |
| W2 | Call as an employee ("I just work here") | Flow goes to node 2 and the agent asks for the owner. |
| W3 | Employee says "hold on", then owner picks up | Agent stays silent while waiting, re-introduces in one sentence, confirms the owner, and moves to node 3. |
| W4 | Employee gives name, time and number | Flow goes to node 5 and the close recaps only that. |
| W5 | Owner agrees to an email comparison | Flow goes 3 to 4, node 4 asks for the email, reads it back, and node 5 closes. |
| W6 | In node 4, change your mind ("actually, what does it cost?") | Flow goes back to node 3, which answers the price question first and does not restart the pitch. |
| W7 | Say "don't call again" in **each** node (1, 2, 3, 4) | The DNC edge fires first every time, the Say node speaks the literal line, and the call ends. |
| W8 | Decline an offer ("not interested") in node 3 | The agent stays in node 3, does one recovery move, and does **not** go to node 5. |
| W9 | Say "okay, bye" in node 3 | Flow goes to node 5. |
| W10 | Reach a voicemail greeting at the start | Agent says nothing and uses voicemail detection. |
| W11 | Voicemail greeting after "hold on" | Same, in node 2. |
| W12 | Silence after the first message | One "Hello?" and then nothing, and the flow reaches End. |
| W13 | Check the context carries across nodes | Node 3 does not re-ask what was already said in node 1 or 2. |
| W14 | Check no node can end the call itself | `end_call` is not enabled on nodes 1 to 4, and an objection never ends the call. |
| W15 | Check edge order in the JSON | Every conversation node's `edge_order` 0 is the DNC edge. Node 3 has only two edges: DNC first, then node 4. There is no farewell edge from node 3. |
| W16 | Run the call to the end, and check the conversation status | The conversation ends normally, with no "dead end" warning. The Close and DNC Say nodes each have an unconditional edge to End. |
| W17 | Owner says a casual yes to the demo ("okay", "sure, why not") | Flow goes 3 to 4. The agent asks for the email, Zoom or Meet, number and a day and time. The Close node does **not** run straight from node 3. |
| W18 | Answer the opener with a business greeting ("Hello, how can I help you?") | The agent says why it's calling and asks the role question. It does not ask about their equipment yet. |
| W19 | Answer the beat-1 check with "Mm-hmm" | The agent moves straight to beat 2. No silence and no repeated beat 1. |
| W20 | Say "Hello? Are you there?" mid-pitch | The agent confirms it's there in a few words and continues. It does not repeat the beat. |

## S: scenarios

For each, "pass" also requires no invented fact, no guaranteed savings, and short natural turns.

| # | Scenario | Prospect says | Pass if the agent |
|---|---|---|---|
| 1 | Busy | "I'm in the middle of lunch rush." | Respects it, offers two times, gets a callback time or offers an email. No bare goodbye. |
| 2 | Not interested, early | "Not interested." | One easy question, no argument, no repeated pitch. |
| 3 | Not interested, late | "No thanks" after the pitch | Clarifies once, steps down to the rate-sheet email, then lets them lead. |
| 4 | Declined twice | Declines the recovery, then the lighter ask | Stops selling, thanks them, answers questions, lets them end it. |
| 5 | Already have a provider | "We're happy with who we use." | Asks what they run, offers a no-cost comparison. |
| 6 | Just send info | "Just email me something." | Says yes, one tailoring question, gets and confirms the email. |
| 7 | Keep machine | "Can I keep my terminal?" | Explains plainly that we always provide the equipment. |
| 8 | Contract | "I'm locked into a contract." | Validates, defers specifics to the Consultant, no invented terms. |
| 9 | Price | "How much does it cost?" | Asks "processing or equipment?", then gives the exact numbers for that persona. |
| 10 | Lease total | "What does that add up to?" | Does the multiplication honestly and mentions the buyout. |
| 11 | Discount | "Can you do better on the price?" | Sends it to the Consultant. Does not quote a floor rate. |
| 12 | Trust | "Is this a scam?" | Validates, Clover Posivex, Atlanta, posivex.com, one easy question. |
| 13 | AI | "Are you a robot?" | Honest line, continues. |
| 14 | Consultant now | "Can I talk to someone now?" | Says the Consultant isn't on this line, offers tomorrow morning. No transfer promise. |
| 15 | Demo booking | Agrees to the live demo | Confirms name, gets and confirms the email for the invite, asks Zoom or Google Meet, confirms the number, asks what day and time suits them (no fixed hours), agrees a specific day, time and time zone. |
| 15b | New pitch, two beats | Answers the setup question, then reacts after beat 1 | Beat 1 covers latest terminals, fast software, backend team and 24/7. Beat 2 comes in a separate turn with free setup and the ten-minute Zoom or Meet demo ask. Devices match the business type. No "free trial" or return promise. |
| 15c | After the demo | "What happens after the demo? Do I have to buy?" | Doesn't guess. Says our Consultant covers that. |
| 15d | No video | "I don't do video calls." | Offers a phone call with the Consultant instead. |
| 15e | Device fit | Restaurant, convenience store, food truck, payments-only shop | Recommends from the lineup: Station Duo or Flex plus KDS, NRS Puma or Cheetah, Clover Go or Flex or Flex Pocket, Clover Compact. Says the Consultant confirms prices for models it has no price for. |
| 16 | Hard to hear | "I can't hear you." | Short plain reply, offers an email or callback. |
| 17 | Language | Speaks only Spanish | Warm, asks about a better time, takes a number, closes politely. |
| 18 | Hostile | Swears at the agent | Calm, one apology, no argument. |
| 19 | Short words | "Yes." / "Okay." / "Mm-hmm." mid-pitch | Responds to what was asked or meant. Does not restart. |
| 20 | Follow-up | `is_followup` true with notes | Refers to the notes in one sentence and doesn't run a cold opener. |

**Michael only (extra):** "We're too busy to switch" (OPS answer), "What if something breaks?" (24/7 answer), "Too good to be true" (interchange explanation), "Let me think about it" (isolates the concern). And check his quoted debit rate is interchange plus five cents.

## A/B: v5 against v4

Run both on comparable batches (same lead types, same hours, similar size). Compare:

| Metric | Where to find it |
|---|---|
| Reached an owner or decision-maker | `reached_decision_maker` in data collection |
| Next step secured (Consultant call, email comparison, info email, callback) | `call_outcome` |
| Calls ending within 10 seconds | call duration |
| Premature endings after an objection | evaluation criteria and transcripts |
| Time to first response | per-turn timestamps in the transcript |
| Where calls drop | v5 node analytics (entries, terminations, edge distribution) |
| Cost per call | ElevenLabs charges |

Look at node analytics specifically for node 3 to node 5 (lost without an ask) and node 4 loops back to node 3 (hesitation during capture).

## Iterate

1. Run W1 to W15, then the scenarios.
2. Fix the biggest failure with the smallest change (one node prompt, one edge condition, or one setting).
3. Re-run the affected tests plus a few unrelated ones.
4. Repeat until v5 matches v4 on the comparison, then decide whether to switch.
