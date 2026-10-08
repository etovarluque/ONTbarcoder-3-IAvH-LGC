from __future__ import annotations
import os
import re
import csv
import json
import time
import datetime
import threading
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtCore, QtGui, QtWidgets
from .shared import *
from .shared import _get_base_dir, _profiles_dir, _tr, _json_mod
from .best_seq_panel import (
    _RefDropZone, read_tax_reference, QUERY_TAX_COLUMNS,
    sample_id_of, lookup_tax, concordance_level, display_taxon,
    read_tax_reference_cached, reference_match_check, fasta_headers,
    table_column, _cached, reference_format_warning, reference_report_lines,
    reference_empty_ids, BLAST_RESULTS_SHEET, repeated_headers, repeated_note,
    
)



# ════════════════════════════════════════════════════════════════════════════
# BATCH PLANNING
#
# NCBI penalises the number of *searches* (over 100 in 24 h moves the IP to a
# slower queue), while its hard limit is the length of one query: 1,000,000
# bases for blastn. Both the panel preview and the worker use the functions
# below, so what the user is shown is exactly what will be submitted.
# ════════════════════════════════════════════════════════════════════════════

# Cap for one query, a little below NCBI's 1,000,000 so headers and percent
# encoding cannot push a batch over the line.
MAX_QUERY_BASES = 900_000
# Ceiling on sequences in one search. Not a documented NCBI limit but a
# practical one: NCBI also enforces an undocumented CPU-time budget per web
# BLAST job (independent of MAX_QUERY_BASES), and a MEGABLAST search of a few
# hundred sequences against a huge database like core_nt can exceed it,
# leaving the submission to time out with no usable reply. In testing, 500
# and 250 both still failed against core_nt; 100 was the first size that
# worked reliably. _BlastWorker also auto-splits a batch NCBI still rejects.
MAX_QUERY_SEQS  = 100


def plan_batch_sizes(lengths, nseq, max_bases=MAX_QUERY_BASES):
    """Split `lengths` into batches of at most `nseq` sequences / `max_bases` bases.

    Returns the size of each batch, in order. A single sequence longer than
    `max_bases` gets a batch of its own rather than being dropped: letting NCBI
    reject it explicitly beats losing it in silence.
    """
    sizes = []
    count = 0
    bases = 0
    for L in lengths:
        if count and (count >= nseq or bases + L > max_bases):
            sizes.append(count)
            count = 0
            bases = 0
        count += 1
        bases += L
    if count:
        sizes.append(count)
    return sizes


