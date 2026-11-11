# Backend Contract: Posivex-lead-gen Compatibility Specification

> **Status:** Production Specification  
> **Source Evidence:** Discovered directly from `C:\Users\Khizar\Desktop\Projects\Posivex-lead-gen` (`src/services/calls.ts`, `src/services/dialer.ts`, `src/services/initiation.ts`, `src/lib/elevenlabs.ts`, `src/lib/elevenlabs-payload.ts`, `src/lib/webhook-signature.ts`).  
> **Target:** LiveKit Agent Outbound Calling Service must satisfy this contract with **zero code modifications** to `Posivex-lead-gen`.

---

## 1. Overview & Dual Interface Strategy

`Posivex-lead-gen` interacts with the voice agent provider in three ways:
1. **Outbound Call Initiation:** Next.js backend issues HTTP POST requests to start calls.
2. **Post-Call Webhook Ingestion:** Next.js backend receives signed HTTP POST requests containing full transcripts, metadata, costs, and `call_outcome` classifications.
3. **Inbound Call Dynamic Variable Lookup:** Next.js backend answers an initiation webhook for inbound calls based on caller ID.

To migrate cleanly without touching `Posivex-lead-gen`:
- The LiveKit Calling Service exposes an **ElevenLabs Emulation Web API layer** (`/v1/convai/*`).
- Post-call completion events are emitted via an **ElevenLabs Webhook Adapter** utilizing HMAC-SHA256 signatures matching ElevenLabs' exact format.

---

## 2. Inbound Dispatch / Call Initiation

### 2.1 Emulated ElevenLabs Endpoints

`Posivex-lead-gen` calls:
- `POST /v1/convai/sip-trunk/outbound-call`. Our `/v1/convai/phone-numbers` lists every number with provider `sip_trunk`, so lead-gen never picks the Twilio route (`/v1/convai/twilio/outbound-call`), and this service does not provide it.

**Headers Required:**
```http
xi-api-key: <string>
Content-Type: application/json
```

**Payload Schema (`OutboundCallRequest`):**
```json
{
  "agent_id": "agent_8401m61r9k8kffpvcjqxtkx3ams1",
  "agent_phone_number_id": "phnum_8501m0asqpg8f00rqy2nx7vv37zf",
  "to_number": "+14045551234",
  "conversation_initiation_client_data": {
    "dynamic_variables": {
      "business_name": "Tony's Pizza & Grill",
      "business_context": "Business: Tony's Pizza & Grill\nCategory: Pizza restaurant\nRating: 4.5 stars with 312 reviews\nAddress: 1450 Peachtree St, Atlanta, GA\nTime zone: America/New_York (local time now: Tuesday, October 20, 2026 at 3:42 PM)\nRecent calls: Voicemail left | Follow-up requested\nCall direction: outbound",
      "current_datetime": "Tuesday, October 20, 2026 at 3:42 PM (America/New_York)",
      "is_followup": "true",
      "followup_notes": "Requested comparison quote",
      "contact_name": "Tony"
    }
  }
}
```

**Success Response (HTTP 200 OK):**
```json
{
  "success": true,
  "conversation_id": "conv_01jbc456def789",
  "callSid": "CA1234567890abcdef",
  "sip_call_id": "sip_987654321"
}
```

**Error Responses:**
- `401 Unauthorized`: Invalid or missing `xi-api-key`.
- `400 Bad Request`: Invalid phone number, missing agent ID, or invalid variable JSON.
  ```json
  {
    "success": false,
    "detail": "Invalid target phone number format"
  }
  ```
- `429 Too Many Requests`: System concurrency or rate limit exceeded.
- `500 Internal Server Error`: Provider or internal failure.

---

### 2.2 Native LiveKit Pydantic Schema (`CallRequest`)

For direct integration and modern services, the system accepts:

