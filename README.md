# basis-query

Tiny stdlib-only Python CLI for querying the Alaska Legislature BASIS API.

## What It Is

This project is a very small local command-line tool for sending read-only requests to the Alaska Legislature BASIS API.

It uses:
- Python standard library only
- one main script: `query_basis.py`
- no dependencies

## Defaults

By default, the script uses:
- base URL: `https://www.akleg.gov/publicservice/basis`
- BASIS version: `1.4`

## Quick Start

Run help:

```bash
python3 query_basis.py --help
```

Basic request:

```bash
python3 query_basis.py --section bills --session 34
```

HEAD request:

```bash
python3 query_basis.py --section bills --session 34 --head
```

Filter by chamber:

```bash
python3 query_basis.py --section bills --session 34 --chamber S
```

Use a BASIS query header:

```bash
python3 query_basis.py --section bills --session 34 --query "Bills;committeecode=RES"
```

Save the raw response:

```bash
python3 query_basis.py --section bills --session 34 --out bills.xml
```

## What The Script Supports

- `GET`, `HEAD`, and `OPTIONS`
- top-level sections: `bills`, `members`, `committees`, `sessions`
- query params like `session`, `chamber`, and `minifyresult`
- repeated `X-Alaska-Legislature-Basis-Query` inputs
- optional `X-Alaska-Query-ResultRange`
- XML pretty-printing when possible

## Importing From Python

The file can also be imported from other Python code:

```python
import query_basis

result = query_basis.fetch_basis(
    base_url=query_basis.DEFAULT_BASE_URL,
    section="bills",
    session="34",
    chamber="S",
    queries=["Bills;committeecode=RES"],
)

print(result["status"])
print(result["body"].decode("utf-8", errors="replace"))
```
