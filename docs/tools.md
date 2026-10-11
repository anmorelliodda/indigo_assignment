# Agent tools — backend API 0.8.0

Four operations: catalogue options, unified product search/lookup, order status and durable returns. Read-only platform verification on 10 October 2026 confirmed Gift Shop Production API 0.8.0, identical Test/Production schemas and an "All published" collection status. The published schema matches the backend after resolving local `$ref` values and excluding editor-added servers. Its search mode enum is semantic/hybrid/Jev, with semantic as default. The configured agent goal matches the repository source, and the four actions are selected. No new workspace Publish was performed during this documentation check. Product Details is removed. Real SOAP submission is enabled for all eligible orders with purchase email verified by SOAP; the single-order deployment restriction is removed. Andrea's supplied Live traces verified real creation and a cross-chat replay with the same return ID and no new send; only Andrea performs further online acceptance. Old preview consent never authorizes the final send.
Search uses POST JSON so the platform does not encode arrays as query-string values. Shop tools need no user-message or message-ID header. A hidden GET compatibility route remains for older clients.

Search modes release 0.8.0: 152 simulated tests passed locally; the final exact Linux image run passed 154 tests, including two added edge cases, with networking disabled. The independent lexical/filter oracle checked 2923 cases and 9969 POST pages; actual mode dispatch has separate mocked-provider tests. A narrow image patch FROM final-v2 is deployed, preserving its ordinet.py/returns.py and original effective runtime bindings. Historical online product-only checks passed semantic/hybrid/Jev. The 10 October documentation check made no live product/order/return calls. Published configuration and schema alignment are verified, not full conversational or production ranking regression. See [search modes](search-modes.md) and the saved 5966-query fixture.

## get_catalog_options

GET `/catalog/options`. Discover the catalogue's filter vocabulary, one field per call. Use it for questions about available labels, an unfamiliar filter or recovery from an invalid category. Do not call it routinely before every product search.

| Argument | Behavior |
| --- | --- |
| field | category (default), subcategory, brand, color, material, occasion, tags or recipient. |
| category | Optional exact category scope. Case/known aliases are ignored. An unknown category returns 422; discover categories without this filter first. |
| query | Optional case-insensitive label substring, maximum 100 characters. Not BM25, translation, wildcards or product-description search. |
| limit | Maximum labels per page: default 10, minimum 1, maximum 20. |
| offset | Start at 0; then use returned next_offset with the same arguments. Maximum 10000. |

Returns `field`, `values` (each with `value` and `product_count`) and `next_offset`. Labels are sorted by case-insensitive alphabetical order, with an exact-text tie-break. Missing/blank labels are omitted. Tags and occasions are split into individual labels; each product counts at most once per label. Category aliases use the same canonical names as product discovery; other labels are not arbitrarily merged.

Counts describe the stored catalogue subset, including unavailable products. They are not live stock, sales, popularity or the number of purchasable gifts. This tool makes no SOAP calls and never reads CSV prices/stock. Use `search_products` for live data. A label's existence does not justify inferring a customer's occasion or recipient constraint.

```text
GET /catalog/options?field=category&limit=20
GET /catalog/options?field=subcategory&category=Tech%20%26%20Gadgets
GET /catalog/options?field=brand&category=Kitchen%20%26%20Dining&limit=10
GET /catalog/options?field=material&query=steel
```

An empty result or an offset past the last label returns `values: []` and `next_offset: null`. Follow pagination before claiming a complete list. `gift_wrap` is already boolean; numeric fields, product names and IDs are not facets.

The replaced `/categories` route is absent from the current OpenAPI and returns 404.

## search_products

POST `/products`, with a ProductFilters JSON body. All business filters and output selection are optional. No business filter applies unless supplied.

