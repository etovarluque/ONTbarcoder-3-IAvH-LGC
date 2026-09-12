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

## [3.4b] — 2026-09

Adds a way to explore several values of a few parameters without re-running
the analysis by hand each time. The analysis pipeline itself is untouched:
a batch with an empty `.cfg` reproduces a normal single run exactly.

### Added

#### Parameter Batch utility (sidebar, right below Parameters — Optional)
- New **"Parameter Batch"** panel (`_utilities/batch_sweep_panel.py`,
  `_utilities/batch_sweep.py`) that runs the currently loaded dataset once per
  combination of a parameter grid — e.g. several *Main consensus calling
  frequency* / *Min. secondary variant fraction* / *Variant tolerance* values —
  instead of changing them by hand and re-running each time. Conventional
  analysis only; locked in the sidebar until a dataset is loaded, and shown in
  italics to mark it as optional rather than a required Workflow step.
- The grid is defined in a plain-text `.cfg` (`key = v1, v2, v3` per line, `#`
  comments): only the parameters listed are overridden, on top of whatever is
  currently set in the Parameters panel — so a sweep of 3 parameters needs 3
  lines, not a full copy of every setting. `_profiles/ontbarcoder_batch.cfg`
  ships as a fully-documented template (every sweepable parameter, its
  default, valid range and an example), reachable from the panel's **"Create
  example cfg"** button.
- Every key and value is validated against an explicit range table (mirroring
  each parameter's GUI spinbox bounds) as soon as the `.cfg` is loaded —
  unknown keys and out-of-range values are rejected with the offending line
  before anything runs, rather than surfacing as a confusing failure mid-batch.
  `non_coi` is deliberately not sweepable (it derives the genetic code and
  which phases exist; set it in the Parameters panel instead). Listing any
  `resolve_mixed.*` key turns intra-sample variant detection on for the whole
  batch even if the panel checkbox is off — otherwise every combination would
  be identical — logged explicitly so it is never a silent surprise.
- Each combination runs as an ordinary `ont-barcoder_*_conv` folder, with its
  own self-documenting log recording the exact parameters used — indistinguishable
  from a manual run except for how it was launched. A batch of more than 15
  combinations shows a visible warning (each one is a full analysis); above
  200 the app asks for confirmation before starting anything.
- Once every combination finishes (or the batch is stopped), three files are
  written to the batch's own output folder, all identifying runs by a short
  run number (1, 2, 3…, run order) instead of the full timestamped folder
  name — readable even for a 40+-combination sweep:
  - `batch_run_summary.tsv` — one row **per analysis**: the parameters
    overridden for that combination and how many sequences its own
    `consensus_filtered.fa` produced.
  - `unique_consensus_filtered.fasta` — every completed run's
    `consensus_filtered.fa` merged into one FASTA: a sample whose sequence is
    identical in every run appears once; a sample with different sequences
    across runs gets one entry per distinct variant, header suffixed with the
    run number that produced it (`;run3`).
  - `batch_dedup_report.tsv` — one row per sample: how many distinct variants,
    in how many runs (`N_runs_present`/`N_runs_total`, two plain numbers, not
    `18/40`), and which run numbers (`Runs`, compressed as ranges, e.g.
    `1..12,15,20..24`) — cross-referenced against `batch_run_summary.tsv` for
    their parameters. Both choices sidestep a real Excel quirk: a value like
    `18/40` or `1-9` is read back as a date when the `.tsv` is opened
    directly, so the counts are split into two columns and ranges use `..`
    instead of `-`.

  BLAST and Best Sequence stay manual steps, run afterwards on the merged
  FASTA — Best Sequence already picks the best variant per sample when there
  is more than one candidate.
- Two progress bars track the batch: combinations completed (`i/N`) and the
  current combination's own phase progress (0-100 %, equal weight per active
  phase), the latter driven by a new `ProgressPanel.overallProgressChanged`
  signal so it updates live even though the Progress panel itself stays off
  screen during a batch. The per-combination bar is clamped to be monotonic
  within a run: some phases restart their own sub-progress mid-phase (e.g.
  phase 1 moving from demultiplexing into file merging, or phase 2a starting
  a new coverage level), which would otherwise make the overall % briefly
  jump backwards.
