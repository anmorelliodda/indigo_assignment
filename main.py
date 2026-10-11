"""Application setup, shared clients and safe tool logging."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from catalog import Catalog
from ordinet import LegacyError, OrdiNet
from retrieval import SearchEngine
from returns import ReturnService, TableStore
from returns import router as return_router
from tools import (
    ToolError,
    legacy_error_handler,
    redact_log,
    router,
    tool_error_handler,
    validation_error_handler,
)

ROOT = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load configuration and share one metadata catalogue and SOAP client per process."""
    load_dotenv(ROOT / ".env")
    required = {
        name: os.environ.get(name, "")
        for name in ("ORDINET_USER", "ORDINET_PASSWORD", "TOOL_API_KEY")
    }
    if not all(required.values()):
        raise RuntimeError(
            "Set ORDINET_USER, ORDINET_PASSWORD and TOOL_API_KEY in .env or environment variables."
        )
    app.state.api_key = required["TOOL_API_KEY"]
    app.state.catalog = Catalog(ROOT / "data" / "gift-shop-catalog.csv")
    submission_enabled = os.environ.get("RETURN_SUBMISSION_ENABLED") == "true"
    async with (
        httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3)) as client,
        httpx.AsyncClient(timeout=httpx.Timeout(20, connect=3)) as search_client,
    ):
        app.state.search = SearchEngine(
            app.state.catalog, search_client, ROOT / "data" / "search-vectors.json"
        )
        app.state.ordinet = OrdiNet(
            client,
            required["ORDINET_USER"],
            required["ORDINET_PASSWORD"],
            writes_enabled=submission_enabled,
        )
        app.state.returns = None
        table_client = credential = None
        if os.environ.get("RETURNS_ENABLED") == "true":
            if (
                "sandbox" in os.environ["RETURN_TABLE_NAME"].casefold()
                or os.environ.get("RETURN_DEMO_DELIVERY") == "true"
            ):
                raise RuntimeError(
                    "Real returns require a separate non-sandbox table and genuine delivery data."
                )
            from azure.core.exceptions import ResourceExistsError
            from azure.data.tables import TableClient
            from azure.identity import AzureCliCredential, ManagedIdentityCredential

            credential = (
                AzureCliCredential()
                if os.environ.get("RETURN_AZURE_AUTH") == "cli"
                else ManagedIdentityCredential(
                    client_id=os.environ.get("AZURE_CLIENT_ID")
                )
            )
            table_client = TableClient(
                endpoint=os.environ["RETURN_TABLE_ENDPOINT"],
                table_name=os.environ["RETURN_TABLE_NAME"],
                credential=credential,
            )
            try:
                await asyncio.to_thread(table_client.create_table)
            except ResourceExistsError:
                pass
            app.state.returns = ReturnService(
                TableStore(table_client),
                os.environ["RETURN_HASH_KEY"],
                os.environ.get("RETURN_ALLOWED_ORDER", ""),
                os.environ.get("RETURN_ALLOWED_EMAIL", ""),
                submission_enabled=submission_enabled,
            )
        try:
            yield
        finally:
            if table_client:
                table_client.close()
                credential.close()


app = FastAPI(title="Gift Shop Tools", version="0.8.0", lifespan=lifespan)
app.add_exception_handler(ToolError, tool_error_handler)
app.add_exception_handler(LegacyError, legacy_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.include_router(router)
app.include_router(return_router)
app.mount("/shop", StaticFiles(directory=ROOT / "frontend", html=True), name="shop")


def log_event(logger, prefix, event, level=logging.INFO):
    """Emit one valid JSON event; bound console bytes without cutting JSON or HTTP data."""
    payload = json.dumps(event, separators=(",", ":"))
    if len(payload) > 3500:
        # Rare long inputs/results: keep correlation and report omitted log data explicitly.
        event = {key: event[key] for key in ("call_id", "tool", "status")}
        event["log_data_omitted"] = "Console size limit; HTTP response unchanged"
        payload = json.dumps(event, separators=(",", ":"))
    logger.log(level, "%s %s", prefix, payload)


def compact_product(product):
    """Keep useful product fields; round only the logged score, never the API response."""
    fields = (
        "product_id",
        "name",
        "price_eur",
        "availability",
        "stock",
        "relevance_score",
    )
    item = {key: product[key] for key in fields if key in product}
    if item.get("relevance_score") is not None:
        item["relevance_score"] = round(item["relevance_score"], 3)
    return item


@app.middleware("http")
async def log_tool_calls(request: Request, call_next):
    """Log redacted tool inputs/results with a common ID; preserve HTTP bytes and headers."""
    response = await call_next(request)
    route = request.scope.get("route")
    operation = getattr(route, "operation_id", None)
    if not operation:
        return response
    # Tool responses are bounded JSON, not streams. Preserve their actual bytes/headers.
    body = b"".join([chunk async for chunk in response.body_iterator])
    try:
        output = redact_log(json.loads(body), request.app.state.api_key)
    except (json.JSONDecodeError, UnicodeDecodeError):
        output = {"omitted": "Non-JSON output"}
    logger = logging.getLogger("uvicorn.error")
    context = {
        "call_id": uuid4().hex,
        "tool": operation,
        "status": response.status_code,
    }
    log_event(
        logger,
        ">>> REQUEST tool_request",
        {**context, "arguments": getattr(request.state, "tool_arguments", {})},
    )
    # Exactly one compact output per handled call, even for ten products or twenty options.
    if isinstance(output, dict) and "products" in output:
        output["returned_count"] = len(output["products"])
        output["products"] = [compact_product(item) for item in output["products"]]
    elif isinstance(output, dict) and "values" in output:
        output["returned_count"] = len(output["values"])
    failed = response.status_code >= 400
    level = (
        logging.ERROR
        if response.status_code >= 500
        else logging.WARNING
        if failed
        else logging.INFO
    )
    prefix = "!!! ERROR tool_output" if failed else "<<< OUTPUT tool_output"
    log_event(logger, prefix, {**context, "output": output}, level)
    return Response(
        body,
        status_code=response.status_code,
        headers=response.headers,
        background=response.background,
    )


@app.get("/health", include_in_schema=False)
async def health():
    """Report process readiness without making a slow upstream SOAP request."""
    return {"status": "ok"}
