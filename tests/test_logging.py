"""Request/output logs are correlated, bounded and redact sensitive data."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app, log_event
from test_backend import FakeOrdiNet


class ToolLoggingTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"TOOL_API_KEY": "test-key", "ORDINET_USER": "test", "ORDINET_PASSWORD": "test"})
        self.environment.start()
        self.client = TestClient(app)
        self.client.__enter__()
        app.state.ordinet = FakeOrdiNet()
        app.state.search.rank = AsyncMock(side_effect=app.state.catalog.search)
        self.headers = {"X-API-Key": "test-key", "X-Chat-User-Ref": "private-visitor-reference"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()

    def logged(self, method, path, **kwargs):
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            response = self.client.request(method, path, **kwargs)
        messages = [record.getMessage() for record in captured.records]
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[0].startswith(">>> REQUEST tool_request "))
        failed = response.status_code >= 400
        prefix = "!!! ERROR tool_output " if failed else "<<< OUTPUT tool_output "
        self.assertTrue(messages[1].startswith(prefix))
        self.assertTrue(all(len(message.encode("utf-8")) < 3600 for message in messages))
        self.assertEqual(captured.records[1].levelname, "ERROR" if response.status_code >= 500 else "WARNING" if failed else "INFO")
        events = [json.loads(message.split(" ", 3)[3]) for message in messages]
        self.assertEqual(len({event["call_id"] for event in events}), 1)
        self.assertTrue(all(event["tool"] == events[0]["tool"] and event["status"] == response.status_code for event in events))
        self.events = events
        return response, events[0], " | ".join(messages)

    def test_omitted_defaults_are_not_logged(self):
        response, event, _ = self.logged("GET", "/products", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(event, {"call_id": event["call_id"], "tool": "search_products", "arguments": {}, "status": 200})

    def test_explicit_defaults_and_arrays_are_logged(self):
        _, event, message = self.logged("GET", "/products", headers=self.headers, params={
            "query": "*", "k": 5, "offset": 0, "available_only": False,
            "category": ["Home & Living", "Kitchen & Dining"], "max_price_eur": 50,
        })
        self.assertEqual(event["arguments"], {
            "query": "*", "k": 5, "offset": 0, "available_only": False,
            "category": ["Home & Living", "Kitchen & Dining"], "max_price_eur": "50",
        })
        self.assertNotIn("min_stock", event["arguments"])
        self.assertNotIn("test-key", message)
        self.assertNotIn("private-visitor-reference", message)

    def test_catalog_options_and_detail_are_named(self):
        _, event, _ = self.logged("GET", "/catalog/options", headers=self.headers)
        self.assertEqual(event["tool"], "get_catalog_options")
        self.assertEqual(event["arguments"], {})
        _, event, _ = self.logged("GET", "/catalog/options", headers=self.headers, params={"field": "brand", "category": "Kitchen & Dining", "limit": 3})
        self.assertEqual(event["arguments"], {"field": "brand", "category": "Kitchen & Dining", "limit": 3})
        _, event, _ = self.logged("POST", "/products", headers=self.headers, json={"product_id": ["HL-003"], "fields": ["name"]})
        self.assertEqual(event["tool"], "search_products")
        self.assertEqual(event["arguments"], {"product_id": ["HL-003"], "fields": ["name"]})
        self.assertEqual(set(self.events[1]["output"]["products"][0]), {"product_id", "name", "relevance_score"})

    def test_order_body_and_headers_never_leak(self):
        for email, status in (("giulia.rinaldi@example.com", 200), ("wrong@example.com", 404)):
            _, event, message = self.logged("POST", "/orders/status", headers=self.headers, json={
                "order_number": "GS-2026-000101", "email": email,
            })
            self.assertEqual(event, {"call_id": event["call_id"], "tool": "get_order_status", "arguments": {
                "order_number": "***0101", "email": "[REDACTED]",
            }, "status": status})
            for private in (email, "GS-2026-000101", "test-key", "private-visitor-reference"):
                self.assertNotIn(private, message)
            self.assertNotIn("headers", message)

    def test_email_and_key_in_search_are_redacted_and_lines_escaped(self):
        _, event, message = self.logged("GET", "/products", headers=self.headers, params={
            "query": "hello@example.com test-key\nfake-log-line", "brand": ["person@example.com"],
        })
        self.assertNotIn("hello@example.com", message)
        self.assertNotIn("person@example.com", message)
        self.assertNotIn("test-key", message)
        self.assertNotIn("\n", message)
        self.assertIn("[REDACTED]", event["arguments"]["query"])

    def test_rejected_requests_do_not_dump_unvalidated_inputs(self):
        for kwargs in ({"params": {"k": 11}, "headers": self.headers}, {"headers": {"X-API-Key": "invalid-secret"}}):
            response, event, message = self.logged("GET", "/products", **kwargs)
            self.assertIn(response.status_code, (401, 422))
            self.assertEqual(event["arguments"], {})
            self.assertNotIn("invalid-secret", message)
        _, event, message = self.logged("POST", "/orders/status", headers=self.headers,
            json={"order_number": "PRIVATE-BAD-NUMBER", "email": "private@example.com"})
        self.assertEqual(event["status"], 422)
        self.assertEqual(event["arguments"], {})
        self.assertNotIn("private@example.com", message)
        self.assertNotIn("PRIVATE-BAD-NUMBER", message)

    def test_search_output_items_and_pagination(self):
        response, _, _ = self.logged("GET", "/products", headers=self.headers, params={"k": 10})
        data = response.json()
        output = self.events[1]["output"]
        self.assertEqual(output["returned_count"], 10)
        self.assertEqual(output["next_offset"], data["next_offset"])
        self.assertEqual(output["checked_candidates"], data["checked_candidates"])
        for logged, product in zip(output["products"], data["products"]):
            expected = {key: product[key] for key in ("product_id", "name", "price_eur", "availability", "stock", "relevance_score")}
            if expected["relevance_score"] is not None:
                expected["relevance_score"] = round(expected["relevance_score"], 3)
            self.assertEqual(logged, expected)
            self.assertNotIn("description", logged)
            self.assertIn("description", product)
        self.assertEqual(len(output["products"]), 10)
        self.assertEqual(int(response.headers["content-length"]), len(response.content))
        response, _, _ = self.logged("GET", "/products", headers=self.headers, params={"query": "candle"})
        score = response.json()["products"][0]["relevance_score"]
        self.assertEqual(self.events[1]["output"]["products"][0]["relevance_score"], round(score, 3))
        self.assertNotEqual(score, round(score, 3))

    def test_options_output_items_and_empty_results(self):
        response, _, _ = self.logged("GET", "/catalog/options", headers=self.headers, params={"limit": 20})
        self.assertEqual(self.events[1]["output"], {"field": "category", "next_offset": None, "returned_count": 11, "values": response.json()["values"]})
        response, _, _ = self.logged("GET", "/products", headers=self.headers, params={"query": "nonexistentword", "brand": "unknown-brand"})
        self.assertEqual(response.json()["products"], [])
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[1]["output"]["returned_count"], 0)

    def test_order_output_tracking_is_redacted_only_in_logs(self):
        import xml.etree.ElementTree as ET
        from unittest.mock import AsyncMock
        app.state.ordinet.order = AsyncMock(return_value=ET.fromstring(
            '<Ordine><Email>giulia.rinaldi@example.com</Email><StatoCod>ST03</StatoCod><NumTracking>private-tracking-value</NumTracking></Ordine>'
        ))
        response, _, message = self.logged("POST", "/orders/status", headers=self.headers, json={
            "order_number": "GS-2026-000101", "email": "giulia.rinaldi@example.com",
        })
        self.assertEqual(response.json()["tracking_number"], "private-tracking-value")
        self.assertEqual(response.json()["order_number"], "GS-2026-000101")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.events[1]["output"]["tracking_number"], "[REDACTED]")
        self.assertEqual(self.events[1]["output"]["order_number"], "***0101")
        self.assertNotIn("private-tracking-value", message)
        self.assertNotIn("GS-2026-000101", message)

    def test_metadata_output_email_key_and_newlines_are_redacted(self):
        app.state.catalog.products["HL-003"]["name"] = "hello@example.com test-key\nfake-log-line"
        response, _, message = self.logged("POST", "/products", headers=self.headers, json={"product_id": ["HL-003"]})
        self.assertIn("hello@example.com", response.json()["products"][0]["name"])
        self.assertNotIn("hello@example.com", message)
        self.assertNotIn("test-key", message)
        self.assertNotIn("\n", message)
        self.assertIn("[REDACTED]", self.events[1]["output"]["products"][0]["name"])
        self.assertNotIn("description", self.events[1]["output"]["products"][0])

    def test_call_ids_differ_and_error_output_is_logged(self):
        _, first, _ = self.logged("GET", "/catalog/options", headers=self.headers)
        response, second, _ = self.logged("GET", "/catalog/options", headers={"X-API-Key": "wrong"})
        self.assertNotEqual(first["call_id"], second["call_id"])
        self.assertEqual(self.events[1]["output"], response.json())
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_live_failure_has_one_error_output(self):
        from unittest.mock import AsyncMock
        from ordinet import LegacyError
        app.state.ordinet.availability = AsyncMock(side_effect=LegacyError("UPSTREAM_UNAVAILABLE"))
        response, _, _ = self.logged("GET", "/products", headers=self.headers, params={"available_only": True})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.events[1]["output"], response.json())
        self.assertTrue(self.events[1]["output"]["retryable"])

    def test_large_events_stay_valid_json_and_http_unchanged(self):
        import logging
        event = {"call_id": "test", "tool": "search_products", "status": 200, "arguments": {"query": "長" * 1000}}
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            log_event(logging.getLogger("uvicorn.error"), ">>> REQUEST tool_request", event)
        message = captured.records[0].getMessage()
        self.assertLess(len(message.encode("utf-8")), 3600)
        self.assertIn("log_data_omitted", json.loads(message.split(" ", 3)[3]))
        app.state.catalog.products["HL-003"]["name"] = "Long name " * 500
        response, _, _ = self.logged("POST", "/products", headers=self.headers, json={"product_id": ["HL-003"]})
        self.assertEqual(response.json()["products"][0]["name"], "Long name " * 500)
        self.assertIn("log_data_omitted", self.events[1])

    def test_non_tool_routes_are_not_logged(self):
        with self.assertNoLogs("uvicorn.error", level="INFO"):
            for path in ("/health", "/shop/", "/openapi.json"):
                self.assertEqual(self.client.get(path).status_code, 200)
