from __future__ import annotations
import os
import re
import itertools
import edlib
import xlsxwriter
from collections import Counter
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtCore, QtGui, QtWidgets
from .shared import *
from .shared import _get_base_dir, _tr, _json_mod


# ═══════════════════════════════════════════════════════════════════════════
# COMPARE RESULTS WINDOW
# ═══════════════════════════════════════════════════════════════════════════

class _CompareResultsWindow(QtWidgets.QDialog):
    """Separate window showing the table of comparison results."""

    # Background colors by state
    _BG = {
        "Identical":           "#EAF3DE",
        "Compatible (IUPAC)":  "#E6F1FB",
        "Different":           "#FCEBEB",
        "Only in reference":      "#FAEEDA",
        "No reference":        "#F5F5F3",
        "Unique":              "#EBEBEB",
    }
    # Status pad colors
    _PILL_BG = {
        "Identical":           ("#3B6D11", "#EAF3DE"),
        "Compatible (IUPAC)":  ("#185FA5", "#E6F1FB"),
        "Different":           ("#A32D2D", "#FCEBEB"),
        "Only in reference":      ("#854F0B", "#FAEEDA"),
        "No reference":        ("#5A5A5A", "#EBEBEB"),
        "Unique":              ("#4A4A4A", "#DCDCDC"),
    }

    def __init__(self, rows, headers, outdir, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Comparison results — ONTbarcoder")
        self.resize(1200, 680)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
        self.setStyleSheet(f"background-color: {WHITE}; font-family: 'Segoe UI', Arial, sans-serif;")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Top bar with statistics ──────────────────────────────────
        topbar = QtWidgets.QWidget()
        topbar.setStyleSheet(f"background-color: #1A1A2E;")
        topbar.setFixedHeight(62)
        tb_layout = QtWidgets.QHBoxLayout(topbar)
        tb_layout.setContentsMargins(16, 6, 16, 6)
        tb_layout.setSpacing(12)

        n_identical = sum(1 for r in rows if r.get("State") == "Identical")
        n_compat    = sum(1 for r in rows if r.get("State") == "Compatible (IUPAC)")
        n_diff      = sum(1 for r in rows if r.get("State") == "Different")
        n_only      = sum(1 for r in rows if r.get("State", "").startswith("Unique"))
        n_only_ref  = sum(1 for r in rows if r.get("State") == "Only in reference")
        n_no_ref    = sum(1 for r in rows if r.get("State") == "No reference")

        def _stat_pill(label, count, bg, fg):
            w = QtWidgets.QLabel(f"{label}: <b>{count}</b>")
            w.setTextFormat(QtCore.Qt.RichText)
            w.setStyleSheet(
                f"background-color:{bg}; color:{fg}; border-radius:8px;"
                f" padding:3px 10px; font-size:15px;"
            )
            return w

        title_lbl = QtWidgets.QLabel(
            f"<b style='color:white;font-size:18px;'>Comparison</b>"
            f"<span style='color:#9AAFCC;font-size:15px;'>  —  {len(rows)} IDs</span>")
        title_lbl.setTextFormat(QtCore.Qt.RichText)
        tb_layout.addWidget(title_lbl)
        tb_layout.addSpacing(10)

        pills = [
            ("Identical",         n_identical, "#2D6A0A", "#C5EAAB"),
            ("Compatible IUPAC",     n_compat,    "#0D4D8A", "#A8CCF0"),
            ("Different",         n_diff,      "#8B1A1A", "#F5AAAA"),
            ("Unique",            n_only,      "#444444", "#CCCCCC"),
        ]
        for lbl, cnt, fg, bg in pills:
            if cnt:
                tb_layout.addWidget(_stat_pill(lbl, cnt, bg, fg))
        if n_only_ref:
            tb_layout.addWidget(_stat_pill("Only ref", n_only_ref, "#6B3D0A", "#F5D9A8"))
        if n_no_ref:
            tb_layout.addWidget(_stat_pill("No ref", n_no_ref, "#3A3A3A", "#DDDDDD"))
        tb_layout.addStretch()
        layout.addWidget(topbar)

        # ── Ruta de salida ────────────────────────────────────────────────────
        if outdir:
            path_bar = QtWidgets.QWidget()
            path_bar.setStyleSheet(f"background-color: #F0EEE8; border-bottom: 1px solid {GRAY_LINE};")
            path_bar.setFixedHeight(30)
            pb_layout = QtWidgets.QHBoxLayout(path_bar)
            pb_layout.setContentsMargins(20, 0, 16, 0)
            path_lbl = QtWidgets.QLabel(f"<span style='color:{TEXT_HINT};'>📁 Salida:</span>"
                                         f" <span style='color:{TEXT_SEC};'>{outdir}</span>")
            path_lbl.setTextFormat(QtCore.Qt.RichText)
            path_lbl.setStyleSheet("font-size: 11px;")
            pb_layout.addWidget(path_lbl, 1)
            layout.addWidget(path_bar)

        # ── Tabla ─────────────────────────────────────────────────────────────
        table = QtWidgets.QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setShowGrid(False)
        table.setAlternatingRowColors(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setHighlightSections(False)
        table.setFocusPolicy(QtCore.Qt.NoFocus)
        table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {WHITE};
                border: none;
                outline: none;
                font-size: 13px;
                gridline-color: transparent;
            }}
            QTableWidget::item {{
                padding: 5px 10px;
                border-bottom: 1px solid {GRAY_LINE};
            }}
            QTableWidget::item:selected {{
                background-color: #CDE0F5;
                color: #1A1A2E;
            }}
            QHeaderView::section {{
                background-color: #1A1A2E;
                color: white;
                font-weight: 600;
                font-size: 12px;
                padding: 7px 10px;
                border: none;
                border-right: 1px solid #2E3A50;
            }}
            QHeaderView::section:last {{
                border-right: none;
            }}
            QScrollBar:vertical {{
                width: 8px;
                background: {GRAY_BG};
            }}
            QScrollBar::handle:vertical {{
                background: {GRAY_LINE};
                border-radius: 4px;
                min-height: 20px;
            }}
            QScrollBar:horizontal {{
                height: 8px;
                background: {GRAY_BG};
            }}
            QScrollBar::handle:horizontal {{
                background: {GRAY_LINE};
                border-radius: 4px;
                min-width: 20px;
            }}
        """)
        table.horizontalHeader().setMinimumSectionSize(60)

        color_map = {
            "Identical":           QtGui.QColor("#EAF3DE"),
            "Compatible (IUPAC)":  QtGui.QColor("#E6F1FB"),
            "Different":           QtGui.QColor("#FCEBEB"),
            "Only in reference":      QtGui.QColor("#FAEEDA"),
            "No reference":        QtGui.QColor("#F0F0EE"),
        }

        # "Status" column receives widget with color pill
        estado_col = headers.index("State") if "State" in headers else -1

        for i, row in enumerate(rows):
            table.setRowHeight(i, 32)
            estado = row.get("State", "")
            bg = color_map.get(estado)
            if not bg and estado.startswith("Unique"):
                bg = QtGui.QColor("#EBEBEB")

            for j, h in enumerate(headers):
                val = str(row.get(h, ""))
                item = QtWidgets.QTableWidgetItem(val)
                item.setTextAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
                if bg:
                    item.setBackground(QtGui.QBrush(bg))
                # Columna ID en negrita
                if h == "ID":
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                table.setItem(i, j, item)

            # Replace State cell with color chip
            if estado_col >= 0:
                # "Unique in <file>" maps to the generic "Unique" pill; every
                # other state must match its key exactly.
                pill_key = "Unique" if estado.startswith("Unique") else (
                    estado if estado in self._PILL_BG else None)
                if pill_key:
                    fg_col, bg_col = self._PILL_BG[pill_key]
                    pill = QtWidgets.QLabel(f"  {estado}  ")
                    pill.setAlignment(QtCore.Qt.AlignCenter)
                    pill.setStyleSheet(
                        f"background-color:{bg_col}; color:{fg_col};"
                        f" border-radius:9px; font-size:11px; font-weight:600;"
                        f" padding:1px 6px;"
                    )
                    if bg:
                        container = QtWidgets.QWidget()
                        container.setStyleSheet(f"background-color:{bg.name()};")
                        cl = QtWidgets.QHBoxLayout(container)
                        cl.setContentsMargins(4, 2, 4, 2)
                        cl.addWidget(pill)
                        table.setCellWidget(i, estado_col, container)

        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)

        # ── Bottom bar ────────────────────────── ──────────────────────────
        footer = QtWidgets.QWidget()
        footer.setStyleSheet(f"background-color:{GRAY_BG}; border-top:1px solid {GRAY_LINE};")
        footer.setFixedHeight(52)
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(20, 8, 20, 8)
        fl.addStretch()
        if outdir:
            open_btn = QtWidgets.QPushButton("Open folder  📂")
            open_btn.setFixedHeight(36)
            open_btn.setStyleSheet(
                f"QPushButton {{ background:{BLUE_LIGHT}; color:{BLUE}; border:1px solid #B8D4F0;"
                f" border-radius:7px; padding:4px 14px; font-size:13px; }}"
                f"QPushButton:hover {{ background:{BLUE}; color:white; }}"
            )
            open_btn.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(outdir)))
            fl.addWidget(open_btn)
            fl.addSpacing(8)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.setFixedHeight(36)
        close_btn.setStyleSheet(
            f"QPushButton {{ background:{GRAY_LINE}; color:{TEXT_SEC}; border:none;"
            f" border-radius:7px; padding:4px 18px; font-size:13px; }}"
            f"QPushButton:hover {{ background:#C8C6C0; color:{TEXT_PRI}; }}"
        )
        close_btn.clicked.connect(self.close)
        fl.addWidget(close_btn)
        layout.addWidget(footer)


# ═══════════════════════════════════════════════════════════════════════════
# COMPARE PANEL
# ═══════════════════════════════════════════════════════════════════════════

class ComparePanel(QtWidgets.QWidget):
    # files, mode ("sets"|"ref"), ref_basename, outdir, extract_cfg (dict)
    # extract_cfg = {"id_patterns": [...], "regex_patterns": [...], "normalize": {...}}
    compareRequested = QtCore.pyqtSignal(list, str, str, str, dict)

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
        self._layout.setSpacing(16)
        scroll.setWidget(self._inner)
        outer_layout.addWidget(scroll, 1)

        self._lbl_compare_title = make_label("Compare barcode sets", size=19, bold=True)
        self._lbl_compare_desc = make_label(
            "Compare multiFASTA files from different runs. "
            "The sample ID is extracted from the FASTA header based on a delimiter and occurrence. "
            "Use Advanced when files follow different header conventions: list several regex "
            "(tried in order) and normalize the result so the same sample collides across files.",
            color=TEXT_SEC
        )
        self._lbl_compare_desc.setWordWrap(True)
        self._layout.addWidget(self._lbl_compare_title)
        self._layout.addWidget(self._lbl_compare_desc)

        # ── ID extraction block ─────────────────────────────────────────────
        id_block = QtWidgets.QFrame()
        id_block_layout = QtWidgets.QVBoxLayout(id_block)
        id_block_layout.setContentsMargins(0, 2, 0, 2)
        id_block_layout.setSpacing(3)

        # Row 1: delimiter + occurrence + live preview
        simple_row = QtWidgets.QWidget()
        simple_layout = QtWidgets.QHBoxLayout(simple_row)
        simple_layout.setContentsMargins(0, 0, 0, 0)
        simple_layout.setSpacing(8)

        self._lbl_id_delim = make_label("ID delimiter:", color=TEXT_SEC)
        simple_layout.addWidget(self._lbl_id_delim, 0, QtCore.Qt.AlignVCenter)

        self._id_delim_combo = QtWidgets.QComboBox()
        self._id_delim_combo.setEditable(True)
        self._id_delim_combo.setFixedWidth(90)
        self._id_delim_combo.setToolTip(
            "Character (or string) that separates the sample ID from the rest of the header.\n"
            "Common choices: '_' for ONT outputs, '|' for BOLD/GenBank.\n"
            "Multi-character delimiters are also accepted (e.g. '_all.fa')."
        )
        for ch in ["_", "|", ";", " ", ".", "-"]:
            self._id_delim_combo.addItem(ch)
        self._id_delim_combo.setCurrentText("_")
        simple_layout.addWidget(self._id_delim_combo)

        self._lbl_id_occ = make_label("before occurrence:", color=TEXT_SEC)
        simple_layout.addWidget(self._lbl_id_occ, 0, QtCore.Qt.AlignVCenter)

        self._id_occ_combo = QtWidgets.QComboBox()
        self._id_occ_combo.setFixedWidth(62)
        self._id_occ_combo.setToolTip(
            "Which occurrence of the delimiter marks the end of the sample ID.\n"
            "Example with '_': '1st' → everything before the first '_'."
        )
        for suffix in ["1st", "2nd", "3rd", "4th", "5th"]:
            self._id_occ_combo.addItem(suffix)
        simple_layout.addWidget(self._id_occ_combo)

        self._id_delim_anyset_chk = QtWidgets.QCheckBox("any of these")
        self._id_delim_anyset_chk.setToolTip(
            "Treat the delimiter field as a SET of single characters and cut at\n"
            "whichever one appears first, instead of matching it literally.\n"
            "Use it when the same ID is followed by different separators across\n"
            "files, e.g. delimiter '_-' makes both 'BIOUG00045_all.fa' and\n"
            "'BIOUG00045-all.fa' yield 'BIOUG00045'."
        )
        simple_layout.addWidget(self._id_delim_anyset_chk)

        simple_layout.addSpacing(6)
        _arr = make_label("→", color=TEXT_SEC)
        simple_layout.addWidget(_arr, 0, QtCore.Qt.AlignVCenter)

        self._lbl_id_preview_head = make_label("Preview:", color=TEXT_SEC)
        simple_layout.addWidget(self._lbl_id_preview_head, 0, QtCore.Qt.AlignVCenter)

        self._lbl_id_preview = QtWidgets.QLabel("—")
        self._lbl_id_preview.setStyleSheet(
            f"color: {BLUE}; font-family: 'Courier New', monospace; font-weight: 600;"
            f" font-size: 19px; padding-top: 6px;"
        )
        self._lbl_id_preview.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        simple_layout.addWidget(self._lbl_id_preview, 1)
        id_block_layout.addWidget(simple_row)

        # Row 2: sample header preview card (shown after files are loaded).
        self._raw_header_frame = QtWidgets.QFrame()
        self._raw_header_frame.setObjectName("raw_header_card")
        self._raw_header_frame.setStyleSheet(f"""
            QFrame#raw_header_card {{
                background: {GRAY_BG};
                border: 1px solid {GRAY_LINE};
                border-radius: 6px;
                margin-top: 12px;
            }}
        """)
        rh_layout = QtWidgets.QVBoxLayout(self._raw_header_frame)
        rh_layout.setContentsMargins(10, 6, 10, 6)
        rh_layout.setSpacing(2)
        rh_title = QtWidgets.QLabel("Sample header")
        rh_title.setStyleSheet(f"font-size:14px; font-weight:600; color:{TEXT_HINT}; background:transparent;")
        rh_layout.addWidget(rh_title)
        self._lbl_raw_header = QtWidgets.QLabel("")
        self._lbl_raw_header.setStyleSheet(
            f"font-family:'Courier New',Consolas,monospace; font-size:15px;"
            f" color:{TEXT_SEC}; background:transparent;"
        )
        self._lbl_raw_header.setWordWrap(True)
        self._lbl_raw_header.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred
        )
        rh_layout.addWidget(self._lbl_raw_header)
        self._raw_header_frame.hide()
        id_block_layout.addWidget(self._raw_header_frame)

        # Row 3: Advanced (Regex) toggle
        adv_toggle_row = QtWidgets.QWidget()
        adv_toggle_layout = QtWidgets.QHBoxLayout(adv_toggle_row)
        adv_toggle_layout.setContentsMargins(0, 16, 0, 0)
        adv_toggle_layout.setSpacing(0)
        self._advanced_toggle_btn = QtWidgets.QPushButton("▶  Advanced (Regex)")
        self._advanced_toggle_btn.setObjectName("secondary_btn")
        self._advanced_toggle_btn.setCheckable(True)
        self._advanced_toggle_btn.setChecked(False)
        self._advanced_toggle_btn.setFixedHeight(40)
        self._advanced_toggle_btn.setFixedWidth(210)
        adv_toggle_layout.addWidget(self._advanced_toggle_btn)
        adv_toggle_layout.addStretch()
        id_block_layout.addWidget(adv_toggle_row)

        # Row 4: Advanced field (hidden by default) — multi-regex + normalize
        self._advanced_widget = QtWidgets.QWidget()
        adv_layout = QtWidgets.QVBoxLayout(self._advanced_widget)
        adv_layout.setContentsMargins(20, 0, 0, 0)
        adv_layout.setSpacing(8)

        # ── Multi-regex (Option B): one regex per line, tried in order ──
        regex_row = QtWidgets.QWidget()
        regex_layout = QtWidgets.QHBoxLayout(regex_row)
        regex_layout.setContentsMargins(0, 0, 0, 0)
        regex_layout.setSpacing(8)
        self._lbl_regex = make_label("Regex (one per line):", color=TEXT_SEC)
        self._lbl_regex.setAlignment(QtCore.Qt.AlignTop)
        regex_layout.addWidget(self._lbl_regex, 0, QtCore.Qt.AlignTop)
        self._regex_edit = QtWidgets.QPlainTextEdit()
        self._regex_edit.setFixedWidth(600)
        self._regex_edit.setFixedHeight(70)
        self._regex_edit.setPlaceholderText(
            "One regex per line — tried in order until one matches.\n"
            r"e.g. ^(\S+?)_all\.fa,   ^([^|]+)\|"
        )
        self._regex_edit.setToolTip(
            "One regular expression per line. Each FASTA header is tried against\n"
            "every line in order until one matches, so files with different header\n"
            "conventions can share a single config (Option B).\n"
            "If a regex has a capturing group, group 1 is used as the ID;\n"
            "otherwise the full match is used.\n"
            "Leave empty to use the positional mode above."
        )
        regex_layout.addWidget(self._regex_edit)
        self._lbl_regex_status = make_label("", color=TEXT_SEC)
        self._lbl_regex_status.setFixedWidth(140)
        self._lbl_regex_status.setAlignment(QtCore.Qt.AlignTop)
        regex_layout.addWidget(self._lbl_regex_status, 0, QtCore.Qt.AlignTop)
        regex_layout.addStretch()
        adv_layout.addWidget(regex_row)

        # ── Normalization (Option C) ──
        norm_row = QtWidgets.QWidget()
        norm_layout = QtWidgets.QHBoxLayout(norm_row)
        norm_layout.setContentsMargins(0, 0, 0, 0)
        norm_layout.setSpacing(8)
        self._lbl_norm = make_label("Normalize ID:", color=TEXT_SEC)
        norm_layout.addWidget(self._lbl_norm, 0, QtCore.Qt.AlignVCenter)
        self._norm_lower_chk = QtWidgets.QCheckBox("lowercase")
        self._norm_lower_chk.setToolTip("Casefold the extracted ID (BIN-A → bin-a).")
        norm_layout.addWidget(self._norm_lower_chk)
        self._norm_zeros_chk = QtWidgets.QCheckBox("strip leading zeros")
        self._norm_zeros_chk.setToolTip(
            "Drop leading zeros inside numeric runs (BIOUG00045 → BIOUG45)."
        )
        norm_layout.addWidget(self._norm_zeros_chk)
        self._lbl_norm_strip = make_label("strip (regex):", color=TEXT_SEC)
        norm_layout.addWidget(self._lbl_norm_strip, 0, QtCore.Qt.AlignVCenter)
        self._norm_strip_edit = QtWidgets.QLineEdit()
        self._norm_strip_edit.setFixedWidth(180)
        self._norm_strip_edit.setPlaceholderText(r"e.g. _S\d+$  or  -RUN\d+")
        self._norm_strip_edit.setToolTip(
            "Substrings matching this regex are removed from the ID after\n"
            "extraction — useful to drop run/replicate suffixes so the same\n"
            "sample collides across files (Option C). Leave empty to disable."
        )
        norm_layout.addWidget(self._norm_strip_edit)
        norm_layout.addStretch()
        adv_layout.addWidget(norm_row)

        self._advanced_widget.hide()
        id_block_layout.addWidget(self._advanced_widget)

        self._layout.addWidget(id_block)

        # Connect ID extraction signals
        self._id_delim_combo.currentTextChanged.connect(self._update_id_preview)
        self._id_occ_combo.currentIndexChanged.connect(self._update_id_preview)
        self._id_delim_anyset_chk.toggled.connect(self._update_id_preview)
        self._advanced_toggle_btn.toggled.connect(self._on_advanced_toggled)
        self._regex_edit.textChanged.connect(self._on_regex_changed)
        self._norm_lower_chk.toggled.connect(self._update_id_preview)
        self._norm_zeros_chk.toggled.connect(self._update_id_preview)
        self._norm_strip_edit.textChanged.connect(self._update_id_preview)

        self._mode_box = QtWidgets.QGroupBox("Comparison mode")
        self._mode_box.setStyleSheet("QGroupBox { font-weight:600; color:#1A1A2E; }")
        mb_layout = QtWidgets.QVBoxLayout(self._mode_box)
        self._radio_sets = QtWidgets.QRadioButton(
            "Compare sets with each other — identical /IUPAC compatible /different /unique"
        )
        self._radio_sets_src = "Compare sets with each other — identical /IUPAC compatible /different /unique"
        self._radio_sets.setChecked(True)
        self._radio_ref = QtWidgets.QRadioButton(
            "Compare against reference — a file acts as a reference"
        )
        self._radio_ref_src = "Compare against reference — a file acts as a reference"
        mb_layout.addWidget(self._radio_sets)
        mb_layout.addWidget(self._radio_ref)
        self._layout.addWidget(self._mode_box)

        self._ref_widget = QtWidgets.QWidget()
        ref_layout = QtWidgets.QHBoxLayout(self._ref_widget)
        ref_layout.setContentsMargins(0, 0, 0, 0)
        self._lbl_ref_file = make_label("Reference file:", color=TEXT_SEC)
        ref_layout.addWidget(self._lbl_ref_file)
        self._ref_combo = QtWidgets.QComboBox()
        self._ref_combo.setMinimumWidth(320)
        ref_layout.addWidget(self._ref_combo)
        ref_layout.addStretch()
        self._ref_widget.hide()
        self._layout.addWidget(self._ref_widget)

        # Connect radio to show/hide selector
        self._radio_ref.toggled.connect(self._on_mode_toggled)

        # ── Output folder ──
        outdir_row = QtWidgets.QWidget()
        outdir_layout = QtWidgets.QHBoxLayout(outdir_row)
        outdir_layout.setContentsMargins(0, 0, 0, 0)
        self._lbl_outdir = make_label("Output folder:", color=TEXT_SEC)
        outdir_layout.addWidget(self._lbl_outdir)
        self._outdir_edit = QtWidgets.QLineEdit()
        self._outdir_edit.setPlaceholderText("It will be automatically generated in …/output/ont-barcoder_<timestamp>_comp")
        self._outdir_edit.setReadOnly(True)
        outdir_layout.addWidget(self._outdir_edit, 1)
        self._outdir_btn = QtWidgets.QPushButton("Change…")
        self._outdir_btn.setObjectName("secondary_btn")
        self._outdir_btn.setFixedWidth(120)
        self._outdir_btn.clicked.connect(self._pick_outdir)
        outdir_layout.addWidget(self._outdir_btn)
        self._layout.addWidget(outdir_row)
        outdir_row.hide()          # The folder is chosen in the dialog at startup
        self._custom_outdir = ""   # empty = use automatic default

        # ── File area ──
        self._drop = MultiDropZone()
        self._drop.filesDropped.connect(self._on_files)
        self._layout.addWidget(self._drop)

        # ── Progress bar ──
        self._comp_bar = QtWidgets.QProgressBar()
        self._comp_bar.setRange(0, 100)
        self._comp_bar.setValue(0)
        self._comp_bar.hide()
        self._layout.addWidget(self._comp_bar)

        # ── Results area ──
        self._result_lbl = make_label("", color=TEXT_SEC)
        self._result_lbl.setWordWrap(True)
        self._layout.addWidget(self._result_lbl)

        self._layout.addStretch()
        self._results_win = None   # reference to the last results window

        # ── Footer ──
        footer = QtWidgets.QWidget()
        footer.setObjectName("compare_footer")
        footer.setStyleSheet(f"""
            QWidget#compare_footer {{
                background: {GRAY_CARD};
                border-top: 1px solid {GRAY_LINE};
            }}
        """)
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(20, 10, 20, 10)

        self._clear_btn = QtWidgets.QPushButton("Clear")
        self._clear_btn.setObjectName("danger_btn")
        self._clear_btn.setFixedHeight(44)
        self._clear_btn.setFixedWidth(140)

        self._clear_btn.clicked.connect(self._reset)
        fl.addWidget(self._clear_btn)

        self._view_btn = QtWidgets.QPushButton("Show table  ↗")
        self._view_btn.setObjectName("secondary_btn")
        self._view_btn.setFixedHeight(44)
        self._view_btn.hide()
        self._view_btn.clicked.connect(self._open_results_win)
        fl.addWidget(self._view_btn)

        self._open_folder_btn = QtWidgets.QPushButton("Open folder  📂")
        self._open_folder_btn.setObjectName("secondary_btn")
        self._open_folder_btn.setFixedHeight(44)
        self._open_folder_btn.hide()
        self._open_folder_btn.clicked.connect(self._open_output_folder)
        fl.addWidget(self._open_folder_btn)

        self._open_excel_btn = QtWidgets.QPushButton("Open Excel  📊")
        self._open_excel_btn.setObjectName("secondary_btn")
        self._open_excel_btn.setFixedHeight(44)
        self._open_excel_btn.hide()
        self._open_excel_btn.clicked.connect(self._open_summary_excel)
        fl.addWidget(self._open_excel_btn)

        fl.addStretch()

        self._compare_btn = QtWidgets.QPushButton("Start comparison  →")
        self._compare_btn.setObjectName("primary_btn")
        self._compare_btn.setFixedHeight(44)
        self._compare_btn.setFixedWidth(300)
        self._compare_btn.setEnabled(False)
        self._compare_btn.clicked.connect(self._emit_compare)
        self._compare_btn.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )
        fl.addWidget(self._compare_btn)
        outer_layout.addWidget(footer)

        self._compare_btn_ref = self._compare_btn

    def retranslateUi(self):
        ctx = "ComparePanel"
        self._lbl_compare_title.setText(_tr(ctx, "Compare barcode sets"))
        self._lbl_compare_desc.setText(_tr(ctx,
            "Compare multiFASTA files from different runs. "
            "The sample ID is extracted from the FASTA header based on a delimiter and occurrence. "
            "Use Advanced when files follow different header conventions: list several regex "
            "(tried in order) and normalize the result so the same sample collides across files."))
        self._lbl_id_delim.setText(_tr(ctx, "ID delimiter:"))
        self._lbl_id_occ.setText(_tr(ctx, "before occurrence:"))
        self._id_delim_anyset_chk.setText(_tr(ctx, "any of these"))
        self._lbl_id_preview_head.setText(_tr(ctx, "Preview:"))
        arrow = "▼  " if self._advanced_toggle_btn.isChecked() else "▶  "
        self._advanced_toggle_btn.setText(arrow + _tr(ctx, "Advanced (Regex)"))
        self._lbl_regex.setText(_tr(ctx, "Regex (one per line):"))
        self._lbl_norm.setText(_tr(ctx, "Normalize ID:"))
        self._norm_lower_chk.setText(_tr(ctx, "lowercase"))
        self._norm_zeros_chk.setText(_tr(ctx, "strip leading zeros"))
        self._lbl_norm_strip.setText(_tr(ctx, "strip (regex):"))
        self._mode_box.setTitle(_tr(ctx, "Comparison mode"))
        self._radio_sets.setText(_tr(ctx, self._radio_sets_src))
        self._radio_ref.setText(_tr(ctx, self._radio_ref_src))
        self._lbl_ref_file.setText(_tr(ctx, "Reference file:"))
        self._lbl_outdir.setText(_tr(ctx, "Output folder:"))
        self._outdir_edit.setPlaceholderText(_tr(ctx, "It will be automatically generated in …/output/ont-barcoder_<timestamp>_comp"))
        self._outdir_btn.setText(_tr(ctx, "Change…"))
        self._clear_btn.setText(_tr(ctx, "Clear"))
        self._view_btn.setText(_tr(ctx, "Show table  ↗"))
        self._open_folder_btn.setText(_tr(ctx, "Open folder  📂"))
        self._open_excel_btn.setText(_tr(ctx, "Open Excel  📊"))
        self._compare_btn.setText(_tr(ctx, "Start comparison  →"))
        self._drop.retranslateUi()

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def _on_mode_toggled(self, ref_active):
        self._ref_widget.setVisible(ref_active)

    def _on_advanced_toggled(self, checked: bool):
        self._advanced_widget.setVisible(checked)
        ctx = "ComparePanel"
        arrow = "▼  " if checked else "▶  "
        self._advanced_toggle_btn.setText(arrow + _tr(ctx, "Advanced (Regex)"))
        self._update_id_preview()

    def _on_regex_changed(self, *_):
        lines = [ln.strip() for ln in self._regex_edit.toPlainText().splitlines()
                 if ln.strip()]
        if not lines:
            self._lbl_regex_status.setText("")
            self._lbl_regex_status.setStyleSheet("")
        else:
            bad = None
            for ln in lines:
                try:
                    re.compile(ln)
                except re.error as e:
                    bad = (ln, e)
                    break
            if bad is None:
                self._lbl_regex_status.setText(f"✓ {len(lines)} valid")
                self._lbl_regex_status.setStyleSheet("color: #16A34A;")
            else:
                self._lbl_regex_status.setText(f"✗ {bad[1].msg}")
                self._lbl_regex_status.setStyleSheet("color: #DC2626;")
        self._update_id_preview()

    def _get_extract_cfg(self) -> dict:
        """
        Build the extraction config from the current UI state.

        The positional delimiter is always kept as a fallback candidate; any
        regex lines (Option B) are tried first, then the positional pattern.
        Normalization (Option C) is applied to whichever ID is extracted.
        """
        delim = self._id_delim_combo.currentText()
        occ = self._id_occ_combo.currentIndex() + 1
        if delim:
            kind = "posany" if self._id_delim_anyset_chk.isChecked() else "pos"
            id_patterns = [f"{kind}:{delim}:{occ}"]
        else:
            id_patterns = []

        regex_patterns: list = []
        normalize: dict = {}
        if self._advanced_toggle_btn.isChecked():
            regex_patterns = [ln.strip()
                              for ln in self._regex_edit.toPlainText().splitlines()
                              if ln.strip()]
            normalize = {
                "lowercase":   self._norm_lower_chk.isChecked(),
                "strip_zeros": self._norm_zeros_chk.isChecked(),
                "strip_regex": self._norm_strip_edit.text().strip(),
            }
        return {
            "id_patterns":    id_patterns,
            "regex_patterns": regex_patterns,
            "normalize":      normalize,
        }

    def _update_id_preview(self, *_):
        """Update the live ID preview from the first header of the first loaded file."""
        files = getattr(self._drop, 'files', [])
        if not files:
            self._lbl_id_preview.setText("—")
            self._lbl_raw_header.setText("")
            self._raw_header_frame.hide()
            return
        first_header = None
        try:
            with open(files[0], encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped.startswith(">"):
                        first_header = stripped
                        break
        except Exception:
            pass
        if not first_header:
            self._lbl_id_preview.setText("—")
            self._lbl_raw_header.setText("")
            self._raw_header_frame.hide()
            return
        raw_display = first_header[:100] + ("…" if len(first_header) > 100 else "")
        self._lbl_raw_header.setText(raw_display)
        self._raw_header_frame.show()
        cfg = self._get_extract_cfg()
        try:
            sid, *_ = _parse_fasta_header(first_header, cfg)
            self._lbl_id_preview.setText(sid if sid else "—")
        except Exception:
            self._lbl_id_preview.setText("—")

    def _emit_compare(self):
        mode = "ref" if self._radio_ref.isChecked() else "sets"
        ref_path = (self._ref_combo.currentData() or "") if mode == "ref" else ""
        extract_cfg = self._get_extract_cfg()
        self.compareRequested.emit(
            self._drop.files, mode, ref_path, self._custom_outdir, extract_cfg
        )

    def _pick_outdir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, _tr("ComparePanel", "Select output folder"), self._custom_outdir or ""
        )
        if path:
            self._custom_outdir = path
            self._outdir_edit.setText(path)

    def _on_files(self, paths):
        # Update reference combo with unique tags
        self._ref_combo.clear()
        labels = _make_unique_labels(paths) if paths else []
        for lbl, p in zip(labels, paths):
            self._ref_combo.addItem(lbl, p)  # userData = full path

        enabled = len(paths) >= 2
        self._compare_btn_ref.setEnabled(enabled)
        if enabled:
            self._compare_btn_ref.setStyleSheet(
                f"QPushButton {{ background-color: {BLUE}; color: white; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
                f"QPushButton:hover {{ background-color: #0C4A82; }}"
            )
        else:
            self._compare_btn_ref.setStyleSheet(
                f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
            )
        self._update_id_preview()

    @QtCore.pyqtSlot(int)
    def update_progress(self, n):
        if self._comp_bar.maximum() == 0:
            self._comp_bar.setRange(0, 100)
        self._comp_bar.show()
        self._comp_bar.setValue(n)

    @QtCore.pyqtSlot(list, list)
    def show_results(self, rows, headers):
        self._comp_bar.hide()
        self._comp_bar.setValue(0)

        n_identical = sum(1 for r in rows if r.get("State") == "Identical")
        n_compat    = sum(1 for r in rows if r.get("State") == "Compatible (IUPAC)")
        n_diff      = sum(1 for r in rows if r.get("State") == "Different")
        n_only      = sum(1 for r in rows if r.get("State", "").startswith("Unique"))
        n_only_ref  = sum(1 for r in rows if r.get("State") == "Only in reference")
        n_no_ref    = sum(1 for r in rows if r.get("State") == "No reference")

        ctx = "ComparePanel"
        summary = (
            f"<b>{_tr(ctx, 'Comparison completed.')}</b> {_tr(ctx, 'Total IDs')}: {len(rows)} &nbsp;|&nbsp; "
            f"🟢 {_tr(ctx, 'Identical')}: {n_identical}"
        )
        if n_compat:
            summary += f" &nbsp;|&nbsp; 🔵 {_tr(ctx, 'Compatibles (IUPAC)')}: {n_compat}"
        if n_diff:
            summary += f" &nbsp;|&nbsp; 🔴 {_tr(ctx, 'Different')}: {n_diff}"
        if n_only:
            summary += f" &nbsp;|&nbsp; ⚪ {_tr(ctx, 'Unique')}: {n_only}"
        if n_only_ref:
            summary += f" &nbsp;|&nbsp; 📌 {_tr(ctx, 'Only in reference')}: {n_only_ref}"
        if n_no_ref:
            summary += f" &nbsp;|&nbsp; ❓ {_tr(ctx, 'No reference')}: {n_no_ref}"

        outdir_shown = self._outdir_edit.text()
        if outdir_shown:
            summary += f"<br>📁 {_tr(ctx, 'Output in')}: <i>{outdir_shown}</i>"
        self._result_lbl.setText(summary)

        self._compare_btn_ref.setEnabled(True)
        self._open_folder_btn.show()
        self._open_excel_btn.show()

        # Open (or refresh) the results window
        if self._results_win is not None:
            try:
                self._results_win.close()
            except Exception:
                pass
        self._results_win = _CompareResultsWindow(rows, headers, outdir_shown, self)
        self._results_win.show()
        self._view_btn.show()

    def _open_results_win(self):
        if self._results_win is not None:
            self._results_win.show()
            self._results_win.raise_()

    def _open_output_folder(self):
        path = self._outdir_edit.text()
        if path and os.path.isdir(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _open_summary_excel(self):
        path = self._outdir_edit.text()
        if path:
            xlsx = os.path.join(path, "summary.xlsx")
            if os.path.isfile(xlsx):
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(xlsx))

    def _reset(self):
        """Leaves the module exactly as it was when you opened it for the first time."""
        # Close results window if open
        if self._results_win is not None:
            try:
                self._results_win.close()
            except Exception:
                pass
            self._results_win = None

        # Clean files from the drop zone
        self._drop.clear()

        # Hide and reset progress bar
        self._comp_bar.hide()
        self._comp_bar.setValue(0)
        self._comp_bar.setRange(0, 100)

        # Clear result label
        self._result_lbl.setText("")

        # Reset output folder
        self._outdir_edit.setText("")
        self._custom_outdir = ""

        # Reset buttons
        self._view_btn.hide()
        self._open_folder_btn.hide()
        self._open_excel_btn.hide()
        self._compare_btn_ref.setEnabled(False)
        self._compare_btn_ref.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )

        # Reset ID extraction controls
        self._id_delim_combo.setCurrentText("_")
        self._id_occ_combo.setCurrentIndex(0)
        self._id_delim_anyset_chk.setChecked(False)
        self._advanced_toggle_btn.setChecked(False)
        self._advanced_widget.hide()
        self._regex_edit.setPlainText("")
        self._lbl_regex_status.setText("")
        self._norm_lower_chk.setChecked(False)
        self._norm_zeros_chk.setChecked(False)
        self._norm_strip_edit.setText("")
        self._lbl_id_preview.setText("—")
        self._lbl_raw_header.setText("")
        self._raw_header_frame.hide()

        # Reset radios to initial state
        self._radio_sets.setChecked(True)
        self._ref_widget.hide()
        self._ref_combo.clear()


# ---------------------------------------------------------------------------
# IUPAC constants
# ---------------------------------------------------------------------------
_IUPAC: Dict[str, set] = {
    'A': {'A'}, 'C': {'C'}, 'G': {'G'}, 'T': {'T'},
    'R': {'A', 'G'}, 'Y': {'C', 'T'}, 'S': {'G', 'C'},
    'W': {'A', 'T'}, 'K': {'G', 'T'}, 'M': {'A', 'C'},
    'B': {'C', 'G', 'T'}, 'D': {'A', 'G', 'T'},
    'H': {'A', 'C', 'T'}, 'V': {'A', 'C', 'G'},
    'N': {'A', 'C', 'G', 'T'},
}

# IUPAC equalities list for edlib (same as original code)
_EDLIB_AMBIGUITY = [
    ("R", "A"), ("R", "G"), ("M", "A"), ("M", "C"),
    ("S", "C"), ("S", "G"), ("Y", "C"), ("Y", "T"),
    ("K", "G"), ("K", "T"), ("W", "A"), ("W", "T"),
    ("V", "A"), ("V", "C"), ("V", "G"),
    ("H", "A"), ("H", "C"), ("H", "T"),
    ("D", "A"), ("D", "G"), ("D", "T"),
    ("B", "C"), ("B", "G"), ("B", "T"),
    ("N", "A"), ("N", "G"), ("N", "C"), ("N", "T"),
]

_REVCOMP_TABLE = str.maketrans("ACGTRYSWKMBDHVNacgtryswkmbdhvn",
                                "TGCAYRSWMKVHDBNtgcayrswmkvhdbn")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _revcomp(seq: str) -> str:
    """Reverse complement respecting IUPAC ambiguities."""
    return seq.translate(_REVCOMP_TABLE)[::-1]


def _split_extract_cfg(cfg) -> Tuple[List[str], List[str], dict]:
    """
    Normalize an extraction config into (id_patterns, regex_patterns, normalize).

    ``cfg`` may be:
      • None  → legacy default (positional substring "_all.fa", no regex, no normalize)
      • a dict with keys "id_patterns" (list), "regex_patterns" (list),
        "normalize" (dict)
      • a plain str → treated as a single positional/substring pattern
    """
    if cfg is None:
        return ["_all.fa"], [], {}
    if isinstance(cfg, str):
        return ([cfg] if cfg else []), [], {}
    return (
        list(cfg.get("id_patterns") or []),
        list(cfg.get("regex_patterns") or []),
        dict(cfg.get("normalize") or {}),
    )


def _extract_sample_id(raw: str, id_patterns: List[str],
                       regex_patterns: List[str]) -> str:
    """
    Option B — try each candidate pattern *in order* until one yields an ID.

    Order: regex candidates first (group 1 if present, else full match), then
    positional/substring candidates. A pattern that does not match is skipped so
    the next candidate gets a chance; only when *none* match do we fall back to
    the text before the first ';'. This lets a single config cover files whose
    headers follow different conventions.

    Positional pattern forms:
      • "pos:DELIM:N"    → text before the Nth occurrence of the literal DELIM
                           (DELIM may be multi-character).
      • "posany:CHARS:N" → text before the Nth occurrence of *any* of the
                           single characters in CHARS. Useful when the same ID is
                           followed by different separators across files, e.g.
                           'BIOUG00045_all.fa' and 'BIOUG00045-all.fa' both yield
                           'BIOUG00045' with CHARS '_-'.
    """
    import re as _re
    for rx in regex_patterns:
        if not rx:
            continue
        try:
            m = _re.search(rx, raw)
        except _re.error:
            continue
        if m:
            # Prefer group 1, but only if it actually participated in the match
            # (an optional group can leave group(1) == None while a later group
            # matched). Skip empty results so the next candidate gets a chance.
            cand = m.group(1) if (m.lastindex and m.group(1) is not None) else m.group(0)
            if cand:
                return cand

    for pat in id_patterns:
        if not pat:
            continue
        if pat.startswith("posany:"):
            # Split on any of the given single characters; cut before the Nth one.
            # Split on the *last* ':' so a delimiter set containing ':' still works
            # (the occurrence index is always the trailing integer).
            try:
                chars, _, n_str = pat[len("posany:"):].rpartition(":")
                n = int(n_str)
                if chars:
                    positions = [i for i, c in enumerate(raw) if c in chars]
                    if len(positions) >= n:
                        return raw[:positions[n - 1]]
            except Exception:
                continue
        elif pat.startswith("pos:"):
            # "pos:DELIM:N" → join the first N parts split by literal DELIM.
            # E.g. "pos:_:2" on "A_B_C" → "A_B". Split on the *last* ':' so DELIM
            # may itself contain ':' (the occurrence index is the trailing integer).
            try:
                sep, _, n_str = pat[len("pos:"):].rpartition(":")
                n = int(n_str)
                if sep and sep in raw:
                    return sep.join(raw.split(sep)[:n])
            except Exception:
                continue
        elif pat in raw:
            return raw.split(pat)[0]

    return raw.split(";")[0]


def _normalize_id(sample_id: str, normalize: dict) -> str:
    """
    Option C — normalize an extracted ID so equivalent IDs collide across files.

    Supported keys in ``normalize``:
      • "strip_regex" (str): substrings matching this regex are removed
        (e.g. run suffixes like '_S\\d+' or '-RUN\\d+').
      • "lowercase" (bool): casefold the ID.
      • "strip_zeros" (bool): drop leading zeros inside each numeric run
        (e.g. 'BIOUG00045' → 'BIOUG45', leaving '100' untouched).
    Applied in that order. Returns the stripped result.
    """
    if not normalize:
        return sample_id
    import re as _re
    sid = sample_id
    strip_rx = normalize.get("strip_regex") or ""
    if strip_rx:
        try:
            sid = _re.sub(strip_rx, "", sid)
        except _re.error:
            pass
    if normalize.get("lowercase"):
        sid = sid.lower()
    if normalize.get("strip_zeros"):
        sid = _re.sub(r"(?<!\d)0+(\d)", r"\1", sid)
    return sid.strip()


def _parse_fasta_header(header_line: str, cfg=None) -> Tuple[str, int, int, int, int]:
    """
    Extract (sample_id, length, coverage, ambs, gaps) from a FASTA header.

    ``cfg`` is an extraction config (see _split_extract_cfg). The ID is resolved
    by trying every candidate pattern in order (Option B) and then normalized
    (Option C). For backward compatibility ``cfg`` may also be a plain pattern
    string or None.
    """
    raw = header_line.strip().lstrip(">")
    id_patterns, regex_patterns, normalize = _split_extract_cfg(cfg)
    sample_id = _extract_sample_id(raw, id_patterns, regex_patterns)
    sample_id = _normalize_id(sample_id, normalize)

    parts = raw.split(";")
    try:
        length = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        length = 0
    try:
        coverage = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        coverage = 0
    ambs = gaps = 0
    for p in parts[3:]:
        if p.startswith("ambs="):
            try:
                ambs = int(p.split("=")[1])
            except Exception:
                pass
        elif p.startswith("estgaps="):
            try:
                gaps = int(p.split("=")[1])
            except Exception:
                pass
    return sample_id, length, coverage, ambs, gaps


def _make_unique_labels(file_list: List[str]) -> List[str]:
    """
    Unique labels for each route. Use only the file name when not
    there is a collision; adds the parent directory when there is one.
    """
    raw = [os.path.basename(f) for f in file_list]
    raw_counts = Counter(raw)

    step1 = []
    for f, bn in zip(file_list, raw):
        if raw_counts[bn] > 1:
            parent = os.path.basename(os.path.dirname(os.path.abspath(f)))
            step1.append(f"{parent}/{bn}" if parent else bn)
        else:
            step1.append(bn)

    step1_counts = Counter(step1)
    if max(step1_counts.values(), default=1) == 1:
        return step1

    seen: Dict[str, int] = {}
    final = []
    for lbl in step1:
        if step1_counts[lbl] > 1:
            seen[lbl] = seen.get(lbl, 0) + 1
            final.append(f"{lbl} ({seen[lbl]})")
        else:
            final.append(lbl)
    return final


def _parse_fasta_file(path: str, cfg=None) -> Dict[str, Tuple[str, int, int, int, int]]:
    """
    Reads a FASTA file and returns dict[sample_id] = (seq, length, cov, ambs, gaps).
    ``cfg`` is the extraction config (see _split_extract_cfg / _parse_fasta_header).
    If there are duplicates in the same file, keep the one with the best quality
    (less both → fewer gaps → greater coverage).
    """
    result: Dict[str, Tuple[str, int, int, int, int]] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception:
        return result

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith(">"):
            sid, length, cov, ambs, gaps = _parse_fasta_header(line, cfg)
            seq = ""
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith(">"):
                seq += lines[j].strip().upper()
                j += 1
            cand = (seq, length, cov, ambs, gaps)
            if sid not in result:
                result[sid] = cand
            else:
                old = result[sid]
                if (ambs, gaps, -cov) < (old[3], old[4], -old[2]):
                    result[sid] = cand
            i = j
        else:
            i += 1
    return result


# ---------------------------------------------------------------------------
# Sequence comparison (core)
# ---------------------------------------------------------------------------

def _align_pair(seq1: str, seq2: str):
    """
    Compare seq1 vs seq2 using edlib in NW (global alignment) mode.
    Retorna (d_noamb, d_amb):
      d_noamb – unambiguous edit distance
      d_amb – edit distance with IUPAC ambiguities
    NW ensures that the difference in length is reflected in the distance,
    which is correct for both Coding (same length) and non-Coding (variable length).
    """
    d_noamb = edlib.align(seq1, seq2, mode='NW', task='path')['editDistance']
    d_amb   = edlib.align(seq1, seq2, mode='NW', task='path',
                          additionalEqualities=_EDLIB_AMBIGUITY)['editDistance']
    return d_noamb, d_amb


def _compare_sequences(seq1: str, seq2: str) -> Tuple[str, int, int, bool]:
    """
    Compares two sequences and returns (state, d_amb, d_noamb, rc_used).

    state:
      'identical'  – identical character for character (d_noamb=0, d_amb=0)
      'compatible' – IUPAC-compatible (d_amb=0, d_noamb>0 = ambiguity positions)
      'different'  – incompatible (d_amb>0)
    d_amb:   edit distance with IUPAC equivalences (0 if identical/compatible)
    d_noamb: unambiguous edit distance (for compatible = IUPAC position count)
    rc_used: True if the match was found with the reverse complement
    """
    d_noamb, d_amb = _align_pair(seq1, seq2)
    if d_amb == 0:
        estado = 'identical' if d_noamb == 0 else 'compatible'
        return estado, 0, d_noamb, False
    rc1 = _revcomp(seq1)
    rc_noamb, rc_amb = _align_pair(rc1, seq2)
    if rc_amb == 0:
        estado = 'identical' if rc_noamb == 0 else 'compatible'
        return estado, 0, rc_noamb, True
    use_rc = rc_amb < d_amb
    best_amb   = rc_amb   if use_rc else d_amb
    best_noamb = rc_noamb if use_rc else d_noamb
    return 'different', best_amb, best_noamb, use_rc


def _iupac_compatible_simple(seq1: str, seq2: str) -> bool:
    """IUPAC position-to-position compatibility (same length only)."""
    if len(seq1) != len(seq2):
        return False
    for b1, b2 in zip(seq1, seq2):
        if not _IUPAC.get(b1, {b1}).intersection(_IUPAC.get(b2, {b2})):
            return False
    return True


def _hamming(seq1: str, seq2: str) -> Optional[int]:
    """Hamming distance; None if different lengths."""
    if len(seq1) != len(seq2):
        return None
    return sum(a != b for a, b in zip(seq1, seq2))


# ---------------------------------------------------------------------------
# Selection of the best barcode
# ---------------------------------------------------------------------------

# Entry: list of (basename, seq, length, cov, ambs, gaps)
_SeqEntry = Tuple[str, str, int, int, int, int]


def _best_barcode(entries: List[_SeqEntry]) -> Optional[_SeqEntry]:
    """
    Choose the entry with: less both → less gaps → greater coverage.
    Returns None if there is an exact tie between two or more entries.
    """
    if not entries:
        return None
    best_key = min((e[4], e[5], -e[3]) for e in entries)
    best = [e for e in entries if (e[4], e[5], -e[3]) == best_key]
    return best[0] if len(best) == 1 else None


# ---------------------------------------------------------------------------
# Writing outputs
# ---------------------------------------------------------------------------

_COLOR_MAP_HEX = {
    "Identical":           "#EAF3DE",
    "Compatible (IUPAC)": "#E6F1FB",
    "Different":          "#FCEBEB",
    "Only in reference": "#FAEEDA",
    "No reference":     "#F5F5F3",
}


def _write_fasta(path: str, entries: List[Tuple[str, str]]) -> None:
    """Write pairs (header, seq) to a FASTA file."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for hdr, seq in entries:
                if seq:
                    f.write(f">{hdr}\n{seq}\n")
    except Exception:
        pass


