from __future__ import annotations
import os
import time
import datetime
from typing import List
from PyQt5 import QtCore, QtGui, QtWidgets
from .shared import *
from .shared import _tr, _get_base_dir, _profiles_dir
from .batch_sweep import (load_batch_config, swept_keys, merge_runs, count_fasta_records,
                          BATCH_STATE_FILE)
from .compare_runs_report import list_batch_runs, compare_runs
from .batch_coverage_report import run_coverage_report

# Combinations above this count get a visible (non-blocking) warning in the
# panel, since each one is a full analysis. MainWindow additionally asks for
# confirmation before starting anything above 200.
WARN_COMBO_THRESHOLD = 15

# The full, self-documenting template — every parameter, its default,
# accepted range and an example — lives in _profiles/ontbarcoder_batch.cfg
# (single source of truth, also readable/editable outside the app). This is
# only the fallback used if that file is ever missing from the install.
_EXAMPLE_CFG_FALLBACK = """# ontbarcoder_batch.cfg - one parameter per line, values separated by commas.
# Blank lines and lines starting with # are ignored.
# A parameter not listed here keeps its current value from the Parameters panel.
# "Detect intra-sample sequence variants" percentages use the SAME 0-100 scale
# as the GUI spinboxes (20, not 0.20).

consfreqfixed                    = 0.30, 0.35, 0.40, 0.45, 0.50
resolve_mixed.min_secondary_frac = 20, 15, 10
resolve_mixed.tolerance          = 10, 9, 8, 7, 6, 5
"""

# Same idea, but for the explicit-combination-list shape (see
# _utilities/batch_sweep.py's is_combo_list_config / parse_combo_list_config).
# Kept as its own file/fallback rather than folded into the grid template
# above: the two shapes are mutually exclusive per batch, so a single file
# mixing both examples invites copying the wrong block by accident.
_EXAMPLE_COMBOS_CFG_FALLBACK = """# combos
# One FULL combination per line: key=value, key=value, ... — e.g. a row
# copied from a previous batch_run_summary.tsv's Parameters column.
# See _profiles/ontbarcoder_batch_combos.cfg for the full explanation.

consfreqfixed=0.30, resolve_mixed.min_secondary_frac=20, resolve_mixed.tolerance=10
consfreqfixed=0.40, resolve_mixed.min_secondary_frac=15, resolve_mixed.tolerance=7
consfreqfixed=0.50, resolve_mixed.min_secondary_frac=10, resolve_mixed.tolerance=5
"""


def _example_cfg_path() -> str:
    return os.path.join(_get_base_dir(), "_profiles", "ontbarcoder_batch.cfg")


def _example_combos_cfg_path() -> str:
    return os.path.join(_get_base_dir(), "_profiles", "ontbarcoder_batch_combos.cfg")


def _is_run_folder(path: str) -> bool:
    """An analysis output folder: it holds its consensus_filtered.fa."""
    return os.path.isfile(os.path.join(path, "consensus_filtered.fa"))


def _run_folders_in(path: str) -> List[str]:
    """`path` itself if it is a run folder, else the run folders directly
    inside it (e.g. the whole output/ folder)."""
    if _is_run_folder(path):
        return [path]
    try:
        subs = sorted(os.path.join(path, d) for d in os.listdir(path))
    except OSError:
        return []
    return [d for d in subs if os.path.isdir(d) and _is_run_folder(d)]


class _RunFolderList(QtWidgets.QListWidget):
    """Run folders to merge; accepts folders dropped from the file explorer."""
    foldersDropped = QtCore.pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        paths = [p for p in paths if p and os.path.isdir(p)]
        if paths:
            self.foldersDropped.emit(paths)
        e.acceptProposedAction()


