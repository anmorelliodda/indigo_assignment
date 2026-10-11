"""REST operations exposed as agent tools."""

import hmac
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from catalog import category_key
from models import (
    CatalogOptions,
    CatalogOptionsRequest,
    Error,
    OrderRequest,
    OrderStatus,
    ProductFilters,
    SearchResults,
)
from ordinet import LegacyError, text
from retrieval import SearchUnavailable

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
ERRORS = {status: {"model": Error} for status in (401, 404, 422, 503)}


class ToolError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        """Carry an HTTP status and safe model-facing error, never an upstream exception."""
        self.status = status
        self.result = Error(code=code, message=message, retryable=retryable)


def redact_log(value, api_key: str):
    """Redact a logging copy, never the HTTP response."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "order_number":
                result[key] = "***" + str(item)[-4:]
            elif key.casefold() in {"email", "tracking_number", "headers", "authorization", "cookie", "password", "token", "confirmation_token", "api_key", "session"}:
                result[key] = "[REDACTED]" if item is not None else None
            else:
                result[key] = redact_log(item, api_key)
        return result
    if isinstance(value, list):
        return [redact_log(item, api_key) for item in value]
    if isinstance(value, str):
        value = value.replace(api_key, "[REDACTED]")
        return re.sub(r"[^\s@]+@[^\s@]+\.[^\s@]+", "[REDACTED]", value)
    return value


def record_arguments(request: Request, arguments: dict):
    """Keep supplied validated inputs; never collect headers."""
    request.state.tool_arguments = redact_log(arguments, request.app.state.api_key)


async def authenticate(request: Request, key: Annotated[str | None, Security(api_key_header)]):
    """Authorize the tool connection using the configured shared key, not customer identity."""
    if key is None or not hmac.compare_digest(key.encode(), request.app.state.api_key.encode()):
        raise ToolError(401, "unauthorized", "The tool connection is not authorized. Ask the operator to check its configuration.")


async def tool_error_handler(request: Request, error: ToolError):
    """Return a no-store error and a short retry hint for temporary service failures."""
    headers = {"Cache-Control": "no-store"}
    if error.status == 503:
        headers["Retry-After"] = "5"
    return JSONResponse(status_code=error.status, content=error.result.model_dump(), headers=headers)


async def legacy_error_handler(request: Request, error: Exception):
    """Hide legacy codes/XML and normalize upstream failures into a recoverable 503."""
    return await tool_error_handler(request, ToolError(
        503, "temporarily_unavailable", "Live information is unavailable. Do not guess prices, availability or order status. Try again later.", True,
    ))


async def validation_error_handler(request: Request, error: RequestValidationError):
    """Expose invalid field names only, never rejected values or private customer inputs."""
    fields = sorted({next((part for part in reversed(item["loc"]) if isinstance(part, str)), "filters") for item in error.errors()})
    return await tool_error_handler(request, ToolError(
        422, "invalid_request", "Check these input fields against the tool schema: " + ", ".join(fields),
    ))


router = APIRouter(dependencies=[Depends(authenticate)], responses=ERRORS)


@router.get(
    "/catalog/options",
    operation_id="get_catalog_options",
    response_model=CatalogOptions,
    description=(
        "Discover stored catalogue labels before using an unfamiliar filter, after an invalid-category error, "
        "or when the customer asks which categories, brands, colours, materials or occasions exist. "
        "Explore one field per call; default category. Optionally restrict to one returned category. "
        "query matches label substrings, not product descriptions or BM25. Labels are sorted alphabetically, not by popularity. "
        "Return at most limit labels (default 10, maximum 20); follow next_offset with unchanged arguments. "
        "Counts describe catalogue products, not sales, live stock or purchasable availability. "
        "List fields such as tags and occasions count each product once per label. "
        "Do not infer recipient or occasion constraints from the customer merely because a label exists. "
        "Use search_products for products and live price/availability. Do not call this tool before every search."
    ),
)
async def get_catalog_options(request: Request, filters: Annotated[CatalogOptionsRequest, Query()]):
    """Validate category scope and page through stored labels without consulting SOAP."""
    record_arguments(request, filters.model_dump(mode="json", include=set(request.query_params.keys())))
    catalog = request.app.state.catalog
    if filters.category and category_key(filters.category) not in catalog.categories:
        raise ToolError(422, "invalid_request", "Unknown category. Call get_catalog_options with field=category and no category filter; use a returned value.")
    return catalog.options(filters)


@router.post(
    "/products",
    operation_id="search_products",
    response_model=SearchResults,
    description=(
        "One tool for product discovery and detail. For known IDs send product_id as a JSON array, omit query and unwanted filters. "
        "fields selects output keys only; omit it or send [] for complete products. ID and relevance_score are always returned. "
        "Request name, description, price_eur, availability and mandatory feature fields for recommendations. "
        "Absent output fields were not requested, not necessarily unknown; request them before claiming facts. "
        "mode selects ranking in this same tool: semantic (default, multilingual vectors), hybrid (BM25 plus vectors/RRF), "
        "or jev (TypeSafe decision model, typed per-candidate match probabilities, no generated answer). "
        "Semantic and vector mean the same method. Use a standalone natural product request; do not routinely call all modes or generate many query variants. "
        "Omit query, leave blank or use * for browse/ID detail without any model call. "
        "Query only ranks; it never excludes filter-compatible candidates. Scores can be partial or weak matches: verify mandatory features. "
        "Semantic cosine and hybrid RRF are not probabilities; Jev probability is not proof of price, stock or eligibility. "
        "Compare scores only within the same mode and query; null means browse. No validated no-match threshold. "
        "Never send email addresses, order numbers or customer/order records in query. "

        "Choice lists match any value (OR); different filters must all match (AND). Send alternatives as JSON arrays in the request body. "
        "Tags also use OR. Combine only constraints stated by the customer. "
        "k sets page size: default 5, minimum 1, maximum 10. Never claim a global price/rating ranking from relevance or one page. "
        "Numeric ranges include both boundaries. Use exact stored catalogue labels in filters, not translated labels. "
        "Semantic/Jev query can remain in the customer's language. "
        "Price, quantity and availability are live. Other fields come from the catalogue. "
        "An explicit availability overrides available_only. "
        "Missing numeric data cannot match a requested range. Minimum values must not exceed maximum values. "
        "An empty page may have next_offset: check it before declaring no matches. "
        "Omitted filters do not restrict results, including availability. For purchasable gifts set available_only=true. "
        "Unknown products have unverified live data; never recommend them as available. "
        "Never use CSV prices or stock as a fallback."
    ),
)
async def search_products(request: Request, filters: ProductFilters):
    """Rank metadata candidates, fetch one bounded live batch and page only valid products."""
    supplied = (
        set(request.query_params)
        if request.method == "GET"
        else filters.model_fields_set
    )
    record_arguments(request, filters.model_dump(mode="json", include=supplied))
    catalog = request.app.state.catalog
    if any(category_key(value) not in catalog.categories for value in filters.category):
        raise ToolError(
            422,
            "invalid_request",
            "Unknown category. Call get_catalog_options with field=category and use a returned category value.",
        )
    if re.search(r"[^\s@]+@[^\s@]+|\bGS-\d{4}-\d{6}\b", filters.query, re.I):
        raise ToolError(422, "invalid_request", "Do not include email addresses or order numbers in a product-search query.")
    try:
        scores = await request.app.state.search.rank(filters)
    except SearchUnavailable as error:
        raise ToolError(503, "temporarily_unavailable", str(error), True) from None
    candidates = list(scores)
    # Bound SOAP calls to 20 candidates; return at most k (1..10) live-filter matches.
    selected = candidates[filters.offset : filters.offset + 20]
    live = await request.app.state.ordinet.availability(selected) if selected else {}
    results = []
    output_fields = {*filters.fields, "product_id", "relevance_score"}
    checked = 0
    for product_id in selected:
        checked += 1
        item = live[product_id]
        if filters.matches_live(item):
            product = {
                **catalog.details(product_id),
                **item,
                "relevance_score": scores[product_id],
            }
            # Select output only after all mandatory metadata/live constraints are applied.
            if filters.fields:
                product = {
                    key: value for key, value in product.items() if key in output_fields
                }
            results.append(product)
            if len(results) == filters.k:
                break
    next_offset = filters.offset + checked
    return {
        "search_mode": "browse" if filters.query in ("", "*") else filters.mode,
        "products": results,
        "next_offset": next_offset if next_offset < len(candidates) else None,
        "checked_candidates": checked,
    }


@router.get("/products", operation_id="search_products", response_model=SearchResults, include_in_schema=False)
async def search_products_get(request: Request, filters: Annotated[ProductFilters, Query()]):
    """Keep the currently published GET client working until its schema migrates."""
    return await search_products(request, filters)



@router.post(
    "/orders/status",
    operation_id="get_order_status",
    response_model=OrderStatus,
    description=(
        "Read an order status ONLY after the customer supplies their order number and order email. "
        "Do not call this tool to discover the order email. Ask for missing values before calling. "
        "Never invent placeholder emails or enumerate credentials. "
        "The service compares the email before releasing any order information. "
        "order_not_verified does not distinguish a missing order from an incorrect email. "
        "Unknown means the current status could not be interpreted. Explain this without guessing its meaning. "
        "Delivery estimates are not guarantees."
    ),
)
async def get_order_status(request: Request, body: OrderRequest, response: Response):
    """Verify order-email match before releasing normalized order details."""
    record_arguments(request, {"order_number": "***" + body.order_number[-4:], "email": "[REDACTED]"})
    unverified = ToolError(404, "order_not_verified", "The order could not be verified. Ask the customer to check their order number and order email.")
    try:
        order = await request.app.state.ordinet.order(body.order_number)
    except LegacyError as error:
        if error.code == "ORDINE_NON_TROVATO":
            raise unverified from None
        raise
    expected_email = text(order, "Email").strip().casefold()
    if not expected_email or not hmac.compare_digest(expected_email.encode(), body.email.casefold().encode()):
        raise unverified
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.ordinet.order_details(order, body.order_number)