- Stopping is handled the same way regardless of how it happens: the panel's
  own **Stop** finishes the running combination cleanly before stopping;
  stopping from the Analysis panel's own Stop button, resetting the analysis,
  or closing the app mid-batch all still deduplicate and write the merged
  FASTA from whatever combinations completed — none of them can leave the
  batch's internal queue silently stuck.
- New **Notes** entry ("Parameter Batch") walking through the workflow.

#### BLAST utility — a second tab, a shared taxonomy reference, Tax_level_match
- The BLAST utility is now two tabs. **BLAST API Search** is the existing live
  NCBI search, unchanged in behaviour. **BLAST web results** is new: it parses
  a Hit Table already downloaded from `blast.ncbi.nlm.nih.gov` (website
  **Download All → Hit Table(text)** or **Hit Table(csv)**, both formats
  auto-detected) instead of submitting a new search — for when NCBI has
  throttled this IP's search traffic. It still fetches organism/taxonomy via
  NCBI E-utilities (a separate, low-volume service from the search queue this
  tab exists to avoid), reusing `_BlastWorker`'s rate limiting, `.dbx` caches
  and XLSX export through a subclass rather than a second implementation. It
  writes `blastfile-<ts>.tsv`/`.xlsx` and its own run log; unlike a live
  search it produces no FASTA of queried sequences, since the input file
  carries hits only. Its **Parse results** button stays disabled once a run
  finishes or errors — even if more files are dropped — until **Clear** is
  pressed, so the same run cannot be launched twice by accident. The two tabs
  refuse to run at the same time (each keeps an independent NCBI rate
  limiter against the same IP; starting one while the other is still running
  is blocked with a message instead of doubling the request rate).
- Both tabs can now add the expected (query) taxonomy from a reference file —
  the same **"I have a reference file with the query taxonomy"** feature Best
  Sequence (§14.1) already offered, writing `Query_Order`/`Query_Family`/
  `Query_Genus`/`Query_organism` into the results table before it is converted
  to `.xlsx`. The matching/writing logic itself is not duplicated: it was
  extracted out of `_BestSeqWorker` into module-level functions in
  `best_seq_panel.py` that both utilities call, so a BLAST table can already
  satisfy Best Sequence's own required columns by the time it gets there.
- When a run has both sides of the taxonomy — hit taxonomy from "Fetch
  organism + taxonomy" and expected taxonomy from the reference file above —
  the table gains one more column, **`Tax_level_match`**, recording the
  deepest rank at which the two agree (`organism`/`genus`/`family`/`order`/
  `none`) — the same rule Best Sequence's own `Tax_level` column (§14.2) uses
  to judge a hit, computed in the same read/rewrite pass that applies the
  reference file rather than as a separate pass over the table.

#### BOLD Formatter utility (sidebar, right below Best Sequence)
- New **"BOLD Formatter"** panel (`_utilities/bold_formatter.py`) that takes a
  BOLD Systems "Barcode ID" results export (`.xlsx`) and restyles it to match
  the look of the BLAST results workbook, rather than adding another
  from-scratch report format: a per-group **Hit rank** column, the same
  navy/teal/burnt-orange column colour coding, banded fills per Query ID
  group, a bold first hit, a frozen header row and column filters.
- Groups every row by `Query ID` and sorts each group by `ID%` descending
  itself — the input does not need to already be sorted or grouped — logging
  any group it had to re-sort so a malformed export is never silently
  reordered without a trace.
