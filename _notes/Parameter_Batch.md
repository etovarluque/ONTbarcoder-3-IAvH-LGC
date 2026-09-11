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
  many runs, and which run numbers (compressed as ranges, e.g. `1-12,15`).

5- BLAST and Best Sequence stay manual steps — run them afterwards on that
merged FASTA to pick the best sequence per sample.

A batch of more than 15 combinations shows a warning (each one is a full
analysis and can take a long time); above 200 the app asks for confirmation
before starting anything.
