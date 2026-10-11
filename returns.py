"""Real returns: durable send claim, one SOAP attempt, no automatic resend."""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.data.tables import UpdateMode
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from models import OrderRequest
from ordinet import ORDER_STATUS, LegacyError, date, order_number, text
from tools import ERRORS, ToolError, authenticate, record_arguments

REASONS = {
    "not_as_expected": "Non corrisponde alle aspettative",
    "damaged": "Articolo danneggiato",
    "wrong_item": "Articolo errato",
    "changed_mind": "Ripensamento",
    "other": "Altro",
}


def utcnow():
    return datetime.now(timezone.utc)


class ReturnLine(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(min_length=1, max_length=40)
    quantity: int = Field(strict=True, ge=1, le=1000)


class ReturnRequest(OrderRequest):
    operation: Literal["options", "prepare", "approve", "cancel", "status"]
    lines: list[ReturnLine] | None = Field(default=None, min_length=1, max_length=20)
    reason: (
        Literal["not_as_expected", "damaged", "wrong_item", "changed_mind", "other"]
        | None
    ) = None
    draft_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def operation_fields(self):
        if self.operation == "prepare":
            if self.lines is None or self.reason is None:
                raise ValueError("Preparation requires lines and reason.")
            if len({line.product_id for line in self.lines}) != len(self.lines):
                raise ValueError("Duplicate article lines are not allowed.")
        elif self.lines is not None or self.reason is not None:
            raise ValueError("Only preparation accepts lines and reason.")
        if (self.operation in {"approve", "cancel"}) != (self.draft_id is not None):
            raise ValueError("Only a decision requires draft_id.")
        return self


class ReturnOptions(BaseModel):
    order_number: str
    lines: list[ReturnLine]
    eligible: bool
    eligibility_message: str
    reason_options: list[str]
    existing_state: (
        Literal[
            "prepared", "submitting", "submitted", "submission_unknown", "cancelled"
        ]
        | None
    ) = None
    submission_enabled: bool = False
    policy_source: Literal["supplied_documentation_not_write_verified"] = (
        "supplied_documentation_not_write_verified"
    )


class ReturnStatus(BaseModel):
    """Canonical summary and outcome. No model-supplied consent or approval token."""

    draft_id: str | None = None
    order_number: str
    lines: list[ReturnLine] = Field(default_factory=list)
    reason: str | None = None
    summary: str = ""
    expires_at: str | None = None
    state: Literal[
        "not_prepared",
        "prepared",
        "submitting",
        "submitted",
        "submission_unknown",
        "cancelled",
    ]
    return_id: str | None = None
    observed_order_status: str | None = None
    message: str
    decision_recorded: bool = False
    submission_enabled: bool = False
    submission_stopped: bool = False


class TableStore:
    """Use only the PRIMARY endpoint: one atomic operation row per order partition."""

    def __init__(self, client):
        self.client = client

    def get(self, partition):
        try:
            entity = self.client.get_entity(partition, "operation")
        except ResourceNotFoundError:
            return None
        return json.loads(entity["Data"]), entity.metadata["etag"]

    def insert(self, partition, data):
        try:
            self.client.create_entity(
                {
                    "PartitionKey": partition,
                    "RowKey": "operation",
                    "Data": json.dumps(data),
                }
            )
            return True
        except ResourceExistsError:
            return False

    def replace(self, partition, data, etag):
        try:
            self.client.update_entity(
                {
                    "PartitionKey": partition,
                    "RowKey": "operation",
                    "Data": json.dumps(data),
                },
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except HttpResponseError as error:
            if error.status_code in (404, 409, 412):
                return False
            raise


class ReturnService:
    """One durable operation per verified order. Unknown sends are never retried."""

    def __init__(
        self,
        store,
        key,
        allowed_order="",
        allowed_email="",
        clock=utcnow,
        submission_enabled=False,
    ):
        if len(key) < 32:
            raise ValueError("Set a stable RETURN_HASH_KEY of at least 32 characters.")
        if bool(allowed_order) != bool(allowed_email.strip()):
            raise ValueError(
                "An optional order restriction requires both order and email."
            )
        if allowed_order:
            order_number(allowed_order)
        self.store, self.key = store, key.encode()
        self.allowed_order, self.allowed_email = (
            allowed_order,
            allowed_email.strip().casefold(),
        )
        self.clock = clock
        self.submission_enabled = submission_enabled

    def digest(self, value):
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def scope(self, body):
        # Optional deployment restriction; actual SOAP order/email verification is mandatory.
        if self.allowed_order and body.order_number != self.allowed_order:
            raise ToolError(
                404, "order_not_verified", "The order could not be verified."
            )
        if self.allowed_email and not hmac.compare_digest(
            body.email.casefold().encode(), self.allowed_email.encode()
        ):
            raise ToolError(
                404, "order_not_verified", "The order could not be verified."
            )
        # A numeric order key also prevents aliases with another year creating a second row.
        # Browser identity is deliberately not authorization. Each operation verifies SOAP email.
        return "order-" + order_number(body.order_number), self.digest(
            "email:" + body.email.casefold()
        )

    async def storage(self, method, *args):
        try:
            return await asyncio.to_thread(getattr(self.store, method), *args)
        except Exception:
            raise ToolError(
                503,
                "temporarily_unavailable",
                "Return verification storage is unavailable. No new submission is allowed.",
            ) from None

    async def verified_order(self, ordinet, body):
        try:
            order = await ordinet.order(body.order_number)
        except LegacyError as error:
            if error.code == "ORDINE_NON_TROVATO":
                raise ToolError(
                    404, "order_not_verified", "The order could not be verified."
                ) from None
            raise
        expected = text(order, "Email").casefold()
        if not expected or not hmac.compare_digest(
            expected.encode(), body.email.casefold().encode()
        ):
            raise ToolError(
                404, "order_not_verified", "The order could not be verified."
            )
        return order

    def eligible(self, order, lines):
        if text(order, "StatoCod") != "ST05":
            raise ToolError(
                422, "invalid_request", "Returns require a genuinely delivered order."
            )
        try:
            delivered = datetime.fromisoformat(date(text(order, "DataConsegna"))).date()
        except (TypeError, ValueError, LegacyError):
            raise ToolError(
                422, "invalid_request", "The delivery date cannot be verified."
            ) from None
        # Documented policy, not verified by a real return write. Inclusive calendar days.
        today = self.clock().astimezone(ZoneInfo("Europe/Rome")).date()
        if not 0 <= (today - delivered).days <= 30:
            raise ToolError(
                422,
                "invalid_request",
                "Delivery is outside the documented 30-day return window.",
            )
        quantities = {}
        for row in order.findall(".//Righe/Riga"):
            code, raw = text(row, "CodArt"), text(row, "Qta")
            if not code or not re.fullmatch(r"[0-9]+", raw) or code in quantities:
                raise ToolError(
                    422, "invalid_request", "Order lines cannot be verified safely."
                )
            quantities[code] = int(raw)
        if any(
            line["product_id"] not in quantities
            or line["quantity"] > quantities[line["product_id"]]
            for line in lines
        ):
            raise ToolError(
                422,
                "invalid_request",
                "Select articles and quantities actually present in this order.",
            )

    def event(self, request):
        """Read the platform-controlled event, never a model argument. Do not trim text."""
        encoded = request.headers.get("X-Chat-User-Message-B64", "")
        message_id = request.headers.get("X-Chat-Message-Id", "")
        try:
            message = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise ToolError(
                422,
                "invalid_request",
                "Configure a valid Base64 user message in the tool connection.",
            ) from None
        if not encoded or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", message_id):
            raise ToolError(
                422,
                "invalid_request",
                "Configure the user message and message ID in the tool connection.",
            )
        return message, message_id

    def result(self, data):
        state = data["state"]
        messages = {
            "prepared": "Mostra il riepilogo. Attendi un NUOVO CONFERMO oppure ANNULLA. Invio reale possibile solo se la proposta dichiara submission_enabled=true; altrimenti la conferma e solo una prova bloccata.",
            "submitting": "Invio preso in carico, esito non ancora registrato. Non inviare di nuovo: richiedere verifica manuale se persiste.",
            "submitted": "Reso gia creato. Nessuna nuova conferma o nuovo invio in questa chiamata.",
            "submission_unknown": "Esito dell'invio non verificabile. Il reso potrebbe essere stato creato. Non inviare di nuovo; serve verifica manuale.",
            "cancelled": "Proposta gia annullata, nessun reso inviato. Nessuna nuova decisione registrata.",
        }
        if state not in messages:
            raise ToolError(
                503,
                "temporarily_unavailable",
                "The stored state is not supported. Do not prepare or submit another return.",
            )
        return ReturnStatus(
            **{
                k: data[k]
                for k in (
                    "draft_id",
                    "order_number",
                    "lines",
                    "reason",
                    "summary",
                    "expires_at",
                    "state",
                )
            },
            message=messages[state],
            return_id=data.get("return_id"),
            submission_enabled=data.get("submission_enabled", False),
        )

    async def options(self, request, body, ordinet):
        partition, _ = self.scope(body)
        order = await self.verified_order(ordinet, body)
        current = await self.storage("get", partition)
        existing_state = None
        if current:
            existing_state = self.result(current[0]).state
        lines = []
        for row in order.findall(".//Righe/Riga"):
            code, quantity = text(row, "CodArt"), text(row, "Qta")
            if (
                not code
                or not re.fullmatch(r"[0-9]+", quantity)
                or not 1 <= int(quantity) <= 1000
            ):
                raise ToolError(
                    422, "invalid_request", "Order lines cannot be verified safely."
                )
            lines.append({"product_id": code, "quantity": int(quantity)})
        if not 1 <= len(lines) <= 20 or len({x["product_id"] for x in lines}) != len(
            lines
        ):
            raise ToolError(
                422, "invalid_request", "Order lines cannot be verified safely."
            )
        eligible, message = (
            True,
            "Eligible under the documented delivered-order return policy. Confirmation is still required.",
        )
        try:
            self.eligible(order, lines)
        except ToolError as error:
            eligible, message = False, error.result.message
        return ReturnOptions(
            order_number=body.order_number,
            lines=lines,
            eligible=eligible,
            eligibility_message=message,
            reason_options=list(REASONS),
            existing_state=existing_state,
            submission_enabled=self.submission_enabled,
        )

    def same_proposal(self, data, lines, reason):
        """Reuse only an open, fresh proposal with identical choices and sending mode."""
        return (
            data["state"] == "prepared"
            and data["lines"] == lines
            and data["reason"] == reason
            and data.get("submission_enabled") == self.submission_enabled
            and bool(data.get("prepared_message_id"))
            and self.clock() < datetime.fromisoformat(data["expires_at"])
        )

    async def prepare(self, request, body, ordinet):
        partition, owner = self.scope(body)
        user_message, message_id = self.event(request)
        if user_message in {"CONFERMO", "ANNULLA"}:
            raise ToolError(
                422,
                "invalid_request",
                "Una decisione non puo preparare una nuova proposta. Leggi lo stato della proposta corrente.",
            )
        lines = sorted(
            [line.model_dump() for line in body.lines],
            key=lambda line: line["product_id"],
        )
        current = await self.storage("get", partition)
        order = await self.verified_order(ordinet, body)
        history = []
        if current:
            old, etag = current
            self.result(old)  # Unsupported stored states fail closed.
            if old["state"] not in {"prepared", "cancelled"}:
                raise ToolError(
                    422,
                    "invalid_request",
                    "An invocation was already claimed or completed. Read status. Never prepare or send again.",
                )
            history = list(old.get("cancelled_history", []))
            if old["state"] == "cancelled":
                if message_id in {
                    old.get("prepared_message_id"),
                    old.get("decision_message_id"),
                } or old.get("return_id"):
                    raise ToolError(
                        422,
                        "invalid_request",
                        "A new request in a new turn is required after cancellation.",
                    )
                history.append(
                    {
                        key: value
                        for key, value in old.items()
                        if key != "cancelled_history"
                    }
                )
        self.eligible(order, lines)
        if current and self.same_proposal(old, lines, body.reason):
            return self.result(old)
        mode = "RESO REALE" if self.submission_enabled else "PROVA RESO"
        articles = ", ".join(
            f"{line['quantity']} x {line['product_id']}" for line in lines
        )
        consent = (
            "CONFERMO autorizza l'invio a OrdiNet."
            if self.submission_enabled
            else "CONFERMO verifica il consenso ma NON invia il reso; ultimo step bloccato sul server."
        )
        data = {
            "draft_id": uuid4().hex,
            "owner": owner,
            "order_number": body.order_number,
            "lines": lines,
            "reason": body.reason,
            "state": "prepared",
            "expires_at": (self.clock() + timedelta(minutes=15)).isoformat(),
            "prepared_message_id": message_id,
            "submission_enabled": self.submission_enabled,
            "cancelled_history": history,
            "summary": f"{mode} - Ordine {body.order_number}: {articles}. Motivo: {REASONS[body.reason]}. {consent}",
        }
        if len(json.dumps(data).encode("utf-16-le")) > 60000:
            raise ToolError(
                503,
                "temporarily_unavailable",
                "Proposal history requires manual archival. No records were deleted or changed.",
            )
        success = (
            await self.storage("replace", partition, data, etag)
            if current
            else await self.storage("insert", partition, data)
        )
        if success:
            return self.result(data)
        # Never retry a stale modification over a concurrent cancellation/decision.
        latest = await self.storage("get", partition)
        if latest:
            saved = latest[0]
            if self.same_proposal(saved, lines, body.reason):
                return self.result(saved)
        raise ToolError(
            422,
            "invalid_request",
            "The proposal changed concurrently. Read the current summary before another operation.",
        )

    async def decide(self, request, body, ordinet):
        partition, _ = self.scope(body)
        message, message_id = self.event(request)
        current = await self.storage("get", partition)
        order = await self.verified_order(ordinet, body)
        if not current:
            raise ToolError(
                404, "order_not_verified", "The request could not be verified."
            )
        data, etag = current
        if body.draft_id != data["draft_id"]:
            raise ToolError(
                422, "invalid_request", "Decision does not match the current proposal."
            )
        expected = "CONFERMO" if body.operation == "approve" else "ANNULLA"
        if (
            message != expected
            or not data.get("prepared_message_id")
            or message_id == data["prepared_message_id"]
        ):
            raise ToolError(
                422,
                "invalid_request",
                "Serve un nuovo messaggio esattamente " + expected + ".",
            )
        if data["state"] != "prepared":
            if (body.operation == "cancel" and data["state"] == "cancelled") or (
                body.operation == "approve"
                and data["state"] in {"submitting", "submitted", "submission_unknown"}
            ):
                return self.result(data)
            raise ToolError(
                422, "invalid_request", "This proposal cannot accept another decision."
            )
        if self.clock() >= datetime.fromisoformat(data["expires_at"]):
            raise ToolError(
                422, "invalid_request", "Proposal expired. Prepare and review it again."
            )
        if body.operation == "approve":
            if self.submission_enabled and not data.get("submission_enabled"):
                raise ToolError(
                    422,
                    "invalid_request",
                    "Preview proposal cannot authorize a real send. Prepare a new real-send summary and wait for another CONFERMO.",
                )
            self.eligible(order, data["lines"])
            if self.clock() >= datetime.fromisoformat(data["expires_at"]):
                raise ToolError(
                    422,
                    "invalid_request",
                    "Proposal expired during verification. Prepare and review it again.",
                )
            if not self.submission_enabled:
                if data.get("preview_message_id") != message_id:
                    preview = {**data, "preview_message_id": message_id}
                    if not await self.storage("replace", partition, preview, etag):
                        raise ToolError(
                            422,
                            "invalid_request",
                            "The proposal changed. Read its current state.",
                        )
                return self.result(data).model_copy(
                    update={
                        "submission_stopped": True,
                        "message": "CONFERMO verificato per la prova. Invio bloccato sul server: nessuna chiamata CreaRichiestaReso, nessun reso creato e nessuna conferma definitiva. La proposta resta aperta.",
                    }
                )
            if message_id == data.get("preview_message_id"):
                raise ToolError(
                    422,
                    "invalid_request",
                    "The preview confirmation cannot authorize a real send. Show the real-send summary and wait for a new CONFERMO.",
                )
        claimed = {
            **data,
            "state": "submitting" if body.operation == "approve" else "cancelled",
            "decision_at": self.clock().isoformat(),
            "decision_message_id": message_id,
        }
        if not await self.storage("replace", partition, claimed, etag):
            latest = await self.storage("get", partition)
            if latest and latest[0]["draft_id"] == body.draft_id:
                return self.result(latest[0])
            raise ToolError(
                422, "invalid_request", "The proposal changed. Read its current state."
            )
        if body.operation == "cancel":
            return self.result(claimed).model_copy(
                update={
                    "decision_recorded": True,
                    "message": "Proposta annullata. Nessun reso inviato.",
                }
            )
        # Read back the durable claim before any network write. Only this CAS winner sends.
        current = await self.storage("get", partition)
        if not current or current[0] != claimed:
            raise ToolError(
                503,
                "temporarily_unavailable",
                "Send claim cannot be verified. Do not retry submission.",
            )
        outcome = {**claimed, "state": "submission_unknown"}
        try:
            outcome["return_id"] = await ordinet.create_return(
                body.order_number, data["lines"], data["reason"]
            )
            outcome["state"] = "submitted"
        except Exception:
            # Includes timeouts, malformed responses and missing IdReso. Never resend.
            pass
        try:
            saved = await self.storage("replace", partition, outcome, current[1])
        except ToolError:
            saved = False
        if not saved:
            outcome["state"] = "submission_unknown"
        result = self.result(outcome).model_copy(update={"decision_recorded": True})
        if outcome["state"] == "submitted":
            result = result.model_copy(
                update={
                    "message": "Reso creato in OrdiNet. Comunica il return_id restituito, senza promettere un rimborso."
                }
            )
        return result

    async def check_status(self, request, body, ordinet):
        """Read only. Order ST07 does not identify our return or recover a missing IdReso."""
        partition, _ = self.scope(body)
        order = await self.verified_order(ordinet, body)
        observed = ORDER_STATUS.get(text(order, "StatoCod"), "unknown")
        # Read Azure AFTER slow SOAP so a completed concurrent cancellation is visible.
        current = await self.storage("get", partition)
        if not current:
            return ReturnStatus(
                order_number=body.order_number,
                state="not_prepared",
                observed_order_status=observed,
                submission_enabled=self.submission_enabled,
                message="Ordine verificato. Non esiste ancora una proposta nel nostro flusso. Chiedi articolo, quantita e motivo mancanti. Usa options per verificare ammissibilita e scelte disponibili. Non chiedere di correggere numero ordine o email gia verificati.",
            )
        proposal = self.result(current[0])
        message = proposal.message + " Current order status: " + observed + "."
        if observed == "return_in_progress":
            message += " Order status alone does not identify this return. Use only the recorded return_id."
        return proposal.model_copy(
            update={"observed_order_status": observed, "message": message}
        )


def return_service(request):
    service = getattr(request.app.state, "returns", None)
    if service is None:
        raise ToolError(404, "not_found", "Returns are disabled.")
    return service


router = APIRouter(dependencies=[Depends(authenticate)], responses=ERRORS)


@router.post(
    "/returns",
    operation_id="manage_return",
    response_model=ReturnOptions | ReturnStatus,
    description=(
        "Manage a REAL return with the user's order number and purchase email. "
        "Use operation=options to check eligibility and read articles, quantities and reasons. "
        "If existing_state is null, no proposal exists. Ask for missing article, quantity and reason. Then use prepare. "
        "If existing_state is prepared, continue with that proposal. "
        "If existing_state is submitting, submitted or submission_unknown, use status. Never prepare or send again. "
        "If existing_state is cancelled, report cancellation. A new prepare requires an explicit new user request in a new turn. "
        "Use operation=prepare with lines and reason to create the proposal. Show the returned summary. "
        "Wait for a NEW user message exactly CONFERMO before operation=approve. "
        "Use operation=cancel only after a NEW user message exactly ANNULLA. "
        "Both decisions require the current draft_id. Show the returned summary before consent. "
        "Do not call status only because options returned existing_state=null. "
        "Status not_prepared means the order is verified but no proposal exists. Ask for missing choices, not credentials again. "
        "To change a prepared proposal, use prepare directly with the user's new choices. Do not cancel first. "
        "cancel requires the entire message ANNULLA. A longer request to change the reason is not cancellation. "
        "Do not call status and a decision in parallel. Use one mutation per turn. "
        "After a successful decision, report its result. Do not call options, status or prepare in that turn. "
        "Browser visitor identity is not an authorization requirement. Every operation verifies order/email against SOAP. "
        "The backend checks the platform message headers, draft, expiry and new message ID. "
        "Do not supply action, confirmed, user_message, message_id or token. "
        "decision_recorded=true means this call recorded a NEW decision. False means no new decision. "
        "submitted means SOAP returned a recorded return_id. submitting/submission_unknown require manual verification, never retry. "
        "Never invent articles, quantities or reason. Ask for missing user choices. "
        "If submission_enabled=false, approve only validates the preview: submission_stopped=true, proposal stays prepared, decision_recorded=false. Never claim a return was created. Otherwise approve can create a REAL return. cancel only cancels a prepared proposal, never a return already sent. No automatic resend."
    ),
)
async def manage_return(body: ReturnRequest, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    record_arguments(request, body.model_dump(exclude_none=True))
    service = return_service(request)
    service.event(request)
    if body.operation == "status":
        return await service.check_status(request, body, request.app.state.ordinet)
    source = request.app.state.ordinet
    if body.operation == "options":
        return await service.options(request, body, source)
    if body.operation == "prepare":
        return await service.prepare(request, body, source)
    return await service.decide(request, body, source)
