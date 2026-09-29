# Safe Scribe by TrustEdge AI — phase 1: the case room

Safe Scribe is designed to turn a nurse-to-nurse shift handoff into a Band case room where agents
on Crusoe review evidence before approval. Phase 1 implements the spine only: Desk, Scribe, Critic and the human charge
nurse in one Band room, with deterministic ownership and identifier checks and an approval that names the exact brief
revision. Downstream execution is not connected to this phase.

**Every patient in this repository is synthetic.** Names, dates of birth, record numbers and phone
numbers in `hallway/fixtures/` are invented (see `hallway/fixtures/README.md`). Say so on screen.

## Verified status

Real Crusoe function calling has passed. Band message parsing, case creation and case reading
were verified in `550fdd7`. With `cfb1268`, live case `4be3e276-d248-4558-878d-6678b75f4ec8`
produced a BRIEF at 12:01:40, a VETO at 12:01:45, and an OWNER_REQUEST for `fu_call_daughter`
(message `0d2a7a40-2f8f-4951-b0a2-a0740823f462`). It is awaiting Erik's real reply; no approval
has been observed. This case's observer also expired after 90 seconds while awaiting the human
reply. The agents remain connected and a late reply can complete the case, but this run failed
the timing requirement.

Setting `reasoning_effort=low` for GLM only restored generation within the existing 10-second
request timeout; model pins and timeouts were unchanged. The earlier GLM/Kimi retry timeouts
and failed 90-second demo remain historical failures. The complete live demo is still not green.
The offline suite has **58 passing tests** (53 product + 5 smoke); those tests do not establish
live end-to-end success.

## What phase 1 is, and is not

| Is | Is not |
| --- | --- |
| Band is the only coordination channel: transcript, brief, owner request, verdict and approval are all Band messages, authenticated by sender id. Delete Band and there is no room, no roster, no veto. | A completed live workflow. BRIEF, VETO and OWNER_REQUEST are now observed; the case awaits a real human reply and has no approval yet. |
| Critic checks are code, explained by the model: every quote is a normalized substring of the transcript; every pending follow-up has an owner; no direct identifier from the transcript appears anywhere in the outbound JSON. | A de-identification system. The identifier guard is a regex set plus an identifier list pulled from the transcript. It is tuned to the synthetic fixtures. It is not HIPAA anything. |
| Tests exercise ownerless follow-up and identifier rejection before revision-bound approval. Live veto count/order depends on the actual extraction; no errors are fabricated to stage a second veto. | The research room, the approved (boundary) room, Grapher, Closer, Neo4j lineage, Brave. Those are phases 2 and 3 (JV-106, JV-107). The agent entrypoints exist, but their downstream execution is disabled in phase 1. |
| An owner for an unowned follow-up comes only from a human's message in the room, recorded with that message id as provenance. Otherwise the item stays `unresolved` and the approval lists it. | Auto-assignment. The model never invents an owner. |
| Fail closed: Crusoe model A, then Crusoe model B, then "inference unavailable, case paused" posted to the room. | Any external provider for agents that hold the transcript. |

## Architecture (phase 1)

```
  presenting laptop                          Band (app.band.ai)
  -----------------                          -----------------------------------------
  fixtures/handoff_2.txt  (or .wav ->        case-<slug> room
  faster-whisper, phase 2)                     participants: Desk, Scribe, Critic, human
        |                                      |
        v                                      |  TRANSCRIPT   (Desk -> @Scribe @Critic)
      Desk ---- creates room, posts ---------> |  BRIEF rev N  (Scribe -> @Critic)
      assigns pseudo_id on the laptop          |  OWNER_REQUEST(Scribe -> @human @Critic)
      (salted hash; mapping never leaves)      |  human reply  @Scribe @Critic "I'll own it" / "/own <id> <name>"
                                               |  VERDICT      (Critic -> @Scribe)  VETO with reasons
                                               |  BRIEF rev N+1 ...
                                               |  VERDICT APPROVE <rev> + APPROVAL (Critic)
                                               |
                                               |  nobody else is in this room
  every agent's brain: Crusoe Managed Inference (ids from scripts/check_crusoe_tools.py, never guessed)
```

