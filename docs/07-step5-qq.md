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

The adapter subscribes to `GROUP_MESSAGE_CREATE`, including non-mention messages. It ignores bot-authored and unrelated events, maps the QQ message ID to a stable event ID, retains limited attachment metadata, and uses the group OpenID as the conversation and reply destination. Repeated gateway delivery reuses the persisted event and publication idempotency key.

The QQ path does not authorize DSH execution. `qq-serve` uses the default workflow policy, which records a proposal for operator review. QQ sending is disabled unless `QQBOT_ALLOWED_PUBLICATIONS` explicitly lists `group_update` or `urgent_escalation`. The allowlist only controls those publication dispositions; it does not enable task execution.

## Configuration and start

Copy `runtime.env.example` to a private runtime configuration file and set `QQBOT_APP_ID` and `QQBOT_CLIENT_SECRET`. Leave `QQBOT_ALLOWED_PUBLICATIONS` empty to keep all outbound group messages suppressed. To enable a specific disposition, set for example `QQBOT_ALLOWED_PUBLICATIONS=group_update`.

Install dependencies and start the adapter after DSSuperAgent is reachable on loopback:

```powershell
python -m pip install -e .
python -m app --data-dir "$HOME/.local/share/company-agent" qq-serve --config "C:/private/company-agent/runtime.env" --dssuperagent-url http://127.0.0.1:3082
```

The process obtains and caches an app access token, gets a Gateway URL, identifies with the group-message intent, maintains heartbeats, and resumes from the last session cursor when QQ permits. Gateway or API failures are reported without printing credentials. Failed Outbox deliveries remain retryable and are retried on a later event or process startup. The active publication allowlist is checked again before every send; queued messages are cancelled if their disposition has since been removed. A process interrupted in `sending` has an unknown delivery result and is intentionally not retried automatically. Inspect it with `python -m app --data-dir <data-dir> outbox list`, verify the QQ group, then use `outbox resolve <outbox-id> --delivered` or `--retry`; both choices are audited.

## Validation status

Unit tests cover event normalization, non-mention events, malformed payloads, bot-message filtering, config allowlisting, idempotent publication, Outbox delivery and failure retry, and Gateway identify/resume payloads. These are local tests with a fake workflow and fake QQ sender. They do not claim that this checkout has connected to the configured QQ application or sent a real group message; that remains a live acceptance step.
