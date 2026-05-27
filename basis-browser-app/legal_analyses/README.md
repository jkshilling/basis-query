# Legal Analyses

Drop legal-analysis PDFs here, one per file. Unlike blue sheets, legal
analyses apply to **both bills and resolutions** — Legislative Legal
Services and the Department of Law write analyses on HJRs/SJRs (especially
constitutional questions) just as much as HBs/SBs.

## Naming convention

Same forgiving rules as `blue_sheets/`. Drop any of these:

- `HB195.pdf`
- `HB 195.pdf`
- `LLS Legal Memo HB195 5.20.26.pdf`
- `2026-05-20 - HJR38 - DOL legal review.pdf`
- `SJR9CS(JUD)-LLS-LEGAL-05-12-26.pdf`

The matcher extracts the first bill prefix + number anywhere in the
filename and uses it as the canonical billnumber.

## Source labels the matcher recognizes

If the filename contains `LLS` (Legislative Legal Services) or
`DOL` / `AG` / `LAW` (Department of Law / Attorney General), the
chip on the page will display the source. Dates in `m.d.yy` /
`mm-dd-yy` / `m/d/yyyy` forms are extracted and displayed as well.

## Deployment

Same as blue sheets — drop, commit, push, deploy:

```bash
cp my-legal.pdf basis-browser-app/legal_analyses/HB195-LLS-5-20-26.pdf
git add basis-browser-app/legal_analyses/
git commit -m "Add LLS legal analysis for HB 195"
git push
```
