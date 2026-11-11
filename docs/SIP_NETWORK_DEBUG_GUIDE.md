> **Historical:** this guide covers the self-hosted `livekit/sip` setup behind a home router, which was replaced by LiveKit Cloud SIP (see `docs/RUNBOOK.md` section 3.1). It is kept for reference only; the commands in it refer to `scripts/call.py` (removed) and compose services (`sip`, `livekit-server`, `posivex_agent`) that no longer exist.

# SIP / Network Debug Guide: "Call is answered but there is no audio"

> **Audience:** the developer and the network engineer running the debug together.
> **Goal:** find out why outbound calls placed through Telnyx are answered on the phone but never connect to the voice agent, and fix it.
> **Nothing in this guide changes application code.** Every step is a check or a router/firewall configuration change.
> **Cost warning:** every call that Telnyx sees as answered is billed ("Trunking Outbound Minute - US - High Cost (Zone 5)"). Use your own number for tests and keep tests short.

---

## 1. The problem in one paragraph

We place an outbound call. The callee's phone rings and the callee answers. Telnyx considers the call answered and bills it. But the voice agent never greets, the callee hears silence, and after about **32 seconds Telnyx hangs up**. On our side, the SIP service (LiveKit SIP) never receives the answer message (`200 OK`) and never receives or sends any audio (RTP). The agent waits 75 seconds, gives up, and records the call as `no_answer`.

The most likely cause is that **return traffic from Telnyx (the `200 OK`, and the audio) is not reaching the server** because of the router, the ISP, or a port that is not forwarded. The application itself is behaving correctly.

## 2. Facts about this environment

| Item | Value |
|---|---|
| Server host | `crm-server-OptiPlex-7020` (aaPanel, Docker Compose), project path `/www/wwwroot/custom-elevenlabs-v1` |
| Server LAN IP / NIC | `192.168.100.87` on `eno1` |
| Public IP seen by the SIP service (STUN) | `175.107.203.4` |
| SIP container | `posivex_sip` (image `livekit/sip`), Docker bridge address `172.21.0.6` |
| SIP signaling | `5060/udp` and `5060/tcp`, published by Docker to the host |
| RTP (audio) range | `10000-10100/udp`, published by Docker (`sip.yaml`: `rtp_port: "10000-10100"`, `use_external_ip: true`) |
| Carrier | Telnyx, outbound trunk to `sip.telnyx.com` (username/password auth) |
| Telnyx signaling IP seen | `192.76.120.10:5060` |
| Telnyx media IP seen | `50.114.144.52`, port `24832` (from the 183 message) |
| Advertised in our INVITE | `Via/Contact 175.107.203.4:5060`, `c=IN IP4 175.107.203.4`, `m=audio 10070` |
| Direction | **Outbound only.** There is no inbound trunk. |

## 3. What the evidence already proves

1. **Packet capture on `eno1`** (110 s covering a whole call) shows: `INVITE → 407 → INVITE → 100 → 180 Ringing → 183 Session Progress → (silence for 77 s) → CANCEL` (the CANCEL is our agent hanging up after its 75 s ring timeout). **No `200 OK`, no retransmissions of it, and zero RTP packets in either direction.**
2. `tcpdump` on `eno1` records packets *before* the host firewall (iptables/ufw) sees them. So whatever is missing is **missing upstream of the server: router, ISP, or Telnyx**, not the host firewall.
3. **Telnyx call report (CDR):** the calls are billed (so Telnyx treated them as answered), hang up is `send_bye` (Telnyx ended them), MoS is `1` (no audio), codec negotiated PCMU/G722. Telnyx sends a BYE roughly 32 s after answering when it never receives an `ACK` for its `200 OK`. That matches the "32 seconds" you observed.
4. The replies that **do** arrive (`100`, `407`, `180`, `183`) are answers on a connection **we opened**, which a NAT router allows back in. The `200 OK` and the audio are the traffic that needs real inbound reachability, and they are the ones that vanish.
5. Docker forwarding is **not** the problem: the packets that arrive reach the container correctly (`eno1 → bridge → veth`).

## 4. What we still do not know (the debug answers these)

