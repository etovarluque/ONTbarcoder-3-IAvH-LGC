from __future__ import annotations
import os
import time
import datetime
from typing import List
from PyQt5 import QtCore, QtGui, QtWidgets
from .shared import *
from .shared import _tr, _get_base_dir, _profiles_dir
from .batch_sweep import (load_batch_config, swept_keys, merge_runs, count_fasta_records,
                          BATCH_STATE_FILE, combo_label, duplicate_combos, phase1_runs,
                          ineffective_keys, read_batch_state, read_batch_progress,
                          PHASE1_PARAM_KEYS)
from .compare_runs_report import list_batch_runs, compare_runs
from .batch_coverage_report import run_coverage_report
from .fasta_tools import _DragDropLineEdit

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


def _zone_style(selector: str, state: str) -> str:
    """Drop-zone look shared by this panel: white with a dashed border when
    empty, grey with a blue line while something is dragged over it (as the
    other drop zones), light green once loaded
    (the same colours as the app's file drop zones)."""
    bg, border = {
        "empty":  (WHITE, f"1.5px dashed {GRAY_LINE}"),
        "drag":   (DROP_DRAG_BG, DROP_DRAG_BORDER),
        "filled": (GREEN_LT, f"1.5px solid {GREEN_MID}"),
    }[state]
    return f"{selector} {{ background:{bg}; border:{border}; border-radius:8px; }}"


class _RunFolderList(QtWidgets.QListWidget):
    """Run folders to merge; accepts folders dropped from the file explorer."""
    foldersDropped = QtCore.pyqtSignal(list)

    _HINT = ("Drag run folders here, or use Add run folder…\n"
             "A folder holding several runs (e.g. output/) adds them all.")
    _EMPTY_H = 80     # compact while empty
    _FILLED_H = 130

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.model().rowsInserted.connect(self._fit_height)
        self.model().rowsRemoved.connect(self._fit_height)
        self.model().modelReset.connect(self._fit_height)
        self._fit_height()

    def _fit_height(self, *_):
        self.setMinimumHeight(self._FILLED_H if self.count() else self._EMPTY_H)
        self.setMaximumHeight(16777215 if self.count() else self._EMPTY_H)
        self._set_state("filled" if self.count() else "empty")

    def _set_state(self, state: str):
        self.setStyleSheet(_zone_style("QListWidget", state))

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            # Placeholder: says what the empty box is for (list + drop target)
            p = QtGui.QPainter(self.viewport())
            p.setPen(QtGui.QColor(TEXT_HINT))
            p.drawText(self.viewport().rect().adjusted(10, 0, -10, 0),
                       QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap, self._HINT)
            p.end()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_state("drag")
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dragLeaveEvent(self, e):
        self._fit_height()

    def dropEvent(self, e):
        self._fit_height()
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        paths = [p for p in paths if p and os.path.isdir(p)]
        if paths:
            self.foldersDropped.emit(paths)
        e.acceptProposedAction()


class _FolderSlot(QtWidgets.QFrame):
    """A titled drop target + Browse… for one folder (white/dashed when
    empty, blue while dragging, green once loaded). Subclasses decide which
    folders they take in _accept()."""
    changed = QtCore.pyqtSignal()

    HINT = "Drag a folder here."
    BROWSE_TITLE = "Pick a folder"

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("folderSlot")
        self.setAcceptDrops(True)
        self._reset_fields()   # folder (the folder taken), label (what is shown)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(make_label(title, size=16, bold=True))
        head.addStretch()
        self._clear_btn = QtWidgets.QPushButton("✕")
        self._clear_btn.setFixedSize(26, 26)
        self._clear_btn.setToolTip("Clear")
        self._clear_btn.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{TEXT_HINT}; border:none;"
            f" font-weight:bold; }} QPushButton:hover {{ color:{RED}; }}")
        self._clear_btn.clicked.connect(self.clear)
        self._clear_btn.hide()
        head.addWidget(self._clear_btn)
        lay.addLayout(head)

        self._lbl = make_label("", size=14, color=TEXT_HINT)
        self._lbl.setWordWrap(True)
        lay.addWidget(self._lbl, 1)

        self._browse_btn = QtWidgets.QPushButton("Browse…")
        self._browse_btn.setObjectName("secondary_btn")
        self._browse_btn.setFixedHeight(36)
        self._browse_btn.clicked.connect(self._browse)
        lay.addWidget(self._browse_btn, 0, QtCore.Qt.AlignLeft)
        self._render()

    def _set_state(self, state: str):
        self.setStyleSheet(_zone_style("QFrame#folderSlot", state))

    def _render(self):
        if self.folder:
            self._lbl.setText(self.label)
            self._lbl.setStyleSheet(f"color:{TEXT_PRI}; font-size:14px;")
            self.setToolTip(self.folder)
        else:
            self._lbl.setText(self.HINT)
            self._lbl.setStyleSheet(f"color:{TEXT_HINT}; font-size:14px;")
            self.setToolTip("")
        self._clear_btn.setVisible(bool(self.folder))
        self._set_state("filled" if self.folder else "empty")

    def _reset_fields(self):
        self.folder = self.label = ""

    def clear(self):
        self._reset_fields()
        self._render()
        self.changed.emit()

    def _browse(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, self.BROWSE_TITLE, os.path.join(_get_base_dir(), "output"))
        if path:
            self.set_path(path)

    def _accept(self, path: str) -> bool:
        """Take *path* (set folder/label); False (after warning) if unusable."""
        self.folder, self.label = path, os.path.basename(path.rstrip("/\\"))
        return True

    def set_path(self, path: str):
        if self._accept(path):
            self._render()
            self.changed.emit()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_state("drag")

    def dragLeaveEvent(self, e):
        self._render()

    def dropEvent(self, e):
        self._render()
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        paths = [x for x in paths if x and os.path.isdir(x)]
        if paths:
            self.set_path(paths[0])
        e.acceptProposedAction()


