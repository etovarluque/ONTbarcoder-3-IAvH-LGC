#!/usr/bin/env python3
"""
orf_trim_fasta.py — Trims the barcodes in a FASTA file down to their coding ORF,
removing the gene's stop codon and the non-coding 3' tail (same logic as
ONTbarcoder's phases 2a/2b/3). Useful for bringing a non-Coding file
(full-length barcodes, e.g. CytB 1062 bp with stop) down to the same coding
region produced by coding-marker mode (e.g. 1032 bp), so that both files are
comparable in the Compare panel (which uses global NW alignment and therefore
penalizes length differences).

Usage:
    python orf_trim_fasta.py input.fa output.fa --gencode 2
    python orf_trim_fasta.py input.fa output.fa --gencode 2 --min-coverage 0.95

- If a sequence already translates with no internal stop (e.g. Folmer COI) it is
  left untouched.
- If after trimming the ORF covers < min-coverage of the original, the sequence
  is written UNTRIMMED and flagged in the log (possible NUMT/pseudogene or
  unexpected length): information is never silently lost.
- The length field in the header (>id;LEN;...) is updated to the new value.
"""
import sys
import argparse
import warnings
warnings.simplefilter("ignore")  # silence Biopython's "Partial codon" warning
from Bio.Seq import Seq


def orf_trim(seq, gencode):
    """Returns (trimmed_seq, n_aa). Longest in-frame ORF with no internal stop;
    if it already translates cleanly it is returned unchanged; if there is an
    internal stop plus a tail, the stop and everything 3' of it are trimmed."""
    s = seq.replace("-", "").upper()
    if not s:
        return "", 0
    frames = [s, s[1:], s[2:],
              Seq(s).reverse_complement().__str__(),
              Seq(s[:-1]).reverse_complement().__str__(),
              Seq(s[:-2]).reverse_complement().__str__()]
    best_fs, best_aa = s, 0
    for fs in frames:
        aa = Seq(fs).translate(table=gencode, to_stop=True).__str__()
        if len(aa) > best_aa:
            best_aa, best_fs = len(aa), fs
    if best_aa * 3 >= len(s) - 2:
        return s, best_aa
    return best_fs[:best_aa * 3], best_aa


def read_fasta(path):
    recs = []
    hdr, parts = None, []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith(">"):
                if hdr is not None:
                    recs.append((hdr, "".join(parts)))
                hdr, parts = line[1:], []
            elif hdr is not None:
                parts.append(line.strip())
    if hdr is not None:
        recs.append((hdr, "".join(parts)))
    return recs


def update_len_field(hdr, newlen):
    """Updates the 2nd field (length) of an ONTbarcoder-style header
    '>id;LEN;cov;...'. If the header doesn't match that format, it is left as-is."""
    parts = hdr.split(";")
    if len(parts) > 1 and parts[1].strip().isdigit():
        parts[1] = str(newlen)
        return ";".join(parts)
    return hdr


def main():
    ap = argparse.ArgumentParser(description="Trim barcodes to their coding ORF.")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--gencode", type=int, required=True,
                    help="NCBI table (2=vert. mt, 5=invert. mt, etc.)")
    ap.add_argument("--min-coverage", type=float, default=0.95,
                    help="Minimum ORF fraction required to trim (default 0.95).")
    args = ap.parse_args()

    recs = read_fasta(args.input)
    n_trim = n_keep = n_skip = 0
    out = []
    for hdr, seq in recs:
        s = seq.replace("-", "").upper()
        if not s:
            continue
        orf, aalen = orf_trim(s, args.gencode)
        if orf != s and aalen * 3 >= len(s) * args.min_coverage:
            out.append((update_len_field(hdr, len(orf)), orf))
            n_trim += 1
        elif orf == s:
            out.append((hdr, s))
            n_keep += 1
        else:
            # ORF too short: don't trim (possible NUMT/unusual length).
            out.append((hdr, s))
            n_skip += 1
            sys.stderr.write(
                f"  [warning] {hdr.split(';')[0]}: clean ORF covers only "
                f"{aalen*3}/{len(s)} bp (<{args.min_coverage:.0%}); left intact.\n")

    with open(args.output, "w", encoding="utf-8") as f:
        for hdr, seq in out:
            f.write(f">{hdr}\n{seq}\n")

    lens = {}
    for _, seq in out:
        lens[len(seq)] = lens.get(len(seq), 0) + 1
    print(f"Processed: {len(out)} barcodes")
    print(f"  trimmed to ORF     : {n_trim}")
    print(f"  unchanged (clean)  : {n_keep}")
    print(f"  not trimmed (short ORF): {n_skip}")
    print(f"  output length distribution: "
          f"{dict(sorted(lens.items(), key=lambda x:-x[1])[:6])}")
    print(f"Written: {args.output}")


if __name__ == "__main__":
    main()
