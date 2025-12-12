"""
Build a URL tree from PostgreSQL and dump it as JSON.

Usage examples:
    python script/url_to_tree/index.py --dsn postgres://user:pass@host:5432/db
    python script/url_to_tree/index.py --host 127.0.0.1 --dbname mydb --user me

Environment defaults (loaded from .env if present):
- PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSCHEMA
- URL_TABLE, URL_COLUMN
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Iterable, Iterator
from urllib.parse import urlparse

from pathlib import Path

from dotenv import load_dotenv

# Load .env from the same directory as this script
_script_dir = Path(__file__).resolve().parent
load_dotenv(_script_dir / ".env")

try:
    import psycopg
    from psycopg import sql

    _connect = psycopg.connect
except ImportError:
    psycopg = None
    try:
        import psycopg2
        from psycopg2 import sql

        _connect = psycopg2.connect
    except ImportError as exc:  # pragma: no cover - dependency check
        raise SystemExit("Install psycopg (v3) or psycopg2 to use this script.") from exc

try:
    from anytree import Node
    from anytree.exporter import JsonExporter
except ImportError as exc:  # pragma: no cover - dependency check
    raise SystemExit("Install anytree to use this script.") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build URL tree from PostgreSQL data")
    parser.add_argument(
        "--schema",
        default=os.getenv("PGSCHEMA") or os.getenv("POSTGRES_SCHEMA"),
        help="Schema name containing the URL table (optional; defaults to none)",
    )
    parser.add_argument(
        "--table",
        default=os.getenv("URL_TABLE", "crawled_urls"),
        help="Table name containing URLs (default: crawled_urls)",
    )
    parser.add_argument(
        "--column",
        default=os.getenv("URL_COLUMN", "url"),
        help="Column name containing URLs (default: url)",
    )
    parser.add_argument(
        "--output",
        default="url_tree.json",
        help="Output JSON filename (default: url_tree.json)",
    )
    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL") or os.getenv("PG_DSN"),
        help="Full PostgreSQL DSN/URL; takes priority over discrete params",
    )
    parser.add_argument("--host", default=os.getenv("PGHOST", "localhost"), help="PostgreSQL host")
    parser.add_argument("--port", default=os.getenv("PGPORT", "5432"), help="PostgreSQL port")
    parser.add_argument(
        "--dbname",
        default=os.getenv("PGDATABASE") or os.getenv("POSTGRES_DB") or "postgres",
        help="Database name (default: postgres)",
    )
    parser.add_argument(
        "--user",
        default=os.getenv("PGUSER") or os.getenv("POSTGRES_USER") or "postgres",
        help="Database user (default: postgres)",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD"),
        help="Database password",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on number of URLs to load (for testing)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        dest="batch_size",
        default=1000,
        help="Batch size for fetching rows (default: 1000)",
    )
    return parser.parse_args()


def _identifier(name: str) -> sql.Composed:
    parts = [part for part in name.split(".") if part]
    return sql.Identifier(*parts)


def connect_db(args: argparse.Namespace):
    if args.dsn:
        return _connect(args.dsn)

    kwargs = {
        "host": args.host,
        "port": args.port,
        "dbname": args.dbname,
        "user": args.user,
    }
    if args.password:
        kwargs["password"] = args.password

    return _connect(**kwargs)


def fetch_urls(
    conn,
    table: str,
    column: str,
    batch_size: int = 1000,
    limit: int | None = None,
) -> Iterator[str]:
    stmt = sql.SQL("SELECT {col} FROM {tbl} WHERE {col} IS NOT NULL").format(
        col=_identifier(column),
        tbl=_identifier(table),
    )
    params = []
    if limit:
        stmt = stmt + sql.SQL(" LIMIT %s")
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(stmt, params)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            for (url,) in rows:
                if url:
                    yield str(url)


def normalize_url(raw_url: str) -> dict[str, str] | None:
    url = str(raw_url).strip()
    if not url:
        return None

    url_to_parse = url if "://" in url else f"http://{url}"
    parsed = urlparse(url_to_parse)
    if not parsed.netloc:
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.query:
        path_parts.append("?" + parsed.query)

    normalized = parsed._replace(fragment="")
    full_url = normalized.geturl()
    domain_label = f"{normalized.scheme or 'http'}://{normalized.netloc}"

    return {
        "full_url": full_url,
        "domain": domain_label,
        "parts": path_parts,
    }


def build_tree(urls: Iterable[str]):
    root = Node("urls")
    node_cache: dict[tuple[str, ...], Node] = {(): root}
    seen_urls: set[str] = set()
    skipped = 0

    for raw_url in urls:
        normalized = normalize_url(raw_url)
        if not normalized:
            skipped += 1
            continue

        full_url = normalized["full_url"]
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        domain = normalized["domain"]
        domain_key = (domain,)
        domain_node = node_cache.get(domain_key)
        if domain_node is None:
            domain_node = Node(domain, parent=node_cache[()], url=domain)
            node_cache[domain_key] = domain_node
        else:
            domain_node.url = getattr(domain_node, "url", domain)

        parts = normalized["parts"]
        if not parts:
            # URL is the domain root
            domain_node.url = full_url
            continue

        parent = domain_node
        acc: list[str] = []
        for idx, segment in enumerate(parts):
            acc.append(segment)
            key = domain_key + tuple(acc)
            node = node_cache.get(key)
            is_leaf = idx == len(parts) - 1

            if node is None:
                node_kwargs = {"parent": parent}
                if is_leaf:
                    node_kwargs["url"] = full_url
                node = Node(segment, **node_kwargs)
                node_cache[key] = node
            elif is_leaf and not getattr(node, "url", None):
                node.url = full_url

            parent = node

    return root, len(seen_urls), skipped


def write_json(root: Node, output_path: str) -> None:
    exporter = JsonExporter(indent=2, sort_keys=False)
    json_data = exporter.export(root)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(json_data)


def main() -> None:
    args = parse_args()

    table_name = args.table
    if args.schema:
        # Allow passing already-qualified names; otherwise prefix with schema
        table_name = table_name if "." in table_name else f"{args.schema}.{table_name}"

    try:
        with connect_db(args) as conn:
            tree_root, unique_count, skipped = build_tree(
                fetch_urls(
                    conn=conn,
                    table=table_name,
                    column=args.column,
                    batch_size=args.batch_size,
                    limit=args.limit,
                )
            )
    except Exception as exc:  # pragma: no cover - runtime error surface
        raise SystemExit(f"Failed to build tree: {exc}") from exc

    write_json(tree_root, args.output)
    print(f"Saved tree to {args.output}")
    print(f"Unique URLs included: {unique_count}")
    if skipped:
        print(f"Skipped {skipped} entries that looked invalid")


if __name__ == "__main__":
    sys.exit(main())
