"""Unified-search regression against independent CSV/BM25/live-data oracles.

No external requests. Exhaustive choice tests and seeded combinations exercise
HTTP serialization, every page and live filtering, not just Catalog methods.
"""

import csv
import math
import random
import re
import unittest
from collections import Counter
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app


CHOICES = ("product_id", "category", "subcategory", "brand", "recipient", "occasion", "tags", "color")
with Path("data/gift-shop-catalog.csv").open(encoding="utf-8-sig", newline="") as source:
    ROWS = list(csv.DictReader(source))


def values(row, field):
    return row[field].split("|") if field in ("tags", "occasion") and row[field] else [row[field]] if row[field] else []


def key(value, field):
    value = value.strip().casefold()
    return value.replace(" and ", " & ") if field == "category" else value


class Inventory:
    def __init__(self):
        self.calls = []
        self.data = {}
        states = ("available", "low_stock", "out_of_stock", "discontinued", "unknown")
        for i, row in enumerate(ROWS):
            state = "unknown" if row["product_id"] in ("KD-024", "EX-001", "EX-002") else states[i % 5]
            self.data[row["product_id"]] = {
                "price_eur": None if state == "unknown" else Decimal(5 + i * 17 % 245) + Decimal(".37"),
                "stock": None if state == "unknown" else 0 if state in ("out_of_stock", "discontinued") else 1 + i % 19,
                "availability": state,
            }

    async def availability(self, codes):
        self.calls.append(list(codes))
        return {code: self.data[code] for code in codes}


def ranking(query):
    """Independent BM25 reference built directly from original CSV columns."""
    if query.strip() in ("", "*"):
        return dict.fromkeys(row["product_id"] for row in ROWS)
    tokenize = lambda text: re.findall(r"[^\W_]+", text.casefold())
    terms = set(tokenize(query))
    documents = []
    frequencies = Counter()
    categories = {}
    for row in ROWS:
        categories.setdefault(key(row["category"], "category"), row["category"].strip())
    for row in ROWS:
        parts = [row["name"]] * 3 + values(row, "tags") * 2 + values(row, "occasion")
        parts += [categories[key(row["category"], "category")]]
        parts += [row[f] for f in ("subcategory", "brand", "description", "material", "color")]
        doc = Counter(tokenize(" ".join(parts)))
        documents.append(doc)
        frequencies.update(doc.keys())
    average = sum(sum(doc.values()) for doc in documents) / len(documents)
    scored = []
    for row, doc in zip(ROWS, documents):
        score = sum(
            math.log(1 + (len(ROWS) - frequencies[t] + .5) / (frequencies[t] + .5))
            * doc[t] * 2.5 / (doc[t] + 1.5 * (.25 + .75 * sum(doc.values()) / average))
            for t in sorted(terms) if doc[t]
        )
        scored.append((row["product_id"], score))
    return dict(sorted(scored, key=lambda pair: pair[1], reverse=True))


