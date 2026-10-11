"""SOAP reads and a dedicated, non-retrying return sender."""

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

ENDPOINT = (
    "https://partner-program-tools-210543522374.europe-west3.run.app/ordinet/soap"
)
NS = "http://ordinet.giftshop.it/ws"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
AVAILABILITY = {
    "01": "available",
    "02": "low_stock",
    "03": "out_of_stock",
    "04": "discontinued",
}
ORDER_STATUS = {
    "ST01": "received",
    "ST02": "preparing",
    "ST03": "shipped",
    "ST04": "out_for_delivery",
    "ST05": "delivered",
    "ST06": "cancelled",
    "ST07": "return_in_progress",
}


class LegacyError(Exception):
    """Internal diagnostic code. Never expose this exception to the model."""

    def __init__(self, code: str):
        """Keep the diagnostic code internal; API handlers expose only normalized errors."""
        self.code = code
        super().__init__(code)


def text(root, name):
    """Read a trimmed XML descendant after transport removes namespace prefixes."""
    return root.findtext(f".//{name}", default="").strip()


def price(value: str) -> Decimal:
    """Convert observed integer cents or documented decimal euros; reject unsafe amounts."""
    value = value.strip()
    try:
        amount = (
            Decimal(value) / 100
            if re.fullmatch(r"\d+", value)
            else Decimal(value.replace(",", "."))
        )
        if not amount.is_finite() or amount < 0:
            raise ValueError
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise LegacyError("INVALID_PRICE") from None


