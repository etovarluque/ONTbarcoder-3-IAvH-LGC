"""Standalone analysis: for a completed Parameter Batch run, work out exactly
which run combinations were needed to recover the taxonomically-identified
best sequences produced by Best Sequence Selection (best_seq_panel.py).

Given:
  - a Parameter Batch output folder (the "..._batch" folder, containing
    batch_run_summary.tsv, produced by batch_sweep.py), and
  - a Best Sequence Selection output folder (the "..._bestseq" folder,
    containing bestseq-<date>.tsv and bestseq-<date>_identified.fasta),

this script:
  1. Re-derives, for every sample with a taxonomic hit, exactly which of the
     N run folders reproduce that winning sequence identically (by direct
     sequence comparison against each run's consensus_filtered.fa AND its
     secondary_variants.fa — a winner can be a secondary variant — not by
     trusting the ";run<N>" tag in the dedup FASTA, which only records the
     FIRST run that produced a given variant, not every run that did).
  2. Reports per-run coverage (how many identified sequences a single run
     alone would reproduce).
  3. Computes a greedy minimal set of runs that together cover as many of
     the identified sequences as possible.
  4. Writes a multi-sheet Excel report with all of the above, plus
     minimal_run_set.cfg: the minimal set as a "# combos" batch config,
     ready for "Load batch config..." in the Parameter Batch panel.

No Qt/PyQt import here on purpose: runnable standalone from the command
line, and used by the "Coverage report" section of the Parameter Batch panel
(run_coverage_report).

Usage:
    python batch_coverage_report.py --batch-dir <..._batch folder> \
        --bestseq-dir <..._bestseq folder> [--output report.xlsx]
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from typing import Callable, Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

FASTA_NAME_DEFAULT = "consensus_filtered.fa"
VARIANTS_FASTA_NAME = "secondary_variants.fa"
CFG_NAME = "minimal_run_set.cfg"


# ---------------------------------------------------------------------------
# FASTA helpers
# ---------------------------------------------------------------------------

def read_fasta(path: str) -> List[Tuple[str, str]]:
    """Return [(header_without_'>', sequence), ...] in file order.
    Tolerant of Windows line endings and stray non-UTF-8 bytes."""
    records: List[Tuple[str, str]] = []
    header = None
    seq_parts: List[str] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line.startswith(">"):
                if header is not None:
                    records.append((header, "".join(seq_parts)))
                header = line[1:]
                seq_parts = []
            else:
                seq_parts.append(line.strip())
    if header is not None:
        records.append((header, "".join(seq_parts)))
    return records


def sample_of(header: str) -> str:
    """Sample label: header text before the first ';' (ONTbarcoder convention)."""
    return header.split(";", 1)[0].strip()


_VARIANT_SUFFIX = re.compile(r"_var\d+$")


def host_of(header: str) -> str:
    """Sample a record belongs to, comparable across consensus and variant
    files: the consensus is '{sample}_all.fa;...' and its secondary variants
    '{sample}_var{i};...' (whose numbering can differ from run to run)."""
    s = sample_of(header)
    s = _VARIANT_SUFFIX.sub("", s)
    if s.endswith("_all.fa"):
        s = s[:-len("_all.fa")]
    return s


def parse_params(param_str: str) -> Dict[str, str]:
    """'k1=v1, k2=v2, ...' -> {'k1': 'v1', 'k2': 'v2', ...}."""
    out: Dict[str, str] = {}
    for part in param_str.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# Input discovery / loading
# ---------------------------------------------------------------------------

def find_one(pattern: str, description: str, log: Callable[[str], None] = print) -> str:
    matches = [p for p in glob.glob(pattern) if not os.path.basename(p).startswith("~$")]
    if not matches:
        raise FileNotFoundError(f"No {description} found matching: {pattern}")
    matches.sort(key=os.path.getmtime, reverse=True)
    if len(matches) > 1:
        log(f"  Note: {len(matches)} {description} candidates found, "
            f"using most recent: {os.path.basename(matches[0])}")
    return matches[0]


def load_runs(batch_dir: str, output_root: str) -> Dict[int, dict]:
    """Parse batch_run_summary.tsv -> {run_number: {folder, params, params_raw, n_consensus_filtered}}."""
    summary_path = os.path.join(batch_dir, "batch_run_summary.tsv")
    if not os.path.isfile(summary_path):
        raise FileNotFoundError(f"batch_run_summary.tsv not found in {batch_dir}")

    runs: Dict[int, dict] = {}
    with open(summary_path, encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            run_no = int(row["Run"])
            runs[run_no] = {
                "folder": os.path.join(output_root, "ont-barcoder_" + row["Folder"]),
                "params_raw": row["Parameters"],
                "params": parse_params(row["Parameters"]),
                "n_consensus_filtered": row.get("N_consensus_filtered", ""),
            }
    return runs


def load_tax_info(bestseq_tsv: str) -> Dict[str, dict]:
    """Best Sequence report rows keyed by the sample label of their Header —
    the exact text of the FASTA record — rather than the Sample column, which
    differs when Best Sequence was run with 'Remove suffix' (Sample 'DNS-1'
    vs header 'DNS-1_all.fa;...')."""
    out: Dict[str, dict] = {}
    with open(bestseq_tsv, encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            key = sample_of(row.get("Header") or "") or row.get("Sample", "")
            out[key] = row
    return out


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def compute_coverage(
    winners: Dict[str, Tuple[str, str]], runs: Dict[int, dict], fasta_name: str,
    log: Callable[[str], None] = print,
) -> Tuple[Dict[str, set], Dict[int, int]]:
    """For each winner (label -> (host sample, sequence)), find every run that
    produced that exact sequence for that sample, either as its consensus
    (`fasta_name`) or as one of its secondary variants (raw or corrected).

    Returns (coverage: label -> set of run numbers, run_hit_count: run -> count).
    """
    coverage: Dict[str, set] = {s: set() for s in winners}
    run_hit_count: Dict[int, int] = {}

    for run_no, info in sorted(runs.items()):
        fa_path = os.path.join(info["folder"], fasta_name)
        if not os.path.isfile(fa_path):
            log(f"  Warning: missing {fa_path}")
            run_hit_count[run_no] = 0
            continue
        produced = {(host_of(h), seq.upper()) for h, seq in read_fasta(fa_path)}
        var_path = os.path.join(info["folder"], VARIANTS_FASTA_NAME)
        if os.path.isfile(var_path):
            produced |= {(host_of(h), seq.upper()) for h, seq in read_fasta(var_path)}
        hits = 0
        for label, (host, seq) in winners.items():
            if (host, seq) in produced:
                coverage[label].add(run_no)
                hits += 1
        run_hit_count[run_no] = hits

    return coverage, run_hit_count


def greedy_set_cover(
    winners: Dict[str, Tuple[str, str]], coverage: Dict[str, set], runs: Dict[int, dict]
) -> List[Tuple[int, set]]:
    """Greedy minimal set of runs covering as many samples as possible."""
    remaining = set(winners.keys())
    chosen: List[Tuple[int, set]] = []
    while remaining:
        best_r, best_cov = None, set()
        for r in runs:
            cov = {s for s in remaining if r in coverage[s]}
            if len(cov) > len(best_cov):
                best_r, best_cov = r, cov
        if best_r is None or not best_cov:
            break
        chosen.append((best_r, best_cov))
        remaining -= best_cov
    return chosen


# ---------------------------------------------------------------------------
# Excel report
# ---------------------------------------------------------------------------

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def style_header(ws, ncols: int, row: int = 1) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")


def autofit(ws, widths: List[int]) -> None:
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build_workbook(
    out_path: str,
    runs: Dict[int, dict],
    winners: Dict[str, Tuple[str, str]],
    tax_info: Dict[str, dict],
    coverage: Dict[str, set],
    run_hit_count: Dict[int, int],
    chosen_runs: List[Tuple[int, set]],
    param_keys: List[str],
) -> None:
    n_runs = len(runs)
    n_winners = len(winners)
    covered_by_set = set()
    sample_assigned_run: Dict[str, int] = {}
    for r, cov in chosen_runs:
        covered_by_set |= cov
        for s in cov:
            sample_assigned_run[s] = r
    never_covered = sorted(set(winners.keys()) - covered_by_set)

    wb = Workbook()

    # --- Sheet 1: Summary ---
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Parameter Batch coverage report: minimal set of run combinations "
               "needed to recover the taxonomically-identified best sequences"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(["Total run combinations tested (Parameter Batch)", n_runs])
    ws.append(["Total taxonomically-identified sequences", n_winners])
    ws.append(["Sequences covered by the greedy minimal set", len(covered_by_set)])
    ws.append(["Run combinations in the minimal set", len(chosen_runs)])
    ws.append(["Sequences not reproduced identically by any run folder", len(never_covered)])
    ws.append([])
    if run_hit_count:
        best_run = max(run_hit_count, key=lambda r: run_hit_count[r])
        worst_run = min(run_hit_count, key=lambda r: run_hit_count[r])
        ws.append(["Best single run", best_run, runs[best_run]["params_raw"], run_hit_count[best_run]])
        ws.append(["Worst single run", worst_run, runs[worst_run]["params_raw"], run_hit_count[worst_run]])
        ws.append(["Average sequences reproduced per single run",
                   round(sum(run_hit_count.values()) / n_runs, 1)])
    if never_covered:
        ws.append([])
        ws.append(["Samples not found identically in any run folder (consensus or "
                   "secondary variants) — e.g. run folders deleted or re-run after "
                   "the merge/BLAST steps:"])
        for s in never_covered:
            ws.append([s])
    autofit(ws, [65, 14, 55, 14])

    # --- Sheet 2: Minimal set of combinations ---
    ws2 = wb.create_sheet("Minimal_run_set")
    headers = (["Order", "Run", "Folder"] + param_keys +
               ["New_sequences_added", "Cumulative_sequences", "Pct_of_total"])
    ws2.append(headers)
    style_header(ws2, len(headers))
    cum = 0
    for i, (r, cov) in enumerate(chosen_runs, start=1):
        cum += len(cov)
        p = runs[r]["params"]
        ws2.append([i, r, os.path.basename(runs[r]["folder"])] +
                    [p.get(k, "") for k in param_keys] +
                    [len(cov), cum, round(100 * cum / n_winners, 1) if n_winners else 0])
    autofit(ws2, [8, 6, 26] + [16] * len(param_keys) + [20, 20, 12])
    ws2.freeze_panes = "A2"

    # --- Sheet 3: Per-sample detail ---
    ws3 = wb.create_sheet("Per_sample_detail")
    headers3 = ["Sample", "Tax_level", "Query_Order", "Query_Family", "Query_Genus", "Query_organism",
                "Hit_Order", "Hit_Family", "Hit_Genus", "Hit_organism",
                "N_runs_reproducing_sequence", "Runs_reproducing_it",
                "Covered_by_minimal_set", "Run_assigned_in_minimal_set"]
    ws3.append(headers3)
    style_header(ws3, len(headers3))
    for s in sorted(winners):
        rs = sorted(coverage[s])
        info = tax_info.get(s, {})
        ws3.append([
            s, info.get("Tax_level", ""), info.get("Query_Order", ""), info.get("Query_Family", ""),
            info.get("Query_Genus", ""), info.get("Query_organism", ""),
            info.get("Hit_Order", ""), info.get("Hit_Family", ""), info.get("Hit_Genus", ""),
            info.get("Hit_organism", ""),
            len(rs), ",".join(map(str, rs)),
            "Yes" if s in covered_by_set else "No",
            sample_assigned_run.get(s, ""),
        ])
    autofit(ws3, [14, 10, 14, 16, 14, 20, 14, 16, 14, 20, 12, 30, 18, 14])
    ws3.freeze_panes = "A2"

    # --- Sheet 4: full per-run coverage (context) ---
    ws4 = wb.create_sheet("All_runs_coverage")
    headers4 = (["Run", "Folder"] + param_keys +
                ["N_consensus_filtered_total", f"N_of_{n_winners}_reproduced", "Pct"])
    ws4.append(headers4)
    style_header(ws4, len(headers4))
    for r in sorted(runs):
        p = runs[r]["params"]
        ws4.append([r, os.path.basename(runs[r]["folder"])] +
                    [p.get(k, "") for k in param_keys] +
                    [runs[r]["n_consensus_filtered"], run_hit_count.get(r, 0),
                     round(100 * run_hit_count.get(r, 0) / n_winners, 1) if n_winners else 0])
    autofit(ws4, [8, 26] + [16] * len(param_keys) + [22, 20, 10])
    ws4.freeze_panes = "A2"

    wb.save(out_path)


def write_combo_cfg(cfg_path: str, runs: Dict[int, dict],
                    chosen_runs: List[Tuple[int, set]], n_winners: int,
                    batch_dir: str) -> bool:
    """Write the minimal set as a "# combos" batch config (one full
    combination per line, the format of the Parameters column). Returns
    False when there is nothing to write."""
    lines = [r for r, _cov in chosen_runs if runs[r]["params_raw"].strip()]
    if not lines:
        return False
    out = [
        "# combos",
        "#",
        f"# Minimal run set from {os.path.basename(os.path.normpath(batch_dir))}",
        "# (batch_coverage_report): the fewest combinations that together",
        "# reproduce the taxonomically-identified best sequences.",
        "# Parameters not listed take their value from the Parameters panel:",
        "# use the same base settings as the original batch (its",
        "# batch_state.json records them).",
        "",
    ]
    cum = 0
    for r, cov in chosen_runs:
        cum += len(cov)
        if not runs[r]["params_raw"].strip():
            continue
        pct = 100 * cum / n_winners if n_winners else 0
        out.append(f"# run {r}: +{len(cov)} sequence(s), cumulative {cum}/{n_winners} ({pct:.1f}%)")
        out.append(runs[r]["params_raw"].strip())
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return True


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_coverage_report(batch_dir: str, bestseq_dir: str,
                        output_root: Optional[str] = None,
                        fasta_name: str = FASTA_NAME_DEFAULT,
                        out_path: Optional[str] = None,
                        log: Callable[[str], None] = print) -> dict:
    """Run the whole analysis; progress goes to `log`. Returns
    {"xlsx", "cfg" ("" if not written), "n_runs", "n_winners", "n_chosen",
    "n_covered", "n_never"}."""
    batch_dir = os.path.abspath(batch_dir)
    bestseq_dir = os.path.abspath(bestseq_dir)
    output_root = os.path.abspath(output_root) if output_root else os.path.dirname(batch_dir)
    out_path = out_path or os.path.join(bestseq_dir, "parameter_batch_coverage_report.xlsx")

    log("Loading run combinations...")
    runs = load_runs(batch_dir, output_root)
    log(f"  {len(runs)} run combinations loaded")

    log("Locating Best Sequence Selection output...")
    bestseq_tsv = find_one(os.path.join(bestseq_dir, "bestseq-*.tsv"), "bestseq report .tsv", log)
    identified_fasta = find_one(os.path.join(bestseq_dir, "bestseq-*_identified.fasta"),
                                "bestseq _identified.fasta", log)

    tax_info = load_tax_info(bestseq_tsv)
    winners = {sample_of(h): (host_of(h), seq.upper())
               for h, seq in read_fasta(identified_fasta)}
    n_var = sum(1 for label in winners if _VARIANT_SUFFIX.search(label))
    log(f"  {len(winners)} taxonomically-identified sequences loaded"
        + (f" ({n_var} of them secondary variants)" if n_var else ""))

    # Discover parameter keys in the order they first appear, so the report
    # adapts to whichever parameters were actually swept.
    param_keys: List[str] = []
    for info in runs.values():
        for k in info["params"]:
            if k not in param_keys:
                param_keys.append(k)

    log("Comparing each run's consensus and secondary variants against the winning sequences...")
    coverage, run_hit_count = compute_coverage(winners, runs, fasta_name, log)

    log("Computing greedy minimal run set...")
    chosen_runs = greedy_set_cover(winners, coverage, runs)
    covered = sum(len(c) for _r, c in chosen_runs)
    log(f"  {len(chosen_runs)} runs needed to cover {covered}/{len(winners)} sequences")

    log(f"Writing report to {out_path}")
    build_workbook(out_path, runs, winners, tax_info, coverage, run_hit_count,
                   chosen_runs, param_keys)
    cfg_path = os.path.join(os.path.dirname(out_path), CFG_NAME)
    if write_combo_cfg(cfg_path, runs, chosen_runs, len(winners), batch_dir):
        log(f"Minimal run set as a batch config: {cfg_path}")
    else:
        cfg_path = ""
    log("Done.")
    return {"xlsx": out_path, "cfg": cfg_path, "n_runs": len(runs),
            "n_winners": len(winners), "n_chosen": len(chosen_runs),
            "n_covered": covered, "n_never": len(winners) - covered}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--batch-dir", required=True,
                     help="Parameter Batch output folder (contains batch_run_summary.tsv)")
    ap.add_argument("--bestseq-dir", required=True,
                     help="Best Sequence Selection output folder (contains bestseq-<date>.tsv "
                          "and bestseq-<date>_identified.fasta)")
    ap.add_argument("--output-root", default=None,
                     help="Folder containing the run folders referenced by batch_run_summary.tsv "
                          "(default: parent folder of --batch-dir)")
    ap.add_argument("--fasta-name", default=FASTA_NAME_DEFAULT,
                     help=f"Per-run FASTA to compare against (default: {FASTA_NAME_DEFAULT})")
    ap.add_argument("--output", default=None,
                     help="Output .xlsx path (default: <bestseq-dir>/parameter_batch_coverage_report.xlsx)")
    args = ap.parse_args()
    run_coverage_report(args.batch_dir, args.bestseq_dir, args.output_root,
                        args.fasta_name, args.output,
                        log=lambda msg: print(msg, file=sys.stderr if msg.lstrip().startswith(
                            ("Warning", "Note")) else sys.stdout))


if __name__ == "__main__":
    main()