def _write_outputs(
    rows: List[dict],
    headers: List[str],
    outdir: str,
    all_bns: List[str],
    ref_bn: Optional[str],
    seqs_store: Dict[str, Dict[str, Tuple[str, int, int, int, int]]],
) -> None:
    """
    Generate all output files:
      • summary.xlsx (with colors)
      • best_barcodes.fa
      • identical.fa
      • compatible_iupac.fa
      • different.fa
      • only_in_reference.fa
      • without_reference.fa
      • unique_<basename>.fa (one per file)
    """
    os.makedirs(outdir, exist_ok=True)

    # ── XLSX ────────────────────────────────────────────────────────────────

    xlsx_path = os.path.join(outdir, "summary.xlsx")
    try:
        wb = xlsxwriter.Workbook(xlsx_path)
        ws = wb.add_worksheet("Comparison")

        fmt_hdr = wb.add_format({
            "bold": True, "font_color": "#FFFFFF", "bg_color": "#185FA5",
            "border": 1, "border_color": "#2E6FAA",
            "align": "center", "valign": "vcenter", "text_wrap": True,
        })
        fmt_by_estado = {}
        for estado, bg in _COLOR_MAP_HEX.items():
            fmt_by_estado[estado] = wb.add_format({
                "bg_color": bg, "border": 1,
                "border_color": "#D0CEC8", "valign": "vcenter",
            })
        fmt_unico = wb.add_format({
            "bg_color": "#EBEBEB", "border": 1,
            "border_color": "#D0CEC8", "valign": "vcenter",
        })
        fmt_default = wb.add_format({
            "border": 1, "border_color": "#D0CEC8", "valign": "vcenter",
        })
        fmt_id_bold = {
            estado: wb.add_format({
                "bold": True, "bg_color": bg, "border": 1,
                "border_color": "#D0CEC8", "valign": "vcenter",
            }) for estado, bg in _COLOR_MAP_HEX.items()
        }
        fmt_id_unico = wb.add_format({
            "bold": True, "bg_color": "#EBEBEB", "border": 1,
            "border_color": "#D0CEC8", "valign": "vcenter",
        })

        ws.set_row(0, 28)
        id_col = headers.index("ID") if "ID" in headers else -1
        for j, h in enumerate(headers):
            ws.write(0, j, h, fmt_hdr)

        for i, row in enumerate(rows, 1):
            ws.set_row(i, 18)
            estado = row.get("State", "")
            if estado in fmt_by_estado:
                row_fmt    = fmt_by_estado[estado]
                row_id_fmt = fmt_id_bold.get(estado, row_fmt)
            elif estado.startswith("Unique"):
                row_fmt    = fmt_unico
                row_id_fmt = fmt_id_unico
            else:
                row_fmt    = fmt_default
                row_id_fmt = fmt_default

            for j, h in enumerate(headers):
                val = row.get(h, "")
                fmt = row_id_fmt if j == id_col else row_fmt
                ws.write(i, j, val, fmt)

        for j, h in enumerate(headers):
            col_vals = [str(row.get(h, "") or "") for row in rows]
            max_len  = max((len(v) for v in col_vals), default=4)
            max_len  = max(max_len, len(h))
            ws.set_column(j, j, min(max_len + 2, 52))

        ws.freeze_panes(1, 1)
        wb.close()
    except Exception as exc:
        print(f"[XLSX] Writing error: {exc}")

    # ── Helpers ─────────────────────────────────────────────────────────────
    def get_seq(bn: str, sid: str):
        entry = seqs_store.get(bn, {}).get(sid)
        return (entry[0], entry[3], entry[2]) if entry else None

    # ── Sort rows and accumulate FASTA entries ───────────────────────────
    best_entries:      List[Tuple[str, str]] = []
    identical_entries: List[Tuple[str, str]] = []
    compat_entries:    List[Tuple[str, str]] = []
    diff_entries:      List[Tuple[str, str]] = []
    only_ref_entries:  List[Tuple[str, str]] = []
    no_ref_entries:    List[Tuple[str, str]] = []
    unique_entries:    Dict[str, List[Tuple[str, str]]] = {bn: [] for bn in all_bns}

    for row in rows:
        sid    = row["ID"]
        estado = row.get("State", "")
        best_bn = row.get("Best_run", "")

        # Best overall barcode
        candidates: List[_SeqEntry] = []
        for bn in all_bns:
            entry = seqs_store.get(bn, {}).get(sid)
            if entry:
                seq, length, cov, ambs, gaps = entry
                candidates.append((bn, seq, length, cov, ambs, gaps))
        if candidates:
            if estado == "Different" and best_bn and best_bn != "Tie":
                chosen = next((c for c in candidates if c[0] == best_bn),
                              candidates[0])
            else:
                chosen = min(candidates, key=lambda c: (c[4], c[5], -c[3]))
            best_entries.append((f"{sid};best_from={chosen[0]}", chosen[1]))

        # Files by category
        if estado == "Identical":
            for bn in all_bns:
                r = get_seq(bn, sid)
                if r:
                    identical_entries.append((f"{sid};src={bn}", r[0]))
                    break
        elif estado == "Compatible (IUPAC)":
            for bn in all_bns:
                r = get_seq(bn, sid)
                if r:
                    compat_entries.append((f"{sid};src={bn}", r[0]))
                    break
        elif estado == "Different":
            for bn in all_bns:
                r = get_seq(bn, sid)
                if r:
                    diff_entries.append((f"{sid};src={bn}", r[0]))
        elif estado == "Only in reference":
            if ref_bn:
                r = get_seq(ref_bn, sid)
                if r:
                    only_ref_entries.append((sid, r[0]))
        elif estado == "No reference":
            for bn in all_bns:
                if bn == ref_bn:
                    continue
                r = get_seq(bn, sid)
                if r:
                    no_ref_entries.append((f"{sid};src={bn}", r[0]))
                    break
        elif estado.startswith("Unique in "):
            src_bn = estado.replace("Unique in ", "")
            r = get_seq(src_bn, sid)
            if r and src_bn in unique_entries:
                unique_entries[src_bn].append((sid, r[0]))

    # ── Write FASTAs ─────────────────────────── ───────────────────────────
    _write_fasta(os.path.join(outdir, "best_barcodes.fa"), best_entries)
    if identical_entries:
        _write_fasta(os.path.join(outdir, "identical.fa"), identical_entries)
    if compat_entries:
        _write_fasta(os.path.join(outdir, "compatible_iupac.fa"), compat_entries)
    if diff_entries:
        _write_fasta(os.path.join(outdir, "different.fa"), diff_entries)
    if only_ref_entries:
        _write_fasta(os.path.join(outdir, "only_in_reference.fa"), only_ref_entries)
    if no_ref_entries:
        _write_fasta(os.path.join(outdir, "without_reference.fa"), no_ref_entries)
    for bn, entries in unique_entries.items():
        if entries:
            safe = bn.replace("/", "_").replace("\\", "_")
            _write_fasta(os.path.join(outdir, f"unique_{safe}.fa"), entries)


