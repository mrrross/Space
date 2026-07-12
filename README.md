# UN Satellite Registration Tracker

Tracks newly posted UNOOSA satellite-registration documents
(ST/SG/SER.E/ and A/AC.105/INF/ series) into a GCAT-style TSV list.

UNOOSA's own site has stopped posting new registrations, but the documents
still appear on the UN ODS. This script probes the ODS access API
(`documents.un.org/api/symbol/access`) for serial numbers above the last
known one: a hit returns the PDF, a miss returns an HTML shell. Dates and
titles are pulled from each PDF's metadata.

The UN Digital Library was evaluated and rejected as a source: its search
export API is bot-blocked, its RSS feed ignores sorting and date filters,
and its symbol index for this series is over a decade behind (verified
2026 Jul 11 — details in the docstring of `un_sat_registrations.py`).

## Quick start

    py -m pip install -r requirements.txt
    py un_sat_registrations.py

Output: `un_sat_registrations.tsv` — tab-separated, '#'-prefixed header,
'# Updated' timestamp line, GCAT Vague Date format (the same file
conventions as planet4589.org/space/gcat).

The TSV is also the state: each run rescans from one past the highest
serial recorded and appends new rows. `FirstSeen` never changes once
written, so it records when each document was first detected — the
"newly posted" signal.

Each run also re-probes gaps in the already-known serial range (UNOOSA
publishes out of order, so gaps fill in later), checks for /Add.N and
/Corr.N supplements on the most recent documents (addenda register extra
objects; corrigenda correct data — both can appear months after the base
document), and each probe falls back through all six UN languages —
documents sometimes appear in French, Russian, etc. before the English
version.

## Options

    --start "ST/SG/SER.E:1250"   force the scan start for a series (repeatable)
    --gap 5                      consecutive misses that end the scan
    --max-scan 300               safety cap on serials scanned per series
    --backfill 30                first-run backfill below a discovered frontier
    --delay 0.5                  seconds between requests
    --no-gap-recheck             skip re-probing known gaps
    --supp-window 25             recent docs per series to check for Add/Corr
    -o FILE                      output path

Series notes: ST/SG/SER.E uses '/' before the serial; A/AC.105/INF uses
'.' (e.g. A/AC.105/INF.401 — confirmed by probing). If a series has no
seed configured, the script auto-discovers its range with an anchor sweep.

## Output columns

    UNReg      UN document symbol (e.g. ST/SG/SER.E/1290)
    Title      title from PDF metadata (best effort; often blank)
    PDFDate    creation date from PDF metadata (Vague Date format)
    FirstSeen  date this script first found the document
    URL        viewer link (docs.un.org/<lang>/<symbol>, first language found)
    Source     ods

## Status

Live since 2026 Jul 11, refreshing daily via GitHub Actions. Current
coverage: 46 documents — ST/SG/SER.E 1290–1324 plus supplements (e.g.
1295/Add.1), and A/AC.105/INF 400–424 (that series has been dormant
since 2013). Unissued serial numbers in the known range and supplements
on recent documents are re-checked on every run, in all six UN languages.
