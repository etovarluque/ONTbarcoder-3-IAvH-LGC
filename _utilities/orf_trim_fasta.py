#!/usr/bin/env python3
"""
orf_trim_fasta.py — Recorta los barcodes de un FASTA a su ORF codificante,
eliminando el codón de paro del gen y la cola 3' no codificante (misma lógica
que las fases 2a/2b/3 de ONTbarcoder). Sirve para llevar un archivo non-Coding
(barcodes de longitud completa, p. ej. CytB 1062 pb con stop) a la misma región
codificante que produce el modo marcador-codificante (p. ej. 1032 pb), de modo
que ambos archivos sean comparables en el panel Compare (que usa alineamiento
global NW y, por tanto, penaliza diferencias de longitud).

Uso:
    python orf_trim_fasta.py entrada.fa salida.fa --gencode 2
    python orf_trim_fasta.py entrada.fa salida.fa --gencode 2 --min-coverage 0.95

- Si una secuencia ya traduce sin paro interno (p. ej. COI Folmer) se deja intacta.
- Si tras recortar el ORF cubre < min-coverage del original, la secuencia se
  escribe SIN recortar y se marca en el log (posible NUMT/pseudogén o longitud
  inesperada): así nunca se pierde información de forma silenciosa.
- El campo de longitud del encabezado (>id;LEN;...) se actualiza al nuevo valor.
"""
import sys
import argparse
import warnings
warnings.simplefilter("ignore")  # silencia el aviso "Partial codon" de Biopython
from Bio.Seq import Seq


def orf_trim(seq, gencode):
    """Devuelve (seq_recortada, n_aa). ORF en marco más largo sin paro interno;
    si ya traduce limpio se devuelve intacto; si hay paro interno + cola se recorta
    el paro y todo lo 3' de él."""
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
    """Actualiza el 2º campo (longitud) de un encabezado estilo ONTbarcoder
    '>id;LEN;cov;...'. Si no tiene ese formato, deja el encabezado igual."""
    parts = hdr.split(";")
    if len(parts) > 1 and parts[1].strip().isdigit():
        parts[1] = str(newlen)
        return ";".join(parts)
    return hdr


def main():
    ap = argparse.ArgumentParser(description="Recorta barcodes al ORF codificante.")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--gencode", type=int, required=True,
                    help="Tabla NCBI (2=vert. mt, 5=invert. mt, etc.)")
    ap.add_argument("--min-coverage", type=float, default=0.95,
                    help="Fracción mínima del ORF para recortar (default 0.95).")
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
            # ORF demasiado corto: no recortar (posible NUMT/longitud rara).
            out.append((hdr, s))
            n_skip += 1
            sys.stderr.write(
                f"  [aviso] {hdr.split(';')[0]}: ORF limpio cubre solo "
                f"{aalen*3}/{len(s)} pb (<{args.min_coverage:.0%}); se deja intacto.\n")

    with open(args.output, "w", encoding="utf-8") as f:
        for hdr, seq in out:
            f.write(f">{hdr}\n{seq}\n")

    lens = {}
    for _, seq in out:
        lens[len(seq)] = lens.get(len(seq), 0) + 1
    print(f"Procesados: {len(out)} barcodes")
    print(f"  recortados al ORF : {n_trim}")
    print(f"  sin cambio (limpio): {n_keep}")
    print(f"  no recortados (ORF corto): {n_skip}")
    print(f"  distribución de longitudes de salida: "
          f"{dict(sorted(lens.items(), key=lambda x:-x[1])[:6])}")
    print(f"Escrito: {args.output}")


if __name__ == "__main__":
    main()
