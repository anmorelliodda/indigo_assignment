# Gift Shop Assistant

One assistant for **gift discovery, order enquiries and explicitly confirmed returns**.

The agent manages the conversation on indigo.ai. A Python service controls access to the catalogue and OrdiNet, the legacy SOAP system. The service validates requests, retrieves live commerce data and records return operations before sending them.

## Try it and review the submission

- **Assignment test page:** [Open the supplied test chat](https://clair.platform.indigo.ai/chatbot/4ac6e5b9-58c4-48e1-bb0e-cf85cc93cb43).
- **Published assistant:** [Open the published assistant](https://clair.platform.indigo.ai/chatbot/0a015e5c-50ee-41c3-96bc-faf2e2e0e7ae).
- **Shop landing:** [Open the page with the embedded widget](https://indigoai-api.whitedesert-4a2505fa.koreacentral.azurecontainerapps.io/shop/).
- **OpenAPI:** [Machine-readable specification](https://indigoai-api.whitedesert-4a2505fa.koreacentral.azurecontainerapps.io/openapi.json) · [Interactive API documentation](https://indigoai-api.whitedesert-4a2505fa.koreacentral.azurecontainerapps.io/docs).
- **Demo video:** [Watch](https://indigoaistorageaccount.blob.core.windows.net/assignment-video/assignment-video.mp4?st=2026-10-10T11%3A23%3A47Z&se=2027-01-08T11%3A38%3A47Z&sp=r&spr=https&sv=2026-04-06&sr=b&rscd=inline&rsct=video%2Fmp4&sig=%2F0yzp%2Be4U3PGgVd4FNWWlkqTVqsn74mlOutPWwcKeJM%3D) · [Download MP4](https://indigoaistorageaccount.blob.core.windows.net/assignment-video/assignment-video.mp4?st=2026-10-10T11%3A23%3A47Z&se=2027-01-08T11%3A38%3A47Z&sp=r&spr=https&sv=2026-04-06&sr=b&rscd=attachment%3B%20filename%3Dassignment-video.mp4&rsct=video%2Fmp4&sig=%2BqqeJY59fbBxo%2BvTAue7IDGHnD0Pb4KF7tKW4isMj38%3D). Duration **14:27**, technical walkthrough from **05:06**.
- **Part 1:** [How I work with AI — one-page A4 PDF](consegna/1/Part1.pdf), with the **exact text** of the [Markdown source](consegna/1/Part1.md). No summary or wording changes.

The video needs no account. Its intentionally shared HTTPS links permit reading **only that video**, until **8 January 2027 at 11:38 UTC**. The Blob container and storage account remain private.

## Contents

1. [Architecture and sources of truth](#1-architecture-and-sources-of-truth)
2. [Code structure and technology choices](#2-code-structure-and-technology-choices)
3. [Four tools designed for an LLM](#3-four-tools-designed-for-an-llm)
4. [How product search works](#4-how-product-search-works)
5. [How order verification works](#5-how-order-verification-works)
6. [How returns work](#6-how-returns-work)
7. [Legacy quirks and recovery](#7-legacy-quirks-and-recovery)
8. [Conversation and platform configuration](#8-conversation-and-platform-configuration)
9. [Local setup and tests](#9-local-setup-and-tests)
10. [Deployment, publication and rollback](#10-deployment-publication-and-rollback)
11. [Observability, limits and deliberate scope](#11-observability-limits-and-deliberate-scope)

## 1. Architecture and sources of truth

```text
Customer → indigo.ai chat → FastAPI REST tools
                              ├─ CSV → metadata, labels and static filters
                              ├─ SearchEngine → semantic / hybrid / Jev ranking
                              ├─ OrdiNet SOAP → live prices, stock and orders
                              └─ Azure Table → durable return proposals and outcomes
```

**The agent is not the integration service.** indigo.ai supplies conversation state, tool selection and the customer-facing answer. Python supplies structured results and applies deterministic checks. No Python endpoint generates the shop's conversational answer.

### Which source supplies each fact?

| Fact | Source | Decision |
| --- | --- | --- |
| Product name, description, category, material, tags and other attributes | Original 152-product CSV | Load once into memory. Keep the supplied export unchanged. |
| Ratings, review counts and shipping estimates | CSV | Preserve missing values. A shipping estimate is not an arrival guarantee. |
| Price, stock and availability | Fresh OrdiNet reads | Never use stale CSV values as a fallback. Apply budget filters to live prices. |
| Order state, dates, carrier and tracking | OrdiNet | Verify the email before disclosing the normalized result. |
| Search relevance | Vector similarity, RRF or Jev decisions | Rank candidates. Do not use relevance as proof of stock or mandatory features. |
| Return proposal, consent and recorded outcome | Azure Table plus the SOAP response | Persist coordination before the write. Stop on uncertain outcomes. |

**Why this boundary?** The CSV is useful for discovery, but its commerce fields can already be stale. Separating metadata from live facts prevents convincing recommendations based on outdated prices or stock.

## 2. Code structure and technology choices

There are seven runtime Python modules. Each owns one boundary:

| File | Main components | Responsibility and reason |
| --- | --- | --- |
| [`main.py`](main.py) | `lifespan`, `app.state`, logging middleware | Load configuration and share catalogue, search and HTTP clients per process. Close resources at shutdown. |
| [`models.py`](models.py) | `ProductFilters`, `OrderRequest`, response models | Define the data contract. Reject invalid fields and contradictory ranges before external calls. |
| [`catalog.py`](catalog.py) | `Catalog`, `metadata_text`, `matches` | Normalize CSV metadata, discover labels, apply static filters and calculate lexical BM25 scores. |
| [`retrieval.py`](retrieval.py) | `SearchEngine`, `rank`, `_cached` | Validate the vector index, select one ranking mode and bound paid inference work. |
| [`tools.py`](tools.py) | Authentication, routes and error handlers | Turn validated HTTP requests into operations. Combine metadata and live data without exposing legacy formats. |
| [`ordinet.py`](ordinet.py) | `OrdiNet`, `_read`, `create_return` | Build and parse SOAP, manage sessions and normalize values. Keep retryable reads separate from writes. |
| [`returns.py`](returns.py) | `ReturnService`, `TableStore` | Validate consent and eligibility. Coordinate changes through persistent ETag checks rather than memory alone. |

### Why these technologies?

- **FastAPI and Pydantic:** validation and OpenAPI come from the same endpoint definitions. This reduces drift between implementation and imported tools.
- **httpx:** asynchronous HTTP clients reuse connections and expose explicit timeouts. SOAP and inference use separate clients and timeout settings.
- **defusedxml:** parse external XML with protections against unsafe XML features. ElementTree builds outgoing XML instead of interpolating strings.
- **Decimal:** represent prices without binary floating-point errors. API prices serialize as decimal strings in euros.
- **In-memory CSV and local vectors:** 152 products do not require a catalogue database or vector service. This reduces infrastructure and dependencies.
- **Azure Table:** proposals and send coordination must survive restarts and work across requests. Only the return workflow needs persistent application state.
- **Managed identities:** production image pulls and Table access do not require embedded account credentials.
- **One static HTML page:** [`frontend/index.html`](frontend/index.html) supplies the optional demo landing page and native widget launcher without a frontend framework.

### Startup and shutdown

`lifespan()` requires `ORDINET_USER`, `ORDINET_PASSWORD` and `TOOL_API_KEY`. It loads the catalogue and vector index, creates the shared clients and conditionally initializes returns.

The SOAP client uses an 8-second HTTP timeout, with a 3-second connection timeout. The inference client uses 20 seconds, with a 3-second connection timeout. These are HTTP operation limits, not a guaranteed total duration for a complete tool call with retries.

An invalid or stale vector index stops application startup. Missing inference credentials fail only a requested inference mode. Browsing does not call an inference provider.

**Trade-off:** startup validation prevents silent use of a stale index, but also makes that index a dependency of the whole application.

## 3. Four tools designed for an LLM

| Tool | HTTP endpoint | When to use it |
| --- | --- | --- |
| `get_catalog_options` | `GET /catalog/options` | Discover an unfamiliar stored category, subcategory, brand, colour, material, occasion, tag or recipient label. |
| `search_products` | `POST /products` | Search, browse or retrieve known product IDs. Select output fields and one ranking mode. |
| `get_order_status` | `POST /orders/status` | Read one order after collecting its number and purchase email. |
| `manage_return` | `POST /returns` | Read eligibility, prepare a proposal, approve, cancel or read its state. |

The schema is served at `/openapi.json`. There is no separate manually maintained search specification. A hidden `GET /products` compatibility route supports older clients but does not create another imported tool.

### Design decisions

- **One search/detail tool:** exact-ID lookup and discovery share result shape and live verification. The model does not choose between duplicate tools.
- **Explicit descriptions:** specify when to call, field meanings, pagination and how to recover from errors. Names alone are insufficient guidance.
- **JSON arrays:** alternatives use arrays in the POST body. This avoids ambiguous list serialization through query strings.
- **Bounded output:** `k` is the maximum products per page: default 5, allowed values 1–10. Actual pages contain 0–`k` products. Label `limit` defaults to 10, accepts 1–20 and may also return fewer labels or none.
- **Selectable fields:** `fields` reduces repeated metadata. It changes the response, not filters, ranking or live checks.
- **Unknown remains unknown:** requested missing values are `null`. Unrequested fields are absent. These states have different meanings.
- **Strict inputs:** models reject extra fields, invalid enum values and minimums above maximums. Return operations accept only their relevant fields.

### Recoverable errors

| HTTP status | Meaning | Expected agent action |
| --- | --- | --- |
| `401` | Tool connection is not authorized | Ask the operator to check configuration. Do not invent customer data. |
| `404` | Order cannot be verified, returns are disabled, or a decision has no stored proposal | Follow the error message. Do not disclose whether an unverified order exists or assume every 404 means incorrect credentials. |
| `422` | Invalid parameters or a rejected workflow operation | Correct the named fields or read the current proposal. Do not relax customer constraints to bypass validation. |
| `503` | Upstream, inference or persistence is unavailable | Explain the limit. Do not guess live facts or repeat uncertain return writes. |

Handled errors use `code`, `message` and `retryable`, with `Cache-Control: no-store`. Service failures include `Retry-After: 5`. Validation errors identify fields without echoing rejected customer values.

## 4. How product search works

The core path is `tools.search_products()` → `SearchEngine.rank()` → `OrdiNet.availability()`.

### Request flow

1. Authenticate the tool connection and validate the request.
2. Check category labels. Reject recognizable email addresses and order references in the search query.
3. Apply static catalogue filters to obtain compatible candidates.
4. Rank those candidates, unless the request is browse or ID lookup without a query.
5. Read live information for at most 20 candidates starting at `offset`, in one logical availability batch. SOAP read recovery may repeat that batch; an empty candidate slice makes no SOAP call.
6. Apply price, quantity and availability filters to the live data.
7. Combine live facts with metadata. Select output fields only after all checks.
8. Return products, `search_mode`, `checked_candidates` and `next_offset`.

Example request, not a recorded product recommendation:

```json
{
  "query": "Qualcosa per tenere caldo il pranzo in ufficio",
  "mode": "semantic",
  "max_price_eur": 60,
  "available_only": true,
  "fields": ["name", "description", "price_eur", "availability"],
  "k": 5
}
```

**Why filter twice?** Metadata filters can run locally. Price and availability filters must wait for the live source. The ranking model never decides whether a product passes a budget or stock rule.

### Three modes, one operation

| Mode | Implementation | Reason and limitation |
| --- | --- | --- |
| `semantic`, default | Azure `text-embedding-3-small` query embedding and local cosine similarity | Supports natural multilingual requests. Similarity is not a probability or proof of every required feature. |
| `hybrid` | BM25 plus semantic ranking, combined by equal-weight reciprocal rank fusion | Combines lexical and semantic evidence. It is not automatically better than semantic search. |
| `jev` | Pinned TypeSafe `jev-1.13.0`, with an independent Noul match question for each candidate | Produces structured per-candidate scores, not customer-facing text. Cost depends on candidate context and question count. |

Semantic ranking does **not** calculate BM25 scores. The initial `Catalog.search()` call receives `query="*"` and only selects metadata-compatible candidates. BM25 scoring runs only in the hybrid branch.

Hybrid considers up to 40 results from each ranking. Only positive BM25 matches contribute. Each list contributes `0.5 / (60 + rank)`. Lexical matching uses catalogue words without stemming or translation.

Jev receives public weighted metadata and a standalone query. Each candidate receives a separate match probability in one batched request. This supports multiple recommendations without treating a winner-only Choice distribution's zero tail as a useful ranking.

Omitted, blank or `*` query means browse. Reading `product_id` values without a query also avoids model calls. The agent should not call all three modes or many synonym variants routinely.

### Index and cache

[`data/search-vectors.json`](data/search-vectors.json) contains the product embeddings. Startup checks product IDs, expected model, 1,536 dimensions, finite vector values and a metadata fingerprint.

The common retrieval text weights names three times and tags twice. It also includes occasions, category, subcategory, brand, description, material and colour. It excludes CSV price and stock.

- Cache successful inference results for ten minutes, at most 256 entries.
- Share one in-progress call for an identical cache key.
- Allow four concurrent inference computations and at most 32 pending keys.
- Share query embeddings between semantic and hybrid modes.
- Include candidate IDs in Jev cache keys, because metadata filters change its questions.
- Do not cache SOAP price, stock, availability or order reads.
- Return a safe error on provider failure. Do not retry automatically or silently switch ranking modes.

**Trade-off:** the cache reduces paid calls, but it is process-local. It is not a durable pagination snapshot across replicas or restarts.

### Filters, output and pagination

- Alternatives inside a list use **OR**. Different supplied filters use **AND**. Tags also use OR.
- Numeric boundaries are inclusive. Missing values fail a requested range rather than becoming zero.
- `available_only` defaults to false. Purchasable recommendations require true or explicit suitable availability states.
- A nonempty `availability` list overrides `available_only`.
- `fields` always preserves `product_id` and `relevance_score`. Omit it or send `[]` for the complete product.
- Query ranks candidates. It does not remove weak semantic matches or apply mandatory exclusions.

`offset` skips ranked candidates, not returned products. SOAP fetches the selected batch first; the local filtering loop then consumes candidates until it finds `k` matches or reaches the batch end. `checked_candidates` counts that consumed prefix, **not necessarily every candidate fetched from SOAP**. `next_offset` is the supplied offset plus that count, or null when no candidates remain. An empty page can still have `next_offset`.

For example, with `k=5` and all candidates matching, SOAP may fetch 20 products while the response returns five, reports `checked_candidates=5` and sets `next_offset=5`. Subsequent pages read fresh live data, including any previously prefetched candidates.

The client must continue with the returned offset and unchanged mode, query, filters and `k`, and reset offset to zero for a changed search. The backend does not bind an offset to an earlier request. Do not claim no matches while relevant pages remain. A global cheapest/best claim requires comparing all relevant pages.

### Catalogue labels and maintenance

`get_catalog_options` reads only metadata. It counts each product once per label and sorts labels alphabetically. Counts are not popularity, sales or live stock.

Label `query` performs a case-insensitive substring match on labels, not BM25 or product descriptions. Label `offset` skips alphabetically sorted labels, unlike product pagination.

Use returned labels in filters. Unknown categories produce 422. Other metadata choice values and product IDs are matched against the catalogue; unknown values can simply produce an empty result rather than a validation error. An options call does not set search context. For example, browsing a verified category still requires passing that category to `/products`.

When retrieval metadata changes, regenerate vectors with [`scripts/prepare_search_vectors.py`](scripts/prepare_search_vectors.py), then test and release the matching CSV/index pair. Regeneration makes paid embedding calls and replaces the index only after a complete result. It is not an automatic nightly ingestion pipeline.

### Saved search benchmark

**Hit@1** measures how often the first result belongs to the query's saved expected product IDs. This is retrieval evaluation, not conversational or live-commerce acceptance.

The following results compare methods on the **same queries**, using full catalogue metadata:

| Query group | Scored queries | BM25 | Semantic vectors | Hybrid 50/50 | Jev baseline: Choice + Noul |
| --- | ---: | ---: | ---: | ---: | ---: |
| Common scored subset | 436 | 70.6% | 81.0% | 83.7% | **97.9%** |
| Natural requests in Italian | 62 | 9.7% | **85.5%** | 56.5% | **100.0%** |
| Natural requests in English | 62 | 46.8% | **87.1%** | 64.5% | **100.0%** |

On the larger **5,709-query** scored set, without Jev, primary-method Hit@1 was **88.9% BM25**, **77.6% semantic** and **88.4% hybrid**. Names and synthetic property queries dominate that set, so its total does not establish superiority for real conversations.

**Decision:** semantic is the default for natural multilingual requests. The natural-request results show that adding lexical evidence through hybrid can reduce relevance. Jev performed best on the common baseline subset, but its evaluation does not establish the quality of the current runtime composition.

**Limits:** labels are largely synthetic or assistant-curated, without independent human review. No SOAP, budget, stock or agent conversation pipeline was evaluated. Italian BM25 received raw Italian text. These results do not establish general 100% accuracy.

The baseline Jev evaluation used **whole-catalogue Choice + Noul**. Runtime uses **independent Noul decisions per candidate**. Do not attribute the baseline's 97.9% or cost to the deployed implementation without a separate evaluation.

Saved public artifacts:

- [Query dataset](tests/fixtures/search-benchmark.jsonl): 5,966 unique queries, expected IDs, groups and scored/diagnostic flags.
- [Results and metrics](tests/fixtures/search-baseline.json): primary and diagnostic method aggregates, including the common Jev subset.
- [Evaluation script](scripts/evaluate_search.py): offline inventory by default. Replaying the current methods requires explicit paid `--online` calls.
- [Methodology and limitations](docs/search-modes.md): experiment scope and interpretation.

Raw rankings, API-usage records and the full experiment report are retained locally under ignored `assignment/embeddings-experiment/`. That private directory is not a public deliverable and contains credential files. Do not publish it wholesale.

## 5. How order verification works

1. Collect the customer-supplied order number and purchase email.
2. Validate their formats and read the corresponding SOAP order.
3. Check that SOAP returned the requested numeric order ID.
4. Compare the email with a case-insensitive, timing-safe comparison.
5. Return only normalized state, dates, carrier and tracking.

Unknown order and incorrect email return the same `order_not_verified` denial. Requests use POST bodies, not identifying URL parameters. Successful responses use `Cache-Control: no-store` and omit email, name, address and raw XML.

**Security limit:** this is a knowledge-based check, not proof of identity or entitlement. Anyone who knows both values can pass. There is no verified customer session, customer-to-order authorization map or application-level rate limit.

The tool API key authenticates the platform connection, not the buyer. Chat visitor IDs, profile values and privacy consent do not authenticate a buyer either. Strong customer authentication remains a production requirement.

## 6. How returns work

Returns use a single `manage_return` operation with explicit phases. The backend enforces these phases instead of trusting a model-supplied `confirmed=true` field.

### Inputs by phase

| Phase | Fields in addition to order number and email | Effect |
| --- | --- | --- |
| `options` | None | Read actual order lines, eligibility, reasons and existing state. |
| `prepare` | `lines`, `reason` | Save or update an unsent proposal and its summary. |
| `approve` | `draft_id` | Check the original confirmation event and, when enabled, claim one send attempt. |
| `cancel` | `draft_id` | Cancel an unsent, valid proposal. Never cancel an already-created return. |
| `status` | None | Read proposal state and current SOAP order state. Never send. |

Every phase verifies the order/email pair against SOAP. Browser identity is not an authorization requirement, so a verified proposal can be resumed from another device.

### Eligibility and consent

- The order must be genuinely delivered, SOAP state `ST05`.
- The delivery date must be verifiable and within the documented inclusive 30-calendar-day window, using Europe/Rome dates.
- Articles and quantities must exist in the order. Duplicate request lines are rejected.
- The customer chooses articles, quantities and reason. There is no inferred reason default.
- A new preparation creates a random `draft_id`, canonical summary, preparation message ID and 15-minute expiry. Repeating identical choices while the proposal is open, fresh and in the same sending mode reuses it without extending its expiry.
- Approval requires a whole message exactly `CONFERMO`. Cancellation requires exactly `ANNULLA`. The message ID must differ from the preparation message ID. No trimming or interpretation of longer phrases.
- Changing articles, quantities, reason or sending mode creates a replacement proposal and invalidates the previous reference. A new request after cancellation preserves the previous cancelled history.
- Expiry is checked during decisions; there is no automatic expiry transition or cleanup. A status read can still report `prepared` for an expired proposal.

The original message arrives as strict Base64/UTF-8 in `X-Chat-User-Message-B64`, with `X-Chat-Message-Id`. These are platform connection headers, not model-selected JSON fields.

**Trust boundary:** Base64 is encoding, not cryptographic proof of authorship. The connection must supply the actual event. A different message ID does not independently prove chronological freshness, and the backend does not verify that the customer actually saw the summary. The platform and agent must ensure that the decision follows presentation of that summary. Quick Reply provenance needs separate acceptance checks.

### Durable state and send coordination

```text
not_prepared → prepared
                  ├─ cancel  → cancelled → new user request → prepared
                  └─ approve → submitting
                                  ├─ verified IdReso + saved result → submitted
                                  └─ uncertain outcome             → submission_unknown
```

`not_prepared` is a read result, not a persisted operation. Preview approval with sending disabled leaves the proposal `prepared`.

Azure Table holds one operation row per numeric order. `TableStore.replace()` uses the row's ETag with `IfNotModified`. This is compare-and-swap: a stale caller cannot overwrite a newer decision.

The real-send path:

1. Recheck eligibility and expiry.
2. Atomically replace `prepared` with `submitting`.
3. Read the durable claim back. Only the winning caller proceeds.
4. Call `CreaRichiestaReso` once through the dedicated sender.
5. Persist the returned `IdReso` and outcome.

Timeouts, invalid responses, missing IDs and uncertain persistence do not trigger a resend. A process interruption or failed final persistence can leave the stored row `submitting`; in the latter case, the HTTP response reports `submission_unknown` even though that outcome could not be saved. Both uncertain states require manual reconciliation and block a new preparation/send.

`decision_recorded=true` reports a newly recorded decision in that call, not proof of return creation. A repeated decision returns the existing result with false. Only `submitted` reports a returned `IdReso` and successfully saved outcome.

**Why stop rather than retry?** The external write might succeed before its response is lost. The design favors duplicate prevention over automatic recovery. It is not an exactly-once transaction across Azure and SOAP.

An external `ST07` order state means return in progress, but does not identify our return. It cannot recover a missing `IdReso` or authorize a second write.

### Two explicit configuration gates

- `RETURNS_ENABLED=true` initializes the durable service.
- `RETURN_SUBMISSION_ENABLED=true` enables real submission in both the service and SOAP transport.

With submission disabled, approval validates the preview but reports `submission_stopped=true`, `decision_recorded=false` and no created return. Old preview consent cannot authorize real sending after a configuration change.

The deployed main service has real sending enabled. Do not assume it is a sandbox because the local example defaults to false. Never delete, reset or restore old operation rows to repeat a test.

See [return configuration and safeguards](docs/returns.md).

## 7. Legacy quirks and recovery

| Observation | Adapter behavior | Reason |
| --- | --- | --- |
| SOAP expects `000101`, not `GS-2026-000101` | Validate the customer reference and send its final six digits. | Keep legacy identifiers outside the model. The year is not forwarded, so cross-year uniqueness needs production confirmation. |
| Observed integer prices represent cents | Convert integer cents or decimal euro strings with `Decimal`. | Prevent a 100× price error and reject invalid/nonfinite amounts. |
| Dates use `DD/MM/YYYY` | Return ISO dates or null when absent. | Provide one unambiguous format. Reject malformed dates. |
| Business failures can use HTTP 200 | Inspect SOAP `Codice` and `CodErrore`. | HTTP success does not prove business success. |
| Availability or order states can be unsupported | Return `unknown`. Unsupported live product data stays unverified. | Do not infer a meaning or use CSV values to fill it. |
| Sessions expire | Reuse sessions for less than nine minutes and serialize refresh with an async lock. | Avoid unnecessary logins and concurrent refresh storms. |
| Reads fail temporarily | One expired-session recovery and at most two transient retries, with 0.25/0.50-second waits. | Bound recovery. These budgets are separate and include transient login failures. |
| Write responses can be lost | No automatic retry of return creation. | A missing response does not prove the write failed. |

Availability initializes each requested article as unknown. A missing or invalid article does not erase valid information for other articles in the batch. Persistent failures map to safe service errors instead of leaking XML or credentials.

## 8. Conversation and platform configuration

The [agent goal](docs/agent-prompts/agent-goal.txt) defines the conversational behavior:

- Ask one or two useful questions for vague requests. Search directly when enough information exists.
- Recommend at most three relevant products with reasons tied to verified features and customer preferences.
- Retain explicit constraints across turns. Do not infer birthdays from age or hard recipient filters from a relationship.
- Read required product fields before making claims. Distinguish accessories and refills from complete objects.
- Explain order states and estimates plainly. Do not invent policies or promise refunds.
- Decline unrelated requests briefly and return to the shop's scope.
- Handle continuations and failures directly instead of delegating the conversation.

The source goal includes an assortment map across the 11 catalogue categories. It supplies orientation and product-family vocabulary, not fixed product facts. Tool results take precedence.

**Decision:** prompt rules guide the conversation. Deterministic validation, live filters, email comparison and send coordination remain in Python. Prompt instructions are not security controls or guarantees of model behavior.

### Efficiency and latency — agent instruction

The following instruction is intended for the end of the agent goal. Five tool calls per turn is a recommended target, not a backend-enforced limit. Necessary live checks and return safeguards take priority. Updating this README does not update or publish the platform agent; the operator must add the instruction and republish it.

> **EFFICIENZA E LATENZA:** punta a non superare 5 chiamate ai tool per turno: è un limite consigliato, non rigido. Riusa metadati e opzioni già verificati, senza sostituire le verifiche live necessarie con risultati precedenti. Evita ricerche ripetitive e fermati quando hai proposte sufficienti e pertinenti. Supera il limite solo per verifiche essenziali. Se interrompi una ricerca parziale, non dichiarare assenza di alternative o primati assoluti: sii completo, ma anche rapido.

### Platform connection

1. Import the served OpenAPI into the Gift Shop collection.
2. Configure Test and Production endpoints and `X-API-Key: [[TOOL_API_KEY]]` through a Workspace Secret.
3. Assign the four actions and the matching agent goal.
4. In Processing start, copy the original message ID and message, then Base64-encode the latter.
5. Bind `X-Chat-Message-Id: {{chat_message_id}}` and `X-Chat-User-Message-B64: {{chat_user_message_b64}}`.
6. Save, reopen and check the actual arguments, including arrays and accented text.
7. Test the complete customer flow, then Publish the coordinated workspace changes.

The return headers are required for all return phases. Shop and order actions ignore them. Secret substitution and normal variable interpolation are different mechanisms.

Publication is workspace-wide. Promote the context workflow and its references together. A previously missing Live hook caused an empty halt, so saved configuration alone is not transport acceptance.

### Optional landing

`frontend/index.html` is served at `/shop/`. It provides a responsive layout, a custom launcher and a lazily loaded native widget. The identifier in the page is a public project token, not the backend API key.

The page has a direct-chat fallback and loading timeout. It does not send customer messages or accept privacy consent automatically. Sharing backend hosting reduces infrastructure, but shares its cold-start behavior. Frontend loader checks do not prove the published conversation works.

## 9. Local setup and tests

### Start the backend

Use **Python 3.12+**, preferably **3.13** to match Docker. The response models use `typing.TypedDict`; Pydantic rejects that implementation on Python below 3.12.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

The copy creates `.env` only when it does not exist. Edit it privately. Do not overwrite existing credentials or commit the file.

| Configuration | Purpose |
| --- | --- |
| `ORDINET_USER`, `ORDINET_PASSWORD`, `TOOL_API_KEY` | Required application credentials. |
| `AZURE_EMBEDDING_ENDPOINT`, `AZURE_EMBEDDING_DEPLOYMENT`, `AZURE_EMBEDDING_KEY` | Semantic and hybrid query embeddings. Deployment defaults to `text-embedding-3-small`. |
| `TYPESAFE_API_KEY` | Required only for explicit Jev inference. |
| `RETURNS_ENABLED`, `RETURN_SUBMISSION_ENABLED` | Initialize returns and separately authorize real writes. Start locally with both false. |
| `RETURN_TABLE_ENDPOINT`, `RETURN_TABLE_NAME`, `RETURN_HASH_KEY` | Durable return storage and stable HMAC key of at least 32 characters. Never reuse sandbox consent data for real operations. |
| `RETURN_AZURE_AUTH`, `AZURE_CLIENT_ID` | Local CLI credential or production managed identity. |
| `RETURN_ALLOWED_ORDER`, `RETURN_ALLOWED_EMAIL` | Optional paired deployment restriction. Supply both or neither. Actual SOAP verification remains mandatory. |

```powershell
$env:RETURNS_ENABLED = "false"
$env:RETURN_SUBMISSION_ENABLED = "false"
.venv\Scripts\python.exe -m uvicorn main:app --reload --no-access-log
```

Local endpoints: `/openapi.json`, `/docs`, `/health` and `/shop/`. Swagger's **Authorize** supplies the tool key. `/health` reports process readiness without checking SOAP or inference availability.

### Run simulated tests

In a separate local terminal:

```powershell
$env:RETURNS_ENABLED = "false"
$env:RETURN_SUBMISSION_ENABLED = "false"
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The environment overrides prevent accidental initialization of a real Table service from a local `.env`. Return tests create their own fake services and sender.

| Test file | What it verifies |
| --- | --- |
| `test_backend.py` | HTTP contracts, metadata, order verification, price conversion and SOAP read recovery. |
| `test_search.py` | Independent lexical/filter/live-pagination oracle, including sparse and empty pages. |
| `test_retrieval.py` | Actual mode routing, mock providers, cache, index validation, errors and privacy guards. |
| `test_returns.py` | Consent, preview/real gates, one sender, replay, storage failures and interrupted sends. |
| `test_return_matrix.py` | State/operation combinations, concurrency, cross-browser access, expiry and cancellation history. |
| `test_return_api.py` | Return phases through HTTP with fake SOAP and storage. |
| `test_logging.py` | Redaction, correlation and unchanged HTTP output. |
| `test_catalog_prompt.py` | Assortment coverage and selected prompt rules. Not LLM adherence. |

Recorded release evidence includes **154 tests in the final Linux image with networking disabled**. The independent search oracle covers **2,923 valid cases and 9,969 HTTP pages**. These are recorded results, not a claim of new live acceptance.

`test_search.py` uses an explicit lexical test double. It does not measure the current semantic provider. Mode/provider tests are separate, so the large oracle is not an online retrieval-quality benchmark.

[`scripts/evaluate_search.py`](scripts/evaluate_search.py) defaults to an offline inventory. `--online` explicitly enables paid inference, with 20 queries by default. Generated labels and passing tests do not establish real-customer relevance or conversational quality.

## 10. Deployment, publication and rollback

### Current infrastructure

| Resource | Role |
| --- | --- |
| `indigoai-api` | The only remaining Container App. Backend image `indigoaicr.azurecr.io/indigoai-assignment:search-modes-v1`, API 0.8.0. |
| `indigoai-env` | Shared Container Apps environment. |
| `indigoaicr` | Image registry. Historical images remain available for rollback. |
| `indigoai-app-pull` | Registry-scoped image-pull identity. |
| `indigoai-returns-data` | Data identity selected for Azure Table access. |
| `indigoaistorageaccount` | Persistent return state and a separate private Blob container for the demo video. |
| Log Analytics workspace | Container environment logs. |

The three obsolete return test apps were removed. Images and current return records were preserved.

Container Apps Consumption uses 0.25 vCPU, 0.5 GiB RAM, one Uvicorn worker and zero-to-one replicas. Scale-to-zero reduces idle compute but adds cold-start latency. Registry, storage, logs and inference have separate costs.

The student deployment is in Korea Central. A commercial deployment needs an approved data-residency decision, including the separate inference endpoints.

### Release procedure

1. **Select the intended subscription and app.** Record the current image and effective configuration privately for rollback.
2. **Test the exact source.** Run simulated tests locally and in the built Linux image. Review OpenAPI changes and regenerate vectors if metadata changed.
3. **Build a new immutable tag.** The Dockerfile copies only runtime code, catalogue, vectors and the static page. It uses a non-root user.
4. **Push the image to ACR.** Confirm its digest. Do not place secrets in build arguments or image layers.
5. **Update the Container App explicitly.** Preserve runtime secret references, identities and return gates unless a separate change is authorized.
6. **Check rollout.** Inspect revision health, active traffic and configuration. Check health and schema without customer mutations.
7. **Update the platform when needed.** Reimport changed OpenAPI, reselect actions and align goal/context workflow. Save, reopen, test and Publish.
8. **Perform customer acceptance deliberately.** Check discovery, pagination, gift-to-order transitions and failures. Real return confirmation belongs to an explicitly authorized operator.

The [deployment guide](docs/deploy-azure.md) contains PowerShell commands. Tests and prompt files are excluded from the production image, so Linux test runs need explicit read-only mounts of those files.

Production uses Container Apps `secretRef` bindings for credentials. Azure Table uses managed identity, not storage account keys. The public video link is a narrowly scoped read-only sharing link, not a return-storage credential.

### What each change requires

| Change | Required action |
| --- | --- |
| Python, dependencies or runtime assets | Test, build image, push image, update Container App and verify. |
| Agent goal, collection or context workflow | Save, reopen, test and Publish in indigo.ai. |
| Backend OpenAPI contract | Both a backend release and matching platform schema update. |
| README or submission PDF | Documentation review only. No backend release. |

**Git push does not deploy. Image push does not update the app. Backend deployment does not publish the workspace.** Verify each operation independently.

### Emergency stop and rollback

1. Disable real submission with `RETURN_SUBMISSION_ENABLED=false`.
2. Verify that the disabled configuration serves active traffic.
3. Restore the privately recorded previous image when required.
4. Align the platform schema and goal with the restored code.
5. Inspect revision health, traffic and effective gates again.

An image-only rollback does not disable the runtime send flag. Never restore a configuration backup that silently re-enables sending. Never roll back or erase customer operation rows with the code.

## 11. Observability, limits and deliberate scope

### Logs

Each handled tool call emits a correlated request and output event at response completion. A random `call_id` links the events. Inputs are validated before logging.

- Redact known sensitive keys, email-like text and the configured tool key.
- Mask structured order references and tracking values in logging copies.
- Compact product output to useful operational fields.
- Cap each JSON event at 3,500 ASCII bytes and explicitly mark omitted data.
- Preserve the complete HTTP response. Do not truncate what the agent receives.
- Disable raw Uvicorn access logs.

Redaction is not a universal privacy guarantee. Free-text fields, including summaries containing order references, need review before commercial logging. Apply access controls, retention limits and further redaction where required.

Log streams are replica-specific. Reconnect after a revision change. Absence of new lines alone does not establish a backend failure.

### Explicit limitations

- Knowledge-based order verification is not strong buyer authentication. Application-level rate limiting is absent.
- A prompt rule does not prove that an LLM follows the rule in every conversation.
- There is no validated universal relevance/no-match threshold or global cheapest/best ranking from one page.
- Inference cache is process-local. Live pages can change and are not a transactional inventory snapshot.
- Query guards recognize common email/order formats, not every possible personal-data representation.
- Return coordination cannot make Azure and SOAP one atomic transaction. Uncertain sends require manual reconciliation.
- The 30-day policy comes from supplied documentation. Targeted tests do not validate every policy boundary in the client's production system.
- There is no automatic CSV refresh pipeline, checkout, payment, order cancellation or refund execution.
- Full reviewer access and final published conversational regression need their own checks.

### Time and scope

**Approximate time spent: 15 hours.**

I prioritized an end-to-end integration, explicit tool contracts, fresh commerce data and durable return coordination. I kept catalogue retrieval local and added persistent storage only where a restart could affect consent or duplicate prevention.

I deliberately omitted a catalogue database, vector database, automatic ingestion pipeline, strong customer login and a full ecommerce frontend. The optional landing reuses the backend hosting. Search experiments and simulated tests support technical decisions, but do not replace human review of actual customer conversations.
