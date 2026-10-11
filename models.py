"""Small, typed tool inputs and results."""

from decimal import Decimal
from typing import Annotated, Literal, Required, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

Availability = Literal["available", "low_stock", "out_of_stock", "discontinued", "unknown"]
ShortChoice = Annotated[str, Field(min_length=1, max_length=40)]
LabelChoice = Annotated[str, Field(min_length=1, max_length=80)]


class Error(BaseModel):
    code: Literal["invalid_request", "unauthorized", "not_found", "order_not_verified", "temporarily_unavailable"]
    message: str
    retryable: bool


CatalogField = Literal["category", "subcategory", "brand", "color", "material", "occasion", "tags", "recipient"]


class CatalogOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: CatalogField = Field(default="category", description="One catalogue field to explore. Default category. Returns stored labels, not products or live availability.")
    category: str | None = Field(default=None, min_length=1, max_length=80, description="Optional exact category to restrict the catalogue subset. Use a category returned by this tool; case and known aliases are ignored.")
    query: str = Field(default="", max_length=100, description="Optional case-insensitive substring of an option label. Not BM25 or product search. Use English catalogue terms; no translation or wildcard syntax.")
    limit: int = Field(default=10, ge=1, le=20, description="Maximum labels per page: default 10, maximum 20.")
    offset: int = Field(default=0, ge=0, le=10000, description="Start at zero; then use returned next_offset with unchanged field, category, query and limit.")


class CatalogOption(BaseModel):
    value: str
    product_count: int = Field(ge=1, description="Catalogue products containing this label in the selected category, regardless of live availability. Not sales or stock. A product counts once per label.")


class CatalogOptions(BaseModel):
    field: CatalogField
    values: list[CatalogOption] = Field(max_length=20)
    next_offset: int | None = Field(description="Next label offset with the same arguments. Null means all matching labels have been returned.")


ProductField = Literal[
    "product_id", "name", "category", "subcategory", "brand", "description", "material", "color", "tags",
    "gift_wrap", "rating", "reviews_count", "recipient", "occasion", "shipping_days", "price_eur",
    "availability", "stock", "relevance_score",
]


class SearchProduct(TypedDict, total=False):
    """Typed selectable output: omitted fields are absent, requested unknown values remain null."""

    product_id: Required[str]
    name: str
    category: str
    subcategory: str
    brand: str
    description: str
    material: str | None
    color: str | None
    tags: list[str]
    gift_wrap: bool
    rating: Annotated[float | None, Field(description="Average rating from the catalogue. Null means no rating is provided.")]
    reviews_count: Annotated[int | None, Field(ge=0, description="Number of reviews from the catalogue. Review texts are not provided. Null means unknown.")]
    recipient: Annotated[str | None, Field(description="Catalogue recipient suggestion, not a restriction on who can use the product.")]
    occasion: Annotated[list[str] | None, Field(description="Suggested gift occasions from the catalogue. Null means no occasions are provided.")]
    shipping_days: Annotated[int | None, Field(ge=0, description="Indicative shipping time in days from the catalogue. Not live data, an arrival date or a delivery guarantee. Null means unknown.")]
    price_eur: Annotated[Decimal | None, Field(description="Live unit price in euros as a decimal string. Null means unverified. Never replace null with the CSV price.")]
    availability: Annotated[Availability, Field(description="Live availability. Unknown means live data could not be verified for this product. Do not recommend it as available.")]
    stock: Annotated[int | None, Field(ge=0, description="Live quantity from the order system. Null means unknown. Never use CSV stock as a fallback.")]
    relevance_score: Required[
        Annotated[
            float | None,
            Field(
                ge=-1,
                description="Always returned. semantic: cosine similarity (-1 to 1), not probability. hybrid: RRF score, not probability. jev: independent candidate-match Noul probability (0 to 1), not stock/price verification. Null means browse or ID detail without a query. Scores are not comparable across modes or queries. Even a high score does not prove all mandatory features: inspect the product and live fields.",
            ),
        ]
    ]