def auto_nseq(lengths, max_bases=MAX_QUERY_BASES, max_seqs=MAX_QUERY_SEQS):
    """Sequences per search that uses the fewest searches, evenly filled.

    Fewest searches is what protects the user's IP quota; spreading the
    sequences evenly over that number avoids a last batch of two or three.
    """
    n = len(lengths)
    if n == 0:
        return max_seqs
    total = sum(lengths)
    # Smallest batch count that satisfies both ceilings.
    k = max(1,
            -(-total // max_bases),   # ceil division
            -(-n // max_seqs))
    return max(1, -(-n // k))         # spread evenly over k batches


def fasta_lengths(path):
    """Sequence lengths in a FASTA file, ignoring headers and blank lines."""
    lengths = []
    cur = 0
    started = False
    try:
        opener = __import__("gzip").open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if ln.startswith(">"):
                    if started:
                        lengths.append(cur)
                    cur = 0
                    started = True
                elif started:
                    cur += len(ln.strip())
        if started:
            lengths.append(cur)
    except Exception:
        return []
    return lengths


# Subject_* (BLAST hit) taxonomy columns, compared against QUERY_TAX_COLUMNS
# to compute how deep the two agree — see apply_reference_and_tax_match().
_SUBJECT_TAX_COLUMNS = ("Subject_Order", "Subject_Family", "Subject_Genus", "Subject_organism")
_TAX_RANK_KEYS = ("order", "family", "genus", "organism")


def apply_reference_and_tax_match(tsv_path: str, ref: dict, ref_lower: dict,
                                   strip_suffix: str = ""):
    """Write the Query_* reference-taxonomy columns into *tsv_path* and, when
    the table also carries Subject_* (hit) taxonomy, append a 'Tax_level_match'
    column recording the deepest rank at which the two agree — the same rule
    Best Sequence uses to judge a hit (`concordance_level`).

    Both additions are made in the same read/rewrite pass, rather than as two
    separate full read-modify-write passes over the file (one to write the
    Query_* columns, another to add Tax_level_match).

    Returns (rows seen, rows filled from the reference, sample IDs absent from
    the reference, rows given a Tax_level_match — or -1 if the table has no
    Subject_* taxonomy to compare against).
    """
    with open(tsv_path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        first = fh.readline()
        fh.seek(0)
        sep = "\t" if "\t" in first else ","
        rows = [r for r in csv.reader(fh, delimiter=sep)]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError(f"Empty BLAST table: {os.path.basename(tsv_path)}")

    headers = [c.strip() for c in rows[0]]
    if "Query_name" not in headers:
        raise ValueError(f"{os.path.basename(tsv_path)} has no 'Query_name' column.")

    # A version of this function used to append a new Tax_level_match column
    # on every re-run instead of reusing the existing one. Clean up any
    # leftover duplicates from that now, keeping only the last (most recently
    # written) one.
    dup_positions = [i for i, h in enumerate(headers) if h == "Tax_level_match"]
    if len(dup_positions) > 1:
        drop = set(dup_positions[:-1])
        keep = [i for i in range(len(headers)) if i not in drop]
        headers = [headers[i] for i in keep]
        rows = [rows[0]] + [
            [r[i] if i < len(r) else "" for i in keep] for r in rows[1:]
        ]

    q_i = headers.index("Query_name")

    query_idx = {}
    for name in QUERY_TAX_COLUMNS:
        if name in headers:
            query_idx[name] = headers.index(name)
        else:
            query_idx[name] = len(headers)
            headers.append(name)

    has_subject_tax = all(c in headers for c in _SUBJECT_TAX_COLUMNS)
    subject_idx = ({c: headers.index(c) for c in _SUBJECT_TAX_COLUMNS}
                   if has_subject_tax else {})
    match_idx = None
    if has_subject_tax:
        # Reuse an existing Tax_level_match column (e.g. from a previous run
        # of this same function) instead of appending a second one.
        if "Tax_level_match" in headers:
            match_idx = headers.index("Tax_level_match")
        else:
            match_idx = len(headers)
            headers.append("Tax_level_match")
    width = len(headers)

    out = [headers]
    n_rows = n_filled = n_match = 0
    unknown = set()
    for row in rows[1:]:
        row = list(row) + [""] * (width - len(row))
        value = row[q_i] if q_i < len(row) else ""
        if str(value).strip():
            n_rows += 1
            sample = sample_id_of(value, strip_suffix)
            tax = lookup_tax(sample, ref, ref_lower)
            if tax is None:
                unknown.add(sample)
                tax = ("", "", "", "")
            else:
                n_filled += 1
            for name, val in zip(QUERY_TAX_COLUMNS, tax):
                row[query_idx[name]] = val
        if has_subject_tax:
            qtax = {k: display_taxon(row[query_idx[c]])
                    for k, c in zip(_TAX_RANK_KEYS, QUERY_TAX_COLUMNS)}
            hit = {k: display_taxon(row[subject_idx[c]])
                   for k, c in zip(_TAX_RANK_KEYS, _SUBJECT_TAX_COLUMNS)}
            row[match_idx] = concordance_level(hit, qtax)
            n_match += 1
        out.append(row)

    tmp = tsv_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, delimiter=sep, lineterminator="\n").writerows(out)
    os.replace(tmp, tsv_path)
    return n_rows, n_filled, sorted(unknown), (n_match if has_subject_tax else -1)


# Hit_rank and the BLAST metric columns (P_identity … Bit_score) are
# numeric. Writing them as strings makes Excel flag every cell with
# "Number stored as text", whose background error-checker re-scans the
# sheet on every sort/filter/scroll → high CPU. Convert these to
# int/float so openpyxl writes native numeric cells; the rest stay text.
# Detect them by header name so it is robust to column shifts.
_NUMERIC_NAMES = frozenset({
    "Hit_rank", "P_identity", "Alignment_length", "Num_mismatches",
    "Gap_opens", "Query_start", "Query_end", "Subject_start",
    "Subject_end", "Evalue", "Bit_score",
})


def _num(v):
    if v == "":
        return v
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)   # handles decimals and e-notation (Evalue)
    except ValueError:
        return v          # leave genuinely non-numeric text as-is


class _XlsxBuildError(Exception):
    """openpyxl missing, or the TSV could not be read — not the write itself."""


def build_xlsx_from_tsv(tsv_path: str, run_info: Optional[dict] = None,
                        params_override: Optional[dict] = None) -> str:
    """Convert *tsv_path* to a formatted .xlsx next to it, overwriting any
    existing file of that name (e.g. from an earlier run, or before the
    reference/Tax_level_match columns were updated by _ApplyReferenceDialog).

    Besides the hit table ("BLAST Results") the workbook gets a "Summary"
    sheet and a "Best hit" sheet (see _add_summary_sheets). *run_info*
    describes the run (input files, parameters, ...); when None it is read
    from the run's state file next to the table, if there is one.
    *params_override* replaces some of its parameters (e.g. the reference
    file just applied by _ApplyReferenceDialog).

    Module-level (rather than a _BlastWorker method) so both the worker and
    _ApplyReferenceDialog — which runs on the UI thread with no worker
    instance to call it on — can regenerate the xlsx after editing the TSV.

    Raises _XlsxBuildError if openpyxl isn't available or the TSV can't be
    read, or whatever exception wb.save() raises (e.g. the xlsx is open
    elsewhere) — the caller decides how to report those.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise _XlsxBuildError(f"openpyxl not available: {e}")
    try:
        with open(tsv_path, "r", encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
    except Exception as e:
        raise _XlsxBuildError(f"could not read TSV: {e}")
    if not lines:
        return ""

    # The TSV already carries Hit_rank (per-sample 1..N, best hit = 1) as its
    # first column, so the layout is read as-is.
    headers = lines[0].split("\t")
    n_cols  = len(headers)

    # openpyxl 3.x requires 8-char ARGB hex strings (alpha + RGB).
    # Zone 1 → cols 0-1   : Hit_rank, Query_name
    # Zone 2 → cols 2-12  : BLAST metric columns (accession … bit-score)
    # Zone 3 → cols 13+   : Taxonomy columns (only when fetch_taxonomy=True)
    H1 = PatternFill(patternType="solid", fgColor="FF1A365D")   # header navy
    H2 = PatternFill(patternType="solid", fgColor="FF0D5E6E")   # header teal
    H3 = PatternFill(patternType="solid", fgColor="FF7C3200")   # header burnt-orange
    D1 = PatternFill(patternType="solid", fgColor="FFE8F1FB")   # data light-blue
    D2 = PatternFill(patternType="solid", fgColor="FFE8F5F6")   # data light-teal
    D3 = PatternFill(patternType="solid", fgColor="FFFEF3E8")   # data light-orange

    white_bold  = Font(color="FFFFFFFF", bold=True, size=10)
    normal_font = Font(size=10)
    bold_font   = Font(size=10, bold=True)   # best hit (Hit_rank == 1) per sample
    hdr_align   = Alignment(horizontal="center", vertical="center")
    dat_align   = Alignment(vertical="center", wrap_text=False)
    thin        = Side(style="thin", color="FFCCCCCC")
    medium      = Side(style="medium", color="FF9AA0A6")   # sample-block divider
    border      = Border(left=thin, right=thin, top=thin, bottom=thin)
    # Same as `border` but with a heavier top edge — marks the first row of each
    # new sample block so the hit groups are visually separated.
    border_group_top = Border(left=thin, right=thin, top=medium, bottom=thin)

    _numeric_idx = frozenset(
        i for i, h in enumerate(headers) if h in _NUMERIC_NAMES)

    def _hfill(ci):
        return H1 if ci <= 1 else (H2 if ci <= 12 else H3)

    def _dfill(ci, tinted):
        if not tinted:
            return None
        return D1 if ci <= 1 else (D2 if ci <= 12 else D3)

    wb = Workbook()
    ws = wb.active
    ws.title = BLAST_RESULTS_SHEET

    # Header row
    ws.append(headers)
    for ci in range(n_cols):
        cell = ws.cell(row=1, column=ci + 1)
        cell.fill      = _hfill(ci)
        cell.font      = white_bold
        cell.alignment = hdr_align
        cell.border    = border
    ws.row_dimensions[1].height = 22

    # Data rows — shaded in BLOCKS by sample (Query_name), not per single row,
    # so each sample's hits share one tint and the next sample flips tint. A
    # heavier top border marks the first row of each new block. Rows already
    # arrive grouped by Query_name (see _rank_rows), so a simple value-change
    # check delimits the blocks. Column indices are looked up by name to stay
    # robust to layout shifts (taxonomy columns present or not).
    try:
        _query_idx = headers.index("Query_name")
    except ValueError:
        _query_idx = 1
    try:
        _rank_idx = headers.index("Hit_rank")
    except ValueError:
        _rank_idx = 0

    prev_query   = None
    block_tinted = True   # first block tinted (matches former row-2 behaviour)
    for rn, line in enumerate(lines[1:], start=2):
        raw_vals = line.split("\t")
        while len(raw_vals) < n_cols:
            raw_vals.append("")
        raw_vals = raw_vals[:n_cols]

        query     = raw_vals[_query_idx] if _query_idx < n_cols else ""
        new_block = (prev_query is not None and query != prev_query)
        if new_block:
            block_tinted = not block_tinted
        prev_query = query

        rank_raw = raw_vals[_rank_idx] if _rank_idx < n_cols else ""
        is_best  = (str(rank_raw).strip() == "1")   # best hit of this sample

        vals = [
            _num(v) if ci in _numeric_idx else v
            for ci, v in enumerate(raw_vals)
        ]
        ws.append(vals)
        row_border = border_group_top if new_block else border
        row_font   = bold_font if is_best else normal_font
        for ci in range(n_cols):
            cell = ws.cell(row=rn, column=ci + 1)
            fill = _dfill(ci, block_tinted)
            if fill:
                cell.fill = fill
            cell.font      = row_font
            cell.alignment = dat_align
            cell.border    = row_border

    # Freeze header, enable auto-filter
    ws.freeze_panes    = "A2"
    ws.auto_filter.ref = ws.dimensions

    # Auto-fit column widths (capped at 55 chars)
    for ci, col_cells in enumerate(ws.columns):
        width = max((len(str(c.value or "")) for c in col_cells), default=8)
        ws.column_dimensions[get_column_letter(ci + 1)].width = min(width + 2, 55)

    if run_info is None:
        run_info = load_run_info(tsv_path)
    if params_override:
        run_info = dict(run_info)
        run_info["params"] = {**(run_info.get("params") or {}), **params_override}
    rows = [(line.split("\t") + [""] * n_cols)[:n_cols] for line in lines[1:]]
    _add_summary_sheets(wb, headers, rows, tsv_path, run_info)

    xlsx_path = tsv_path.rsplit(".", 1)[0] + ".xlsx"
    wb.save(xlsx_path)   # overwrites any existing file of that name
    return xlsx_path


# ── Summary and Best hit sheets ─────────────────────────────────────────────

# How far each query was identified, deepest first. The four ranks and "none"
# are Tax_level_match values (best hit vs. the reference taxonomy); the rest
# describe queries without a usable comparison.
_ID_LABELS = {
    "organism":     "Species (organism)",
    "genus":        "Genus",
    "family":       "Family",
    "order":        "Order",
    "none":         "No match with the reference (none)",
    "no_reference": "Sample not in the reference",
    "blast_hit":    "With BLAST hits",       # no reference applied to the table
    "no_hit":       "No BLAST hit",
    "not_searched": "Not searched (run incomplete)",
}
_RANK_SCORE = {"organism": 4, "genus": 3, "family": 2, "order": 1}
_ID_FILL = {
    "organism": "FFC6EFCE", "genus": "FFE2F0D9", "family": "FFFFF2CC",
    "order": "FFFCE4D6", "none": "FFF8CBAD", "no_reference": "FFEDEDED",
    "no_hit": "FFD9D9D9", "not_searched": "FFD9D9D9",
}
# Header colours of the Best hit sheet, by column name: the query block
# (navy), taxonomy (burnt orange) and, for everything else, BLAST metrics (teal).
_ID_ZONE_COLS = frozenset(("Identification", "Hits", "Hit_rank", "Query_name"))
_TAX_ZONE_COLS = frozenset(
    ("Subject_Kingdom", "Subject_Class", "Tax_level_match")
    + _SUBJECT_TAX_COLUMNS + tuple(QUERY_TAX_COLUMNS))


def load_run_info(tsv_path: str) -> dict:
    """What the Summary sheet needs to know about the run that produced
    *tsv_path*, read from its blast-<run_id>.state.json (input files,
    parameters, sequences searched). {} when there is no state file, e.g. a
    table from the BLAST Results File tab: the summary then describes the
    table alone."""
    base = os.path.splitext(tsv_path)[0]
    try:
        with open(base + ".state.json", encoding="utf-8") as fh:
            st = json.load(fh)
    except Exception:
        return {}
    if st.get("kind") == "hit_table":
        # Written by the BLAST Results File tab (see save_hit_table_info).
        return {"kind": "hit_table", "params": st.get("params") or {},
                "inputs": [(f, qs) for f, qs in st.get("inputs") or []]}
    fasta = base + ".fa"
    return {"kind": "search", "run_id": st.get("run_id", ""),
            "files": st.get("files") or [], "params": st.get("params") or {},
            "processed": st.get("processed"), "status": st.get("status", ""),
            "fasta": fasta if os.path.isfile(fasta) else ""}


def save_hit_table_info(tsv_path: str, inputs: list, params: dict):
    """Keep what the Summary sheet says about a table built from NCBI Hit Table
    files (the files and the queries of each, the settings) beside it, so the
    sheet can be rebuilt when a reference is applied later. Never raises: this
    only feeds the Summary."""
    info = {"kind": "hit_table", "params": params,
            "inputs": [[f, list(qs)] for f, qs in inputs]}
    try:
        with open(os.path.splitext(tsv_path)[0] + ".state.json", "w", encoding="utf-8") as fh:
            json.dump(info, fh)
    except Exception:
        pass


def _fasta_query_names(path: str) -> Optional[List[str]]:
    """Query_name of every record of a FASTA as a BLAST run writes it (the
    header without '>', spaces as '_'; see _to_single_line_fasta), or None if
    the file cannot be read."""
    if not path:
        return None
    try:
        opener = __import__("gzip").open if path.lower().endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
            return [ln.strip().replace(" ", "_")[1:]
                    for ln in fh if ln.lstrip().startswith(">")]
    except Exception:
        return None


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def pick_best_hits(headers: List[str], rows: List[list]) -> Dict[str, tuple]:
    """Best hit of every query of a hit table: {Query_name: (row, n_hits)}.

    The best hit is the one that agrees with the reference taxonomy at the
    deepest rank (Tax_level_match), which is not necessarily BLAST's first.
    Hits at the same rank (and every hit when no reference was applied, or
    when none agrees, "none") are ordered by bit score, then identity, then
    fewest gap openings and mismatches, then BLAST's own order. The query's
    own ambiguities are the same for all of its hits, so they cannot
    separate them.
    """
    col = {h: i for i, h in enumerate(headers)}

    def get(r, name):
        i = col.get(name)
        return r[i] if i is not None and i < len(r) else ""

    groups: Dict[str, list] = {}
    for r in rows:
        q = str(get(r, "Query_name")).strip()
        if q:
            groups.setdefault(q, []).append(r)

    def key(r):
        return (_RANK_SCORE.get(str(get(r, "Tax_level_match")).strip(), 0),
                _to_float(get(r, "Bit_score")), _to_float(get(r, "P_identity")),
                -_to_float(get(r, "Gap_opens")), -_to_float(get(r, "Num_mismatches")),
                -_to_float(get(r, "Hit_rank")))

    return {q: (max(hits, key=key), len(hits)) for q, hits in groups.items()}


def _identification(row: list, col: dict, has_match: bool) -> str:
    """Identification category of a query, from its best hit row."""
    if not has_match:
        return "blast_hit"
    if not any(display_taxon(row[col[c]]) for c in QUERY_TAX_COLUMNS if c in col):
        return "no_reference"
    level = str(row[col["Tax_level_match"]]).strip()
    return level if level in _RANK_SCORE else "none"


def refresh_summary_sheets(wb, path: str, params_override: Optional[dict] = None) -> bool:
    """Rebuild the Summary and Best hit sheets of a BLAST results workbook
    already open in *wb* (saved at *path*) from its hit table, e.g. after the
    table was edited in place by Best Sequence's reference step. Does nothing
    and returns False when the workbook has neither sheet (not a BLAST panel
    workbook, or one made before they existed)."""
    if not ({"Summary", "Best hit"} & set(wb.sheetnames)):
        return False
    from .best_seq_panel import blast_sheet
    sheet = blast_sheet(wb)
    values = [["" if v is None else str(v) for v in r]
              for r in sheet.iter_rows(values_only=True)]
    values = [r for r in values if any(v.strip() for v in r)]
    for name in ("Summary", "Best hit"):
        if name in wb.sheetnames:
            del wb[name]
    if not values:
        return False
    headers = [h.strip() for h in values[0]]
    n_cols = len(headers)
    rows = [(r + [""] * n_cols)[:n_cols] for r in values[1:]]
    run_info = load_run_info(path)
    if params_override:
        run_info["params"] = {**(run_info.get("params") or {}), **params_override}
    _add_summary_sheets(wb, headers, rows, path, run_info)
    return True


def _add_summary_sheets(wb, headers: List[str], rows: List[list], tsv_path: str,
                        run_info: dict):
    """Add the "Summary" (first, shown on opening) and "Best hit" sheets
    ahead of the hit table, in the hit table's style."""
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    thin = Side(style="thin", color="FFCCCCCC")
    st = {
        "H1": PatternFill(patternType="solid", fgColor="FF1A365D"),   # navy
        "H2": PatternFill(patternType="solid", fgColor="FF0D5E6E"),   # teal
        "H3": PatternFill(patternType="solid", fgColor="FF7C3200"),   # burnt orange
        "white_bold": Font(color="FFFFFFFF", bold=True, size=10),
        "normal_font": Font(size=10),
        "bold_font": Font(size=10, bold=True),
        "hdr_align": Alignment(horizontal="center", vertical="center"),
        "dat_align": Alignment(vertical="center", wrap_text=False),
        "border": Border(left=thin, right=thin, top=thin, bottom=thin),
    }

    col = {h: i for i, h in enumerate(headers)}
    has_match = "Tax_level_match" in col
    best = pick_best_hits(headers, rows)
    kind = run_info.get("kind", "")
    unit = "Queries" if kind == "hit_table" else "Sequences"

    # ── Input files and the queries each one holds ──
    inputs = run_info.get("inputs")          # [(path, [Query_name] | None)]
    if inputs is None:
        inputs = [(f, _fasta_query_names(f)) for f in run_info.get("files") or []]

    # Every query of the run: those of the FASTA actually searched (so the
    # sequences without hits count too), else those of the input files, plus
    # any in the table.
    all_q = set(best)
    fa_q = _fasta_query_names(run_info.get("fasta", ""))
    if fa_q:
        all_q.update(fa_q)
    else:
        for _f, qs in inputs:
            all_q.update(qs or [])
    processed = run_info.get("processed")
    processed = set(processed) if processed is not None else None

    cats: Dict[str, str] = {}
    for q in all_q:
        if q in best:
            cats[q] = _identification(best[q][0], col, has_match)
        elif processed is not None and q not in processed:
            cats[q] = "not_searched"
        else:
            cats[q] = "no_hit"

    if has_match:
        levels = ["organism", "genus", "family", "order", "none", "no_reference",
                  "no_hit", "not_searched"]
    else:
        levels = ["blast_hit", "no_hit", "not_searched"]
    counts = {lv: sum(1 for c in cats.values() if c == lv) for lv in levels}
    levels = [lv for lv in levels
              if counts[lv] or lv not in ("no_reference", "not_searched")]
    n_total = len(all_q)

    def fill(argb):
        return PatternFill(patternType="solid", fgColor=argb)

    # ── Best hit: one row per query ──
    wsb = wb.create_sheet("Best hit", 0)
    b_headers = ["Identification", "Hits"] + headers
    wsb.append(b_headers)
    for ci, name in enumerate(b_headers, 1):
        cell = wsb.cell(row=1, column=ci)
        cell.fill = (st["H1"] if name in _ID_ZONE_COLS
                     else st["H3"] if name in _TAX_ZONE_COLS else st["H2"])
        cell.font, cell.alignment, cell.border = st["white_bold"], st["hdr_align"], st["border"]
    wsb.row_dimensions[1].height = 22
    q_i = col.get("Query_name")
    for rn, q in enumerate(sorted(all_q), start=2):
        cat = cats[q]
        if q in best:
            row, n_hits = best[q]
            vals = [_num(v) if headers[ci] in _NUMERIC_NAMES else v
                    for ci, v in enumerate(row)]
        else:
            n_hits = 0
            vals = [""] * len(headers)
            if q_i is not None:
                vals[q_i] = q
        wsb.append([cat, n_hits] + vals)
        for ci in range(1, len(b_headers) + 1):
            cell = wsb.cell(row=rn, column=ci)
            cell.font, cell.alignment, cell.border = st["normal_font"], st["dat_align"], st["border"]
        if cat in _ID_FILL:
            wsb.cell(row=rn, column=1).fill = fill(_ID_FILL[cat])
    wsb.freeze_panes = "C2"
    wsb.auto_filter.ref = wsb.dimensions
    for ci, col_cells in enumerate(wsb.columns, 1):
        width = max((len(str(c.value)) for c in col_cells if c.value is not None), default=8)
        wsb.column_dimensions[get_column_letter(ci)].width = min(width + 2, 55)

    # ── Summary ──
    ws = wb.create_sheet("Summary", 0)
    title_font = Font(size=14, bold=True, color="FF1A365D")
    note_font = Font(size=9, italic=True, color="FF666666")
    r = [1]   # next row to write

    def put(values, font=None, cell_fill=None, pct_from=None):
        for ci, v in enumerate(values, 1):
            cell = ws.cell(row=r[0], column=ci, value=v)
            cell.font = font or st["normal_font"]
            if cell_fill:
                cell.fill = cell_fill
            if pct_from and ci >= pct_from and isinstance(v, float):
                cell.number_format = "0.0%"
        r[0] += 1

    def section(title, columns=()):
        r[0] += 1
        put([title] + list(columns), font=st["white_bold"], cell_fill=st["H1"])

    def kv(label, value):
        put([label, value])
        ws.cell(row=r[0] - 1, column=1).font = st["bold_font"]

    put(["BLAST results summary"], font=title_font)
    kv("Results table", os.path.basename(tsv_path))
    kv("Generated", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if run_info.get("run_id"):
        kv("Run ID", run_info["run_id"])
    if run_info.get("status"):
        kv("Run status", run_info["status"])

    if inputs:
        section("Input result files" if kind == "hit_table" else "Input FASTA files",
                [unit, "Path"])
        n_sum = 0
        for f, qs in inputs:
            n_sum += len(qs or [])
            put([os.path.basename(f), len(qs) if qs is not None else "file not found",
                 os.path.abspath(f)])
        put(["Total", n_sum], font=st["bold_font"])
        if n_total != n_sum:
            put([f"Unique {unit.lower()} in the run", n_total], font=st["bold_font"])
    else:
        section("Input")
        kv(f"{unit} in the table", len(best))

    params = run_info.get("params") or {}
    ref = params.get("tax_reference") or ""
    section("Taxonomy reference")
    kv("Reference file", os.path.basename(ref) if ref else "(none)")
    if ref:
        kv("Path", os.path.abspath(ref))
        kv("Sample-ID suffix removed", params.get("strip_suffix") or "(none)")

    if params.get("nhits"):
        section("Parameters")
        if kind == "hit_table":
            kv("Hits per query kept", params.get("nhits"))
        else:
            kv("Database", params.get("database", ""))
            kv("Program", params.get("program", ""))
            kv("Hits per sequence", params.get("nhits"))
            kv("Sequences per batch", params.get("nseq") or "Automatic")
        kv("NCBI taxonomy fetched", "Yes" if params.get("fetch_taxonomy", True) else "No")

    section("Results")
    kv("Hit rows in the table", len(rows))
    kv(f"{unit} with hits", len(best))
    kv(f"{unit} without hits", counts.get("no_hit", 0))
    if counts.get("not_searched"):
        kv(f"{unit} not searched", counts["not_searched"])
    if has_match and "Hit_rank" in col:
        kv("Best hit is not BLAST's first hit",
           sum(1 for row, _n in best.values()
               if str(row[col["Hit_rank"]]).strip() not in ("", "1")))

    section("Identification level", [unit, "%", "Cumulative %"])
    cum = 0
    for lv in levels:
        n = counts[lv]
        pct = n / n_total if n_total else 0.0
        if lv in _RANK_SCORE:
            cum += n
            cum_pct = cum / n_total if n_total else 0.0
        else:
            cum_pct = ""
        put([_ID_LABELS[lv], n, float(pct), cum_pct], pct_from=3)
        if lv in _ID_FILL:
            ws.cell(row=r[0] - 1, column=1).fill = fill(_ID_FILL[lv])
    put(["Total", n_total], font=st["bold_font"])
    if has_match:
        put(["Rank at which each query's best hit agrees with the reference taxonomy "
             "(Tax_level_match). Cumulative %: identified at that rank or deeper."],
            font=note_font)
    else:
        put(["No reference taxonomy applied: add a query taxonomy reference to get "
             "identification levels (organism, genus, family, order)."], font=note_font)
    put(["Best hit sheet: one row per query. Best hit = deepest rank agreeing with the "
         "reference; ties and 'none' by bit score, identity, fewest gaps and mismatches."],
        font=note_font)

    with_qs = [(f, qs) for f, qs in inputs if qs]
    if len(with_qs) > 1:
        section("Identification level by file",
                [os.path.basename(f) for f, _qs in with_qs] + ["Total"])
        for lv in levels:
            per = [sum(1 for q in set(qs) if cats.get(q) == lv) for _f, qs in with_qs]
            put([_ID_LABELS[lv]] + per + [counts[lv]])
            if lv in _ID_FILL:
                ws.cell(row=r[0] - 1, column=1).fill = fill(_ID_FILL[lv])
        put(["Total"] + [len(set(qs)) for _f, qs in with_qs] + [n_total],
            font=st["bold_font"])

    # Column A holds labels and the long notes (left to overflow); the rest
    # fit their values.
    ws.column_dimensions["A"].width = 38
    for ci in range(2, ws.max_column + 1):
        letter = get_column_letter(ci)
        width = max((len(str(c.value)) for c in ws[letter]
                     if c.value is not None and not isinstance(c.value, float)), default=8)
        ws.column_dimensions[letter].width = min(max(width + 2, 12), 60)

    # Open on the Summary. Only one tab may be selected, or Excel groups them.
    for sheet in wb.worksheets:
        sheet.sheet_view.tabSelected = False
    ws.sheet_view.tabSelected = True
    wb.active = 0


# ═══════════════════════════════════════════════════════════════════════════
# BLAST RESULT FILE DROP ZONE
# ═══════════════════════════════════════════════════════════════════════════

class _BlastFileDropZone(QtWidgets.QFrame):
    """Drop zone for one or more downloaded NCBI BLAST result files.

    Accepts the Hit Table exported from blast.ncbi.nlm.nih.gov after running a
    search on the NCBI website ('Download All' → 'Hit Table(text)' or
    'Hit Table(csv)'), so several jobs can be combined in one run.
    """

    filesDropped = QtCore.pyqtSignal(list)

    _SUPPORTED_EXT = (".txt", ".csv")
    _EMPTY_H = 160
    _ROW_H   = 40
    _CHROME  = 140

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drop_zone")
        self.setAcceptDrops(True)
        self.setFixedHeight(self._EMPTY_H)
        self._files: List[str] = []

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        self._lbl_src_empty  = "Drag/Add BLAST result files here (.txt, .csv)"
        self._lbl_src_filled = "Result files"
        self._lbl = make_label(self._lbl_src_empty, size=18, color=TEXT_SEC)
        self._lbl.setAlignment(QtCore.Qt.AlignCenter)
        self._lbl.setToolTip(
            "Drop the Hit Table exported from blast.ncbi.nlm.nih.gov after running\n"
            "a search on the NCBI website: 'Download All' → 'Hit Table(text)' or\n"
            "'Hit Table(csv)'. You can drop more than one file at a time."
        )

        self._files_container = QtWidgets.QWidget()
        self._files_layout = QtWidgets.QVBoxLayout(self._files_container)
        self._files_layout.setContentsMargins(0, 0, 0, 0)
        self._files_layout.setSpacing(6)
        self._files_container.hide()

        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.setSpacing(8)
        btn_layout.setAlignment(QtCore.Qt.AlignCenter)
        self._browse_btn = QtWidgets.QPushButton("Add")
        self._browse_btn.setObjectName("secondary_btn")
        self._browse_btn.setFixedWidth(130)
        self._browse_btn.clicked.connect(self._browse_files)
        self._clear_btn = QtWidgets.QPushButton("Clear")
        self._clear_btn.setObjectName("danger_btn")
        self._clear_btn.setFixedWidth(130)
        self._clear_btn.clicked.connect(self.clear)
        self._clear_btn.hide()
        btn_layout.addWidget(self._browse_btn)
        btn_layout.addWidget(self._clear_btn)

        _exp = QtWidgets.QSizePolicy
        self._top_spacer = QtWidgets.QSpacerItem(0, 0, _exp.Minimum, _exp.Expanding)
        self._bot_spacer = QtWidgets.QSpacerItem(0, 0, _exp.Minimum, _exp.Expanding)
        layout.addSpacerItem(self._top_spacer)
        layout.addWidget(self._lbl)
        layout.addWidget(self._files_container)
        layout.addLayout(btn_layout)
        layout.addSpacerItem(self._bot_spacer)

    @property
    def files(self):
        return self._files

    def retranslateUi(self):
        ctx = "BlastFileDropZone"
        self._browse_btn.setText(_tr(ctx, "Add"))
        self._clear_btn.setText(_tr(ctx, "Clear"))
        if not self._files:
            self._lbl.setText(_tr(ctx, self._lbl_src_empty))
        else:
            self._lbl.setText(
                f"{_tr(ctx, self._lbl_src_filled)} ({len(self._files)} file(s))"
            )

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def _create_file_row(self, filepath):
        row = QtWidgets.QWidget()
        row.setObjectName("file_row")
        row.setStyleSheet(f"""
            QWidget#file_row {{
                background-color: {GRAY_BG};
                border-radius: 7px;
                border: 1px solid {GRAY_LINE};
            }}
        """)
        row.setToolTip(filepath)
        rl = QtWidgets.QHBoxLayout(row)
        rl.setContentsMargins(12, 6, 10, 6)
        rl.setSpacing(10)
        icon = make_label("📄", size=15)
        icon.setFixedWidth(24)
        name = make_label(os.path.basename(filepath), size=15, color=TEXT_PRI)
        remove_btn = QtWidgets.QPushButton("✕")
        remove_btn.setFixedSize(26, 26)
        remove_btn.setToolTip("Remove file")
        remove_btn.setStyleSheet(f"""
            QPushButton {{ background-color: transparent; color:{TEXT_HINT};
                border:none; border-radius:5px; font-size:13px; font-weight:bold; }}
            QPushButton:hover {{ background-color:{RED_LT}; color:{RED}; }}
        """)
        remove_btn.clicked.connect(lambda checked=False, f=filepath: self._remove(f))
        rl.addWidget(icon)
        rl.addWidget(name, 1)
        rl.addWidget(remove_btn)
        return row

    def _update_display(self):
        n = len(self._files)
        while self._files_layout.count() > 0:
            item = self._files_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if n == 0:
            self._files_container.hide()
            self._clear_btn.hide()
            self._lbl.setText(_tr("BlastFileDropZone", self._lbl_src_empty))
            self.setFixedHeight(self._EMPTY_H)
            self.setProperty("filled", "false")
        else:
            for f in self._files:
                self._files_layout.addWidget(self._create_file_row(f))
            self._files_container.show()
            self._clear_btn.show()
            self._lbl.setText(
                f"{_tr('BlastFileDropZone', self._lbl_src_filled)} ({n} file(s))"
            )
            self.setFixedHeight(self._CHROME + n * self._ROW_H)
            self.setProperty("filled", "true")
        refresh_style(self)

    def _add_files(self, paths):
        added = 0
        for p in paths:
            if p not in self._files:
                self._files.append(p)
                added += 1
        if added:
            self._update_display()
            self.filesDropped.emit(self._files.copy())

    def _remove(self, filepath):
        if filepath in self._files:
            self._files.remove(filepath)
            self._update_display()
            self.filesDropped.emit(self._files.copy())

    def clear(self):
        self._files = []
        self._update_display()
        self.filesDropped.emit([])

    def _browse_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, _tr("BlastFileDropZone", "Select BLAST result files"), "",
            "BLAST hit table (*.txt *.csv);;All (*)"
        )
        if files:
            self._add_files(files)

    def _dropped_paths(self, mime) -> List[str]:
        paths = [u.toLocalFile() for u in mime.urls()]
        return [p for p in paths
                if p and p.lower().endswith(self._SUPPORTED_EXT)]

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setProperty("dragging", "true")
            refresh_style(self)

    def dragLeaveEvent(self, e):
        self.setProperty("dragging", "false")
        refresh_style(self)

    def dropEvent(self, e):
        self.setProperty("dragging", "false")
        refresh_style(self)
        paths = self._dropped_paths(e.mimeData())
        if paths:
            self._add_files(paths)


def hit_table_queries(path: str) -> List[str]:
    """Query IDs (first column) of an NCBI 'Hit Table' export (.txt / .csv)."""
    def load(p):
        out = []
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                sep = "\t" if "\t" in line else ","
                out.append(line.split(sep, 1)[0].strip())
        return out
    try:
        return _cached(path, "hitq", load)
    except Exception:
        return []


class _ReferenceFileGroup:
    """The optional 'query taxonomy reference file' control: a checkbox, a
    _RefDropZone and a description/validation label, together with the
    toggle/describe/validate logic that goes with them.

    Both BLAST tabs offer this exact feature (add Query_Order/Query_Family/
    Query_Genus/Query_organism from a reference list, the same way Best
    Sequence does), so it is built once here and used twice — one instance
    per tab — instead of duplicating the widgets and their logic in each.
    """

    def __init__(self):
        self.check = QtWidgets.QCheckBox(
            "I have a reference file with the query taxonomy")
        self.check.setToolTip(
            "Adds Query_Order / Query_Family / Query_Genus / Query_organism to the\n"
            "results table from a single reference list, keyed by the sample ID\n"
            "(the text before the first ';' in the query name)."
        )
        self.check.toggled.connect(self._on_toggled)

        # Link to the stand-alone "apply to an existing table" tool (see
        # _ApplyReferenceDialog). Shown regardless of the checkbox above: it
        # opens its own dialog with its own reference-file picker, so it does
        # not depend on this group's state. Styled as a link (flat, blue,
        # underline on hover) rather than a boxed button, so it reads as a
        # secondary action next to the checkbox instead of competing with it.
        self.apply_link = QtWidgets.QPushButton("Apply reference taxonomy to a results file…")
        self.apply_link.setToolTip(
            "Add or update Query_Order / Query_Family / Query_Genus / Query_organism\n"
            "(and Tax_level_match) on an existing BLAST results table — no new search\n"
            "is submitted. Use this after fixing a reference file or its sample-ID\n"
            "suffix, or when the reference file wasn't ready when the search ran."
        )
        self.apply_link.setCursor(QtCore.Qt.PointingHandCursor)
        self.apply_link.setFlat(True)
        self.apply_link.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                color: {BLUE};
                font-size: 14px;
                text-align: left;
                padding: 0px;
            }}
            QPushButton:hover {{ color: {BLUE_MID}; text-decoration: underline; }}
        """)

        self.zone = _RefDropZone()
        self.zone.fileChanged.connect(self._on_file_changed)
        self.zone.hide()

        # Matches Best Sequence's own "Strip suffix from sample ID" field: the
        # sample ID is the text before the first ';' in Query_name, and this
        # suffix (if present) is removed from it before the reference lookup,
        # e.g. DNS-1343_all.fa;758;807 -> DNS-1343.
        self.suffix_label = QtWidgets.QLabel("Strip suffix from sample ID:")
        self.suffix_edit = QtWidgets.QLineEdit("_all.fa")
        self.suffix_edit.setFixedWidth(300)
        self.suffix_edit.setPlaceholderText("suffix stripped from the sample ID (optional)")
        self.suffix_edit.setToolTip(
            "The sample ID is the text before the first ';' in the query name.\n"
            "This suffix is removed from it, e.g. DNS-1343_all.fa;758;807 → DNS-1343."
        )
        self.suffix_label.hide()
        self.suffix_edit.hide()

        self.info_label = QtWidgets.QLabel("")
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet(f"color:{TEXT_HINT}; font-size:14px;")
        self.info_label.hide()

        self.ok = False
        self.found = None             # sample IDs found in the reference (None: unknown)
        self._names_provider = None   # () -> query names to check against it
        self.suffix_edit.textChanged.connect(lambda _t: self._describe())
        self._describe()   # seed the hint text shown before any file is set

    def add_to(self, form: QtWidgets.QFormLayout):
        """Add the rows to a QFormLayout, in the order they're meant to appear."""
        form.addRow(self.check)
        form.addRow(self.apply_link)
        form.addRow(self.zone)
        form.addRow(self.suffix_label, self.suffix_edit)
        form.addRow(self.info_label)

    @property
    def checked(self) -> bool:
        return self.check.isChecked()

    @property
    def path(self) -> str:
        """The reference file path, or '' when the checkbox is off."""
        return self.zone.path if self.checked else ""

    @property
    def suffix(self) -> str:
        """The suffix to strip from the sample ID, or '' when the checkbox is off."""
        return self.suffix_edit.text().strip() if self.checked else ""

    def set_names_provider(self, provider):
        """provider() -> the query names (FASTA headers / Query_name values)
        of the data the reference will be applied to, for the match check."""
        self._names_provider = provider
        self._describe()

    def refresh_check(self):
        """Re-run the match check after the data files changed."""
        self._describe()

    def _on_toggled(self, checked: bool):
        self.zone.setVisible(checked)
        self.suffix_label.setVisible(checked)
        self.suffix_edit.setVisible(checked)
        self.info_label.setVisible(checked)
        self._describe()

    def _on_file_changed(self, _path: str):
        self._describe()

    def _describe(self):
        """Validate the reference file and describe how it will be read."""
        self.ok = False
        self.found = None
        path = self.zone.path
        if not path:
            self.info_label.setText(
                "The identifier must be the same sample ID as in the query name "
                "(the text before the first ';'). The <b>4 columns following it</b> "
                "are read, in this order, as <b>Order, Family, Genus and Organism</b>."
            )
            self.info_label.setStyleSheet(f"color:{TEXT_HINT}; font-size:14px;")
            return
        try:
            id_col, tax_cols, table = read_tax_reference_cached(path)
        except Exception as exc:
            self.info_label.setText(f"⚠  {exc}")
            self.info_label.setStyleSheet(f"color:{RED}; font-size:14px;")
            return
        self.ok = True
        check = ""
        if self.checked and self._names_provider is not None:
            try:
                names = self._names_provider() or []
            except Exception:
                names = []
            suffix = self.suffix_edit.text().strip()
            check, colour, found, total = reference_match_check(
                [sample_id_of(n, suffix) for n in names], table)
            if total:
                self.found = found
        self.info_label.setText(
            f"{len(table):,} entries  ·  identifier: <b>{id_col}</b>  ·  "
            f"<b>{' · '.join(tax_cols)}</b> → Query_Order · Query_Family · "
            f"Query_Genus · Query_organism.<br>"
            f"These columns will be added to the results table."
            + (f"<br><span style='color:{colour}'><b>{check}</b></span>" if check else "")
        )
        self.info_label.setStyleSheet(f"color:{TEXT_HINT}; font-size:14px;")

    def validate_or_warn(self, parent: QtWidgets.QWidget) -> bool:
        """True if the run may proceed; warns and returns False when the
        checkbox is on but no valid reference file has been supplied."""
        if self.checked and not self.ok:
            QtWidgets.QMessageBox.warning(
                parent, "Reference file",
                "Add a valid query taxonomy reference file, or uncheck "
                "'I have a reference file with the query taxonomy'."
            )
            return False
        self._describe()   # the data may have changed since the last check
        if self.checked and self.found == 0:
            return QtWidgets.QMessageBox.question(
                parent, "Reference file",
                "No sample ID of the input was found in the reference file, so "
                "no query taxonomy would be written (see the reference check).\n\n"
                "Continue anyway?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) == QtWidgets.QMessageBox.Yes
        return True

    def retranslateUi(self, ctx: str):
        self.check.setText(_tr(ctx, "I have a reference file with the query taxonomy"))
        self.apply_link.setText(_tr(ctx, "Apply reference taxonomy to a results file…"))
        self.suffix_label.setText(_tr(ctx, "Strip suffix from sample ID:"))
        self.zone.retranslateUi()
        self._describe()


class _ResultsDropZone(_RefDropZone):
    """A BLAST results table (.tsv/.csv) drop zone — same drag-and-drop zone
    and "Add"/"Clear" button styling as _RefDropZone (consistent with the rest
    of the app), just pointed at a different file kind and dialog text."""

    _HINT = "Drag the BLAST results table here  (.tsv, .csv)"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tip = (
            "The BLAST results table to update, in place (.tsv or .csv) — the\n"
            "one produced by a previous BLAST run. Must have a 'Query_name' column."
        )
        self._render()

    def _browse(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select a BLAST results table", "",
            "BLAST results (*.tsv *.csv);;All files (*)"
        )
        if f:
            self.set_path(f)

    def _dropped_paths(self, mime) -> List[str]:
        paths = [u.toLocalFile() for u in mime.urls()]
        return [p for p in paths if p.lower().endswith((".tsv", ".csv"))]


class _ApplyReferenceDialog(QtWidgets.QDialog):
    """Stand-alone tool: (re-)apply a query-taxonomy reference file to an
    already-generated BLAST results table, in place — no BLAST search is run.

    Exists for the two situations that leave a finished results table without
    query taxonomy even though a reference file exists: the reference wasn't
    ready when the search ran, or the match needed a sample-ID suffix that
    wasn't set at the time. Either way, re-submitting the whole search just to
    fix the taxonomy columns is wasteful (and, for a large run, slow and
    rate-limited) — this reruns only the local, offline matching step.
    """

    # A word-wrapped QLabel's sizeHint() is its *unwrapped* single-line width —
    # wrapping only affects heightForWidth, which plain sizeHint() ignores. Left
    # uncapped, a label whose text changes at runtime (the status line, the
    # reference info line) balloons the layout's preferred width every time it
    # gets a longer message, and adjustSize()/the layout pass then squeezes
    # other rows to compensate instead of growing the window sanely. Every
    # word-wrapped label in this dialog is capped at this width so their
    # sizeHint reflects their real wrapped height instead.
    _WRAP_WIDTH = 560

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Apply query taxonomy to a results file")
        self.setMinimumWidth(600)

        v = QtWidgets.QVBoxLayout(self)
        v.setSpacing(12)

        desc = make_label(
            "Adds or updates Query_Order, Query_Family, Query_Genus and "
            "Query_organism (and Tax_level_match, if the table already has "
            "hit taxonomy) on an existing BLAST results table. The table is "
            "edited in place; run this again after fixing the reference file "
            "or the sample-ID suffix below.",
            color=TEXT_SEC
        )
        desc.setWordWrap(True)
        desc.setMaximumWidth(self._WRAP_WIDTH)
        v.addWidget(desc)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        form.setSpacing(10)

        self._tsv_zone = _ResultsDropZone()
        self._tsv_zone.fileChanged.connect(self._on_changed)
        form.addRow("Results table:", self._tsv_zone)
        v.addLayout(form)

        self._ref_group = _ReferenceFileGroup()
        self._ref_group.check.setChecked(True)
        self._ref_group.check.hide()        # always "on" in this dialog
        self._ref_group.apply_link.hide()   # this dialog IS that link's destination
        # The reference block's content (info_label text, in particular) changes
        # height when a file is added/removed. A QDialog does not auto-resize to
        # fit content that grows *after* it was first shown, which was clipping
        # the Add/Clear button labels and the info text below them — resize
        # after every such change instead of relying on the initial layout pass.
        self._ref_group.zone.fileChanged.connect(self._on_changed)
        self._ref_group.info_label.setMaximumWidth(self._WRAP_WIDTH)
        ref_form = QtWidgets.QFormLayout()
        ref_form.setLabelAlignment(QtCore.Qt.AlignRight)
        ref_form.setSpacing(10)
        self._ref_group.add_to(ref_form)
        self._ref_group.set_names_provider(
            lambda: table_column(self._tsv_zone.path, "Query_name")
            if self._tsv_zone.path else [])
        v.addLayout(ref_form)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        self._status.setMaximumWidth(self._WRAP_WIDTH)
        v.addWidget(self._status)

        v.addStretch(1)
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.setObjectName("secondary_btn")
        close_btn.clicked.connect(self.reject)
        btns.addWidget(close_btn)
        self._apply_btn = QtWidgets.QPushButton("Apply  →")
        self._apply_btn.setObjectName("primary_btn")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._apply)
        btns.addWidget(self._apply_btn)
        v.addLayout(btns)

    def _on_changed(self, *_args):
        self._ref_group.refresh_check()
        self._apply_btn.setEnabled(bool(self._tsv_zone.path))
        self._set_status("")

    def _set_status(self, text: str, color: str = ""):
        self._status.setText(text)
        self._status.setStyleSheet(f"color:{color};" if color else "")
        # The status text's own height (0, 1 or several wrapped lines) is part
        # of what the dialog must fit. Without this, a longer message after
        # Apply — or a shorter one after clearing it — left the window sized
        # for whatever it last was, clipping content instead of growing, or
        # leaving dead space instead of shrinking. Deferred so it runs after
        # the layout has recomputed for the new text.
        QtCore.QTimer.singleShot(0, self.adjustSize)

    def _set_busy(self, busy: bool):
        """Make the run visibly in progress while _apply() does its (blocking,
        UI-thread) work on a large table. The default #primary_btn :pressed
        tint is too close to its normal color to notice, and — since a click
        is only ever momentarily "pressed" — it does not even stay visible for
        work that takes a second or more; a disabled button with changed text
        plus a wait cursor is unambiguous regardless of how long it takes.
        """
        self._apply_btn.setEnabled(not busy and bool(self._tsv_zone.path))
        self._apply_btn.setText("Applying…" if busy else "Apply  →")
        if busy:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        else:
            QtWidgets.QApplication.restoreOverrideCursor()
        # Force the repaint now: the work below runs on this same thread and
        # would otherwise keep the old button/cursor on screen until it's done.
        QtWidgets.QApplication.processEvents()

    def _apply(self):
        tsv_path = self._tsv_zone.path
        if not tsv_path or not os.path.isfile(tsv_path):
            self._set_status("⚠  Pick an existing results file first.", RED)
            return
        if not self._ref_group.validate_or_warn(self):
            return
        ref_path = self._ref_group.path
        if not ref_path:
            self._set_status("⚠  Add a query taxonomy reference file first.", RED)
            return

        self._set_busy(True)
        try:
            try:
                _id_col, _tax_cols, ref_table = read_tax_reference(ref_path)
                ref_lower = {k.lower(): v for k, v in ref_table.items()}
                n_rows, n_filled, unknown, n_match = apply_reference_and_tax_match(
                    tsv_path, ref_table, ref_lower, self._ref_group.suffix
                )
            except Exception as exc:
                self._set_status(f"⚠  {exc}", RED)
                return
            msg = f"✓  {n_filled}/{n_rows} row(s) matched to the reference and written to the file."
            if unknown:
                examples = ", ".join(sorted(unknown)[:5])
                msg += f"  {len(unknown)} sample ID(s) not found in the reference (e.g. {examples})."
            suffix = self._ref_group.suffix
            empty = reference_empty_ids(
                [sample_id_of(q, suffix) for q in table_column(tsv_path, "Query_name")],
                ref_table)
            if empty:
                msg += (f"  {len(empty)} sample ID(s) in the reference with empty "
                        f"taxonomy (e.g. {', '.join(empty[:5])}).")
            if n_match >= 0:
                msg += f"  Tax_level_match updated on {n_match} row(s)."
            _fmt = reference_format_warning(ref_table)
            if _fmt:
                msg += "  ⚠ " + _fmt

            # Regenerate the matching .xlsx from the just-updated table, overwriting
            # whichever one (if any) sits next to it — otherwise it would keep
            # showing the pre-fix taxonomy even though the .tsv is now correct.
            try:
                xlsx_path = build_xlsx_from_tsv(tsv_path, params_override={
                    "tax_reference": ref_path, "strip_suffix": suffix})
                if xlsx_path:
                    msg += f"  {os.path.basename(xlsx_path)} updated."
            except Exception as exc:
                msg += f"  ⚠ .xlsx not updated: {exc}"

            self._set_status(msg, GREEN)
        finally:
            self._set_busy(False)


class _FullWidthTabBar(QtWidgets.QTabBar):
    """Tab bar that always splits the full width of the tab widget evenly
    across its tabs, instead of leaving them hugging their own text."""

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        bar_width = self.width()
        if bar_width <= 0:
            return size
        count = max(self.count(), 1)
        return QtCore.QSize(bar_width // count, size.height())


class _TwoLineTabBar(_FullWidthTabBar):
    """Tabs whose text is "title\nsubtitle", with the title TITLE_GROW
    px larger than the subtitle.

    A tab's text has one font, and the style sheet's ::tab font-size wins over
    any font set on the painter. So both lines are drawn through the style at
    the sheet's size (SUB_PX) — which keeps their colour following the tab
    states (selected, hover) — and the title is drawn scaled up. The sheet
    must give ::tab font-size: SUB_PX and no vertical padding.
    """
    SUB_PX     = 15
    TITLE_GROW = 4

    def paintEvent(self, event):
        painter = QtWidgets.QStylePainter(self)
        font = QtGui.QFont(self.font())
        font.setPixelSize(self.SUB_PX)
        line_h = QtGui.QFontMetrics(font).height()
        scale = (self.SUB_PX + self.TITLE_GROW) / self.SUB_PX
        for i in range(self.count()):
            opt = QtWidgets.QStyleOptionTab()
            self.initStyleOption(opt, i)
            title, _, sub = opt.text.partition("\n")
            opt.text = ""
            painter.drawControl(QtWidgets.QStyle.CE_TabBarTabShape, opt)
            lines = [(title, scale)] + ([(sub, 1.0)] if sub else [])
            heights = [round(line_h * k) for _t, k in lines]
            y = opt.rect.center().y() - sum(heights) // 2 + 1
            cx = opt.rect.center().x()
            for (text, k), h in zip(lines, heights):
                cy = y + h // 2
                line = QtWidgets.QStyleOptionTab(opt)
                line.text = text
                # The style sheet stretches the label rect back to the tab's
                # min-height and centres the text in it, so pass a full-height
                # rect centred on this line (in the scaled coordinates).
                line.rect = opt.rect.translated(0, cy - opt.rect.center().y())
                painter.save()
                painter.translate(cx, cy)
                painter.scale(k, k)
                painter.translate(-cx, -cy)
                painter.drawControl(QtWidgets.QStyle.CE_TabBarTabLabel, line)
                painter.restore()
                y += h


class _FullWidthTabWidget(QtWidgets.QTabWidget):
    """QTabWidget whose tab bar is forced to the widget's own width on every
    resize, so _FullWidthTabBar has the full width to split across tabs
    (QTabWidget otherwise only ever sizes the bar to its tabs' own hints)."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        bar = self.tabBar()
        bar.resize(self.width(), bar.sizeHint().height())


# ═══════════════════════════════════════════════════════════════════════════
# BLAST PANEL
# ═══════════════════════════════════════════════════════════════════════════

class BlastPanel(QtWidgets.QWidget):
    blastRequested     = QtCore.pyqtSignal(list, dict)   # files, config dict
    stopRequested      = QtCore.pyqtSignal()             # user clicked Stop (tab 1)
    resumeRequested    = QtCore.pyqtSignal(str, dict)    # run's .state.json, config (API key, param_changes)
    blastFileRequested = QtCore.pyqtSignal(list, dict)   # result files, config dict (tab 2)
    stopFileRequested  = QtCore.pyqtSignal()             # user clicked Stop (tab 2)
    sendToBestSeq      = QtCore.pyqtSignal(list)         # [fasta, results] pair of the last run

    # (title, subtitle) of each tab
    _TAB_TEXTS = (
        ("BLAST API Search", "Send your FASTA to NCBI from here"),
        ("BLAST web results", "Use a Hit Table downloaded from the NCBI website"),
    )

    def _tab_text(self, index: int, ctx: str = "BlastPanel") -> str:
        title, sub = self._TAB_TEXTS[index]
        return f"{_tr(ctx, title)}\n{_tr(ctx, sub)}"

    _DATABASES        = ["core_nt", "nt", "refseq_rna", "16S_ribosomal_RNA"]
    _PROGRAMS         = ["blastn&MEGABLAST=on", "blastn", "megablast"]
    _PROGRAM_LABELS   = ["blastn + MEGABLAST (recommended)", "blastn", "megablast"]

    # Live-log slot keys (fixed lines, updated in place)
    _SLOT_KEYS = ("info", "blast", "organism", "taxonomy", "progress", "result")
    # Same shape for tab 2 (file parsing), "blast" slot relabelled to "parse"
    _FILE_SLOT_KEYS = ("info", "parse", "organism", "taxonomy", "progress", "result")

    def __init__(self, parent=None):
        super().__init__(parent)

        # Page 1 ("BLAST API Search") holds everything this panel used to be,
        # unchanged, so behaviour and layout for the live NCBI search stay
        # identical to before the tab was introduced.
        self._page1 = QtWidgets.QWidget()
        outer_layout = QtWidgets.QVBoxLayout(self._page1)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

        self._inner = QtWidgets.QWidget()
        self._layout = QtWidgets.QVBoxLayout(self._inner)
        self._layout.setContentsMargins(20, 20, 20, 8)
        self._layout.setSpacing(14)
        scroll.setWidget(self._inner)
        outer_layout.addWidget(scroll, 0)   # scroll takes only what content needs

        # ── Title + description ──
        self._lbl_title = make_label("BLAST API Search", size=19, bold=True)
        self._lbl_desc = make_label(
            "BLAST sequences in NCBI. "
            "Drag-and-drop one or more FASTA files (.fa, .fas, .fasta).\n"
            "Optional: results might include organism and taxonomic classification from NCBI Taxonomy.\n"
            "The queried sequences are saved as a FASTA named after the results, "
            "ready to use as a pair in Best Sequence.",
            color=TEXT_SEC
        )
        self._lbl_desc.setWordWrap(True)
        self._layout.addWidget(self._lbl_title)
        self._layout.addWidget(self._lbl_desc)

        # ── Settings group ──
        self._settings_box = QtWidgets.QGroupBox("BLAST Settings")
        self._settings_box.setStyleSheet(group_box_style())
        sg = QtWidgets.QFormLayout(self._settings_box)
        sg.setLabelAlignment(QtCore.Qt.AlignRight)
        sg.setSpacing(10)
        sg.setContentsMargins(16, 16, 16, 16)

        self._lbl_api = QtWidgets.QLabel("NCBI API Key:")
        api_row = QtWidgets.QWidget()
        al = QtWidgets.QHBoxLayout(api_row)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(4)
        self._api_key_edit = QtWidgets.QLineEdit()
        self._api_key_edit.setPlaceholderText("Paste your NCBI API key here…")
        self._api_key_edit.setMinimumWidth(500)
        self._api_key_edit.setText(self._load_api_key())
        self._api_key_edit.editingFinished.connect(self._save_api_key)
        self._api_key_edit.textChanged.connect(self._on_api_key_changed)
        # Masked by default so the key does not leak into screenshots/recordings
        self._api_key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        al.addWidget(self._api_key_edit)
        self._api_key_show_btn = QtWidgets.QPushButton("Show")
        self._api_key_show_btn.setCheckable(True)
        self._api_key_show_btn.setFixedSize(64, 28)
        self._api_key_show_btn.setToolTip("Show / hide API key")
        self._api_key_show_btn.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid #CCC;"
            " border-radius: 4px; color: #555; font-size:13px; }"
            "QPushButton:checked { background: #E6F1FB; border-color: #378ADD; }"
        )

        def _toggle_key_visibility(on):
            self._api_key_edit.setEchoMode(
                QtWidgets.QLineEdit.Normal if on else QtWidgets.QLineEdit.Password)
            self._api_key_show_btn.setText("Hide" if on else "Show")
        self._api_key_show_btn.toggled.connect(_toggle_key_visibility)
        al.addWidget(self._api_key_show_btn)
        self._api_key_clear_btn = QtWidgets.QPushButton("✕")
        self._api_key_clear_btn.setFixedSize(28, 28)
        self._api_key_clear_btn.setToolTip("Clear API key")
        self._api_key_clear_btn.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid #CCC;"
            " border-radius: 4px; color: #888; font-size:11px; }"
            "QPushButton:hover { background: #FEE2E2; border-color: #EF4444; color: #EF4444; }"
        )
        self._api_key_clear_btn.clicked.connect(self._clear_api_key)
        al.addWidget(self._api_key_clear_btn)
        al.addStretch()
        sg.addRow(self._lbl_api, api_row)

        self._lbl_api_warn = QtWidgets.QLabel(
            "<table cellspacing='0' cellpadding='0'><tr>"
            "<td valign='top'>⚠&nbsp;&nbsp;</td>"
            "<td>A personal NCBI API key is required — key is saved automatically.<br>"
            "Register free at: "
            "<a href='https://www.ncbi.nlm.nih.gov/account' "
            "style='color:#185FA5;'>ncbi.nlm.nih.gov/account</a></td>"
            "</tr></table>"
        )
        self._lbl_api_warn.setWordWrap(True)
        self._lbl_api_warn.setOpenExternalLinks(True)
        self._lbl_api_warn.setStyleSheet("color:#B45309; font-size:16px;")
        sg.addRow("", self._lbl_api_warn)
        self._lbl_api_warn.setVisible(not bool(self._api_key_edit.text().strip()))

        self._db_combo = QtWidgets.QComboBox()
        for db in self._DATABASES:
            self._db_combo.addItem(db)
        self._db_combo.setFixedWidth(500)
        self._lbl_db = QtWidgets.QLabel("Database:")
        sg.addRow(self._lbl_db, self._db_combo)

        self._prog_combo = QtWidgets.QComboBox()
        for lbl in self._PROGRAM_LABELS:
            self._prog_combo.addItem(lbl)
        self._prog_combo.setFixedWidth(500)
        self._lbl_prog = QtWidgets.QLabel("Program:")
        sg.addRow(self._lbl_prog, self._prog_combo)

        hits_row = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(hits_row)
        hl.setContentsMargins(0, 0, 0, 0)
        self._hits_spin = QtWidgets.QSpinBox()
        self._hits_spin.setRange(1, 100)
        self._hits_spin.setValue(5)
        self._hits_spin.setFixedWidth(80)
        hl.addWidget(self._hits_spin)
        hl.addStretch()
        self._lbl_hits = QtWidgets.QLabel("Hits per sequence (1–100):")
        sg.addRow(self._lbl_hits, hits_row)

        batch_row = QtWidgets.QWidget()
        bl2 = QtWidgets.QHBoxLayout(batch_row)
        bl2.setContentsMargins(0, 0, 0, 0)
        bl2.setSpacing(6)
        # Two ways to size a batch. Automatic is the default because the good
        # setting depends on the data (NCBI's ceiling is total bases, not a
        # sequence count) and because too many small searches is what gets an
        # IP moved to the slow queue.
        self._batch_mode = QtWidgets.QComboBox()
        self._batch_mode.addItems(["Automatic", "Manual"])
        self._batch_mode.setFixedWidth(140)
        self._batch_mode.currentIndexChanged.connect(self._on_batch_mode)
        bl2.addWidget(self._batch_mode)

        self._batch_spin = QtWidgets.QSpinBox()
        # NCBI's documented limit is total query length (1,000,000 bases for
        # blastn), not a sequence count — but in practice its undocumented
        # CPU-time budget for a MEGABLAST job against core_nt rejects a batch
        # well before that: 500 and 250 sequences/batch both failed in testing
        # and 100 was the first size that worked reliably. The default below
        # is half that: on a busy public queue 100-sequence searches can still
        # wait for most of the poll budget, and smaller ones return sooner.
        # The worker also auto-splits a batch NCBI still rejects at run time.
        self._batch_spin.setRange(1, 1000)
        self._batch_spin.setValue(50)
        self._batch_spin.setFixedWidth(80)
        self._batch_spin.valueChanged.connect(self._update_batch_plan)
        bl2.addWidget(self._batch_spin)
        # A tooltip is the wrong place for the full NCBI policy: it gets clipped
        # and cannot be read at leisure. Keep the hint to one line and put the
        # detail behind a click.
        self._ncbi_warn_icon = QtWidgets.QPushButton("⚠")
        self._ncbi_warn_icon.setFlat(True)
        self._ncbi_warn_icon.setFixedSize(26, 26)
        self._ncbi_warn_icon.setCursor(QtCore.Qt.PointingHandCursor)
        self._ncbi_warn_icon.setStyleSheet(
            "QPushButton { color:#B45309; font-size:17px; border:none;"
            " background:transparent; }"
            "QPushButton:hover { color:#92400E; background:#FEF3C7;"
            " border-radius:13px; }"
        )
        self._ncbi_warn_icon.setToolTip(
            "NCBI usage policy — click to read")
        self._ncbi_warn_icon.clicked.connect(self._show_ncbi_policy)
        bl2.addWidget(self._ncbi_warn_icon)
        bl2.addStretch()
        self._lbl_batch = QtWidgets.QLabel("Sequences per BLAST search:")
        sg.addRow(self._lbl_batch, batch_row)

        self._lbl_plan = QtWidgets.QLabel("")
        self._lbl_plan.setWordWrap(True)
        self._lbl_plan.setStyleSheet(f"color:{TEXT_HINT}; font-size:14px;")
        sg.addRow("", self._lbl_plan)

        # Repeated headers among the loaded FASTA files (see _update_repeat_note).
        self._lbl_repeats = QtWidgets.QLabel("")
        self._lbl_repeats.setWordWrap(True)
        self._lbl_repeats.setStyleSheet("color:#B45309; font-size:14px;")
        self._lbl_repeats.hide()
        sg.addRow("", self._lbl_repeats)


        self._tax_check = QtWidgets.QCheckBox("Fetch organism + taxonomic classification")
        self._tax_check.setChecked(True)
        self._lbl_tax = QtWidgets.QLabel("Taxonomy lookup:")
        sg.addRow(self._lbl_tax, self._tax_check)

        # ── Optional query-taxonomy reference file ──
        # Adds Query_Order/Query_Family/Query_Genus/Query_organism to every hit
        # row, exactly like the Best Sequence utility does for its own tables.
        self._ref_group = _ReferenceFileGroup()
        self._ref_group.apply_link.clicked.connect(self._open_apply_reference_dialog)
        self._ref_group.add_to(sg)
        self._ref_group.set_names_provider(
            lambda: [h.replace(" ", "_")
                     for f in self._drop.files for h in fasta_headers(f)])

        self._layout.addWidget(self._settings_box)

        # ── Drop zone: above the settings (load → configure → run), as in
        # the other panels; the reference check below reads these files ──
        self._drop = MultiDropZone()
        self._drop.filesDropped.connect(self._on_files)
        self._layout.insertWidget(self._layout.indexOf(self._settings_box), self._drop)
        self._layout.addStretch()   # packs content at top; log lives outside the scroll

        # ── Live progress display (outside scroll so it expands to fill space) ──
        self._log_slots = {k: "" for k in self._SLOT_KEYS}
        self._log = QtWidgets.QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QtGui.QFont("Consolas", 9))
        self._log.setMinimumHeight(200)
        self._log.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        self._log.setStyleSheet(
            f"QPlainTextEdit {{ background:{GRAY_BG}; border:1px solid {GRAY_LINE}; "
            f"border-radius:6px; padding:6px; color:{TEXT_PRI}; margin:0 20px 8px 20px; "
            f"font-family:'Consolas','Courier New',monospace; }}"
        )
        self._log.hide()
        outer_layout.addWidget(self._log, 0)   # stretch=0: fixed height (~30% of panel, min 200px)

        # ── Elapsed-time timer ──
        self._info_base   = ""    # static part of the info line (without time)
        self._start_time  = 0.0
        self._elapsed_timer = QtCore.QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        # ── Footer ──
        footer = QtWidgets.QWidget()
        footer.setObjectName("blast_footer")
        footer.setStyleSheet(f"""
            QWidget#blast_footer {{
                background: {GRAY_CARD};
                border-top: 1px solid {GRAY_LINE};
            }}
        """)
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(20, 10, 20, 10)

        self._clear_btn = QtWidgets.QPushButton("Clear")
        self._clear_btn.setObjectName("danger_btn")
        self._clear_btn.setFixedHeight(44)
        self._clear_btn.setFixedWidth(140)
        self._clear_btn.clicked.connect(self._reset)
        fl.addWidget(self._clear_btn)

        self._open_folder_btn = QtWidgets.QPushButton("Open folder")
        self._open_folder_btn.setObjectName("secondary_btn")
        self._open_folder_btn.setFixedHeight(44)
        self._open_folder_btn.hide()
        self._open_folder_btn.clicked.connect(self._open_output_folder)
        fl.addWidget(self._open_folder_btn)

        # Hands the queried FASTA + its results table to Best Sequence as a
        # ready-made pair (same base name), so nothing has to be re-selected.
        self._send_best_btn = QtWidgets.QPushButton("Open in Best Sequence  →")
        self._send_best_btn.setObjectName("secondary_btn")
        self._send_best_btn.setFixedHeight(44)
        self._send_best_btn.setToolTip(
            "Load the queried FASTA and this BLAST table as a pair in Best Sequence.")
        self._send_best_btn.hide()
        self._send_best_btn.clicked.connect(
            lambda: self.sendToBestSeq.emit(list(self._best_seq_pair)))
        fl.addWidget(self._send_best_btn)
        self._best_seq_pair = []

        self._stop_btn = QtWidgets.QPushButton("Stop")
        self._stop_btn.setObjectName("danger_btn")
        self._stop_btn.setFixedHeight(44)
        self._stop_btn.setFixedWidth(120)
        self._stop_btn.hide()
        self._stop_btn.clicked.connect(self.stopRequested)
        fl.addWidget(self._stop_btn)

        fl.addStretch()

        # Continues a stopped or incomplete run in its own files (see
        # _BlastWorker._load_resume), so its results end up in one table.
        self._resume_btn = QtWidgets.QPushButton("Resume run…")
        self._resume_btn.setObjectName("secondary_btn")
        self._resume_btn.setFixedHeight(44)
        self._resume_btn.setToolTip(
            "Continue a stopped or incomplete BLAST run: only its missing sequences\n"
            "are searched and their hits are added to the same results table.\n"
            "Pick the blast-<date>.state.json in the run's folder.")
        self._resume_btn.clicked.connect(self._emit_resume)
        fl.addWidget(self._resume_btn)
        fl.addSpacing(8)

        self._blast_btn = QtWidgets.QPushButton("Run BLAST  →")
        self._blast_btn.setObjectName("primary_btn")
        self._blast_btn.setFixedHeight(44)
        # Up to 300 px, but gives way down to 200 px so a narrow window
        # does not clip it once the after-run buttons are shown.
        self._blast_btn.setMinimumWidth(200)
        self._blast_btn.setMaximumWidth(300)
        self._blast_btn.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self._blast_btn.setEnabled(False)
        self._blast_btn.clicked.connect(self._emit_blast)
        self._blast_btn.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )
        fl.addWidget(self._blast_btn, 1)   # takes free space first, up to its 300 px
        outer_layout.addWidget(footer)

        # path -> ((mtime, size), [lengths]) so the plan refreshes only when
        # a file actually changes on disk.
        self._len_cache: dict = {}
        self._on_batch_mode(self._batch_mode.currentIndex())

        self._last_outdir = ""
        self._last_tsv    = ""

        # ── Page 2: parse local NCBI web-BLAST result files ──
        self._page2 = self._build_file_tab()

        self._tabs = _FullWidthTabWidget()
        self._tabs.setTabBar(_TwoLineTabBar())
        self._tabs.addTab(self._page1, self._tab_text(0))
        self._tabs.addTab(self._page2, self._tab_text(1))
        # Card-style tabs with a one-line subtitle: a plain text tab next to
        # the active one was easy to miss, and with it the whole second way
        # of getting results (the NCBI website's Hit Table).
        self._tabs.tabBar().setStyleSheet(f"""
            QTabBar::tab {{
                background: #EDEDED; color: {TEXT_SEC};
                font-size: {_TwoLineTabBar.SUB_PX}px; font-weight: 600;
                min-height: 65px; padding: 0 14px; margin: 0 6px 0 0;
                border: 1px solid {GRAY_LINE}; border-bottom: none;
                border-top-left-radius: 10px; border-top-right-radius: 10px;
            }}
            QTabBar::tab:last {{ margin-right: 0; }}
            QTabBar::tab:selected {{
                background: {BLUE}; color: white; border-color: {BLUE};
            }}
            QTabBar::tab:!selected:hover {{ background: {BLUE_LIGHT}; color: {BLUE}; }}
        """)
        # The pane takes the panel's own ground, like every other panel; the
        # app's pane rule fills it with the card colour, which reads as white.
        self._tabs.setStyleSheet(
            f"QTabWidget::pane {{ border: none; border-top: 3px solid {BLUE};"
            f" background: transparent; }}")

        root_layout = QtWidgets.QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._tabs)

        self.installEventFilter(self)

    def eventFilter(self, obj, event):
        """Detect when the panel is resized to adjust the log height."""
        if obj == self and event.type() == QtCore.QEvent.Resize:
            self._adjust_log_height()
            self._adjust_file_log_height()
        return super().eventFilter(obj, event)

    def _adjust_log_height(self):
        """Set the log height to 30% of the panel."""
        if self._log.isVisible():
            target_height = int(self.height() * 0.3)  # 30% of the panel
            # Enforce minimum height
            target_height = max(target_height, 200)
            self._log.setFixedHeight(target_height)

    def showEvent(self, event):
        """Cuando el panel se muestra por primera vez."""
        super().showEvent(event)
        self._adjust_log_height()
        self._adjust_file_log_height()

    # ── Batch planning ───────────────────────────────────────────

    def _seq_lengths(self):
        """Lengths of every sequence currently loaded, cached per file."""
        lengths = []
        for f in self._drop.files:
            try:
                stamp = (os.path.getmtime(f), os.path.getsize(f))
            except OSError:
                stamp = None
            hit = self._len_cache.get(f)
            if not hit or hit[0] != stamp:
                hit = (stamp, fasta_lengths(f))
                self._len_cache[f] = hit
            lengths.extend(hit[1])
        return lengths

    def _effective_nseq(self, lengths=None):
        """Sequences per search actually used: computed, or the user's number."""
        if self._batch_mode.currentIndex() == 0:      # Automatic
            if lengths is None:
                lengths = self._seq_lengths()
            return auto_nseq(lengths)
        return self._batch_spin.value()

    def _on_batch_mode(self, index):
        auto = index == 0
        # In Automatic the number is the program's decision, so the box has
        # nothing to offer: hide it and let the plan line below state the
        # result. The box keeps the user's own number (default 50) for Manual;
        # it is never overwritten with the computed value.
        self._batch_spin.setVisible(not auto)
        self._update_batch_plan()

    def _loaded_repeats(self):
        """{header: copies} of the headers repeated within or across the loaded
        files, as the search submits them (spaces as '_')."""
        if len(self._drop.files) == 1:
            return repeated_headers(list(fasta_headers(self._drop.files[0])), True)
        headers = [h for f in self._drop.files for h in fasta_headers(f)]
        return repeated_headers(headers, True)

    def _update_repeat_note(self):
        repeats = self._loaded_repeats() if self._drop.files else {}
        if not repeats:
            self._lbl_repeats.hide()
            return
        self._lbl_repeats.setText(
            f"⚠ {repeated_note(repeats)}. A repeated header is one Query_name in "
            f"the results table: the hits of its copies are mixed in the same rows, "
            f"so they cannot be told apart. Give each sequence its own header.")
        self._lbl_repeats.show()

    def _update_batch_plan(self, *_):
        """Show how the loaded sequences will be split, for the current mode."""
        ctx = "BlastPanel"
        self._update_repeat_note()
        lengths = self._seq_lengths()
        auto = self._batch_mode.currentIndex() == 0

        if not lengths:
            self._lbl_plan.setText(_tr(ctx, "Add FASTA files to see the batch plan.")
                                   if auto else "")
            return

        nseq = self._effective_nseq(lengths)

        sizes = plan_batch_sizes(lengths, nseq)
        n = len(sizes)
        total = sum(lengths)
        word = _tr(ctx, "search") if n == 1 else _tr(ctx, "searches")
        if n == 1:
            plan = f"<b>1 {word}</b> of {sizes[0]:,} sequences"
        else:
            if len(set(sizes)) == 1:
                shape = f"{sizes[0]:,} each"
            else:
                shape = (f"{sizes[0]:,} + {sizes[-1]:,}" if n == 2 else
                         f"{sizes[0]:,} × {n - 1} + {sizes[-1]:,}")
            plan = f"<b>{n} {word}</b> ({shape})"
        msg = (f"{len(lengths):,} sequences · {total:,} bases → " + plan)

        # Only the search count is worth a warning: it is what NCBI penalises.
        if n > 100:
            msg += ("<br>⚠ Over NCBI's limit of 100 searches per 24 h — "
                    "your IP would be moved to a slower queue.")
            colour = RED
        elif not auto and n > len(plan_batch_sizes(lengths, auto_nseq(lengths))):
            best = len(plan_batch_sizes(lengths, auto_nseq(lengths)))
            msg += (f"<br>Automatic would use {best} instead. Fewer searches is "
                    f"what protects your NCBI quota.")
            colour = "#B45309"
        else:
            colour = TEXT_HINT
        self._lbl_plan.setStyleSheet(f"color:{colour}; font-size:14px;")
        self._lbl_plan.setText(msg)


    # ── NCBI usage policy ───────────────────────────────────────

    def _show_ncbi_policy(self):
        """Explain, in full, how NCBI counts and penalises usage."""
        ctx = "BlastPanel"
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(_tr(ctx, "NCBI usage policy"))
        dlg.setMinimumWidth(560)
        dlg.setStyleSheet(f"""
            QDialog {{ background-color: {GRAY_CARD}; }}
            QLabel {{ color: {TEXT_PRI}; background-color: transparent;
                      font-size: 15px; }}
        """)
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(14)

        body = QtWidgets.QLabel(_tr(ctx,
            "<b>Each batch is one BLAST search.</b> NCBI counts searches, not "
            "sequences.<br><br>"
            "Submitting more than <b>100 searches in 24 h</b> moves your traffic to "
            "a slower queue and, in extreme cases, blocks it. The limit applies to "
            "your <b>IP address</b>, so using a different API key does not lift "
            "it.<br><br>"
            "This means <b>larger batches are safer, not riskier.</b> NCBI's own "
            "guidance is that several queries sent as one search run more "
            "efficiently than one search each.<br><br>"
            "The real ceiling is total length: <b>1,000,000 bases per search</b> "
            "(blastn). A batch above that is split automatically.<br><br>"
            "Avoid simultaneous BLAST sessions, and for large projects prefer "
            "off-peak hours — weekends, or 21:00–05:00 US Eastern."
        ))
        body.setWordWrap(True)
        body.setTextFormat(QtCore.Qt.RichText)
        lay.addWidget(body)

        link = QtWidgets.QLabel(
            "<a href='https://blast.ncbi.nlm.nih.gov/doc/blast-help/developerinfo.html'"
            f" style='color:#185FA5;'>{_tr(ctx, 'NCBI developer guidelines')}</a>")
        link.setOpenExternalLinks(True)
        lay.addWidget(link)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch()
        ok = QtWidgets.QPushButton(_tr(ctx, "Close"))
        ok.setObjectName("secondary_btn")
        ok.setMinimumWidth(110)
        ok.clicked.connect(dlg.accept)
        btns.addWidget(ok)
        lay.addLayout(btns)

        dlg.exec_()


    # ── API key persistence ───────────────────────────────────────────────

    @staticmethod
    def _config_path():
        return os.path.join(_profiles_dir(), "blast_config.json")

    def _load_api_key(self) -> str:
        try:
            with open(self._config_path(), "r", encoding="utf-8") as f:
                return _json_mod.load(f).get("api_key", "")
        except Exception:
            return ""

    def _save_api_key(self):
        path = self._config_path()
        try:
            data: dict = {}
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = _json_mod.load(f)
                except Exception:
                    pass
            data["api_key"] = self._api_key_edit.text().strip()
            with open(path, "w", encoding="utf-8") as f:
                _json_mod.dump(data, f, indent=2)
        except Exception:
            pass

    def _on_api_key_changed(self, text: str):
        self._lbl_api_warn.setVisible(not bool(text.strip()))

    def _clear_api_key(self):
        self._api_key_edit.clear()
        self._save_api_key()

    # ── UI helpers ────────────────────────────────────────────────────────

    def retranslateUi(self):
        ctx = "BlastPanel"
        self._lbl_title.setText(_tr(ctx, "BLAST API Search"))
        self._lbl_desc.setText(_tr(ctx,
            "BLAST multiFASTA sequences against NCBI. "
            "Drag-and-drop one or more FASTA files (.fa, .fas, .fasta). "
            "Results include top hits with organism and taxonomic classification, "
            "plus a FASTA of the queried sequences named after the results."))
        self._settings_box.setTitle(_tr(ctx, "BLAST Settings"))
        self._lbl_api.setText(_tr(ctx, "NCBI API Key:"))
        self._lbl_api_warn.setText(_tr(ctx,
            "⚠  A personal NCBI API key is required. Register free at: "
            "ncbi.nlm.nih.gov/account — key is saved automatically."))
        self._lbl_db.setText(_tr(ctx, "Database:"))
        self._lbl_prog.setText(_tr(ctx, "Program:"))
        self._lbl_hits.setText(_tr(ctx, "Hits per sequence (1–100):"))
        self._lbl_batch.setText(_tr(ctx, "Sequences per BLAST search:"))
        self._ncbi_warn_icon.setToolTip(_tr(ctx, "NCBI usage policy — click to read"))
        _mode = self._batch_mode.currentIndex()
        self._batch_mode.blockSignals(True)
        self._batch_mode.setItemText(0, _tr(ctx, "Automatic"))
        self._batch_mode.setItemText(1, _tr(ctx, "Manual"))
        self._batch_mode.setCurrentIndex(_mode)
        self._batch_mode.blockSignals(False)
        self._update_batch_plan()
        self._lbl_tax.setText(_tr(ctx, "Taxonomy lookup:"))
        self._tax_check.setText(_tr(ctx, "Fetch organism + taxonomy"))
        self._ref_group.retranslateUi(ctx)
        self._clear_btn.setText(_tr(ctx, "Clear"))
        self._open_folder_btn.setText(_tr(ctx, "Open folder"))
        self._blast_btn.setText(_tr(ctx, "Run BLAST  →"))
        self._drop.retranslateUi()

        for i in range(len(self._TAB_TEXTS)):
            self._tabs.setTabText(i, self._tab_text(i, ctx))

        self._file_lbl_title.setText(_tr(ctx, "BLAST Web Results"))
        self._file_lbl_desc.setText(_tr(ctx,
            "Parse a Hit Table already downloaded from blast.ncbi.nlm.nih.gov "
            "(website BLAST → Download All → 'Hit Table(text)' or 'Hit Table(csv)'), "
            "instead of submitting a new search — useful when NCBI has throttled this "
            "IP. The same per-query hit selection and organism/taxonomy lookup as "
            "'BLAST API Search' are applied, without a new BLAST search.\n"
            "No FASTA of queried sequences is produced (the file has no sequences, "
            "only hits), so the table cannot be paired in Best Sequence unless you "
            "already have a matching FASTA under the same base name."))
        self._file_settings_box.setTitle(_tr(ctx, "BLAST File Settings"))
        self._file_lbl_hits.setText(_tr(ctx, "Hits per sequence to keep (1–100):"))
        self._file_lbl_tax.setText(_tr(ctx, "Taxonomy lookup:"))
        self._file_tax_check.setText(_tr(ctx, "Fetch organism + taxonomy"))
        self._file_lbl_api_note.setText(_tr(ctx,
            "Uses the NCBI API key configured in the 'BLAST API Search' tab."))
        self._file_ref_group.retranslateUi(ctx)
        self._file_clear_btn.setText(_tr(ctx, "Clear"))
        self._file_open_folder_btn.setText(_tr(ctx, "Open folder"))
        self._file_open_results_btn.setText(_tr(ctx, "Open results"))
        self._file_run_btn.setText(_tr(ctx, "Parse results  →"))
        self._file_drop.retranslateUi()

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    # ── Slots ─────────────────────────────────────────────────────────────

    def _on_files(self, paths):
        self._ref_group.refresh_check()
        enabled = len(paths) >= 1
        self._blast_btn.setEnabled(enabled)
        if enabled:
            self._blast_btn.setStyleSheet(
                f"QPushButton {{ background-color: {BLUE}; color: white; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
                f"QPushButton:hover {{ background-color: #0C4A82; }}"
            )
        else:
            self._blast_btn.setStyleSheet(
                f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
            )
        self._update_batch_plan()

    def _open_apply_reference_dialog(self):
        dlg = _ApplyReferenceDialog(self)
        dlg.exec_()

    def _emit_blast(self):
        if not self._ref_group.validate_or_warn(self):
            return
        cfg = {
            "api_key":        self._api_key_edit.text().strip(),
            "database":       self._DATABASES[self._db_combo.currentIndex()],
            "program":        self._PROGRAMS[self._prog_combo.currentIndex()],
            "nhits":          self._hits_spin.value(),
            "nseq":           self._effective_nseq(),
            "fetch_taxonomy": self._tax_check.isChecked(),
            "tax_reference":  self._ref_group.path,
            "strip_suffix":   self._ref_group.suffix,
        }
        self.blastRequested.emit(list(self._drop.files), cfg)

    def _emit_resume(self):
        # Open on the last run's state file when there is one, so resuming
        # the run just stopped is a single click.
        start = os.path.join(_get_base_dir(), "output")
        if self._last_outdir and os.path.isdir(self._last_outdir):
            start = self._last_outdir
            states = sorted(
                (os.path.join(start, f) for f in os.listdir(start)
                 if f.startswith("blast-") and f.endswith(".state.json")),
                key=os.path.getmtime, reverse=True)
            if states:
                start = states[0]
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Resume a BLAST run — pick its state file",
            start,
            "BLAST run state (blast-*.state.json)")
        if not path:
            return
        # The run keeps its own parameters (saved in the state file) and the
        # API key comes from the panel. Hits per sequence and sequences per
        # batch may change between sessions without making the table
        # inconsistent — but only when the user confirms it, since the panel
        # may simply hold its defaults after a restart.
        cfg = {"api_key": self._api_key_edit.text().strip()}
        try:
            with open(path, encoding="utf-8") as fh:
                params = json.load(fh).get("params") or {}
        except Exception:
            params = {}   # the worker reports an unreadable state file
        panel = {"nhits": self._hits_spin.value()}
        if self._batch_mode.currentIndex() != 0:      # Manual: a number the user chose
            panel["nseq"] = self._batch_spin.value()
        changed = {k: v for k, v in panel.items() if params.get(k) and params[k] != v}
        if changed:
            names = {"nhits": "Hits per sequence", "nseq": "Sequences per BLAST search"}
            diff = "\n".join(f"   {names[k]}: {params[k]} → {v}" for k, v in changed.items())
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Question)
            box.setWindowTitle("Resume BLAST run")
            box.setText("The panel's settings differ from the ones this run used:\n\n"
                        f"{diff}\n\nWhich ones should the rest of the run use?")
            box.setInformativeText("Database, program and taxonomy always stay as in the run.")
            use_new  = box.addButton("Use the panel's", QtWidgets.QMessageBox.AcceptRole)
            keep_old = box.addButton("Keep the run's", QtWidgets.QMessageBox.RejectRole)
            box.addButton(QtWidgets.QMessageBox.Cancel)
            box.setDefaultButton(keep_old)
            box.exec_()
            if box.clickedButton() not in (use_new, keep_old):
                return
            if box.clickedButton() is use_new:
                cfg["param_changes"] = changed
        self.resumeRequested.emit(path, cfg)

    def _open_output_folder(self):
        if self._last_outdir and os.path.isdir(self._last_outdir):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._last_outdir))

    def _reset(self):
        self._drop.clear()
        for k in self._SLOT_KEYS:
            self._log_slots[k] = ""
        self._info_base = ""
        self._elapsed_timer.stop()
        self._log.clear()
        self._log.hide()
        self._open_folder_btn.hide()
        self._send_best_btn.hide()
        self._best_seq_pair = []
        self._stop_btn.hide()
        self._blast_btn.show()
        self._blast_btn.setEnabled(False)
        self._blast_btn.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )
        self._last_outdir = ""
        self._last_tsv    = ""
        self._clear_btn.setEnabled(True)

    # ── Public API (called by MainWindow) ──────────────────────────────────

    def _rebuild_log(self):
        """Rewrite the entire log widget from the fixed slots dict."""
        sep = "─" * 56
        lines = [
            self._log_slots.get("info",     ""),
            sep,
            self._log_slots.get("blast",    ""),
            self._log_slots.get("organism", ""),
            self._log_slots.get("taxonomy", ""),
            self._log_slots.get("progress", ""),
            sep,
            self._log_slots.get("result",   ""),
        ]
        self._log.setPlainText("\n".join(lines))

    def update_status(self, key: str, text: str):
        """Update a named live slot and refresh the display."""
        self._log.show()
        self._adjust_log_height()       # keep log at ~30% of panel once visible
        if key == "info":
            self._info_base = text      # save static part; timer appends elapsed
        self._log_slots[key] = text
        self._rebuild_log()

    def _tick_elapsed(self):
        """Called every second by the timer to update elapsed time in the info line."""
        elapsed = int(time.monotonic() - self._start_time)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        t_str   = f"{h}h {m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        self._log_slots["info"] = f"{self._info_base}  │  Time: {t_str}"
        self._rebuild_log()

    def set_running(self, running: bool):
        self._blast_btn.setVisible(not running)
        self._resume_btn.setVisible(not running)
        self._stop_btn.setVisible(running)
        self._clear_btn.setEnabled(not running)
        if running:
            # A new search invalidates the previous run's pair
            self._send_best_btn.hide()
            self._best_seq_pair = []
            self._start_time = time.monotonic()
            self._elapsed_timer.start()
            self._log.show()
            self._adjust_log_height()   # size log to ~30% of panel when run starts
        else:
            self._elapsed_timer.stop()

    def set_progress(self, current: int, total: int):
        if total > 0:
            pct = int(current * 100 / total)
            bar_len = 28
            filled = int(bar_len * current / total)
            bar = "█" * filled + " " * (bar_len - filled)
            self.update_status(
                "progress",
                f"Progress    │ [{bar}] {pct}%"
            )

    def on_finished(self, outdir: str):
        self.set_running(False)
        self._last_outdir = outdir
        if outdir and os.path.isdir(outdir):
            self._open_folder_btn.show()
            # Prefer xlsx; fall back to tsv if xlsx conversion failed
            for ext in (".xlsx", ".tsv"):
                matches = sorted(
                    (os.path.join(outdir, f) for f in os.listdir(outdir)
                     if f.endswith(ext) and f.startswith("blast-")),
                    key=os.path.getmtime, reverse=True
                )
                if matches:
                    self._last_tsv = matches[0]
                    break
            # FASTA saved by the worker with the same base name as the table
            if self._last_tsv:
                fa = os.path.splitext(self._last_tsv)[0] + ".fa"
                if os.path.isfile(fa):
                    self._best_seq_pair = [fa, self._last_tsv]
                    self._send_best_btn.show()
        # Do NOT overwrite the result slot — the worker already set the final message
        self._blast_btn.setEnabled(bool(self._drop.files))
        if self._drop.files:
            self._blast_btn.setStyleSheet(
                f"QPushButton {{ background-color: {BLUE}; color: white; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
                f"QPushButton:hover {{ background-color: #0C4A82; }}"
            )

    def on_error(self, msg: str):
        self.set_running(False)
        self.update_status("result", f"ERROR       │ {error_summary(msg)}")
        show_error_dialog(self, "BLAST error", msg)
        self._blast_btn.setEnabled(bool(self._drop.files))
        if self._drop.files:
            self._blast_btn.setStyleSheet(
                f"QPushButton {{ background-color: {BLUE}; color: white; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
                f"QPushButton:hover {{ background-color: #0C4A82; }}"
            )


    # ── Tab 2: parse local NCBI BLAST result files ──────────────────────────

    def _build_file_tab(self):
        page = QtWidgets.QWidget()
        outer_layout = QtWidgets.QVBoxLayout(page)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

        inner = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(inner)
        lay.setContentsMargins(20, 20, 20, 8)
        lay.setSpacing(14)
        scroll.setWidget(inner)
        outer_layout.addWidget(scroll, 0)

        # ── Title + description ──
        self._file_lbl_title = make_label("BLAST Web Results", size=19, bold=True)
        self._file_lbl_desc = make_label(
            "Parse a Hit Table already downloaded from blast.ncbi.nlm.nih.gov "
            "(website BLAST → Download All → 'Hit Table(text)' or 'Hit Table(csv)'), "
            "instead of submitting a new search — useful when NCBI has throttled this "
            "IP. The same per-query hit selection and organism/taxonomy lookup as "
            "'BLAST API Search' are applied, without a new BLAST search.\n"
            "No FASTA of queried sequences is produced (the file has no sequences, "
            "only hits), so the table cannot be paired in Best Sequence unless you "
            "already have a matching FASTA under the same base name.",
            color=TEXT_SEC
        )
        self._file_lbl_desc.setWordWrap(True)
        lay.addWidget(self._file_lbl_title)
        lay.addWidget(self._file_lbl_desc)

        # ── Settings group ──
        self._file_settings_box = QtWidgets.QGroupBox("BLAST File Settings")
        self._file_settings_box.setStyleSheet(group_box_style())
        sg = QtWidgets.QFormLayout(self._file_settings_box)
        sg.setLabelAlignment(QtCore.Qt.AlignRight)
        sg.setSpacing(10)
        sg.setContentsMargins(16, 16, 16, 16)

        hits_row = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(hits_row)
        hl.setContentsMargins(0, 0, 0, 0)
        self._file_hits_spin = QtWidgets.QSpinBox()
        self._file_hits_spin.setRange(1, 100)
        self._file_hits_spin.setValue(5)
        self._file_hits_spin.setFixedWidth(80)
        self._file_hits_spin.setToolTip(
            "Keeps only the top N hits per query, as ranked in the file. If a query\n"
            "has fewer hits than this in the file, all of them are kept."
        )
        hl.addWidget(self._file_hits_spin)
        hl.addStretch()
        self._file_lbl_hits = QtWidgets.QLabel("Hits per sequence to keep (1–100):")
        sg.addRow(self._file_lbl_hits, hits_row)

        self._file_tax_check = QtWidgets.QCheckBox("Fetch organism + taxonomic classification")
        self._file_tax_check.setChecked(True)
        self._file_lbl_tax = QtWidgets.QLabel("Taxonomy lookup:")
        sg.addRow(self._file_lbl_tax, self._file_tax_check)

        self._file_lbl_api_note = QtWidgets.QLabel(
            "Uses the NCBI API key configured in the 'BLAST API Search' tab."
        )
        self._file_lbl_api_note.setStyleSheet(f"color:{TEXT_HINT}; font-size:14px;")
        sg.addRow("", self._file_lbl_api_note)

        # ── Optional query-taxonomy reference file ──
        self._file_ref_group = _ReferenceFileGroup()
        self._file_ref_group.apply_link.clicked.connect(self._open_apply_reference_dialog)
        self._file_ref_group.add_to(sg)
        self._file_ref_group.set_names_provider(
            lambda: [q for f in self._file_drop.files for q in hit_table_queries(f)])

        lay.addWidget(self._file_settings_box)

        # ── Drop zone: above the settings, as in tab 1 ──
        self._file_drop = _BlastFileDropZone()
        self._file_drop.filesDropped.connect(self._on_file_files)
        lay.insertWidget(lay.indexOf(self._file_settings_box), self._file_drop)
        lay.addStretch()

        # ── Live progress display ──
        self._file_log_slots = {k: "" for k in self._FILE_SLOT_KEYS}
        self._file_log = QtWidgets.QPlainTextEdit()
        self._file_log.setReadOnly(True)
        self._file_log.setFont(QtGui.QFont("Consolas", 9))
        self._file_log.setMinimumHeight(200)
        self._file_log.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        self._file_log.setStyleSheet(
            f"QPlainTextEdit {{ background:{GRAY_BG}; border:1px solid {GRAY_LINE}; "
            f"border-radius:6px; padding:6px; color:{TEXT_PRI}; margin:0 20px 8px 20px; "
            f"font-family:'Consolas','Courier New',monospace; }}"
        )
        self._file_log.hide()
        outer_layout.addWidget(self._file_log, 0)

        # ── Elapsed-time timer ──
        self._file_info_base     = ""
        self._file_start_time    = 0.0
        self._file_elapsed_timer = QtCore.QTimer(self)
        self._file_elapsed_timer.setInterval(1000)
        self._file_elapsed_timer.timeout.connect(self._tick_file_elapsed)

        # ── Footer ──
        footer = QtWidgets.QWidget()
        footer.setObjectName("blast_footer")
        footer.setStyleSheet(f"""
            QWidget#blast_footer {{
                background: {GRAY_CARD};
                border-top: 1px solid {GRAY_LINE};
            }}
        """)
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(20, 10, 20, 10)

        self._file_clear_btn = QtWidgets.QPushButton("Clear")
        self._file_clear_btn.setObjectName("danger_btn")
        self._file_clear_btn.setFixedHeight(44)
        self._file_clear_btn.setFixedWidth(140)
        self._file_clear_btn.clicked.connect(self._reset_file_tab)
        fl.addWidget(self._file_clear_btn)

        self._file_open_folder_btn = QtWidgets.QPushButton("Open folder")
        self._file_open_folder_btn.setObjectName("secondary_btn")
        self._file_open_folder_btn.setFixedHeight(44)
        self._file_open_folder_btn.hide()
        self._file_open_folder_btn.clicked.connect(self._open_file_output_folder)
        fl.addWidget(self._file_open_folder_btn)

        self._file_open_results_btn = QtWidgets.QPushButton("Open results")
        self._file_open_results_btn.setObjectName("secondary_btn")
        self._file_open_results_btn.setFixedHeight(44)
        self._file_open_results_btn.hide()
        self._file_open_results_btn.clicked.connect(self._open_file_results_file)
        fl.addWidget(self._file_open_results_btn)

        self._file_stop_btn = QtWidgets.QPushButton("Stop")
        self._file_stop_btn.setObjectName("danger_btn")
        self._file_stop_btn.setFixedHeight(44)
        self._file_stop_btn.setFixedWidth(120)
        self._file_stop_btn.hide()
        self._file_stop_btn.clicked.connect(self.stopFileRequested)
        fl.addWidget(self._file_stop_btn)

        fl.addStretch()

        self._file_run_btn = QtWidgets.QPushButton("Parse results  →")
        self._file_run_btn.setObjectName("primary_btn")
        self._file_run_btn.setFixedHeight(44)
        self._file_run_btn.setFixedWidth(300)
        self._file_run_btn.setEnabled(False)
        self._file_run_btn.clicked.connect(self._emit_blast_file)
        self._file_run_btn.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )
        fl.addWidget(self._file_run_btn)
        outer_layout.addWidget(footer)

        self._file_last_outdir = ""
        self._file_last_tsv    = ""
        # Set once a run finishes/errors; only Clear lifts it, so the button
        # cannot be clicked again (even by dropping more files) until the
        # user deliberately resets the tab.
        self._file_run_locked  = False

        return page

    def _adjust_file_log_height(self):
        if self._file_log.isVisible():
            target_height = int(self.height() * 0.3)
            target_height = max(target_height, 200)
            self._file_log.setFixedHeight(target_height)

    def _on_file_files(self, paths):
        self._file_ref_group.refresh_check()
        enabled = len(paths) >= 1 and not self._file_run_locked
        self._file_run_btn.setEnabled(enabled)
        if enabled:
            self._file_run_btn.setStyleSheet(
                f"QPushButton {{ background-color: {BLUE}; color: white; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
                f"QPushButton:hover {{ background-color: #0C4A82; }}"
            )
        else:
            self._file_run_btn.setStyleSheet(
                f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
            )

    def _emit_blast_file(self):
        if not self._file_ref_group.validate_or_warn(self):
            return
        cfg = {
            "api_key":        self._api_key_edit.text().strip(),
            "nhits":          self._file_hits_spin.value(),
            "fetch_taxonomy": self._file_tax_check.isChecked(),
            "tax_reference":  self._file_ref_group.path,
            "strip_suffix":   self._file_ref_group.suffix,
        }
        self.blastFileRequested.emit(list(self._file_drop.files), cfg)

    def _open_file_output_folder(self):
        if self._file_last_outdir and os.path.isdir(self._file_last_outdir):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._file_last_outdir))

    def _open_file_results_file(self):
        if self._file_last_tsv and os.path.isfile(self._file_last_tsv):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._file_last_tsv))

    def _reset_file_tab(self):
        self._file_drop.clear()
        for k in self._FILE_SLOT_KEYS:
            self._file_log_slots[k] = ""
        self._file_info_base = ""
        self._file_elapsed_timer.stop()
        self._file_log.clear()
        self._file_log.hide()
        self._file_open_folder_btn.hide()
        self._file_open_results_btn.hide()
        self._file_stop_btn.hide()
        self._file_run_btn.show()
        self._file_run_btn.setEnabled(False)
        self._file_run_btn.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )
        self._file_last_outdir = ""
        self._file_last_tsv    = ""
        self._file_run_locked  = False
        self._file_clear_btn.setEnabled(True)

    def _rebuild_file_log(self):
        sep = "─" * 56
        lines = [
            self._file_log_slots.get("info",     ""),
            sep,
            self._file_log_slots.get("parse",    ""),
            self._file_log_slots.get("organism", ""),
            self._file_log_slots.get("taxonomy", ""),
            self._file_log_slots.get("progress", ""),
            sep,
            self._file_log_slots.get("result",   ""),
        ]
        self._file_log.setPlainText("\n".join(lines))

    def _tick_file_elapsed(self):
        elapsed = int(time.monotonic() - self._file_start_time)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        t_str   = f"{h}h {m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        self._file_log_slots["info"] = f"{self._file_info_base}  │  Time: {t_str}"
        self._rebuild_file_log()

    # ── Public API for tab 2 (called by MainWindow) ─────────────────────────

    def update_file_status(self, key: str, text: str):
        self._file_log.show()
        self._adjust_file_log_height()
        if key == "info":
            self._file_info_base = text
        self._file_log_slots[key] = text
        self._rebuild_file_log()

    def set_file_progress(self, current: int, total: int):
        if total > 0:
            pct = int(current * 100 / total)
            bar_len = 28
            filled = int(bar_len * current / total)
            bar = "█" * filled + " " * (bar_len - filled)
            self.update_file_status("progress", f"Progress    │ [{bar}] {pct}%")

    def set_file_running(self, running: bool):
        self._file_run_btn.setVisible(not running)
        self._file_stop_btn.setVisible(running)
        self._file_clear_btn.setEnabled(not running)
        if running:
            self._file_start_time = time.monotonic()
            self._file_elapsed_timer.start()
            self._file_log.show()
            self._adjust_file_log_height()
        else:
            self._file_elapsed_timer.stop()

    def on_file_finished(self, outdir: str):
        self.set_file_running(False)
        self._file_last_outdir = outdir
        if outdir and os.path.isdir(outdir):
            self._file_open_folder_btn.show()
            for ext in (".xlsx", ".tsv"):
                matches = sorted(
                    (os.path.join(outdir, f) for f in os.listdir(outdir)
                     if f.endswith(ext) and f.startswith("blastfile-")),
                    key=os.path.getmtime, reverse=True
                )
                if matches:
                    self._file_last_tsv = matches[0]
                    self._file_open_results_btn.show()
                    break
        # Stays disabled until Clear is pressed, so the same run cannot be
        # launched again by accident (dropping more files does not lift this).
        self._file_run_locked = True
        self._file_run_btn.setEnabled(False)
        self._file_run_btn.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )

    def on_file_error(self, msg: str):
        self.set_file_running(False)
        self.update_file_status("result", f"ERROR       │ {error_summary(msg)}")
        show_error_dialog(self, "BLAST web results error", msg)
        self._file_run_locked = True
        self._file_run_btn.setEnabled(False)
        self._file_run_btn.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# BLAST WORKER
