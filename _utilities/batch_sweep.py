"""Parameter-sweep engine for ONTbarcoder3: parses a plain-text grid config,
expands it into a Cartesian product of parameter combinations, applies each
combination on top of the analysis's base parameters, and — once every
combination has been run — deduplicates the consensus_filtered.fa produced by
each run folder into a single FASTA with one entry per distinct sequence per
sample.

No Qt/PyQt import here on purpose: this module is orchestrated by the GUI
(ONTbarcoder3.py / batch_sweep_panel.py) but stays independently testable.
"""
from __future__ import annotations
import os
import copy
import itertools
from typing import Dict, List, Tuple

# Every sweepable parameter, its kind and its valid range/choices — mirrors
# the bounds enforced by the corresponding GUI widget in ParamsPanel, so a
# value the GUI could never produce is rejected here too. Any key not listed
# here is rejected as unknown. A parameter simply absent from the .cfg keeps
# whatever value is currently set in the Parameters panel — the sweep only
# overrides what it lists, exactly like a manual run except for those keys.
#
# The default shown in each PARAM_SPECS-derived doc/comment matches
# ParamsPanel.DEFAULTS (a fresh install's Parameters panel), for reference
# only — the value actually used at runtime is whatever the Parameters panel
# currently holds, since apply_overrides() is applied on top of it.
#
# kind:
#   "int"/"float" -> (min, max), plain numeric range
#   "percent"     -> (min, max) on a 0-100 scale; stored internally as a 0-1
#                    fraction (min/100), same convention as the "Min.
#                    secondary variant fraction (%)" / "Variant tolerance (%)"
#                    spinboxes
#   "bool"        -> true/false/1/0/yes/no/on/off (case-insensitive)
#   "enum"        -> (allowed_values,), stored as-is
PARAM_SPECS: Dict[str, tuple] = {
    "minlen":            ("int", 0, 5000),
    "explen":            ("int", 0, 5000),
    "demlen":            ("int", 0, 500),
    "primersearchlen":   ("int", 10, 500),
    "primermismatch":    ("int", 0, 30),
    "tagmm":             ("int", 0, 5),
    "consfreqfixed":     ("float", 0.05, 0.95),
    "consfreqmin":       ("float", 0.05, 0.95),
    "consfreqmax":       ("float", 0.05, 0.95),
    "consfreqstep":      ("float", 0.01, 0.5),
    "mincoverage":       ("int", 1, 5000),
    "coverage2b":        ("int", 1, 5000),
    "n_threads":         ("int", 1, 256),
    "gencode":           ("enum", (1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 14,
                                    16, 21, 22, 23, 24, 25, 26)),
    "lendev":            ("int", 0, 500),
    "qclentol":          ("int", 0, 300),
    "maxcoverage":       ("int", 0, 100000),
    "minq":              ("int", 0, 50),
    # non_coi is deliberately NOT sweepable: in get_params() it derives three
    # other values (gencode -> 0, run_phase2b/run_phase3 -> False), and those
    # cannot be re-derived here when it is switched back off (the real
    # gencode lives in the GUI combo, not in the params dict). Set the
    # Non-Coding marker in the Parameters panel; the batch inherits it.
    "run_phase1":        ("bool",),
    "run_phase2a":       ("bool",),
    "run_phase2b":       ("bool",),
    "run_phase3":        ("bool",),
    "resolve_mixed.enabled":            ("bool",),
    "resolve_mixed.min_secondary_frac": ("percent", 10, 50),
    "resolve_mixed.tolerance":          ("percent", 0, 40),
}

_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off")


def _cast_and_validate(dotted_key: str, raw_value: str):
    """Cast `raw_value` (a string from the .cfg) to its typed value, and
    check it against PARAM_SPECS. Raises ValueError naming the offending
    key/value on any problem — unknown key, unparsable number, or a value
    outside the range the GUI itself would allow."""
    spec = PARAM_SPECS.get(dotted_key)
    if spec is None:
        valid = ", ".join(sorted(PARAM_SPECS))
        raise ValueError(f"Unknown sweep parameter '{dotted_key}'. Valid keys: {valid}")
    kind = spec[0]

    if kind == "bool":
        low = raw_value.strip().lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
        raise ValueError(
            f"{dotted_key}: '{raw_value}' is not a boolean "
            f"(use true/false, yes/no, on/off, or 1/0)")

    if kind == "enum":
        choices = spec[1]
        try:
            value = int(float(raw_value))
        except ValueError:
            raise ValueError(f"{dotted_key}: '{raw_value}' is not a number")
        if value not in choices:
            raise ValueError(
                f"{dotted_key}: {value} is not one of {choices}")
        return value

    _, lo, hi = spec
    try:
        number = float(raw_value)
    except ValueError:
        raise ValueError(f"{dotted_key}: '{raw_value}' is not a number")

    if kind == "percent":
        if not (lo <= number <= hi):
            raise ValueError(
                f"{dotted_key}: {number} is out of range [{lo}, {hi}] (%)")
        return number / 100.0

    if not (lo <= number <= hi):
        raise ValueError(f"{dotted_key}: {number} is out of range [{lo}, {hi}]")
    return int(number) if kind == "int" else number


def validate_sweep(sweep: Dict[str, List[str]]) -> None:
    """Validate every key/value of a parsed sweep dict against PARAM_SPECS.
    Raises ValueError on the first problem found. Does not need a params
    baseline, so the panel can call it right after loading the .cfg."""
    for key, values in sweep.items():
        for value in values:
            _cast_and_validate(key, value)


