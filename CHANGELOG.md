# Changelog

All notable changes to this fork of **ONTbarcoder** are documented here.

This project is a derivative work of **ONTbarcoder 2.0** by Srivathsan, Feng,
Suárez, Emerson & Meier (Srivathsan et al. 2024, *Cladistics* 40: 192–203,
<https://doi.org/10.1111/cla.12566>). The "original" baseline referenced below
is the upstream source kept locally in `original/` (not tracked in this
repository):

| Baseline file                        | Role                                   |
| ------------------------------------ | -------------------------------------- |
| `original/ONTbarcoder2.py`           | Monolithic PyQt5 GUI (~7 000 lines)    |
| `original/ONTbarcoder_multiprocessing.py` | Worker/pipeline module (~4 200 lines, Python 2) |

---

## [3.1b] — 2026 · "base version"

Full rewrite of the desktop application. The barcoding algorithm and its
results are unchanged by design; everything around it — the interface, the
process model, the packaging and a set of new analysis tools — is new.

### Added

#### New GUI (complete rebuild)
- Replaced the single tabbed `OptWindow` with a `QMainWindow` + left **sidebar
  navigation** split into two groups:
  - **Workflow:** Input files · Parameters · Progress · 📈 RT Charts · Results
  - **Utilities:** FASTA Compare · FASTA Tools · FASTQ Inspector · BLAST · Notes
- Panel-based architecture (`SetupPanel`, `ParamsPanel`, `ProgressPanel`,
  `ResultsPanel`, `LiveChartPanel`, …) instead of stacked widgets.
- Embedded stylesheet and theme constants — the external `stylesheet.qss` file
  is no longer required.
- **Automatic UI scaling:** the window measures the available desktop area
  (DPI-aware, taskbar excluded) and picks the largest scale at which the
  1280×… design canvas still fits, floored for readability. One build now
  adapts to any screen resolution — no more hand-editing `UI_SCALE` per monitor
  (`UI_FIT_SCREEN`, `UI_MAX_SCALE`, `UI_MIN_SCALE`).
- Drag-and-drop zones (`DropZone`, `PathDropLineEdit`) for input files/folders.
- `About` dialog crediting the original authors and the version-3 development.
- Real-time progress view with per-phase rows (`PhaseRow`) and summary
  stat cards (`StatCard`).

#### Charts (custom, dependency-free)
- All plotting rewritten with `QPainter` widgets; **`pyqtgraph` dependency
  removed**.
- Real-time barcode-yield charts (`_ChartWidget`, `LiveChartPanel`) with a
  detachable window (`DetachedChartsWindow`).
- Per-sample bar chart (`_SampleBarChartWidget`, `SampleBarChartWindow`).
- Charts render to a scaled pixmap for crisp PDF/PNG export.

#### New analysis tools (`_utilities/`)
- **BLAST panel** (`blast_panel.py`): remote NCBI BLAST of the resulting
  barcodes (`core_nt` / `nt` / `refseq_rna` / `16S`), blastn + MEGABLAST,
  organism/taxonomy lookup, live log, optional XLSX export of hits (lazy
  `openpyxl`). API key stored in `_profiles/blast_config.json`.
- **FASTA Compare panel** (`compare_panel.py`): pairwise / multi-set comparison
  of barcode FASTA files with global (NW) alignment, IUPAC-aware matching and a
  colour-coded results window (Identical / Compatible / Different).
- **FASTA Tools panel** (`fasta_tools.py`): batch FASTA utilities
  (inspect / filter / reformat) with worker thread and XLSX reports.
- **FASTQ Inspector panel** (`fastq_inspector.py`): read-length and quality
  histograms drawn with `QPainter`, PDF export.
- **Notes panel** (`notes_panel.py`): file-based Markdown notebook stored in
  `_notes/`; colleagues can drop `.md`/`.txt` files in and they are
  auto-detected. Ships with marker-specific protocol notes (Cytb, rbcL, ITS, …).
- **`orf_trim_fasta.py`** CLI: trims full-length coding barcodes to their ORF
  (stop codon + 3′ tail removed) so non-coding-mode output becomes comparable
  with coding-mode output; never drops data silently (flags possible
  NUMT/pseudogene when ORF coverage drops).

#### Non-coding marker mode
- **"Non-Coding marker" checkbox** (ITS, trnL, 16S, 12S, …): disables genetic
  code validation and skips phases 2b and 3, producing barcodes from phase 2a.
  Recommended workflow for markers of unknown fragment length is documented in
  `_notes/Nuevos_barcodes.md`.

#### Parameter profiles
- Named parameter profiles (`_profiles/`) — save/load the full parameter set
  per marker.

#### Packaging
- `ONTbarcoder3.spec` (PyInstaller, one-folder, windowed) — *not tracked;*
  see below.
- Bundled MAFFT `disttbfast.exe` + `parfile` + `_aamtx` moved to `_mafftfiles/`
  and copied next to the executable at build time.
- Windows and Linux executable releases are published as **GitHub Release
  assets** (`ONTbarcoder3.zip`, `ONTbarcoder3_linux.tar.gz`), built from
  `dist/` — which is not tracked.

### Changed

- **Ported to Python 3.** `original/ONTbarcoder_multiprocessing.py` was Python 2
  (`print` statements, `dict.iteritems()`, `subprocess32`); the worker module is
  now `_utilities/ONTbarcoder3_multiprocessing.py`, pure Python 3.
- **Deterministic parallelism.** The worker module was reworked so the
  multiprocessing pipeline yields results *identical* to the original
  single-process run and identical between conventional and real-time mode:
  - `deterministic_sort()` / `resolve_ties_by_name()` for stable ordering,
  - explicit tie-breaking by sequence name,
  - isolated per-worker temp directories with lifecycle management
    (`_get_worker_tmpdir`, `cleanup_worker_tmpdirs`, `_reset_worker_tmpdir`),
  - `_run_disttbfast()` wrapper with timeout around the bundled MAFFT binary.
- **Worker-count autodetection:** `physical_core_count()` /
  `optimal_worker_count()` size the pool to physical cores.
- Monolithic script split: reusable logic extracted from the GUI file into
  `_utilities/` (`shared.py` holds common constants/widgets/path helpers).
- `_utilities/shared.py::_get_base_dir()` resolves paths correctly both frozen
  (next to the executable) and from source (project root).
- Main entry file renamed `ONTbarcoder2.py` → `ONTbarcoder3.py`; worker file
  `ONTbarcoder_multiprocessing.py` → `_utilities/ONTbarcoder3_multiprocessing.py`.
- Application version string set to `3.1b`.
- Internationalization scaffolding (`_tr()` / `retranslateUi()`) added
  throughout the UI.

### Removed

- **Remote / SSH real-time sequencing** over the network: the `paramiko`
  SSH client and the `liveremotedetector` / `liveremotetransfer` workers are
  gone. Real-time mode now runs against a local run directory only.
- **Guppy basecaller support** — replaced entirely by **Dorado**.
- **FAST5 input** — replaced by **POD5** (matching Dorado / R10.4).
- `pyqtgraph` dependency (custom `QPainter` charts).
- `subprocess32` dependency (Python 3 `subprocess`).
- `seqpy` dependency.
- `Bio.Entrez` usage in the core (NCBI access now confined to the BLAST panel).
- External `stylesheet.qss` file.

### Notes

- The barcode-calling algorithm (primer detection, length filtering, phases
  1 / 2a / 2b / 3, consensus by length and by genetic code) is deliberately
  unchanged from ONTbarcoder 2.0. This release is a **UI + engineering** update.
- `build/`, `dist/`, `output/`, `*.spec` and `original/` are intentionally
  excluded from version control (see `.gitignore`).
