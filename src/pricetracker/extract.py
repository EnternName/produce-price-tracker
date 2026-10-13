"""Read offers off a flyer page image with Claude (vision + structured JSON output).

Claude only transcribes what is printed (names, prices, sizes, promo labels) and adds a generic
Spanish product name, which also translates Catalan flyers. All arithmetic (EUR/kg, promos) is done
afterwards in Python (``pricing.py``) so it is deterministic and can be cross-checked against the
unit price the store prints.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Protocol

from .catalog import CATEGORIES

DEFAULT_MODEL = os.environ.get("PRICETRACKER_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = os.environ.get("PRICETRACKER_EFFORT", "high")
FALLBACK_BETA = "server-side-fallback-2026-07-01"



def _nullable(kind: str, description: str = "") -> dict:
    schema: dict = {"anyOf": [{"type": kind}, {"type": "null"}]}
    if description:
        schema["description"] = description
    return schema


ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name_printed": {"type": "string", "description": "Product name exactly as printed, in its original language."},
        "generic_name_es": {
            "type": "string",
            "description": "Short generic Spanish name of the product type, no brand/size/marketing words "
            "(e.g. 'leche semidesnatada', 'pechuga de pollo', 'tomate cherry'). Translate Catalan to Spanish.",
        },
        "brand": _nullable("string"),
        "is_food": {"type": "boolean", "description": "Food or drink for people."},
        "category": {"type": "string", "enum": [*CATEGORIES, "Otros"]},
        "quantity_text": _nullable("string", "Size/format as printed, e.g. '6 x 1 L', 'Malla 2 kg', 'Bandeja 500 g aprox.'"),
        "pack_count": _nullable("integer", "Items in the pack (6 in '6 x 1 L'); 1 if single."),
        "unit_amount": _nullable("number", "Size of ONE item in `unit` (1 in '6 x 1 L', 500 in '500 g')."),
        "unit": {"anyOf": [{"type": "string", "enum": ["g", "kg", "ml", "cl", "l", "ud"]}, {"type": "null"}]},
        "approx_weight": {"type": "boolean", "description": "Weight is approximate (aprox., variable weight)."},
        "price_basis": {
            "type": "string",
            "enum": ["item", "per_kg", "per_l", "per_unit"],
            "description": "'item' if `price` buys the item/pack shown; 'per_kg'/'per_l'/'per_unit' if the big price is itself per kg/L/unit (e.g. fruit '1,99 €/kg').",
        },
        "price": {"type": "number", "description": "The price anyone pays (the main, large price), in EUR."},
        "regular_price": _nullable("number", "Crossed-out / previous / 'antes' price, if shown."),
        "loyalty_price": _nullable("number", "Lower price that needs a card or app (Lidl Plus, Club Dia, ...), if shown separately."),
        "printed_unit_price": _nullable("number", "Small-print reference price, e.g. 2.90 from '2,90 €/kg'."),
        "printed_unit": _nullable("string", "Unit of printed_unit_price: 'kg', 'l', 'ud', '100 g'..."),
        "promo_text": _nullable("string", "Promo label verbatim: '3x2', '2ª unidad -50%', '-30%', 'Lidl Plus'..."),
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": [
        "name_printed", "generic_name_es", "brand", "is_food", "category", "quantity_text", "pack_count",
        "unit_amount", "unit", "approx_weight", "price_basis", "price", "regular_price", "loyalty_price",
        "printed_unit_price", "printed_unit", "promo_text", "confidence",
    ],
    "additionalProperties": False,
}

PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "enum": ["es", "ca", "other"]},
        "valid_from": _nullable("string", "Offer start date printed on the page, YYYY-MM-DD."),
        "valid_to": _nullable("string", "Offer end date printed on the page, YYYY-MM-DD."),
        "items": {"type": "array", "items": ITEM_SCHEMA},
    },
    "required": ["language", "valid_from", "valid_to", "items"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You transcribe Spanish supermarket flyers (folletos), written in Spanish or Catalan, into structured data for a price-comparison app.

For every distinct product offer that shows a price, output one item. Rules:
- Transcribe only what is printed. Use null for anything not shown; never guess prices or sizes.
- Prices are in euros; Spanish flyers use a decimal comma ("1,99" -> 1.99). Prices like "1'99" or a large "1" with a small "99" also mean 1.99.
- `price` is what anyone pays for the item or pack pictured. If the main price is itself per kilo / litre / unit (typical for fruit, vegetables, meat and fish counters, e.g. "1,99 €/kg"), set price_basis accordingly.
- If a lower price needs a loyalty card or app (Lidl Plus, Club Dia, Mi Alcampo, Club Carrefour...), put it in loyalty_price and keep the normal price in `price`. If only the card price is shown, use it as `price` and mention the card in promo_text.
- Copy multi-buy and discount labels verbatim into promo_text ("3x2", "2ª unidad -50%", "2a unitat al 70%", "-30%").
- Spanish law requires a reference price per kg or litre in small print ("2,90 €/kg", "Precio por kg"); copy it into printed_unit_price/printed_unit when visible.
- generic_name_es is a short generic Spanish name of what the product IS, without brand or size, so offers from different stores can be compared. Translate Catalan ("pit de pollastre" -> "pechuga de pollo", "llet sencera" -> "leche entera").
- If the page states offer validity dates ("Del 13 al 19 de octubre", "Ofertes vàlides del..."), return them as ISO dates, using the year given below when none is printed.
- When several overlapping slices of one page are given, they are one page: list each offer once.
- {food_rule}
- If the page has no priced products (cover, index, terms), return an empty items list."""