```python
from pydantic import BaseModel, Field
from typing import Optional, Literal, Dict, Any

class DynamicVariables(BaseModel):
    business_name: str = Field(..., description="Name of the target business")
    business_context: str = Field("", description="Category, rating, reviews, timezone context")
    current_datetime: str = Field(..., description="Local formatted datetime string with timezone")
    is_followup: Literal["true", "false"] = "false"
    followup_notes: str = ""
    contact_name: str = ""

class CallRequest(BaseModel):
    call_id: Optional[str] = Field(None, description="Client-provided unique call identifier")
    lead_id: Optional[str] = Field(None, description="Posivex MongoDB Lead ObjectId string")
    campaign_id: Optional[str] = Field(None, description="Campaign ID if part of an automated campaign")
    phone: str = Field(..., regex=r"^\+?[1-9]\d{1,14}$", description="E.164 phone number to dial")
    agent_id: Optional[str] = Field(None, description="ElevenLabs agent ID or internal agent identifier")
    persona: Literal["amelia", "michael"] = "amelia"
    language: Literal["en", "es", "hi", "ur"] = "en"
    phone_number_id: Optional[str] = Field(None, description="Outbound caller ID phone number ID")
    dynamic_variables: DynamicVariables
    config_overrides: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Allowed per-call overrides")
```

---

## 3. Post-Call Webhook Contract (`/api/webhooks/elevenlabs`)

### 3.1 HTTP Transport & Authentication

- **Method:** `POST`
- **Destination:** `${LEAD_GEN_BASE_URL}/api/webhooks/elevenlabs`
- **Content-Type:** `application/json`
- **Signature Header:** `elevenlabs-signature`

**Signature Construction (`lib/webhook-signature.ts`):**
1. Obtain current Unix timestamp in seconds `now_secs`.
2. Construct signature payload: `f"{now_secs}.{raw_body_json_string}"`.
3. Compute HMAC-SHA256 hex digest using secret `ELEVENLABS_WEBHOOK_SECRET`.
4. Transmit header:
   ```http
   elevenlabs-signature: t=<now_secs>,v0=<hex_hmac>
   ```

---

### 3.2 Webhook Payload Structure

`Posivex-lead-gen` parser (`parseConversation` in `src/lib/elevenlabs-payload.ts`) extracts the following structure:

```json
{
  "type": "post_call_transcription",
  "event_timestamp": 1793208500,
  "data": {
    "conversation_id": "conv_01jbc456def789",
    "status": "done",
    "agent_id": "agent_8401m61r9k8kffpvcjqxtkx3ams1",
    "agent_name": "Posivex - 5 - Amelia",
    "transcript": [
      {
        "role": "agent",
        "message": "Hi, this is Amelia from Clover Posivex. Am I speaking with the owner of Tony's Pizza & Grill?",
        "time_in_call_secs": 1.5,
        "interrupted": false
      },
      {
        "role": "user",
        "message": "Yes, this is Tony speaking. How can I help you?",
        "time_in_call_secs": 5.2,
        "interrupted": false
      },
      {
        "role": "agent",
        "message": "I'm calling from Clover Posivex. I just need thirty seconds of your time to explain why I'm calling, and you can tell me if it makes sense for your business?",
        "time_in_call_secs": 8.0,
        "interrupted": false
      }
    ],
    "analysis": {
      "data_collection_results": {
        "call_outcome": {
          "value": "consultant_call_scheduled",
          "rationale": "Owner agreed to a 10-minute demo over Google Meet for Thursday at 2 PM; email collected and confirmed."
        }
      },
      "transcript_summary": "Amelia contacted Tony, owner of Tony's Pizza & Grill. Owner currently uses Square with 2 terminals. Tony agreed to a 10-minute Google Meet demonstration with the Consultant on Thursday at 2:00 PM EST. Meeting invite to be sent to tony@tonyspizza.com.",
      "call_successful": "success",
      "call_success_score": 1.0,
      "call_summary_title": "Demo Scheduled with Tony's Pizza & Grill"
    },
    "metadata": {
      "call_duration_secs": 182,
      "start_time_unix_secs": 1793208318,
      "queue_wait_secs": 0,
      "termination_reason": "call_ended_normally",
      "cost": 0.084,
      "charging": {
        "call_charge": 0.060,
        "llm_charge": 0.024
      },
      "phone_call": {
        "call_result": "answered",
        "call_sid": "CA1234567890abcdef",
        "external_number": "+14045551234",
        "direction": "outbound",
        "phone_number_id": "phnum_8501m0asqpg8f00rqy2nx7vv37zf"
      }
    }
  }
}
```