class SearchResults(BaseModel):
    search_mode: Literal["browse", "semantic", "hybrid", "jev"] = Field(
        description="Actual ranking mode. browse means no query inference was performed."
    )
    products: list[SearchProduct] = Field(max_length=10)
    next_offset: int | None = Field(
        description="Pass this offset with the SAME mode, query, filters and k to check the next candidate page. Null means no more candidates."
    )
    checked_candidates: int


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    order_number: str = Field(pattern=r"^GS-\d{4}-\d{6}$", description="Customer-facing order number, such as GS-2026-000101.")
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$", description="Order email supplied by the customer in this conversation. If missing, ask before calling. Never use examples, placeholders or guessed emails.")


class OrderStatus(BaseModel):
    order_number: str
    status: Literal["received", "preparing", "shipped", "out_for_delivery", "delivered", "cancelled", "return_in_progress", "unknown"] = Field(description="Current order status. Unknown means the live status could not be interpreted. Do not infer its meaning from dates or tracking.")
    expected_delivery_date: str | None = Field(description="Estimated date in YYYY-MM-DD. Not a delivery guarantee.")
    delivery_date: str | None
    carrier: str | None
    tracking_number: str | None


def in_range(value, minimum, maximum) -> bool:
    """Check inclusive bounds; missing values fail only when a bound was supplied."""
    if minimum is None and maximum is None:
        return True
    if value is None:
        return False
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


