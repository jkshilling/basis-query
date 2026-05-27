# Briefing packets

Drop briefing-packet PDFs or DOCX files in this folder. They'll be
indexed by the bill number found in the filename and rendered as
chips on each bill's card on the `/awaiting-transmittal` page.

## Filename conventions

The indexer is forgiving — it scans for the first bill prefix +
digits anywhere in the filename, optionally preceded by a CS/HCS/SCS
committee-substitute marker. Leading zeros are stripped.

These all index under `HB 110`:
- `HB 110 Briefing Packet.pdf`
- `HB110_BP_2026-05-22.pdf`
- `Briefing Packet - SCS HB 110(FIN).docx`
- `HB0110 GLO Brief.pdf`

A single packet covering multiple bills (e.g. an omnibus override
brief) indexes under each one. Example:
- `Briefing Packet HB 10 and HB 176.pdf` → both HB 10 and HB 176

## What's parsed

For each file the indexer extracts:
- The bill number(s) referenced
- A date (from filename or file mtime as last resort)
- A label for the chip (currently just "Brief" + date)

If you want me to extract recommendations or substantive content
from the briefing packet body — same way we mine blue sheets for
SIGN/VETO/LWOS and the "What does this Bill do?" section — let me
know what fields the briefing packets carry and I'll wire it up.