class UnifiedSearchTests(unittest.TestCase):
    cases = 0
    requests = 0
    ranks = {}

    def setUp(self):
        self.environment = patch.dict("os.environ", {"TOOL_API_KEY": "test-key", "ORDINET_USER": "test", "ORDINET_PASSWORD": "test"})
        self.environment.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.inventory = Inventory()
        app.state.ordinet = self.inventory
        # This suite retains the independent BM25+filters oracle, not an online inference test.
        app.state.search.rank = AsyncMock(side_effect=app.state.catalog.search)
        self.headers = {"X-API-Key": "test-key"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.environment.stop()

    @classmethod
    def tearDownClass(cls):
        print(f"Unified search oracle: {cls.cases} valid cases, {cls.requests} HTTP pages checked.")

    def expected(self, filters):
        query = filters.get("query", "*").strip()
        if query not in self.ranks:
            self.ranks[query] = ranking(query)
        matched = set()
        for row in ROWS:
            if any(
                field in filters and not set(key(x, field) for x in filters[field]).intersection(key(x, field) for x in values(row, field))
                for field in CHOICES
            ):
                continue
            if any(field in filters and filters[field].strip().casefold() not in row[field].casefold() for field in ("name", "material", "description")):
                continue
            if "gift_wrap" in filters and (row["gift_wrap"] == "yes") != filters["gift_wrap"]:
                continue
            live = self.inventory.data[row["product_id"]]
            if filters.get("availability"):
                if live["availability"] not in filters["availability"]:
                    continue
            elif filters.get("available_only", False) and live["availability"] not in ("available", "low_stock"):
                continue
            valid = True
            for field in ("price_eur", "stock", "rating", "reviews_count", "shipping_days"):
                value = live[field] if field in ("price_eur", "stock") else Decimal(row[field]) if row[field] else None
                lower, upper = filters.get("min_" + field), filters.get("max_" + field)
                if (lower is not None or upper is not None) and (value is None or lower is not None and value < Decimal(str(lower)) or upper is not None and value > Decimal(str(upper))):
                    valid = False
            if valid:
                matched.add(row["product_id"])
        return [code for code in self.ranks[query] if code in matched]

    def verify(self, filters):
        type(self).cases += 1
        expected = self.expected(filters)
        offset, found, seen = 0, [], set()
        k = filters.get("k", 5)
        while offset is not None:
            self.assertNotIn(offset, seen, "Pagination loop")
            seen.add(offset)
            self.inventory.calls.clear()
            body = {key: str(value) if isinstance(value, Decimal) else value for key, value in {**filters, "offset": offset}.items()}
            response = self.client.post("/products", json=body, headers=self.headers)
            type(self).requests += 1
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertLessEqual(len(data["products"]), k)
            self.assertLessEqual(data["checked_candidates"], 20)
            self.assertLessEqual(len(self.inventory.calls), 1)
            self.assertTrue(all(0 < len(call) <= 20 for call in self.inventory.calls))
            for product in data["products"]:
                live = self.inventory.data[product["product_id"]]
                self.assertEqual(product["price_eur"], None if live["price_eur"] is None else str(live["price_eur"]))
                self.assertEqual(product["stock"], live["stock"])
                self.assertEqual(product["availability"], live["availability"])
                score = self.ranks[filters.get("query", "*").strip()][product["product_id"]]
                if score is None:
                    self.assertIsNone(product["relevance_score"])
                else:
                    self.assertAlmostEqual(product["relevance_score"], score, places=12)
                found.append(product["product_id"])
            following = data["next_offset"]
            if following is not None:
                self.assertEqual(following, offset + data["checked_candidates"])
                self.assertGreater(following, offset)
            offset = following
        self.assertEqual(found, expected, filters)
        self.assertEqual(len(found), len(set(found)))
        return found

    def test_every_catalogue_choice_and_or_pair(self):
        for field in CHOICES:
            choices = sorted({v for row in ROWS for v in values(row, field)})
            for i, choice in enumerate(choices):
                with self.subTest(field=field, choice=choice):
                    self.verify({field: [choice.swapcase()], "available_only": False, "k": 10})
                    self.verify({field: [choice, choices[(i + 1) % len(choices)]], "available_only": False, "k": 7})

    def test_seeded_cross_filter_and_query_combinations(self):
        rng = random.Random(20261006)
        queries = ("*", "", "home", "candle", "japanese", "watch travel", "coffee mug", "ceramic scented", "!!!", "qzx404token")
        for i in range(300):
            row, other = rng.sample(ROWS, 2)
            filters = {"query": rng.choice(queries), "k": rng.randint(1, 10)}
            for field in rng.sample(CHOICES, rng.randint(0, 4)):
                alternatives = values(row, field)[:1] + values(other, field)[:1]
                if alternatives:
                    filters[field] = alternatives
            if i % 3 == 0:
                filters.update(min_price_eur=10, max_price_eur=rng.choice((30, 80, 200)), min_stock=1)
            if i % 4 == 0:
                filters.update(min_rating=4, max_shipping_days=5)
            if i % 5 == 0:
                filters["availability"] = ["unknown", "out_of_stock"]
            if i % 7 == 0:
                filters["gift_wrap"] = bool(i % 2)
            with self.subTest(case=i):
                self.verify(filters)

    def test_all_page_sizes_and_browse_forms(self):
        for query in ("", "*", "  *  ", "   ", "home", "candle", "japanese", "qzx404token", "!!!"):
            for k in range(1, 11):
                with self.subTest(query=query, k=k):
                    self.verify({"query": query, "k": k, "available_only": False})
        self.verify({"k": 10})

    def test_all_text_fields_and_wrapping_choices(self):
        for row in ROWS:
            for field in ("name", "material", "description"):
                if row[field]:
                    self.verify({field: row[field][:24].swapcase(), "k": 10})
        for gift_wrap in (True, False):
            self.verify({"gift_wrap": gift_wrap})

    def test_default_filters_are_inactive(self):
        self.assertEqual(self.verify({}), [row["product_id"] for row in ROWS])
        self.verify({"available_only": True})
        body = self.client.get("/openapi.json").json()["components"]["schemas"]["ProductFilters"]
        self.assertFalse(body["properties"]["available_only"]["default"])
        self.assertEqual(body.get("required", []), [])

    def test_tags_use_or_and_fields_use_and(self):
        for filters in (
            {"tags": ["candle", "qzx404token"], "available_only": False},
            {"tags": ["candle", "knife"], "category": ["Home & Living"], "available_only": False},
            {"category": ["Home & Living", "Kitchen & Dining"], "brand": ["Fold Studio", "qzx404token"], "available_only": False},
            {"category": ["  HOME AND LIVING  ", "Home & Living"], "available_only": False},
            {"recipient": ["her", "anyone"], "occasion": ["birthday", "housewarming"], "available_only": False},
        ):
            self.verify(filters)

    def test_live_states_override_default_and_missing_data(self):
        for state in ("available", "low_stock", "out_of_stock", "discontinued", "unknown"):
            self.verify({"availability": [state], "k": 10})
            self.verify({"availability": [state, "unknown"], "min_price_eur": 0, "min_stock": 0})
        self.verify({"product_id": ["KD-024", "EX-001", "EX-002"], "available_only": False})
        self.verify({"product_id": ["HL-008", "EX-001"], "min_rating": 0, "available_only": False})

    def test_live_and_metadata_range_boundaries(self):
        for row in ROWS:
            live = self.inventory.data[row["product_id"]]
            for field in ("price_eur", "stock", "rating", "reviews_count", "shipping_days"):
                value = live[field] if field in ("price_eur", "stock") else Decimal(row[field]) if row[field] else None
                if value is not None:
                    self.verify({"product_id": [row["product_id"]], "available_only": False, "min_" + field: value, "max_" + field: value})

    def test_empty_pages_continue_without_losing_sparse_matches(self):
        selected = {row["product_id"] for row in ROWS[25::7]}
        for code, live in self.inventory.data.items():
            live.update(price_eur=Decimal("1") if code in selected else Decimal("99"), stock=1, availability="available")
        for k in range(1, 11):
            self.verify({"max_price_eur": 5, "k": k})

    def test_invalid_limits_choices_and_ranges(self):
        invalid = [{"k": v} for v in (0, -1, 11, 100, "five", "2.5")]
        invalid += [{"offset": v} for v in (-1, 10001)]
        invalid += [{"query": "x" * 201}, {"category": ["Home & Living", "unknown"]}, {"category": ["Home & Living,Kitchen & Dining"]}]
        for field in CHOICES:
            invalid.extend(({field: [""]}, {field: ["   "]}, {field: ["x"] * 11}))
        invalid += [{"availability": ["available", "invalid"]}, {"brand": ["x" * 81]}, {"product_id": ["x" * 41]}]
        invalid += [{"min_" + f: 2, "max_" + f: 1} for f in ("price_eur", "stock", "rating", "reviews_count", "shipping_days")]
        for params in invalid:
            with self.subTest(params=params):
                self.inventory.calls.clear()
                response = self.client.get("/products", params=params, headers=self.headers)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_request")
                self.assertEqual(self.inventory.calls, [])
        response = self.client.get("/products", params={"brand": ["PRIVATE-OVERSIZED-VALUE" * 10]}, headers=self.headers)
        self.assertNotIn("PRIVATE-OVERSIZED-VALUE", response.text)
        self.assertIn("brand", response.json()["message"])

    def test_offset_exhaustion_unknown_values_and_no_upstream_work(self):
        for params in ({"offset": 10000}, {"brand": "qzx404token"}, {"query": "qzx404token", "category": "Tech & Gadgets", "brand": "qzx404token"}):
            response = self.client.get("/products", params=params, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"search_mode": "semantic" if params.get("query", "*") not in ("", "*") else "browse", "products": [], "next_offset": None, "checked_candidates": 0})
            self.assertEqual(self.inventory.calls, [])

    def test_zero_scores_keep_hard_filters_and_paginate_all_alternatives(self):
        for query in ("qzx404token", "videogioco", "geek nerd technology gaming", "!!!"):
            filters = {"query": query, "category": ["Tech & Gadgets"], "max_price_eur": 100, "available_only": True, "k": 2}
            found = self.verify(filters)
            self.assertTrue(found)
            response = self.client.get("/products", params=filters, headers=self.headers).json()
            self.assertTrue(all(p["relevance_score"] == 0 and p["category"] == "Tech & Gadgets" for p in response["products"]))
        self.assertEqual(self.verify({"query": "qzx404token", "max_price_eur": 0}), [])

    def test_array_schema_limits_and_removed_tool(self):
        schema = self.client.get("/openapi.json").json()
        self.assertEqual(schema["info"]["version"], "0.8.0")
        self.assertNotIn("/products/{product_id}", schema["paths"])
        self.assertEqual(len(schema["paths"]), 4)
        self.assertNotIn("/products/search", schema["paths"])
        self.assertEqual(self.client.get("/products/search", headers=self.headers).status_code, 404)
        self.assertEqual(set(schema["paths"]["/products"]), {"post"})
        self.assertIn("requestBody", schema["paths"]["/products"]["post"])
        params = schema["components"]["schemas"]["ProductFilters"]["properties"]
        for field in (*CHOICES, "availability"):
            self.assertEqual(params[field]["type"], "array")
        self.assertEqual(params["k"]["default"], 5)
        self.assertEqual(params["k"]["maximum"], 10)
        self.assertEqual(params["query"]["default"], "*")
        self.assertEqual(params["mode"]["default"], "semantic")
        self.assertEqual(params["mode"]["enum"], ["semantic", "hybrid", "jev"])