## Sponsor tools in phase 1

| Tool | Phase-1 role | Code | State at this commit |
| --- | --- | --- | --- |
| Crusoe | inference for Desk, Scribe, Critic | `hallway/common/llm.py` | live function calling and brief generation passed after GLM-only reasoning adjustment (`cfb1268`) |
| Band | room, roster, messages, events, gate | `hallway/common/room.py`, `hallway/common/runtime.py` | live case read/write, BRIEF, VETO and OWNER_REQUEST observed; awaiting human reply, no approval |
| Neo4j, Nebius, Brave, OpenRouter, Vultr | none in phase 1 | deferred | see `docs/hackday/integration-ledger.md` |
| Merge.dev | cut at the pivot | none | not attempted |
| Plaud, DuploCloud, UserTesting | cut | none | not attempted, need a device or a provisioned tenant |

The ledger in `docs/hackday/integration-ledger.md` tracks verified / mocked / attempted / deferred
integrations. Passing isolated live calls does not mean the complete demo works.

## Run

Run these commands from the repository root.

Optionally set `CRUSOE_DISABLE_THINKING_MODELS` to comma-separated exact model IDs verified to
support `enable_thinking=false`; its blank default leaves model behavior unchanged.

```sh
make install
make check                      # offline protocol tests, no network
doppler setup                   # ai-hackday-2026 / dev; see docs/hackday/secrets.md
doppler run --no-fallback -- make demo         # real Crusoe + Band credentials required; red otherwise
```

Desk runs on the presenting laptop in every topology, never on the Vultr VM, so audio stays local. Text is explicitly sent to Band and Crusoe. Start Desk with
`doppler run --no-fallback -- .venv/bin/python -m hallway.agents.desk`; start Scribe and Critic in separate terminals with
`doppler run --no-fallback -- .venv/bin/python -m hallway.agents.scribe` and `doppler run --no-fallback -- .venv/bin/python -m hallway.agents.critic`. The
compose file deliberately omits Desk; it carries Scribe, Critic and, in later phases, Grapher and Closer.

For a deliberately offline unit-test harness, run:

```sh
MOCK_BAND=1 MOCK_CRUSOE=1 make demo
```

It prints `OFFLINE HARNESS`, runs synthetic protocol tests against a Band double and mocked
model responses, and explicitly reports no live sponsor evidence. It does not process the selected
fixture as a live end-to-end run. Mixed mock/live flags are rejected; a failed live run never
switches to this harness. Set both flags to `0` for live operation.

Doppler supplies `CRUSOE_API_KEY`, exact model IDs and `BAND_<ROLE>_AGENT_ID` /
`BAND_<ROLE>_API_KEY`. Each process needs its own API key and the three case-role IDs. Legacy
`agent_config.yaml` remains an optional local fallback when environment credentials are absent.
A partially configured environment credential pair fails closed. The cloud Compose configuration
passes each service only its own Band key; no secret-file mount is required. To avoid Docker
Compose reading a legacy `.env` during interpolation, start cloud services with
`doppler run --no-fallback -- docker compose --env-file /dev/null up -d`. Docker deployment
has not been verified in this checkout. `--no-fallback` disables Doppler secret-cache files.

Create a Band lobby containing Desk and the human operator; copy its ID to `BAND_LOBBY_ROOM_ID`.
`make demo` requires `BAND_HUMAN_API_KEY` to send the authenticated intake request. Alternatively,
run `doppler run --no-fallback -- .venv/bin/python -m hallway.demo --watch-only` and follow the printed command in Band,
mentioning Desk. Reply to owner requests mentioning both Scribe and Critic so both can verify the
human message. The full live `make demo` remains red until lineage and the approved room are
implemented, even if this phase's case approval succeeds.


## Fixtures