def parse_sweep_config(path: str) -> Dict[str, List[str]]:
    """Parse a sweep config file: one 'key = v1, v2, v3' per line.

    Blank lines and lines starting with '#' are ignored. Order of keys (and
    of values within a key) is preserved, since it determines run order.
    """
    sweep: Dict[str, List[str]] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(
                    f"{os.path.basename(path)}:{lineno}: expected 'key = value, "
                    f"value, ...', got: {raw.strip()!r}"
                )
            key, raw_values = line.split("=", 1)
            key = key.strip()
            values = [v.strip() for v in raw_values.split(",") if v.strip()]
            if not key or not values:
                raise ValueError(
                    f"{os.path.basename(path)}:{lineno}: empty key or value list"
                )
            sweep[key] = values
    if not sweep:
        raise ValueError(f"{os.path.basename(path)}: no parameters found")
    return sweep


def expand_grid(sweep: Dict[str, List[str]]) -> List[Dict[str, str]]:
    """Cartesian product of every key's value list, in the given key order."""
    keys = list(sweep.keys())
    combos = []
    for values in itertools.product(*(sweep[k] for k in keys)):
        combos.append(dict(zip(keys, values)))
    return combos


def apply_overrides(base_params: dict, override: Dict[str, str]) -> dict:
    """Return a copy of base_params with `override` applied on top.

    Keys may be dotted (e.g. "resolve_mixed.tolerance") to reach nested
    dicts. Every key/value is validated against PARAM_SPECS (see above) —
    raises ValueError naming the offending key on an unknown key, an
    unparsable value, or a value out of the GUI's own valid range.

    resolve_mixed.minor_thresh is not itself sweepable (there is no GUI
    control for it — it is always derived from min_secondary_frac), so it is
    recomputed here whenever min_secondary_frac is overridden, using the same
    formula as ParamsPanel.get_params(): min(0.20, min_secondary_frac).
    """
    params = copy.deepcopy(base_params)
    for dotted_key, raw_value in override.items():
        value = _cast_and_validate(dotted_key, raw_value)
        parts = dotted_key.split(".")
        node = params
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    if "resolve_mixed.min_secondary_frac" in override:
        secfrac = params["resolve_mixed"]["min_secondary_frac"]
        params["resolve_mixed"]["minor_thresh"] = min(0.20, secfrac)
    return params


def combo_label(override: Dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in override.items())


# ── Deduplication ────────────────────────────────────────────────────────

def _read_fasta(path: str) -> List[Tuple[str, str]]:
    """Return [(header, sequence), ...] preserving file order. header excludes '>'."""
    records: List[Tuple[str, str]] = []
    header = None
    seq_parts: List[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n").rstrip("\r")
            if not line:
                continue
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


def _sample_of(header: str) -> str:
    """Sample id: header text before the first ';' (ONTbarcoder header convention)."""
    return header.split(";", 1)[0].strip()


def dedup_consensus_filtered(run_folders: List[Tuple[str, str]],
                              out_fasta: str, out_report: str,
                              fasta_name: str = "consensus_filtered.fa") -> dict:
    """Merge consensus_filtered.fa from every run folder into one FASTA.

    run_folders: [(folder_path, tag), ...] in run order, tag = folder name
    with the "ont-barcoder_" prefix stripped (used to suffix the header of a
    sample that has more than one distinct sequence across runs).

    A sample whose sequence is identical in every folder it appears in is
    written once, with its original header untouched. A sample with distinct
    sequences across folders gets one record per distinct sequence, header
    suffixed with ";<tag>" of the folder that first produced it.
    """
    # sample -> list of (folder_tag, header, seq), in run order
    by_sample: Dict[str, List[Tuple[str, str, str]]] = {}
    for folder, tag in run_folders:
        fa_path = os.path.join(folder, fasta_name)
        if not os.path.isfile(fa_path):
            continue
        for header, seq in _read_fasta(fa_path):
            sample = _sample_of(header)
            by_sample.setdefault(sample, []).append((tag, header, seq))

    n_samples = len(by_sample)
    n_collapsed = 0
    n_with_variants = 0
    n_sequences_written = 0

    os.makedirs(os.path.dirname(out_fasta), exist_ok=True)
    with open(out_fasta, "w", encoding="utf-8") as fh_fa, \
         open(out_report, "w", encoding="utf-8") as fh_rep:
        fh_rep.write("Sample\tN_variants\tFolders\n")
        for sample in sorted(by_sample):
            entries = by_sample[sample]
            # first occurrence per distinct sequence, in run order
            seen: Dict[str, Tuple[str, str]] = {}
            for tag, header, seq in entries:
                if seq not in seen:
                    seen[seq] = (tag, header)
            variants = list(seen.items())  # [(seq, (tag, header)), ...]
            all_tags = [t for t, _h, _s in entries]

            if len(variants) == 1:
                n_collapsed += 1
                _seq, (_tag, header) = variants[0]
                fh_fa.write(f">{header}\n{_seq}\n")
                n_sequences_written += 1
            else:
                n_with_variants += 1
                for seq, (tag, header) in variants:
                    fh_fa.write(f">{header};{tag}\n{seq}\n")
                    n_sequences_written += 1

            fh_rep.write(f"{sample}\t{len(variants)}\t{';'.join(all_tags)}\n")

    return {
        "n_samples": n_samples,
        "n_collapsed": n_collapsed,
        "n_with_variants": n_with_variants,
        "n_sequences_written": n_sequences_written,
    }
