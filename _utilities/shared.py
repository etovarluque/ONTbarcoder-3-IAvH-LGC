#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared utilities, constants, and base widgets for ONTbarcoder3."""
from __future__ import annotations
import sys
import os
import json as _json_mod
import xml.etree.ElementTree as _ET
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtCore, QtGui, QtWidgets


# ── Application path ──────────────────────────────────────────────────────────

def _get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # Go up one level from _utilities/ to the project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _profiles_dir() -> str:
    path = os.path.join(_get_base_dir(), "_profiles")
    os.makedirs(path, exist_ok=True)
    return path


# ── i18n ──────────────────────────────────────────────────────────────────────

_LANG_CONFIG = os.path.join(os.path.expanduser("~"), ".ontbarcoder_lang.json")

def _load_lang():
    try:
        with open(_LANG_CONFIG) as f:
            return _json_mod.load(f).get("lang", "en")
    except Exception:
        return "en"

def _save_lang(lang):
    try:
        with open(_LANG_CONFIG, "w") as f:
            _json_mod.dump({"lang": lang}, f)
    except Exception:
        pass

_CURRENT_LANG = [_load_lang()]
_TRANSLATIONS = {}

def _parse_ts(path):
    result = {}
    try:
        root = _ET.parse(path).getroot()
        for ctx in root.findall("context"):
            name = ctx.findtext("name", "")
            d = {}
            for msg in ctx.findall("message"):
                src = msg.findtext("source", "")
                tr_el = msg.find("translation")
                if tr_el is not None and tr_el.text and tr_el.get("type") != "unfinished":
                    d[src] = tr_el.text
            result[name] = d
    except Exception:
        pass
    return result

def _load_translations():
    ts_dir = os.path.join(_get_base_dir(), "translations")
    for lang in ["en"]:
        ts_path = os.path.join(ts_dir, f"{lang}.ts")
        if os.path.exists(ts_path):
            _TRANSLATIONS[lang] = _parse_ts(ts_path)

_load_translations()

def _tr(context, source):
    lang = _CURRENT_LANG[0]
    if lang == "es":
        return source
    return _TRANSLATIONS.get(lang, {}).get(context, {}).get(source, source)

def set_language(lang, panels=None):
    _CURRENT_LANG[0] = lang
    _save_lang(lang)
    if panels:
        for panel in panels:
            QtWidgets.QApplication.postEvent(
                panel, QtCore.QEvent(QtCore.QEvent.LanguageChange)
            )


# ── Color palette ──────────────────────────────────────────────────────────
BLUE = "#185FA5"
BLUE_LIGHT = "#E6F1FB"
BLUE_MID = "#378ADD"
GREEN = "#3B6D11"
GREEN_LT = "#EAF3DE"
GREEN_MID = "#639922"
AMBER = "#854F0B"
AMBER_LT = "#FAEEDA"
RED = "#A32D2D"
WHITE = "#FFFFFF"
RED_LT = "#FCEBEB"
GRAY_BG = "#F5F5F3"
GRAY_CARD = "#F5F5F3"
GRAY_LINE = "#E0DED8"
TEXT_PRI = "#1A1A18"
TEXT_SEC = "#6B6960"
TEXT_HINT = "#A09D96"
SIDEBAR_BG = "#EDEDED"
TOPBAR_BG = "#2A2D3A"
GRAY_DARK = "#203864"