From `hallway/fixtures/README.md` (Erik's PR #9): `handoff_1` prior encounter, all follow-ups owned;
`handoff_2` the demo (name + DOB, warfarin + ciprofloxacin, unowned daughter call); `handoff_3`
boundary stress (spoken MRN and phone, one unsupported claim, unowned nutrition consult).
Agents read `fixtures/<name>.txt` by name; `handoff_2` is the default.

## Attempted / cut

- Merge.dev: cut at the pivot, no healthcare fit.
- Plaud, DuploCloud, UserTesting: not attempted; device or provisioned tenant required.
- Emit.THOUGHTS: not supported by the Band LangGraph adapter (`SUPPORTED_EMIT` is tool calls and
  usage); explicit `thought` events are posted through `band_send_event` at each protocol step instead.


### Opt-in approved room and Grapher (JV-106)

Set `ENABLE_APPROVED_ROOM=1` on Critic and configure the Grapher peer ID; run the
Grapher process separately. Default remains the case-only phase 1 behavior. After
approval, Critic creates a separate `Safe Scribe approved <id>` room containing
Critic, Grapher and the initiating human. Only the redacted approved brief crosses;
Grapher never joins the case room. Closer recruitment is not part of this slice.

Grapher calls the existing graph store with the case room ID as the stable encounter
ID. `MOCK_NEO4J=1` remains explicitly mock and posts `MOCK_GRAPH_WRITTEN`. Without
mock mode, missing `NEO4J_URI` fails closed instead of silently using memory. A mock
receipt never suppresses a later real write. Actual Neo4j operation still requires
live verification; tests mock the graph API and Band transport.

The manifest records observed, authenticated Desk intake, Scribe extraction and
Critic review messages. It is **processing provenance, not delivery/read proof**.
No identifier-field access edges are invented; the real `who_saw_identifiers()`
result may be empty and is labeled as a global query across all encounters. The
existing graph store retains agent/field/purpose/timestamp but not source message
IDs; those remain in the Band manifest. Runtime-generated ISO processing timestamps
are appended after identifier checks to avoid confusing transport dates with DOBs.

Band checkpoints resume room delivery on retry and graph writes use stable MERGE
keys. A crash immediately after room creation can leave an empty orphan room before
its checkpoint exists. A graph call timeout emits no success receipt; its worker
thread may still complete, so a later retry can safely rewrite the same encounter.

The live demo observer now waits for an authenticated boundary checkpoint, matching Critic approval, and a real `GRAPH_WRITTEN` receipt with the actual lineage query result. Mock graph receipts never pass. Success is labeled **phase 2 verified**, not completion of Researcher, Closer, or event submission requirements.
## Drug-only research helper (JV-107)

`hallway/common/research_room.py` recruits Researcher into a separate Band room only for
supported, transcript-backed medication names. The request carries those names and an opaque
routing UUID, never the transcript or patient identity. Scribe validates and relays source URLs
back into the case; Critic waits for that relay or an explicit failure. The small medication
vocabulary skips unsupported names explicitly. Recruitment resumes from a Band checkpoint.

Brave results count as live evidence only when their own metadata says `mock: false` and
`source: brave`; missing keys and mock results produce no evidence. Eleven focused offline tests
cover the boundary and retries. Runtime tool wiring and a live end-to-end research run remain
pending; this helper alone is not sponsor-demo proof.

New Scribe publication and Critic review tools record the identifier field categories they processed against the authenticated Desk source message. The boundary verifies these runtime records and emits only field labels and evidence IDs. This is tool/runtime processing evidence, **not human reading or Band delivery measurement**. Legacy or mismatched metadata remains unverified and blocks full phase-2 observer success. Detection is conservative and does not certify complete identification. The graph query is global; current-case proof comes from the matching approved manifest.

Set `ENABLE_DRUG_RESEARCH=1` on Scribe, Critic and Researcher to activate the separate drug-only room. Runtime tool wiring now starts recruitment after Scribe publishes, dispatches research-room messages to the relay, and prevents Critic approval until an authenticated result or explicit unavailability arrives. Mock search facts are never propagated as evidence. Live research verification remains pending.