class _RunSlot(_FolderSlot):
    """One side of 'Compare two runs': a run folder. A sweep folder (…_sweep,
    older …_batch) is accepted too: one of its runs is then picked from a list."""
    HINT = ("Drag a run folder here, or a sweep folder (…_sweep) "
            "to pick one of its runs.")
    BROWSE_TITLE = "Compare two runs — pick a run folder (or a sweep folder)"

    def _reset_fields(self):
        super()._reset_fields()
        self.tag = ""         # short name used in the TSV file name
        self.batch_dir = ""   # sweep folder the run was picked from, if any

    def _accept(self, path: str) -> bool:
        """Take a run folder, or pick one run of a sweep folder."""
        if _is_run_folder(path):
            self.folder, self.batch_dir = path, ""
            self.label = os.path.basename(path.rstrip("/\\"))
            self.tag = self.label.replace("ont-barcoder_", "")
            return True
        try:
            runs = [r for r in list_batch_runs(path) if os.path.isdir(r[1])]
        except (OSError, ValueError):
            runs = None
        if not runs:
            QtWidgets.QMessageBox.warning(
                self, "Compare two runs",
                f"{os.path.basename(path)} is neither an analysis output folder "
                f"(no consensus_filtered.fa) nor a sweep folder with runs.")
            return False
        labels = [label for _n, _f, label in runs]
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, "Compare two runs",
            f"Pick a run of {os.path.basename(path)}:", labels, 0, False)
        if not ok:
            return False
        _n, folder, label = runs[labels.index(choice)]
        self.folder, self.label, self.batch_dir = folder, label, path
        self.tag = label.split(" ", 1)[0]
        return True


class _CheckedFolderSlot(_FolderSlot):
    """A folder that must contain one of NEEDS (glob patterns)."""
    NEEDS: tuple = ()
    WHAT = ""

    def _accept(self, path: str) -> bool:
        import glob
        if not any(glob.glob(os.path.join(path, n)) for n in self.NEEDS):
            QtWidgets.QMessageBox.warning(
                self, "Coverage report",
                f"{os.path.basename(path)} has no {' or '.join(self.NEEDS)}.\n\n"
                f"{self.WHAT}")
            return False
        return super()._accept(path)


class _SweepFolderSlot(_CheckedFolderSlot):
    HINT = ("Drag the sweep folder here (…_sweep, older …_batch). "
            "A Merge existing runs folder (…_merge) works too.")
    BROWSE_TITLE = "Coverage report — pick the sweep (or merge) folder"
    NEEDS = ("batch_run_summary.tsv", "merge_run_summary.tsv")
    WHAT = ("Pick a finished Parameter Sweep folder (…_sweep) or a "
            "Merge existing runs folder (…_merge).")


