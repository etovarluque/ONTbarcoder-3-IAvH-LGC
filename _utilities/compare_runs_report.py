"""Standalone A/B comparison of two ONTbarcoder3 run folders, per sample.

For any two runs of the same dataset: two combinations of a Parameter Sweep
(picked by run number from its batch_run_summary.tsv), or two analyses run by
hand (e.g. the current version vs. a run made with an older one).

For every sample it reports, in each run:
  - whether it produced a QC-compliant barcode (consensus_no_errors.fa) and a
    filtered one (consensus_filtered.fa), and whether the sequence is identical;
  - the secondary variants exported (secondary_variants.fa): count and the
    highest divergence vs. the dominant.
and classifies the change (barcode gained / lost / changed, mixture newly
flagged / no longer flagged).

Writes a per-sample TSV (only samples that differ, unless --all) and prints a
summary. No Qt/PyQt import: runnable from the command line, and used by the
"Compare two runs" section of the Parameter Sweep panel (list_batch_runs /
compare_runs).

Usage:
    python compare_runs_report.py --batch-dir <..._sweep folder> [--runs 1 2]
    python compare_runs_report.py --run-a <run folder> --run-b <run folder>
Options: --label-a/--label-b, --output <file.tsv>, --all
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Dict, List, Tuple


def read_barcodes(path: str) -> Dict[str, str]:
    """{sample: sequence} from a consensus FASTA (header '>{sample}_all.fa;...')."""
    out: Dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    name, parts = None, []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(">"):
                if name is not None:
                    out[name] = "".join(parts).upper()
                name = line[1:].split(";")[0].replace("_all.fa", "")
                parts = []
            elif name is not None:
                parts.append(line)
    if name is not None:
        out[name] = "".join(parts).upper()
    return out


def _parse_div(fields: List[str]) -> float:
    """Divergence fraction from a 'div=X%' field; nan if absent, NA or malformed."""
    for f in fields:
        if f.startswith("div="):
            try:
                return float(f[4:].rstrip("%")) / 100.0
            except ValueError:
                return float("nan")
    return float("nan")


def read_variants(path: str) -> Dict[str, List[float]]:
    """{sample: [divergence fraction or nan, ...]} from secondary_variants.fa
    (header '>{sample}_var{i};type=raw|corrected;frac=..;div=X%;...').

    One entry per variant NAME ({sample}_var{i}): a variant is usually written
    twice (type=raw, then its type=corrected copy), but one whose raw consensus
    had too many Ns to export appears ONLY as type=corrected — counting raw
    records alone would miss it. The raw record's divergence is preferred; the
    corrected one is the fallback."""
    out: Dict[str, List[float]] = {}
    if not os.path.isfile(path):
        return out
    by_name: Dict[str, float] = {}   # variant name -> divergence (insertion-ordered)
    has_raw: set = set()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith(">"):
                continue
            fields = line[1:].strip().split(";")
            name = fields[0]
            is_raw = "type=corrected" not in fields
            if name in by_name and (name in has_raw or not is_raw):
                continue   # keep the first raw record, else the first seen
            by_name[name] = _parse_div(fields[1:])
            if is_raw:
                has_raw.add(name)
    for name, div in by_name.items():
        out.setdefault(name.rsplit("_var", 1)[0], []).append(div)
    return out


def load_run(folder: str) -> dict:
    return {"qc": read_barcodes(os.path.join(folder, "consensus_no_errors.fa")),
            "filt": read_barcodes(os.path.join(folder, "consensus_filtered.fa")),
            "var": read_variants(os.path.join(folder, "secondary_variants.fa"))}


def list_batch_runs(batch_dir: str) -> List[Tuple[int, str, str]]:
    """[(run number, run folder, label), ...] from a batch's
    batch_run_summary.tsv. The run folders sit next to the batch folder
    (both in output/). Raises FileNotFoundError / ValueError."""
    summary = os.path.join(batch_dir, "batch_run_summary.tsv")
    if not os.path.isfile(summary):
        raise FileNotFoundError(f"batch_run_summary.tsv not found in {batch_dir}")
    root = os.path.dirname(os.path.abspath(batch_dir))
    out = []
    with open(summary, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            try:
                n = int(row["Run"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append((n, os.path.join(root, "ont-barcoder_" + row.get("Folder", "")),
                        f"run{n} ({row.get('Parameters', '')})"))
    if not out:
        raise ValueError(f"No runs listed in {summary}")
    return out


def runs_from_batch(batch_dir: str, run_a: int, run_b: int
                    ) -> Tuple[str, str, str, str]:
    """(folder_a, label_a, folder_b, label_b) from batch_run_summary.tsv."""
    try:
        runs = {n: (folder, label) for n, folder, label in list_batch_runs(batch_dir)}
    except (OSError, ValueError) as e:
        sys.exit(str(e))
    for r in (run_a, run_b):
        if r not in runs:
            sys.exit(f"Run {r} not listed in {batch_dir}")
    return runs[run_a] + runs[run_b]


def classify(sample: str, A: dict, B: dict) -> List[str]:
    cats = []
    for kind, tag in (("qc", "QC"), ("filt", "filtered")):
        sa, sb = A[kind].get(sample), B[kind].get(sample)
        if sa and not sb:
            cats.append(f"{tag} barcode only in A")
        elif sb and not sa:
            cats.append(f"{tag} barcode only in B")
        elif sa and sb and sa != sb:
            cats.append(f"{tag} barcode differs")
    va, vb = A["var"].get(sample, []), B["var"].get(sample, [])
    if vb and not va:
        cats.append("variants only in B")
    elif va and not vb:
        cats.append("variants only in A")
    elif len(va) != len(vb):
        cats.append("variant count differs")
    return cats


def _maxdiv(divs: List[float]) -> str:
    vals = [d for d in divs if d == d]
    return f"{max(vals) * 100:.1f}" if vals else ("NA" if divs else "")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--batch-dir", help="Parameter Sweep output folder "
                    "(contains batch_run_summary.tsv)")
    ap.add_argument("--runs", nargs=2, type=int, default=[1, 2], metavar=("A", "B"),
                    help="Run numbers to compare from the batch (default: 1 2)")
    ap.add_argument("--run-a", help="Run folder A (instead of --batch-dir)")
    ap.add_argument("--run-b", help="Run folder B (instead of --batch-dir)")
    ap.add_argument("--label-a", default=None)
    ap.add_argument("--label-b", default=None)
    ap.add_argument("--output", default=None, help="Per-sample TSV "
                    "(default: compare_runs_report.tsv in the batch/run-A folder)")
    ap.add_argument("--all", action="store_true",
                    help="List every sample in the TSV, not only those that differ")
    args = ap.parse_args()

    if args.batch_dir:
        fa, la, fb, lb = runs_from_batch(args.batch_dir, *args.runs)
        default_out = os.path.join(args.batch_dir, "compare_runs_report.tsv")
    elif args.run_a and args.run_b:
        fa, fb = args.run_a, args.run_b
        la, lb = os.path.basename(fa.rstrip("/\\")), os.path.basename(fb.rstrip("/\\"))
        default_out = os.path.join(fa, "compare_runs_report.tsv")
    else:
        ap.error("give --batch-dir, or both --run-a and --run-b")
    la = args.label_a or la
    lb = args.label_b or lb
    for f in (fa, fb):
        if not os.path.isdir(f):
            sys.exit(f"Run folder not found: {f}")
    print("\n".join(compare_runs(fa, fb, la, lb, args.output or default_out, args.all)))


def compare_runs(fa: str, fb: str, la: str, lb: str, out_path: str,
                 all_samples: bool = False) -> List[str]:
    """Compare run folders A and B, write the per-sample TSV to `out_path`
    (only samples that differ unless `all_samples`) and return the summary
    as text lines."""
    A, B = load_run(fa), load_run(fb)
    samples = sorted(set().union(*(A[k].keys() | B[k].keys() for k in A)))

    counts: Dict[str, int] = {}
    n_diff = 0
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["Sample", "QC_A", "QC_B", "Filtered_A", "Filtered_B",
                    "Same_filtered_seq", "N_variants_A", "N_variants_B",
                    "Max_div_A_%", "Max_div_B_%", "Changes"])
        for s in samples:
            cats = classify(s, A, B)
            for c in cats:
                counts[c] = counts.get(c, 0) + 1
            if cats:
                n_diff += 1
            if not cats and not all_samples:
                continue
            fa_s, fb_s = A["filt"].get(s), B["filt"].get(s)
            w.writerow([s, "yes" if s in A["qc"] else "no",
                        "yes" if s in B["qc"] else "no",
                        "yes" if fa_s else "no", "yes" if fb_s else "no",
                        ("yes" if fa_s == fb_s else "no") if fa_s and fb_s else "",
                        len(A["var"].get(s, [])), len(B["var"].get(s, [])),
                        _maxdiv(A["var"].get(s, [])), _maxdiv(B["var"].get(s, [])),
                        "; ".join(cats)])

    lines = [f"A = {la}", f"    {fa}", f"B = {lb}", f"    {fb}", "",
             f"{'':32s}{'A':>8s}{'B':>8s}"]
    for tag, key in (("QC-compliant barcodes", "qc"), ("Filtered barcodes", "filt")):
        lines.append(f"{tag:32s}{len(A[key]):8d}{len(B[key]):8d}")
    lines.append(f"{'Samples with secondary variants':32s}{len(A['var']):8d}{len(B['var']):8d}")
    lines.append(f"{'Secondary variants (total)':32s}"
                 f"{sum(map(len, A['var'].values())):8d}{sum(map(len, B['var'].values())):8d}")
    lines += ["", f"Samples compared: {len(samples)}   with any difference: {n_diff}"]
    for c in sorted(counts):
        lines.append(f"  {c:36s}{counts[c]:6d}")
    lines += ["", f"Per-sample detail: {out_path}"]
    return lines


if __name__ == "__main__":
    main()
