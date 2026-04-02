# AGENTS.md

## Project goal
This repo is a minimal local CLI for querying the Alaska Legislature BASIS API.

## Constraints
- Keep the project tiny.
- Prefer a single executable Python file.
- Use Python standard library only.
- Do not add frameworks, databases, Docker, virtualenv setup, or frontend code.
- Do not add dependencies unless the user explicitly asks.

## API assumptions
- Treat the BASIS API as a read-only XML HTTP service.
- Support these top-level sections: bills, members, committees, sessions.
- Include the required BASIS version header.
- Support repeated `X-Alaska-Legislature-Basis-Query` headers.
- Support optional `X-Alaska-Query-ResultRange`.

## CLI expectations
The script should:
- show the final URL before making the request
- support GET, HEAD, and OPTIONS
- accept common query params like session, chamber, and minifyresult
- accept optional subsection/path text
- allow multiple BASIS query headers
- optionally save the raw response to a file
- pretty-print XML to stdout when possible

## Done means
A good result passes:
- `python3 -m py_compile query_basis.py`
- `python3 query_basis.py --help`

## Style
- Clear code
- Defensive error handling
- Helpful command-line help text
- No unnecessary abstraction
