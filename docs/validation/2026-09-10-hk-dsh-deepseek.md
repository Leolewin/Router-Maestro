# HK deployment and real DSH TUI validation — 2026-09-10

## Result

The plugin-boundary changes are deployed to the **HK main instance**, and live
DeepSeek generation, real DSH TUI sessions, and the Files API lifecycle passed.
This is an explicitly tagged validation build of the dirty checkout, not a new
release. No commit, push, package publication, or client upgrade was performed.

Endpoint: `https://ai-hk.likanwen-niuma.online`.

- Running image: `router-maestro:hk-plugins-f72fef0e-20260910`.
- Docker image ID: `sha256:e90a39bf808fea9461d38cdd8c4dcd39879a93f61917e0e3f276a34e61695bc7`.
- Base commit: `5d191de2b1ca82b49e4b3fdf8bae39a53fa8a512`.
- Branch: `users/kanwli/provider-plugin-boundaries`; package version `0.9.6`.
- Source SHA-256: `f72fef0ef54398fab91ab231178df23d1c7ea37a60f00eb54f8048a2e4a5679d`.
  Recipe: sorted package-relative Python paths, each followed by a NUL and its
  file bytes. All **146 installed Python files** match the validated checkout.
- Main container healthy, restart count `0` after testing. Traefik,
  `router-maestro-deepseek-test`, and `star-office-ui` retained their container
  identities, images, and start times. Base Compose, `.env`, and mounts unchanged.

See [structured evidence](2026-09-10-hk-dsh-deepseek.evidence.json) for sanitized
request IDs, selected transports, client session IDs, Files results, and checks.
The earlier [GPT-only local report](2026-09-10-gpt-live.md) covers a previous
source fingerprint and is not counted as a test of this final HK image.

## DSH real interactive sessions

Installed clients: DSH `0.1.2-rc.1`, dsh-TUI `0.10.0-beta.5`, Node `22.22.0`.
Each sequence ran in one real PTY-backed TUI, with a fresh conversation and
unique token; these were not headless single-prompt calls or API simulations.

| Final TUI sequence | Remember / ACK | Recall | Local file tool | Read-only MCP tool | Recall after tools |
|---|---|---|---|---|---|
| DeepSeek V4 Pro, native DSH adapter → Chat | PASS | PASS | PASS | PASS | PASS |
| DeepSeek V4 Pro, pi-ai adapter → Responses | PASS | PASS | PASS | PASS | PASS |
| GPT-5.6 Luna, pi-ai adapter → Responses | PASS | PASS | PASS | PASS | PASS |

Each tool round used exactly one `bash` or `mcp__qmd__status` call. Bash
created, verified, and deleted only its unique test file. MCP performed a status
query only, with no document retrieval. Both tool results were successful in
every session; original tokens survived both tool-result continuations.

Final answers matched the requested acknowledgement, token, and tool-pass text.
Some clients also emitted a progress heading before a tool call; those are
recorded separately from the post-tool final answer. All final sessions ended
with five completed turns and normal `/exit`, exit code `0`.

The first DeepSeek Responses diagnostic session completed all functional rounds,
but prepended a progress heading to its first `TURN1 ACK`. That exact-match check
is recorded as **failed**, not silently normalized. A new session, new token,
and explicit “no heading or preamble” prompts passed the full five-round sequence
on the same image, without code or model changes. The diagnostic session also
exited normally. Four real sessions ran in total, including that diagnostic.

GPT requested one sandbox escalation for its exact test-file command, although
the path was within the intended scratch workspace. The command was inspected
and approved **once** through the TUI. No persistent approval policy changed.

Client isolation:

- Separate `DSH_HOME`, sessions, scratch project, and model catalog for each path.
- Explicit model/provider pairs; no auto-router substitution or default-model leak.
- Context capacity from HK's live catalog: DeepSeek `1,000,000`, GPT `1,050,000`.
- Existing TUI and escalation-fix packages reused without installation or editing.
- The installed TUI has no data-directory override for its preference/history
  constants. A process-local Node loader hook redirected **only those two path
  constants** into the isolated DSH directory. It did not change protocol,
  request, model, tool, or rendering logic, and did not modify installed files.
- Telemetry and auxiliary LLM title generation disabled in test-only config.
- Hashes of 13 existing DSH configuration/state/package files were unchanged.

## DeepSeek live protocol coverage

Models were selected from the deployed HK catalog, not an assumed catalog.

| Model / test | Observed upstream | Result |
|---|---|---|
| V4 Pro Responses, non-streaming | DeepSeek Responses, identity | HTTP 200; exact text; completed |
| V4 Pro Responses, streaming TUI with tool continuations | DeepSeek Responses, identity | All final rounds pass |
| V4 Pro Chat, non-streaming | DeepSeek Chat, identity | HTTP 200; exact text; stop |
| V4 Pro Chat, native DSH streaming TUI | DeepSeek Chat, identity | All final rounds pass |
| V4 Pro Anthropic Messages | DeepSeek Messages, identity | HTTP 200; exact text; end_turn |
| V4 Pro Gemini generateContent | DeepSeek Chat, semantic IR | HTTP 200; exact text; STOP |
| V4 Pro Gemini streamGenerateContent | DeepSeek Chat, semantic IR | HTTP 200; exact text; STOP |
| V4 Pro Anthropic count_tokens | `/anthropic/v1/messages/count_tokens` | HTTP 200; native count `84` |
| Catalog alias `deepseek/deepseek-flash`, Chat | DeepSeek Chat | HTTP 200; exact text |
| GPT-5.6 Luna Responses control | Copilot Responses, identity | HTTP 200; exact text |

