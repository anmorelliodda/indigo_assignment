"""Read the original CSV once. Never use its prices or stock."""

import csv
import math
import re
from collections import Counter
from pathlib import Path

from models import CatalogOptionsRequest, ProductFilters, in_range


def category_key(value: str) -> str:
    """Normalize category case, whitespace and the documented and/& alias."""
    return value.strip().casefold().replace(" and ", " & ")


def metadata_text(product: dict) -> str:
    """Shared retrieval evidence. Never include CSV prices, stock or customer data."""
    parts = [product["name"]] * 3 + product["tags"] * 2 + (product["occasion"] or [])
    parts += [product[field] or "" for field in (
        "category", "subcategory", "brand", "description", "material", "color"
    )]
    return " ".join(parts)


class Catalog:
    def __init__(self, path: Path):
        """Load CSV metadata once and build the weighted BM25 index, excluding prices/stock."""
        with path.open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        self.products = {row["product_id"]: row for row in rows}
        self.categories = {}
        for row in rows:
            self.categories.setdefault(category_key(row["category"]), row["category"].strip())
        # Index metadata only. Prices and stock must never enter retrieval.
        self.terms = {}
        self.document_frequency = Counter()
        for product_id in self.products:
            product = self.details(product_id)
            terms = Counter(self.tokenize(metadata_text(product)))
            self.terms[product_id] = terms
            self.document_frequency.update(terms.keys())
        self.average_length = sum(sum(terms.values()) for terms in self.terms.values()) / max(len(self.terms), 1)

    def options(self, filters: CatalogOptionsRequest) -> dict:
        """Count distinct metadata labels; never consult CSV prices or stock."""
        counts = Counter()
        category = category_key(filters.category) if filters.category else None
        query = filters.query.casefold()
        for row in self.products.values():
            key = category_key(row["category"])
            if category and key != category:
                continue
            value = self.categories[key] if filters.field == "category" else row[filters.field]
            labels = value.split("|") if filters.field in ("tags", "occasion") else [value]
            counts.update({label.strip() for label in labels if label.strip()})
        labels = sorted(
            (value for value in counts if query in value.casefold()),
            key=lambda value: (value.casefold(), value),
        )
        selected = labels[filters.offset:filters.offset + filters.limit]
        next_offset = filters.offset + len(selected)
        return {
            "field": filters.field,
            "values": [{"value": value, "product_count": counts[value]} for value in selected],
            "next_offset": next_offset if next_offset < len(labels) else None,
        }

    @staticmethod
    def tokenize(value: str) -> list[str]:
        """Extract case-folded word/number tokens without stemming or translating them."""
        return re.findall(r"[^\W_]+", value.casefold())

    def search(self, filters: ProductFilters) -> dict[str, float | None]:
        """Ordered candidates and scores; filters exclude, query only ranks."""
        candidates = [product_id for product_id in self.products if self.matches(self.details(product_id), filters)]
        if filters.query in ("", "*"):
            return dict.fromkeys(candidates)
        query = sorted(set(self.tokenize(filters.query)))
        count = len(self.products)
        scores = {}
        for product_id in candidates:
            terms = self.terms[product_id]
            length = sum(terms.values())
            score = 0.0
            for word in query:
                frequency = terms[word]
                if frequency:
                    idf = math.log(1 + (count - self.document_frequency[word] + 0.5) / (self.document_frequency[word] + 0.5))
                    score += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / self.average_length))
            scores[product_id] = score
        # Positive matches first, then zero-score alternatives. Stable ties preserve CSV order.
        return dict(sorted(scores.items(), key=lambda item: item[1], reverse=True))

    def details(self, product_id: str) -> dict:
        """Convert one CSV row into typed metadata; preserve missing values and exclude live fields."""
        row = self.products[product_id]
        return {
            "product_id": product_id,
            "name": row["name"],
            "category": self.categories[category_key(row["category"])],
            "subcategory": row["subcategory"],
            "brand": row["brand"],
            "description": row["description"],
            "material": row["material"] or None,
            "color": row["color"] or None,
            "tags": row["tags"].split("|") if row["tags"] else [],
            "gift_wrap": row["gift_wrap"] == "yes",
            "rating": float(row["rating"]) if row["rating"] else None,
            "reviews_count": int(row["reviews_count"]) if row["reviews_count"] else None,
            "recipient": row["recipient"] or None,
            "occasion": row["occasion"].split("|") if row["occasion"] else None,
            "shipping_days": int(row["shipping_days"]) if row["shipping_days"] else None,
        }

    @staticmethod
    def matches(product: dict, filters: ProductFilters) -> bool:
        """Apply mandatory metadata constraints: OR within choices, AND between fields."""
        if filters.category and category_key(product["category"]) not in {category_key(value) for value in filters.category}:
            return False

        for field in ("product_id", "subcategory", "brand", "recipient", "color"):
            expected = getattr(filters, field)
            actual = product[field] or ""
            if expected and actual.casefold() not in {value.casefold() for value in expected}:
                return False

        for field in ("name", "material", "description"):
            expected = getattr(filters, field)
            actual = product[field] or ""
            if expected is not None and expected.casefold() not in actual.casefold():
                return False

        for field in ("occasion", "tags"):
            expected = getattr(filters, field)
            actual = {value.casefold() for value in product[field] or []}
            if expected and not any(value.casefold() in actual for value in expected):
                return False
        if filters.gift_wrap is not None and product["gift_wrap"] != filters.gift_wrap:
            return False

        for field in ("rating", "reviews_count", "shipping_days"):
            if not in_range(product[field], getattr(filters, f"min_{field}"), getattr(filters, f"max_{field}")):
                return False

        return True