FOOD_ONLY = "Only list food and drink for people. Skip household, cleaning, cosmetics, pet, clothing, electronics, garden and other non-food items."
ALL_ITEMS = "List every priced product; set is_food to false for non-food items."


class ExtractionError(RuntimeError):
    pass


@dataclass
class PageResult:
    data: dict
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


@dataclass
class PageInput:
    number: int
    images: list[bytes]  # JPEG bytes; several when a tall screenshot was sliced
    text_layer: str = ""
    media_type: str = "image/jpeg"
    extra: dict = field(default_factory=dict)


class Extractor(Protocol):
    def extract(self, page: PageInput, *, store_name: str, total_pages: int, today: str) -> PageResult: ...


class ClaudeExtractor:
    """Extract offers with the Anthropic API. Credentials come from the environment
    (ANTHROPIC_API_KEY, or an `ant auth login` profile)."""

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT, include_non_food: bool = False, client=None):
        import anthropic

        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.system = SYSTEM_PROMPT.replace("{food_rule}", ALL_ITEMS if include_non_food else FOOD_ONLY)

    def extract(self, page: PageInput, *, store_name: str, total_pages: int, today: str) -> PageResult:
        import anthropic

        content: list[dict] = []
        for image in page.images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": page.media_type, "data": base64.standard_b64encode(image).decode()},
            })
        context = f"Store: {store_name}. Page {page.number} of {total_pages}. Today is {today}."
        if len(page.images) > 1:
            context += f" The {len(page.images)} images are overlapping top-to-bottom slices of this one page."
        if page.text_layer.strip():
            context += (
                "\n\nThe PDF's embedded text for this page follows (may be incomplete or out of order; "
                "use it to read small print accurately, but trust the image for layout):\n"
                + page.text_layer[:8000]
            )
        content.append({"type": "text", "text": context})

        try:
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                system=self.system,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": PAGE_SCHEMA}},
                betas=[FALLBACK_BETA],
                fallbacks="default",
                messages=[{"role": "user", "content": content}],
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise ExtractionError("Anthropic API key missing or invalid (set ANTHROPIC_API_KEY)") from e
        except anthropic.RateLimitError as e:
            raise ExtractionError("Anthropic API rate limit hit; try again later") from e
        except anthropic.APIStatusError as e:
            raise ExtractionError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ExtractionError("Could not reach the Anthropic API") from e

        if message.stop_reason == "refusal":
            raise ExtractionError("The model declined to read this page")
        if message.stop_reason == "max_tokens":
            raise ExtractionError("Page output was cut off (max_tokens); try splitting the page")
        text = next((b.text for b in message.content if b.type == "text"), None)
        if text is None:
            raise ExtractionError("No output for this page")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ExtractionError(f"Could not parse model output: {e}") from e
        return PageResult(data, message.usage.input_tokens, message.usage.output_tokens, message.model)
