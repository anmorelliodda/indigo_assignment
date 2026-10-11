# Durable real returns

`POST /returns`, operationId `manage_return`, is the only return tool. Required: customer-supplied `order_number`, purchase `email`, and `operation` (`options`, `prepare`, `approve`, `cancel`, `status`). `lines` and `reason` belong only to prepare; current 32-hex `draft_id` only to approve/cancel. Other-phase fields and model consent/token/message fields are rejected.

## Verification and phases

Every operation verifies the actual SOAP order email, including cancellation and terminal reads/replays. An optional paired deployment restriction can limit orders, but is unset on the current service. Visitor IDs never authorize or restrict these operations: a customer can continue from a phone. Number/email knowledge is a limited check, not authenticated customer identity.

1. options reads actual delivery, documented inclusive 30-day return policy and available order lines/reasons. Ineligible orders do not create a proposal. null existing_state means choices must be collected; prepared means an existing proposal; final/uncertain state means read the previous result.
2. prepare independently validates actual order lines, quantities, delivery and reason. It creates one operation row, a new draft and 15-minute expiry, then returns the summary. Identical valid choices/mode can reuse the proposal. A modification or expiry renewal produces a new draft/summary.
3. Show the returned summary, clearly stating real or disabled sending. Wait for a NEW whole message CONFERMO or ANNULLA, without trimming, case conversion or interpreting a longer sentence. Do not prepare and decide in the same turn.
4. approve validates message/draft/expiry and actual eligibility, conditionally saves submitting, reads back its durable claim and calls the one-attempt SOAP sender. Only the CAS winner sends.
5. Valid SOAP OK + bounded nonempty IdReso + successful final persistence produces submitted/return_id. No refund promise. Errors/timeouts/invalid response or ambiguous persistence produce unknown; a crash may leave submitting. Neither reopens or automatically retries.
6. cancel only cancels an unsent prepared proposal. A new explicit request in a different non-decision turn can create a new proposal; the complete cancelled record is retained in cancelled_history. History-size limit fails without deleting evidence. Existing submitting/submitted/submission_unknown never reopen.

Change a prepared reason directly with prepare; do not cancel first. Old drafts cannot authorize modified/new proposals. A stale prepare losing ETag is not retried over a concurrent cancellation. status verifies SOAP before reading Azure, so a slow SOAP read does not return an earlier snapshot. Like every read, its response can become stale after the read. Avoid parallel status/decision calls; report a successful decision without more mutations in that turn.

For options/status, send only operation, order_number and email. A read-schema HTTP 422 permits correcting and retrying the read, never retrying approve. A request for the current order state requires a fresh get_order_status call. A submitted replay with decision_recorded=false means already created: explicitly report that no new send occurred. These prompt corrections are published; their latest online verification is still pending.

`not_prepared` is an ephemeral status after valid order verification with no operation entity; it is not persisted and does not mean wrong credentials. Cancellation, expiry and old preview approvals never modify an order in SOAP. Time passage does not delete the row or change its ETag.

## Consent transport

Processing start copies the original platform message ID/text before routing and sets `chat_user_message_b64={{last_user_message | base64_encode}}`. Headers:

```text
X-API-Key: [[TOOL_API_KEY]]
X-Chat-Message-Id: {{chat_message_id}}
X-Chat-User-Message-B64: {{chat_user_message_b64}}
```

Strict Base64/UTF-8 decoding, valid ID syntax and freshness relative to the preparation are enforced. These headers are connection context, not cryptographic proof of who typed. API key authenticates the platform, not the buyer. Button/Quick Reply provenance needs separate acceptance; ordinary typed text is the verified path. Only Andrea performs real-send CONFERMO acceptance.

## Durable duplicate prevention

An Azure Table operation has PartitionKey derived from the six-digit order and RowKey=operation. create_entity uniqueness protects first creation. update_entity with IfNotModified compares the opaque Azure ETag and writes atomically. Read checks alone are insufficient; the conditional claim is the arbitration point. Readback occurs before network creation. A failed or uncertain claim acknowledgement prevents the sender.

There is no atomic Azure/SOAP transaction, legacy idempotency key or precise correlation recovery endpoint. This is a conservative one-attempt design, not exactly-once delivery. Do not delete/reset submitting/unknown rows to retry. Manual reconciliation is required. ST07 signals some return in progress, not that our request succeeded, and cannot recover our IdReso. Independent external actors are outside this service's concurrency guarantee.

## Server configuration and acceptance

Use Entra/managed identity for Azure Table data; never storage account keys. `RETURNS_ENABLED=true` initializes storage. A dedicated real table (ReturnOperations) avoids consuming prior preview/sandbox consent. Historical evidence was privately backed up before authorized removal of obsolete preview/chat sandbox tables and disabling their apps. Names containing sandbox and synthetic delivery flags fail startup.

`RETURN_SUBMISSION_ENABLED=false` is the safe default. It prevents service submission and independently blocks SOAP creation at the client transport before login/HTTP. In this mode exact approve only saves a preview message ID and returns submission_stopped=true, state=prepared, decision_recorded=false; no definitive approval or SOAP creation.

Enable true only with explicit authorization for real SOAP creation. `RETURN_ALLOWED_ORDER` / `RETURN_ALLOWED_EMAIL` are an optional paired deployment restriction, not a prerequisite for enabling sending. Leave both empty for all eligible orders; setting only one fails startup. Actual SOAP email verification is always mandatory. A preview draft/mode or previous preview confirmation cannot authorize a real send; prepare a new real-mode draft, show it and wait for another confirmation. The model cannot change this configuration through a JSON body.

The final live release has real sending enabled for all eligible orders, with the initial single-order restriction removed at Andrea's request. Existing preview proposals stay unchanged until the customer explicitly requests a new real proposal; no migration approves or sends them. Status/options do not mutate them. Andrea's supplied Live traces verified cancellation across chats, a new real proposal, real creation and replay with the same return ID and no new send. This is targeted acceptance, not full regression.

Use simulated storage and fake SOAP/MockTransport for ALL assistant tests. Do not call live order/return endpoints, even for read-only testing. Only inspect deployment configuration; Andrea performs the online chat tests. Andrea performs any further real-write acceptance after reviewing the live real-send summary. Test/Production tool secret values and the context workflow must be published/configured consistently; publishing the workspace promotes all flows.
