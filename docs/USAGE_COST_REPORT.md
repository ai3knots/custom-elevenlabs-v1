# Voice agent usage and cost report

**Written for:** leadership deciding whether to keep running calls on ElevenLabs or move to our own agent.
**Data:** every call in the lead-gen database from 29 Sep to 9 Nov 2026 (41.1 days, 18,288 calls, all on ElevenLabs).
**Prepared:** 9 Nov 2026. Figures in dollars (USD).

---

## 1. The short version

- **What ran.** 18,288 calls were dialed. 14,566 (80%) connected, for 5,711 minutes. The average connected call lasted 24 seconds and the median 16 seconds.
- **Most calls are not conversations.** Only 3,958 calls (22% of dials) reached a person. 5,082 (28%) hit voicemail. 733 calls (4.0%) produced a useful result (callback set, owner details captured, info email, consultant call booked, comparison email).
- **What it cost on ElevenLabs.** About $634 over the 41 days: $457 for call minutes plus $177 for LLM usage. That is $0.111 per connected minute, $0.035 per dial and $0.86 per useful result.
- **The cost is rising.** The LLM part grew from 11% to 42% of the bill as prompts got larger. The all-in rate went from $0.086 to $0.135 per minute (week of 28 Sep versus week of 2 Nov).
- **Our own agent would cost less at this volume.** Replaying the same calls through our stack comes to about **$330 a month against $463** on ElevenLabs, a saving of $132 a month (29%). Both exclude the phone carrier, about $21 a month on either system.
- **At the planned 20,000 dials a month the saving is about $187 a month (27%), about $2,244 a year.** The custom agent costs about $506 a month against $693 on ElevenLabs. See section 5.
- **It only pays off above about 2,910 dials a month.** Below that, ElevenLabs is cheaper because our setup has a $50 fixed LiveKit fee.
- **These are estimates.** The ElevenLabs numbers come from the database and their published rates. The custom-agent numbers are a simulation built on published prices and the real transcripts. Section 7 lists what is assumed and what could not be confirmed.

---

## 2. What ran on ElevenLabs

### Volume and funnel

| Measure | Value |
| :--- | ---: |
| Window | 29 Sep to 9 Nov 2026 (41.1 days) |
| Calls dialed | 18,288 |
| Calls that connected (billed) | 14,566 (79.7%) |
| Calls with no billed time | 3,722 (no charge from ElevenLabs; 2,743 of them never connected at all) |
| Connected minutes | 5,711 |
| Busiest day | 1,696 calls |
| Peak calls in flight at once | 31 (dial to hang-up, including ringing) |

### Where the calls ended

| Result | Calls | Share of dials | Minutes | ElevenLabs cost |
| :--- | ---: | ---: | ---: | ---: |
| Spoke with someone | 3,958 | 22% | 2,626 | $301 |
| Voicemail | 5,082 | 28% | 1,661 | $186 |
| No contact | 6,505 | 36% | 1,423 | $147 |
| Never connected | 2,743 | 15% | 0 | $0 |

Voicemail, unanswered calls, hang-ups and phone menus take 53% of the ElevenLabs bill. ElevenLabs charges the same per minute for them as for a real conversation, and it also bills LLM usage on voicemail calls.

### Outcome detail

| Outcome | Calls | Connected | Avg length (s) | Minutes | ElevenLabs cost |
| :--- | ---: | ---: | ---: | ---: | ---: |
| voicemail | 5,082 | 5,081 | 19.6 | 1,661 | $186.21 |
| hangup_no_speak | 3,814 | 3,024 | 11.7 | 590 | $50.65 |
| (none) | 2,743 | 0 | 0.0 | 0 | $0.00 |
| no_answer | 1,871 | 1,684 | 16.6 | 466 | $48.17 |
| other | 1,812 | 1,812 | 26.7 | 807 | $86.38 |
| employee_no_owner_details | 810 | 810 | 38.1 | 514 | $62.97 |
| automated_menu | 659 | 659 | 26.5 | 291 | $38.69 |
| declined | 545 | 545 | 42.9 | 390 | $43.77 |
| callback_set | 369 | 369 | 67.0 | 412 | $45.57 |
| owner_details_captured | 257 | 257 | 62.9 | 270 | $34.95 |
| unknown_dropped | 161 | 160 | 28.9 | 77 | $9.08 |
| info_email | 72 | 72 | 104.5 | 125 | $13.99 |
| do_not_call | 58 | 58 | 39.2 | 38 | $5.12 |
| consultant_call_scheduled | 27 | 27 | 114.5 | 52 | $6.04 |
| email_comparison | 8 | 8 | 138.6 | 18 | $2.31 |