class BatchSweepPanel(QtWidgets.QWidget):
    sweepRequested = QtCore.pyqtSignal(str)   # path to the batch config file
    stopRequested  = QtCore.pyqtSignal()
    resumeRequested = QtCore.pyqtSignal(str)  # path to an interrupted batch folder
    stopNowRequested = QtCore.pyqtSignal()    # abort the running combination too

    _BTN_H = 40   # shared height for Load / Save buttons

    def __init__(self, parent=None):
        super().__init__(parent)

        outer_layout = QtWidgets.QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

        self._inner = QtWidgets.QWidget()
        self._layout = QtWidgets.QVBoxLayout(self._inner)
        self._layout.setContentsMargins(20, 20, 20, 8)
        self._layout.setSpacing(14)
        scroll.setWidget(self._inner)
        # Scroll content on top, progress bars + log below, in a splitter so
        # the log can be dragged taller during long batches.
        self._splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(scroll)
        outer_layout.addWidget(self._splitter, 1)

        # ── Title + description ──
        self._lbl_title = make_label("Parameter Batch (Optional)", size=19, bold=True)
        self._lbl_desc = make_label(
            "Optional, not a required step of the workflow. Starts from "
            "whatever is currently set in the Parameters card, and overrides "
            "only the parameter(s) listed in the .cfg — so you only need to "
            "list the ones you actually want to vary, not every field. Runs "
            "the current dataset once per combination of the grid (e.g. "
            "several Main consensus calling frequency / Min. secondary "
            "variant fraction / Variant tolerance values), then merges every "
            "run's consensus_filtered.fa into one FASTA with the unique "
            "sequences found per sample. BLAST and Best Sequence stay manual "
            "steps, run afterwards on that merged FASTA.",
            color=TEXT_SEC
        )
        self._lbl_desc.setWordWrap(True)
        self._lbl_summary_line = make_label(
            "Runs the current dataset once per parameter combination and merges "
            "the unique sequences per sample. Optional — not needed to reach Results.",
            color=TEXT_SEC)
        self._lbl_summary_line.setWordWrap(True)
        self._layout.addWidget(self._lbl_title)
        self._layout.addWidget(self._lbl_summary_line)
        self._layout.addWidget(make_collapsible(self._lbl_desc))

        self._lbl_conv_only = QtWidgets.QLabel(
            "⚠ Conventional analysis only — not available in Real-Time mode.")
        self._lbl_conv_only.setStyleSheet(
            f"color:{AMBER}; font-size:13px; font-weight:600;")
        self._layout.addWidget(self._lbl_conv_only)

        # ── Config file ──
        cfg_box = QtWidgets.QGroupBox("Batch configuration")
        cfg_box.setStyleSheet("QGroupBox { font-weight:600; color:#1A1A2E; }")
        cl = QtWidgets.QVBoxLayout(cfg_box)
        cl.setSpacing(10)
        cl.setContentsMargins(16, 16, 16, 16)

        row = QtWidgets.QHBoxLayout()
        self._load_btn = QtWidgets.QPushButton("Load batch config…")
        self._load_btn.setObjectName("secondary_btn")
        self._load_btn.setFixedHeight(self._BTN_H)
        self._load_btn.clicked.connect(self._on_load_clicked)
        row.addWidget(self._load_btn)
        self._lbl_path = QtWidgets.QLabel("No config loaded.")
        self._lbl_path.setStyleSheet(f"color:{TEXT_SEC};")
        row.addWidget(self._lbl_path, 1)
        self._resume_btn = QtWidgets.QPushButton("Resume batch…")
        self._resume_btn.setObjectName("secondary_btn")
        self._resume_btn.setFixedHeight(self._BTN_H)
        self._resume_btn.setEnabled(False)
        self._resume_btn.setToolTip(
            "Pick an interrupted batch folder (output/ont-barcoder_*_batch) to "
            "run only the combinations it has not completed yet, with the same "
            ".cfg and parameters it was started with, then merge every run.")
        self._resume_btn.clicked.connect(self._on_resume_clicked)
        row.addWidget(self._resume_btn)
        cl.addLayout(row)

        self._lbl_summary = QtWidgets.QLabel("")
        self._lbl_summary.setWordWrap(True)
        self._lbl_summary.setTextFormat(QtCore.Qt.RichText)
        self._lbl_summary.setStyleSheet(f"color:{TEXT_PRI}; font-size:15px;")
        cl.addWidget(self._lbl_summary)

        self._lbl_warn = QtWidgets.QLabel("")
        self._lbl_warn.setWordWrap(True)
        self._lbl_warn.setTextFormat(QtCore.Qt.RichText)
        self._lbl_warn.setStyleSheet(
            f"color:{AMBER}; background-color:{AMBER_LT}; border:1px solid "
            f"{AMBER}; border-radius:6px; padding:8px 10px; font-size:14px;")
        self._lbl_warn.hide()
        cl.addWidget(self._lbl_warn)

        help_lbl = QtWidgets.QLabel(
            "<b>Grid:</b> one line per parameter, <code>key = v1, v2, v3</code> — every "
            "combination of every listed value is run. <b>Combo list:</b> first line "
            "<code># combos</code>, then one full combination per line, "
            "<code>key=value, key=value, ...</code> — for re-running specific "
            "combinations rather than every combination of a grid. "
            "<code>#</code> for comments in both. The two buttons below each write out "
            "a self-documented starting point for their own shape."
        )
        help_lbl.setWordWrap(True)
        help_lbl.setTextFormat(QtCore.Qt.RichText)
        cl.addWidget(make_collapsible(help_lbl, "Config file format (grid / combo list)"))

        example_row = QtWidgets.QHBoxLayout()
        example_btn = QtWidgets.QPushButton("Create example cfg (grid)")
        example_btn.setObjectName("secondary_btn")
        example_btn.setFixedHeight(self._BTN_H)
        example_btn.clicked.connect(self._save_example)
        example_row.addWidget(example_btn)
        example_combos_btn = QtWidgets.QPushButton("Create example cfg (combo list)")
        example_combos_btn.setObjectName("secondary_btn")
        example_combos_btn.setFixedHeight(self._BTN_H)
        example_combos_btn.clicked.connect(self._save_example_combos)
        example_row.addWidget(example_combos_btn)
        example_row.addStretch()
        cl.addLayout(example_row)

        self._layout.addWidget(cfg_box)

        # ── Merge existing runs (analyses already run by hand, no sweep) ──
        merge_box = QtWidgets.QGroupBox("Merge existing runs")
        merge_box.setStyleSheet("QGroupBox { font-weight:600; color:#1A1A2E; }")
        ml = QtWidgets.QVBoxLayout(merge_box)
        ml.setSpacing(10)
        ml.setContentsMargins(16, 16, 16, 16)

        merge_help = QtWidgets.QLabel(
            "Compare analyses already run by hand, without a batch: add their "
            "output folders (button or drag &amp; drop) and merge them into the same "
            "files a batch produces — <code>unique_consensus_filtered.fasta</code> "
            "and, from the runs that had <b>Detect intra-sample sequence variants</b> "
            "on, <code>unique_secondary_variants.fasta</code>. Adding a folder that "
            "holds several runs (e.g. <code>output/</code>) adds every run inside it."
        )
        merge_help.setWordWrap(True)
        merge_help.setTextFormat(QtCore.Qt.RichText)
        ml.addWidget(make_collapsible(merge_help, "How merging works"))

        self._merge_list = _RunFolderList()
        self._merge_list.setMinimumHeight(130)
        self._merge_list.foldersDropped.connect(self._add_merge_folders)
        ml.addWidget(self._merge_list)

        merge_row = QtWidgets.QHBoxLayout()
        self._merge_add_btn = QtWidgets.QPushButton("Add run folder…")
        self._merge_add_btn.setObjectName("secondary_btn")
        self._merge_add_btn.setFixedHeight(self._BTN_H)
        self._merge_add_btn.clicked.connect(self._on_merge_add_clicked)
        merge_row.addWidget(self._merge_add_btn)
        self._merge_remove_btn = QtWidgets.QPushButton("Remove selected")
        self._merge_remove_btn.setObjectName("secondary_btn")
        self._merge_remove_btn.setFixedHeight(self._BTN_H)
        self._merge_remove_btn.clicked.connect(self._remove_merge_selected)
        merge_row.addWidget(self._merge_remove_btn)
        self._merge_clear_btn = QtWidgets.QPushButton("Clear")
        self._merge_clear_btn.setObjectName("secondary_btn")
        self._merge_clear_btn.setFixedHeight(self._BTN_H)
        self._merge_clear_btn.clicked.connect(self._clear_merge)
        merge_row.addWidget(self._merge_clear_btn)
        merge_row.addStretch()
        self._merge_btn = QtWidgets.QPushButton("Merge runs  →")
        self._merge_btn.setObjectName("primary_btn")
        self._merge_btn.setFixedHeight(self._BTN_H)
        self._merge_btn.setEnabled(False)
        self._merge_btn.clicked.connect(self._merge_existing_runs)
        merge_row.addWidget(self._merge_btn)
        ml.addLayout(merge_row)

        self._layout.addWidget(merge_box)

        # ── Compare two runs (per-sample A/B report) ──
        cmp_box = QtWidgets.QGroupBox("Compare two runs")
        cmp_box.setStyleSheet("QGroupBox { font-weight:600; color:#1A1A2E; }")
        cml = QtWidgets.QVBoxLayout(cmp_box)
        cml.setSpacing(10)
        cml.setContentsMargins(16, 16, 16, 16)

        cmp_help = QtWidgets.QLabel(
            "Per-sample A/B comparison of two analyses of the same dataset — "
            "e.g. two combinations of a batch, or a run made with an older "
            "version: which samples gain, lose or change their QC-compliant "
            "(<code>consensus_no_errors.fa</code>) or filtered "
            "(<code>consensus_filtered.fa</code>) barcode, and whose secondary "
            "variants appear or disappear. Unlike the Compare panel, it looks at "
            "the whole analysis result, not at sequences of one file. Load a batch "
            "folder to pick two of its combinations, and/or add run folders. The "
            "per-sample TSV is written to the batch folder (two runs of the loaded "
            "batch) or else to run A's folder."
        )
        cmp_help.setWordWrap(True)
        cmp_help.setTextFormat(QtCore.Qt.RichText)
        cml.addWidget(make_collapsible(cmp_help, "What this compares"))

        src_row = QtWidgets.QHBoxLayout()
        self._cmp_batch_btn = QtWidgets.QPushButton("Load batch…")
        self._cmp_batch_btn.setObjectName("secondary_btn")
        self._cmp_batch_btn.setFixedHeight(self._BTN_H)
        self._cmp_batch_btn.clicked.connect(self._on_cmp_load_batch)
        src_row.addWidget(self._cmp_batch_btn)
        self._cmp_add_btn = QtWidgets.QPushButton("Add run folder…")
        self._cmp_add_btn.setObjectName("secondary_btn")
        self._cmp_add_btn.setFixedHeight(self._BTN_H)
        self._cmp_add_btn.clicked.connect(self._on_cmp_add_run)
        src_row.addWidget(self._cmp_add_btn)
        self._lbl_cmp_src = QtWidgets.QLabel("")
        self._lbl_cmp_src.setStyleSheet(f"color:{TEXT_SEC};")
        src_row.addWidget(self._lbl_cmp_src, 1)
        cml.addLayout(src_row)

        cmp_form = QtWidgets.QFormLayout()
        cmp_form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self._cmp_a = QtWidgets.QComboBox()
        self._cmp_b = QtWidgets.QComboBox()
        for combo in (self._cmp_a, self._cmp_b):
            combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(30)
            combo.currentIndexChanged.connect(self._sync_cmp_btn)
        cmp_form.addRow("Run A:", self._cmp_a)
        cmp_form.addRow("Run B:", self._cmp_b)
        cml.addLayout(cmp_form)

        cmp_row = QtWidgets.QHBoxLayout()
        self._cmp_all_chk = QtWidgets.QCheckBox(
            "List every sample in the TSV (not only those that differ)")
        cmp_row.addWidget(self._cmp_all_chk)
        cmp_row.addStretch()
        self._cmp_btn = QtWidgets.QPushButton("Compare runs  →")
        self._cmp_btn.setObjectName("primary_btn")
        self._cmp_btn.setFixedHeight(self._BTN_H)
        self._cmp_btn.setEnabled(False)
        self._cmp_btn.clicked.connect(self._on_cmp_run)
        cmp_row.addWidget(self._cmp_btn)
        cml.addLayout(cmp_row)

        self._layout.addWidget(cmp_box)
        self._cmp_batch_dir = ""

        # ── Coverage report (minimal combination set) ──
        cov_box = QtWidgets.QGroupBox("Coverage report (minimal combination set)")
        cov_box.setStyleSheet("QGroupBox { font-weight:600; color:#1A1A2E; }")
        cvl = QtWidgets.QVBoxLayout(cov_box)
        cvl.setSpacing(10)
        cvl.setContentsMargins(16, 16, 16, 16)

        cov_help = QtWidgets.QLabel(
            "After a batch → merge → BLAST → Best Sequence: finds which "
            "combinations reproduce each taxonomically-identified best sequence "
            "(as consensus or as secondary variant) and the fewest combinations "
            "that together recover them all. Writes "
            "<code>parameter_batch_coverage_report.xlsx</code> and "
            "<code>minimal_run_set.cfg</code> — a combo-list config with just "
            "those combinations, ready to load for the next dataset — into the "
            "Best Sequence folder."
        )
        cov_help.setWordWrap(True)
        cov_help.setTextFormat(QtCore.Qt.RichText)
        cvl.addWidget(make_collapsible(cov_help, "What this does"))

        self._cov_batch_dir = ""
        self._cov_bestseq_dir = ""
        for attr, text in (("batch", "Batch folder…"), ("bestseq", "Best Sequence folder…")):
            row = QtWidgets.QHBoxLayout()
            btn = QtWidgets.QPushButton(text)
            btn.setObjectName("secondary_btn")
            btn.setFixedHeight(self._BTN_H)
            btn.setFixedWidth(220)
            btn.clicked.connect(lambda _c=False, a=attr: self._on_cov_pick(a))
            row.addWidget(btn)
            lbl = QtWidgets.QLabel("Not selected.")
            lbl.setStyleSheet(f"color:{TEXT_SEC};")
            row.addWidget(lbl, 1)
            setattr(self, f"_lbl_cov_{attr}", lbl)
            cvl.addLayout(row)

        cov_row = QtWidgets.QHBoxLayout()
        cov_row.addStretch()
        self._cov_btn = QtWidgets.QPushButton("Create coverage report  →")
        self._cov_btn.setObjectName("primary_btn")
        self._cov_btn.setFixedHeight(self._BTN_H)
        self._cov_btn.setEnabled(False)
        self._cov_btn.clicked.connect(self._on_cov_run)
        cov_row.addWidget(self._cov_btn)
        cvl.addLayout(cov_row)

        self._layout.addWidget(cov_box)
        self._layout.addStretch()

        # ── Progress bars: overall (combinations) + current iteration (%) ──
        # Outside the scroll area, pinned above the log, so the log appearing
        # (which shrinks the scroll viewport) can't push them out of view.
        self._bottom = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(self._bottom)
        pl.setContentsMargins(0, 8, 0, 0)
        pl.setSpacing(6)
        bars = QtWidgets.QVBoxLayout()
        bars.setContentsMargins(20, 0, 20, 0)
        bars.setSpacing(6)
        pl.addLayout(bars)

        self._progress = QtWidgets.QProgressBar()
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.hide()
        bars.addWidget(self._progress)

        self._run_progress = QtWidgets.QProgressBar()
        self._run_progress.setRange(0, 100)
        self._run_progress.setValue(0)
        self._run_progress.setTextVisible(True)
        self._run_progress.setFormat("Current run: %p%")
        self._run_progress.hide()
        bars.addWidget(self._run_progress)

        # ── Live log (outside scroll, same convention as other panels) ──
        self._log = QtWidgets.QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QtGui.QFont("Consolas", 9))
        self._log.setMinimumHeight(120)
        self._log.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self._log.setStyleSheet(
            f"QPlainTextEdit {{ background:{GRAY_BG}; border:1px solid {GRAY_LINE}; "
            f"border-radius:6px; padding:6px; color:{TEXT_PRI}; margin:0 20px 8px 20px; "
            f"font-family:'Consolas','Courier New',monospace; }}"
        )
        self._log.hide()
        pl.addWidget(self._log, 1)
        self._splitter.addWidget(self._bottom)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._bottom.hide()

        self._start_time = 0.0
        self._elapsed_timer = QtCore.QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        # ── Footer ──
        footer = QtWidgets.QWidget()
        footer.setObjectName("batch_footer")
        footer.setStyleSheet(f"""
            QWidget#batch_footer {{
                background: {GRAY_CARD};
                border-top: 1px solid {GRAY_LINE};
            }}
        """)
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(20, 10, 20, 10)

        self._open_folder_btn = QtWidgets.QPushButton("Open folder  📂")
        self._open_folder_btn.setObjectName("secondary_btn")
        self._open_folder_btn.setFixedHeight(44)
        self._open_folder_btn.hide()
        self._open_folder_btn.clicked.connect(self._open_output_folder)
        fl.addWidget(self._open_folder_btn)

        self._stop_btn = QtWidgets.QPushButton("Stop")
        self._stop_btn.setObjectName("danger_btn")
        self._stop_btn.setFixedHeight(44)
        self._stop_btn.setFixedWidth(120)
        self._stop_btn.hide()
        self._stop_btn.setToolTip(
            "Finishes the combination currently running, then stops — it does "
            "not abort it mid-run.")
        self._stop_btn.clicked.connect(self.stopRequested)
        fl.addWidget(self._stop_btn)

        self._stop_now_btn = QtWidgets.QPushButton("Stop now")
        self._stop_now_btn.setObjectName("danger_btn")
        self._stop_now_btn.setFixedHeight(44)
        self._stop_now_btn.setFixedWidth(120)
        self._stop_now_btn.hide()
        self._stop_now_btn.setToolTip(
            "Aborts the combination currently running right away. Combinations "
            "already completed are kept and merged; use Resume batch… later to "
            "run the rest (the aborted one is re-run from scratch).")
        self._stop_now_btn.clicked.connect(self._on_stop_now_clicked)
        fl.addWidget(self._stop_now_btn)

        fl.addStretch()

        self._start_btn = QtWidgets.QPushButton("Start batch  →")
        self._start_btn.setObjectName("primary_btn")
        self._start_btn.setFixedHeight(44)
        self._start_btn.setFixedWidth(220)
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._emit_sweep)
        fl.addWidget(self._start_btn)
        outer_layout.addWidget(footer)

        self._cfg_path = ""
        self._cfg_count = 0
        self._last_outdir = ""
        self._prog_done = 0
        self._prog_total = 0
        self._prog_base = None   # combos already done when this session started (resume)
        self._dataset_loaded = False
        self._running = False

    def set_dataset_loaded(self, loaded: bool):
        """The sweep needs the dataset from Input files; merging does not."""
        self._dataset_loaded = loaded
        self._sync_start_btn()

    def _sync_start_btn(self):
        ok = bool(self._cfg_path) and self._dataset_loaded and not self._running
        self._start_btn.setEnabled(ok)
        self._resume_btn.setEnabled(self._dataset_loaded and not self._running)
        self._start_btn.setToolTip(
            "" if self._dataset_loaded else
            "Load a dataset in the Input files panel to run a sweep. "
            "Merging existing runs does not need one.")

    def _on_resume_clicked(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Resume batch — pick the interrupted batch folder",
            os.path.join(_get_base_dir(), "output"))
        if not path:
            return
        if not os.path.isfile(os.path.join(path, BATCH_STATE_FILE)):
            QtWidgets.QMessageBox.warning(
                self, "Resume batch",
                f"{os.path.basename(path)} is not a resumable batch folder "
                f"(no {BATCH_STATE_FILE}). Only batches started with this "
                f"version or later can be resumed.")
            return
        self.resumeRequested.emit(path)

    # ── Config loading ───────────────────────────────────────────────────

    def _on_load_clicked(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load batch config", _profiles_dir(),
            "Config files (*.cfg *.txt *.ini);;All files (*)")
        if path:
            self._load_cfg(path)

    def _load_cfg(self, path: str) -> bool:
        """Parse `path` and show its combination count. Returns False (with a
        warning) if it does not parse."""
        try:
            combos, mode, sweep = load_batch_config(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Invalid batch config", str(e))
            return False
        self._cfg_path = path
        self._cfg_count = len(combos)
        self._lbl_path.setText(os.path.basename(path))
        if mode == "grid":
            lines = ", ".join(f"{k} ({len(v)} values)" for k, v in sweep.items())
            self._lbl_summary.setText(
                f"<b>{len(combos)} combination(s)</b> from {len(sweep)} parameter(s): {lines}"
            )
        else:
            keys = ", ".join(sorted(swept_keys(combos)))
            self._lbl_summary.setText(
                f"<b>{len(combos)} combination(s)</b> loaded as an explicit list "
                f"(varies: {keys})"
            )
        if len(combos) > WARN_COMBO_THRESHOLD:
            self._lbl_warn.setText(
                f"⚠ {len(combos)} combinations — each one is a full analysis run "
                f"on the current dataset, one after another. This may take a "
                f"long time to complete.")
            self._lbl_warn.show()
        else:
            self._lbl_warn.hide()
        self._sync_start_btn()
        return True

    def _save_template(self, dialog_title: str, default_name: str,
                       template_path: str, fallback: str):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, dialog_title, os.path.join(_profiles_dir(), default_name),
            "Config files (*.cfg);;All files (*)")
        if not path:
            return
        try:
            with open(template_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            content = fallback
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Could not save file", str(e))

    def _save_example(self):
        self._save_template("Create example batch cfg (grid)", "my_batch.cfg",
                             _example_cfg_path(), _EXAMPLE_CFG_FALLBACK)

    def _save_example_combos(self):
        self._save_template("Create example batch cfg (combo list)",
                             "my_batch_combos.cfg",
                             _example_combos_cfg_path(), _EXAMPLE_COMBOS_CFG_FALLBACK)

    def _emit_sweep(self):
        if not self._cfg_path:
            return
        # The file may have been edited since it was loaded: re-read it so the
        # count the user approved is the count that actually runs.
        shown = self._cfg_count
        if not self._load_cfg(self._cfg_path):
            return
        if self._cfg_count != shown:
            reply = QtWidgets.QMessageBox.question(
                self, "Parameter Batch",
                f"{os.path.basename(self._cfg_path)} changed since it was loaded: "
                f"it now expands to {self._cfg_count} combination(s) instead of "
                f"{shown}. Start the batch with the new content?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if reply != QtWidgets.QMessageBox.Yes:
                return
        self.sweepRequested.emit(self._cfg_path)

    def _on_stop_now_clicked(self):
        reply = QtWidgets.QMessageBox.question(
            self, "Stop now",
            "Abort the combination currently running? Its partial output is "
            "discarded; completed combinations are kept, and Resume batch… "
            "can run the rest later.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if reply == QtWidgets.QMessageBox.Yes:
            self.stopNowRequested.emit()

    # ── Compare two runs ─────────────────────────────────────────────────

    def _cmp_entries(self) -> List[str]:
        return [self._cmp_a.itemData(i)[0] for i in range(self._cmp_a.count())]

    def _cmp_add_entry(self, label: str, folder: str, from_batch: bool):
        """Same entry in both pickers; userData = (folder, from_batch, tag)."""
        key = os.path.normcase(os.path.abspath(folder))
        if any(os.path.normcase(os.path.abspath(f)) == key for f in self._cmp_entries()):
            return
        tag = (label.split(" ", 1)[0] if from_batch
               else os.path.basename(folder.rstrip("/\\")).replace("ont-barcoder_", ""))
        for combo in (self._cmp_a, self._cmp_b):
            combo.addItem(label, (folder, from_batch, tag))
            combo.setItemData(combo.count() - 1, folder, QtCore.Qt.ToolTipRole)

    def _on_cmp_load_batch(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Compare two runs — pick a batch folder",
            os.path.join(_get_base_dir(), "output"))
        if not path:
            return
        try:
            runs = list_batch_runs(path)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, "Compare two runs", str(e))
            return
        # A new batch replaces the previous batch's runs (hand-added folders stay).
        for combo in (self._cmp_a, self._cmp_b):
            for i in reversed(range(combo.count())):
                if combo.itemData(i)[1]:
                    combo.removeItem(i)
        missing = 0
        for _n, folder, label in runs:
            if os.path.isdir(folder):
                self._cmp_add_entry(label, folder, True)
            else:
                missing += 1
        self._cmp_batch_dir = path
        note = f"{os.path.basename(path)}: {len(runs) - missing} run(s)"
        if missing:
            note += f" · {missing} folder(s) not found"
        self._lbl_cmp_src.setText(note)
        if self._cmp_b.count() > 1 and self._cmp_b.currentIndex() == self._cmp_a.currentIndex():
            self._cmp_b.setCurrentIndex(1)
        self._sync_cmp_btn()

    def _on_cmp_add_run(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Compare two runs — add a run folder",
            os.path.join(_get_base_dir(), "output"))
        if not path:
            return
        if not _is_run_folder(path):
            QtWidgets.QMessageBox.warning(
                self, "Compare two runs",
                f"{os.path.basename(path)} is not an analysis output folder "
                f"(no consensus_filtered.fa).")
            return
        self._cmp_add_entry(os.path.basename(path), path, False)
        # Newly added folder becomes B (A keeps what it had), the usual flow
        # being "this run vs the one I just added".
        if self._cmp_b.count() > 1:
            self._cmp_b.setCurrentIndex(self._cmp_b.count() - 1)
        self._sync_cmp_btn()

    def _sync_cmp_btn(self, *_):
        self._cmp_btn.setEnabled(self._cmp_a.count() >= 2 and not self._running)

    def _on_cmp_run(self):
        a, b = self._cmp_a.currentData(), self._cmp_b.currentData()
        if not a or not b:
            return
        (fa, a_batch, ta), (fb, b_batch, tb) = a, b
        if os.path.normcase(os.path.abspath(fa)) == os.path.normcase(os.path.abspath(fb)):
            QtWidgets.QMessageBox.warning(
                self, "Compare two runs", "Run A and Run B are the same run.")
            return
        out_dir = self._cmp_batch_dir if (a_batch and b_batch and self._cmp_batch_dir) else fa
        out_path = os.path.join(out_dir, f"compare_runs_{ta}_vs_{tb}.tsv")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            lines = compare_runs(fa, fb, self._cmp_a.currentText(),
                                 self._cmp_b.currentText(), out_path,
                                 self._cmp_all_chk.isChecked())
        except Exception as e:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.warning(self, "Compare two runs", f"Comparison failed: {e}")
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        self._log.clear()
        self.append_log("\n".join(lines))
        self._last_outdir = out_dir
        self._open_folder_btn.show()

    # ── Coverage report ──────────────────────────────────────────────────

    def _on_cov_pick(self, which: str):
        start = (self._cov_batch_dir or self._cmp_batch_dir if which == "batch"
                 else self._cov_bestseq_dir) or os.path.join(_get_base_dir(), "output")
        title = ("Coverage report — pick the batch folder" if which == "batch"
                 else "Coverage report — pick the Best Sequence output folder")
        path = QtWidgets.QFileDialog.getExistingDirectory(self, title, start)
        if not path:
            return
        need = ("batch_run_summary.tsv" if which == "batch" else "bestseq-*_identified.fasta")
        import glob
        if not glob.glob(os.path.join(path, need)):
            QtWidgets.QMessageBox.warning(
                self, "Coverage report",
                f"{os.path.basename(path)} has no {need}.\n\n"
                + ("Pick a finished Parameter Batch folder (…_batch)."
                   if which == "batch" else
                   "Pick a Best Sequence Selection output folder."))
            return
        setattr(self, f"_cov_{which}_dir", path)
        getattr(self, f"_lbl_cov_{which}").setText(os.path.basename(path))
        getattr(self, f"_lbl_cov_{which}").setToolTip(path)
        self._sync_cov_btn()

    def _sync_cov_btn(self, *_):
        self._cov_btn.setEnabled(bool(self._cov_batch_dir and self._cov_bestseq_dir)
                                 and not self._running)

    def _on_cov_run(self):
        lines: List[str] = []
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            res = run_coverage_report(self._cov_batch_dir, self._cov_bestseq_dir,
                                      log=lines.append)
        except Exception as e:
            QtWidgets.QApplication.restoreOverrideCursor()
            self._log.clear()
            self.append_log("\n".join(lines + [f"ERROR: {e}"]))
            QtWidgets.QMessageBox.warning(self, "Coverage report", f"Report failed: {e}")
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        self._log.clear()
        lines += ["", f"✓ {res['n_chosen']} of {res['n_runs']} combination(s) recover "
                      f"{res['n_covered']}/{res['n_winners']} identified sequence(s)"
                      + (f" · {res['n_never']} not reproduced by any run" if res["n_never"] else "")]
        self.append_log("\n".join(lines))
        self._last_outdir = os.path.dirname(res["xlsx"])
        self._open_folder_btn.show()
        if res["cfg"]:
            reply = QtWidgets.QMessageBox.question(
                self, "Coverage report",
                f"{res['n_chosen']} combination(s) recover {res['n_covered']} of "
                f"{res['n_winners']} identified sequence(s).\n\n"
                f"Load {os.path.basename(res['cfg'])} now as the batch config?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if reply == QtWidgets.QMessageBox.Yes:
                self._load_cfg(res["cfg"])

    # ── Merge existing runs ───────────────────────────────────────────────

    def _merge_folders(self) -> List[str]:
        return [self._merge_list.item(i).data(QtCore.Qt.UserRole)
                for i in range(self._merge_list.count())]

    def _sync_merge_buttons(self):
        self._merge_btn.setEnabled(self._merge_list.count() >= 2
                                   and self._stop_btn.isHidden())   # no batch running

    def _on_merge_add_clicked(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Add run folder (or a folder holding several runs)",
            os.path.join(_get_base_dir(), "output"))
        if path:
            self._add_merge_folders([path])

    def _add_merge_folders(self, paths: List[str]):
        present = {os.path.normcase(os.path.abspath(p)) for p in self._merge_folders()}
        not_runs = []
        for path in paths:
            found = _run_folders_in(path)
            if not found:
                not_runs.append(path)
            for run in found:
                key = os.path.normcase(os.path.abspath(run))
                if key in present:
                    continue
                present.add(key)
                has_var = count_fasta_records(
                    os.path.join(run, "secondary_variants.fa")) > 0
                item = QtWidgets.QListWidgetItem(
                    os.path.basename(run) + ("   [variants]" if has_var else ""))
                item.setData(QtCore.Qt.UserRole, run)
                item.setToolTip(run)
                self._merge_list.addItem(item)
        if not_runs:
            QtWidgets.QMessageBox.warning(
                self, "Merge existing runs",
                "No analysis output found (no consensus_filtered.fa) in:\n"
                + "\n".join(not_runs))
        self._sync_merge_buttons()

    def _remove_merge_selected(self):
        for item in self._merge_list.selectedItems():
            self._merge_list.takeItem(self._merge_list.row(item))
        self._sync_merge_buttons()

    def _clear_merge(self):
        self._merge_list.clear()
        self._sync_merge_buttons()

    def _merge_existing_runs(self):
        folders = self._merge_folders()
        if len(folders) < 2:
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        outdir = os.path.join(_get_base_dir(), "output", f"ont-barcoder_{ts}_merge")
        # The folder path is the tag: two runs may share a folder name.
        run_folders = [(f, f) for f in folders]
        # secondary_variants.fa only exists (non-empty) when the run had
        # 'Detect intra-sample sequence variants' on.
        n_var = {f: count_fasta_records(os.path.join(f, "secondary_variants.fa"))
                 for f in folders}
        var_folders = [(f, f) for f in folders if n_var[f] > 0]
        self._log.clear()
        self._log.show()
        self._open_folder_btn.hide()
        try:
            os.makedirs(outdir, exist_ok=True)
            n_filts = []
            with open(os.path.join(outdir, "merge_run_summary.tsv"),
                      "w", encoding="utf-8") as fh:
                fh.write("Run\tFolder\tN_consensus_filtered\tN_secondary_variants\n")
                for i, f in enumerate(folders, start=1):
                    n_filt = count_fasta_records(os.path.join(f, "consensus_filtered.fa"))
                    n_filts.append(n_filt)
                    fh.write(f"{i}\t{f}\t{n_filt}\t{n_var[f] or ''}\n")
            summary = merge_runs(run_folders, outdir, var_folders)
        except Exception as e:
            self.append_log(f"ERROR: merge failed: {e}")
            QtWidgets.QMessageBox.warning(self, "Merge existing runs", str(e))
            return
        summary.update({"n_filt_min": min(n_filts), "n_filt_max": max(n_filts)})
        self._last_outdir = outdir
        self._open_folder_btn.show()
        self.append_log("\n".join(
            [f"✓ Merged {len(folders)} existing run(s) -> {outdir}"]
            + self._summary_lines(summary, "merge_run_summary.tsv")))

    def _open_output_folder(self):
        if self._last_outdir and os.path.isdir(self._last_outdir):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._last_outdir))

    # ── Public API (called by MainWindow) ──────────────────────────────────

    def _show_bottom(self):
        if self._bottom.isHidden():
            self._bottom.show()
            total = max(self._splitter.height(), 400)
            self._splitter.setSizes([total - 300, 300])

    def append_log(self, text: str, level: str = "info"):
        self._show_bottom()
        self._log.show()
        self._log.appendPlainText(text)

    def set_progress(self, done: int, total: int, finished: bool = False):
        """`done` = combinations already completed, which is what drives the
        bar percentage; while the batch runs the label names the combination
        in flight (1-based), which is `done + 1`."""
        self._show_bottom()
        self._progress.show()
        self._progress.setRange(0, max(total, 1))
        self._progress.setValue(done)
        self._prog_done, self._prog_total = done, total
        if self._prog_base is None:
            self._prog_base = done   # resumed batches start with some already done
        if finished:
            self._progress.setFormat(
                f"Completed {done}/{total}  —  %p%  —  {self.elapsed_str()}")
        else:
            self._update_progress_text()
            self.set_run_progress(0)

    def _update_progress_text(self):
        """'Combination N/T — %  — elapsed — ETA'. The ETA averages only the
        combinations completed in THIS session (not ones carried over from
        before a resume), so it is blank until the first one finishes."""
        done, total = self._prog_done, self._prog_total
        text = f"Combination {min(done + 1, total)}/{total}  —  %p%  —  {self.elapsed_str()}"
        ran = done - (self._prog_base or 0)
        if ran > 0 and self._start_time:
            avg = (time.monotonic() - self._start_time) / ran
            text += f"  —  ETA ~{self.format_elapsed(int(avg * (total - done)))}"
        self._progress.setFormat(text)

    def set_run_progress(self, pct: int):
        """Progress (0-100) of the combination currently running — phases of
        that one analysis, not how many combinations are done."""
        self._show_bottom()
        self._run_progress.show()
        self._run_progress.setValue(max(0, min(100, pct)))

    def set_running(self, running: bool):
        self._running = running
        self._sync_start_btn()
        self._load_btn.setEnabled(not running)
        self._stop_btn.setVisible(running)
        self._stop_now_btn.setVisible(running)
        self._sync_merge_buttons()
        self._sync_cmp_btn()
        self._sync_cov_btn()
        if running:
            self._show_bottom()
            self._log.clear()
            self._log.show()
            self._open_folder_btn.hide()
            self._prog_base = None
            self._start_time = time.monotonic()
            self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()
            self._run_progress.hide()

    @staticmethod
    def format_elapsed(seconds: int) -> str:
        """"Xh MM:SS" (or "MM:SS" under an hour) — shared by the batch-elapsed
        tooltip/log timestamps and by MainWindow's per-combination duration."""
        h, rem = divmod(max(0, seconds), 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def elapsed_str(self) -> str:
        """Wall-clock time elapsed since the batch started (set_running(True))
        — used to timestamp each combination's log line."""
        if not self._start_time:
            return "00:00"
        return self.format_elapsed(int(time.monotonic() - self._start_time))

    def _tick_elapsed(self):
        self._progress.setToolTip(f"Elapsed: {self.elapsed_str()}")
        if self._running and self._prog_total:
            self._update_progress_text()

    def on_finished(self, summary: dict):
        self.set_running(False)
        self._last_outdir = summary.get("outdir", "")
        if self._last_outdir and os.path.isdir(self._last_outdir):
            self._open_folder_btn.show()

        n_runs, n_total = summary.get("n_runs", 0), summary.get("n_total", 0)
        if summary.get("stopped"):
            lines = [f"\n■ Batch stopped — {n_runs}/{n_total} combination(s) completed. "
                     f"Use Resume batch… on this folder to run the rest."]
        else:
            lines = [f"\n✓ Batch completed — {n_runs} run(s)."]
        lines += self._summary_lines(summary, "batch_run_summary.tsv")
        self.append_log("\n".join(lines))

    @staticmethod
    def _summary_lines(summary: dict, run_summary_name: str) -> List[str]:
        """Log lines for merge_runs() results — shared by the batch and by
        'Merge existing runs'."""
        lines = []
        if "n_filt_min" in summary:
            lo, hi = summary["n_filt_min"], summary["n_filt_max"]
            rng = f"{lo}" if lo == hi else f"{lo}–{hi}"
            lines.append(
                f"  consensus_filtered.fa per run: {rng} barcode(s) — "
                f"see {run_summary_name} for the per-run count.")
        if "n_samples" in summary:
            lines.append(
                f"  Deduplication across runs: {summary.get('n_samples', 0)} unique sample(s) "
                f"total, {summary.get('n_collapsed', 0)} with the same sequence in every run "
                f"they appear in, {summary.get('n_with_variants', 0)} with 2+ distinct "
                f"sequences across runs -> {summary.get('n_sequences_written', 0)} sequence(s) "
                f"written to unique_consensus_filtered.fasta "
                f"(see batch_dedup_report.tsv for the per-sample breakdown).")
        if "variants" in summary:
            v = summary["variants"]
            lines.append(
                f"  Secondary variants (raw + corrected) across runs: "
                f"{v.get('n_samples', 0)} sample(s) with variants -> "
                f"{v.get('n_sequences_written', 0)} distinct sequence(s) written to "
                f"unique_secondary_variants.fasta "
                f"(see batch_variants_dedup_report.tsv).")
        return lines

    def on_error(self, msg: str):
        self.set_running(False)
        self.append_log(f"ERROR: {msg}")
        QtWidgets.QMessageBox.warning(self, "Parameter Batch", msg)