- Is `175.107.203.4` really this site's public IP, or is the ISP using carrier-grade NAT (CGNAT)?
- Are `5060` and `10000-10100/udp` forwarded to `192.168.100.87` on the router?
- Is something on the router (SIP ALG, a firewall feature) altering or dropping SIP?
- To which address and port did Telnyx send the `200 OK`? (Telnyx's call-flow page answers this.)

## 5. Prerequisites

- SSH access to the server as a user with `sudo`.
- Admin access to the router at the site (the network engineer).
- A second network to test from (a laptop on a phone hotspot, or a cloud VPS).
- Access to the Telnyx portal.
- A phone number you control for test calls (the examples use `+19432094556`).

Run all commands from `/www/wwwroot/custom-elevenlabs-v1` unless stated otherwise.

---

## Step 0: Confirm the server side is healthy

**Why:** rule out a stopped container before blaming the network.

```bash
docker compose ps
docker port posivex_sip
sudo ss -lunp | grep -E ':5060|:100[0-9][0-9]' | head
sudo ss -ltnp | grep ':5060'
```

**Expected:** `posivex_sip` is `Up`. `docker port` lists `5060/udp`, `5060/tcp` and the `10000-10100/udp` range. `ss` shows `5060` listening (UDP and TCP).
**If not:** start the stack (`docker compose up -d`) and re-check before continuing.

---

## Step 1: Check for carrier-grade NAT (CGNAT)

**Why:** if the ISP shares one public IP between many customers, no inbound port forward can work, and nothing else in this guide will fix it.

1. On the server:
   ```bash
   curl -s ifconfig.me; echo
   curl -s ifconfig.me/ip; echo
   ```
   Note the address (expected: `175.107.203.4`).
2. The network engineer opens the router admin page and reads the **WAN / Internet IP address**.
3. Compare:

| Result | Meaning | Action |
|---|---|---|
| Router WAN IP **equals** the address from `ifconfig.me` | Public IP is yours | Continue to Step 2 |
| Router WAN IP is `100.64.x.x`-`100.127.x.x`, `10.x.x.x`, `172.16-31.x.x` or `192.168.x.x` | **CGNAT** (or double NAT) | **Stop.** Ask the ISP for a public (non-CGNAT) IP, or use the VPS option in Step 10. |
| Router WAN IP differs but is a public address | Another router/modem is in front | Find that device and apply Steps 2-3 there too (or put it in bridge mode) |

Optional extra check: `traceroute -n 8.8.8.8 | head -5`. Two private hops at the start suggest double NAT.

---

## Step 2: Give the server a fixed LAN address

**Why:** port forwards point at an IP. If the server's address changes, the forwards silently break.

On the router, create a **DHCP reservation** (static lease) for the server's NIC so it always receives `192.168.100.87`.
Find the NIC's MAC address with:
```bash
ip link show eno1 | awk '/link\/ether/ {print $2}'
```

**Done when:** the router shows `192.168.100.87` bound to that MAC address.

---

## Step 3: Router configuration

Make these changes on the router (the engineer knows the exact menu names for the model).

1. **Port forwards** to `192.168.100.87`:

   | Name | Protocol | External port(s) | Internal port(s) |
   |---|---|---|---|
   | SIP | UDP | `5060` | `5060` |
   | SIP | TCP | `5060` | `5060` |
   | RTP | UDP | `10000-10100` | `10000-10100` (same range, no remapping) |

   Do **not** remap ports. The SIP service advertises these exact numbers to Telnyx.
2. **Disable SIP ALG** (sometimes named "SIP Passthrough", "SIP Helper", "VoIP ALG" or "Application Layer Gateway"). SIP ALGs rewrite SIP headers and commonly break answers and audio.
3. Turn off any router feature that inspects or filters UDP traffic (IPS/IDS, "SPI" strictness options, DoS protection that rate-limits UDP) for these ports, or confirm it allows them.
4. Check the router's **inbound firewall rules**: the forwards must not be overridden by a default "deny WAN to LAN" rule. Some routers need an explicit allow rule in addition to the forward.
5. Save, and **reboot the router** if the model requires it for NAT changes.

**Optional hardening (do after it works):** Telnyx publishes its signaling and media IP ranges. The engineer may restrict the forwards to those ranges. Do not do this until the unrestricted version works, otherwise a missing range looks like the original bug.

---

## Step 4: Prove the ports are reachable from the internet (no call yet)

**Why:** this separates "router/ISP problem" from everything else without spending money on calls.

### 4a. On the server, start a listener

```bash
sudo tcpdump -ni eno1 'udp and (port 5060 or port 10070)' -c 20
```
Leave it running. (It exits after 20 packets or on Ctrl+C.)

### 4b. From a different network (phone hotspot, VPS), send test packets

```bash
# UDP SIP port
echo hi | nc -u -w1 175.107.203.4 5060
# UDP RTP port inside the forwarded range
echo hi | nc -u -w1 175.107.203.4 10070
```
Alternative if `nc` is unavailable (bash):
```bash
echo hi > /dev/udp/175.107.203.4/5060
echo hi > /dev/udp/175.107.203.4/10070
```

TCP check:
```bash
nc -v -w3 175.107.203.4 5060
```

### 4c. Read the result

| tcpdump on the server shows | Meaning | Action |
|---|---|---|
| Both UDP packets arrive from the test machine's IP | Router and ISP forward correctly | Go to Step 5 |
| Nothing arrives | The forward is wrong, a firewall is blocking, the ISP blocks the port, or you are behind CGNAT | Re-check Steps 1-3. If the router is correct, test from a second network and ask the ISP whether they block inbound `5060`. |
| Only the `5060` packet arrives | RTP range not forwarded | Fix the `10000-10100` forward |
| Packets arrive for UDP but TCP `5060` is refused or times out | TCP forward missing (UDP calls can still work, but fix it) | Add the TCP forward |

**Pass criteria:** both UDP packets arrive on `eno1`.

---

## Step 5: Confirm the packets reach the container

**Why:** a packet arriving on `eno1` does not guarantee it reaches the SIP service. This checks Docker's side.

1. Repeat Step 4b while capturing on all interfaces:
   ```bash
   sudo tcpdump -ni any 'udp and port 5060' -c 10
   ```
   Expected: the packet appears on `eno1`, then on the Docker bridge and the `veth…` interface for the `posivex_sip` container (address `172.21.0.6`).
2. Check Docker's rules for the published ports:
   ```bash
   sudo iptables -t nat -S DOCKER | grep -E '5060|10000'
   sudo iptables -S DOCKER-USER
   ```
   Expected: DNAT rules for `5060` (udp and tcp) pointing to `172.21.0.6`, and a range rule for `10000:10100/udp`. `DOCKER-USER` must not contain a DROP that matches these ports.
3. If aaPanel or ufw is active, make sure they do not drop forwarded traffic to Docker:
   ```bash
   sudo ufw status verbose 2>/dev/null
   ```
   Open `5060/udp`, `5060/tcp` and `10000:10100/udp` in the aaPanel security/firewall page as well.

**Pass criteria:** packets reach the container's interface.

---

## Step 6: Full live-call capture

**Why:** this is the real test. It shows whether the `200 OK` and the audio now arrive.

1. **Terminal A: start the capture** (it stops itself after 110 seconds):
   ```bash
   sudo timeout 110 tcpdump -ni eno1 -s0 -w /tmp/sip3.pcap 'port 5060 or portrange 10000-10100'
   ```
2. **Terminal B: place the call** (immediately after starting Terminal A):
   ```bash
   docker compose run --rm --entrypoint python -v ./scripts:/app/scripts posivex_api scripts/call.py --to +19432094556 --name "Khizar" --business "Apex Solutions" --wait
   ```
3. **On the phone:** answer, say a few words, wait about 15 seconds, then hang up. Note how many seconds after dialing you answered.
4. Wait for Terminal A to finish, then analyse:
   ```bash
   # Signaling timeline
   sudo tcpdump -nr /tmp/sip3.pcap port 5060 -A 2>/dev/null | grep -E "^[0-9:.]+ IP|SIP/2.0 [0-9]|^INVITE|^ACK|^BYE|^CANCEL" | cut -c1-140

   # Audio packets, per direction
   sudo tcpdump -nr /tmp/sip3.pcap 'udp portrange 10000-10100' 2>/dev/null | head -5
   sudo tcpdump -nr /tmp/sip3.pcap 'udp portrange 10000-10100' 2>/dev/null | awk '{print $3, ">", $5}' | sed 's/\.[0-9]*:$//' | sort | uniq -c
   ```

### Expected good result

```
INVITE      (out)
SIP/2.0 407 (in)  → INVITE again (out)
SIP/2.0 100, 180, 183 (in)
SIP/2.0 200 OK (in)       <- the missing message
ACK         (out)         <- acknowledging it
... RTP packets in BOTH directions between 175.107.203.4:100xx and 50.114.144.x ...
BYE         (either side, when you hang up)
```
And in the application log (Step 9) you will see `SIP callee answered`, an AMD result, and the greeting.

---

## Step 7: How to read the result

| What the capture shows | Diagnosis | Next action |
|---|---|---|
| `200 OK` **and** `ACK` **and** RTP in both directions | Network is fixed | Go to Step 9 |
| `200 OK` arrives, **no `ACK`** leaves | The container received it but cannot reply (routing/advertised address problem) | Capture inside Docker (`-i any`), check `use_external_ip` and the `Contact` header, send me the pcap |
| `200 OK` retransmitted several times, no `ACK` | Telnyx is sending it; we are not acknowledging | Same as above |
| **No `200 OK`** but Step 4 passed | Telnyx sent it somewhere else (different IP or port) | Do Step 8 (Telnyx call flow) and note the exact address it used |
| `200 OK` and `ACK` fine, **no inbound RTP** | Media path blocked or wrong media IP | Re-check the `10000-10100/udp` forward; confirm `c=` in our SDP is the right public IP |
| Inbound RTP but **no outbound RTP** (or the reverse) | One-way audio: asymmetric NAT or ALG | Disable SIP ALG again; check the router's UDP NAT timeout and symmetric behavior |
| Everything missing again and Step 4 failed | Reachability problem persists | Back to Steps 1-4 |

---

## Step 8: Read the Telnyx call flow (authoritative view of what Telnyx did)

**Why:** the packet capture shows what reached us. Telnyx shows what it sent, and to where.

1. In the Telnyx portal, open the debugging/reporting area for calls (the SIP call flow or "SIP ladder" view). The exact menu name depends on Telnyx's current UI.
2. Find the call by time or by SIP call-id. For the earlier failing attempt the SIP call-id was `UletcjPJqmcPI3SJE7afaoLXmIfB`.
3. Record:
   - Did Telnyx send a `200 OK`? At what time?
   - To which IP and port?
   - Did it receive an `ACK`?
   - Which IP does it send media from, and to which address/port does it send it?
   - The hangup reason and time.
4. Compare the destination of the `200 OK` with `175.107.203.4:5060`.

If Telnyx sent the `200 OK` somewhere other than `175.107.203.4:5060`, send us the exact address. That would mean the SIP service is advertising the wrong address and we will look at the SIP service configuration.

---

## Step 9: Verify the application once audio works

After the network is fixed, run one more call and watch the agent:

```bash
docker compose logs -f --tail=0 posivex_agent | grep --line-buffered '"logger": "posivex'
```

Expected sequence:
1. `Starting agent session for call_id=…`
2. `Waiting for callee to join the room...`
3. `SIP callee answered (phone_+…)`
4. `AMD result: human …`
5. `Speaking first message: 'Hi, this is Amelia from Clover Posivex…'`
6. FSM transition lines as you talk
7. At the end: `Finalized call id=… outcome=…`

Check the database:
```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT status,call_outcome,termination_reason,duration_secs,total_cost FROM calls ORDER BY created_at DESC LIMIT 3;" -c "SELECT role,left(message,70) FROM transcripts ORDER BY id DESC LIMIT 10;" -c "SELECT id,status,attempts,last_error FROM webhook_outbox ORDER BY created_at DESC LIMIT 3;"'
```
Expected: `completed` with a real outcome, transcript rows for both roles, and an outbox row marked `delivered`.

---

## Step 10: Fallback options if the router path cannot be fixed

Only use these if Steps 1-5 prove the site cannot receive inbound UDP.

1. **Run the SIP service on a public VPS** (a small cloud server with a public IP and the same ports open). The agent and API can stay where they are and connect to it. This is the most reliable option for networks behind CGNAT. It is a deployment change and needs its own plan.
2. **Ask the ISP** for a public (non-CGNAT) address or for the inbound `5060` block to be lifted.
3. **Switch the outbound trunk to TCP signaling.** This would remove the need for inbound UDP `5060`, because replies come back over the same TCP connection. It does **not** solve inbound audio, which still needs an inbound UDP path. LiveKit's documentation does not list the available transport values, so this has not been verified. Do not try it without checking the documentation first.
4. **Move the SIP port** (for example to `5080`) if the ISP blocks `5060`. This changes `sip.yaml` and the published ports and has not been verified against Telnyx. Only try this after confirming the ISP block.

---

## Appendix A: Command cheat sheet

```bash
# Place a test call
docker compose run --rm --entrypoint python -v ./scripts:/app/scripts posivex_api scripts/call.py --to +19432094556 --name "Khizar" --business "Apex Solutions" --wait

# End any stuck rooms/calls immediately
docker compose run --rm --entrypoint python -v ./scripts:/app/scripts posivex_api scripts/end_calls.py

# Watch the services
docker compose logs -f --tail=50 posivex_agent
docker compose logs -f --tail=50 posivex_api
docker compose logs -f --tail=30 sip livekit-server

# SIP lines for one call (replace the call id)
docker compose logs --since 30m sip | grep conv_XXXXXXXXXXXX | grep -E "INVITE|ringing|answered|failed|Closing|reason"
```

## Appendix B: Information to send back after each step

Please paste these so the next decision does not rely on guesswork:

1. Output of Step 1 (`ifconfig.me` address and the router's WAN IP).
2. Router model and ISP name.
3. Result of Step 4: which test packets arrived and which did not.
4. The output of the Step 6 analysis commands.
5. The Telnyx call-flow details from Step 8 (where the `200 OK` was sent).
6. How many seconds after dialing you answered.

## Appendix C: Warnings in the logs that are not the cause

- `failed to get server reflexive address udp6 stun:…` (the IPv6 STUN lookups fail; IPv4 works).
- `event loop blocked for … ms` at session start (the VAD model loads synchronously; a minor performance note).
- `elevenlabs text-to-dialogue returned error … input_timeout_exceeded` (an idle text-to-speech connection timing out while waiting for the call to be answered).
- `Unclosed client session` in the API log (a small resource leak when dialing).
- `llm model gemini-3.5-flash-lite hasn't been evaluated … for amd` (a LiveKit compatibility notice).
