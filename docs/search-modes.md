# One search tool, three ranking modes

`POST /products`, operationId `search_products`. No extra model-facing search/detail tool.

```json
{"mode":"semantic","query":"Qualcosa per tenere caldo il pranzo in ufficio","available_only":true,"max_price_eur":60,"fields":["name","description","price_eur","availability"],"k":5}
```

- `semantic` (default) means vector search. `vector` is not an additional enum value. Italian/natural requests are supported by Azure `text-embedding-3-small`.
- `hybrid` combines BM25 and vectors using equal-weight RRF, k=60/depth40. Only positive lexical matches contribute. Hybrid is not automatically better.
- `jev` uses pinned TypeSafe `jev-1.13.0`, a **decision model**, not a chatbot/text generator. Each metadata-compatible candidate gets an independent Noul match decision. Decisions are batched in one request over shared metadata/query, then code sorts their probabilities. This supplies multiple meaningful candidate scores without inventing a list from a winner-only Choice's zero-probability tail.
- No query, blank, or `*`: browse/exact-ID detail without embedding or Jev calls, regardless of selected mode.

Metadata filters run before ranking. Budget/quantity/availability use bounded live SOAP reads after ranking. No CSV price/stock fallback. `fields` changes output only. The response's `search_mode` specifies score meaning; `browse` has null scores. Semantic cosine can be negative, hybrid uses RRF, Jev uses 0..1 Noul match probability. A score is not a purchase authorization or proof of all requirements. No production no-match threshold is claimed.

Keep mode/query/filters/k unchanged with `next_offset`; changed search starts at zero. Infer only a standalone product request, never email addresses, order numbers or private conversation/order records. Recognizable email/order identifiers are rejected before inference/SOAP. One complete query is preferred to paid synonym variants. The agent may change the query/mode when useful; it should not call every mode routinely.

## Assets and secrets

`data/search-vectors.json` ships with Docker: 152 normalized metadata embeddings, 1,536 dimensions, model/version and SHA-256 of the actual weighted metadata text. Startup checks identity/dimensions/finite values/fingerprint; stale catalogues must regenerate their vectors. No vector DB or NumPy dependency in runtime.

Runtime secrets: `AZURE_EMBEDDING_KEY`, optional `TYPESAFE_API_KEY`. Nonsecret config: `AZURE_EMBEDDING_ENDPOINT`, `AZURE_EMBEDDING_DEPLOYMENT`. Use Container Apps secrets via secretRef, not build args or copied env files. Missing credentials cause safe 503 only for a requested inference mode, without silent BM25 fallback. Browse/order/return operations do not require search credentials.

Inference cache is bounded to 256 entries, ten minutes, process-local; concurrent identical requests share one call. Four distinct misses can run concurrently with bounded queueing. Embeddings are shared between semantic/hybrid. Jev cache keys include exact query and metadata candidate IDs. SOAP data is always fresh. Cache is not a durable pagination snapshot across restarts/replicas; Jev/paged inference needs further production validation before claiming strong snapshot consistency.

## Saved benchmark

Hit@1 is the fraction of scored queries whose first result is one of the saved expected product IDs. The common-subset rows compare all methods on the same queries with full metadata.

| Query group | N | BM25 Hit@1 | Semantic Hit@1 | Hybrid 50/50 Hit@1 | Jev baseline Hit@1 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Common scored subset | 436 | 70.6% | 81.0% | 83.7% | 97.9% |
| Natural Italian requests | 62 | 9.7% | 85.5% | 56.5% | 100.0% |
| Natural English requests | 62 | 46.8% | 87.1% | 64.5% | 100.0% |
| Complete scored set | 5,709 | 88.9% | 77.6% | 88.4% | Not evaluated |

The natural-request rows belong to the common subset, not additional independent samples. The complete set is dominated by synthetic lexical/property variants. Jev used whole-catalogue Choice + Noul, not runtime's independent per-candidate Noul composition. No SOAP, live eligibility or agent conversation pipeline was tested. Italian BM25 received raw Italian queries.

These results support semantic as the natural-language default and show that hybrid is not automatically better. The Jev baseline won the common subset, but labels require human review and a separate evaluation of runtime composition. No general 100% accuracy claim follows from 62 curated requests.

Public results are saved in [search-baseline.json](../tests/fixtures/search-baseline.json), with queries in [search-benchmark.jsonl](../tests/fixtures/search-benchmark.jsonl). Raw rankings, usage and experiment reports remain in ignored local `assignment/embeddings-experiment/`; do not publish that directory, which also contains private credentials. The [evaluation script](../scripts/evaluate_search.py) replays current methods only with explicit paid `--online` calls.

- `tests/fixtures/search-benchmark.jsonl`: 5,966 unique queries with fixed labels/families/language and scored/diagnostic flag. Public search text/product IDs only, no credentials or customer/order fixtures.
- `tests/fixtures/search-baseline.json`: original local BM25/vector/RRF results and common Jev subset.
- 5,709 scored queries are largely synthetic property/name variants. 62 English semantic requests and the same 62 Italian requests have assistant-curated, human-unreviewed labels.
- 237 original testbook search queries are diagnostic, not stock/budget/conversation acceptance. 20 no-match queries are separated; nearest neighbours are not a no-match detector.
- Original Jev result (97.9% Hit@1 on 436 scored common-subset queries) used **Choice + Noul on all 152 products**. Runtime's per-candidate Noul ranking is a different composition: do not transfer that percentage or cost untested.
- Existing exhaustive BM25/filter/live-pagination oracle stays offline with an explicit lexical test double. `tests/test_retrieval.py` separately tests actual mode routing/providers/cache/validation/filters/errors/one-tool schema without paid calls.

## Cost and release status

Vectors: catalogue embedding cost paid once on metadata change; a fresh query embeds only its own text. Hybrid reuses that embedding. Jev: shared candidate context plus one question per candidate; cost varies with candidate count and query/state length and is **not** the earlier 14.3k-token winner-only Choice+Noul cost. Keep batching inside a request, metadata scopes and cache; retain live verification in code. Actual API usage should be measured separately from cached ranking timings.

The backend serves API 0.8.0 with all three modes in the general `/openapi.json`. No separate static search specification is maintained. Changes to source goals or backend schemas do not automatically update the imported platform collection or publish the workspace. Verify the matching collection, goal and context workflow during release. Backend deployment, Git push and platform Publish are separate operations. Search releases must preserve the existing order/return transport, runtime secrets and submission gates.