# ---------------------------------------------------------------------------
# Worker: everyone vs everyone
# ---------------------------------------------------------------------------

class _CompareWorker(QtCore.QThread):
    """
    Compare N files with each other (all possible pairs).
    For each ID determines the global state (worst among all peers)
    and details the result by pair in the Note column.
    """
    notifyProgress = QtCore.pyqtSignal(int)
    taskFinished   = QtCore.pyqtSignal(list, list)   # rows, headers

    def __init__(self, file_list: List[str], outdir: str, extract_cfg=None,
                 parent=None):
        super().__init__(parent)
        self.file_list   = file_list
        self.outdir      = outdir
        self.extract_cfg = extract_cfg

    def run(self):
        file_list = self.file_list
        basenames = _make_unique_labels(file_list)

        # Parse files
        seqs: Dict[str, Dict[str, Tuple]] = {}
        for fname, bn in zip(file_list, basenames):
            seqs[bn] = _parse_fasta_file(fname, self.extract_cfg)

        pairs = list(itertools.combinations(basenames, 2))
        all_ids = sorted({sid for d in seqs.values() for sid in d})
        total   = len(all_ids)

        # Build rows
        rows: List[dict] = []
        priority = {"different": 0, "compatible": 1, "identical": 2}

        for prog_i, sid in enumerate(all_ids):
            present_in  = [bn for bn in basenames if sid in seqs.get(bn, {})]
            absent_from = [bn for bn in basenames if sid not in seqs.get(bn, {})]

            row: dict = {"ID": sid}

            # Columns per file
            for bn in basenames:
                entry = seqs.get(bn, {}).get(sid)
                if entry:
                    row[f"{bn}_len"]  = entry[1]
                    row[f"{bn}_cov"]  = entry[2]
                    row[f"{bn}_ambs"] = entry[3]
                    row[f"{bn}_gaps"] = entry[4]
                else:
                    row[f"{bn}_len"] = row[f"{bn}_cov"] = \
                    row[f"{bn}_ambs"] = row[f"{bn}_gaps"] = ""

            if len(present_in) == 1:
                row["State"]        = f"Unique in {present_in[0]}"
                row["Best_run"] = present_in[0]
                row["Diff_bases"]     = ""
                row["Note"]          = "Not found in other runs"
                rows.append(row)
                self.notifyProgress.emit(int((prog_i + 1) / total * 100))
                continue

            # Compare all pairs where the ID is present
            pair_results: Dict[Tuple[str, str], Tuple[str, int, int, bool]] = {}
            for bn1, bn2 in pairs:
                if sid not in seqs.get(bn1, {}) or sid not in seqs.get(bn2, {}):
                    continue
                s1 = seqs[bn1][sid][0]
                s2 = seqs[bn2][sid][0]
                estado_par, d_amb, d_noamb, rc_used = _compare_sequences(s1, s2)
                pair_results[(bn1, bn2)] = (estado_par, d_amb, d_noamb, rc_used)

            # Length column: show when any pair has different lengths
            lens = {bn: len(seqs[bn][sid][0]) for bn in present_in}
            unique_lens = sorted(set(lens.values()))
            if len(unique_lens) > 1:
                if len(present_in) == 2:
                    bns = list(lens.keys())
                    l0, l1_ = lens[bns[0]], lens[bns[1]]
                    delta = abs(l0 - l1_)
                    sym = "<" if l0 < l1_ else ">"
                    row["Length"] = f"{bns[0]} {sym} {bns[1]}: Δ{delta} bp"
                else:
                    row["Length"] = f"{unique_lens[0]}–{unique_lens[-1]} bp"
            else:
                row["Length"] = ""

            # Global status = worst of peers
            estados_pares = [v[0] for v in pair_results.values()]
            global_raw = min(estados_pares, key=lambda e: priority.get(e, 99)) \
                         if estados_pares else 'identical'

            estado_display = {
                'identical':  'Identical',
                'compatible': 'Compatible (IUPAC)',
                'different':  'Different',
            }.get(global_raw, global_raw)
            row["State"] = estado_display

            # Note with detail per pair + accumulate Diff_bases and IUPAC_pos
            detail_parts = []
            max_dist = 0
            max_d_noamb = 0
            for (bn1, bn2), (ep, d_amb, d_noamb, rc_used) in pair_results.items():
                ep_es = {'identical': 'Identical', 'compatible': 'Compatible (IUPAC)',
                         'different': 'Different'}.get(ep, ep)
                nota_par = f"{bn1}↔{bn2}: {ep_es}"
                if ep == 'different' and d_amb:
                    nota_par += f" (d={d_amb})"
                    if d_amb > max_dist:
                        max_dist = d_amb
                elif ep == 'compatible' and d_noamb:
                    nota_par += f" (IUPAC_pos={d_noamb})"
                    if d_noamb > max_d_noamb:
                        max_d_noamb = d_noamb
                if rc_used:
                    nota_par += " [RC]"
                detail_parts.append(nota_par)

            row["Diff_bases"] = max_dist if max_dist else ""
            row["IUPAC_pos"]  = max_d_noamb if max_d_noamb else ""

            # best barcode
            seqs_present: List[_SeqEntry] = [
                (bn, seqs[bn][sid][0], seqs[bn][sid][1],
                 seqs[bn][sid][2], seqs[bn][sid][3], seqs[bn][sid][4])
                for bn in present_in
            ]
            best = _best_barcode(seqs_present)
            if global_raw == 'different':
                if best:
                    row["Best_run"] = best[0]
                    detail_parts.append(
                        f"Best: {best[0]} "
                        f"(ambs={best[4]}, gaps={best[5]}, cov={best[3]})"
                    )
                else:
                    row["Best_run"] = "Tie"
                    detail_parts.append("Tie in quality")
            else:
                row["Best_run"] = ""

            if absent_from:
                detail_parts.append(f"Absent from: {', '.join(absent_from)}")

            row["Note"] = " | ".join(detail_parts)
            rows.append(row)
            self.notifyProgress.emit(int((prog_i + 1) / total * 100))

        # Headers — include IUPAC_pos and Length only when present in data
        has_iupac  = any(row.get("IUPAC_pos", "") for row in rows)
        has_length = any(row.get("Length", "")    for row in rows)
        headers = ["ID", "State", "Best_run", "Diff_bases"]
        if has_iupac:
            headers.append("IUPAC_pos")
        if has_length:
            headers.append("Length")
        for bn in basenames:
            headers += [f"{bn}_len", f"{bn}_cov", f"{bn}_ambs", f"{bn}_gaps"]
        headers.append("Note")

        # Outputs
        _write_outputs(
            rows, headers, self.outdir,
            all_bns=basenames,
            ref_bn=None, seqs_store=seqs,
        )

        self.taskFinished.emit(rows, headers)


