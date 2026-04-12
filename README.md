# basis-query

Tiny stdlib-only Python CLI for querying the Alaska Legislature BASIS API.

This repo contains:
- `query_basis.py` for direct BASIS API requests
- `chat_basis.py` for an optional plain-English terminal wrapper

Defaults:
- base URL: `https://www.akleg.gov/publicservice/basis`
- BASIS version: `1.4`

## Quick Start

```bash
python3 query_basis.py --help
python3 query_basis.py --section bills --session 34
python3 query_basis.py --section bills --session 34 --head
python3 query_basis.py --section bills --session 34 --query "Actions" --range "..1"
python3 query_basis.py --section meetings --session 34 --query "Media" --range "..3"
python3 query_basis.py --section meetings --session 34 --query "Documents" --range "..3"
```

Optional chat wrapper:

```bash
python3 chat_basis.py --help
export OPENAI_API_KEY=your_key_here
python3 chat_basis.py
```

Supports:
- `GET`, `HEAD`, and `OPTIONS`
- sections: `bills`, `members`, `committees`, `sessions`, `meetings`
- query params like `session`, `chamber`, and `minifyresult`
- repeated `X-Alaska-Legislature-Basis-Query`
- optional `X-Alaska-Query-ResultRange`
- XML pretty-printing when possible

Current caution:
- `meetings` + `Documents` is real, but broader filtered queries can still fault.
- `meetings` is the flakiest public section, so small ranges and narrow date windows work best.

## Importing From Python

`query_basis.py` can also be imported from other Python code:

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

## LLM Chat Wrapper

`chat_basis.py` adds a very small terminal chat layer on top of `query_basis.py`.

Requirements:
- `OPENAI_API_KEY` must be set
- Python standard library only
