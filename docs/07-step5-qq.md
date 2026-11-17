# Step 5: QQ channel event path

## Purpose

Connect the QQ Bot Platform directly to Company Agent's channel adapter and the established event workflow. QQ group events become standard Company Agent events, enter the persisted control plane and DSSuperAgent workflow, and any external publication is decided and stored by Publication Decision and Outbox.

## Event and execution boundary

```text
QQ Gateway
  -> QQ Channel Adapter
  -> Event Envelope + Control Plane
  -> DSSuperAgent Adapter / Workflow
  -> proposed Work Item and saved publication recommendation
  -> explicit QQ publication allowlist
  -> Publication Decision + Outbox
  -> QQ group message API
```

The adapter subscribes to `GROUP_MESSAGE_CREATE`, including non-mention messages. It ignores bot-authored and unrelated events, maps the QQ message ID to a stable event ID, registers a stable attachment placeholder in receive order, persists the original before Agent execution, and uses the group OpenID as the conversation and reply destination. OCR/AnythingLLM processing continues in the background. Repeated gateway delivery reuses the persisted event and publication idempotency key.

When a user uploads a file and asks about it in a later message, the workflow keeps the prior message in the same local thread. Its history entry includes safe attachment metadata (stable attachment ID, filename, type, ingest status, document ID, and size), so DSSuperAgent can resolve references such as “这个文件” without receiving a download URL or secret. It can list/read the current-scope history, attachment original, or indexed document through the Company Agent knowledge bridge. The current message still receives a fresh scoped knowledge grant; DSSuperAgent decides which `attachment_id` or `document_id` belongs in a DSH proposal.

QQ passive replies are time-limited. The adapter records the reply deadline on the Outbox destination and uses `msg_id` only while the passive-reply window is open. Once it expires, the request becomes an active group send and QQ may reject it; a rejected send remains `failed` in Outbox and is not considered delivered. Long-running QQ tasks therefore require an active-send path that the platform accepts or a result path that completes within the passive-reply window. A locally `sent` Outbox record proves the API accepted the request, not that a human saw it.

By default, QQ events only create proposals; they do not authorize DSH execution. To explicitly allow QQ-proposed `dsh.task` work to execute, set `QQBOT_AUTO_EXECUTE_CAPABILITIES=dsh.task` and `COMPANY_AGENT_QQ_WORKSPACE=qq-agent`. This is a broad capability grant for QQ-originated proposals, so enable it only when that behavior is intended. DSH still receives only the task request and any explicitly authorized source files staged into its workspace; it receives no direct knowledge-base credentials or search access.

Each assistant execution writes its report below `artifacts/executions/<execution-id>/`. The persistent QQ workspace therefore does not make later tasks fail because an earlier task left a same-named report. DSH never sends QQ messages; Company Agent owns publication. For QQ tasks, the last DSH step must put the final Chinese answer, no longer than 1,500 characters, between execution-specific `COMPANY_AGENT_FINAL_REPLY_BEGIN` and `COMPANY_AGENT_FINAL_REPLY_END` markers. The DSH adapter validates the marker pair and answer length before it reports success. Company Agent sends only the marked answer; it never forwards DSH execution narration. A missing or invalid answer block fails the DSH task and produces a bounded failure notice. Each DSH step can run up to 300 seconds. If DSSuperAgent decides a message needs no DSH work (`observed`), an allowlisted `group_update` can still publish its short status text; `proposed` remains silent because no result exists yet.

QQ sending is disabled unless `QQBOT_ALLOWED_PUBLICATIONS` explicitly lists `group_update` or `urgent_escalation`. When execution is enabled and an allowed publication is selected, the final QQ message contains only the validated DSH answer block or a bounded failure reason rather than the pre-execution acknowledgement or internal execution notes. The publication allowlist does not itself enable task execution.

## Configuration and start

Copy `runtime.env.example` to a private runtime configuration file and set `QQBOT_APP_ID` and `QQBOT_CLIENT_SECRET`. The OpenAPI base URL defaults to `https://api.bot.qq.com`; set `QQBOT_API_BASE_URL` only when the platform provides a different environment-specific endpoint. Leave `QQBOT_ALLOWED_PUBLICATIONS` empty to keep outbound group messages suppressed. To enable a specific disposition, set for example `QQBOT_ALLOWED_PUBLICATIONS=group_update`. Keep `QQBOT_AUTO_EXECUTE_CAPABILITIES` empty to require manual approval; explicitly set it to `dsh.task` and choose `COMPANY_AGENT_QQ_WORKSPACE` only to enable automatic QQ task execution.

Install dependencies and start the adapter after DSSuperAgent is reachable on loopback:

```powershell
python -m pip install -e .
python -m app --data-dir "$HOME/.local/share/company-agent" qq-serve --config "C:/private/company-agent/runtime.env" --dssuperagent-url http://127.0.0.1:3082
```

The process obtains and caches an app access token, gets a Gateway URL, identifies with the group-message intent, maintains heartbeats, and resumes from the last session cursor when QQ permits. Gateway or API failures are reported without printing credentials. Failed Outbox deliveries remain retryable and are retried on a later event or process startup. The active publication allowlist is checked again before every send; queued messages are cancelled if their disposition has since been removed. A process interrupted in `sending` has an unknown delivery result and is intentionally not retried automatically. Inspect it with `python -m app --data-dir <data-dir> outbox list`, verify the QQ group, then use `outbox resolve <outbox-id> --delivered` or `--retry`; both choices are audited.

QQ event work runs off the Gateway reader so DSSuperAgent waits do not prevent heartbeat acknowledgements from being read. The default DSSuperAgent decision wait is 600 seconds. If a wait times out, the trigger and thread IDs remain persisted; a duplicate delivery resumes that same trigger instead of submitting a second one. Processing failures create a separate, idempotent Outbox notice when `group_update` is allowlisted. The notice states that no result was produced and excludes internal exception details; a later successful result uses its own publication idempotency key.

## Validation status

Unit tests cover event normalization, non-mention events, malformed payloads, bot-message filtering, config allowlisting, idempotent publication, Outbox delivery and failure retry, passive-reply expiry handling, Gateway heartbeat responsiveness during slow event handling, late-decision resumption, explicit DSH authorization, persistent-workspace artifact isolation, required final-answer marker validation, observed-run status publication, and execution-result feedback. These tests use local fixtures. The pipeline fixture now covers placeholder registration, original-first storage, explicit attachment selection, permission recheck, DSH staging, and later knowledge indexing; it does not claim that a real QQ-to-DSH task reached the QQ API. Live end-to-end acceptance requires a real uploaded attachment, successful AnythingLLM ingestion and scoped retrieval, DSH receiving a hash-verified original, a valid final-answer block, and a persisted Outbox status of `sent` whose body contains the answer but no DSH execution narration.
