#!/usr/bin/env python3
"""
Track new UNOOSA satellite-registration documents (ST/SG/SER.E and
A/AC.105/INF series) by probing the UN ODS access API directly. Writes a
GCAT-style TSV list (planet4589/McDowell file conventions).

Why probing instead of a search API (verified 2026 Jul 11 vs the live site):

  * digitallibrary.un.org /search export formats (of=xm, of=recjson) are
    bot-blocked: HTTP 202 with empty body. /api/v1/search needs an account.
  * /rss works anonymously BUT ignores sort order and the dt=c date-added
    filter, and the library's symbol index for ST/SG/SER.E stops around
    /666 (year 2013) -- useless for finding new documents.
  * docs.un.org / undocs.org serve identical HTML shells for valid and
    invalid symbols, so page status can't be used as a hit test.
  * https://documents.un.org/api/symbol/access?s=<sym>&l=en&t=pdf is what
    the document viewer iframes:
        hit  -> 200, Content-Type: application/pdf
        miss -> 200, Content-Type: text/html  (app shell)
    That content-type difference is the reliable hit/miss signal.

Strategy per series, each run:
  1. Re-probe GAPS: serials missing between the lowest and highest already
     recorded (UNOOSA publishes out of order, so gaps fill in later).
  2. Scan UPWARD from one past the highest recorded serial (or a seed /
     auto-discovered frontier on first run) until --gap consecutive misses.
Date/title are pulled from each hit's PDF metadata. Existing rows keep
their FirstSeen date, so the TSV is both deliverable and state: FirstSeen
is the "newly posted" signal.

The A/AC.105/INF series separator ('/' vs '.') is auto-detected on first
run and thereafter inferred from the symbols already in the TSV.

Usage:
    py un_sat_registrations.py                 # incremental update
    py un_sat_registrations.py --start "ST/SG/SER.E:1250"   # force start
    py un_sat_registrations.py --gap 10 --max-scan 500

Requires: requests
"""

import argparse
import os
import re
import sys
import time

import requests

ACCESS = "https://documents.un.org/api/symbol/access"
HUMAN_URL = "https://docs.un.org/{lang}/{symbol}"   # human-friendly viewer link
LANGS = ["en", "fr", "es", "ru", "zh", "ar"]  # probe order; docs sometimes
                                              # appear in other langs first
HEADERS = {"User-Agent": "sat-registration-tracker/2.1 (research use; "
                         "https://github.com/mrrross/Space)"}

# series prefix -> first-run config. sep None = try '/' then '.' during
# frontier discovery; afterwards the sep is inferred from the TSV itself.
SERIES = {
    "ST/SG/SER.E": {"seed": 1290, "sep": "/"},
    # dot separator + anchor confirmed by discovery sweep 2026 Jul 11;
    # set seed None to re-run auto-discovery
    "A/AC.105/INF": {"seed": 401, "sep": "."},
}

