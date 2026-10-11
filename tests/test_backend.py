import csv
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import xml.etree.ElementTree as ET

import httpx
from fastapi.testclient import TestClient

from main import app
from catalog import Catalog
from models import ProductFilters
from ordinet import LegacyError, OrdiNet, price


class BM25Tests(unittest.TestCase):
    def test_relevance_case_punctuation_and_stale_fields(self):
        with Path("data/gift-shop-catalog.csv").open(encoding="utf-8-sig", newline="") as file:
            template = next(csv.DictReader(file))
        rows = [
            {**template, "product_id": "A", "name": "Cedar Candle", "description": "Rareterm", "tags": "", "price_eur": "999991234", "stock": "888887777"},
            {**template, "product_id": "B", "name": "Desk Tray", "description": "cedar", "tags": ""},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=template.keys())
                writer.writeheader()
                writer.writerows(rows)
            catalog = Catalog(path)
            self.assertEqual(list(catalog.search(ProductFilters(query="CEDAR!!!"))), ["A", "B"])
            self.assertEqual(next(iter(catalog.search(ProductFilters(query="rareterm cedar")))), "A")
            for query in ("999991234", "888887777", "!!!"):
                self.assertEqual(catalog.search(ProductFilters(query=query)), {"A": 0.0, "B": 0.0})
            self.assertEqual(list(catalog.search(ProductFilters(query="cedar", product_id=["B"]))), ["B"])
            scores = catalog.search(ProductFilters(query="rareterm"))
            self.assertGreater(scores["A"], 0)
            self.assertEqual(scores["B"], 0)
            self.assertEqual(list(scores), ["A", "B"])
            self.assertEqual(catalog.search(ProductFilters(query="cedar")), catalog.search(ProductFilters(query="cedar")))