Audit contains **36 inference/count requests**, all HTTP 200 with
`explicit_terminal/completed`. There are 35 selected generation attempts; native
token counting has its own upstream request, not a generation attempt.

The **28 DSH requests** comprise four sessions × seven requests: five user turns
plus two tool-result continuations per session. Each has exactly one selected
attempt, with no failed replay, provider fallback, or unexpected transport switch:

- DeepSeek Responses: 14 requests (initial diagnostic plus fresh final session).
- DeepSeek Chat: 7 requests.
- Copilot Responses: 7 requests.

The two Gemini requests explicitly record `semantic_ir` and `ir_materialized=true`.
The native paths record `identity` and `ir_materialized=false`. The earlier
local Codex audit cancellation-classification warning was not reproduced here;
this test does not claim that separate issue is fixed.

## Files API lifecycle

Both plugin paths were tested over the deployed public HTTPS endpoint:

- `/api/openai/v1/files` and `/{file_id}`.
- `/api/providers/deepseek/v1/files` and `/{file_id}`.

Final validation used a generated, CRC-correct, 1×1 RGBA PNG with
`purpose=user_data`. For each upload path:

1. Unauthenticated collection access was denied with HTTP 401.
2. Upload returned HTTP 200 and a new `file-api-*` ID.
3. Both aliases listed the file and returned matching filename, ID, and byte count.
4. The other alias deleted it with HTTP 200 and `deleted=true`.
5. Both aliases then omitted it from listings and rejected its deleted ID.

The real DeepSeek API uses **HTTP 400**, `invalid_request_error`, and
`application/octet-stream` for a deleted ID, with the message
“file_id does not exist or is not created under your account”. RM preserves this
upstream status/body/content type; it does not rewrite the error to 404. Offline
regressions now cover this response as well as quota errors and header filtering.

Two test-harness corrections are preserved in the evidence:

- The initial deletion check incorrectly expected 404. The file had been deleted;
  an explicit follow-up confirmed absence through both aliases. The test was
  corrected to assert the observed provider contract.
- The initial base64 PNG sample had an invalid IDAT CRC. Those calls demonstrated
  opaque file transfer but not a valid image fixture. Both full lifecycles were
  rerun using the generated CRC-correct PNG; only these are the final fixture cases.

Exactly **five remote files** were created across diagnostics and final tests.
All five were independently rechecked as absent through both aliases. No
pre-existing file was deleted. Files requests did not invoke a model or use
generation routing. Image understanding, automatic DSH image offload, quota
exhaustion, and the unlisted vision model were not exercised.

## Deployment-time compatibility fixes and offline gates

The initial candidate was checked against the actual HK dependencies before
switching production. FastAPI `0.141.1` uses lazy included routers, unlike the
locked local FastAPI `0.128.0`. This exposed two route-inspection assumptions:

- Plugin/core collision detection did not inspect lazy included routers.
- CORS preflight metrics could label known paths as `unmatched`.

The deployed fix inspects prefix-aware effective route contexts, including nested
routers and hidden routes. Regression tests also verify noncolliding HTTP methods
and low-cardinality Files path templates. The main service was switched only
after these checks passed.

| Gate | Final result |
|---|---|
| Locked environment: Python 3.14.0, FastAPI 0.128.0, Starlette 0.50.0 | 4,077 tests passed |
| HK-compatible FastAPI 0.141.1 / Starlette 1.6.0 dependency overlay | 4,077 tests passed |
| Controlled integration suite | 25 tests passed |
| Ruff check | PASS |
| Ruff format check | PASS |
| BasedPyright 1.39.10 | 0 errors, 0 warnings |
| Built image dependency check and installed-source parity | PASS |

The HK-dependency pytest run emits one Starlette deprecation warning about its
httpx test client; no behavior test failed. Actual HK runs Python `3.14.7` and
Pydantic `2.13.5`; the local overlay is not claimed to be an identical Python
runtime. Installed-image construction and live tests validate that runtime.

## Rollback and cleanup

Only the main `router-maestro` service was recreated, using a deployment-specific
Compose override outside the remote Git checkout. The checkout remained on its
original clean `master`; base Compose and `.env` were not edited.

Deployment directory:
`/home/likanwen/rm-deployments/hk-plugins-c390df2b-20260910`.
The directory name identifies the initial candidate; the active image and its
manifest use the final `f72fef0e` fingerprint.

Rollback image: `router-maestro:hk-rollback-20260910-d270e70e`.
Protected original config/data/Compose/environment backups and a rollback
override are retained in that deployment directory. Rollback, if requested:

```bash
cd /home/likanwen/Router-Maestro
docker compose -f docker-compose.yml \
  -f /home/likanwen/rm-deployments/hk-plugins-c390df2b-20260910/rollback.compose.yml \
  up -d --no-deps --pull never router-maestro
```

Use the corresponding `candidate.compose.yml` override when recreating this
validation build. Running base Compose alone would select its original image
reference; this deployment has not changed the published `latest` tag.

Audit was temporarily enabled with a dedicated trace directory and restored by
revision-aware update. Final audit is `{"enabled": false, "trace_dir": null}`;
the **original entire configuration revision** was restored. Test-only audit
evidence is retained with a mode-0700 parent directory, separate from existing
traces. All four local probe files and all five remote test files are absent.
The local 1.4 MiB isolated test directory was moved to macOS Trash after the
sanitized report and evidence were saved; its original path is absent and the
data remains recoverable. Remote rollback and protected audit artifacts remain.
