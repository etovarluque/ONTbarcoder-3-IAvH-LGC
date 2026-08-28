# ONTbarcoder v3

Desktop tool for demultiplexing and analysis of **Oxford Nanopore (ONT)** reads
using DNA barcodes.

This is a fork / version-3 rewrite of **ONTbarcoder 2.0**
(Srivathsan et al. 2024, *Cladistics* 40: 192–203,
<https://doi.org/10.1111/cla.12566>). The barcode-calling algorithm is unchanged;
version 3 rebuilds the interface, ports the pipeline to Python 3 with
deterministic multiprocessing, and adds a set of analysis tools (BLAST, FASTA
Compare, FASTA Tools, FASTQ Inspector, Notes) plus a non-coding marker mode.

See [`CHANGELOG.md`](CHANGELOG.md) for the full list of differences from the
original.

## Releases (ready-to-run executables)

Prebuilt bundles are published on the **[Releases](../../releases)** page — no
Python install needed:

| Platform | Asset                        |
| -------- | ---------------------------- |
| Windows  | `ONTbarcoder3.zip`           |
| Linux    | `ONTbarcoder3_linux.tar.gz`  |

Unpack and run the `ONTbarcoder3` executable.

## Running from source

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    |    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python ONTbarcoder3.py
```

Requires Python 3.11+ (tested on 3.13). For real-time barcoding you also need
**Dorado** and the **POD5** tooling installed and on `PATH`.

## Project layout

```
ONTbarcoder3.py          Main application / GUI
_utilities/              Worker pipeline + tool panels
  ONTbarcoder3_multiprocessing.py   Deterministic multiprocessing pipeline
  blast_panel.py / compare_panel.py / fasta_tools.py /
  fastq_inspector.py / notes_panel.py / shared.py
  orf_trim_fasta.py      CLI: trim coding barcodes to their ORF
_mafftfiles/             Bundled MAFFT (disttbfast) + parameter files
_profiles/               Saved parameter profiles + BLAST config (git-ignored)
_notes/                  Markdown notes shown in the Notes panel
guide/                   User manual (HTML) + screenshots
icon.ico                 Application icon
```


## Credits

- **Original software:** Amrita Srivathsan, V. Feng, D. Suárez, B. Emerson &
  R. Meier — *ONTbarcoder 2.0*.
- **Version 3 development:** Eduardo Tovar Luque — Instituto Humboldt, 2026.

## License

Licensed under the **GNU General Public License v3.0** — see [`LICENSE`](LICENSE).
As a derivative of ONTbarcoder 2.0, this fork keeps the upstream copyleft terms.