- **"Hits to keep per Query ID"** caps the output to each query's top-N hits
  (default 5, same idea as BLAST's "Hits per sequence"), or **"Keep all
  hits"** to skip the cap entirely.
- Writes `<input name>_formatted.xlsx` to the automatic
  `output/ont-barcoder_<timestamp>_bold_format/` folder or a manually chosen
  one, the same output-folder choice offered by the other export tools.
- New manual section (§15) and screenshot.

---

## [3.3b] — 2026-09

Adds a post-processing utility for reconciling several runs of the same library.
The analysis pipeline is untouched: 3.2b parameters reproduce 3.2b results
exactly.

### Added

#### Best Sequence Selector utility (sidebar, below BLAST)
- New **"Best Sequence"** panel (`_utilities/best_seq_panel.py`) that picks the
  best consensus sequence per sample across **two or more** ONTbarcoder runs made
  with different parameters. Each run is supplied as a FASTA + its BLAST table
  (`.xlsx` / `.tsv` / `.csv`), paired automatically by base file name.
- Samples whose sequence is identical in every run are kept as they are; the rest
  are resolved by scoring each candidate: taxonomic rank shared by the best hit
  and the expected classification of the query (species 400 / genus 300 /
  family 200 / order 100), plus the weighted bit score of that hit, minus
  penalties per `ambs` and `estgaps`. Ties break by longer sequence, then reads.
- The panel exposes only the two settings that depend on the data (minimum
  alignment length and the sample-ID suffix). The three scoring weights are
  design constants at the top of `best_seq_panel.py`, documented with their
  rationale: their values only make sense relative to the 100-point gap between
  taxonomic ranks, and the result is flat over a wide range of the penalties
  (1-5 selected identical sequences on a 517-sample test set).
- **Requirement:** every BLAST table must carry `Query_Order`, `Query_Family`,
  `Query_Genus` and `Query_organism` on **each hit row**; files missing those
  columns are rejected in the drop zone before the run starts. Hits shorter than
  the configurable minimum alignment length (default 100 bp) are discarded as
  spurious.
- Outputs a TSV + formatted XLSX decision report, a run log, and three
  single-line FASTA files that preserve the
  original headers verbatim: `_all`, plus a disjoint split into `_identified`
  (best hit concordant at some rank) and `_no_tax_hit` (no rank matched). The
  split is decided by taxonomy alone: a sequence identical in every run is a
  reproducible consensus, not a verified identification, so without a taxonomic
  hit it lands in `_no_tax_hit` for review like any other.
- The decision report carries the expected taxonomy and the taxonomy of the best
  hit **side by side, rank by rank** (`Query_Order/Family/Genus/organism` vs.
  `Hit_Order/Family/Genus/organism`), so a mismatch shows at which rank it breaks
  — the information needed to tell a distant relative from a contamination.
  Taxon names keep their original spelling (*Epidendrum fimbriatum*, not
  `epidendrum_fimbriatum`); the concordance test stays case-insensitive.
- The `Query_*` columns are read from the BLAST table independently of the hits,
  so the expected taxonomy is still reported for a sample whose hits were all
  filtered out. `N_hits` (hits that passed the minimum-alignment filter) is
  reported next to `N_hits_raw` (rows present before filtering).
- `Flag` values name the actual situation: **`tax_mismatch`** (hits passed the
  filter but none matches the expected taxonomy — the contamination /
  mislabelling candidate), `low_taxonomic_support` (now only for a hit reaching
  order level), **`hits_below_min_aln`** (hits exist but all too short),
  `no_blast_hit` (nothing in the table at all), `near_tie`,
  `missing_in_N_run(s)` and `ambs=N`. The run log prints a one-line meaning next
  to each count.
- `Decision` = **`resolved_by_score`** (was `blast_selected`) for the samples
  whose runs disagreed: the name no longer implies BLAST settled it, since
  sequences with no usable hit are decided by length, reads and ambiguities.

#### Best Sequence: single-run classification mode
- The panel now accepts **one** FASTA + BLAST table pair, not just two or more.
  With a single run there is nothing to select between, so every sequence is
  kept and the module works as a classifier: the report and the
  `_identified` / `_no_tax_hit` split are produced exactly the same, which is
  the quick way to triage one BLAST run and pull out the sequences with a
  taxonomic match (and spot the `tax_mismatch` ones) without processing the
  library twice.
- `Decision` = **`single_run`** on those rows, instead of misreporting them as
  `identical_in_all_runs` — with one run there is nothing to be identical to.
  Progress messages, the final summary and the run log switch to classification
  wording, and the drop zone states which mode will run.

#### Worker module renamed
- `_utilities/ONTbarcoder3_multiprocessing.py` → **`_utilities/pipeline.py`**
  (imports updated in `ONTbarcoder3.py`; PyInstaller picks it up automatically
  through `collect_submodules('_utilities')`). Shorter and descriptive, with no
  behaviour change.
- The module docstring now warns that this file must never be named after a
  standard-library module. `_utilities` is inserted at the front of `sys.path`,
  so a file called `multiprocessing.py` there would shadow the real package:
  the parent process might survive on import order alone, but every spawned
  worker dies with `No module named 'multiprocessing.context'` and the Pool
  retries forever, turning a run into a process storm.

#### BLAST utility saves the queried sequences
- A run now also writes `blast-<ts>.fa` next to `blast-<ts>.tsv` / `.xlsx`,
  holding the sequences submitted in the normalised single-line form actually
  sent to NCBI, so its headers are exactly the `Query_name` values of the table.
  Sharing the base name is what lets the Best Sequence utility pair a run's
  sequences with its BLAST table, so no renaming is needed between the two
  panels. It is written before the first query (it survives a run stopped half
  way) and a failure to write it never aborts the BLAST run.

#### Best Sequence accepts a query taxonomy reference file
- New **"I have a reference file with the query taxonomy"** option in the panel
  settings. When ticked it opens a drop zone for a single reference table
  (`.csv` / `.xlsx` / `.tsv`) and the four `Query_Order` / `Query_Family` /
  `Query_Genus` / `Query_organism` columns are written into **every** BLAST table
  of the run from that one list, instead of preparing each table by hand.
- The identifier of the reference is the sample ID the panel already uses to
  group sequences (header up to the first `;`, minus the *Strip suffix* value),
  and the **four columns following the identifier** are read as Order, Family,
  Genus and Organism whatever their headers say. The identifier column is the
  first one named `Sample` / `ID` / `Query_name` / `Code` / `Voucher`…, else the
  first column of the file.
- Tables are rewritten **in place**: existing `Query_*` columns are overwritten,
  missing ones are appended at the right end, and a table locked by Excel stops
  the run with a message rather than losing the change. With the option on, the
  drop zone no longer rejects tables lacking those columns.
- The run log records the reference file, its identifier and taxonomy columns,
  the rows filled per table and the samples that were not found in it.

#### BLAST utility reports the sequences with no match
- A run now writes `nohit_seqs_<ts>.fa` with the sequences that were queried
  successfully but got no hit from NCBI, so they can be re-run against another
  database or inspected without diffing the input FASTA against the table.
  It is built from the batches whose rows reached the TSV, so it never overlaps
  `missing_seqs_<ts>.fa` (batches that failed or were never processed).
- The run log gains `Seqs with hits`, `Seqs with no hits`, the no-hit file name
  and the list of the query names that returned no match; the live-log result
  line reports the same summary.

#### Compare utility: readable summary for many files
- Files now get short single-letter aliases (`A`, `B`, `C`…, plus `REF` in
  reference mode) instead of their full path in every column header. A new
  **`Runs`** sheet in `summary.xlsx` maps each alias to its label, role, file,
  sequence count and full path; the on-screen results window shows the same
  mapping as a chip bar and in the header tooltips.
- `summary.xlsx` gains a two-row header: a merged band naming the run
  (`A · 103536`, with the full path as a cell comment) over the `len` / `cov` /
  `ambs` / `gaps` sub-columns. The sheet also gets an autofilter and freezes the
  `ID` / `State` columns.
- The `Note` column no longer enumerates the N·(N−1)/2 pairs with both file
  names spelled out. Files carrying the same sequence are collapsed into
  variant groups, **numbered** `1, 2, 3…` — deliberately not lettered, so a
  group label is never confused with the letter-aliased runs it lists — and
  only the distances **between groups** are reported:
  `1=A-D | 2=E | 3=F || 1-2 d=34, 1-3 d=43, 2-3 d=15 || Best F (…)`.
  On a 6-file comparison this cut a typical note from ~1 450 to ~90 characters.
  Reference mode groups the files by their verdict against the reference
  instead: `= REF: A-C || ≠ REF d=34: D || …`.
- Two new columns in all-vs-all mode: **`Vars`**, the number of distinct
  sequences found for the ID, and **`Pattern`**, one group number per file
  (`111123`, `.` for absent), so rows sharing a discrepancy pattern sort and
  filter together.
- FASTA outputs are unchanged and still record the full source file name in the
  `best_from=` / `src=` header fields.

### Documentation
- Manual brought up to date (`guide/MANUAL.html`): new **§14 Utility — Best
  Sequence**, and the two 3.2b features that were still undocumented — the
  **QC length tolerance** parameter (§6.3) and the **mixture triage rules** of
  intra-sample variant detection (§6.1: 3 % divergence review threshold,
  anti-hotspot guard, bimodal read-length warning), plus the new columns of the
  *Intra-sample variants* sheet (§8.1).

---

## [3.2b] — 2026-09

Reliability release plus targeted analysis features. Default parameters
reproduce 3.1b results exactly; the new behaviours are opt-in or advisory.

### Added

#### QC length tolerance for coding markers
- New **"QC length tolerance ± (bp)"** parameter (Consensus tab, default `0` =
  classic exact-length rule). Admits legitimate in-frame length variation
  between taxa for coding markers such as rbcL or matK. Translation validation
  still applies unchanged, so lengths shifted by sequencing errors
  (frameshifts) are rejected regardless. Saved in parameter profiles, written
  to `log.txt` and the HTML report; disabled in non-Coding mode.

#### Intra-sample variant detection (contamination / sample-mix triage)
- **Dominant↔secondary divergence** computed per haplotype cluster (edlib,
  IUPAC-aware) and reported in the run log, in `secondary_variants.fa` /
  `secondary_variants_recovered.fa` headers (`div=…%`) and in a new
  **"Divergence vs dominant (%)"** column of the *Intra-sample variants*
  sheet. Divergence ≥ 3 % (heterospecific level) now forces **NEEDS REVIEW**
  with an explicit reason — conspecific-level mixes stay informational.
- **Anti-hotspot guard:** a mixture is only declared with ≥ 2 linked
  polymorphic columns and secondary clusters of ≥ 3 reads. Single-SNP
  conspecific mixtures are ignored by design (indistinguishable from
  heteroplasmy; same species identification either way).
- New **"Identical to N other barcodes"** column: how many *other* samples
  carry a final barcode identical to each secondary variant. With many
  conspecific samples per run, a high count means a common haplotype of the
  species; count = 1 combined with high divergence flags a cross-contamination
  candidate.
- **Bimodal read-length warning**, computed on *all* demultiplexed reads of a
  sample **before** the by-length subsampling (which could hide a
  different-length contaminant from the haplotype resolver): second length
  mode ≥ 30 bp away holding ≥ 20 % of reads is logged 🟠 and listed in the
  variants sheet.

### Fixed

#### Real-Time mode (data integrity)
- **Stability gate for MinKNOW FASTQs and POD5s:** a file is only consumed
  once its (size, mtime) is unchanged between two consecutive polls.
  Previously a half-written file could be concatenated partially and marked
  as known forever — silently losing every read written afterwards and
  possibly corrupting the 4-line FASTQ frame of the accumulated file.
- The accumulated FASTQ is always normalized to complete 4-line records
  (both cycle concatenation and the finalize rebuild drop a truncated
  trailing record with a warning).
- **Dorado batches are no longer lost:** POD5s are marked as known only after
  a successful launch; a failed batch (launch error or non-zero exit)
  requeues its POD5s and discards the partial FASTQ, so reads are neither
  lost nor duplicated.
- No concatenation happens while a cycle may be reading the accumulated
  FASTQ (torn-record race); the Dorado path defers with a retry.
- A pending next cycle is triggered right after a cycle completes instead of
  waiting for the next new file to arrive.
- **Canceling RT finalization resumes monitoring** (poll timers restarted,
  bookkeeping synced with the rebuilt accumulated file) instead of leaving a
  silently dead session.
- Demultiplexing worker failures are now reported prominently instead of
  being swallowed.
- `totalseqs` is no longer overwritten by `prepdemultiplex` in RT mode.

#### Biological logic
- **Sample names containing dots are no longer truncated** in the consensus
  worker (`BC.01` kept its full identity; previously `X.1` and `X.2`
  collided into one key, and per-sample genetic codes silently fell back to
  the global code).
- Per-sample genetic codes are validated against **existing NCBI tables**:
  7, 8, 17–20, 32 and 0 are rejected at *Start analysis* with a clear
  message instead of crashing inside Biopython per sample.
- Coding QC ORF-coverage check now measures against the consensus's own
  length (identical at tolerance 0; correct when the tolerance admits other
  lengths).

#### UI / reporting
- "Please select a non-empty folder" message corrected to *empty*.
- Folder-organization log now says `intermediate_files/` (the real folder).
- Removed the duplicate Chart.js `<script>` include in the HTML report.
- Progress log capped at 5 000 blocks so multi-day RT runs don't slow the
  GUI (full log always in `log.txt`).
- Floating-window / per-sample-chart buttons show "Hide…" while open.
- Empty or invalid phase-2a coverage list is validated at *Start analysis*.
- Non-Coding checkbox tooltip now states the actual acceptance criterion
  (absence of ambiguous bases; length not enforced).

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