---

### 3.3 Call Outcome Enum & Normalization Rules

`Posivex-lead-gen` (`src/lib/call-outcome.ts`) categorizes outcomes into strict groups:

| `call_outcome` Enum Value | Outcome Group | Meaning in Lead-Gen |
|---|---|---|
| `consultant_call_scheduled` | `positive` | Demo booked over Zoom/Meet with time + email |
| `email_comparison` | `positive` | Lead requested rate comparison against recent statement |
| `info_email` | `positive` | Wholesale rate sheet requested; email confirmed |
| `owner_details_captured` | `positive` | Staff gave owner name, best number, callback time window |
| `callback_set` | `callback` | Agreed to specific scheduled phone callback time |
| `declined` | `declined` | Clear refusal / not interested after recovery |
| `do_not_call` | `declined` | Explicit request to stop calling / remove number |
| `employee_no_owner_details` | `no_contact` | Spoke to staff, but no owner contact details yielded |
| `voicemail` | `no_contact` | Reached voicemail greeting or answering machine |
| `automated_menu` | `no_contact` | IVR phone tree or automated greeting |
| `no_answer` | `no_contact` | Call rang out or network declined (SIP 486/480/603) |
| `hangup_no_speak` | `no_contact` | Answered but dropped/hung up in < 15 seconds |
| `unknown_dropped` | `no_contact` | Call disconnected prematurely without resolution |
| `other` | `no_contact` | Ambiguous / unclassified resolution |

**Critical Lead-Gen Logic Dependent on Outcome:**
- Positive outcomes (`consultant_call_scheduled`, `email_comparison`, `info_email`, `owner_details_captured`) update lead pipeline status to `positive` and trigger automated follow-up email drafts.
- `callback_set` creates an open `FollowUp` task in MongoDB with a specific target timestamp.
- `do_not_call` creates an immediate record in `suppressions` collection, blocking future automated dials.
- `no_contact` triggers automated retries according to campaign retry schedule.

---

## 4. Inbound Initiation Webhook (not provided by this service)

Lead-gen serves its own `POST /api/webhooks/elevenlabs/initiation` (on its own domain) for inbound calls; ElevenLabs calls it
there. This service does not implement it (`supports_inbound` is `false` in `/v1/convai/phone-numbers`), so inbound calls
stay on ElevenLabs agents. The contract below is lead-gen's, kept for reference.

- **Endpoint (lead-gen):** `POST /api/webhooks/elevenlabs/initiation`
- **Auth Header:** `X-Initiation-Secret: <secret>` or `Authorization: Bearer <secret>`
- **Request Body:** `{ "caller_id": "+14045551234" }`
- **Response Budget:** 2000 ms timeout.
- **Response:** `{"type": "conversation_initiation_client_data", "dynamic_variables": {...}}` with `call_direction`, `caller_status`,
  `caller_phone`, `business_name`, `business_context`, `current_datetime`, `is_followup`, `followup_notes`, `contact_name`.

---

## 5. Webhook Delivery Resilience (Outbox Pattern)

Because `Posivex-lead-gen` handles post-call analysis asynchronously via Inngest events, the LiveKit Webhook Adapter must guarantee:
1. **Idempotency Key:** `X-Delivery-Id: <uuid>` and stable `dedupeKey` (`post_call_transcription:<conversationId>:<timestamp>`).
2. **Exponential Backoff:** If `Posivex-lead-gen` returns HTTP 500 or network drops, retry up to 10 times over 24 hours.
3. **Outbox Persistence:** Webhook events are committed transactionally to Postgres in the same transaction as call completion, then dispatched by a reliable background worker.

