"""Command line: `pricetracker serve`, `pricetracker flyer lidl.pdf --store Lidl`, `pricetracker mercadona`..."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import db


def _conn(args):
    conn = db.connect(args.db)
    db.init_db(conn)
    return conn


def cmd_init(args):
    _conn(args)
    print(f"Database ready at {args.db}")


def cmd_serve(args):
    import os

    import uvicorn

    from .web import app

    if args.host in ("0.0.0.0", "::"):
        print("Listening on all interfaces: open the app as http://<this computer's IP>:"
              f"{args.port} after adding that IP to PRICETRACKER_ALLOWED_HOSTS.")
    elif args.host not in ("127.0.0.1", "localhost", "::1"):
        current = os.environ.get("PRICETRACKER_ALLOWED_HOSTS", "")
        os.environ["PRICETRACKER_ALLOWED_HOSTS"] = ",".join(filter(None, [current, args.host]))

    app.state.db_path = Path(args.db)
    uvicorn.run(app, host=args.host, port=args.port)


def cmd_flyer(args):
    from . import flyers

    conn = _conn(args)
    files = [Path(f) for f in args.files]
    missing = [f for f in files if not f.exists()]
    if missing:
        sys.exit(f"Not found: {', '.join(map(str, missing))}")
    source_id = flyers.create_flyer_source(
        conn, store=args.store, files=files, title=args.title, observed_on=args.date,
        valid_from=args.valid_from, valid_to=args.valid_to,
    )
    print(f"Reading flyer #{source_id} ({len(files)} file(s))…")
    totals = flyers.process_flyer(conn, source_id, include_non_food=args.all_items, workers=args.workers)
    _report_source(conn, source_id, totals)


def cmd_flyer_url(args):
    import tempfile

    from . import flyers
    from .connectors.download import download

    conn = _conn(args)
    path = download(args.url, Path(tempfile.mkdtemp(prefix="flyer-")))
    source_id = flyers.create_flyer_source(
        conn, store=args.store, files=[path], title=args.title, origin=args.url,
        valid_from=args.valid_from, valid_to=args.valid_to,
    )
    totals = flyers.process_flyer(conn, source_id, workers=args.workers)
    _report_source(conn, source_id, totals)


def _report_source(conn, source_id, totals):
    src = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    n_review = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE source_id = ? AND needs_review", (source_id,)
    ).fetchone()[0]
    print(f"Status: {src['status']} · {totals['items']} items · {n_review} to review · "
          f"{totals['input_tokens']} input / {totals['output_tokens']} output tokens")
    if src["error"]:
        print(f"Errors: {src['error']}")


def cmd_lidl(args):
    from .connectors import lidl

    leaflets = lidl.list_leaflets(args.locale)
    if not leaflets:
        sys.exit("No leaflets found (the service may have changed; download the PDF from lidl.es instead).")
    for i, leaflet in enumerate(leaflets, 1):
        print(f"{i:2d}. {leaflet.title}  [{leaflet.valid_from or '?'} → {leaflet.valid_to or '?'}]\n    {leaflet.pdf_url}")
    if args.pick:
        import tempfile

        from . import flyers
        from .connectors.download import download

        conn = _conn(args)
        for n in args.pick:
            leaflet = leaflets[n - 1]
            path = download(leaflet.pdf_url, Path(tempfile.mkdtemp(prefix="lidl-")))
            source_id = flyers.create_flyer_source(
                conn, store="Lidl", files=[path], title=leaflet.title, origin=leaflet.pdf_url,
                valid_from=leaflet.valid_from, valid_to=leaflet.valid_to,
            )
            print(f"Reading {leaflet.title}…")
            _report_source(conn, source_id, flyers.process_flyer(conn, source_id, workers=args.workers))


def cmd_mercadona(args):
    from .connectors import mercadona

    counts = mercadona.fetch(_conn(args), args.warehouse or mercadona.DEFAULT_WAREHOUSE)
    print(f"Mercadona: {counts['products']} food products, {counts['matched']} matched to tracked products")


def cmd_import_csv(args):
    from .importers import import_csv

    counts = import_csv(_conn(args), Path(args.file).read_text(encoding="utf-8-sig"), default_store=args.store,
                        origin=Path(args.file).name)
    print(f"Imported {counts['rows']} rows ({counts['skipped']} skipped) into {counts['sources']} source(s)")
    for error in counts["errors"]:
        print("  " + error)


def cmd_best(args):
    from . import queries

    rows = queries.best_prices(_conn(args), days=args.days, category=args.category, search=args.search)
    if not rows:
        print("No comparable prices yet.")
        return
    for p in rows:
        b = p["best"]
        others = ", ".join(f"{s['store_name']} {s['unit_price']:.2f}" for s in p["stores"][1:])
        print(f"{p['name_es']:<34} {b['unit_price']:>7.2f} €/{p['base_unit']:<2}  {b['store_name']:<12} {others}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pricetracker", description="Compare Spanish supermarket food prices.")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH), help="SQLite file (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    def flyer_options(p):
        p.add_argument("--store", required=True, help="store name, e.g. Lidl, Aldi, Bonpreu")
        p.add_argument("--title")
        p.add_argument("--valid-from")
        p.add_argument("--valid-to")
        p.add_argument("--workers", type=int, default=3, help="pages read in parallel")

    p = sub.add_parser("flyer", help="read a flyer PDF or page images with Claude")
    p.add_argument("files", nargs="+")
    flyer_options(p)
    p.add_argument("--date", help="date the prices were published (default: valid-from or today)")
    p.add_argument("--all-items", action="store_true", help="keep non-food items too")
    p.set_defaults(func=cmd_flyer)

    p = sub.add_parser("flyer-url", help="download a flyer PDF from a link and read it")
    p.add_argument("url")
    flyer_options(p)
    p.set_defaults(func=cmd_flyer_url)

    p = sub.add_parser("lidl", help="list Lidl leaflets; --pick N imports leaflet N")
    p.add_argument("--pick", type=int, nargs="*")
    p.add_argument("--locale", default="lidl/es-ES")
    p.add_argument("--workers", type=int, default=3)
    p.set_defaults(func=cmd_lidl)

    p = sub.add_parser("mercadona", help="snapshot Mercadona online shop food prices")
    p.add_argument("--warehouse", default=None, help="warehouse code for your area, e.g. bcn1, mad1, vlc1")
    p.set_defaults(func=cmd_mercadona)

    p = sub.add_parser("import-csv", help="import prices from a CSV file")
    p.add_argument("file")
    p.add_argument("--store", help="store for rows without one")
    p.set_defaults(func=cmd_import_csv)

    p = sub.add_parser("best", help="print the best price per product")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--category")
    p.add_argument("--search")
    p.set_defaults(func=cmd_best)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