# ═══════════════════════════════════════════════════════════════════════════

class _BlastWorker(QtCore.QThread):
    """
    Python equivalent of blast-tax-remote.sh.
    Submits multiFASTA batches to NCBI BLAST via the CGI API, then retrieves
    organism names (nucleotide DB) and taxonomic classification (taxonomy DB)
    for each hit accession.  Results are saved as a TSV file, written per
    batch so data is never lost if the run is interrupted.
    """
    statusUpdated   = QtCore.pyqtSignal(str, str)   # (slot_key, text)
    progressUpdated = QtCore.pyqtSignal(int, int)   # current, total
    taskFinished    = QtCore.pyqtSignal(str)         # output directory
    taskError       = QtCore.pyqtSignal(str)

    _NCBI_BASE    = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    _BLAST_URL    = "https://blast.ncbi.nlm.nih.gov/blast/Blast.cgi"
    # NCBI asks every client to identify itself; unidentified traffic is the
    # first to be throttled when their servers are busy.
    _NCBI_TOOL    = "ONTbarcoder3"
    _USER_AGENT   = "ONTbarcoder3 (NCBI E-utilities client)"
    _MAX_RETRY    = 50   # HTTP-level retries inside _http_get / _http_post
    # A BLAST job is not a request that "fails after N retries": it is a queued
    # search that takes as long as it takes. A batch of 50 sequences against
    # core_nt routinely needs 10-40 min on the public queue, so the wait is a
    # time budget, not a poll count. Polling follows the NCBI BLAST URL API
    # guidelines (blast.ncbi.nlm.nih.gov/doc/blast-help/developerinfo.html):
    # "Do not poll for any single RID more often than once a minute" and
    # "Do not contact the server more often than once every 10 seconds".
    _POLL_BUDGET  = 2700  # max seconds to wait for one RID before giving up
    _POLL_MIN     = 60    # polling interval while the job still looks quick
    _POLL_MAX     = 120   # interval cap once the job is clearly a long one
    _POLL_FAST_S  = 300   # keep the short interval for this long before backing off
    _BLAST_GAP    = 10.0  # min seconds between ANY two Blast.cgi requests
    # Max seconds blocked in one socket operation (connect, or one read — not
    # the whole download). Bounds how long Stop can take to unwind a request.
    _SOCK_TIMEOUT = 30
    _TAX_RETRIES  = 2    # extra retry rounds for "Not_found_in_Taxonomy" results
    _NCBI_RATE    = 9.0  # max HTTP requests/second (NCBI allows 10 with API key)
    # If NCBI outright rejects a batch (no RID, or Status=FAILED — typically its
    # undocumented CPU-time budget for a heavy MEGABLAST job), halve it and retry
    # rather than losing the whole batch: nseq -> nseq/2 -> nseq/4 ..., down to
    # _MIN_SPLIT_SEQS sequences or _MAX_SPLIT_DEPTH halvings, whichever comes first.
    # Default batches are already MAX_QUERY_SEQS (100) or less, so this mostly
    # matters for a manual batch size set higher than that.
    _MIN_SPLIT_SEQS  = 20
    _MAX_SPLIT_DEPTH = 3
    # NCBI rejects a blastn query longer than 1,000,000 bases. Cap a batch a
    # little below that so headers and encoding overhead cannot push it over.
    _MAX_QUERY_BASES = MAX_QUERY_BASES
    _BATCH_UNITS  = 1000 # progress units per batch (phases: wait 0-400, org 400-600, tax 600-800, rows 800-1000)

    def __init__(self, files: List[str], cfg: dict, parent=None):
        super().__init__(parent)
        self.files  = files
        self.cfg    = cfg
        self._stop  = False

        # Shared cache dicts (populated in _run_blast, accessed from threads)
        self._accdb:  dict = {}
        self._taxadb: dict = {}
        self._cache_lock = threading.Lock()
        # Keys already persisted to disk — only new ones are appended per batch
        self._saved_acc_keys:  set = set()
        self._saved_tax_keys:  set = set()
        # Org keys whose CURRENT _taxadb value is a negative that came from a
        # FAILED efetch (transient/network), not from a real "no lineage" answer.
        # These are kept in memory (so the in-run retry loop still finds them) but
        # must NOT be persisted to taxadb.dbx — otherwise a network blip would mark
        # an organism "Not_found_in_Taxonomy" permanently across future runs.
        self._tax_unconfirmed: set = set()

        # Internal map built during batch organism fetch: org_key → taxid string
        # Used to skip the esearch step when fetching taxonomy
        self._org_to_taxid: dict = {}

        # Rate limiter: serialises HTTP calls across all worker threads so
        # we stay within NCBI's 10 req/s limit (using 9 for safety margin).
        self._rl_lock     = threading.Lock()
        self._rl_next     = 0.0   # monotonic time of next allowed request
        # Blast.cgi has its own, much stricter limit (_BLAST_GAP).
        self._blast_rl_lock = threading.Lock()
        self._blast_rl_next = 0.0
        self._last_pct = -1       # last integer % emitted by _emit_progress

        # Usage monitor — accessed from multiple threads via _usage_lock
        self._usage_lock       = threading.Lock()
        self._req_count        = 0
        self._rate_limit_count = 0
        self._server_err_count = 0

        # Resumable-run state (blast-<run_id>.state.json), see _save_state.
        self._state: Optional[dict] = None
        self._state_path = ""
        self._processed: set = set()   # Query_names answered and written to the TSV
        self._session: dict = {}
        self._session_t0 = 0.0

    def stop(self):
        self._stop = True

    # ── Resumable-run state ───────────────────────────────────────────────
    # blast-<run_id>.state.json sits next to the run's .fa and .tsv and is
    # rewritten after every batch. It records which sequences already reached
    # the TSV — including those BLAST found no match for, which leave no row —
    # and the run's parameters, so a stopped run can be resumed into the same
    # files (see _load_resume).

    _STATE_VERSION = 1

    def _save_state(self, status: str = ""):
        """Write the state file atomically. Never raises: losing the ability
        to resume must not abort the run itself."""
        st = self._state
        if st is None or not self._state_path:
            return
        if status:
            st["status"] = status
            self._session["status"] = status
        self._session["elapsed_s"] = int(time.monotonic() - self._session_t0)
        st["processed"] = sorted(self._processed)
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(st, fh, indent=1)
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    @staticmethod
    def _changes_text(changes: dict) -> str:
        names = {"nhits": "hits/seq", "nseq": "seqs/batch"}
        return ", ".join(f"{names.get(k, k)} {old} → {new}"
                         for k, (old, new) in changes.items())

    @staticmethod
    def _remove_stale(path: str):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def _rate_acquire(self, url: str = ""):
        """Block until the next NCBI request slot is available: one Blast.cgi
        request every _BLAST_GAP s, E-utilities at _NCBI_RATE per second."""
        if url.startswith(self._BLAST_URL):
            with self._blast_rl_lock:
                wait = self._blast_rl_next - time.monotonic()
                if wait > 0:
                    self._interruptible_sleep(wait)
                self._blast_rl_next = time.monotonic() + self._BLAST_GAP
            return
        interval = 1.0 / self._NCBI_RATE
        with self._rl_lock:
            now  = time.monotonic()
            wait = self._rl_next - now
            if wait > 0:
                time.sleep(wait)
            self._rl_next = time.monotonic() + interval

    def _emit_progress(self, current: int, total: int):
        """progressUpdated, but only when the integer percentage changes (or
        at 100%): every emit rebuilds the whole log widget in the GUI thread."""
        if total <= 0:
            return
        pct = int(current * 100 / total)
        if pct != self._last_pct or current >= total:
            self._last_pct = pct
            self.progressUpdated.emit(current, total)

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _eutils_params(self, url: str, params: dict) -> dict:
        """Add the API key and tool name to every E-utilities request.

        Without the key NCBI allows 3 requests/second, not the 10 the rate
        limiter here assumes, and answers the excess with HTTP 429 — so a call
        that forgets it fails in a way no amount of retrying can fix.
        """
        if "eutils.ncbi" not in url:
            return params or {}
        out = dict(params or {})
        out.setdefault("tool", self._NCBI_TOOL)
        key = (self.cfg.get("api_key") or "").strip()
        if key:
            out.setdefault("api_key", key)
        return out

    @staticmethod
    def _ssl_context():
        """Default verifying context minus Python 3.13's VERIFY_X509_STRICT,
        which rejects some Windows root CAs ("Basic Constraints of CA cert
        not marked critical"). Chain and hostname checks stay enabled."""
        import ssl
        ctx = ssl.create_default_context()
        strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict:
            ctx.verify_flags &= ~strict
        return ctx

    def _http_get(self, url, params=None, timeout=60):
        import urllib.request, urllib.parse, urllib.error
        params = self._eutils_params(url, params)
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        if url.startswith(self._BLAST_URL) and "tool=" not in url:
            url += ("&" if "?" in url else "?") + f"tool={self._NCBI_TOOL}"
        timeout = min(timeout, self._SOCK_TIMEOUT)
        for attempt in range(self._MAX_RETRY):
            if self._stop:
                return ""
            self._rate_acquire(url)
            if self._stop:
                return ""
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": self._USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=self._ssl_context()) as resp:
                    if resp.status == 200:
                        text = resp.read().decode("utf-8", errors="replace")
                        if text:
                            with self._usage_lock:
                                self._req_count += 1
                            return text
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    with self._usage_lock:
                        self._rate_limit_count += 1
                    try:
                        wait = int(e.headers.get("Retry-After") or 0)
                    except Exception:
                        wait = 0
                    if wait <= 0:
                        wait = min(30 * (attempt + 1), 120)
                    self._interruptible_sleep(wait)
                elif 500 <= e.code < 600:
                    with self._usage_lock:
                        self._server_err_count += 1
                    self._interruptible_sleep(min(10 * (attempt + 1), 60))
                else:
                    return ""  # 4xx other than 429 — not retryable
            except Exception:
                self._interruptible_sleep(2)
        return ""

    def _http_post(self, url, data: str, timeout=120, label=""):
        """POST `data`; returns (body, reason). `body` is "" on failure and
        `reason` says why (HTTP code, timeout, connection error, …) so a caller
        that cares — currently only the BLAST submission — can tell the user
        what NCBI actually did instead of a silent retry loop. `label`, when
        given, also logs that reason itself once retries are exhausted.
        """
        import urllib.request, urllib.error
        if ("eutils.ncbi" in url or url.startswith(self._BLAST_URL)) and "tool=" not in data:
            data = f"{data}&tool={self._NCBI_TOOL}"
        timeout = min(timeout, self._SOCK_TIMEOUT)
        reason = ""
        for attempt in range(self._MAX_RETRY):
            if self._stop:
                return "", "stopped"
            self._rate_acquire(url)
            if self._stop:
                return "", "stopped"
            try:
                req = urllib.request.Request(
                    url,
                    data=data.encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "User-Agent": self._USER_AGENT},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=self._ssl_context()) as resp:
                    if resp.status == 200:
                        text = resp.read().decode("utf-8", errors="replace")
                        if text:
                            with self._usage_lock:
                                self._req_count += 1
                            return text, ""
                        reason = "HTTP 200 with an empty body"
                    else:
                        reason = f"HTTP {resp.status}"
            except urllib.error.HTTPError as e:
                reason = f"HTTP {e.code} {e.reason}".strip()
                if e.code == 429:
                    with self._usage_lock:
                        self._rate_limit_count += 1
                    try:
                        wait = int(e.headers.get("Retry-After") or 0)
                    except Exception:
                        wait = 0
                    if wait <= 0:
                        wait = min(30 * (attempt + 1), 120)
                    self._interruptible_sleep(wait)
                elif 500 <= e.code < 600:
                    with self._usage_lock:
                        self._server_err_count += 1
                    self._interruptible_sleep(min(10 * (attempt + 1), 60))
                else:
                    return "", reason  # 4xx other than 429 — not retryable
            except urllib.error.URLError as e:
                reason = f"connection error: {e.reason}"
                self._interruptible_sleep(2)
            except Exception as e:
                reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                self._interruptible_sleep(2)
            if label and reason and attempt < self._MAX_RETRY - 1 and not self._stop:
                # Make a stalled submission visible instead of silent
                self.statusUpdated.emit(
                    "blast",
                    f"BLAST       │ [{label}] NCBI did not accept the request "
                    f"({reason}) — retrying {attempt + 2}/{self._MAX_RETRY}…"
                )
        if label:
            self.statusUpdated.emit(
                "blast",
                f"BLAST       │ [{label}] no usable reply from NCBI after "
                f"{self._MAX_RETRY} attempts — {reason or 'unknown error'}"
            )
        return "", reason

    def _interruptible_sleep(self, seconds: float):
        """Sleep in 0.5 s chunks so _stop is checked frequently."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop:
                return
            time.sleep(0.5)

    # ── FASTA helpers ─────────────────────────────────────────────────────

    def _merge_fasta_files(self, files):
        parts = []
        for f in files:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read().replace("\r\n", "\n").replace("\r", "\n")
                    parts.append(content.strip())
            except Exception as e:
                self.statusUpdated.emit("blast", f"BLAST       │ Warning: could not read {os.path.basename(f)}")
        return "\n".join(parts)

    def _to_single_line_fasta(self, text):
        out = []
        header = None
        seq_parts = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if header is not None:
                    out.append(header)
                    out.append("".join(seq_parts))
                header = line.replace(" ", "_")
                seq_parts = []
            else:
                seq_parts.append(line)
        if header is not None:
            out.append(header)
            out.append("".join(seq_parts))
        return "\n".join(out)

    @staticmethod
    def _fasta_pairs(fasta_text):
        """(header, seq) pairs of a FASTA from _to_single_line_fasta()."""
        # Each header is followed by exactly one sequence line (possibly
        # empty). Do NOT drop blank lines here — filtering an empty sequence
        # line would shift the next header into its place and desync every
        # header/seq pair from that point on.
        lines = fasta_text.splitlines()
        pairs = []
        i = 0
        while i < len(lines):
            if lines[i].startswith(">"):
                header = lines[i]
                seq    = lines[i + 1] if i + 1 < len(lines) else ""
                pairs.append((header, seq))
                i += 2
            else:
                i += 1
        return pairs

    def _plan_batches(self, pairs, nseq):
        """Split (header, seq) pairs into batches (lists of pairs).

        A batch is capped both by `nseq` and by _MAX_QUERY_BASES: NCBI rejects
        a blastn query longer than 1,000,000 bases, and that length — not a
        sequence count — is its real per-search limit. A single sequence above
        the cap is still sent on its own: letting NCBI reject it is better than
        dropping it silently.
        """
        sizes = plan_batch_sizes([len(s) for _, s in pairs], nseq,
                                 self._MAX_QUERY_BASES)
        out, pos = [], 0
        for size in sizes:
            out.append(pairs[pos:pos + size])
            pos += size
        return out

    # ── BLAST API ─────────────────────────────────────────────────────────

    def _url_encode_fasta(self, text):
        from urllib.parse import quote
        safe = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
            "0123456789_-"
        )
        return quote(text, safe=safe)

    def _extract_ncbi_message(self, resp: str) -> str:
        """Best-effort extraction of NCBI's own wording from a Blast.cgi reply
        (e.g. a CPU-time-limit rejection), so a failure can be reported in the
        server's own words instead of a bare "no response"."""
        import re
        m = re.search(r"(?:Message|Error|ThereWasAnError)\s*[:=]\s*([^\n<]+)", resp, re.I)
        return m.group(1).strip() if m else ""

    def _blast_submit(self, fasta_text, label=""):
        """Submit one BLAST batch. Returns (rid, rtoe, reason) — reason is ""
        on success, otherwise why NCBI (or the network) did not hand back a
        usable RID, worded from its own reply when possible."""
        cfg = self.cfg
        encoded = self._url_encode_fasta(fasta_text)
        data = (
            f"CMD=Put&PROGRAM={cfg['program']}&DATABASE={cfg['database']}"
            f"&api_key={cfg['api_key']}&HITLIST_SIZE={cfg['nhits']}&QUERY={encoded}"
        )
        resp, reason = self._http_post(self._BLAST_URL, data, label=label)
        if not resp:
            return None, 30, reason or "no response from NCBI"
        import re
        rid_m  = re.search(r"RID = ([^\s]+)\s+RTOE", resp)
        rtoe_m = re.search(r"RTOE = (\d+)", resp)
        rid  = rid_m.group(1) if rid_m else None
        rtoe = int(rtoe_m.group(1)) if rtoe_m else 30
        if not rid:
            reason = self._extract_ncbi_message(resp) or "NCBI accepted the request but returned no RID"
            return None, rtoe, reason
        return rid, rtoe, ""

    def _blast_poll(self, rid, batch_label=""):
        """Wait for one RID, up to _POLL_BUDGET seconds.

        The interval grows from _POLL_MIN to _POLL_MAX: a short job is picked up
        quickly, a long one is not polled needlessly. Returns (ready, reason) —
        reason is "" on success, otherwise NCBI's own wording when available.
        """
        # SearchInfo returns just the status block; a bare CMD=Get returns the
        # full HTML result page once READY (MBs for a 100-sequence batch),
        # which would then be downloaded again as tabular.
        url = f"{self._BLAST_URL}?CMD=Get&FORMAT_OBJECT=SearchInfo&RID={rid}"
        polls    = 0
        interval = self._POLL_MIN
        t0       = time.monotonic()
        deadline = t0 + self._POLL_BUDGET
        prefix = f"BLAST       │ [{batch_label}] " if batch_label else "BLAST       │ "

        def _progress(state=""):
            elapsed = int(time.monotonic() - t0)
            self.statusUpdated.emit(
                "blast",
                f"{prefix}Waiting for RID {rid}{state}… "
                f"({elapsed // 60}m {elapsed % 60:02d}s of "
                f"{self._POLL_BUDGET // 60}m max · try {polls})"
            )

        while not self._stop and time.monotonic() < deadline:
            resp = self._http_get(url)
            polls += 1
            if self._stop:
                return False, "stopped"
            if "Status=READY" in resp:
                return True, ""
            if "Status=FAILED" in resp:
                reason = (self._extract_ncbi_message(resp) or
                          "search FAILED (often NCBI's CPU-time budget for a large batch)")
                self.statusUpdated.emit("blast", f"{prefix}Search failed for RID {rid}: {reason}")
                return False, reason
            if "Status=UNKNOWN" in resp:
                self.statusUpdated.emit("blast", f"{prefix}Search expired for RID {rid}.")
                return False, "search expired (RID UNKNOWN)"
            # WAITING, or an empty/unrecognised reply: both mean "not yet".
            _progress("" if "Status=WAITING" in resp else " (no status in reply)")
            self._interruptible_sleep(interval)
            # Once a minute (the NCBI minimum per RID) for the first
            # _POLL_FAST_S; only once the job is clearly long does the
            # interval grow towards _POLL_MAX, so a 45 min wait costs ~30
            # polls.
            if time.monotonic() - t0 > self._POLL_FAST_S:
                interval = min(interval + 5, self._POLL_MAX)

        if not self._stop:
            elapsed = int(time.monotonic() - t0)
            self.statusUpdated.emit(
                "blast",
                f"{prefix}Gave up after {elapsed // 60}m {elapsed % 60:02d}s "
                f"({polls} polls). The search may still be running at NCBI — "
                f"open blast.ncbi.nlm.nih.gov and enter RID {rid}, or retry this "
                f"batch with fewer sequences per BLAST."
            )
        return False, "timed out waiting for RID"

    def _blast_get_tabular(self, rid):
        url = (
            f"{self._BLAST_URL}"
            f"?CMD=Get&FORMAT_TYPE=Text&ALIGNMENT_VIEW=Tabular&RID={rid}"
        )
        resp = self._http_get(url, timeout=120)
        # A real reply (even with 0 hits) always carries '#' comment lines;
        # an empty one means the download failed, not "no hits".
        if not resp or "#" not in resp:
            return None
        return self._parse_tabular(resp)

    def _parse_tabular(self, text):
        # Equivalent to: grep -A nhits '^#' | sed '/^#/d; /^--/d'
        rows = []
        nhits   = self.cfg["nhits"]
        capture = False
        count   = 0
        for line in text.splitlines():
            if line.startswith("# Query:") or (
                line.startswith("#") and "Fields:" in line
            ):
                capture = True
                count   = 0
                continue
            if capture:
                if line.startswith("#") or line.startswith("--"):
                    continue
                stripped = line.strip()
                if stripped:
                    rows.append(stripped)
                    count += 1
                    if count >= nhits:
                        capture = False
        return rows

    @staticmethod
    def _rank_rows(rows):
        """Prepend a per-sample Hit_rank column (1 = best hit) to each tabular row.

        `rows` must arrive in BLAST quality order (best first) within each query,
        as produced by _parse_tabular — so the rank is assigned BEFORE any
        alphabetical reordering. The output is grouped by Query_name with hits in
        ascending rank, and each row gains the rank as its first tab field."""
        counter = {}
        ranked = []
        for row in rows:
            query = row.split("\t", 1)[0]
            rank = counter.get(query, 0) + 1
            counter[query] = rank
            ranked.append((query, rank, row))
        ranked.sort(key=lambda t: (t[0], t[1]))
        return [f"{rank}\t{row}" for (query, rank, row) in ranked]

    # ── NCBI metadata ────────────────────────────────────────────────────

    def _fetch_organism(self, accession: str) -> str:
        """Return organism name for *accession*, using in-memory cache."""
        with self._cache_lock:
            if accession in self._accdb:
                return self._accdb[accession]
        if self._stop:
            return ""
        import xml.etree.ElementTree as ET
        base = self._NCBI_BASE
        xml = self._http_get(
            f"{base}esearch.fcgi",
            params={"db": "nucleotide", "term": accession, "usehistory": "y"}
        )
        if not xml or self._stop:
            return ""   # transient failure — do NOT cache so a later batch can retry
        try:
            root = ET.fromstring(xml)
            web = root.findtext("WebEnv", "")
            key = root.findtext("QueryKey", "")
        except Exception:
            return ""
        if not web or not key:
            return ""
        xml2 = self._http_get(
            f"{base}efetch.fcgi",
            params={
                "db": "nucleotide", "query_key": key,
                "WebEnv": web, "rettype": "gbc", "retmode": "xml"
            },
            timeout=90
        )
        if not xml2 or self._stop:
            return ""   # transient failure — do NOT cache
        try:
            root2 = ET.fromstring(xml2)
            el = root2.find(".//INSDSeq_organism")
            if el is None or not el.text:
                return ""
            org = (
                el.text.strip()
                .replace(" ", "_")
                .replace("&apos;", "'")
                .replace("&amp;", "&")
            )
        except Exception:
            return ""
        with self._cache_lock:
            self._accdb[accession] = org
        return org

    def _fetch_taxonomy(self, organism: str) -> str:
        """Return taxonomy string for *organism*, using in-memory cache."""
        with self._cache_lock:
            if organism in self._taxadb:
                return self._taxadb[organism]
        if self._stop:
            return "Not_found_in_Taxonomy"
        import xml.etree.ElementTree as ET
        base = self._NCBI_BASE
        # NCBI scientific names use spaces; organism is stored with underscores
        search_name = organism.replace("_", " ")
        xml = self._http_get(
            f"{base}esearch.fcgi",
            params={
                "db": "taxonomy",
                "term": f'"{search_name}"[Scientific Name]',
                "usehistory": "y"
            }
        )
        if not xml or self._stop:
            return "Not_found_in_Taxonomy"   # transient — do NOT cache
        try:
            root = ET.fromstring(xml)
            web = root.findtext("WebEnv", "")
            key = root.findtext("QueryKey", "")
        except Exception:
            return "Not_found_in_Taxonomy"
        if not web or not key:
            return "Not_found_in_Taxonomy"
        xml2 = self._http_get(
            f"{base}efetch.fcgi",
            params={
                "db": "taxonomy", "query_key": key,
                "WebEnv": web, "retmode": "xml"
            },
            timeout=90
        )
        if not xml2 or self._stop:
            return "Not_found_in_Taxonomy"   # transient — do NOT cache
        tax = self._parse_taxonomy_xml(xml2)
        result = tax if tax else "Not_found_in_Taxonomy"
        # Cache confirmed results (both found and genuinely absent)
        with self._cache_lock:
            self._taxadb[organism] = result
        return result

    def _parse_taxonomy_xml(self, xml_text):
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return ""
        ranks_wanted = {"kingdom", "class", "order", "family", "genus"}
        found = {}
        for taxon in root.iter("Taxon"):
            rank = (taxon.findtext("Rank") or "").strip().lower()
            name = (taxon.findtext("ScientificName") or "").strip()
            if rank in ranks_wanted and rank not in found:
                found[rank] = name
        return "\t".join(
            found.get(r, "-") for r in ["kingdom", "class", "order", "family", "genus"]
        )

    # ── Batch fetch methods ───────────────────────────────────────────────

    def _fetch_organisms_batch(self, accessions: List[str]) -> None:
        """Fetch organism names + taxids for all accessions in one POST per 500-ID chunk.

        Populates _accdb (accession → org_key) and _org_to_taxid (org_key → taxid).
        Accessions already in _accdb are skipped.
        """
        import xml.etree.ElementTree as ET
        import urllib.parse as _ulp

        to_fetch = [a for a in accessions if a not in self._accdb]
        if not to_fetch:
            return

        api_key = self.cfg.get("api_key", "").strip()
        base    = self._NCBI_BASE

        for start in range(0, len(to_fetch), 500):
            if self._stop:
                return
            chunk  = to_fetch[start:start + 500]
            params: dict = {"db": "nucleotide", "id": ",".join(chunk),
                            "rettype": "gbc", "retmode": "xml"}
            if api_key:
                params["api_key"] = api_key
            xml_text, _reason = self._http_post(f"{base}efetch.fcgi",
                                       _ulp.urlencode(params), timeout=120)
            if not xml_text or self._stop:
                continue
            try:
                root = ET.fromstring(xml_text)
            except Exception:
                continue
            with self._cache_lock:
                for seq in root.iter("INSDSeq"):
                    accver   = (seq.findtext("INSDSeq_accession-version") or "").strip()
                    organism = (seq.findtext("INSDSeq_organism") or "").strip()
                    if not accver or not organism:
                        continue
                    org_key = (organism.replace(" ", "_")
                                       .replace("&apos;", "'")
                                       .replace("&amp;", "&"))
                    self._accdb[accver] = org_key
                    # Extract taxid from the source feature db_xref qualifier
                    for feat in seq.iter("INSDFeature"):
                        if feat.findtext("INSDFeature_key", "") == "source":
                            for qual in feat.iter("INSDQualifier"):
                                if qual.findtext("INSDQualifier_name", "") == "db_xref":
                                    val = qual.findtext("INSDQualifier_value", "")
                                    if val.startswith("taxon:"):
                                        self._org_to_taxid[org_key] = val.split(":")[1].strip()
                                        break
                            break

    def _fetch_taxonomy_batch(self, organisms: List[str]) -> None:
        """Fetch taxonomy for all organisms using taxids when available.

        Fast path: organisms in _org_to_taxid → single batch efetch on taxonomy DB.
        Fallback: organisms without a known taxid use the existing esearch-based
        _fetch_taxonomy() (rare — only for accessions loaded from .dbx cache files
        created by a previous run before this batch optimisation was added).
        Populates _taxadb (org_key → tab-separated ranks).
        """
        import urllib.parse as _ulp

        to_fetch = [o for o in organisms if o not in self._taxadb]
        if not to_fetch:
            return

        api_key = self.cfg.get("api_key", "").strip()
        base    = self._NCBI_BASE

        with_taxid    = [(o, self._org_to_taxid[o]) for o in to_fetch
                         if o in self._org_to_taxid]
        without_taxid = [o for o in to_fetch if o not in self._org_to_taxid]

        # ── Fast path: batch efetch by taxid ─────────────────────────────
        unique_taxids = list(dict.fromkeys(tid for _, tid in with_taxid))
        taxid_results: Dict[str, str] = {}
        # Taxids whose efetch chunk did NOT return a usable response (network
        # failure, stop, etc.). A negative for these is transient, not confirmed.
        failed_taxids: set = set()
        for start in range(0, len(unique_taxids), 500):
            if self._stop:
                # Treat the rest as transient so they are retried, never persisted.
                failed_taxids.update(unique_taxids[start:])
                break
            chunk  = unique_taxids[start:start + 500]
            params: dict = {"db": "taxonomy", "id": ",".join(chunk), "retmode": "xml"}
            if api_key:
                params["api_key"] = api_key
            xml_text, _reason = self._http_post(f"{base}efetch.fcgi",
                                       _ulp.urlencode(params), timeout=120)
            if xml_text and not self._stop:
                taxid_results.update(self._parse_taxonomy_xml_batch(xml_text))
            else:
                failed_taxids.update(chunk)

        with self._cache_lock:
            for org, taxid in with_taxid:
                if taxid in taxid_results:
                    # Confirmed answer (lineage found) from a successful response.
                    self._taxadb[org] = taxid_results[taxid]
                    self._tax_unconfirmed.discard(org)
                elif taxid in failed_taxids:
                    # Transient failure: keep a negative in memory so the in-run
                    # retry loop still finds it, but mark it unconfirmed so it is
                    # excluded from persistence to taxadb.dbx.
                    self._taxadb[org] = "Not_found_in_Taxonomy"
                    self._tax_unconfirmed.add(org)
                else:
                    # Chunk succeeded but this taxid was absent from the response =
                    # genuine "no lineage" negative → safe to cache and persist.
                    self._taxadb[org] = "Not_found_in_Taxonomy"
                    self._tax_unconfirmed.discard(org)

        # ── Fallback: esearch by scientific name (legacy cache hits) ──────
        for org in without_taxid:
            if self._stop:
                return
            self._fetch_taxonomy(org)

    def _parse_taxonomy_xml_batch(self, xml_text: str) -> Dict[str, str]:
        """Parse a multi-taxon taxonomy XML; returns {taxid_str: tab-separated ranks}."""
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return {}
        ranks_wanted = {"kingdom", "class", "order", "family", "genus"}
        results: Dict[str, str] = {}
        for taxon_el in root.findall("Taxon"):   # direct children = one per requested taxid
            taxid = (taxon_el.findtext("TaxId") or "").strip()
            if not taxid:
                continue
            found: Dict[str, str] = {}
            for child in taxon_el.iter("Taxon"):  # includes LineageEx descendants
                rank = (child.findtext("Rank") or "").strip().lower()
                name = (child.findtext("ScientificName") or "").strip()
                if rank in ranks_wanted and rank not in found:
                    found[rank] = name
            lineage = "\t".join(
                found.get(r, "-") for r in ["kingdom", "class", "order", "family", "genus"]
            )
            results[taxid] = lineage
            # NCBI returns a merged taxon under its CURRENT TaxId; a requested taxid
            # that was merged is listed under <AkaTaxIds>. Map those too so the
            # caller's lookup by the requested id resolves instead of being wrongly
            # cached/persisted as "Not_found_in_Taxonomy".
            aka = taxon_el.find("AkaTaxIds")
            if aka is not None:
                for alt_el in aka.findall("TaxId"):
                    alt = (alt_el.text or "").strip()
                    if alt:
                        results[alt] = lineage
        return results

    # ── Cache I/O ────────────────────────────────────────────────────────

    def _load_cache(self, path):
        cache = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        parts = line.rstrip("\n").split("\t", 1)
                        if len(parts) == 2:
                            cache[parts[0]] = parts[1]
            except Exception:
                pass
        return cache

    def _preload_caches(self, output_dir: str):
        """Load the .dbx caches of output_dir and of its SIBLING folders (other
        BLAST runs saved next to it). Deliberately not a recursive walk: the
        parent is usually output/, full of analysis runs with thousands of
        files, and for a hand-picked folder it can be a whole drive."""
        parent = os.path.dirname(output_dir)
        try:
            dirs = [e.path for e in os.scandir(parent) if e.is_dir()]
        except OSError:
            dirs = []
        if output_dir not in dirs:
            dirs.append(output_dir)
        for d in dirs:
            for name, db in (("taxadb.dbx", self._taxadb), ("accdb.dbx", self._accdb)):
                path = os.path.join(d, name)
                if os.path.isfile(path):
                    db.update(self._load_cache(path))
        # Snapshot keys already on disk — only new ones will be appended
        self._saved_tax_keys = set(self._taxadb.keys())
        self._saved_acc_keys = set(self._accdb.keys())

    _TAX_NOT_FOUND = "Not_found_in_Taxonomy"
    _N_TAX_FIELDS = 5    # Subject_Kingdom … Subject_Genus

    def _tax_fields(self, org: str) -> str:
        """The 5 tab-separated taxonomy fields of a hit row. An unresolved
        lineage keeps the 5 columns (marker in Subject_Kingdom, '-' in the
        rest), so Subject_organism never slides into the wrong column."""
        tax = self._taxadb.get(org, self._TAX_NOT_FOUND) if org else self._TAX_NOT_FOUND
        if tax == self._TAX_NOT_FOUND:
            return "\t".join([self._TAX_NOT_FOUND] + ["-"] * (self._N_TAX_FIELDS - 1))
        return tax

    def _repair_taxonomy_rows(self, tsv_path: str) -> Tuple[int, int]:
        """Final pass over the table: every hit has an accession, and every
        accession has an organism and a lineage in NCBI, so a row without them
        is a failed lookup, not an answer. Re-fetch the organism of those
        accessions and the lineage of those organisms (including negatives
        cached earlier) and rewrite the rows that now resolve.
        Returns (rows repaired, rows still unresolved)."""
        n_blast = 13   # Hit_rank + 12 BLAST columns
        try:
            with open(tsv_path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return 0, 0
        if len(lines) < 2:
            return 0, 0

        def _bad(fields):
            return (len(fields) != n_blast + self._N_TAX_FIELDS + 1
                    or not fields[-1].strip()
                    or fields[n_blast] == self._TAX_NOT_FOUND)

        rows = [l.split("\t") for l in lines[1:]]
        bad = [i for i, f in enumerate(rows) if len(f) > 2 and _bad(f)]
        if not bad:
            return 0, 0
        self.statusUpdated.emit(
            "taxonomy", f"Taxonomy    │ Final check: {len(bad)} hit row(s) without "
                        f"organism / lineage, fetching them again…")
        accs = list(dict.fromkeys(rows[i][2] for i in bad if rows[i][2]))
        with self._cache_lock:
            for a in accs:
                if not self._accdb.get(a):
                    self._accdb.pop(a, None)
        self._fetch_organisms_batch(accs)
        orgs = list(dict.fromkeys(self._accdb.get(a, "") for a in accs if self._accdb.get(a)))
        with self._cache_lock:
            for o in orgs:
                if self._taxadb.get(o) == self._TAX_NOT_FOUND:
                    self._taxadb.pop(o, None)
        if orgs and not self._stop:
            self._fetch_taxonomy_batch(orgs)
            self._retry_unresolved_taxonomy(orgs, "[final check] ")
        n_fixed = n_still = 0
        for i in bad:
            f = rows[i]
            org = self._accdb.get(f[2], "")
            new = f[:n_blast] + self._tax_fields(org).split("\t") + [org]
            if org and new[n_blast] != self._TAX_NOT_FOUND:
                n_fixed += 1
            else:
                n_still += 1
            rows[i] = new
        tmp = tsv_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(lines[0] + "\n")
                for f in rows:
                    fh.write("\t".join(f) + "\n")
            os.replace(tmp, tsv_path)
        except OSError:
            return 0, len(bad)
        return n_fixed, n_still

    def _retry_unresolved_taxonomy(self, unique_orgs: List[str], prefix: str = ""):
        """Re-fetch only what failed TRANSIENTLY (network/stop) — never an
        organism NCBI already confirmed has no lineage, which re-asking cannot
        change and which the .dbx caches carry from run to run."""
        for attempt in range(1, self._TAX_RETRIES + 1):
            if self._stop:
                return
            retry_orgs = [o for o in unique_orgs
                          if o in self._tax_unconfirmed or o not in self._taxadb]
            if not retry_orgs:
                return
            with self._cache_lock:
                for org in retry_orgs:
                    self._taxadb.pop(org, None)
            self.statusUpdated.emit(
                "taxonomy",
                f"Taxonomy    │ {prefix}Retry {attempt}/{self._TAX_RETRIES}: "
                f"{len(retry_orgs)} not resolved (network)…"
            )
            self._fetch_taxonomy_batch(retry_orgs)

    def _append_cache(self, new_entries: dict, path: str):
        """Append only new key-value pairs to the cache file (no full rewrite)."""
        if not new_entries:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                for k in sorted(new_entries):
                    fh.write(f"{k}\t{new_entries[k]}\n")
        except Exception:
            pass

    # ── TSV → XLSX conversion ─────────────────────────────────────────────

    def _tsv_to_xlsx(self, tsv_path: str, run_info: Optional[dict] = None) -> str:
        """Convert *tsv_path* to a formatted xlsx (with its Summary and Best hit
        sheets, see build_xlsx_from_tsv). Returns xlsx path or '' on failure."""
        try:
            return build_xlsx_from_tsv(tsv_path, run_info)
        except _XlsxBuildError as e:
            self.statusUpdated.emit("result", f"XLSX skip  │ {e}")
            return ""
        except Exception as e:
            self.statusUpdated.emit("result", f"XLSX error │ {e}")
            return ""

    def _submit_batch_with_retry(self, pairs, batch_label, depth=0):
        """Submit one BLAST batch; if NCBI rejects it outright (no RID, or
        Status=FAILED — typically its undocumented CPU-time budget for a large
        MEGABLAST job) rather than losing the whole batch, halve it and retry
        as two sub-batches. Halves down to _MIN_SPLIT_SEQS sequences or
        _MAX_SPLIT_DEPTH levels, whichever comes first, before giving up.

        Returns (blast_rows, used_pairs): used_pairs is the subset of `pairs`
        that NCBI actually answered (blast_rows is their combined hit table) —
        callers use it (rather than assuming the whole batch succeeded) to know
        exactly which sequences were really queried.
        """
        batch_fasta = "\n".join(h + "\n" + s for h, s in pairs)
        rid, rtoe, reason = self._blast_submit(batch_fasta, label=batch_label)
        if rid:
            self.statusUpdated.emit(
                "blast",
                f"BLAST       │ [{batch_label}] RID={rid}  waiting {rtoe}s…"
            )
            for _ in range(rtoe):
                if self._stop:
                    return [], []
                time.sleep(1)
            ok, poll_reason = self._blast_poll(rid, batch_label)
            if ok:
                rows = self._blast_get_tabular(rid)
                if rows is not None:
                    return rows, pairs
                if self._stop:
                    return [], []
                # The search finished but its results could not be downloaded:
                # mark the batch missing (so it can be re-run) instead of
                # reporting its sequences as "no hits".
                self.statusUpdated.emit(
                    "blast",
                    f"BLAST       │ [{batch_label}] results of RID {rid} could not be "
                    f"downloaded — {len(pairs)} sequence(s) marked missing."
                )
                return [], []
            reason = poll_reason

        if self._stop:
            return [], []

        n = len(pairs)
        if n > self._MIN_SPLIT_SEQS and depth < self._MAX_SPLIT_DEPTH:
            half = -(-n // 2)  # ceil
            self.statusUpdated.emit(
                "blast",
                f"BLAST       │ [{batch_label}] rejected ({reason}) — "
                f"retrying as 2 batches of ~{half} sequences…"
            )
            rows, used = [], []
            for i, sub in enumerate((pairs[:half], pairs[half:])):
                if not sub or self._stop:
                    continue
                r, u = self._submit_batch_with_retry(sub, f"{batch_label}.{i + 1}", depth + 1)
                rows.extend(r)
                used.extend(u)
            return rows, used

        self.statusUpdated.emit(
            "blast",
            f"BLAST       │ [{batch_label}] giving up ({reason}) — "
            f"{n} sequence(s) marked missing."
        )
        return [], []

    # ── Main run ─────────────────────────────────────────────────────────

    def run(self):
        try:
            self._run_blast()
        except Exception as e:
            import traceback
            self.taskError.emit(f"{e}\n{traceback.format_exc()}")

    _RUN_PARAMS = ("database", "program", "nhits", "nseq", "fetch_taxonomy",
                   "tax_reference", "strip_suffix")

    def _blast_headings(self, fetch_tax: bool) -> str:
        cols = (
            "Query_name\tSubject_accession.ver\tP_identity\tAlignment_length\t"
            "Num_mismatches\tGap_opens\tQuery_start\tQuery_end\t"
            "Subject_start\tSubject_end\tEvalue\tBit_score"
        )
        if fetch_tax:
            cols += ("\tSubject_Kingdom\tSubject_Class\tSubject_Order\t"
                     "Subject_Family\tSubject_Genus\tSubject_organism")
        # Per-sample hit rank (1 = best hit) as the first column, so results can
        # be filtered by rank (e.g. Hit_rank == 1 keeps only each sample's top hit).
        return "Hit_rank\t" + cols

    def _new_run(self, run_start) -> Optional[dict]:
        """Set up the files of a new run. Returns the run context, or None
        after reporting the error."""
        cfg    = self.cfg
        run_id = run_start.strftime("%Y%m%d-%H%M%S")

        # ── Output directory (passed from MainWindow dialog) ──
        output_dir = cfg["outdir"]
        os.makedirs(output_dir, exist_ok=True)

        # ── Merge & normalize FASTA ──
        raw   = self._merge_fasta_files(self.files)
        fasta = self._to_single_line_fasta(raw)
        pairs = self._fasta_pairs(fasta)
        if not pairs:
            self.taskError.emit("No FASTA sequences found in the provided files.")
            return None

        # ── Save the sequences that were queried ──
        # Same base name as the results, so the pair (FASTA + table) is what the
        # Best Sequence utility expects: it matches a run's sequences to its
        # BLAST table by file name. The text written is the normalised FASTA
        # actually submitted, so its headers are the Query_name values of the
        # table. It is written before the queries start, so it is there even if
        # the run is stopped half way — resuming the run reads it back.
        fasta_path = os.path.join(output_dir, f"blast-{run_id}.fa")
        try:
            with open(fasta_path, "w", encoding="utf-8") as fa_fh:
                fa_fh.write(fasta + "\n")
        except Exception as e:
            # Auxiliary output: a failure here must not abort the BLAST run.
            fasta_path = ""
            self.statusUpdated.emit(
                "result", f"FASTA skip │ could not write sequences: {e}")

        # ── Open TSV for incremental writing ──
        tsv_path = os.path.join(output_dir, f"blast-{run_id}.tsv")
        for _attempt in range(10):
            try:
                with open(tsv_path, "w", encoding="utf-8") as tsv_fh:
                    tsv_fh.write(self._blast_headings(cfg.get("fetch_taxonomy", True)) + "\n")
                break
            except PermissionError:
                self.statusUpdated.emit(
                    "result",
                    f"⚠ Output file locked — close it to continue… ({_attempt + 1}/10)"
                )
                self._interruptible_sleep(0.5)
        else:
            # Without the header there's no point issuing the BLAST queries (network):
            # it would produce a TSV with no header. Abort cleanly.
            self.taskError.emit(
                f"Could not write output file (locked/permission denied):\n{tsv_path}"
            )
            return None

        state = {
            "version":   self._STATE_VERSION,
            "run_id":    run_id,
            "files":     [os.path.abspath(f) for f in self.files],
            "params":    {k: cfg.get(k) for k in self._RUN_PARAMS},
            "processed": [],
            "status":    "running",
            "sessions":  [],
        }
        return {"state": state, "outdir": output_dir, "pairs": pairs,
                "fasta_path": fasta_path, "tsv_path": tsv_path,
                "hit_queries": set(), "hits_written": 0}

    def _load_resume(self, path: str) -> Optional[dict]:
        """Load an interrupted run to continue it in its own files.

        `path` is the run's blast-<run_id>.state.json; the run's .fa and .tsv
        are read from the same folder. The run's own parameters replace the
        panel's, so the rows added now match the rest of the table; only the
        API key is taken from the panel.
        Returns the run context, or None after reporting the error.
        """
        folder = os.path.dirname(os.path.abspath(path))
        m = re.fullmatch(r"blast-(\d{8}-\d{6})\.state\.json", os.path.basename(path))
        if not m:
            self.taskError.emit(
                f"{os.path.basename(path)} is not the state file of a BLAST run.\n"
                "Pick the blast-<date>.state.json in the run's folder.")
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception as e:
            self.taskError.emit(f"Could not read {os.path.basename(path)}: {e}")
            return None

        run_id     = m.group(1)
        base       = os.path.join(folder, f"blast-{run_id}")
        fasta_path = base + ".fa"
        tsv_path   = base + ".tsv"
        if not (os.path.isfile(fasta_path) and os.path.isfile(tsv_path)):
            self.taskError.emit(
                f"Cannot resume run {run_id}: its sequences "
                f"({os.path.basename(fasta_path)}) and its table "
                f"({os.path.basename(tsv_path)}) must still be in\n{folder}")
            return None

        with open(fasta_path, encoding="utf-8", errors="replace") as fh:
            pairs = self._fasta_pairs(self._to_single_line_fasta(fh.read()))

        # Queries already in the table, and how many rows it holds.
        hit_queries, hits_written = set(), 0
        with open(tsv_path, encoding="utf-8", errors="replace") as fh:
            header = fh.readline().rstrip("\r\n").split("\t")
            if "Query_name" not in header:
                self.taskError.emit(f"{os.path.basename(tsv_path)} has no 'Query_name' column.")
                return None
            q_i = header.index("Query_name")
            for line in fh:
                fields = line.rstrip("\r\n").split("\t")
                if len(fields) > q_i and fields[q_i]:
                    hit_queries.add(fields[q_i])
                    hits_written += 1

        state.pop("pending", None)   # RID kept by earlier versions; never reused
        for k, v in (state.get("params") or {}).items():
            if v is not None:
                self.cfg[k] = v
        if not self.cfg.get("nseq"):
            # Not recorded (a state written while the batch size was on
            # Automatic): compute it from the run's own sequences.
            self.cfg["nseq"] = auto_nseq([len(s) for _h, s in pairs])
            state.setdefault("params", {})["nseq"] = self.cfg["nseq"]
        # Hits per sequence / sequences per batch the user chose to change for
        # the rest of the run (confirmed in the panel). Saved as the run's
        # parameters from now on; the session records what changed.
        changes = {}
        for k, v in (self.cfg.pop("param_changes", None) or {}).items():
            if k in ("nhits", "nseq") and v and v != self.cfg.get(k):
                changes[k] = [self.cfg.get(k), v]
                self.cfg[k] = v
                state.setdefault("params", {})[k] = v
        self.cfg["outdir"] = folder
        self.files = list(state.get("files") or [fasta_path])
        return {"state": state, "outdir": folder, "pairs": pairs,
                "fasta_path": fasta_path, "tsv_path": tsv_path,
                "hit_queries": hit_queries, "hits_written": hits_written,
                "changes": changes}

    def _run_blast(self):
        cfg       = self.cfg
        run_start = datetime.datetime.now()
        resuming  = bool(cfg.get("resume"))

        ctx = self._load_resume(cfg["resume"]) if resuming else self._new_run(run_start)
        if ctx is None:
            return
        nhits      = cfg["nhits"]
        nseq       = cfg["nseq"]
        fetch_tax  = cfg.get("fetch_taxonomy", True)
        state      = ctx["state"]
        mydate     = state["run_id"]
        output_dir = ctx["outdir"]
        fasta_path = ctx["fasta_path"]
        tsv_path   = ctx["tsv_path"]
        all_pairs  = ctx["pairs"]
        seq_count  = len(all_pairs)
        # Query_name values with at least one hit written to the TSV. Sequences
        # in self._processed but absent here got no match from BLAST.
        hit_queries: set = ctx["hit_queries"]
        total_hits_done: int = ctx["hits_written"]

        # self._processed: Query_name (header, no leading '>') of every sequence
        # NCBI actually answered and whose hits reached the TSV. A batch that
        # gets split on rejection (see _submit_batch_with_retry) can succeed
        # only partially, so this is tracked per sequence, not per batch.
        self._state      = state
        self._state_path = os.path.join(output_dir, f"blast-{mydate}.state.json")
        # Sequences with rows in the table are done even if the state file
        # missed them (e.g. the app closed between a batch and its save), so
        # resuming never searches them again and duplicates their rows.
        self._processed  = set(state.get("processed") or []) | hit_queries
        self._session_t0 = time.monotonic()
        self._session    = {"start": run_start.strftime("%Y-%m-%d %H:%M:%S"),
                            "elapsed_s": 0, "status": "running"}
        if ctx.get("changes"):
            self._session["changes"] = ctx["changes"]
        state.setdefault("sessions", []).append(self._session)
        self._save_state("running")

        # ── Cache files stored alongside the output folder ──
        taxadb_path = os.path.join(output_dir, "taxadb.dbx")
        accdb_path  = os.path.join(output_dir, "accdb.dbx")
        if fetch_tax:
            self._preload_caches(output_dir)

        # ── Batches: every sequence not yet in the table ──
        # On resume these always go out as new searches (new RIDs): a search
        # left at NCBI by the stopped run is never waited for or reused — a
        # RID can stay WAITING for good, and a run is often stopped because
        # one did.
        to_search = [p for p in all_pairs if p[0][1:] not in self._processed]
        batch_pairs_list = self._plan_batches(to_search, nseq)
        n_batches = len(batch_pairs_list)

        # ── Summary line (fixed slot "info") ──
        seq_info = (f"Resuming: {len(to_search)}/{seq_count} seqs left" if resuming
                    else f"Sequences: {seq_count}")
        # Repeated headers share one Query_name: their hits end up mixed.
        repeats = repeated_headers([h[1:] for h, _s in all_pairs])
        self.statusUpdated.emit(
            "info",
            f"{seq_info}  │  Batches: {n_batches}"
            f"  │  Hits/seq: {nhits}  │  DB: {cfg['database']}"
            + (f"  │  ⚠ {len(repeats)} repeated header(s)" if repeats else "")
        )
        if resuming and not batch_pairs_list:
            self.statusUpdated.emit(
                "blast", "BLAST       │ Every sequence of this run was already searched.")
        elif ctx.get("changes"):
            self.statusUpdated.emit("blast", "BLAST       │ From now on: " + self._changes_text(ctx["changes"]))

        def _run_batches(plist, tag):
            """Search a list of batches and append their hits to the table."""
            nonlocal total_hits_done
            n_batches = len(plist)
            total_expected = n_batches * self._BATCH_UNITS
            for batch_idx, batch_pairs in enumerate(plist):
                if self._stop:
                    break

                batch_seq   = len(batch_pairs)
                batch_label = f"{tag}{batch_idx+1}/{n_batches}"
                batch_base  = batch_idx * self._BATCH_UNITS

                # ── BLAST (auto-retries as smaller sub-batches if NCBI rejects it) ──
                self.statusUpdated.emit(
                    "blast",
                    f"BLAST       │ [{batch_label}] Submitting {batch_seq} sequences…"
                )
                blast_rows, used_pairs = self._submit_batch_with_retry(batch_pairs, batch_label)
                if self._stop:
                    break
                self._emit_progress(batch_base + 400, total_expected)
                if not used_pairs:
                    continue  # every split down to the floor was rejected; already logged

                self.statusUpdated.emit(
                    "blast",
                    f"BLAST       │ [{batch_label}] {len(blast_rows)} hits retrieved  ✓"
                )

                if fetch_tax:
                    # ── Fetch organisms (batch: one POST for all accessions) ──
                    accessions = [
                        (row.split("\t")[1] if "\t" in row else "") for row in blast_rows
                    ]
                    unique_accs   = list(dict.fromkeys(a for a in accessions if a))
                    n_unique_accs = len(unique_accs)
                    self.statusUpdated.emit(
                        "organism",
                        f"Organism ID │ [{batch_label}] Fetching {n_unique_accs} accessions…"
                    )
                    self._fetch_organisms_batch(unique_accs)
                    if self._stop:
                        break
                    n_org_found = sum(1 for a in unique_accs if a in self._accdb)
                    self.statusUpdated.emit(
                        "organism",
                        f"Organism ID │ [{batch_label}] {n_org_found}/{n_unique_accs} resolved  ✓"
                    )
                    self._emit_progress(batch_base + 600, total_expected)

                    organisms: List[str] = [
                        (self._accdb.get(acc, "") if acc else "") for acc in accessions
                    ]

                    # ── Fetch taxonomy (batch: one POST per 500 taxids) ───────
                    unique_orgs   = list(dict.fromkeys(o for o in organisms if o))
                    n_unique_orgs = len(unique_orgs)
                    self.statusUpdated.emit(
                        "taxonomy",
                        f"Taxonomy    │ [{batch_label}] Fetching {n_unique_orgs} organisms…"
                    )
                    self._fetch_taxonomy_batch(unique_orgs)
                    if self._stop:
                        break

                    # ── Retry organisms whose lookup failed transiently ───────
                    self._retry_unresolved_taxonomy(unique_orgs, f"[{batch_label}] ")

                    n_tax_found = sum(
                        1 for o in unique_orgs
                        if self._taxadb.get(o, "Not_found_in_Taxonomy") != "Not_found_in_Taxonomy"
                    )
                    self.statusUpdated.emit(
                        "taxonomy",
                        f"Taxonomy    │ [{batch_label}] {n_tax_found}/{n_unique_orgs} resolved  ✓"
                    )
                    self._emit_progress(batch_base + 800, total_expected)

                    taxonomies: List[str] = [self._tax_fields(o) for o in organisms]

                    batch_rows_out = self._rank_rows([
                        f"{row}\t{tax}\t{org}"
                        for row, tax, org in zip(blast_rows, taxonomies, organisms)
                    ])

                    # ── Persist only new entries after every batch ──
                    # Exclude _tax_unconfirmed: negatives from a failed efetch are kept
                    # in memory for the in-run retry but must not poison taxadb.dbx.
                    with self._cache_lock:
                        new_tax = {k: self._taxadb[k] for k in self._taxadb
                                   if k not in self._saved_tax_keys
                                   and k not in self._tax_unconfirmed}
                        new_acc = {k: self._accdb[k]  for k in self._accdb  if k not in self._saved_acc_keys}
                        self._append_cache(new_tax, taxadb_path)
                        self._append_cache(new_acc,  accdb_path)
                        self._saved_tax_keys.update(new_tax.keys())
                        self._saved_acc_keys.update(new_acc.keys())

                else:
                    batch_rows_out = self._rank_rows(blast_rows)

                # ── Append batch rows to disk immediately ──
                row_phase_start = 800 if fetch_tax else 400
                row_phase_range = 200 if fetch_tax else 600
                n_batch_rows = max(len(batch_rows_out), 1)
                _batch_written = False
                for _attempt in range(20):   # retry up to 10 s if TSV is open in Excel
                    try:
                        with open(tsv_path, "a", encoding="utf-8") as tsv_fh:
                            for row_idx, r in enumerate(batch_rows_out, 1):
                                tsv_fh.write(r + "\n")
                                self._emit_progress(
                                    batch_base + row_phase_start + int(row_phase_range * row_idx / n_batch_rows),
                                    total_expected
                                )
                        _batch_written = True
                        break  # write succeeded
                    except PermissionError:
                        self.statusUpdated.emit(
                            "result",
                            f"⚠ TSV file is open — close it and the run will resume… ({_attempt + 1}/20)"
                        )
                        self._interruptible_sleep(0.5)

                # Solo marcar como procesadas las secuencias cuyas filas llegaron al disco;
                # de lo contrario deben aparecer en el FASTA de "missing". used_pairs es
                # el subconjunto de batch_pairs que NCBI realmente respondio (puede ser
                # parcial si _submit_batch_with_retry tuvo que dividir el lote).
                if _batch_written:
                    total_hits_done += len(batch_rows_out)
                    self._processed.update(h[1:] for h, _s in used_pairs)
                    # Field 0 is Hit_rank and field 1 is Query_name (see `headings`),
                    # which is the FASTA header of the query without the leading '>'.
                    for _r in batch_rows_out:
                        _fields = _r.split("\t")
                        if len(_fields) > 1:
                            hit_queries.add(_fields[1])
                    self._save_state()

        _run_batches(batch_pairs_list, "Batch ")

        # ── Final retry pass ──
        # A sequence with no hit, or whose batch failed, is searched once more
        # in a batch of its own at the end: an empty answer from NCBI can be
        # transient (overload, a job dropped server-side), and every sequence
        # of a barcode marker is expected to hit something in core_nt.
        n_retried = n_rescued = 0
        if not self._stop:
            retry_pairs = [p for p in all_pairs if p[0][1:] not in hit_queries]
            if retry_pairs:
                n_retried = len(retry_pairs)
                had = set(hit_queries)
                self.statusUpdated.emit(
                    "blast", f"BLAST       │ Final retry: searching again {n_retried} "
                             f"sequence(s) without hits or from failed batches…")
                _run_batches(self._plan_batches(retry_pairs, nseq), "Retry ")
                n_rescued = len(set(hit_queries) - had)
                self.statusUpdated.emit(
                    "blast", f"BLAST       │ Final retry: {n_rescued}/{n_retried} "
                             f"now with hits  ✓")

        # ── Final taxonomy check: rows without organism / lineage ──
        n_tax_fixed = n_tax_still = 0
        if fetch_tax and not self._stop and total_hits_done > 0:
            n_tax_fixed, n_tax_still = self._repair_taxonomy_rows(tsv_path)
            if n_tax_fixed or n_tax_still:
                self.statusUpdated.emit(
                    "taxonomy", f"Taxonomy    │ Final check: {n_tax_fixed} row(s) repaired"
                                + (f", {n_tax_still} still unresolved" if n_tax_still else "")
                                + "  ✓")
                with self._cache_lock:
                    new_tax = {k: self._taxadb[k] for k in self._taxadb
                               if k not in self._saved_tax_keys
                               and k not in self._tax_unconfirmed}
                    new_acc = {k: self._accdb[k] for k in self._accdb
                               if k not in self._saved_acc_keys and self._accdb[k]}
                    self._append_cache(new_tax, taxadb_path)
                    self._append_cache(new_acc, accdb_path)
                    self._saved_tax_keys.update(new_tax.keys())
                    self._saved_acc_keys.update(new_acc.keys())

        # ── Build missing-sequences FASTA (unprocessed or failed batches) ──
        # Over the whole run, so after a resume it lists what is still left.
        missing_pairs = [(h, s) for h, s in all_pairs if h[1:] not in self._processed]

        miss_msg = ""
        miss_path = os.path.join(output_dir, f"missing_seqs_{mydate}.fa")
        if not missing_pairs:
            self._remove_stale(miss_path)   # left by an earlier session of this run
        else:
            try:
                with open(miss_path, "w", encoding="utf-8") as fh:
                    for h, s in missing_pairs:
                        fh.write(h + "\n" + s + "\n")
                miss_msg = (
                    f"{len(missing_pairs)} unprocessed seqs → "
                    f"{os.path.basename(miss_path)}"
                )
            except Exception as exc:
                miss_msg = f"Could not write missing FASTA: {exc}"

        # ── Build no-hit FASTA (queried, but BLAST returned no match) ──
        # Only actually-processed sequences are inspected: sequences of a
        # failed or unprocessed (sub-)batch were never really queried and are
        # already reported in the missing FASTA above.
        nohit_pairs = [(h, sq) for h, sq in all_pairs
                       if h[1:] in self._processed and h[1:] not in hit_queries]

        nohit_msg  = ""
        nohit_path = ""
        if not nohit_pairs:
            self._remove_stale(os.path.join(output_dir, f"nohit_seqs_{mydate}.fa"))
        else:
            nohit_path = os.path.join(output_dir, f"nohit_seqs_{mydate}.fa")
            try:
                with open(nohit_path, "w", encoding="utf-8") as fh:
                    for h, sq in nohit_pairs:
                        fh.write(h + "\n" + sq + "\n")
                nohit_msg = (
                    f"{len(nohit_pairs)} seqs without BLAST match → "
                    f"{os.path.basename(nohit_path)}"
                )
            except Exception as exc:
                nohit_path = ""
                nohit_msg  = f"Could not write no-hit FASTA: {exc}"

        # ── Query taxonomy from a reference file, plus Tax_level_match ──
        # Written into the TSV before the xlsx conversion, so both carry them.
        # Tax_level_match (deepest rank where Subject and Query agree) is only
        # possible once both sides of the taxonomy are in the table
        # (Subject_* from fetch_taxonomy, Query_* from the reference here) —
        # both are added in the same read/rewrite pass over the file.
        ref_msg = ""
        tax_match_msg = ""
        ref_report: List[str] = []
        ref_path = cfg.get("tax_reference", "")
        if ref_path and not self._stop and total_hits_done > 0:
            try:
                _id_col, _tax_cols, ref_table = read_tax_reference(ref_path)
                ref_lower = {k.lower(): v for k, v in ref_table.items()}
                n_rows, n_filled, unknown, n_match = apply_reference_and_tax_match(
                    tsv_path, ref_table, ref_lower, cfg.get("strip_suffix", ""))
                ref_msg = f"Reference query taxonomy: {n_filled}/{n_rows} rows filled"
                if unknown:
                    ref_msg += f" · {len(unknown)} sample(s) not in the reference"
                if n_match >= 0:
                    tax_match_msg = f"Tax_level_match added ({n_match} rows)"
                _fmt = reference_format_warning(ref_table)
                if _fmt:
                    ref_msg += " · ⚠ " + _fmt
                ref_report = reference_report_lines(
                    {sample_id_of(q, cfg.get("strip_suffix", ""))
                     for q in table_column(tsv_path, "Query_name") if q},
                    ref_table, unknown=unknown)
            except Exception as exc:
                ref_msg = f"Reference query taxonomy skipped: {exc}"

        # ── Final state: whether (and why) this run can still be resumed ──
        run_status = ("stopped" if self._stop
                      else "incomplete" if missing_pairs else "completed")

        # ── Convert TSV → XLSX ──
        xlsx_path = ""
        if not self._stop and total_hits_done > 0:
            xlsx_path = self._tsv_to_xlsx(tsv_path, {
                "kind": "search", "run_id": mydate, "files": list(self.files),
                "params": {k: cfg.get(k) for k in self._RUN_PARAMS},
                "processed": self._processed, "status": run_status,
                "fasta": fasta_path,
            })

        self._save_state(run_status)
        resume_hint = "use Resume run… to finish it" if missing_pairs else ""

        extra_msgs = [m for m in (miss_msg, nohit_msg, ref_msg, tax_match_msg) if m]
        if self._stop:
            result_msg = (
                "Stopped     │ " + "  │  ".join(extra_msgs + ([resume_hint] if resume_hint else []))
                if extra_msgs else "Stopped by user."
            )
        else:
            out_name = os.path.basename(xlsx_path if xlsx_path else tsv_path)
            head = f"{total_hits_done} hits written"
            if not extra_msgs:
                head += f" → {out_name}"
            tail = [resume_hint] if resume_hint else []
            result_msg = "Done  ✓     │ " + "  │  ".join([head] + extra_msgs + tail)
        self.statusUpdated.emit("result", result_msg)

        # ── Write run log ──────────────────────────────────────────────────
        elapsed   = datetime.datetime.now() - run_start
        total_sec = int(elapsed.total_seconds())
        h, rem    = divmod(total_sec, 3600)
        m, s      = divmod(rem, 60)
        elapsed_str = f"{h}h {m:02d}m {s:02d}s" if h else f"{m:02d}m {s:02d}s"

        api_key = cfg.get("api_key", "")
        if len(api_key) > 8:
            api_masked = api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:]
        elif api_key:
            api_masked = "****"
        else:
            api_masked = "(not set)"

        status_str = {"stopped": "Stopped", "incomplete": "Completed with missing sequences",
                      "completed": "Completed"}[run_status]
        seqs_queried = len(self._processed)

        log_lines = [
            "BLAST Run Log",
            "=" * 60,
            f"Date/Time  : {run_start.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Status     : {status_str}",
            f"Total time : {elapsed_str}",
            f"Run ID     : {mydate}",
        ]
        sessions = state.get("sessions") or []
        if len(sessions) > 1:
            # One line per session of a resumed run (this one included).
            log_lines.append("Sessions   :")
            for i, ses in enumerate(sessions, 1):
                sec = int(ses.get("elapsed_s", 0))
                line = (f"  {i}. {ses.get('start', '?')}  "
                        f"{sec // 3600}h {sec % 3600 // 60:02d}m  {ses.get('status', '')}")
                if ses.get("changes"):
                    line += "  · " + self._changes_text(ses["changes"])
                log_lines.append(line)
        log_lines += [
            "",
            "Input files:",
        ]
        for f in self.files:
            log_lines.append(f"  {os.path.abspath(f)}")
        log_lines += [
            "",
            "Parameters:",
            f"  Database          : {cfg.get('database', '')}",
            f"  Program           : {cfg.get('program', '')}",
            f"  Hits per sequence : {nhits}",
            f"  Sequences / batch : {nseq}",
            f"  Fetch taxonomy    : {'Yes' if fetch_tax else 'No'}",
            f"  NCBI API key      : {api_masked}",
            "",
            "Results:",
            f"  Sequences found   : {seq_count}"
            + (f"  (⚠ {repeated_note(repeats)}: their hits share one Query_name)"
               if repeats else ""),
            f"  Sequences queried : {seqs_queried}/{seq_count}"
            f" ({n_batches} batch(es) planned{' this session' if resuming else ''})",
            f"  Hits written      : {total_hits_done}",
            f"  Seqs with hits    : {len(hit_queries)}",
            f"  Seqs with no hits : {len(nohit_pairs)}",
            f"  Output folder     : {output_dir}",
            f"  TSV file          : {os.path.basename(tsv_path)}",
            f"  XLSX file         : {os.path.basename(xlsx_path) if xlsx_path else 'N/A'}",
            f"  FASTA file        : {os.path.basename(fasta_path) if fasta_path else 'N/A'}"
            f"  (queried sequences, pairs with the table for Best Sequence)",
        ]
        if miss_msg:
            log_lines.append(f"  Missing seqs      : {miss_msg}")
        if nohit_msg:
            log_lines.append(f"  No-hit seqs       : {nohit_msg}")
        if n_retried:
            log_lines.append(f"  Final retry       : {n_retried} seq(s) without hits or from "
                             f"failed batches searched again · {n_rescued} now with hits"
                             + (" (the rest are confirmed: no hit after retry)"
                                if n_rescued < n_retried else ""))
        if n_tax_fixed or n_tax_still:
            log_lines.append(f"  Taxonomy check    : {n_tax_fixed} hit row(s) without organism / "
                             f"lineage repaired · {n_tax_still} still unresolved")
        if ref_msg:
            log_lines.append(f"  Reference tax     : {ref_msg}")
        if tax_match_msg:
            log_lines.append(f"  Tax level match   : {tax_match_msg}")
        if ref_report:
            log_lines += ["", "Reference taxonomy:"] + ref_report
        if nohit_pairs:
            log_lines += ["", "Sequences with no BLAST hit:"]
            for h, _sq in nohit_pairs:
                log_lines.append(f"  {h[1:]}")
        log_lines += [
            "",
            "NOTE: Do not delete the .dbx cache files (accdb.dbx, taxadb.dbx).",
            "      They store organism and taxonomy lookups already performed and",
            "      will significantly speed up future BLAST runs on the same or",
            "      overlapping accession numbers.",
            "",
        ]

        os.makedirs(output_dir, exist_ok=True)
        log_path = os.path.join(output_dir, f"blast_run_log_{mydate}.txt")
        try:
            with open(log_path, "w", encoding="utf-8") as lf:
                lf.write("\n".join(log_lines))
        except Exception as exc:
            self.statusUpdated.emit("result", f"{result_msg}  │  Log error: {exc}")

        self.statusUpdated.emit(
            "info",
            f"Session stats  │  {self._req_count} requests · "
            f"{self._rate_limit_count} rate limit(s) (429) · "
            f"{self._server_err_count} server error(s) (5xx)"
        )
        self.taskFinished.emit(output_dir)


# ═══════════════════════════════════════════════════════════════════════════
# BLAST FILE WORKER — parse an already-downloaded NCBI Hit Table
# ═══════════════════════════════════════════════════════════════════════════

class _BlastFileWorker(_BlastWorker):
    """Builds the same kind of results table as `_BlastWorker`, but from a Hit
    Table already downloaded from blast.ncbi.nlm.nih.gov instead of submitting
    a new search — useful when NCBI has throttled this IP's search traffic.

    Reuses `_BlastWorker`'s NCBI-metadata primitives as-is (rate limiting,
    `_fetch_organisms_batch`/`_fetch_taxonomy_batch`, the `.dbx` caches,
    `_rank_rows`, `_parse_tabular`, `_tsv_to_xlsx`) — only the search step is
    replaced by reading local files, so `_BlastWorker._run_blast` (the live
    search) is never touched.
    """

    def run(self):
        try:
            self._run_blast_file()
        except Exception as e:
            import traceback
            self.taskError.emit(f"{e}\n{traceback.format_exc()}")

    # ── Parsing ──────────────────────────────────────────────────────────

    def _parse_hit_csv(self, text: str) -> List[str]:
        """Headerless comma-delimited Hit Table (NCBI 'Hit Table(csv)' export).

        Rows already arrive best-hit-first per query, so hits are kept in the
        order they appear and simply capped at cfg['nhits'] per query.
        """
        import csv, io
        nhits = self.cfg["nhits"]
        counts: Dict[str, int] = {}
        rows: List[str] = []
        for fields in csv.reader(io.StringIO(text)):
            if not fields or not any(f.strip() for f in fields):
                continue
            query = fields[0]
            n = counts.get(query, 0)
            if n >= nhits:
                continue
            counts[query] = n + 1
            rows.append("\t".join(fields))
        return rows

    def _read_hit_file(self, path: str) -> List[str]:
        """Rows of one result file, in whichever of the two NCBI export shapes
        it uses: '#'-commented tabular text, or headerless CSV."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception as e:
            self.statusUpdated.emit(
                "parse",
                f"Parse       │ Warning: could not read {os.path.basename(path)}: {e}"
            )
            return []
        if text.lstrip("﻿").lstrip().startswith("#"):
            return self._parse_tabular(text)
        return self._parse_hit_csv(text)

    # ── Main run ─────────────────────────────────────────────────────────

    def _run_blast_file(self):
        cfg       = self.cfg
        nhits     = cfg["nhits"]
        run_start = datetime.datetime.now()
        mydate    = run_start.strftime("%Y%m%d-%H%M%S")

        output_dir = cfg["outdir"]
        os.makedirs(output_dir, exist_ok=True)

        taxadb_path = os.path.join(output_dir, "taxadb.dbx")
        accdb_path  = os.path.join(output_dir, "accdb.dbx")
        fetch_tax = cfg.get("fetch_taxonomy", True)
        if fetch_tax:
            self._preload_caches(output_dir)

        # ── Parse every result file ──
        self.statusUpdated.emit(
            "parse", f"Parse       │ Reading {len(self.files)} file(s)…")
        blast_rows: List[str] = []
        file_queries = []   # (file, its Query_names) for the xlsx Summary
        for f in self.files:
            if self._stop:
                break
            rows = self._read_hit_file(f)
            blast_rows.extend(rows)
            file_queries.append(
                (f, list(dict.fromkeys(r.split("	", 1)[0] for r in rows))))
            self.statusUpdated.emit(
                "parse",
                f"Parse       │ {os.path.basename(f)}: {len(rows)} hit row(s) kept"
            )

        if not blast_rows:
            if self._stop:
                self.taskFinished.emit(output_dir)
            else:
                self.taskError.emit(
                    "No hit rows could be parsed from the provided file(s). Make "
                    "sure they are the NCBI 'Hit Table(text)' or 'Hit Table(csv)' export."
                )
            return

        n_queries = len({r.split("\t", 1)[0] for r in blast_rows})
        self.statusUpdated.emit(
            "info",
            f"Queries: {n_queries}  │  Hits kept: {len(blast_rows)}  │  Hits/query: {nhits}"
        )

        _blast_cols = (
            "Query_name\tSubject_accession.ver\tP_identity\tAlignment_length\t"
            "Num_mismatches\tGap_opens\tQuery_start\tQuery_end\t"
            "Subject_start\tSubject_end\tEvalue\tBit_score"
        )
        headings = (
            _blast_cols + "\tSubject_Kingdom\tSubject_Class\tSubject_Order\t"
            "Subject_Family\tSubject_Genus\tSubject_organism"
            if fetch_tax else _blast_cols
        )
        headings = "Hit_rank\t" + headings

        total_expected = 1000
        self._emit_progress(50, total_expected)

        if fetch_tax and not self._stop:
            accessions = [
                (row.split("\t")[1] if "\t" in row else "") for row in blast_rows
            ]
            unique_accs = list(dict.fromkeys(a for a in accessions if a))
            self.statusUpdated.emit(
                "organism", f"Organism ID │ Fetching {len(unique_accs)} accessions…")
            self._fetch_organisms_batch(unique_accs)
            n_org_found = sum(1 for a in unique_accs if a in self._accdb)
            self.statusUpdated.emit(
                "organism", f"Organism ID │ {n_org_found}/{len(unique_accs)} resolved  ✓")
            self._emit_progress(400, total_expected)

            organisms = [(self._accdb.get(acc, "") if acc else "") for acc in accessions]

            if not self._stop:
                unique_orgs = list(dict.fromkeys(o for o in organisms if o))
                self.statusUpdated.emit(
                    "taxonomy", f"Taxonomy    │ Fetching {len(unique_orgs)} organisms…")
                self._fetch_taxonomy_batch(unique_orgs)

                self._retry_unresolved_taxonomy(unique_orgs)

                n_tax_found = sum(
                    1 for o in unique_orgs
                    if self._taxadb.get(o, "Not_found_in_Taxonomy") != "Not_found_in_Taxonomy"
                )
                self.statusUpdated.emit(
                    "taxonomy", f"Taxonomy    │ {n_tax_found}/{len(unique_orgs)} resolved  ✓")
                self._emit_progress(800, total_expected)

                with self._cache_lock:
                    new_tax = {k: self._taxadb[k] for k in self._taxadb
                               if k not in self._saved_tax_keys
                               and k not in self._tax_unconfirmed}
                    new_acc = {k: self._accdb[k] for k in self._accdb
                               if k not in self._saved_acc_keys}
                    self._append_cache(new_tax, taxadb_path)
                    self._append_cache(new_acc, accdb_path)
                    self._saved_tax_keys.update(new_tax.keys())
                    self._saved_acc_keys.update(new_acc.keys())

            taxonomies = [self._tax_fields(o) for o in organisms]
            ranked_rows = self._rank_rows([
                f"{row}\t{tax}\t{org}"
                for row, tax, org in zip(blast_rows, taxonomies, organisms)
            ])
        else:
            ranked_rows = self._rank_rows(blast_rows)

        tsv_path = os.path.join(output_dir, f"blastfile-{mydate}.tsv")
        for _attempt in range(10):
            try:
                with open(tsv_path, "w", encoding="utf-8") as tsv_fh:
                    tsv_fh.write(headings + "\n")
                    for r in ranked_rows:
                        tsv_fh.write(r + "\n")
                break
            except PermissionError:
                self.statusUpdated.emit(
                    "result",
                    f"⚠ Output file locked — close it to continue… ({_attempt + 1}/10)"
                )
                self._interruptible_sleep(0.5)
        else:
            self.taskError.emit(
                f"Could not write output file (locked/permission denied):\n{tsv_path}"
            )
            return

        self._emit_progress(900, total_expected)

        # ── Query taxonomy from a reference file, plus Tax_level_match ──
        # Both are added in the same read/rewrite pass over the file.
        ref_msg = ""
        tax_match_msg = ""
        ref_report: List[str] = []
        ref_path = cfg.get("tax_reference", "")
        if ref_path and ranked_rows and not self._stop:
            try:
                _id_col, _tax_cols, ref_table = read_tax_reference(ref_path)
                ref_lower = {k.lower(): v for k, v in ref_table.items()}
                n_rows, n_filled, unknown, n_match = apply_reference_and_tax_match(
                    tsv_path, ref_table, ref_lower, cfg.get("strip_suffix", ""))
                ref_msg = f"Reference query taxonomy: {n_filled}/{n_rows} rows filled"
                if unknown:
                    ref_msg += f" · {len(unknown)} sample(s) not in the reference"
                if n_match >= 0:
                    tax_match_msg = f"Tax_level_match added ({n_match} rows)"
                _fmt = reference_format_warning(ref_table)
                if _fmt:
                    ref_msg += " · ⚠ " + _fmt
                ref_report = reference_report_lines(
                    {sample_id_of(q, cfg.get("strip_suffix", ""))
                     for q in table_column(tsv_path, "Query_name") if q},
                    ref_table, unknown=unknown)
            except Exception as exc:
                ref_msg = f"Reference query taxonomy skipped: {exc}"

        xlsx_path = ""
        if ranked_rows and not self._stop:
            hit_params = {"nhits": nhits, "fetch_taxonomy": fetch_tax,
                          "tax_reference": ref_path,
                          "strip_suffix": cfg.get("strip_suffix", "")}
            save_hit_table_info(tsv_path, file_queries, hit_params)
            xlsx_path = self._tsv_to_xlsx(tsv_path, {
                "kind": "hit_table", "inputs": file_queries, "params": hit_params,
            })
        self._emit_progress(total_expected, total_expected)

        extra_msgs = [m for m in (ref_msg, tax_match_msg) if m]
        if self._stop:
            result_msg = (
                "Stopped     │ " + "  │  ".join(extra_msgs)
                if extra_msgs else "Stopped by user."
            )
        else:
            out_name = os.path.basename(xlsx_path if xlsx_path else tsv_path)
            head = f"{len(ranked_rows)} hits written → {out_name}"
            result_msg = "Done  ✓     │ " + "  │  ".join([head] + extra_msgs)
        self.statusUpdated.emit("result", result_msg)

        # ── Run log ──────────────────────────────────────────────────────
        elapsed   = datetime.datetime.now() - run_start
        total_sec = int(elapsed.total_seconds())
        h, rem    = divmod(total_sec, 3600)
        m, s      = divmod(rem, 60)
        elapsed_str = f"{h}h {m:02d}m {s:02d}s" if h else f"{m:02d}m {s:02d}s"
        status_str  = "Stopped" if self._stop else "Completed"

        log_lines = [
            "BLAST Web Results Run Log",
            "=" * 60,
            f"Date/Time  : {run_start.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Status     : {status_str}",
            f"Total time : {elapsed_str}",
            "",
            "Input files:",
        ]
        for f in self.files:
            log_lines.append(f"  {os.path.abspath(f)}")
        log_lines += [
            "",
            "Parameters:",
            f"  Hits per query kept : {nhits}",
            f"  Fetch taxonomy      : {'Yes' if fetch_tax else 'No'}",
            "",
            "Results:",
            f"  Queries           : {n_queries}",
            f"  Hits written      : {len(ranked_rows)}",
            f"  Output folder     : {output_dir}",
            f"  TSV file          : {os.path.basename(tsv_path)}",
            f"  XLSX file         : {os.path.basename(xlsx_path) if xlsx_path else 'N/A'}",
        ]
        if ref_msg:
            log_lines.append(f"  Reference tax     : {ref_msg}")
        if tax_match_msg:
            log_lines.append(f"  Tax level match   : {tax_match_msg}")
        if ref_report:
            log_lines += ["", "Reference taxonomy:"] + ref_report
        log_lines += [
            "",
            "NOTE: this tab does not produce a FASTA of queried sequences (the input",
            "      file carries hits only, not sequences), so the table can only be",
            "      paired in Best Sequence if a matching FASTA already exists under",
            "      the same base name.",
            "",
            "NOTE: Do not delete the .dbx cache files (accdb.dbx, taxadb.dbx).",
            "      They store organism and taxonomy lookups already performed and",
            "      will significantly speed up future BLAST runs on the same or",
            "      overlapping accession numbers.",
            "",
        ]

        log_path = os.path.join(output_dir, f"blastfile_run_log_{mydate}.txt")
        try:
            with open(log_path, "w", encoding="utf-8") as lf:
                lf.write("\n".join(log_lines))
        except Exception as exc:
            self.statusUpdated.emit("result", f"{result_msg}  │  Log error: {exc}")

        self.taskFinished.emit(output_dir)
