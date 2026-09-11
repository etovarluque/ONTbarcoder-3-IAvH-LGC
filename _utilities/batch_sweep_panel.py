from __future__ import annotations
import os
import time
from PyQt5 import QtCore, QtGui, QtWidgets
from .shared import *
from .shared import _tr, _get_base_dir
from .batch_sweep import parse_sweep_config, expand_grid, validate_sweep

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


def _example_cfg_path() -> str:
    return os.path.join(_get_base_dir(), "_profiles", "ontbarcoder_batch.cfg")


class BatchSweepPanel(QtWidgets.QWidget):
    sweepRequested = QtCore.pyqtSignal(str)   # path to the batch config file
    stopRequested  = QtCore.pyqtSignal()

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
        outer_layout.addWidget(scroll, 0)

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
        self._layout.addWidget(self._lbl_title)
        self._layout.addWidget(self._lbl_desc)

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
            "<b>Format:</b> one line per parameter, <code>key = v1, v2, v3</code>, "
            "<code>#</code> for comments. “Create example cfg” below writes "
            "every available parameter with its default, valid range and an "
            "example — uncomment and edit the ones you want to sweep."
        )
        help_lbl.setWordWrap(True)
        help_lbl.setTextFormat(QtCore.Qt.RichText)
        help_lbl.setStyleSheet(f"color:{TEXT_HINT}; font-size:13px;")
        cl.addWidget(help_lbl)

        example_row = QtWidgets.QHBoxLayout()
        example_btn = QtWidgets.QPushButton("Create example cfg")
        example_btn.setObjectName("secondary_btn")
        example_btn.setFixedHeight(self._BTN_H)
        example_btn.clicked.connect(self._save_example)
        example_row.addWidget(example_btn)
        example_row.addStretch()
        cl.addLayout(example_row)

        self._layout.addWidget(cfg_box)
        self._layout.addStretch()

        # ── Progress bars: overall (combinations) + current iteration (%) ──
        self._progress = QtWidgets.QProgressBar()
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.hide()
        self._layout.addWidget(self._progress)

        self._run_progress = QtWidgets.QProgressBar()
        self._run_progress.setRange(0, 100)
        self._run_progress.setValue(0)
        self._run_progress.setTextVisible(True)
        self._run_progress.setFormat("Current run: %p%")
        self._run_progress.hide()
        self._layout.addWidget(self._run_progress)

        # ── Live log (outside scroll, same convention as other panels) ──
        self._log = QtWidgets.QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QtGui.QFont("Consolas", 9))
        self._log.setMinimumHeight(200)
        self._log.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed
        )
        self._log.setStyleSheet(
            f"QPlainTextEdit {{ background:{GRAY_BG}; border:1px solid {GRAY_LINE}; "
            f"border-radius:6px; padding:6px; color:{TEXT_PRI}; margin:0 20px 8px 20px; "
            f"font-family:'Consolas','Courier New',monospace; }}"
        )
        self._log.hide()
        outer_layout.addWidget(self._log, 0)

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
        self._last_outdir = ""

    # ── Config loading ───────────────────────────────────────────────────

    def _on_load_clicked(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load batch config",
            filter="Config files (*.cfg *.txt *.ini);;All files (*)")
        if not path:
            return
        try:
            sweep = parse_sweep_config(path)
            validate_sweep(sweep)
            combos = expand_grid(sweep)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Invalid batch config", str(e))
            return
        self._cfg_path = path
        self._lbl_path.setText(os.path.basename(path))
        lines = ", ".join(f"{k} ({len(v)} values)" for k, v in sweep.items())
        self._lbl_summary.setText(
            f"<b>{len(combos)} combination(s)</b> from {len(sweep)} parameter(s): {lines}"
        )
        if len(combos) > WARN_COMBO_THRESHOLD:
            self._lbl_warn.setText(
                f"⚠ {len(combos)} combinations — each one is a full analysis run "
                f"on the current dataset, one after another. This may take a "
                f"long time to complete.")
            self._lbl_warn.show()
        else:
            self._lbl_warn.hide()
        self._start_btn.setEnabled(True)

    def _save_example(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Create example batch cfg", "ontbarcoder_batch.cfg",
            filter="Config files (*.cfg);;All files (*)")
        if not path:
            return
        try:
            with open(_example_cfg_path(), "r", encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            content = _EXAMPLE_CFG_FALLBACK
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Could not save file", str(e))

    def _emit_sweep(self):
        if self._cfg_path:
            self.sweepRequested.emit(self._cfg_path)

    def _open_output_folder(self):
        if self._last_outdir and os.path.isdir(self._last_outdir):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._last_outdir))

    # ── Public API (called by MainWindow) ──────────────────────────────────

    def append_log(self, text: str, level: str = "info"):
        self._log.show()
        self._log.appendPlainText(text)

    def set_progress(self, done: int, total: int, finished: bool = False):
        """`done` = combinations already completed, which is what drives the
        bar percentage; while the batch runs the label names the combination
        in flight (1-based), which is `done + 1`."""
        self._progress.show()
        self._progress.setRange(0, max(total, 1))
        self._progress.setValue(done)
        if finished:
            self._progress.setFormat(f"Completed {done}/{total}  —  %p%")
        else:
            self._progress.setFormat(
                f"Combination {min(done + 1, total)}/{total}  —  %p%")
            self.set_run_progress(0)

    def set_run_progress(self, pct: int):
        """Progress (0-100) of the combination currently running — phases of
        that one analysis, not how many combinations are done."""
        self._run_progress.show()
        self._run_progress.setValue(max(0, min(100, pct)))

    def set_running(self, running: bool):
        self._start_btn.setEnabled(not running and bool(self._cfg_path))
        self._load_btn.setEnabled(not running)
        self._stop_btn.setVisible(running)
        if running:
            self._log.clear()
            self._log.show()
            self._open_folder_btn.hide()
            self._start_time = time.monotonic()
            self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()
            self._run_progress.hide()

    def _tick_elapsed(self):
        elapsed = int(time.monotonic() - self._start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        t_str = f"{h}h {m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        self._progress.setToolTip(f"Elapsed: {t_str}")

    def on_finished(self, summary: dict):
        self.set_running(False)
        self._last_outdir = summary.get("outdir", "")
        if self._last_outdir and os.path.isdir(self._last_outdir):
            self._open_folder_btn.show()

        lines = [f"\n✓ Batch completed — {summary.get('n_runs', 0)} run(s)."]
        if "n_filt_min" in summary:
            lo, hi = summary["n_filt_min"], summary["n_filt_max"]
            rng = f"{lo}" if lo == hi else f"{lo}–{hi}"
            lines.append(
                f"  consensus_filtered.fa per run: {rng} barcode(s) — "
                f"see batch_run_summary.tsv for the per-run parameters/count.")
        if "n_samples" in summary:
            lines.append(
                f"  Deduplication across runs: {summary.get('n_samples', 0)} unique sample(s) "
                f"total, {summary.get('n_collapsed', 0)} with the same sequence in every run "
                f"they appear in, {summary.get('n_with_variants', 0)} with 2+ distinct "
                f"sequences across runs -> {summary.get('n_sequences_written', 0)} sequence(s) "
                f"written to unique_consensus_filtered.fasta "
                f"(see batch_dedup_report.tsv for the per-sample breakdown).")
        self.append_log("\n".join(lines))

    def on_error(self, msg: str):
        self.set_running(False)
        self.append_log(f"ERROR: {msg}")
        QtWidgets.QMessageBox.warning(self, "Parameter Batch", msg)