class ProductFilters(BaseModel):
    """OR within each choice list, AND between supplied filters."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    fields: list[ProductField] = Field(default_factory=list, max_length=19, description="Output fields only, not product filters. Omit or send [] for all fields. Otherwise return only requested fields plus product_id and relevance_score, which are always included. Send a JSON array of schema field names. Omitted output keys do not mean unknown; explicitly requested unknown values remain null. For recommendations request name, description, price_eur, availability and any required feature fields. For one product use product_id with no query or unwanted filters; get_product_details no longer exists.")
    mode: Literal["semantic", "hybrid", "jev"] = Field(
        default="semantic",
        description="Ranking strategy within this same search tool. semantic (default) is multilingual vector/cosine search; semantic and vector are the same method. hybrid combines lexical BM25 with vectors via RRF, not necessarily better. jev uses a TypeSafe decision model: typed per-candidate match probabilities, no generated answer or reasoning text. Prefer semantic for natural requests; optionally choose hybrid for explicit lexical requirements or jev for a difficult semantic comparison. Do not call all modes routinely. Keep mode unchanged when paging; changed mode starts at offset=0. Browse/ID detail without query does not invoke any model.",
    )
    query: str = Field(
        default="*",
        max_length=200,
        description="Standalone product request, object, purpose and preferences in the customer's language. semantic and jev do not require English translation. Hybrid lexical matching uses English catalogue words; semantic matching remains multilingual. Do not include email, order numbers or unrelated conversation data. Omit, leave blank or use * to browse or read exact IDs without inference. Query only ranks: it does not apply hard exclusions, budget or stock rules. Use explicit filters and verify mandatory features. No invented AND/OR/wildcard syntax.",
    )
    product_id: list[ShortChoice] = Field(default_factory=list, max_length=10, description="One or up to ten exact product IDs, case-insensitive. Any supplied ID may match (OR). Send alternatives as a JSON array.")
    name: str | None = Field(default=None, max_length=100, description="Case-insensitive text contained in the product name.")
    category: list[LabelChoice] = Field(default_factory=list, max_length=10, description="One or up to ten exact category names (OR). Case and known aliases are ignored. Use names returned by get_catalog_options with field=category; every supplied category must be valid. Send a JSON array, not comma-separated text.")
    subcategory: list[LabelChoice] = Field(default_factory=list, max_length=10, description="One or up to ten exact subcategories, case-insensitive (OR). Send a JSON array.")
    brand: list[LabelChoice] = Field(default_factory=list, max_length=10, description="One or up to ten exact brands, case-insensitive (OR). Send a JSON array.")
    recipient: list[ShortChoice] = Field(default_factory=list, max_length=10, description="One or up to ten exact recipient labels (OR): anyone, her, him, couple or kids. Use only labels explicitly requested. Do not infer this filter from gender or age; it would exclude unisex gifts.")
    occasion: list[ShortChoice] = Field(default_factory=list, max_length=10, description="Explicit occasions (OR): birthday, christmas, thank-you, graduation, anniversary or housewarming. Up to ten values. Leave unset if only a relative, gender or age is given; never infer birthday.")
    tags: list[LabelChoice] = Field(default_factory=list, max_length=10, description="One or up to ten exact tags, case-insensitive (OR, not all-tags matching). Send a JSON array. Verify all mandatory customer features before recommending.")
    color: list[LabelChoice] = Field(default_factory=list, max_length=10, description="One or up to ten exact colours, case-insensitive (OR). Send a JSON array.")
    material: str | None = Field(default=None, max_length=100, description="Case-insensitive text contained in the material.")
    description: str | None = Field(default=None, max_length=200, description="Case-insensitive text contained in the description.")
    gift_wrap: bool | None = Field(default=None, description="True requires gift wrapping. False requires no gift wrapping.")

    min_price_eur: Decimal | None = Field(default=None, ge=0, le=10000, description="Inclusive minimum LIVE price in euros.")
    max_price_eur: Decimal | None = Field(default=None, ge=0, le=10000, description="Inclusive maximum LIVE price in euros.")
    min_stock: int | None = Field(default=None, ge=0, description="Inclusive minimum LIVE quantity. Unknown quantity does not match.")
    max_stock: int | None = Field(default=None, ge=0, description="Inclusive maximum LIVE quantity. Unknown quantity does not match.")
    min_rating: float | None = Field(default=None, ge=0, le=5, description="Inclusive minimum catalogue rating. Missing ratings do not match.")
    max_rating: float | None = Field(default=None, ge=0, le=5, description="Inclusive maximum catalogue rating. Missing ratings do not match.")
    min_reviews_count: int | None = Field(default=None, ge=0, description="Inclusive minimum number of catalogue reviews. Missing counts do not match.")
    max_reviews_count: int | None = Field(default=None, ge=0, description="Inclusive maximum number of catalogue reviews. Missing counts do not match.")
    min_shipping_days: int | None = Field(default=None, ge=0, description="Inclusive minimum catalogue shipping estimate. Not a delivery guarantee.")
    max_shipping_days: int | None = Field(default=None, ge=0, description="Inclusive maximum catalogue shipping estimate. Missing estimates do not match. Not a delivery guarantee.")

    availability: list[Availability] = Field(default_factory=list, max_length=5, description="One or more exact LIVE states (OR), including unknown. Send a JSON array. A nonempty list overrides available_only; unknown prices/quantities stay null.")
    available_only: bool = Field(default=False, description="No availability filter by default. Set true to return only available or low-stock products when seeking purchasable gifts. A nonempty availability list overrides this flag. Never recommend unknown or unavailable products as purchasable.")
    k: int = Field(default=5, ge=1, le=10, description="Maximum products returned per page: 1 to 10, default 5. This is a page size, not a global cheapest/best ranking. Use next_offset to continue; keep query, filters and k unchanged.")
    offset: int = Field(
        default=0,
        ge=0,
        le=10000,
        description="Candidate offset. Use only returned next_offset with the same mode, query, filters and k. An empty page can still have a next_offset.",
    )

    @model_validator(mode="after")
    def validate_ranges(self):
        """Reject contradictory minimum/maximum pairs before any SOAP request."""
        for field in ("price_eur", "stock", "rating", "reviews_count", "shipping_days"):
            minimum = getattr(self, f"min_{field}")
            maximum = getattr(self, f"max_{field}")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError(f"min_{field} must not exceed max_{field}.")
        return self

    def matches_live(self, product: dict) -> bool:
        """Enforce live state/price/quantity constraints; explicit states override available_only."""
        if self.availability:
            if product["availability"] not in self.availability:
                return False
        elif self.available_only and product["availability"] not in {"available", "low_stock"}:
            return False
        return (
            in_range(product["price_eur"], self.min_price_eur, self.max_price_eur)
            and in_range(product.get("stock"), self.min_stock, self.max_stock)
        )