class FakeOrdiNet:
    async def availability(self, codes):
        return {code: {"price_eur": Decimal("38.00"), "availability": "available", "stock": 12} for code in codes}

    async def order(self, number):
        if number == "GS-2026-999999":
            raise LegacyError("ORDINE_NON_TROVATO")
        return ET.fromstring('<Ordine><Email>giulia.rinaldi@example.com</Email><StatoCod>ST01</StatoCod></Ordine>')

    order_details = staticmethod(OrdiNet.order_details)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict("os.environ", {"TOOL_API_KEY": "test-key", "ORDINET_USER": "test", "ORDINET_PASSWORD": "test"})
        self.environment.start()
        self.client = TestClient(app)
        self.client.__enter__()
        app.state.ordinet = FakeOrdiNet()
        # Preserve the existing independent lexical/filter oracle without paid inference.
        # Actual default/mode dispatch is covered separately in test_retrieval.py.
        app.state.search.rank = AsyncMock(side_effect=app.state.catalog.search)
        self.headers = {"X-API-Key": "test-key", "X-Chat-User-Ref": "visitor-1"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()

    def test_shop_is_public_and_separate_from_tools(self):
        response = self.client.get("/shop/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Sottobosco", response.text)
        self.assertIn("0a015e5c-50ee-41c3-96bc-faf2e2e0e7ae", response.text)
        self.assertNotIn("4ac6e5b9-58c4-48e1-bb0e-cf85cc93cb43", response.text)
        self.assertNotIn("Anteprima Test", response.text)
        self.assertIn("trigger=off", response.text)
        self.assertNotIn("/shop", self.client.get("/openapi.json").json()["paths"])
        self.assertEqual(self.client.get("/catalog/options").status_code, 401)

    def test_shop_redirect_and_unknown_asset(self):
        self.assertEqual(self.client.get("/shop", follow_redirects=False).status_code, 307)
        self.assertEqual(self.client.get("/shop/missing.html").status_code, 404)

    def test_shop_cannot_serve_backend_files(self):
        for path in ("/shop/.env", "/shop/main.py", "/shop/%2e%2e%2f.env"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_authentication(self):
        response = self.client.get("/catalog/options")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "unauthorized")

    def test_bm25_authentication_and_optional_query(self):
        self.assertEqual(self.client.get("/products", params={"query": "candle"}).status_code, 401)
        for params in ({}, {"query": ""}, {"query": "   "}, {"query": " * "}):
            data = self.client.get("/products", params=params, headers=self.headers).json()
            self.assertEqual([p["product_id"] for p in data["products"]], list(app.state.catalog.products)[:5])

    def test_bm25_matches_any_term(self):
        params = {"query": "refillable nonexistentword"}
        data = self.client.get("/products", params=params, headers=self.headers).json()
        self.assertIn("HL-003", [p["product_id"] for p in data["products"]])

    def test_bm25_preserves_metadata_and_live_filters(self):
        params = {"query": "candle", "brand": "Fold Studio", "gift_wrap": True, "max_price_eur": 40}
        data = self.client.get("/products", params=params, headers=self.headers).json()
        self.assertTrue(data["products"])
        self.assertTrue(all(p["brand"] == "Fold Studio" and p["gift_wrap"] and p["price_eur"] == "38.00" for p in data["products"]))
        params["max_price_eur"] = 30
        self.assertEqual(self.client.get("/products", params=params, headers=self.headers).json()["products"], [])

    def test_bm25_no_match_returns_labelled_filter_compatible_alternatives(self):
        data = self.client.get("/products", params={"query": "nonexistentword", "category": "Tech & Gadgets", "available_only": True}, headers=self.headers).json()
        self.assertEqual(len(data["products"]), 5)
        self.assertTrue(all(p["category"] == "Tech & Gadgets" and p["relevance_score"] == 0 for p in data["products"]))
        self.assertIsNotNone(data["next_offset"])
        detail = self.client.post("/products", json={"product_id": ["HL-003"]}, headers=self.headers).json()["products"][0]
        self.assertIsNone(detail["relevance_score"])

    def test_bm25_pagination_preserves_rank_without_duplicates(self):
        filters = ProductFilters(query="home", available_only=False)
        expected = list(app.state.catalog.search(filters))
        offset, found = 0, []
        while offset is not None:
            response = self.client.get("/products", params={"query": "home", "available_only": False, "offset": offset}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertLessEqual(len(data["products"]), 5)
            found.extend(p["product_id"] for p in data["products"])
            if data["next_offset"] is not None:
                self.assertGreater(data["next_offset"], offset)
            offset = data["next_offset"]
        self.assertEqual(found, expected)

    def test_bm25_invalid_category_and_ranges(self):
        for params in ({"category": "unknown"}, {"min_price_eur": 40, "max_price_eur": 30}):
            response = self.client.get("/products", params={"query": "candle", **params}, headers=self.headers)
            self.assertEqual(response.status_code, 422)

    def test_catalog_options_categories(self):
        data = self.client.get("/catalog/options", params={"limit": 20}, headers=self.headers).json()
        self.assertEqual(data["field"], "category")
        self.assertEqual(len(data["values"]), 11)
        self.assertEqual(sum(item["product_count"] for item in data["values"]), 152)
        self.assertIsNone(data["next_offset"])

    def test_catalog_options_default_page_and_no_soap(self):
        app.state.ordinet.availability = AsyncMock()
        app.state.ordinet.order = AsyncMock()
        data = self.client.get("/catalog/options", headers=self.headers).json()
        self.assertEqual(len(data["values"]), 10)
        self.assertEqual(data["next_offset"], 10)
        app.state.ordinet.availability.assert_not_awaited()
        app.state.ordinet.order.assert_not_awaited()

    def test_catalog_options_all_fields_against_csv(self):
        from collections import Counter
        category_labels = {}
        for row in app.state.catalog.products.values():
            key = row["category"].strip().casefold().replace(" and ", " & ")
            category_labels.setdefault(key, row["category"].strip())
        for field in ("category", "subcategory", "brand", "color", "material", "occasion", "tags", "recipient"):
            with self.subTest(field=field):
                counts = Counter()
                for row in app.state.catalog.products.values():
                    labels = row[field].split("|") if field in ("occasion", "tags") else [row[field]]
                    if field == "category":
                        labels = [category_labels[row[field].strip().casefold().replace(" and ", " & ")]]
                    counts.update({label.strip() for label in labels if label.strip()})
                expected = [{"value": value, "product_count": counts[value]} for value in sorted(counts, key=lambda value: (value.casefold(), value))]
                found, offset = [], 0
                while offset is not None:
                    response = self.client.get("/catalog/options", params={"field": field, "limit": 3, "offset": offset}, headers=self.headers)
                    self.assertEqual(response.status_code, 200)
                    data = response.json()
                    self.assertLessEqual(len(data["values"]), 3)
                    found.extend(data["values"])
                    if data["next_offset"] is not None:
                        self.assertGreater(data["next_offset"], offset)
                    offset = data["next_offset"]
                self.assertEqual(found, expected)

    def test_catalog_options_category_alias_and_label_query(self):
        params = {"field": "subcategory", "category": " home AND living ", "query": " CANDLE "}
        response = self.client.get("/catalog/options", params=params, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        expected = sum(row["category"].strip().casefold().replace(" and ", " & ") == "home & living" and row["subcategory"] == "Candles" for row in app.state.catalog.products.values())
        self.assertEqual(response.json()["values"], [{"value": "Candles", "product_count": expected}])
        # The query searches option labels, not descriptions of products with this brand.
        self.assertEqual(self.client.get("/catalog/options", params={"field": "brand", "query": "candle"}, headers=self.headers).json()["values"], [])

    def test_catalog_options_empty_and_beyond_last_page(self):
        for params in ({"query": "nonexistentword"}, {"offset": 10000}):
            data = self.client.get("/catalog/options", params=params, headers=self.headers).json()
            self.assertEqual(data["values"], [])
            self.assertIsNone(data["next_offset"])

    def test_catalog_options_validation_and_auth(self):
        invalid = ({"field": "price_eur"}, {"field": "stock"}, {"field": "name"}, {"field": "unknown"},
                   {"limit": 0}, {"limit": 21}, {"offset": -1}, {"offset": 10001},
                   {"category": "unknown"}, {"category": " "}, {"query": "x" * 101}, {"available_only": True})
        for params in invalid:
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/catalog/options", params=params, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get("/catalog/options").status_code, 401)

    def test_catalog_options_deduplicates_per_product_and_omits_blanks(self):
        catalog = app.state.catalog
        first = next(iter(catalog.products.values()))
        catalog.products = {
            "A": {**first, "product_id": "A", "tags": "repeat|repeat|second|", "occasion": "birthday|birthday", "material": ""},
            "B": {**first, "product_id": "B", "tags": "repeat", "occasion": "", "material": ""},
        }
        data = self.client.get("/catalog/options", params={"field": "tags"}, headers=self.headers).json()
        self.assertEqual(data["values"], [{"value": "repeat", "product_count": 2}, {"value": "second", "product_count": 1}])
        data = self.client.get("/catalog/options", params={"field": "occasion"}, headers=self.headers).json()
        self.assertEqual(data["values"], [{"value": "birthday", "product_count": 1}])
        self.assertEqual(self.client.get("/catalog/options", params={"field": "material"}, headers=self.headers).json()["values"], [])

    def test_catalog_options_openapi_and_retired_categories(self):
        schema = self.client.get("/openapi.json").json()
        operation = schema["paths"]["/catalog/options"]["get"]
        self.assertEqual(operation["operationId"], "get_catalog_options")
        params = {p["name"]: p for p in operation["parameters"]}
        self.assertEqual(set(params), {"field", "category", "query", "limit", "offset"})
        self.assertEqual(params["limit"]["schema"]["maximum"], 20)
        self.assertEqual(params["field"]["schema"]["default"], "category")
        self.assertNotIn("/categories", schema["paths"])
        self.assertEqual(self.client.get("/categories", headers=self.headers).status_code, 404)

    def test_live_price_and_budget(self):
        result = self.client.get("/products", params={"query": "candle", "max_price_eur": 40}, headers=self.headers).json()
        self.assertTrue(result["products"])
        self.assertTrue(all(item["price_eur"] == "38.00" for item in result["products"]))
        result = self.client.get("/products", params={"query": "candle", "max_price_eur": 30}, headers=self.headers).json()
        self.assertEqual(result["products"], [])

    def test_optional_filters_for_every_catalogue_field(self):
        values = {
            "name": "EMBER", "category": "home and living", "subcategory": "candles",
            "brand": "fold studio", "recipient": "ANYONE", "occasion": "birthday",
            "color": "terracotta", "material": "soy wax", "description": "black pepper",
            "gift_wrap": True, "query": "ceramic candle", "tags": ["candle", "refillable"],
            "min_price_eur": 38, "max_price_eur": 38, "min_stock": 12, "max_stock": 12,
            "min_rating": 4.8, "max_rating": 4.8,
            "min_reviews_count": 431, "max_reviews_count": 431,
            "min_shipping_days": 2, "max_shipping_days": 2, "availability": "available",
        }
        for field, value in values.items():
            with self.subTest(field=field):
                response = self.client.get("/products", params={"product_id": "hl-003", field: value}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item["product_id"] for item in response.json()["products"]], ["HL-003"])
        response = self.client.get("/products", params={"product_id": "HL-003", **values}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["products"]), 1)

    def test_filters_exclude_nonmatches(self):
        values = {
            "name": "missing", "brand": "other", "material": "steel", "recipient": "kids",
            "subcategory": "knives", "color": "blue", "description": "missing",
            "occasion": "graduation", "tags": ["missing"], "gift_wrap": False,
            "min_price_eur": 39, "max_price_eur": 37, "min_stock": 13, "max_stock": 11,
            "min_rating": 4.9, "max_rating": 4.7,
            "min_reviews_count": 432, "max_reviews_count": 430,
            "min_shipping_days": 3, "max_shipping_days": 1,
        }
        for field, value in values.items():
            with self.subTest(field=field):
                response = self.client.get("/products", params={"product_id": "HL-003", field: value}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["products"], [])

    def test_invalid_ranges_and_values(self):
        for field in ("price_eur", "stock", "rating", "reviews_count", "shipping_days"):
            with self.subTest(field=field):
                response = self.client.get("/products", params={f"min_{field}": 2, f"max_{field}": 1}, headers=self.headers)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_request")
        for params in ({"min_stock": -1}, {"min_rating": 6}, {"availability": "invalid"}):
            self.assertEqual(self.client.get("/products", params=params, headers=self.headers).status_code, 422)

    def test_missing_metadata_does_not_match_ranges(self):
        response = self.client.get("/products", params={"product_id": "HL-008", "min_rating": 0}, headers=self.headers)
        self.assertEqual(response.json()["products"], [])

    def test_availability_overrides_default_and_stock_is_live(self):
        async def unavailable(codes):
            return {code: {"price_eur": Decimal("38.00"), "availability": "out_of_stock", "stock": 0} for code in codes}
        app.state.ordinet.availability = unavailable
        response = self.client.get("/products", params={"product_id": "HL-003", "available_only": True}, headers=self.headers)
        self.assertEqual(response.json()["products"], [])
        response = self.client.get("/products", params={"product_id": "HL-003", "availability": "out_of_stock", "max_stock": 0}, headers=self.headers)
        self.assertEqual(response.json()["products"][0]["stock"], 0)
        response = self.client.get("/products", params={"product_id": "HL-003", "available_only": False, "min_stock": 1}, headers=self.headers)
        self.assertEqual(response.json()["products"], [])

    def test_unknown_product_does_not_hide_other_results(self):
        async def mixed(codes):
            return {
                code: {"price_eur": None, "availability": "unknown", "stock": None}
                if code == "HL-003" else {"price_eur": Decimal("38.00"), "availability": "available", "stock": 12}
                for code in codes
            }
        app.state.ordinet.availability = mixed
        response = self.client.get("/products", params={"query": "candle", "available_only": True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HL-003", [item["product_id"] for item in response.json()["products"]])
        self.assertIn("HL-004", [item["product_id"] for item in response.json()["products"]])
        detail = self.client.post("/products", json={"product_id": ["HL-003"]}, headers=self.headers).json()["products"][0]
        self.assertEqual(detail["availability"], "unknown")
        self.assertIsNone(detail["price_eur"])
        response = self.client.get("/products", params={"product_id": "HL-003", "availability": "unknown"}, headers=self.headers)
        self.assertEqual(response.json()["products"][0], detail)
        response = self.client.get("/products", params={"product_id": "HL-003", "available_only": False, "min_price_eur": 0}, headers=self.headers)
        self.assertEqual(response.json()["products"], [])

    def test_unknown_order_status_still_requires_email_verification(self):
        app.state.ordinet.order = AsyncMock(return_value=ET.fromstring(
            '<Ordine><Email>giulia.rinaldi@example.com</Email><StatoCod>ST09</StatoCod></Ordine>'
        ))
        body = {"order_number": "GS-2026-000115", "email": "giulia.rinaldi@example.com"}
        response = self.client.post("/orders/status", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "unknown")
        self.assertNotIn("ST09", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body["email"] = "wrong@example.com"
        self.assertEqual(self.client.post("/orders/status", json=body, headers=self.headers).status_code, 404)

    def test_filter_arguments_in_openapi(self):
        schema = self.client.get("/openapi.json").json()
        body = schema["components"]["schemas"]["ProductFilters"]
        parameters = body["properties"]
        for field in ("product_id", "name", "brand", "tags", "min_price_eur", "max_price_eur", "min_stock", "max_stock", "min_rating", "max_rating", "min_reviews_count", "max_reviews_count", "min_shipping_days", "max_shipping_days"):
            self.assertIn(field, parameters)
            self.assertNotIn(field, body.get("required", []))

    def test_selectable_fields_and_complete_default(self):
        from typing import get_args
        from models import ProductField
        body = {"product_id": ["hl-003"]}
        complete = self.client.post("/products", json=body, headers=self.headers).json()
        self.assertEqual(set(complete["products"][0]), set(get_args(ProductField)))
        self.assertEqual(self.client.post("/products", json={**body, "fields": []}, headers=self.headers).json(), complete)
        for field in get_args(ProductField):
            with self.subTest(field=field):
                data = self.client.post("/products", json={**body, "fields": [field]}, headers=self.headers).json()
                expected = {key: value for key, value in complete["products"][0].items() if key in {field, "product_id", "relevance_score"}}
                self.assertEqual(data["products"], [expected])
                self.assertEqual(data["next_offset"], complete["next_offset"])
                self.assertEqual(data["checked_candidates"], complete["checked_candidates"])
        response = self.client.get("/products", params={"product_id": "hl-003", "fields": ["name", "price_eur"]}, headers=self.headers)
        self.assertEqual(set(response.json()["products"][0]), {"product_id", "relevance_score", "name", "price_eur"})

    def test_projection_keeps_ranking_filters_and_pagination(self):
        body = {"query": "candle", "max_price_eur": 40, "available_only": True, "k": 2}
        complete = self.client.post("/products", json=body, headers=self.headers).json()
        selected = self.client.post("/products", json={**body, "fields": ["name", "name"]}, headers=self.headers).json()
        self.assertEqual(selected, {**complete, "products": [{key: value for key, value in product.items() if key in {"product_id", "name", "relevance_score"}} for product in complete["products"]]})
        self.assertTrue(all(p["relevance_score"] > 0 for p in selected["products"]))
        response = self.client.post("/products", json={"product_id": ["HL-003"], "fields": ["name"], "max_price_eur": 37}, headers=self.headers)
        self.assertEqual(response.json()["products"], [])

    def test_requested_unknown_values_are_null_not_omitted(self):
        app.state.ordinet.availability = AsyncMock(return_value={"HL-008": {"price_eur": None, "stock": None, "availability": "unknown"}})
        body = {"product_id": ["HL-008"], "fields": ["rating", "price_eur", "stock", "availability"]}
        product = self.client.post("/products", json=body, headers=self.headers).json()["products"][0]
        self.assertEqual(product, {"product_id": "HL-008", "relevance_score": None, "rating": None, "price_eur": None, "stock": None, "availability": "unknown"})
        body["available_only"] = True
        self.assertEqual(self.client.post("/products", json=body, headers=self.headers).json()["products"], [])

    def test_invalid_projection_is_rejected_before_soap(self):
        app.state.ordinet.availability = AsyncMock()
        for fields in (["nonexistent"], "name", [None], ["name"] * 20, None):
            with self.subTest(fields=fields):
                response = self.client.post("/products", json={"fields": fields}, headers=self.headers)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_request")
        app.state.ordinet.availability.assert_not_awaited()

    def test_projection_schema_matches_output_contract(self):
        schema = self.client.get("/openapi.json").json()["components"]["schemas"]
        product = schema["SearchProduct"]
        fields = schema["ProductFilters"]["properties"]["fields"]
        self.assertEqual(set(product["required"]), {"product_id", "relevance_score"})
        self.assertEqual(set(fields["items"]["enum"]), set(product["properties"]))
        self.assertEqual(fields["maxItems"], 19)
        self.assertNotIn("Product", schema)

    def test_post_search_arrays_and_unicode(self):
        body = {"category": ["Tech & Gadgets", "Outdoor & Travel"], "max_price_eur": 50, "available_only": True, "query": "è", "k": 10}
        response = self.client.post("/products", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["products"])
        self.assertTrue(all(p["category"] in body["category"] for p in response.json()["products"]))
        self.assertEqual(self.client.post("/products", json=body).status_code, 401)
        self.assertEqual(self.client.post("/products", json={**body, "category": "Tech & Gadgets"}, headers=self.headers).status_code, 422)

    def test_pagination(self):
        result = self.client.get("/products", headers=self.headers).json()
        self.assertEqual(len(result["products"]), 5)
        self.assertEqual(result["next_offset"], 5)

    def test_empty_page_checks_a_full_soap_batch(self):
        response = self.client.get("/products", params={"max_price_eur": 5}, headers=self.headers)
        data = response.json()
        self.assertEqual(data["products"], [])
        self.assertEqual(data["checked_candidates"], 20)
        self.assertEqual(data["next_offset"], 20)

    def test_sparse_pagination_does_not_skip_or_duplicate_matches(self):
        ids = list(app.state.catalog.products)
        expected = [ids[i] for i in (3, 9, 17, 21, 25, 29, 33, 37, 41)]

        async def sparse(codes):
            self.assertLessEqual(len(codes), 20)
            return {code: {"price_eur": Decimal("1.00" if code in expected else "38.00"),
                           "availability": "available", "stock": 12} for code in codes}

        app.state.ordinet.availability = sparse
        offset, found = 0, []
        while offset is not None:
            data = self.client.get("/products", params={"max_price_eur": 5, "offset": offset}, headers=self.headers).json()
            self.assertLessEqual(len(data["products"]), 5)
            self.assertLessEqual(data["checked_candidates"], 20)
            found.extend(item["product_id"] for item in data["products"])
            if data["next_offset"] is not None:
                self.assertGreater(data["next_offset"], offset)
            offset = data["next_offset"]
        self.assertEqual(found, expected)

    def test_unknown_category(self):
        response = self.client.get("/products", params={"category": "unknown"}, headers=self.headers)
        self.assertEqual(response.status_code, 422)

    def test_catalogue_metadata_in_details_and_search(self):
        detail = self.client.post("/products", json={"product_id": ["HL-003"]}, headers=self.headers).json()["products"][0]
        self.assertEqual(detail["rating"], 4.8)
        self.assertEqual(detail["reviews_count"], 431)
        self.assertEqual(detail["recipient"], "anyone")
        self.assertEqual(detail["occasion"], ["thank-you", "birthday", "christmas"])
        self.assertEqual(detail["shipping_days"], 2)
        results = self.client.get("/products", params={"query": "candle"}, headers=self.headers).json()
        found = next(item for item in results["products"] if item["product_id"] == "HL-003")
        self.assertGreater(found.pop("relevance_score"), 0)
        self.assertEqual(found, {key: value for key, value in detail.items() if key != "relevance_score"})

    def test_missing_catalogue_metadata_and_zero_shipping_days(self):
        detail = self.client.post("/products", json={"product_id": ["HL-008"]}, headers=self.headers).json()["products"][0]
        self.assertIsNone(detail["rating"])
        self.assertIsNone(detail["reviews_count"])
        detail = self.client.post("/products", json={"product_id": ["HL-013"]}, headers=self.headers).json()["products"][0]
        self.assertIsNone(detail["occasion"])
        detail = self.client.post("/products", json={"product_id": ["EX-001"]}, headers=self.headers).json()["products"][0]
        self.assertEqual(detail["shipping_days"], 0)
        self.assertIsNone(detail["color"])
        self.assertIsNone(detail["material"])

    def test_metadata_in_openapi(self):
        properties = self.client.get("/openapi.json").json()["components"]["schemas"]["SearchProduct"]["properties"]
        for field in ("rating", "reviews_count", "recipient", "occasion", "shipping_days"):
            self.assertIn(field, properties)
        self.assertIn("delivery guarantee", properties["shipping_days"]["description"])

    def test_missing_product(self):
        response = self.client.post("/products", json={"product_id": ["UNKNOWN"]}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["products"], [])
        self.assertEqual(self.client.get("/products/HL-003", headers=self.headers).status_code, 404)

    def test_email_verification_and_minimal_response(self):
        response = self.client.post("/orders/status", json={"order_number": "GS-2026-000101", "email": "GIULIA.RINALDI@example.com"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "received")
        self.assertNotIn("email", response.json())
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_wrong_email_and_missing_order_indistinguishable(self):
        wrong = self.client.post("/orders/status", json={"order_number": "GS-2026-000101", "email": "wrong@example.com"}, headers=self.headers)
        missing = self.client.post("/orders/status", json={"order_number": "GS-2026-999999", "email": "wrong@example.com"}, headers=self.headers)
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(wrong.json(), missing.json())

    def test_validation_does_not_echo_email(self):
        response = self.client.post("/orders/status", json={"order_number": "bad", "email": "private-value"}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-value", response.text)

    def test_order_does_not_require_platform_visitor_header(self):
        body = {"order_number": "GS-2026-000101", "email": "giulia.rinaldi@example.com"}
        for visitor in (None, "{{$user_ref}}", " "):
            headers = {"X-API-Key": "test-key"}
            if visitor is not None:
                headers["X-Chat-User-Ref"] = visitor
            response = self.client.post("/orders/status", json=body, headers=headers)
            self.assertEqual(response.status_code, 200)
            wrong = self.client.post("/orders/status", json={**body, 'email': 'wrong@example.com'}, headers=headers)
            self.assertEqual(wrong.status_code, 404)

    def test_order_checks_have_no_application_rate_limit(self):
        body = {"order_number": "GS-2026-000101", "email": "giulia.rinaldi@example.com"}
        for _ in range(12):
            response = self.client.post("/orders/status", json=body, headers=self.headers)
            self.assertEqual(response.status_code, 200)
        schema = self.client.get("/openapi.json").json()
        self.assertNotIn("429", schema["paths"]["/orders/status"]["post"]["responses"])

    def test_visitor_id_does_not_bypass_email_verification(self):
        response = self.client.post("/orders/status", json={"order_number": "GS-2026-000101", "email": "wrong@example.com"}, headers={**self.headers, "X-Chat-User-Ref": "giulia"})
        self.assertEqual(response.status_code, 404)
        schema = self.client.get("/openapi.json").json()
        arguments = schema["components"]["schemas"]["OrderRequest"]["properties"]
        self.assertEqual(set(arguments), {"order_number", "email"})

    def test_openapi_tool_mapping(self):
        schema = self.client.get("/openapi.json").json()
        operations = [operation for path in schema["paths"].values() for operation in path.values()]
        self.assertEqual({item["operationId"] for item in operations}, {"get_catalog_options", "search_products", "get_order_status", "manage_return"})
        self.assertTrue(all(item.get("security") for item in operations))


class SoapTests(unittest.IsolatedAsyncioTestCase):
    def test_price_conversion(self):
        for value in ("4200", "42.00", "42,00"):
            self.assertEqual(price(value), Decimal("42.00"))
        with self.assertRaises(LegacyError):
            price("NaN")

    async def test_session_renewal_after_http_200_error(self):
        logins = 0
        reads = 0

        def transport(request):
            nonlocal logins, reads
            root = ET.fromstring(request.content)
            operation = list(list(root)[0])[0].tag.rsplit("}", 1)[-1]
            if operation == "Login":
                logins += 1
                payload = f'<LoginResponse><Esito><Codice>OK</Codice></Esito><IdSessione>s{logins}</IdSessione></LoginResponse>'
            else:
                reads += 1
                if reads == 1:
                    payload = '<Response><Esito><Codice>KO</Codice><CodErrore>SESSIONE_SCADUTA</CodErrore></Esito></Response>'
                else:
                    payload = '<Response><Esito><Codice>OK</Codice></Esito><Articoli><Articolo><CodArt>HL-003</CodArt><StatoDisp>03</StatoDisp><PrezzoVendita>4200</PrezzoVendita><Valuta>EUR</Valuta></Articolo></Articoli></Response>'
            return httpx.Response(200, content=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result = await OrdiNet(client, "user", "password").availability(["HL-003"])
        self.assertEqual(logins, 2)
        self.assertEqual(reads, 2)
        self.assertEqual(result["HL-003"]["availability"], "out_of_stock")
        self.assertEqual(result["HL-003"]["price_eur"], Decimal("42.00"))

    async def test_session_renewal_does_not_consume_transient_retry(self):
        login = ET.fromstring('<Response><IdSessione>session</IdSessione></Response>')
        article = ET.fromstring('<Response><Articolo><CodArt>HL-004</CodArt><StatoDisp>01</StatoDisp><PrezzoVendita>3800</PrezzoVendita><Valuta>EUR</Valuta></Articolo></Response>')
        async with httpx.AsyncClient() as client:
            api = OrdiNet(client, "user", "password")
            api._send = AsyncMock(side_effect=[login, LegacyError("SESSIONE_SCADUTA"), login, LegacyError("ERRORE_INTERNO"), article])
            result = await api.availability(["HL-004"])
            operations = [call.args[0] for call in api._send.await_args_list]
        self.assertEqual(operations, ["Login", "GetDisponibilita", "Login", "GetDisponibilita", "GetDisponibilita"])
        self.assertEqual(result["HL-004"]["price_eur"], Decimal("38.00"))

    async def test_temporary_login_failure_is_retried(self):
        login = ET.fromstring('<Response><IdSessione>session</IdSessione></Response>')
        async with httpx.AsyncClient() as client:
            api = OrdiNet(client, "user", "password")
            api._send = AsyncMock(side_effect=[LegacyError("ERRORE_INTERNO"), login, ET.fromstring('<Response/>')])
            result = await api.availability(["EX-001"])
            self.assertEqual(api._send.await_count, 3)
        self.assertEqual(result["EX-001"]["availability"], "unknown")

    async def test_two_temporary_failures_then_success(self):
        login = ET.fromstring('<Response><IdSessione>session</IdSessione></Response>')
        async with httpx.AsyncClient() as client:
            api = OrdiNet(client, "user", "password")
            api._send = AsyncMock(side_effect=[login, LegacyError("TRANSPORT_ERROR"), LegacyError("ERRORE_INTERNO"), ET.fromstring('<Response/>')])
            result = await api.availability(["EX-001"])
            self.assertEqual(api._send.await_count, 4)
        self.assertEqual(result["EX-001"]["availability"], "unknown")

    async def test_partial_live_data_is_isolated_per_product(self):
        def transport(request):
            if b'Login' in request.content:
                payload = '<Response><Esito><Codice>OK</Codice></Esito><IdSessione>s</IdSessione></Response>'
            else:
                payload = '''<Response><Esito><Codice>OK</Codice></Esito><Articoli>
                  <Articolo><CodArt>HL-004</CodArt><StatoDisp>01</StatoDisp><PrezzoVendita>3800</PrezzoVendita><Qta>0012</Qta><Valuta>EUR</Valuta></Articolo>
                  <Articolo><CodArt>KD-024</CodArt><StatoDisp>99</StatoDisp></Articolo>
                  <Articolo><CodArt>EX-001</CodArt><StatoDisp>01</StatoDisp><PrezzoVendita>bad</PrezzoVendita><Valuta>EUR</Valuta></Articolo>
                </Articoli></Response>'''
            return httpx.Response(200, content=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result = await OrdiNet(client, "user", "password").availability(["HL-004", "KD-024", "EX-001", "EX-002"])
        self.assertEqual(result["HL-004"], {"price_eur": Decimal("38.00"), "availability": "available", "stock": 12})
        for code in ("KD-024", "EX-001", "EX-002"):
            self.assertEqual(result[code], {"price_eur": None, "availability": "unknown", "stock": None})

    async def test_timeout_has_bounded_retries(self):
        requests = 0

        def transport(request):
            nonlocal requests
            if b'Login' in request.content:
                return httpx.Response(200, content='<Response><Esito><Codice>OK</Codice></Esito><IdSessione>s</IdSessione></Response>')
            requests += 1
            raise httpx.ReadTimeout("timeout", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            with self.assertRaises(LegacyError):
                await OrdiNet(client, "user", "password").availability(["HL-003"])
        self.assertEqual(requests, 3)