class _BestSeqFolderSlot(_CheckedFolderSlot):
    HINT = "Drag the Best Sequence output folder here (…_bestseq)."
    BROWSE_TITLE = "Coverage report — pick the Best Sequence output folder"
    # The report .tsv is always written; the FASTA files only when not empty.
    NEEDS = ("bestseq-*.tsv",)
    WHAT = "Pick a Best Sequence Selection output folder."


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
        self._lbl_title = make_label("Parameter Sweep (Optional)", size=19, bold=True)
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
        self._lbl_conv_only.hide()   # shown only in Real-Time mode (set_live_mode)
        self._layout.addWidget(self._lbl_conv_only)

        # ── Config file ──
        cfg_box = QtWidgets.QGroupBox("Sweep configuration")
        cfg_box.setStyleSheet(group_box_style())
        cl = QtWidgets.QVBoxLayout(cfg_box)
        cl.setSpacing(10)
        cl.setContentsMargins(16, 16, 16, 16)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(make_label("Sweep config:", color=TEXT_SEC))
        self._cfg_edit = _DragDropLineEdit(accepted_extensions=[".cfg", ".txt", ".ini"])
        self._cfg_edit.setReadOnly(True)
        self._cfg_edit.setPlaceholderText("No sweep config loaded… (or drag & drop a .cfg)")
        self._cfg_edit.textChanged.connect(self._on_cfg_text)
        row.addWidget(self._cfg_edit, 1)
        self._cfg_clear_btn = QtWidgets.QPushButton("✕")
        self._cfg_clear_btn.setFixedSize(26, 26)
        self._cfg_clear_btn.setToolTip("Clear")
        self._cfg_clear_btn.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{TEXT_HINT}; border:none;"
            f" border-radius:5px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:{RED_LT}; color:{RED}; }}")
        self._cfg_clear_btn.clicked.connect(self._clear_cfg)
        self._cfg_clear_btn.hide()
        row.addWidget(self._cfg_clear_btn)
        self._load_btn = QtWidgets.QPushButton("Browse…")
        self._load_btn.setObjectName("secondary_btn")
        self._load_btn.setFixedWidth(120)
        self._load_btn.clicked.connect(self._on_load_clicked)
        row.addWidget(self._load_btn)
        cl.addLayout(row)

        self._resume_btn = QtWidgets.QPushButton("Resume sweep…")
        self._resume_btn.setObjectName("secondary_btn")
        self._resume_btn.setFixedHeight(44)
        self._resume_btn.setEnabled(False)
        self._resume_btn.setToolTip(
            "Pick an interrupted sweep folder (output/ont-barcoder_*_sweep) to "
            "run only the combinations it has not completed yet, with the same "
            ".cfg and parameters it was started with, then merge every run.")
        self._resume_btn.clicked.connect(self._on_resume_clicked)

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

        self._preview_table = QtWidgets.QTableWidget()
        self._preview_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._preview_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self._preview_table.verticalHeader().setVisible(False)
        self._preview_table.horizontalHeader().setStretchLastSection(True)
        self._preview_table.setStyleSheet(
            f"QTableWidget {{ background:{WHITE}; border:1px solid {GRAY_LINE};"
            f" border-radius:8px; font-size:13px; gridline-color:{GRAY_LINE}; }}"
            f"QHeaderView::section {{ background:{GRAY_BG}; color:{TEXT_SEC};"
            f" font-weight:600; border:none; border-bottom:1px solid {GRAY_LINE};"
            f" padding:4px 8px; }}")
        self._preview_table.hide()
        cl.addWidget(self._preview_table)

        help_lbl = QtWidgets.QLabel(
            "<b>Grid:</b> one line per parameter, <code>key = v1, v2, v3</code> — every "
            "combination of every listed value is run. <b>Combo list:</b> first line "
            "<code># combos</code>, then one full combination per line, "
            "<code>key=value, key=value, ...</code> — for re-running specific "
            "combinations rather than every combination of a grid. "
            "<code>#</code> for comments in both. Each button below writes a "
            "self-documented example of its shape, ready to edit and load."
        )
        help_lbl.setWordWrap(True)
        help_lbl.setTextFormat(QtCore.Qt.RichText)
        help_body = QtWidgets.QWidget()
        hb = QtWidgets.QVBoxLayout(help_body)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(8)
        hb.addWidget(help_lbl)
        example_row = QtWidgets.QHBoxLayout()
        example_btn = QtWidgets.QPushButton("Create example (grid)")
        example_btn.setObjectName("secondary_btn")
        example_btn.setFixedHeight(36)
        example_btn.clicked.connect(self._save_example)
        example_row.addWidget(example_btn)
        example_combos_btn = QtWidgets.QPushButton("Create example (combo list)")
        example_combos_btn.setObjectName("secondary_btn")
        example_combos_btn.setFixedHeight(36)
        example_combos_btn.clicked.connect(self._save_example_combos)
        example_row.addWidget(example_combos_btn)
        example_row.addStretch()
        hb.addLayout(example_row)
        cl.addWidget(make_collapsible(help_body, "Config file format (grid / combo list)"))

        self._layout.addWidget(cfg_box)

        # ── Additional tools: collapsed by default so the batch itself reads
        # as the panel's only required input ──
        tools_title = make_label("Additional tools (optional)", size=17, bold=True)
        tools_title.setContentsMargins(0, 10, 0, 0)
        self._layout.addWidget(tools_title)
        tools_desc = make_label(
            "Independent of the sweep above — each works on run folders that "
            "already exist. Click a title to open it.", color=TEXT_SEC)
        tools_desc.setWordWrap(True)
        self._layout.addWidget(tools_desc)

        # ── Merge existing runs (analyses already run by hand, no sweep) ──
        merge_box = QtWidgets.QGroupBox()
        merge_box.setStyleSheet(group_box_style(titled=False))
        ml = QtWidgets.QVBoxLayout(merge_box)
        ml.setSpacing(10)
        ml.setContentsMargins(16, 16, 16, 16)

        merge_help = QtWidgets.QLabel(
            "Compare analyses already run by hand, without a sweep: add their "
            "output folders (button or drag &amp; drop) and merge them into the same "
            "files a sweep produces — <code>unique_consensus_filtered.fasta</code> "
            "and, from the runs that had <b>Detect intra-sample sequence variants</b> "
            "on, <code>unique_secondary_variants.fasta</code>. Adding a folder that "
            "holds several runs (e.g. <code>output/</code>) adds every run inside it."
        )
        merge_help.setWordWrap(True)
        merge_help.setTextFormat(QtCore.Qt.RichText)
        ml.addWidget(make_collapsible(merge_help, "How merging works"))

        self._merge_list = _RunFolderList()
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

        self._layout.addWidget(self._tool_section(
            "Merge existing runs",
            "Merge runs made by hand into one unique-sequences FASTA.", merge_box))

        # ── Compare two runs (per-sample A/B report) ──
        cmp_box = QtWidgets.QGroupBox()
        cmp_box.setStyleSheet(group_box_style(titled=False))
        cml = QtWidgets.QVBoxLayout(cmp_box)
        cml.setSpacing(10)
        cml.setContentsMargins(16, 16, 16, 16)

        cmp_help = QtWidgets.QLabel(
            "Per-sample A/B comparison of two analyses of the same dataset — "
            "e.g. two combinations of a sweep, or a run made with an older "
            "version: which samples gain, lose or change their QC-compliant "
            "(<code>consensus_no_errors.fa</code>) or filtered "
            "(<code>consensus_filtered.fa</code>) barcode, and whose secondary "
            "variants appear or disappear. Unlike the Compare panel, it looks at "
            "the whole analysis result, not at sequences of one file. Give each side "
            "a run folder, or a sweep folder to pick one of its combinations. The "
            "per-sample TSV is written to the sweep folder (two runs of the same "
            "sweep) or else to run A's folder."
        )
        cmp_help.setWordWrap(True)
        cmp_help.setTextFormat(QtCore.Qt.RichText)
        cml.addWidget(make_collapsible(cmp_help, "What this compares"))

        slots_row = QtWidgets.QHBoxLayout()
        slots_row.setSpacing(12)
        self._cmp_a = _RunSlot("Run A")
        self._cmp_b = _RunSlot("Run B")
        for slot in (self._cmp_a, self._cmp_b):
            slot.setMinimumHeight(130)
            slot.changed.connect(self._sync_cmp_btn)
            slots_row.addWidget(slot, 1)
        cml.addLayout(slots_row)
        self._lbl_cmp_same = make_label(
            "Run A and Run B are the same run: pick a different one on either side.",
            size=14, color=RED)
        self._lbl_cmp_same.hide()
        cml.addWidget(self._lbl_cmp_same)

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

        self._layout.addWidget(self._tool_section(
            "Compare two runs",
            "Per-sample A/B report: barcodes gained, lost or changed.", cmp_box))
        self._cmp_batch_dir = ""

        # ── Coverage report (minimal combination set) ──
        cov_box = QtWidgets.QGroupBox()
        cov_box.setStyleSheet(group_box_style(titled=False))
        cvl = QtWidgets.QVBoxLayout(cov_box)
        cvl.setSpacing(10)
        cvl.setContentsMargins(16, 16, 16, 16)

        cov_help = QtWidgets.QLabel(
            "After a sweep → merge → BLAST → Best Sequence: finds which "
            "combinations reproduce each taxonomically-identified best sequence "
            "(as consensus or as secondary variant) and the fewest combinations "
            "that together recover them all. Writes "
            "<code>parameter_batch_coverage_report.xlsx</code> and "
            "<code>minimal_run_set.cfg</code> — a combo-list config with just "
            "those combinations, ready to load for the next dataset — into the "
            "Best Sequence folder. Also works on a <b>Merge existing runs</b> "
            "folder (…_merge): the report then names the minimal set by run "
            "folder, and no .cfg is written (those runs record no parameters)."
        )
        cov_help.setWordWrap(True)
        cov_help.setTextFormat(QtCore.Qt.RichText)
        cvl.addWidget(make_collapsible(cov_help, "What this does"))

        cov_slots = QtWidgets.QHBoxLayout()
        cov_slots.setSpacing(12)
        self._cov_sweep = _SweepFolderSlot("Sweep/Merge folder")
        self._cov_bestseq = _BestSeqFolderSlot("Best Sequence folder")
        for slot in (self._cov_sweep, self._cov_bestseq):
            slot.setMinimumHeight(130)
            slot.changed.connect(self._sync_cov_btn)
            cov_slots.addWidget(slot, 1)
        cvl.addLayout(cov_slots)

        cov_row = QtWidgets.QHBoxLayout()
        cov_row.addStretch()
        self._cov_btn = QtWidgets.QPushButton("Create coverage report  →")
        self._cov_btn.setObjectName("primary_btn")
        self._cov_btn.setFixedHeight(self._BTN_H)
        self._cov_btn.setEnabled(False)
        self._cov_btn.clicked.connect(self._on_cov_run)
        cov_row.addWidget(self._cov_btn)
        cvl.addLayout(cov_row)

        self._layout.addWidget(self._tool_section(
            "Coverage report (minimal combination set)",
            "After BLAST + Best Sequence: the fewest combinations that recover "
            "every identified sequence.", cov_box))
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

        self._open_folder_btn = QtWidgets.QPushButton("Open folder")
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
            "already completed are kept and merged; use Resume sweep… later to "
            "run the rest (the aborted one is re-run from scratch).")
        self._stop_now_btn.clicked.connect(self._on_stop_now_clicked)
        fl.addWidget(self._stop_now_btn)

        fl.addStretch()

        self._lbl_start_hint = make_label("", size=14, color=TEXT_HINT)
        fl.addWidget(self._lbl_start_hint)
        fl.addSpacing(10)
        fl.addWidget(self._resume_btn)

        self._start_btn = QtWidgets.QPushButton("Start sweep  →")
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
        self._live = False
        self._combos: list = []
        self._cfg_mode = ""
        self._params_provider = None   # () -> current Parameters-panel values
        self._sync_start_btn()

    def set_params_provider(self, provider):
        """provider() -> the Parameters-panel values a sweep would start from,
        used to warn about swept keys that cannot change the result."""
        self._params_provider = provider

    def set_live_mode(self, live: bool):
        """Sweeps run in Conventional mode only."""
        self._live = live
        self._lbl_conv_only.setVisible(live)
        self._sync_start_btn()

    def showEvent(self, event):
        super().showEvent(event)
        # The Parameters panel may have changed since the .cfg was loaded.
        self._update_notes()

    def set_dataset_loaded(self, loaded: bool):
        """The sweep needs the dataset from Input files; merging does not."""
        self._dataset_loaded = loaded
        self._sync_start_btn()

    def _sync_start_btn(self):
        ok = (bool(self._cfg_path) and self._dataset_loaded
              and not self._running and not self._live)
        self._start_btn.setEnabled(ok)
        self._resume_btn.setEnabled(self._dataset_loaded and not self._running
                                    and not self._live)
        if self._running:
            hint = ""
        elif self._live:
            hint = "Sweeps run in Conventional mode only."
        elif not self._dataset_loaded:
            hint = "Load a dataset in Input files to start."
        elif not self._cfg_path:
            hint = "Load a sweep config to start."
        else:
            hint = ""
        self._lbl_start_hint.setText(hint)
        self._start_btn.setToolTip(
            "" if self._dataset_loaded else
            "Load a dataset in the Input files panel to run a sweep. "
            "Merging existing runs does not need one.")

    @staticmethod
    def _interrupted_batches() -> list:
        """[(folder, done, total)] of the sweeps in output/ with combinations
        still to run, newest first."""
        import glob
        found = []
        out = os.path.join(_get_base_dir(), "output")
        # Sweep folders are named …_sweep; older versions wrote …_batch.
        dirs = glob.glob(os.path.join(out, "*_sweep")) + glob.glob(os.path.join(out, "*_batch"))
        for d in sorted(dirs, key=os.path.basename, reverse=True):
            if not os.path.isfile(os.path.join(d, BATCH_STATE_FILE)):
                continue
            try:
                total = int(read_batch_state(d).get("n_combos") or 0)
                done = len(read_batch_progress(d))
            except Exception:
                continue
            if total and done < total:
                found.append((d, done, total))
        return found

    def _on_resume_clicked(self):
        path = ""
        found = self._interrupted_batches()
        if found:
            other = "Other folder…"
            labels = [f"{os.path.basename(d)}  —  {done}/{total} combination(s) completed"
                      for d, done, total in found]
            choice, ok = QtWidgets.QInputDialog.getItem(
                self, "Resume sweep", "Interrupted sweeps found in output/:",
                labels + [other], 0, False)
            if not ok:
                return
            if choice != other:
                path = found[labels.index(choice)][0]
        if not path:
            path = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Resume sweep — pick the interrupted sweep folder",
                os.path.join(_get_base_dir(), "output"))
        if not path:
            return
        if not os.path.isfile(os.path.join(path, BATCH_STATE_FILE)):
            QtWidgets.QMessageBox.warning(
                self, "Resume sweep",
                f"{os.path.basename(path)} is not a resumable sweep folder "
                f"(no {BATCH_STATE_FILE}). Only batches started with this "
                f"version or later can be resumed.")
            return
        self.resumeRequested.emit(path)

    # ── Config loading ───────────────────────────────────────────────────

    def _on_load_clicked(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load sweep config", _profiles_dir(),
            "Config files (*.cfg *.txt *.ini);;All files (*)")
        if path:
            self._load_cfg(path)

    def _on_cfg_text(self, text: str):
        """A .cfg dropped on the field: load it (or restore the previous one)."""
        text = text.strip()
        if text and text != self._cfg_path and not self._load_cfg(text):
            self._show_cfg_path(self._cfg_path)

    def _show_cfg_path(self, path: str):
        self._cfg_edit.blockSignals(True)
        self._cfg_edit.setText(path)
        self._cfg_edit.blockSignals(False)
        self._cfg_edit.setToolTip(path)
        self._cfg_clear_btn.setVisible(bool(path))

    def _clear_cfg(self):
        self._cfg_path, self._cfg_count, self._combos, self._cfg_mode = "", 0, [], ""
        self._show_cfg_path("")
        self._lbl_summary.setText("")
        self._preview_table.hide()
        self._update_notes()
        self._sync_start_btn()

    def _fill_preview(self, combos):
        """One row per combination, one column per swept parameter. A key
        absent from a combo-list line keeps the Parameters-panel value."""
        keys = []
        for c in combos:
            for k in c:
                if k not in keys:
                    keys.append(k)
        shown = combos[:1000]
        t = self._preview_table
        t.clear()
        t.setColumnCount(len(keys) + 1)
        t.setRowCount(len(shown))
        t.setHorizontalHeaderLabels(["#"] + keys)
        for r, c in enumerate(shown):
            t.setItem(r, 0, QtWidgets.QTableWidgetItem(str(r + 1)))
            for j, k in enumerate(keys, start=1):
                item = QtWidgets.QTableWidgetItem(c.get(k, "(panel)"))
                if k not in c:
                    item.setForeground(QtGui.QColor(TEXT_HINT))
                t.setItem(r, j, item)
        t.resizeColumnsToContents()
        row_h = t.verticalHeader().defaultSectionSize()
        t.setFixedHeight(min(len(shown), 8) * row_h + t.horizontalHeader().height() + 6)
        t.show()

    def _load_cfg(self, path: str) -> bool:
        """Parse `path` and show its combination count. Returns False (with a
        warning) if it does not parse."""
        try:
            combos, mode, sweep = load_batch_config(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Invalid sweep config", str(e))
            return False
        self._cfg_path = path
        self._cfg_count = len(combos)
        self._combos = combos
        self._cfg_mode = mode
        self._show_cfg_path(path)
        n_keys = len(sweep) if mode == "grid" else len(swept_keys(combos))
        shape = "grid" if mode == "grid" else "explicit list"
        try:
            runs = phase1_runs(combos)[0]
        except ValueError:
            runs = "?"
        self._lbl_summary.setText(
            f"<b>{len(combos)} combination(s)</b>  ·  {n_keys} parameter(s), {shape}"
            f"  ·  Phase 1 (demultiplexing) runs: {runs}")
        self._fill_preview(combos)
        self._update_notes()
        self._sync_start_btn()
        return True

    def _update_notes(self):
        """Warnings about the loaded .cfg, shown before the sweep starts:
        size, repeated combinations, Phase 1 re-runs caused by the line
        order, and swept keys that cannot change the result."""
        combos = self._combos
        if not combos:
            self._lbl_warn.hide()
            return
        notes = []
        n = len(combos)
        if n > WARN_COMBO_THRESHOLD:
            notes.append(f"{n} combinations — each one is a full analysis run on the "
                         f"current dataset, one after another. This may take a long "
                         f"time to complete.")
        try:
            dups = duplicate_combos(combos)
            runs, best = phase1_runs(combos)
        except ValueError:
            dups, runs, best = [], 0, 0
        if dups:
            d, first = dups[0]
            notes.append(f"{len(dups)} combination(s) repeat an earlier one (e.g. #{d} = "
                         f"#{first}) and would give the same result: remove the "
                         f"repeated values from the .cfg.")
        if runs > best:
            demux = ", ".join(k for k in PHASE1_PARAM_KEYS if k in swept_keys(combos))
            how = ("listed first in the .cfg" if self._cfg_mode == "grid"
                   else "kept together (lines sorted by them)")
            notes.append(f"Demultiplexing (Phase 1) will run {runs} times; {best} would "
                         f"be enough with the demultiplexing parameters ({demux}) {how}.")
        base = None
        if self._params_provider is not None:
            try:
                base = self._params_provider()
            except Exception:
                base = None
        if base:
            keys = swept_keys(combos)
            if (any(k.startswith("resolve_mixed.") for k in keys)
                    and not base.get("resolve_mixed", {}).get("enabled")
                    and "resolve_mixed.enabled" not in keys):
                notes.append("'Detect intra-sample sequence variants' is off in the "
                             "Parameters panel: it will be turned ON for this sweep, "
                             "because the .cfg varies resolve_mixed.* values.")
            for key, why in ineffective_keys(keys, base):
                notes.append(f"<b>{key}</b> has no effect: {why}, so its values give "
                             f"identical runs.")
        if notes:
            self._lbl_warn.setText("<br>".join("• " + t for t in notes))
            self._lbl_warn.show()
        else:
            self._lbl_warn.hide()

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
            return
        reply = QtWidgets.QMessageBox.question(
            self, dialog_title,
            f"{os.path.basename(path)} created.\n\nLoad it as the sweep config and "
            f"open it in your text editor to adjust the values? Changes saved in "
            f"the editor are picked up when the sweep starts.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes)
        if reply == QtWidgets.QMessageBox.Yes and self._load_cfg(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _save_example(self):
        self._save_template("Create example sweep cfg (grid)", "my_batch.cfg",
                             _example_cfg_path(), _EXAMPLE_CFG_FALLBACK)

    def _save_example_combos(self):
        self._save_template("Create example sweep cfg (combo list)",
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
                self, "Parameter Sweep",
                f"{os.path.basename(self._cfg_path)} changed since it was loaded: "
                f"it now expands to {self._cfg_count} combination(s) instead of "
                f"{shown}. Start the sweep with the new content?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if reply != QtWidgets.QMessageBox.Yes:
                return
        self.sweepRequested.emit(self._cfg_path)

    def _on_stop_now_clicked(self):
        reply = QtWidgets.QMessageBox.question(
            self, "Stop now",
            "Abort the combination currently running? Its partial output is "
            "discarded; completed combinations are kept, and Resume sweep… "
            "can run the rest later.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if reply == QtWidgets.QMessageBox.Yes:
            self.stopNowRequested.emit()

    # ── Collapsible tool sections ────────────────────────────────────────
    def _tool_section(self, title: str, subtitle: str,
                      body: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """Card with a clickable '▸ title — subtitle' header that shows or
        hides *body*; starts collapsed."""
        card = QtWidgets.QFrame()
        card.setObjectName("toolSection")
        card.setStyleSheet(
            f"QFrame#toolSection {{ background:transparent; border:1px solid {GRAY_LINE};"
            f" border-radius:10px; }}")
        lay = QtWidgets.QVBoxLayout(card)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)

        # QPushButton (not QToolButton) so text-align:left is honoured.
        btn = QtWidgets.QPushButton()
        btn.setCheckable(True)
        btn.setCursor(QtCore.Qt.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton { border:none; background:transparent; text-align:left;"
            f" color:{TEXT_PRI}; font-size:16px; font-weight:600; padding:6px 0; }}"
            f"QPushButton:hover {{ color:{BLUE}; }}"
            "QPushButton:checked { background:transparent; }")
        sub = make_label(subtitle, size=14, color=TEXT_SEC)
        sub.setWordWrap(True)
        sub.setContentsMargins(20, 0, 0, 4)
        body.setStyleSheet("QGroupBox { border:none; margin-top:0; }")

        def _sync(on):
            btn.setText(("▾  " if on else "▸  ") + title)
            body.setVisible(on)
            sub.setVisible(not on)
        btn.toggled.connect(_sync)

        lay.addWidget(btn)
        lay.addWidget(sub)
        lay.addWidget(body)
        # After the widgets are parented: setVisible(True) on a parentless
        # widget would flash it as a top-level window at startup.
        _sync(False)
        return card

    # ── Compare two runs ─────────────────────────────────────────────────

    def _sync_cmp_btn(self, *_):
        a, b = self._cmp_a, self._cmp_b
        # Remembered as the start folder of the Coverage report picker
        self._cmp_batch_dir = a.batch_dir or b.batch_dir or self._cmp_batch_dir
        same = bool(a.folder and b.folder) and (
            os.path.normcase(os.path.abspath(a.folder))
            == os.path.normcase(os.path.abspath(b.folder)))
        self._lbl_cmp_same.setVisible(same)
        self._cmp_btn.setEnabled(bool(a.folder and b.folder) and not same
                                 and not self._running)

    def _on_cmp_run(self):
        a, b = self._cmp_a, self._cmp_b
        if not (a.folder and b.folder):
            return
        fa, fb = a.folder, b.folder
        if os.path.normcase(os.path.abspath(fa)) == os.path.normcase(os.path.abspath(fb)):
            QtWidgets.QMessageBox.warning(
                self, "Compare two runs", "Run A and Run B are the same run.")
            return
        same_sweep = (a.batch_dir and b.batch_dir and
                      os.path.normcase(os.path.abspath(a.batch_dir))
                      == os.path.normcase(os.path.abspath(b.batch_dir)))
        out_dir = a.batch_dir if same_sweep else fa
        out_path = os.path.join(out_dir, f"compare_runs_{a.tag}_vs_{b.tag}.tsv")
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            lines = compare_runs(fa, fb, a.label, b.label, out_path,
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

    def _sync_cov_btn(self, *_):
        self._cov_btn.setEnabled(bool(self._cov_sweep.folder and self._cov_bestseq.folder)
                                 and not self._running)

    def _on_cov_run(self):
        lines: List[str] = []
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            res = run_coverage_report(self._cov_sweep.folder, self._cov_bestseq.folder,
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
        mismatch = res["n_winners"] > 0 and res["n_covered"] == 0
        if mismatch:
            lines.append("WARNING: no identified sequence was found in any run of this "
                         "sweep — the two folders are probably from different datasets.")
        self.append_log("\n".join(lines))
        self._last_outdir = os.path.dirname(res["xlsx"])
        self._open_folder_btn.show()
        if mismatch:
            QtWidgets.QMessageBox.warning(
                self, "Coverage report",
                f"None of the {res['n_winners']} identified sequence(s) of "
                f"{os.path.basename(self._cov_bestseq.folder)} appears in any run of "
                f"{os.path.basename(self._cov_sweep.folder)}.\n\n"
                "The Best Sequence result and the sweep are probably from different "
                "datasets: check that Best Sequence was run on this sweep's "
                "unique_consensus_filtered.fasta.")
        elif res["cfg"]:
            reply = QtWidgets.QMessageBox.question(
                self, "Coverage report",
                f"{res['n_chosen']} combination(s) recover {res['n_covered']} of "
                f"{res['n_winners']} identified sequence(s).\n\n"
                f"Load {os.path.basename(res['cfg'])} now as the sweep config?",
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
            lines = [f"\n■ Sweep stopped — {n_runs}/{n_total} combination(s) completed. "
                     f"Use Resume sweep… on this folder to run the rest."]
        else:
            lines = [f"\n✓ Sweep completed — {n_runs} run(s)."]
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
                f"sequences across runs -> {summary.get('n_sequences_written', 0)} sequence(s)"
                + (" written to unique_consensus_filtered.fasta "
                   if summary.get("n_sequences_written") else " (no FASTA written) ")
                + "(see batch_dedup_report.tsv for the per-sample breakdown).")
        if "variants" in summary:
            v = summary["variants"]
            lines.append(
                f"  Secondary variants (raw + corrected) across runs: "
                f"{v.get('n_samples', 0)} sample(s) with variants -> "
                f"{v.get('n_sequences_written', 0)} distinct sequence(s)"
                + (" written to unique_secondary_variants.fasta "
                   if v.get("n_sequences_written") else " (no FASTA written) ")
                + "(see batch_variants_dedup_report.tsv).")
        return lines

    def on_error(self, msg: str):
        self.set_running(False)
        self.append_log(f"ERROR: {msg}")
        QtWidgets.QMessageBox.warning(self, "Parameter Sweep", msg)