def date(value: str) -> str | None:
    """Convert legacy DD/MM/YYYY to ISO; absent dates stay null and invalid dates fail safely."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date().isoformat()
    except ValueError:
        raise LegacyError("INVALID_DATE") from None


def order_number(value: str) -> str:
    """Extract the six-digit SOAP order ID from a validated customer-facing number."""
    match = re.fullmatch(r"GS-\d{4}-(\d{6})", value)
    if not match:
        raise LegacyError("INVALID_ORDER_NUMBER")
    return match.group(1)


class OrdiNet:
    def __init__(
        self, client: httpx.AsyncClient, user: str, password: str, writes_enabled=False
    ):
        """Reuse the HTTP client and guard session renewal with a per-process async lock."""
        self.client = client
        self.user = user
        self.password = password
        self.writes_enabled = writes_enabled
        self.session = ""
        self.session_started = 0.0
        self.login_lock = asyncio.Lock()

    async def _send(self, operation: str, fields: list[tuple[str, str]]):
        """Send one SOAP request, safely parse XML and translate transport/business failures."""
        if operation not in {"Login", "GetOrdine", "GetDisponibilita"}:
            raise LegacyError("LIVE_WRITES_DISABLED")
        return await self._exchange(operation, fields)

    async def _exchange(self, operation, fields):
        if operation == "CreaRichiestaReso" and not self.writes_enabled:
            raise LegacyError("LIVE_WRITES_DISABLED")
        envelope = ET.Element(f"{{{SOAP_NS}}}Envelope")
        body = ET.SubElement(envelope, f"{{{SOAP_NS}}}Body")
        request = ET.SubElement(body, f"{{{NS}}}{operation}")
        for name, value in fields:
            if isinstance(value, ET.Element):
                request.append(value)
            else:
                ET.SubElement(request, f"{{{NS}}}{name}").text = value
        try:
            response = await self.client.post(
                ENDPOINT,
                content=ET.tostring(envelope, encoding="utf-8", xml_declaration=True),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f'"{NS}/{operation}"',
                },
            )
            response.raise_for_status()
            root = SafeET.fromstring(response.content)
        except (httpx.HTTPError, ET.ParseError, DefusedXmlException):
            raise LegacyError("TRANSPORT_ERROR") from None
        for node in root.iter():
            node.tag = node.tag.rsplit("}", 1)[-1]
        if text(root, "Codice") != "OK":
            raise LegacyError(text(root, "CodErrore") or "INVALID_RESPONSE")
        return root

    async def _login(self, expired_session: str | None = None):
        """Renew stale sessions once under a lock, reusing a session another caller refreshed."""
        async with self.login_lock:
            if self.session and time.monotonic() - self.session_started < 540:
                if expired_session is None or self.session != expired_session:
                    return
            root = await self._send(
                "Login", [("Utente", self.user), ("Password", self.password)]
            )
            self.session = text(root, "IdSessione")
            if not self.session:
                raise LegacyError("INVALID_RESPONSE")
            self.session_started = time.monotonic()

    async def _read(self, operation: str, fields: list[tuple[str, str]]):
        """Retry reads only: one expired-session renewal and at most two transient retries."""
        expired_session = None
        refreshed = False
        retries = 0
        while True:
            try:
                await self._login(expired_session=expired_session)
                expired_session = None
                session = self.session
                return await self._send(operation, [("IdSessione", session), *fields])
            except LegacyError as error:
                if error.code == "SESSIONE_SCADUTA" and not refreshed:
                    expired_session = self.session
                    refreshed = True
                elif (
                    error.code in {"ERRORE_INTERNO", "TRANSPORT_ERROR"} and retries < 2
                ):
                    retries += 1
                    await asyncio.sleep(0.25 * retries)
                else:
                    raise

    async def availability(self, product_ids: list[str]) -> dict:
        """Fetch at most 20 live articles; absent, invalid or unsupported data stays unknown."""
        if not 1 <= len(product_ids) <= 20:
            raise ValueError("Availability requires 1 to 20 product IDs.")
        root = await self._read(
            "GetDisponibilita", [("CodArt", code) for code in product_ids]
        )
        products = {
            code: {"price_eur": None, "availability": "unknown", "stock": None}
            for code in product_ids
        }
        for article in root.findall(".//Articolo"):
            code = text(article, "CodArt")
            status = AVAILABILITY.get(text(article, "StatoDisp"))
            if code not in products or not status or text(article, "Valuta") != "EUR":
                continue
            try:
                amount = price(text(article, "PrezzoVendita"))
            except LegacyError:
                continue
            quantity = text(article, "Qta")
            products[code] = {
                "price_eur": amount,
                "availability": status,
                "stock": int(quantity) if re.fullmatch(r"\d+", quantity) else None,
            }
        return products

    async def order(self, customer_number: str):
        """Read one order and verify its returned ID; ownership checks happen in the API route."""
        number = order_number(customer_number)
        root = await self._read("GetOrdine", [("NumeroOrdine", number)])
        order = root.find(".//Ordine")
        if order is None or text(order, "NumeroOrdine") != number:
            raise LegacyError("INVALID_RESPONSE")
        return order

    async def create_return(self, customer_number, lines, reason):
        """Log in, then send exactly once. Never use the read-retry loop for writes."""
        if not self.writes_enabled:
            raise LegacyError("LIVE_WRITES_DISABLED")
        reasons = {
            "not_as_expected": "R01",
            "damaged": "R02",
            "wrong_item": "R03",
            "changed_mind": "R04",
            "other": "R05",
        }
        number = order_number(customer_number)
        code = reasons[reason]
        rows = ET.Element(f"{{{NS}}}Righe")
        for line in lines:
            row = ET.SubElement(rows, f"{{{NS}}}Riga")
            ET.SubElement(row, f"{{{NS}}}CodArt").text = line["product_id"]
            ET.SubElement(row, f"{{{NS}}}Qta").text = str(line["quantity"])
        await self._login()
        root = await self._exchange(
            "CreaRichiestaReso",
            [
                ("IdSessione", self.session),
                ("NumeroOrdine", number),
                ("Righe", rows),
                ("CodMotivo", code),
            ],
        )
        identifier = text(root, "IdReso")
        if not identifier or len(identifier) > 200:
            raise LegacyError("INVALID_RETURN_RESPONSE")
        return identifier

    @staticmethod
    def order_details(order, customer_number: str) -> dict:
        """Map status/dates/tracking without exposing customer email, address or raw XML."""
        return {
            "order_number": customer_number,
            "status": ORDER_STATUS.get(text(order, "StatoCod"), "unknown"),
            "expected_delivery_date": date(text(order, "DataConsegnaPrev")),
            "delivery_date": date(text(order, "DataConsegna")),
            "carrier": text(order, "Corriere") or None,
            "tracking_number": text(order, "NumTracking") or None,
        }
