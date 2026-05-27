# Blue Sheets

Drop PDF blue sheets here, one per bill, named by the bill number.

## Naming convention

Any of these forms work — the loader normalizes on disk scan:

- `HB195.pdf`
- `HB 195.pdf`
- `hb195.pdf`
- `HJR38.pdf`
- `SB 252.pdf`

The mapping rule is: **strip whitespace, uppercase, drop `.pdf`** →
that's the bill number (e.g. "HB195" → "HB 195" when displayed).

## Deployment

This folder is tracked in git. To publish a new blue sheet:

```bash
cp my-blue-sheet.pdf basis-browser-app/blue_sheets/HB195.pdf
git add basis-browser-app/blue_sheets/HB195.pdf
git commit -m "Add blue sheet for HB 195"
git push
# auto-deploys to toolshed
```

The "Governor's Desk" page will show a `📄 Blue sheet` link next to
every bill that has a PDF in this folder, and nothing for the ones
that don't.
