---
title: Parameter Batch
created: 2026-09-11T00:00:00
updated: 2026-09-11T00:00:00
tags: help, batch
---

### Running a parameter grid automatically (Optional, Conventional analysis only)

Runs the currently loaded dataset once per combination of a parameter grid
(e.g. several Main consensus calling frequency / Min. secondary variant
fraction / Variant tolerance values), instead of changing them by hand and
re-running each time. It is an optional, self-contained tool — not a
required step of the Workflow — and only works in Conventional mode.

**Important:** Parameter Batch starts from whatever is currently set in the
**Parameters** card and overrides only the parameter(s) listed in the
`.cfg` — so only list the ones you actually want to vary, not every field.

1- Set up the dataset in **Input files** and every parameter you want to
keep FIXED as usual, in **Parameters**.

2- Go to **Parameter Batch** (right below Parameters) and click **Create
example cfg** to get a template listing every sweepable parameter, its
default, valid range and an example. Uncomment and edit only the ones you
want to vary.

3- Click **Load batch config…**, check the combination count shown, then
**Start batch**. Each combination runs as a normal analysis in its own
`ont-barcoder_*_conv` folder — nothing about a single run changes.

**Demultiplexing reuse:** Phase 1 (demultiplexing) only depends on 7 of the
sweepable parameters — `minlen`, `explen`, `demlen`, `minq`, `tagmm`,
`primersearchlen`, `primermismatch`. When a combination has the exact same
values for all 7 as the *previous* one in the queue, its `demultiplexed`
output is reused (hard-linked, or copied if that's not possible) instead of
demultiplexing again from scratch — only phases 2a/2b/3 re-run. This is
automatic and needs no configuration; it only kicks in for *consecutive*
combinations, so listing the 7 demultiplex parameters before any consensus
parameter in the `.cfg` groups matching combinations together and reuses the
most.

4- Once every combination finishes, the tool writes three files to the
batch's own output folder, all identifying runs by a short run number
(1, 2, 3…) instead of the full timestamped folder name:
- `batch_run_summary.tsv` — one row per analysis: parameters used and how
  many sequences its `consensus_filtered.fa` produced.
- `unique_consensus_filtered.fasta` — every run's `consensus_filtered.fa`
  merged: a sample whose sequence is identical in every run appears once, a
  sample with different sequences across runs gets one entry per variant
  (header tagged `;run3`).
- `batch_dedup_report.tsv` — one row per sample: how many variants, in how
  many runs (as two plain numbers, not `18/40`), and which run numbers
  (compressed as ranges, e.g. `1..12,15`) — deliberately using `..` instead
  of `-` so Excel doesn't read a value like `1-9` back as a date.

5- BLAST and Best Sequence stay manual steps — run them afterwards on that
merged FASTA to pick the best sequence per sample.

**Resuming an interrupted batch:** every batch folder keeps a copy of its
`.cfg` (`batch_config.cfg`), the dataset and Parameters-panel values it was
started with (`batch_state.json`), and a `batch_progress.tsv` that gets one
line as *each* combination completes — so it survives Stop, closing the app,
a crash or a power cut. With the same dataset loaded, click **Resume batch…**
and pick that `ont-barcoder_*_batch` folder: only the combinations not yet
completed are run (a combination cut off mid-run is re-run from scratch, its
partial folder is ignored), using the saved `.cfg` and parameters — not
whatever is in the Parameters panel now. When it finishes, the summary and
merged FASTA in that same folder cover the old and new runs together. A
different dataset loaded than the one recorded asks for confirmation first;
completed runs whose folder was deleted are simply run again.

**Compare two runs:** a per-sample A/B report of two analyses of the same
dataset — which samples gain, lose or change their QC-compliant / filtered
barcode, and whose secondary variants appear or disappear. *Load batch…*
lists that batch's combinations (run1, run2… with their parameters);
*Add run folder…* adds any other analysis folder. The summary goes to the
log and the per-sample detail to `compare_runs_<A>_vs_<B>.tsv` (in the batch
folder, or in run A's folder). Same logic as the command-line
`_utilities/compare_runs_report.py`.

**Coverage report (minimal combination set):** after batch → merge →
BLAST → Best Sequence, pick the batch folder and the Best Sequence folder.
For every taxonomically-identified best sequence it finds which
combinations produced exactly that sequence (as consensus or as a secondary
variant, whatever its `_var` number in each run), then the fewest
combinations that together recover them all (greedy). Writes
`parameter_batch_coverage_report.xlsx` and `minimal_run_set.cfg` — a
combo-list config with just those combinations, offered for loading right
away — into the Best Sequence folder. Same logic as the command-line
`_utilities/batch_coverage_report.py`.

**Stop vs Stop now:** *Stop* lets the combination in progress finish, then
ends the batch. *Stop now* aborts it immediately (after a confirmation); its
partial folder is left out of the summary and merge, and *Resume batch…*
re-runs it later. Either way the final log line says how many of the
combinations completed.

A batch of more than 15 combinations shows a warning (each one is a full
analysis and can take a long time); above 200 the app asks for confirmation
before starting anything.