- Omitted, blank or `*` query: browse filtered products in stable catalogue order.
- Other query: a standalone natural product request, up to 200 characters, in the customer's language. All metadata-filter candidates remain eligible; query only ranks.
- `mode`: semantic (default, multilingual vectors), hybrid (BM25+vectors/RRF), jev (TypeSafe typed decision model, independent candidate-match Noul probabilities, no generated response). No extra search tool; semantic and vector mean the same method.
- `search_mode` identifies `relevance_score`: cosine (-1..1), RRF, or Jev probability (0..1); null for omitted/blank/* browse. Scores are not comparable across modes/queries and do not verify live prices/stock or all requirements. ID lookup without query does not invoke inference.
- Explicit metadata/live filters still exclude candidates. No guarantee of k results: there may be fewer valid products or an empty candidate batch with next_offset.
- Semantic/Jev can use Italian directly. Hybrid's lexical component uses English metadata tokens. No invented wildcard/AND/OR syntax. Do not send email addresses, order IDs or private customer/order data in query.
- Choice lists use **OR within each field**. Different fields and ranges combine with **AND**.
- `tags` now uses OR, not the previous all-tags behavior. Verify every mandatory customer feature before recommending.
- Prices, quantities and availability come only from live OrdiNet. CSV prices/stock are never indexed or used as fallback.
- Availability is unfiltered by default. Set `available_only=true` for purchasable gift recommendations.
- `k`: page size 1–10, default 5. Values outside that range return 422, not unlimited results.
- Each request examines at most 20 candidates in one SOAP batch. Follow `next_offset` with unchanged mode, query, filters and k, including after empty pages.
- Relevance and page size do not establish a global cheapest/highest-rated ranking.

### Parameters

| Argument | Behavior |
| --- | --- |
| fields | Optional JSON list of output field names; omitted or [] returns all. ID/score always included. Selection never changes filters, ranking or pagination. |
| mode | `semantic` default; optionally `hybrid` or `jev`. Change mode only when useful, not three calls for every query. |
| query | Standalone natural product request. Default `*`; omitted/blank/`*` means browse without inference. Maximum 200 characters. |
| product_id | Up to ten exact IDs (OR), ignoring case. Each value up to 40 characters. |
| category | Up to ten exact category names (OR), ignoring case/known aliases. Every category must be valid. |
| subcategory | Up to ten exact subcategory names (OR), ignoring case. |
| brand | Up to ten exact brands (OR), ignoring case. |
| recipient | Up to ten exact labels (OR): anyone, her, him, couple, kids. Only explicit labels; do not infer from age/gender. |
| occasion | Up to ten explicit occasions (OR): birthday, christmas, thank-you, graduation, anniversary, housewarming. Do not infer birthday from age. |
| tags | Up to ten exact tags (OR), ignoring case. This does not require every supplied tag. |
| color | Up to ten exact colours (OR), ignoring case. |
| name | Single case-insensitive substring of the product name, maximum 100 characters. |
| material | Single case-insensitive substring, maximum 100 characters. |
| description | Single case-insensitive substring, maximum 200 characters. |
| gift_wrap | true requires wrapping, false requires no wrapping; omitted includes both. |
| min_price_eur / max_price_eur | Inclusive live price bounds, 0–10000 EUR. |
| min_stock / max_stock | Inclusive live quantity bounds, nonnegative integers. |
| min_rating / max_rating | Inclusive catalogue rating bounds, 0–5. |
| min_reviews_count / max_reviews_count | Inclusive catalogue review-count bounds, nonnegative integers. |
| min_shipping_days / max_shipping_days | Inclusive catalogue shipping-estimate bounds, nonnegative integers. Not delivery guarantees. |
| availability | Up to five live states (OR): available, low_stock, out_of_stock, discontinued, unknown. A nonempty list overrides available_only. |
| available_only | false by default. Set true to allow only available/low_stock products. |
| k | Maximum results per page: default 5, minimum 1, maximum 10. |
| offset | Candidate offset, 0–10000. Start at 0; then use only returned next_offset. |

Choice values must be nonblank. Category/subcategory/brand/color/tags values have a maximum of 80 characters; recipient/occasion values have a maximum of 40. Empty lists are inactive.
Omit unused parameters. Do not send the text `null`, comma-separated alternatives or the wildcard `*` as a metadata value.
Missing numeric values cannot match a requested range, even a minimum of zero. Minimum values must not exceed maximum values.

### Examples and serialization

Structured tool arguments:

```json
{
  "query": "*",
  "category": ["Home & Living", "Kitchen & Dining"],
  "brand": ["Fold Studio", "Kanso"],
  "max_price_eur": 50,
  "available_only": true,
  "k": 10
}
```

This means (Home OR Kitchen) AND (Fold Studio OR Kanso) AND live price <= 50 AND purchasable availability. Brand values in this example illustrate the shape; use verified catalogue values when calling.

Send arrays in the JSON body, including single-element lists. Do not use repeated query parameters in the imported tool:

```json
{"category":["Home & Living","Kitchen & Dining"],"max_price_eur":50,"available_only":true,"k":10}
```

```json
{"query":"scented candle","subcategory":["Candles"],"available_only":true,"k":5}
```

```json
{"product_id":["KD-024","EX-001"],"availability":["unknown"]}
```

Do not use `/products/search`: the separate endpoint and `search_catalog` operation were removed in API 0.3.0. Verify actual POST bodies and filter preservation in Preview/Timeline before declaring full conversational acceptance.

### Lookup and selectable output

Use the same POST `/products` for known IDs, with query/irrelevant filters omitted. It returns a list even for one ID; unknown IDs yield zero products. IDs are case-insensitive. get_product_details and GET `/products/{product_id}` are removed in0.5.0.

```json
{"product_id":["HL-004"],"fields":["material","color","shipping_days"]}
```

For all details omit fields. For a lighter first selection:

```json
{"query":"candle","available_only":true,"max_price_eur":50,"fields":["name","category","description","price_eur","availability"]}
```

Allowed fields: product_id, name, category, subcategory, brand, description, material, color, tags, gift_wrap, rating, reviews_count, recipient, occasion, shipping_days, price_eur, availability, stock, relevance_score (max19 list entries). ID and score are always included even if not requested. Unknown field names/wrong types return422 before SOAP. Duplicates do not duplicate output. Response omits unrequested fields; it does not insert null defaults. Requested unknown values stay null. Request price/availability and all needed features before recommending or claiming facts. Selection does not affect live filtering or offset; SOAP still refreshes live data.

## get_order_status

POST `/orders/status`, JSON body with customer-supplied `order_number` (`GS-YYYY-NNNNNN`) and `email`.
Do not call before both are supplied. Never guess credentials or use the tool to discover an email.
The backend compares email before returning normalized status/date/carrier/tracking. Missing orders and wrong emails return identical `order_not_verified` errors. Names/addresses/emails are not returned; responses are no-store.
Unknown status remains unknown. Do not infer its meaning or promise estimated dates. A request for current order status requires a new call, not repetition of an earlier result.
This is a limited knowledge-based mock check, not authenticated customer entitlement.

## manage_return

POST `/returns` uses one typed phase tool: options, prepare, approve, cancel or status. Each call requires order_number/email, independently verified via actual SOAP. Options/status accept only operation, order_number and email. Prepare adds only the customer-selected lines/quantities and reason; approve/cancel add only the current draft_id. A read-schema 422 permits correcting and retrying the read, never retrying approve. Follow the returned summary, mode and state. Only a NEW whole-message CONFERMO authorizes real approval, and only ANNULLA cancels an unsent proposal. No parallel status/decision or same-turn prepare/decision. No resend after submitting, submitted or submission_unknown. For submitted with decision_recorded=false, explicitly say the return was already created and this call sent nothing new. See [durable state, CAS, transport and limitations](returns.md).

## Shared connection requirements

All four operations require `X-API-Key: [[TOOL_API_KEY]]`. Order and return lookup never require `X-Chat-User-Ref`: they verify order number and purchase email against SOAP, independently of browser/profile. Optional visitor headers are unused metadata. The published Gift Shop collection now contains all four actions, with no separate preview-return assignment.

Processing start copies the original message and `{{$message_id}}` and Base64-encodes `{{last_user_message}}`. The collection sends `X-Chat-Message-Id: {{chat_message_id}}` and `X-Chat-User-Message-B64: {{chat_user_message_b64}}`; these are required on all return phases, ignored on shop/order actions. No raw user text in HTTP headers. The JSON body must not supply consent, tokens, message IDs or original-message text.
The visitor reference is connection context, not an LLM argument or ownership proof. No application rate limit, backend profile mapping or order cache is implemented.
SOAP sessions, credentials, raw XML and legacy codes never reach the model. Reads use bounded timeouts/recovery. Return creation is implemented as a separate one-attempt sender behind independent service/transport gates; it never enters the read retry loop. Creation is enabled without a single-order restriction; default local configuration remains disabled. All assistant tests use simulated SOAP/storage, with no live order/return endpoint calls. Andrea alone performs real-write acceptance.

## Console logging

**Current logging (introduced in v14):** exactly two lines per handled call, at response completion: `>>> REQUEST tool_request`, then `<<< OUTPUT tool_output` or `!!! ERROR tool_output`. Shared `call_id`, tool/status;4xx outputs WARNING,5xx ERROR. Product lists/details keep ID/name/live price/availability/stock/score (rounded to3 decimals only in logging copies); lists also retain count/pagination. Options stay in one output. Full descriptions/materials/tags remain in HTTP responses but not logs. JSON payloads cap at3500 ASCII bytes; oversized data becomes explicit `log_data_omitted` plus correlation fields. Never cut HTTP data. Recursive redaction remains; no header/visitor values collected. Colours depend on viewer; no ANSI. Historical v14 verification passed 77 exact-image tests, 10 public and 6 collected-console checks. Current verification is summarized above. HTTP 503 errors still normalize the underlying live-data failure without identifying its specific cause.

Only supplied, validated inputs are logged. Omitted defaults are absent; explicit defaults and arrays remain. Invalid authentication/schema requests log `{}`, not rejected values. Non-tool routes emit no tool events.

The released image disables raw Uvicorn access logs. View these structured events in Container Apps Log stream; reconnect to the new replica after deployment. See [README tests](../README.md#9-local-setup-and-tests) and [observability and limitations](../README.md#11-observability-limits-and-deliberate-scope).
