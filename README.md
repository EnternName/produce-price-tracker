# produce-price-tracker

Track and compare **food prices across Spanish supermarkets** (Mercadona, Lidl, Aldi, Carrefour, Dia,
Bonpreu/Esclat, Consum, Condis…) over time. Every offer is normalized to a **unit of reference —
€/kg, €/L or €/unit** — so a 6 × 1 L pack of milk in a Catalan Lidl flyer and a 1 L brick in the
Mercadona shop land on the same scale.

- Reads **flyers ("folletos")** in Spanish or Catalan, from a PDF or from screenshots taken in a flyer
  app, using Claude's vision API.
- Pulls the **Mercadona online shop** and **Lidl's weekly leaflets** directly.
- Accepts **CSV files** and **single prices typed in by hand**.
- Links every offer to a generic product (≈170 seeded, with Spanish *and* Catalan names), so
  "Pit de pollastre" and "Pechuga de pollo" compare against each other.
- Web app: best price per product across stores, price history per store, and a review queue for
  anything the importer was unsure about.

## Where the data comes from

None of the chains publishes an official price API. This is what exists and what the app does with
it. The connectors were written against the endpoints as community projects document them, but this
repo was built in a sandbox that could not reach the supermarket sites, so **they have not been run
against the live services yet**. Expect to adjust them the first time you run them.

| Store | Best route | In the app |
|---|---|---|
| **Mercadona** | Mercadona has no weekly flyer. Its online shop (tienda.mercadona.es) runs on a JSON API that lists every product with its price and its €/kg or €/L reference price. Prices vary by warehouse (region). | `pricetracker mercadona --warehouse bcn1`, or the button on the Flyers page |
| **Lidl** | Weekly PDF leaflets, listed by the Schwarz group leaflet service (`endpoints.leaflets.schwarz`) that powers lidl.es's viewer. | `pricetracker lidl --pick 1`, or "Find Lidl leaflets" |
| **Aldi, Carrefour, Dia, Alcampo, Eroski, Consum, Bonpreu/Esclat, Condis, Caprabo…** | Their folletos are published on their websites, usually as PDFs (look for "Descargar"). Several also have online shops; scrapers exist for some of them (Dia, Eroski, Alcampo) but none is built into the app yet. | Upload the PDF, or paste its link |
| **Flyer aggregator apps** (Tiendeo and similar "folletos de alimentación" apps) | No public API or export found. | Take screenshots of the pages, then upload them. Long scrolling screenshots are sliced automatically. |
| **Anything else** | Shelf prices you note down, a spreadsheet you keep. | "Add prices" page: one price, or a CSV |

Spanish law (Real Decreto 3423/2000) requires the reference price (*precio por unidad de medida*) to be
shown. Flyers print it in small type. The app reads it and checks it against its own calculation;
rows that disagree go to the review queue.

Use these sources for personal comparison, at a polite pace (the connectors sleep between requests),
and check each site's terms.

## Quick start

```bash
uv sync                                   # Python 3.11+ ; or: pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...       # needed only for reading flyers
uv run pricetracker serve                 # http://127.0.0.1:8000
```

Data lives in `data/prices.db` (SQLite; set `PRICETRACKER_DB` to move it). Uploaded flyers are kept
under `data/flyers/`.

### Command line

```bash
uv run pricetracker flyer folleto-lidl.pdf --store Lidl          # PDF
uv run pricetracker flyer IMG_0412.PNG IMG_0413.PNG --store Aldi  # screenshots, one page each
uv run pricetracker flyer-url https://…/folleto.pdf --store Dia
uv run pricetracker lidl                  # list current Lidl leaflets
uv run pricetracker lidl --pick 1         # …and import the first one
uv run pricetracker mercadona --warehouse bcn1
uv run pricetracker import-csv my-prices.csv
uv run pricetracker best --search leche   # cheapest store per product, in the terminal
```

A weekly routine could be: import the Lidl leaflet and a Mercadona snapshot (both are a single command,
easy to put in `cron`), then upload the PDFs or screenshots for the other stores you use.

## How prices are compared

1. **Quantity** – sizes like `6 x 1 L`, `pack 6 llaunes 33 cl`, `Malla 2 kg`, `bandeja 400 g aprox.`,
   `1 docena`, `12 ous`, `peso escurrido 240 g` (canned goods compare on drained weight), and per-kilo
   pricing (`1,99 €/kg`, `el kilo`, `a granel`) are parsed into kg, L or units (`units.py`).
2. **Unit price** – price ÷ quantity, in the product's comparison unit. Eggs, lettuce and coffee
   capsules compare per unit; everything else per kg or L.
3. **Promotions** – `3x2`, `2x1`, `2ª unidad -50%`, `2a unitat al 70%`, `3ª unidad gratis` produce an
   *effective* unit price for when you buy the promo quantity. This is shown next to the normal price,
   not instead of it. A plain `-30%` is informational, because the printed price already includes it.
   Prices that need a loyalty card or app (Lidl Plus, Club Dia…) are stored separately and badged.
4. **Matching** – the generic Spanish name Claude adds for each item ("pechuga de pollo") is matched
   first, then the printed name. A synonym matches only if its first word is the item's first word,
   because Spanish and Catalan put the noun first. This keeps "zumo de naranja" from matching
   "naranja". When you assign a product on the review screen, the name is remembered for next time.

Claude only transcribes what is printed (names, prices, sizes, labels, validity dates) and adds the
generic name. All arithmetic happens in Python, so it is deterministic and testable.

## Claude usage and cost

Each flyer page is one request to `claude-opus-5-5`, with adaptive thinking and structured JSON output.
Server-side refusal fallback is enabled (`fallbacks: "default"`), so if a page is declined the API
retries it on another model. PDF pages are sent as images, together with the PDF's own text layer
to help read small print. Token usage is stored on each flyer and shown on its page, so you can see
the real cost per flyer. To trade accuracy for cost:

```bash
export PRICETRACKER_MODEL=claude-sonnet-5-5   # default: claude-opus-5-5
export PRICETRACKER_EFFORT=medium             # default: high
```

## Project layout

```
src/pricetracker/
  units.py        quantity, price and promo parsing (es/ca)
  catalog.py      seed products (es/ca names, synonyms, comparison unit) and stores
  matching.py     offer name -> product
  pricing.py      offer -> normalized observation (+ review flags)
  extract.py      Claude vision extraction (prompt + JSON schema)
  flyers.py       PDF/image loading, slicing, page-by-page ingestion
  connectors/     mercadona.py, lidl.py, download.py
  importers.py    CSV and manual entry
  queries.py      best price per store, history, review queue
  web.py          FastAPI app; templates/ and static/ (Chart.js 4.4.1 vendored, MIT)
  cli.py          command line
tests/            parser, matcher, pricing, flyer pipeline (with a fake extractor), connectors, web
```

Run the tests with `uv run pytest`. They don't call the network or the Claude API.

## Next steps

- Verify the Mercadona and Lidl connectors against the live sites; add online-shop connectors for
  Dia, Carrefour, Consum and Bonpreu/Esclat.
- Shopping-basket view: total cost of a list at each store.
- Non-food categories (the extractor can already keep them with `--all-items`).
