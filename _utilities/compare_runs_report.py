"""Standalone A/B comparison of two ONTbarcoder3 run folders, per sample.

Built to evaluate the coarse pre-partition of intra-sample variants
(resolve_mixed.coarse_partition = true vs false, see
_profiles/coarse_partition_ab.cfg), but works for any two runs of the same
dataset (e.g. the current version vs. a run made with an older one).

For every sample it reports, in each run:
  - whether it produced a QC-compliant barcode (consensus_no_errors.fa) and a
    filtered one (consensus_filtered.fa), and whether the sequence is identical;
  - the secondary variants exported (secondary_variants.fa): count and the
    highest divergence vs. the dominant.
and classifies the change (barcode gained / lost / changed, mixture newly
flagged / no longer flagged).

Writes a per-sample TSV (only samples that differ, unless --all) and prints a
summary. No Qt/PyQt import: runnable from the command line.

Usage:
    python compare_runs_report.py --batch-dir <..._batch folder> [--runs 1 2]
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


def read_variants(path: str) -> Dict[str, List[float]]:
    """{sample: [divergence fraction or nan, ...]} from secondary_variants.fa
    (header '>{sample}_var{i};frac=..;len=..;div=X%;translates=..')."""
    out: Dict[str, List[float]] = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith(">"):
                continue
            fields = line[1:].strip().split(";")
            sample = fields[0].rsplit("_var", 1)[0]
            div = float("nan")
            for f in fields[1:]:
                if f.startswith("div=") and f[4:].rstrip("%") not in ("", "NA"):
                    div = float(f[4:].rstrip("%")) / 100.0
            out.setdefault(sample, []).append(div)
    return out


def load_run(folder: str) -> dict:
    return {"qc": read_barcodes(os.path.join(folder, "consensus_no_errors.fa")),
            "filt": read_barcodes(os.path.join(folder, "consensus_filtered.fa")),
            "var": read_variants(os.path.join(folder, "secondary_variants.fa"))}


def runs_from_batch(batch_dir: str, run_a: int, run_b: int
                    ) -> Tuple[str, str, str, str]:
    """(folder_a, label_a, folder_b, label_b) from batch_run_summary.tsv."""
    summary = os.path.join(batch_dir, "batch_run_summary.tsv")
    if not os.path.isfile(summary):
        sys.exit(f"batch_run_summary.tsv not found in {batch_dir}")
    root = os.path.dirname(os.path.abspath(batch_dir))
    rows = {}
    with open(summary, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            rows[int(row["Run"])] = row
    for r in (run_a, run_b):
        if r not in rows:
            sys.exit(f"Run {r} not listed in {summary}")
    a, b = rows[run_a], rows[run_b]
    return (os.path.join(root, "ont-barcoder_" + a["Folder"]),
            f"run{run_a} ({a['Parameters']})",
            os.path.join(root, "ont-barcoder_" + b["Folder"]),
            f"run{run_b} ({b['Parameters']})")


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
    ap.add_argument("--batch-dir", help="Parameter Batch output folder "
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

    A, B = load_run(fa), load_run(fb)
    samples = sorted(set().union(*(A[k].keys() | B[k].keys() for k in A)))
    out_path = args.output or default_out

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
            if not cats and not args.all:
                continue
            fa_s, fb_s = A["filt"].get(s), B["filt"].get(s)
            w.writerow([s, "yes" if s in A["qc"] else "no",
                        "yes" if s in B["qc"] else "no",
                        "yes" if fa_s else "no", "yes" if fb_s else "no",
                        ("yes" if fa_s == fb_s else "no") if fa_s and fb_s else "",
                        len(A["var"].get(s, [])), len(B["var"].get(s, [])),
                        _maxdiv(A["var"].get(s, [])), _maxdiv(B["var"].get(s, [])),
                        "; ".join(cats)])

    print(f"A = {la}\n    {fa}\nB = {lb}\n    {fb}\n")
    print(f"{'':32s}{'A':>8s}{'B':>8s}")
    for tag, key in (("QC-compliant barcodes", "qc"), ("Filtered barcodes", "filt")):
        print(f"{tag:32s}{len(A[key]):8d}{len(B[key]):8d}")
    print(f"{'Samples with secondary variants':32s}{len(A['var']):8d}{len(B['var']):8d}")
    print(f"{'Secondary variants (total)':32s}"
          f"{sum(map(len, A['var'].values())):8d}{sum(map(len, B['var'].values())):8d}")
    print(f"\nSamples compared: {len(samples)}   with any difference: {n_diff}")
    for c in sorted(counts):
        print(f"  {c:36s}{counts[c]:6d}")
    print(f"\nPer-sample detail: {out_path}")


if __name__ == "__main__":
    main()