STYLESHEET = f"""
QWidget {{
    font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
    font-size: 18px;
    color: {TEXT_PRI};
    background-color: transparent;
}}
QMainWindow, #root_bg {{
    background-color: {GRAY_BG};
}}

/* ── Dialogs — explicit white background to avoid inheritance of OS dark theme ── */
QDialog {{
    background-color: {GRAY_CARD};
    color: {TEXT_PRI};
}}
QDialog QLabel {{
    color: {TEXT_PRI};
    background-color: transparent;
}}
QDialog QRadioButton {{
    color: {TEXT_PRI};
    background-color: transparent;
}}
QDialog QPushButton {{
    color: {TEXT_PRI};
}}
QMessageBox {{
    background-color: {GRAY_CARD};
    color: {TEXT_PRI};
}}
QMessageBox QLabel {{
    color: {TEXT_PRI};
    background-color: transparent;
}}

/* ── Sidebar ── */
#sidebar {{
    background-color: {SIDEBAR_BG};
    border-right: 1px solid {GRAY_LINE};
}}
#sidebar_item {{
    padding: 12px 16px;
    border-left: 8px solid transparent;
    color: {TEXT_SEC};
    background: transparent;
    text-align: left;
    border-radius: 0;
    font-size: 20px;
}}
#sidebar_item:hover {{
    background-color: {GRAY_BG};
}}
#sidebar_item[state="active"] {{
    color: {GRAY_DARK};
    background-color: {GRAY_BG};
    border-left-color: {BLUE_MID};
    font-weight: 500;
}}
#sidebar_item[state="done"] {{
    color: {GREEN};
    background: transparent;
    border-left: 2px solid transparent;
}}
#sidebar_item[state="locked"] {{
    color: {TEXT_HINT};
    background: transparent;
    border-left: 2px solid transparent;
}}
#sidebar_section {{
    font-size: 10px;
    font-weight: 600;
    color: {TEXT_HINT};
    padding: 8px 16px 2px;
    letter-spacing: 0.5px;
    background: transparent;
}}

/* ── Topbar ── */
#topbar {{
    background-color: {TOPBAR_BG};
    border-bottom: 1px solid {GRAY_LINE};
}}
#topbar_logo {{
    font-size: 28px;
    font-weight: 600;
    letter-spacing: -0.3px;
    color: {WHITE};
}}
#topbar_badge {{
    font-size: 15px;
    padding: 2px 8px;
    border-radius: 10px;
    background-color: {GRAY_BG};
    color: {BLUE};
    margin-top: 8px;
    margin-bottom: 8px;
}}

/* ── Cards ── */
#card {{
    background-color: {GRAY_CARD};
    border: 1px solid {GRAY_LINE};
    border-radius: 10px;
    padding: 16px;
}}
#stat_card {{
    background-color: {GRAY_BG};
    border-radius: 8px;
    padding: 12px;
}}

/* ── Mode cards ── */
#mode_card {{
    background-color: {GRAY_CARD};
    border: 1px solid {GRAY_LINE};
    border-radius: 10px;
    padding: 16px;
}}
#mode_card:hover {{
    border-color: {BLUE_MID};
}}
#mode_card[selected="true"] {{
    border: 2px solid {BLUE_MID};
    background-color: {BLUE_LIGHT};
}}

/* ── Drop zones ── */
#drop_zone {{
    background-color: {GRAY_BG};
    border: 0.5px solid #E6E6E3;
    border-radius: 10px;
    padding: 24px;
}}
#drop_zone[dragging="true"] {{
    background-color: #EBEBEA;
    border: 2px dashed {BLUE_MID};
    border-radius: 10px;
    padding: 24px;
}}
#drop_zone[filled="true"] {{
    background-color: {GREEN_LT};
    border: 1px solid {GREEN_MID};
    border-style: solid;
}}

/* ── Buttons ── */
#primary_btn {{
    background-color: {BLUE};
    color: white;
    border: none;
    border-radius: 8px;
    padding: 9px 20px;
    font-size: 18px;
    font-weight: 500;
}}
#primary_btn:hover {{ background-color: #0C4A82; }}
#primary_btn:pressed {{ background-color: #083460; }}
#primary_btn:disabled {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; }}

/* Unified style for secondary buttons (Search, Gold, MinKNOW, etc.) */
.secondary-btn, #secondary_btn, .basecalling-btn {{
    background-color: transparent;
    color: {BLUE};
    border: 1px solid {BLUE_MID};
    border-radius: 8px;
    padding: 7px 16px;
    font-size: 18px;
}}
.secondary-btn:hover, #secondary_btn:hover, .basecalling-btn:hover {{
    background-color: {BLUE_LIGHT};
    color: {BLUE};
    border-color: {BLUE};
}}
.secondary-btn:pressed, #secondary_btn:pressed, .basecalling-btn:pressed {{
    background-color: {BLUE_MID};
    color: white;
}}

#danger_btn {{
    background-color: transparent;
    color: {RED};
    border: 1px solid #F09595;
    border-radius: 8px;
    padding: 7px 14px;
    font-size: 18px;
}}
#danger_btn:hover {{
    background-color: {RED_LT};
    color: {RED};
    border-color: {RED};
}}
#danger_btn:pressed {{
    background-color: {RED};
    color: white;
}}

/* ── Inputs ── */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {GRAY_CARD};
    border: 1px solid {GRAY_LINE};
    border-radius: 6px;
    padding: 5px 9px;
    font-size: 18px;
    selection-background-color: {BLUE_LIGHT};
    selection-color: {TEXT_PRI};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {BLUE_MID};
    background-color: {GRAY_CARD};
    color: {TEXT_PRI};
}}

QSpinBox QLineEdit, QDoubleSpinBox QLineEdit {{
    background-color: {GRAY_CARD};
    selection-background-color: {BLUE_LIGHT};
    selection-color: {TEXT_PRI};
    color: {TEXT_PRI};
}}
QComboBox {{ padding-right: 20px; }}
QComboBox::drop-down {{ width: 20px; subcontrol-origin: padding; subcontrol-position: top right; }}

/* ── Tabs ── */
QTabWidget::pane {{
    border: 1px solid {GRAY_LINE};
    border-top: none;
    border-radius: 0 0 8px 8px;
    background: {GRAY_CARD};
}}
QTabBar::tab {{
    background: transparent;
    border-bottom: 2px solid transparent;
    padding: 7px 14px;
    font-size: 17px;
    color: {TEXT_SEC};
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    color: {BLUE};
    border-bottom-color: {BLUE_MID};
    font-weight: 500;
}}
QTabBar::tab:hover:!selected {{
    color: {TEXT_PRI};
}}

/* ── Progress bar ── */
QProgressBar {{
    background-color: {GRAY_LINE};
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background-color: {BLUE_MID};
    border-radius: 3px;
}}

/* ── Log / text areas ── */
#log_area {{
    background-color: {GRAY_CARD};
    border: 1px solid {GRAY_LINE};
    border-radius: 8px;
    font-family: 'Courier New', monospace;
    font-size: 14px;
    color: {TEXT_SEC};
    padding: 10px;
}}

/* ── Scrollbars ── */
QScrollBar:vertical {{
    width: 6px;
    background: transparent;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {GRAY_LINE};
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

/* ── Phase rows ── */
#phase_row {{
    background-color: {GRAY_CARD};
    border: 1px solid {GRAY_LINE};
    border-radius: 8px;
    padding: 18px 14px;
}}
#phase_row[state="current"] {{
    border-color: {BLUE_MID};
    background-color: {BLUE_LIGHT};
}}
#phase_row[state="done"] {{
    border-color: {GREEN_MID};
    background-color: {GREEN_LT};
}}

/* ── Dialogs and buttons ── */
QDialog QDialogButtonBox QPushButton,
QMessageBox QPushButton {{
    background-color: transparent;
    color: #185FA5;
    border: 1px solid #378ADD;
    border-radius: 8px;
    padding: 7px 16px;
    font-size: 18px;
    min-width: 80px;
}}

QDialog QDialogButtonBox QPushButton:hover,
QMessageBox QPushButton:hover {{
    background-color: #E6F1FB;
    color: #185FA5;
    border-color: #185FA5;
}}

QDialog QDialogButtonBox QPushButton:pressed,
QMessageBox QPushButton:pressed {{
    background-color: #378ADD;
    color: white;
}}

/* Default button (the one with focus) */
QDialog QDialogButtonBox QPushButton[default="true"],
QMessageBox QPushButton[default="true"] {{
    background-color: #185FA5;
    color: white;
    border: none;
}}

QDialog QDialogButtonBox QPushButton[default="true"]:hover {{
    background-color: #0C4A82;
}}

"""