Useful results (callback set, owner details captured, info email, consultant call scheduled, email comparison): 733 calls, or 4.0% of dials.

### How long calls last

| Length | Calls | Share of calls | Minutes | Share of minutes |
| :--- | ---: | ---: | ---: | ---: |
| under 10 s | 4,058 | 27.9% | 426 | 7.4% |
| 10 to 20 s | 4,984 | 34.2% | 1,159 | 20.3% |
| 20 to 30 s | 1,995 | 13.7% | 788 | 13.8% |
| 30 to 60 s | 2,541 | 17.4% | 1,693 | 29.6% |
| 1 to 2 min | 817 | 5.6% | 1,079 | 18.9% |
| 2 to 5 min | 157 | 1.1% | 421 | 7.4% |
| over 5 min | 14 | 0.1% | 144 | 2.5% |

62% of connected calls last under 20 seconds. Calls over a minute are 6.8% of calls but 29% of the minutes. The 90th percentile is 49 seconds.

---

## 3. What ElevenLabs charged

**Published pricing** ([elevenlabs.io/pricing/agents](https://elevenlabs.io/pricing/agents)): $0.08 per call minute on every plan, with plans only prepaying minutes (Creator 275 min for $22, Pro 1,238 for $99, Scale 3,738 for $299, Business 12,375 for $990). LLM usage is billed separately in credits. The carrier is billed at cost by the carrier. Calls over the plan's concurrency limit are charged double ("burst", up to 3 times the limit).

**What the database shows.** Each call stores a call charge and an LLM charge in credits. The call charge works out to a steady 485 credits per connected minute (median 485.5), which matches $0.08 per minute at about $0.000165 per credit. Calls are billed to the second with no minimum. Calls that never connected carry no charge.

| Week starting | Calls | Minutes | Call $/min | LLM $/min | All-in $/min | LLM share |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| 28 Sep | 765 | 315 | $0.077 | $0.009 | $0.086 | 11% |
| 5 Oct | 1,333 | 469 | $0.078 | $0.012 | $0.090 | 13% |
| 12 Oct | 3,504 | 1,389 | $0.078 | $0.015 | $0.093 | 16% |
| 19 Oct | 6,800 | 1,979 | $0.078 | $0.034 | $0.112 | 31% |
| 26 Oct | 2,037 | 544 | $0.079 | $0.044 | $0.124 | 36% |
| 2 Nov | 3,742 | 975 | $0.079 | $0.056 | $0.135 | 42% |
| 9 Nov (1 day) | 107 | 40 | $0.056 | $0.029 | $0.086 | 34% |

The call price stayed at about $0.08 a minute. The LLM price per minute rose about six times, from $0.009 to $0.056, as agents moved to longer prompts with the knowledge base. The last three weeks (26 Oct to 9 Nov) average $0.131 per minute all-in, higher than the full-period $0.111.

**Plan note.** The plan price does not change the per-minute cost unless a plan is underused. At about 4,167 connected minutes a month, Scale ($299, 3,738 minutes, 30 concurrent calls) covers nearly all of it. Business ($990) would work out to about $0.24 a minute at this usage and only makes sense for its 40-call concurrency. I could not see which plan is on the account.

Cost per useful result: **$0.86** ($634 spent for 733 useful results).

---

## 4. How the custom agent was simulated

Each of the 14,566 real connected calls was replayed through the cost model of our own stack (LiveKit Cloud, Deepgram, Gemini, ElevenLabs voice API, Telnyx), using that call's real duration, outcome, ring time and transcript length. Monthly figures scale the 41-day totals to 30 days (13,345 dials and 4,167 connected minutes a month).

| Input | Value used | Source |
| :--- | :--- | :--- |
| ElevenLabs agent call minute | $0.08 per minute, all plans | elevenlabs.io/pricing/agents |
| ElevenLabs LLM charge | Credits at about $0.000165 each (Pro, Scale and Business plans) | elevenlabs.io/pricing |
| Deepgram Nova-3, streaming | $0.0077 per minute | deepgram.com/pricing |
| ElevenLabs voice (eleven_v3_conversational) | $0.04 per 1,000 characters | elevenlabs.io/pricing/api |
| Gemini 3.5 Flash | $1.50 in, $0.15 cached, $9.00 out per 1M tokens | third-party trackers, not confirmed on Google's page |
| LiveKit Cloud Ship plan | $50 a month; 5,000 agent and 5,000 SIP minutes, 600 recording minutes included | livekit.com/pricing |
| LiveKit overage | $0.01 agent, $0.004 SIP, $0.005 recording per minute | livekit.com/pricing |
| Telnyx outbound US | $0.005 per minute, the same on both systems (left out of the totals) | telnyx.com/pricing/elastic-sip |
| Prompt size | About 9,000 tokens per LLM turn (40,026 characters rendered) | our prompt files |
| Prompt caching | 70% of repeated input tokens billed at the cached rate after the first turn | assumption, 60% seen in an earlier test call |
| Voice characters | Taken from the real transcripts: 446 per connected minute | call database |
| Ring time on LiveKit | Dial to answer time from the database; 20 seconds assumed for calls that never connected | call database, assumption |

**How each cost is worked out per call**

- **Speech to text:** connected minutes times the Deepgram rate (audio is streamed for the whole call).
- **Voice:** the agent's real transcript characters times $0.04 per 1,000. The transcripts average 446 characters per connected minute.
- **LLM:** one request per agent turn after the scripted opening line. Each request carries the full prompt (about 9,000 tokens) plus the conversation so far, with 70% of repeated tokens at the cached rate. Output is the spoken text plus a small allowance for tool calls.
- **Post-call classifier:** one extra Gemini request per call that has a real conversation. It is skipped for voicemail, unanswered calls and calls where the person never spoke, as in the code.
- **LiveKit:** agent minutes are dial-to-answer time plus the call, plus 20 seconds for calls that never connected. SIP and recording minutes are counted too. The plan's included quotas are subtracted first.

---

## 5. Custom agent cost report

### Monthly cost at today's volume (13,345 dials, 4,167 connected minutes)

| System | Component | Per month |
| :--- | :--- | ---: |
| ElevenLabs | Call minutes ($0.08/min) | $333 |
| ElevenLabs | LLM (billed in credits) | $129 |
| Custom agent | Speech to text (Deepgram) | $32 |
| Custom agent | Voice (ElevenLabs API) | $74 |
| Custom agent | LLM and classifier (Gemini) | $119 |
| Custom agent | LiveKit plan | $50 |
| Custom agent | LiveKit over included quota | $55 |

| | ElevenLabs | Custom agent |
|---|---:|---:|
| Total per month (carrier excluded) | **$463** | **$330** |
| Per connected minute | $0.111 | $0.079 |
| Per dial | $0.035 | $0.025 |
| Carrier (Telnyx), same on both | $21 | $21 |

At the recent ElevenLabs rate of $0.131 a minute, ElevenLabs would run about $546 a month at this volume, which widens the gap to about 40%.

### Where the custom agent's money goes

The LLM is the largest provider cost ($119), then the voice ($74) and speech to text ($32). LiveKit costs $105 a month: the $50 plan plus $55 for usage above the included quota. At this volume the agent needs about 7,936 agent minutes a month against 5,000 included, and about 4,167 recording minutes against 600 included.

### Sensitivity to the LLM

| Scenario | Per month | Versus ElevenLabs |
| :--- | ---: | ---: |
| ElevenLabs at today's rates | $463 | reference |
| Custom, Gemini 3.5 Flash, no prompt caching | $396 | -14% |
| Custom, Gemini 3.5 Flash, 70% cached (base) | $330 | -29% |
| Custom, Gemini 3.5 Flash, 90% cached | $311 | -33% |
| Custom, Gemini 3.6 Flash (cheaper until 31 Dec 2026) | $270 | -42% |
| Custom, Gemini 3.5 Flash-Lite (needs a quality test) | $236 | -49% |

Caching matters most: our prompt is about 9,000 tokens and is sent on every turn. Gemini 3.6 Flash is cheaper only until 31 Dec 2026 and then reaches about the 3.5 Flash price. Flash-Lite is the durable low-cost option and is already our fallback model, but it needs a quality test before it becomes the main model.

### Sensitivity to volume (same call mix)

| Dials per month | ElevenLabs | Custom agent | Custom versus ElevenLabs |
| :--- | ---: | ---: | ---: |
| 1,000 | $35 | $67 | +93% |
| 3,000 | $104 | $102 | -2% |
| 5,000 | $173 | $139 | -20% |
| 10,000 | $347 | $242 | -30% |
| 13,346 | $463 | $330 | -29% |
| 25,000 | $867 | $639 | -26% |
| 50,000 | $1,733 | $1,300 | -25% |

The two lines cross at about **2,910 dials a month**. Below that the $50 fixed LiveKit fee dominates. Above it the custom agent saves roughly 25 to 30%.

### Planned volume: 20,000 dials a month

Same call mix as the history, scaled to 20,000 dials a month: about 6,245 connected minutes, 11,894 LiveKit agent minutes, 10,232 SIP minutes and 6,245 recording minutes.

| System | Component | Per month |
| :--- | :--- | ---: |
| ElevenLabs | Call minutes ($0.08/min) | $500 |
| ElevenLabs | LLM (billed in credits) | $194 |
| Custom agent | Speech to text (Deepgram) | $48 |
| Custom agent | Voice (ElevenLabs API) | $111 |
| Custom agent | LLM and classifier (Gemini) | $179 |
| Custom agent | LiveKit plan | $50 |
| Custom agent | LiveKit over included quota | $118 |

| Scenario | Custom per month | Saving per month | Saving | Saving per year |
| :--- | ---: | ---: | ---: | ---: |
| Gemini 3.5 Flash, no prompt caching | $605 | $88 | 13% | $1,054 |
| **Gemini 3.5 Flash, 70% cached (base)** | $506 | $187 | 27% | $2,244 |
| Gemini 3.5 Flash, 90% cached | $478 | $215 | 31% | $2,584 |
| Gemini 3.6 Flash (until 31 Dec 2026) | $416 | $278 | 40% | $3,332 |
| Gemini 3.5 Flash-Lite (needs a quality test) | $365 | $329 | 47% | $3,945 |

ElevenLabs at this volume is **$693 a month** using the full-period average LLM cost ($500 for call minutes, $194 for LLM). If the ElevenLabs LLM charge stays at the recent rate of $0.131 a minute all-in, it is about **$818 a month**, and the base-case saving grows to about $312 a month (38%), or $3,742 a year.

- **Base case:** the custom agent costs about **$506 a month against $693**, a saving of **$187 a month (27%), about $2,244 a year**. Realistic range across the caching cases: $88 to $215 a month.
- **Per useful result:** at the historical rate of 4.0% of dials, 20,000 dials is about 802 useful results a month, or about $0.86 each on ElevenLabs against $0.63 on the custom agent.
- **If ElevenLabs is on the Business plan ($990):** the call minutes would cost $990 a month for 6,245 of the 12,375 included minutes, so the real saving would be larger. On Scale ($299 plus $0.08 a minute above 3,738 minutes) the cost matches the $500 above.
- **LiveKit:** 20,000 dials uses about 11,894 agent minutes against 5,000 included on the $50 Ship plan, so the overage is $118 a month. I did not check whether LiveKit's higher plans would be cheaper at this volume.
- **Concurrency:** 20,000 dials a month is about 670 a day. Ship allows 20 calls at once. Lead-gen should cap campaign concurrency at 20, or the custom agent needs a higher plan.
- **Not included:** the carrier (about $31 a month on either system), the Pakistan server and engineering time.

### What the current LiveKit plan allows

On the Build plan (free) the quotas are 1,000 agent minutes, 1,000 SIP minutes, 60 recording minutes and 5 concurrent calls. With this call mix (0.595 agent minutes and 0.312 connected minutes per dial):

- Agent and SIP minutes last for about **1,682 dials a month**.
- Recording lasts for only about **192 dials**. Turn recording off on Build, or accept that recordings stop after about 60 minutes.
- 5 concurrent calls is tight: in the history, calls in flight exceeded 5 for about 570 seconds in total, with a peak of 31.

Your current volume of about 13,345 dials a month does not fit on Build. It fits on the $50 Ship plan, which allows 20 concurrent calls. The history spent about 14 seconds above 20 in flight, so lead-gen should keep campaign concurrency under 20.

---

## 6. Side by side

| | ElevenLabs agents | Custom agent |
|---|---|---|
| Cost at 13,345 dials a month | $463 | $330 (29% less) |
| Cost at the planned 20,000 dials a month | $693 | $506 (27% less) |
| Cost at 1,000 dials a month | $35 | $67 |
| Fixed monthly fee | none (plans prepay minutes at the same rate) | $50 (LiveKit) |
| Pricing shape | $0.08 a minute plus LLM that grows with prompt size | Providers billed separately; LLM can be cached or swapped |
| Voicemail and no-contact calls | full call-minute price plus LLM | no LLM calls; about $129 against $333 over the window (before LiveKit fees) |
| Concurrency | 30 on Scale, 40 on Business | 20 on Ship |
| Ops burden | managed | we run the API server, database and worker |
| Feature status | live transfer, team briefing and inbound all working | **not built yet**: live transfer, team briefing, demo-ready mode, inbound |

---

## 7. Assumptions and limits

1. **Credits to dollars.** ElevenLabs reports cost in credits. I converted at $0.000165 a credit, the Pro, Scale and Business rate. The call charge cross-checks against $0.08 a minute (within 2%). The LLM credit price is an assumption; on the Creator plan credits cost about 10% more.
2. **The simulation replays old transcripts.** The calls ran on earlier multi-step ElevenLabs agents. Our single-prompt agent may talk more or less, and may get a different mix of outcomes. Voice characters and turns per call come from those transcripts. The scripted opening line and the two-second voicemail check in our agent are not modelled.
3. **Gemini 3.5 Flash price** ($1.50 in, $9.00 out per million tokens) comes from third-party price trackers. Google's own page, as far as I could read, does not list that model.
4. **Prompt caching** is assumed at 70% of repeated tokens. One earlier test call showed about 60%. The no-cache and 90% cases are in section 5.
5. **LiveKit minutes** include ring time, estimated from the time between dialing and the call starting. SIP minutes are taken equal to agent minutes, which slightly overstates them.
6. **Carrier cost** (Telnyx, about $0.005 a minute) is the same on both systems and left out of the totals. The actual Telnyx rate should be confirmed on an invoice.
7. **Not included:** the server in Pakistan, the engineering time to build and maintain the agent, Deepgram's free credit, any LiveKit "observability" charge (the calculator lists $0.01 a minute, the plan table lists no quota), taxes, and the cost of the missing features.
8. **Quality is not compared.** This report is about cost. It says nothing on whether the custom agent books as many meetings as the ElevenLabs agents did.
9. **Peak concurrency** counts dial to hang-up. ElevenLabs may count only connected conversations.

## 8. Sources

- ElevenLabs agents pricing: https://elevenlabs.io/pricing/agents
- ElevenLabs pricing and voice API: https://elevenlabs.io/pricing, https://elevenlabs.io/pricing/api
- LiveKit Cloud pricing: https://livekit.com/pricing
- Deepgram pricing: https://deepgram.com/pricing
- Telnyx SIP pricing: https://telnyx.com/pricing/elastic-sip
- Gemini pricing: https://aistudio.google.com/docs/pricing
- Call data: lead-gen database `calls` collection, read-only, de-identified (no phone numbers, names or transcript text were copied; only durations, outcomes, costs and character counts).