# ---------------------------------------------------------------------------
# Worker: N files vs reference
# ---------------------------------------------------------------------------

class _PairCompareWorker(QtCore.QThread):
    """
    Compares N files against a reference file chosen by the user.
    For each ID present in the reference determines its status in each of
    the compared files (Identical /IUPAC Compatible /Different /
    Without reference /Only in reference).
    Includes reverse complement detection and alignment with edlib.
    """
    notifyProgress = QtCore.pyqtSignal(int)
    taskFinished   = QtCore.pyqtSignal(list, list)   # rows, headers

    def __init__(self, file_list: List[str], ref_path: str,
                 outdir: str, extract_cfg=None, parent=None):
        super().__init__(parent)
        self.file_list   = file_list
        self.ref_path    = ref_path
        self.outdir      = outdir
        self.extract_cfg = extract_cfg

    def run(self):
        file_list = self.file_list
        all_bns   = _make_unique_labels(file_list)

        # Etiqueta de la referencia
        ref_bn = next(
            (lbl for f, lbl in zip(file_list, all_bns)
             if os.path.abspath(f) == os.path.abspath(self.ref_path)),
            all_bns[0],
        )
        comp_bns = [bn for bn in all_bns if bn != ref_bn]

        # Parse files
        seqs: Dict[str, Dict[str, Tuple]] = {}
        for fname, bn in zip(file_list, all_bns):
            seqs[bn] = _parse_fasta_file(fname, self.extract_cfg)

        ref_seqs = seqs.get(ref_bn, {})
        all_ids  = sorted(
            set(ref_seqs.keys()) |
            {sid for bn in comp_bns for sid in seqs.get(bn, {}).keys()}
        )
        total = len(all_ids)

        # Build rows
        rows: List[dict] = []
        priority = {"different": 0, "compatible": 1, "identical": 2}

        for prog_i, sid in enumerate(all_ids):
            row: dict = {"ID": sid}

            # Reference info
            if sid in ref_seqs:
                r = ref_seqs[sid]
                row[f"{ref_bn}_len"]  = r[1]
                row[f"{ref_bn}_cov"]  = r[2]
                row[f"{ref_bn}_ambs"] = r[3]
                row[f"{ref_bn}_gaps"] = r[4]
                ref_seq = r[0]
            else:
                row[f"{ref_bn}_len"] = row[f"{ref_bn}_cov"] = \
                row[f"{ref_bn}_ambs"] = row[f"{ref_bn}_gaps"] = ""
                ref_seq = None

            # Info files compared
            for bn in comp_bns:
                entry = seqs.get(bn, {}).get(sid)
                if entry:
                    row[f"{bn}_len"]  = entry[1]
                    row[f"{bn}_cov"]  = entry[2]
                    row[f"{bn}_ambs"] = entry[3]
                    row[f"{bn}_gaps"] = entry[4]
                else:
                    row[f"{bn}_len"] = row[f"{bn}_cov"] = \
                    row[f"{bn}_ambs"] = row[f"{bn}_gaps"] = ""
                row[f"{bn}_dif"] = ""

            # ── Only in reference ──────────────────────────────────────────
            if ref_seq is not None and all(
                sid not in seqs.get(bn, {}) for bn in comp_bns
            ):
                row["State"]        = "Only in reference"
                row["Best_run"] = ref_bn
                row["Diff_bases"]     = ""
                row["Note"]          = "Not found in any compared file"
                rows.append(row)
                self.notifyProgress.emit(int((prog_i + 1) / total * 100))
                continue

            # ── No reference ──────────────────────────────────────────────
            if ref_seq is None:
                present_bns = [bn for bn in comp_bns if sid in seqs.get(bn, {})]
                row["State"]        = "No reference"
                row["Best_run"] = ""
                row["Diff_bases"]     = ""
                row["Note"]          = f"Only in: {', '.join(present_bns)}"
                rows.append(row)
                self.notifyProgress.emit(int((prog_i + 1) / total * 100))
                continue

            # ── General case: compare each file vs reference ───────────
            estados_por_bn:  Dict[str, str]  = {}
            dists_por_bn:    Dict[str, int]  = {}
            noamb_por_bn:    Dict[str, int]  = {}
            rc_por_bn:       Dict[str, bool] = {}
            diff_candidates: List[_SeqEntry] = []
            max_dist    = 0
            max_d_noamb = 0

            for bn in comp_bns:
                if sid not in seqs.get(bn, {}):
                    estados_por_bn[bn] = "Absent"
                    dists_por_bn[bn]   = 0
                    noamb_por_bn[bn]   = 0
                    rc_por_bn[bn]      = False
                    row[f"{bn}_length"] = ""
                    continue

                comp_seq = seqs[bn][sid][0]
                estado_raw, d_amb, d_noamb, rc_used = _compare_sequences(
                    ref_seq, comp_seq)

                estados_por_bn[bn] = estado_raw
                dists_por_bn[bn]   = d_amb
                noamb_por_bn[bn]   = d_noamb
                rc_por_bn[bn]      = rc_used

                row[f"{bn}_dif"] = d_amb if d_amb else ""
                if d_amb and d_amb > max_dist:
                    max_dist = d_amb
                if estado_raw == 'compatible' and d_noamb > max_d_noamb:
                    max_d_noamb = d_noamb

                # Per-file length delta vs reference
                delta = len(comp_seq) - len(ref_seq)
                if delta == 0:
                    row[f"{bn}_length"] = ""
                elif delta > 0:
                    row[f"{bn}_length"] = f"> {delta} bp"
                else:
                    row[f"{bn}_length"] = f"< {-delta} bp"

                if estado_raw == 'different':
                    e = seqs[bn][sid]
                    diff_candidates.append(
                        (bn, e[0], e[1], e[2], e[3], e[4]))

            # Global Length summary: show when any file differs in length
            length_parts = [
                f"{bn}: {row[f'{bn}_length']}"
                for bn in comp_bns
                if row.get(f"{bn}_length", "")
            ]
            row["Length"] = " | ".join(length_parts) if length_parts else ""

            # Global status = worst individual (excluding absentees)
            valid = [e for e in estados_por_bn.values() if e != "Absent"]
            if not valid:
                global_raw = "Absent in all"
            else:
                global_raw = min(valid, key=lambda e: priority.get(e, 99))

            estado_display = {
                'identical':  'Identical',
                'compatible': 'Compatible (IUPAC)',
                'different':  'Different',
            }.get(global_raw, global_raw)
            row["State"]      = estado_display
            row["Diff_bases"] = max_dist    if max_dist    else ""
            row["IUPAC_pos"]  = max_d_noamb if max_d_noamb else ""

            # Detailed note per file
            detail_parts = []
            for bn in comp_bns:
                ep = estados_por_bn.get(bn, "Absent")
                ep_es = {'identical': 'Identical', 'compatible': 'Compatible (IUPAC)',
                         'different': 'Different'}.get(ep, ep)
                nota_bn = f"{bn}: {ep_es}"
                d = dists_por_bn.get(bn, 0)
                if d:
                    nota_bn += f" (d={d})"
                iupac_p = noamb_por_bn.get(bn, 0)
                if ep == 'compatible' and iupac_p:
                    nota_bn += f" (IUPAC_pos={iupac_p})"
                if rc_por_bn.get(bn):
                    nota_bn += " [RC]"
                detail_parts.append(nota_bn)

            # best barcode
            best = _best_barcode(diff_candidates) if diff_candidates else None
            if global_raw == 'different':
                if best:
                    row["Best_run"] = best[0]
                    detail_parts.append(
                        f"Best: {best[0]} "
                        f"(ambs={best[4]}, gaps={best[5]}, cov={best[3]})"
                    )
                else:
                    row["Best_run"] = "Tie"
                    detail_parts.append("Tie in quality")
            else:
                row["Best_run"] = ""

            row["Note"] = " | ".join(detail_parts)
            rows.append(row)
            self.notifyProgress.emit(int((prog_i + 1) / total * 100))

        # Headers — include IUPAC_pos and Length only when present in data
        has_iupac  = any(row.get("IUPAC_pos", "") for row in rows)
        has_length = any(row.get("Length", "")    for row in rows)
        headers = ["ID", "State", "Best_run", "Diff_bases"]
        if has_iupac:
            headers.append("IUPAC_pos")
        if has_length:
            headers.append("Length")
        headers += [f"{ref_bn}_len", f"{ref_bn}_cov",
                    f"{ref_bn}_ambs", f"{ref_bn}_gaps"]
        for bn in comp_bns:
            headers += [f"{bn}_len", f"{bn}_cov",
                        f"{bn}_ambs", f"{bn}_gaps", f"{bn}_dif",
                        f"{bn}_length"]
        headers.append("Note")

        # Outputs
        _write_outputs(
            rows, headers, self.outdir,
            all_bns=[ref_bn] + list(comp_bns),
            ref_bn=ref_bn, seqs_store=seqs,
        )

        self.taskFinished.emit(rows, headers)