# ═══════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════

def make_label(text, size=19, bold=False, color=TEXT_PRI):
    lbl = QtWidgets.QLabel(text)
    weight = "600" if bold else "400"
    lbl.setStyleSheet(f"font-size:{size}px; font-weight:{weight}; color:{color};")
    return lbl


def make_section_label(text):
    lbl = QtWidgets.QLabel(text.upper())
    lbl.setStyleSheet(f"font-size:16px; font-weight:600; color:{TEXT_HINT}; letter-spacing:0.5px;")
    return lbl


def hline():
    line = QtWidgets.QFrame()
    line.setFrameShape(QtWidgets.QFrame.HLine)
    line.setStyleSheet(f"color:{GRAY_LINE}; border:none; border-top:1px solid {GRAY_LINE};")
    return line


def refresh_style(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def _fmt_num(v):
    """Format a number compactly for axis labels: 1234567 -> '1.23M', 12345 -> '12.3K'."""
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v/1_000:.1f}K"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(int(v))


# ═══════════════════════════════════════════════════════════════════════════
# BASE PANEL
# ═══════════════════════════════════════════════════════════════════════════

class BasePanel(QtWidgets.QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._inner = QtWidgets.QWidget()
        self._layout = QtWidgets.QVBoxLayout(self._inner)
        self._layout.setContentsMargins(20, 20, 20, 20)
        self._layout.setSpacing(16)
        self.setWidget(self._inner)

    def add(self, widget, stretch=0):
        self._layout.addWidget(widget, stretch)

    def add_stretch(self):
        self._layout.addStretch()


# ═══════════════════════════════════════════════════════════════════════════
# DROP ZONE (single file)
# ═══════════════════════════════════════════════════════════════════════════

class DropZone(QtWidgets.QFrame):
    fileDropped = QtCore.pyqtSignal(str)

    def __init__(self, label, hint="", extensions=None, parent=None):
        super().__init__(parent)
        self.setObjectName("drop_zone")
        self.setAcceptDrops(True)
        self._extensions = extensions or []
        self._filepath = ""
        self._original_label = label
        self._original_hint = hint

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(4)
        layout.setAlignment(QtCore.Qt.AlignCenter)

        self._label = make_label(label, color=TEXT_SEC)
        self._label.setAlignment(QtCore.Qt.AlignCenter)
        self._hint = make_label(hint, size=15, color=TEXT_HINT)
        self._hint.setAlignment(QtCore.Qt.AlignCenter)
        self._file_lbl = make_label("", size=15, bold=True, color=GREEN)
        self._file_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self._file_lbl.hide()

        self._drag_icon_lbl = QtWidgets.QLabel()
        self._drag_icon_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self._drag_icon_lbl.hide()

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.setAlignment(QtCore.Qt.AlignCenter)

        self._browse_btn = QtWidgets.QPushButton("Browse...")
        self._browse_btn.setObjectName("secondary_btn")
        self._browse_btn.setFixedWidth(120)
        self._browse_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {BLUE};
                border: 1px solid {BLUE_MID};
                border-radius: 8px;
                padding: 7px 16px;
                font-size: 18px;
            }}
            QPushButton:hover {{
                background-color: {BLUE_LIGHT};
                color: {BLUE};
                border-color: {BLUE};
            }}
            QPushButton:pressed {{
                background-color: {BLUE_MID};
                color: white;
            }}
        """)
        self._browse_btn.clicked.connect(self._browse)

        self._clear_btn = QtWidgets.QPushButton("✕ Remove")
        self._clear_btn.setObjectName("danger_btn")
        self._clear_btn.setFixedWidth(120)
        self._clear_btn.clicked.connect(self.clear)
        self._clear_btn.hide()

        btn_row.addWidget(self._browse_btn)
        btn_row.addWidget(self._clear_btn)

        layout.addWidget(self._drag_icon_lbl)
        layout.addWidget(self._label)
        layout.addWidget(self._hint)
        layout.addWidget(self._file_lbl)
        layout.addSpacing(8)
        layout.addLayout(btn_row)

    def retranslateUi(self):
        ctx = "DropZone"
        self._browse_btn.setText(_tr(ctx, "Browse..."))
        self._clear_btn.setText(_tr(ctx, "✕ Remove"))
        self._hint.setText(_tr(ctx, self._original_hint))
        if not self._filepath:
            self._label.setText(_tr(ctx, self._original_label))
        else:
            self._label.setText(_tr(ctx, "File loaded"))

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def _browse(self):
        ctx = "DropZone"
        ext_str = " ".join(f"*{e}" for e in self._extensions) if self._extensions else "*"
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, _tr(ctx, "Select file"), "", f"Files ({ext_str})"
        )
        if path:
            self._set_file(path)

    def _set_file(self, path):
        self._filepath = path
        name = os.path.basename(path)
        size = os.path.getsize(path)
        if size >= 1_073_741_824:
            size_str = f"{size/1_073_741_824:.2f} GB"
        elif size >= 1_048_576:
            size_str = f"{size/1_048_576:.1f} MB"
        elif size >= 1_024:
            size_str = f"{size/1_024:.1f} KB"
        else:
            size_str = f"{size} B"
        self._file_lbl.setText(f"{name}  ·  {size_str}")
        self._label.setText(_tr("DropZone", "File loaded"))
        self._file_lbl.show()
        self._clear_btn.show()
        self.setProperty("filled", "true")
        self.setCursor(QtCore.Qt.PointingHandCursor)
        refresh_style(self)
        self.fileDropped.emit(path)

    def clear(self):
        self._filepath = ""
        self._file_lbl.hide()
        self._clear_btn.hide()
        self._label.setText(_tr("DropZone", self._original_label))
        self.setProperty("filled", "false")
        refresh_style(self)
        self.fileDropped.emit("")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setProperty("dragging", "true")
            urls = e.mimeData().urls()
            if urls:
                path = urls[0].toLocalFile()
                if path:
                    icon = QtWidgets.QFileIconProvider().icon(QtCore.QFileInfo(path))
                    self._drag_icon_lbl.setPixmap(icon.pixmap(48, 48))
                    self._drag_icon_lbl.show()
            refresh_style(self)

    def dragLeaveEvent(self, e):
        self.setProperty("dragging", "false")
        self._drag_icon_lbl.hide()
        refresh_style(self)

    def dropEvent(self, e):
        self.setProperty("dragging", "false")
        self._drag_icon_lbl.hide()
        refresh_style(self)
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            if self._extensions:
                if any(path.lower().endswith(ext) for ext in self._extensions):
                    self._set_file(path)
                    return
            else:
                self._set_file(path)
                return

    @property
    def filepath(self):
        return self._filepath


# ═══════════════════════════════════════════════════════════════════════════
# WIDGET HELPERS (used by ParamsPanel and others)
# ═══════════════════════════════════════════════════════════════════════════

def _spin(min_v, max_v, default, step=1, decimals=0):
    if decimals > 0:
        w = QtWidgets.QDoubleSpinBox()
        w.setDecimals(decimals)
        w.setSingleStep(step)
    else:
        w = QtWidgets.QSpinBox()
    w.setRange(min_v, max_v)
    w.setValue(default)
    return w


def _field(label_text, widget, tooltip=""):
    row = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    lbl = make_label(label_text, size=16, color=TEXT_SEC)
    if tooltip:
        lbl.setToolTip(tooltip)
        widget.setToolTip(tooltip)
    layout.addWidget(lbl)
    layout.addWidget(widget)
    return row


def _grid(*fields, cols=2):
    container = QtWidgets.QWidget()
    grid = QtWidgets.QGridLayout(container)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setSpacing(12)
    for i, f in enumerate(fields):
        grid.addWidget(f, i // cols, i % cols)
    return container


# ═══════════════════════════════════════════════════════════════════════════
# MULTI DROP ZONE (multiple files, used by Compare, BLAST, FastaTools)
# ═══════════════════════════════════════════════════════════════════════════

class MultiDropZone(QtWidgets.QFrame):
    filesDropped = QtCore.pyqtSignal(list)

    _ROW_H   = 60   # px per file row (content + spacing)
    _CHROME  = 200   # label + buttons + layout margins/spacings (6+24+8+8+36+6)
    _EMPTY_H = 200   # height when no files

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drop_zone")
        self.setAcceptDrops(True)
        self.setFixedHeight(self._EMPTY_H)
        self._files = []  # Lista acumulativa
        self._seq_cache: dict = {}  # path → seq count (evita reconteo al refrescar)
        self._supported_extensions = (".fa", ".fas", ".fasta", ".fa.gz", ".fas.gz", ".fasta.gz", ".fna", ".fna.gz")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        # ── Main label (compact, with hint as tooltip) ──
        self._lbl = make_label(
            "Drag/Add FASTA files here",
            size=18, color=TEXT_SEC)
        self._lbl.setAlignment(QtCore.Qt.AlignCenter)
        self._lbl.setToolTip("You can drag multiple files at once or one at a time.")

        # ── Hint (hidden, kept for compatibility) ──
        self._hint = make_label("", size=14, color=TEXT_HINT)
        self._hint.hide()

        self._drag_icon_lbl = QtWidgets.QLabel()
        self._drag_icon_lbl.setAlignment(QtCore.Qt.AlignCenter)
        self._drag_icon_lbl.hide()

        # ── Container for the list of files ──
        self._files_container = QtWidgets.QWidget()
        self._files_layout = QtWidgets.QVBoxLayout(self._files_container)
        self._files_layout.setContentsMargins(0, 0, 0, 0)
        self._files_layout.setSpacing(8)
        self._files_container.hide()

        # ── Buttons ──
        btn_layout = QtWidgets.QHBoxLayout()
        btn_layout.setSpacing(8)
        btn_layout.setAlignment(QtCore.Qt.AlignCenter)

        self._browse_btn = QtWidgets.QPushButton("Add")
        self._browse_btn.setObjectName("secondary_btn")
        self._browse_btn.setFixedWidth(130)
        self._browse_btn.clicked.connect(self._browse_files)
        self._lbl_src_empty = "Drag/Add FASTA files here"
        self._lbl_src_filled = "Uploaded"

        self._clear_btn = QtWidgets.QPushButton("Clear")
        self._clear_btn.setObjectName("danger_btn")
        self._clear_btn.setFixedWidth(130)
        self._clear_btn.clicked.connect(self.clear)
        self._clear_btn.hide()

        btn_layout.addWidget(self._browse_btn)
        btn_layout.addWidget(self._clear_btn)

        # ── Add everything to the main layout ──
        _exp = QtWidgets.QSizePolicy
        self._top_spacer = QtWidgets.QSpacerItem(0, 0, _exp.Minimum, _exp.Expanding)
        self._bot_spacer = QtWidgets.QSpacerItem(0, 0, _exp.Minimum, _exp.Expanding)
        layout.addSpacerItem(self._top_spacer)
        layout.addWidget(self._drag_icon_lbl)
        layout.addWidget(self._lbl)
        layout.addWidget(self._files_container)
        layout.addLayout(btn_layout)
        layout.addSpacerItem(self._bot_spacer)

    def retranslateUi(self):
        ctx = "MultiDropZone"
        self._browse_btn.setText(_tr(ctx, "Add"))
        self._clear_btn.setText(_tr(ctx, "Clear"))
        if len(self._files) == 0:
            self._lbl.setText(_tr(ctx, self._lbl_src_empty))
        else:
            total_seqs = sum(self._seq_cache.get(f, 0) for f in self._files)
            self._lbl.setText(
                f"{_tr(ctx, self._lbl_src_filled)} ({len(self._files)} files · {total_seqs:,} seqs)"
            )

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    @staticmethod
    def _count_seqs(filepath):
        """Count FASTA sequences by counting lines starting with '>'."""
        try:
            opener = __import__("gzip").open if filepath.endswith(".gz") else open
            with opener(filepath, "rt", encoding="utf-8", errors="replace") as fh:
                return sum(1 for ln in fh if ln.startswith(">"))
        except Exception:
            return 0

    def _create_file_row(self, filepath, index):
        """Create a row with folder/file, sequence count and remove button."""
        parent_dir = os.path.basename(os.path.dirname(os.path.abspath(filepath)))
        display_name = f"{parent_dir}/{os.path.basename(filepath)}" if parent_dir else os.path.basename(filepath)

        row_widget = QtWidgets.QWidget()
        row_widget.setObjectName("file_row")
        row_widget.setStyleSheet(f"""
            QWidget#file_row {{
                background-color: {GRAY_BG};
                border-radius: 7px;
                border: 1px solid {GRAY_LINE};
            }}
            QWidget#file_row:hover {{
                background-color: {BLUE_LIGHT};
                border-color: #B8D4F0;
            }}
        """)
        row_widget.setToolTip(filepath)

        row_layout = QtWidgets.QHBoxLayout(row_widget)
        row_layout.setContentsMargins(12, 8, 10, 8)
        row_layout.setSpacing(10)

        icon_lbl = make_label("🧬", size=15)
        icon_lbl.setFixedWidth(24)

        center = QtWidgets.QVBoxLayout()
        center.setSpacing(1)
        name_lbl = make_label(display_name, size=15, color=TEXT_PRI)
        name_lbl.setWordWrap(False)

        n_seqs = self._seq_cache.get(filepath)
        if n_seqs is None:
            n_seqs = self._count_seqs(filepath)
            self._seq_cache[filepath] = n_seqs
        seq_lbl = make_label(
            f"{n_seqs:,} sequences",
            size=14, color=TEXT_HINT
        )
        center.addWidget(name_lbl)
        center.addWidget(seq_lbl)

        remove_btn = QtWidgets.QPushButton("✕")
        remove_btn.setFixedSize(26, 26)
        remove_btn.setToolTip("Remove file")
        remove_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {TEXT_HINT};
                border: none;
                border-radius: 5px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {RED_LT};
                color: {RED};
            }}
            QPushButton:pressed {{ background-color: {RED}; color: white; }}
        """)
        remove_btn.clicked.connect(lambda checked, idx=index: self.remove_file(idx))

        row_layout.addWidget(icon_lbl)
        row_layout.addLayout(center, 1)
        row_layout.addWidget(remove_btn)

        return row_widget

    def _adjust_height(self):
        n = len(self._files)
        if n == 0:
            self.setFixedHeight(self._EMPTY_H)
        else:
            content_h = n * self._ROW_H - (n - 1) * 8
            self.setFixedHeight(self._CHROME + content_h)

    def _update_display(self):
        """Update the file list in the interface"""
        n = len(self._files)

        if n == 0:
            self._files_container.hide()
            self._clear_btn.hide()
            self._lbl.setText(_tr("MultiDropZone", self._lbl_src_empty))
            self.setProperty("filled", "false")
        else:
            while self._files_layout.count() > 0:
                item = self._files_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

            for i, f in enumerate(self._files):
                row = self._create_file_row(f, i)
                self._files_layout.addWidget(row)

            self._files_container.show()
            self._clear_btn.show()
            total_seqs = sum(self._seq_cache.get(f, 0) for f in self._files)
            self._lbl.setText(
                f"{_tr('MultiDropZone', self._lbl_src_filled)} ({n} files · {total_seqs:,} seqs)"
            )
            self.setProperty("filled", "true")

        _sp = QtWidgets.QSizePolicy
        if n == 0:
            self._top_spacer.changeSize(0, 0, _sp.Minimum, _sp.Expanding)
            self._bot_spacer.changeSize(0, 0, _sp.Minimum, _sp.Expanding)
        else:
            self._top_spacer.changeSize(0, 0, _sp.Minimum, _sp.Fixed)
            self._bot_spacer.changeSize(0, 0, _sp.Minimum, _sp.Fixed)
        self.layout().invalidate()

        self._adjust_height()
        refresh_style(self)
        refresh_style(self._files_container)

    def _browse_files(self):
        """Open dialog to select multiple files"""
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, _tr("MultiDropZone", "Select FASTA files"), "",
            "FASTA files (*.fa *.fas *.fasta *.fa.gz *.fas.gz *.fasta.gz *.fna *.fna.gz);;All (*)"
        )
        if files:
            self._add_files(files)

    def _add_files(self, new_files):
        """Add new files without overwriting existing ones"""
        added = 0
        for f in new_files:
            if f not in self._files:
                self._files.append(f)
                added += 1

        if added > 0:
            self._update_display()
            self.filesDropped.emit(self._files.copy())

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setProperty("dragging", "true")
            urls = e.mimeData().urls()
            if urls:
                path = urls[0].toLocalFile()
                if path:
                    icon = QtWidgets.QFileIconProvider().icon(QtCore.QFileInfo(path))
                    self._drag_icon_lbl.setPixmap(icon.pixmap(48, 48))
                    self._drag_icon_lbl.show()
            refresh_style(self)

    def dragLeaveEvent(self, e):
        self.setProperty("dragging", "false")
        self._drag_icon_lbl.hide()
        refresh_style(self)

    def dropEvent(self, e):
        self.setProperty("dragging", "false")
        self._drag_icon_lbl.hide()
        refresh_style(self)
        paths = []
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            if any(path.lower().endswith(ext) for ext in self._supported_extensions):
                paths.append(path)

        if paths:
            self._add_files(paths)

    def clear(self):
        self._files = []
        self._seq_cache = {}
        self._update_display()
        self.filesDropped.emit([])

    def remove_file(self, index):
        """Delete a specific file by index"""
        if 0 <= index < len(self._files):
            removed = self._files.pop(index)
            self._update_display()
            self.filesDropped.emit(self._files.copy())
            return removed
        return None

    @property
    def files(self):
        return self._files.copy()