COLS = ["UNReg", "Title", "PDFDate", "FirstSeen", "URL", "Source"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MAX_PDF_BYTES = 3 * 1024 * 1024   # metadata lives near start/end; cap reads


def vague_date(y, m, d):
    """GCAT Vague Date, e.g. '2026 Jul 8'."""
    return f"{y} {MONTHS[int(m) - 1]} {int(d)}"


def pdf_meta(data):
    """Best-effort date + title from PDF XMP/Info metadata."""
    date = ""
    m = (re.search(rb"<xmp:CreateDate>(\d{4})-(\d{2})-(\d{2})", data)
         or re.search(rb"/CreationDate\s*\(D:(\d{4})(\d{2})(\d{2})", data))
    if m:
        try:
            date = vague_date(m.group(1).decode(), m.group(2).decode(),
                              m.group(3).decode())
        except (ValueError, IndexError):
            pass

    title = ""
    m = re.search(rb"<dc:title>\s*<rdf:Alt>\s*<rdf:li[^>]*>(.*?)</rdf:li>",
                  data, re.S)
    if not m:
        m = re.search(rb"/Title\s*\(([^)]{0,300})\)", data)
    if m:
        raw = m.group(1)
        try:
            if raw.startswith(b"\xfe\xff"):
                title = raw[2:].decode("utf-16-be", "replace")
            else:
                title = raw.decode("utf-8", "replace")
        except Exception:
            title = ""
        title = re.sub(r"\s+", " ", title).strip()
        # PDF-producer junk like a bare job number is worse than nothing
        if re.fullmatch(r"[0-9A-Za-z_.-]{0,12}", title):
            title = ""
    return date, title


def probe(session, symbol, want_body=False, langs=("en",)):
    """Try the access API in each language until a PDF is found.

    Returns (is_hit, body_bytes_or_None, lang_or_None)."""
    for lang in langs:
        try:
            r = session.get(ACCESS,
                            params={"s": symbol, "l": lang, "t": "pdf"},
                            timeout=60, stream=True)
        except requests.RequestException as exc:
            print(f"  {symbol} ({lang}): request failed: {exc}",
                  file=sys.stderr)
            continue
        if "pdf" not in r.headers.get("Content-Type", "").lower():
            r.close()
            continue
        body = None
        if want_body:
            chunks, total = [], 0
            for c in r.iter_content(65536):
                chunks.append(c)
                total += len(c)
                if total >= MAX_PDF_BYTES:
                    break
            body = b"".join(chunks)
        r.close()
        return True, body, lang
    return False, None, None


def make_row(series, sep, n, body, today, lang):
    symbol = f"{series}{sep}{n}"
    date, title = pdf_meta(body or b"")
    return {"UNReg": symbol, "Title": title, "PDFDate": date,
            "FirstSeen": today,
            "URL": HUMAN_URL.format(lang=lang, symbol=symbol),
            "Source": "ods"}


def window_has_hit(session, series, sep, n, gap, delay):
    """True if any serial in n..n+gap-1 exists (gap-tolerant frontier test).
    English-only to keep the discovery sweep cheap."""
    for i in range(n, n + gap):
        hit, _, _ = probe(session, f"{series}{sep}{i}")
        time.sleep(delay)
        if hit:
            return True
    return False


def discover_frontier(session, series, seps, gap, delay, limit=2000):
    """Locate the modern range of a series with unknown extent/separator.

    Early docs may be absent from ODS, so contiguity from serial 1 can't be
    assumed. Sweep anchor windows every 100 serials up to `limit` for each
    candidate separator; return (highest hit anchor, sep) or (None, None).
    """
    for sep in seps:
        print(f"  sweeping {series}{sep}<n> for hits (1..{limit}) ...")
        best = None
        for a in range(1, limit + 1, 100):
            if window_has_hit(session, series, sep, a, gap, delay):
                best = a
        if best is not None:
            print(f"  highest hit anchor: {series}{sep}{best}")
            return best, sep
    return None, None


def collect_upward(session, series, sep, start, gap, max_scan, delay, today,
                   out, min_until=0):
    """Linear scan upward from start, appending hit rows to `out` as they
    are found (so an interrupt loses nothing already discovered).

    Misses don't count toward the stop condition until n >= min_until —
    used after frontier discovery so a sparse backfill region can't abort
    the scan before it reaches the known-good anchor."""
    found, misses, n = 0, 0, start
    while misses < gap and (n - start) < max_scan:
        hit, body, lang = probe(session, f"{series}{sep}{n}", want_body=True,
                                langs=LANGS)
        if hit:
            out.append(make_row(series, sep, n, body, today, lang))
            print(f"  HIT  {series}{sep}{n}  [{lang}]  "
                  f"({out[-1]['PDFDate'] or 'no date'})")
            found += 1
            misses = 0
        elif n >= min_until:
            misses += 1
        n += 1
        if (n - start) % 25 == 0:
            print(f"  ... at {series}{sep}{n} ({found} hits so far)")
        time.sleep(delay)


def recheck_gaps(session, series, sep, known, delay, today, out):
    """Probe serials missing between min(known) and max(known), appending
    hit rows to `out` as they are found."""
    gaps = sorted(set(range(min(known), max(known))) - set(known))
    if not gaps:
        return
    print(f"  re-probing {len(gaps)} gap serial(s): "
          f"{', '.join(map(str, gaps[:20]))}{' ...' if len(gaps) > 20 else ''}")
    for n in gaps:
        hit, body, lang = probe(session, f"{series}{sep}{n}", want_body=True,
                                langs=LANGS)
        if hit:
            out.append(make_row(series, sep, n, body, today, lang))
            print(f"  HIT  {series}{sep}{n}  [{lang}]  (gap filled, "
                  f"{out[-1]['PDFDate'] or 'no date'})")
        time.sleep(delay)


def read_existing(path):
    """Parse a previous TSV run. Returns {symbol: row_dict}."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            vals = line.split("\t")
            if len(vals) == len(COLS):
                out[vals[0]] = dict(zip(COLS, vals))
    return out


def parse_symbol(symbol, series):
    """Return (sep, serial) if symbol belongs to series, else (None, None)."""
    m = re.match(re.escape(series) + r"([/.])(\d+)$", symbol)
    return (m.group(1), int(m.group(2))) if m else (None, None)


def sort_key(row):
    for series in SERIES:
        _, s = parse_symbol(row["UNReg"], series)
        if s is not None:
            return (series, s)
    return (row["UNReg"], 0)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", action="append", default=[], metavar="SERIES:N",
                    help='force start serial, e.g. --start "ST/SG/SER.E:1250" '
                         "(repeatable)")
    ap.add_argument("--gap", type=int, default=5,
                    help="consecutive misses that end the scan (default 5)")
    ap.add_argument("--max-scan", type=int, default=300,
                    help="safety cap on serials scanned per series (default 300)")
    ap.add_argument("--backfill", type=int, default=30,
                    help="on first run of an auto-discovered series, also "
                         "collect this many serials below the frontier")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds between requests (default 0.5)")
    ap.add_argument("--no-gap-recheck", action="store_true",
                    help="skip re-probing gaps in the already-known range")
    ap.add_argument("-o", "--output", default="un_sat_registrations.tsv")
    args = ap.parse_args()

    forced = {}
    for spec in args.start:
        series, _, num = spec.rpartition(":")
        forced[series] = int(num)

    session = requests.Session()
    session.headers.update(HEADERS)

    now = time.gmtime()
    today = vague_date(now.tm_year, now.tm_mon, now.tm_mday)

    existing = read_existing(args.output)
    print(f"{len(existing)} entries already in {args.output}" if existing
          else "no existing TSV — first run")

    new_rows = []
    try:
        run_all_series(session, existing, forced, new_rows, args, today)
    except KeyboardInterrupt:
        print("\nInterrupted — saving what was found so far ...",
              file=sys.stderr)

    added = 0
    for row in new_rows:
        if row["UNReg"] not in existing:      # never overwrite FirstSeen
            existing[row["UNReg"]] = row
            added += 1

    rows = sorted(existing.values(), key=sort_key)
    stamp = (f"{now.tm_year} {MONTHS[now.tm_mon - 1]} {now.tm_mday} "
             f"{now.tm_hour:02d}{now.tm_min:02d}:{now.tm_sec:02d}")
    with open(args.output, "w", newline="", encoding="utf-8") as fh:
        fh.write("#" + "\t".join(COLS) + "\n")
        fh.write(f"# Updated {stamp}\n")
        for row in rows:
            # GCAT convention: '-' for empty/unknown fields
            fh.write("\t".join(row[c] or "-" for c in COLS) + "\n")
    print(f"Wrote {len(rows)} entries ({added} new) to {args.output}")


def run_all_series(session, existing, forced, new_rows, args, today):
    for series, cfg in SERIES.items():
        print(f"Probing {series} ...")
        known, sep = [], cfg["sep"]
        for symbol in existing:
            s_sep, serial = parse_symbol(symbol, series)
            if serial is not None:
                known.append(serial)
                sep = s_sep          # infer separator from recorded symbols

        if known and not args.no_gap_recheck:
            recheck_gaps(session, series, sep, known, args.delay, today,
                         new_rows)

        min_until = 0
        if series in forced:
            start = forced[series]
        elif known:
            start = max(known) + 1
        elif cfg["seed"]:
            start = cfg["seed"]
        else:
            anchor, sep = discover_frontier(
                session, series, ["/", "."], args.gap, args.delay)
            if anchor is None:
                print(f"  no documents found for {series} with '/' or '.' "
                      "separators; check the symbol format", file=sys.stderr)
                continue
            start = max(1, anchor - args.backfill)
            min_until = anchor + args.gap   # don't abort before the anchor

        # never let misses in the very first stretch kill the scan
        min_until = max(min_until, start + args.gap)
        print(f"  scanning upward from {series}{sep}{start}")
        collect_upward(session, series, sep, start, args.gap,
                       args.max_scan, args.delay, today, new_rows,
                       min_until=min_until)


if __name__ == "__main__":
    main()
