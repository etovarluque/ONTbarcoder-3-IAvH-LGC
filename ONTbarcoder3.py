#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ontbarcoder3_gui.py - Fixed version with full multiprocessing support
                    and identical results between conventional and real-time mode
"""
from __future__ import annotations
import sys
sys.setrecursionlimit(5000)
import os

# ── UI scale ─────────────────────────────────────────────────────────────────
# The GUI is laid out in *logical* pixels for a design canvas of
# UI_DESIGN_W × UI_DESIGN_H (which matches the main window's minimum size).  On a
# screen smaller than that canvas the widgets/text get clipped ("everything
# hidden"); on a much larger screen the UI looks tiny.  Because the stylesheet
# font sizes are in fixed *pixels*, a single hand-picked factor only fits one
# resolution — which is why UI_SCALE used to be edited by hand per monitor.
#
# When UI_FIT_SCREEN is True (default) the app measures the available desktop
# area at start-up and picks the largest scale (≤ UI_MAX_SCALE) at which the
# whole design canvas still fits, flooring at UI_MIN_SCALE for readability.  One
# build then adapts to any resolution with no manual editing.  Set
# UI_FIT_SCREEN = False to apply UI_SCALE verbatim (old behaviour).
# These are read before QApplication is created; they take effect app-wide.
UI_SCALE: float = 0.8          # manual factor, used only when UI_FIT_SCREEN=False
UI_FIT_SCREEN: bool = True     # auto-fit the window to the screen resolution
UI_MAX_SCALE: float = 1.0      # never enlarge beyond the design baseline
UI_MIN_SCALE: float = 0.6      # never shrink below this (readability floor)
UI_DESIGN_W: int = 1280        # design canvas width  (logical px, = min window)
UI_DESIGN_H: int = 920         # design canvas height (logical px, = min window)
# ─────────────────────────────────────────────────────────────────────────────


def _compute_ui_scale_factor() -> float:
    """Return the value to assign to QT_SCALE_FACTOR.

    Qt multiplies QT_SCALE_FACTOR by the Windows DPI scale, so the physical
    footprint of the window is  design_px × win_scale × QT_SCALE_FACTOR.  To
    guarantee the design canvas (UI_DESIGN_W × UI_DESIGN_H) fits inside the
    available desktop area (taskbar excluded) we solve for the largest factor
    that keeps that footprint on-screen:

        QT_SCALE_FACTOR = min( avail_w / (UI_DESIGN_W × win_scale),
                               avail_h / (UI_DESIGN_H × win_scale) )

    capped at UI_MAX_SCALE and floored at UI_MIN_SCALE.  If the screen metrics
    cannot be read we fall back to applying UI_SCALE verbatim."""
    if not UI_FIT_SCREEN:
        return UI_SCALE
    try:
        import ctypes
        # Become DPI-aware so the pixel/size queries return physical values,
        # not the logical ones Windows hands to non-aware processes.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        user32 = ctypes.windll.user32

        # Windows display-scaling factor (1.0 @100%, 1.25 @125% …).
        try:
            win_scale = (user32.GetDpiForSystem() or 96) / 96.0
        except Exception:
            win_scale = 1.0
        if win_scale <= 0:
            win_scale = 1.0

        # Available desktop area in *physical* pixels (taskbar excluded).
        class _RECT(ctypes.Structure):
            _fields_ = [("left",  ctypes.c_long), ("top",    ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        wa = _RECT()
        SPI_GETWORKAREA = 0x0030
        if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0,
                                            ctypes.byref(wa), 0):
            return UI_SCALE
        avail_w = wa.right - wa.left
        avail_h = wa.bottom - wa.top
        if avail_w <= 0 or avail_h <= 0:
            return UI_SCALE

        # Reserve room for the native title bar and window frame so the whole
        # window (not just the client area) stays inside the work area.  The OS
        # frame scales with the Windows DPI (win_scale), not with QT_SCALE_FACTOR,
        # so the reserve must scale with win_scale too or it under-reserves at
        # high DPI (e.g. 150%) and the window slips behind the taskbar.
        frame_w = 16 * win_scale
        frame_h = 48 * win_scale
        fit_w = (avail_w - frame_w) / float(UI_DESIGN_W * win_scale)
        fit_h = (avail_h - frame_h) / float(UI_DESIGN_H * win_scale)

        factor = min(UI_MAX_SCALE, fit_w, fit_h)
        return max(UI_MIN_SCALE, factor)
    except Exception:
        return UI_SCALE
# ─────────────────────────────────────────────────────────────────────────────
import datetime
import time
import shutil
import fnmatch
import csv
import warnings
import functools
import multiprocessing
import threading
import concurrent.futures
import subprocess
import xlsxwriter
import itertools
import edlib
from collections import Counter
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtCore

# Suppress non-relevant warnings from Biopython (partial codons, etc.)
warnings.filterwarnings("ignore", message="Partial codon")
warnings.filterwarnings("ignore", category=UserWarning, module="Bio")

from PyQt5 import QtCore, QtGui, QtWidgets

# Import workers from ONTbarcoder3_multiprocessing
def _get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _profiles_dir() -> str:
    path = os.path.join(_get_base_dir(), "_profiles")
    os.makedirs(path, exist_ok=True)
    return path

sys.path.insert(0, _get_base_dir())
sys.path.insert(0, os.path.join(_get_base_dir(), "_utilities"))
from _utilities.ONTbarcoder3_multiprocessing import (
    prepdemultiplex, runconsensusparts, MSAcheck, mergedemfiles,
    calculatecoverage, runtoptwenty, copyfiles,
    rundemultiplex, pool_init, pool_init1, pool_init2
)
import _utilities.ONTbarcoder3_multiprocessing as _ont_mp



from _utilities.compare_panel import ComparePanel, _CompareWorker, _PairCompareWorker
from _utilities.blast_panel import BlastPanel, _BlastWorker
from _utilities.fastq_inspector import FastqInspectorPanel
from _utilities.fasta_tools import FastaToolsPanel
from _utilities.notes_panel import NotesPanel
# ── i18n ──────────────────────────────────────────────────────────────────────
import json as _json_mod
import xml.etree.ElementTree as _ET

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


# ── Color palette ──────────────────────────────────────────────────────
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
SIDEBAR_BG = "#E4E4E4"
TOPBAR_BG = "#2A2D3A"
GRAY_DARK = "#203864"

STYLESHEET = f"""
QWidget {{
    font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
    font-size: 16px;
    color: {TEXT_PRI};
    background-color: transparent;
}}
QRadioButton {{
    font-size: 18px;
}}
QMainWindow, #root_bg {{
    background-color: {GRAY_BG};
}}
QToolTip {{
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 15px;
    color: {TEXT_PRI};
    background-color: {GRAY_CARD};
    border: 1px solid {GRAY_LINE};
    padding: 6px 8px;
    border-radius: 4px;
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
    margin-top: 8px;      /* ← Space above */
    margin-bottom: 8px;   /* ← Space below */
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
    border: 2px dashed #E6E6E3;
    border-radius: 10px;
    padding: 24px;
}}
#drop_zone[dragging="true"] {{
    background-color: #EBEBEA;
    border: 2px dashed {BLUE_MID};
    border-radius: 10px;
    padding: 24px;
}}
#drop_zone[dragging="invalid"] {{
    background-color: {RED_LT};
    border: 2px dashed {RED};
    border-radius: 10px;
    padding: 24px;
}}
#drop_zone[filled="true"] {{
    background-color: {GREEN_LT};
    border: 1px dashed {GREEN_MID};
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
    font-size: 16px;
    color: {TEXT_SEC};
    padding: 10px;
}}

/* ── Context menus ── */
QMenu {{
    background-color: {WHITE};
    color: {TEXT_PRI};
    border: 1px solid {GRAY_LINE};
    border-radius: 6px;
    padding: 4px 0px;
    font-size: 13px; 
}}
QMenu::item {{
    padding: 6px 24px 6px 12px;
    background-color: transparent;
}}
QMenu::item:selected {{
    background-color: {BLUE_LIGHT};
    color: {TEXT_PRI};
    border-radius: 4px;
}}
QMenu::item:disabled {{
    color: {TEXT_HINT};
}}
QMenu::separator {{
    height: 1px;
    background: {GRAY_LINE};
    margin: 4px 8px;
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
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════

class SidebarWidget(QtWidgets.QWidget):
    panelRequested = QtCore.pyqtSignal(str)

    ITEMS = [
        ("setup",      "Input files"),
        ("params",     "Parameters"),
        ("progress",   "Progress"),
        ("live_chart", "📈 RT Charts"),
        ("results",    "Results"),
    ]
    TOOLS = [
        ("compare",         "FASTA Compare"),
        ("fasta_tools",     "FASTA Tools"),
        ("fastq_inspector", "FASTQ Inspector"),
        ("blast",           "BLAST"),
        ("notes",           "NOTES 📝"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setFixedWidth(220)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)

        self._buttons = {}
        self._states = {k: "pending" for k, _ in self.ITEMS + self.TOOLS}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 12)
        layout.setSpacing(0)

        _section_style = (
            f"font-size:18px; font-weight:600; color:{WHITE}; letter-spacing:0.5px;"
            f" background-color:{TOPBAR_BG}; padding:4px 0px;"
        )
        self._lbl_workflow = make_section_label("  Workflow")
        self._lbl_workflow.setStyleSheet(_section_style)
        layout.addWidget(self._lbl_workflow)
        layout.addSpacing(4)

        for key, label in self.ITEMS:
            btn = self._make_item(key, label)
            layout.addWidget(btn)
            self._buttons[key] = btn

        # RT graphics only visible in live mode
        self._buttons["live_chart"].setVisible(False)

        layout.addSpacing(12)
        self._lbl_tools = make_section_label("  Utilities")
        self._lbl_tools.setStyleSheet(_section_style)
        layout.addWidget(self._lbl_tools)
        layout.addSpacing(4)

        for key, label in self.TOOLS:
            btn = self._make_item(key, label)
            layout.addWidget(btn)
            self._buttons[key] = btn

        layout.addStretch()

        self._quit_btn = QtWidgets.QPushButton("Quit")
        self._quit_btn.setObjectName("secondary_btn")
        self._quit_btn.setFixedHeight(40)
        self._quit_btn.setStyleSheet(
            f"QPushButton {{ margin:0 12px; color:{RED}; border-color:#F09595; "
            f"background:transparent; border-radius:8px; padding:5px 10px; font-size:18px; }}"
            f"QPushButton:hover {{ background-color:#F58181; color:{WHITE}; }}"
            f"QPushButton:pressed {{ background-color:#EE5050; color:{WHITE}; }}"
        )
        self._quit_btn.clicked.connect(QtWidgets.QApplication.quit)
        layout.addWidget(self._quit_btn)

        self.set_active("setup")

    def retranslateUi(self):
        ctx = "SidebarWidget"
        self._lbl_workflow.setText(("  " + _tr(ctx, "Workflow")).upper())
        self._lbl_tools.setText(("  " + _tr(ctx, "Utilities")).upper())
        self._quit_btn.setText(_tr(ctx, "Quit"))
        for key, src_label in self.ITEMS + self.TOOLS:
            if key in self._buttons:
                self._buttons[key].setText(_tr(ctx, src_label))

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def _make_item(self, key, label):
        btn = QtWidgets.QPushButton(label)
        btn.setObjectName("sidebar_item")
        btn.setFixedHeight(58)
        btn.setCursor(QtCore.Qt.PointingHandCursor)
        btn.setProperty("state", "pending")
        btn.clicked.connect(lambda _, k=key: self._on_item_clicked(k))
        return btn

    def _on_item_clicked(self, key):
        if self._states.get(key) == "locked":
            return   # ignore click on locked panel
        self.panelRequested.emit(key)

    def lock_item(self, key):
        """Locks a panel: you cannot navigate to it from the sidebar."""
        self._states[key] = "locked"
        if key in self._buttons:
            btn = self._buttons[key]
            btn.setProperty("state", "locked")
            btn.setCursor(QtCore.Qt.ForbiddenCursor)
            refresh_style(btn)

    def unlock_item(self, key):
        """Unlock a panel allowing navigation."""
        # Drive the reset from _states (the source of truth) rather than the
        # button's visual property: set_active() may have repainted a locked
        # button as "pending" without touching its cursor, so checking the
        # property would skip the cursor reset and leave the lock icon stuck.
        if self._states.get(key) == "locked":
            self._states[key] = "pending"
            if key in self._buttons:
                btn = self._buttons[key]
                btn.setProperty("state", "pending")
                btn.setCursor(QtCore.Qt.PointingHandCursor)
                refresh_style(btn)

    def set_active(self, key):
        for k, btn in self._buttons.items():
            # Never override a locked item: keep its locked look and cursor so
            # navigating elsewhere doesn't desync _states from the button.
            if self._states.get(k) == "locked":
                btn.setProperty("state", "locked")
                btn.setCursor(QtCore.Qt.ForbiddenCursor)
                refresh_style(btn)
                continue
            if k == key:
                btn.setProperty("state", "active")
            elif self._states[k] == "done":
                btn.setProperty("state", "done")
            else:
                btn.setProperty("state", "pending")
            refresh_style(btn)

    def mark_done(self, key):
        self._states[key] = "done"
        btn = self._buttons[key]
        if btn.property("state") != "active":
            btn.setProperty("state", "done")
            refresh_style(btn)
        btn.setCursor(QtCore.Qt.PointingHandCursor)

    def show_item(self, key):
        if key in self._buttons:
            self._buttons[key].setVisible(True)

    def hide_item(self, key):
        if key in self._buttons:
            self._buttons[key].setVisible(False)


# ═══════════════════════════════════════════════════════════════════════════
# ABOUT DIALOG
# ═══════════════════════════════════════════════════════════════════════════

class AboutDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About ONTbarcoder")
        self.setFixedSize(480, 540)
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowContextHelpButtonHint)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(36, 32, 36, 24)
        layout.setSpacing(0)

        # --- Title ---
        name_lbl = make_label("ONTbarcoder", size=26, bold=True)
        name_lbl.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(name_lbl)

        layout.addSpacing(4)

        ver_lbl = make_label("Version 3.1b", size=16, color=TEXT_SEC)
        ver_lbl.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(ver_lbl)

        layout.addSpacing(18)

        # --- Description ---
        desc_lbl = make_label(
            "Desktop tool for demultiplexing and analysis\n"
            "of Oxford Nanopore Technology (ONT) reads\n"
            "using DNA barcodes.",
            size=14,
        )
        desc_lbl.setAlignment(QtCore.Qt.AlignCenter)
        desc_lbl.setWordWrap(True)
        layout.addWidget(desc_lbl)

        layout.addSpacing(22)

        # --- Separator ---
        line1 = QtWidgets.QFrame()
        line1.setFrameShape(QtWidgets.QFrame.HLine)
        line1.setFrameShadow(QtWidgets.QFrame.Sunken) 
        line1.setFixedHeight(1)                        
        line1.setStyleSheet("background-color: #C5C3C3;")
        layout.addWidget(line1)

        layout.addSpacing(14)

        # --- Original authors ---
        orig_title = make_label("Original Software", size=15, bold=True)
        orig_title.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(orig_title)

        layout.addSpacing(4)

        orig_author = make_label(
            "Amrita Srivathsan, V. Feng, D. Suárez,\nB. Emerson & R. Meier",
            size=14, color=TEXT_SEC
        )
        orig_author.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(orig_author)

        layout.addSpacing(10)

        # --- Citation ---
        cite_lbl = QtWidgets.QLabel(
            f'<span style="font-size:13px; color:{TEXT_HINT};">'
            "Srivathsan et al. (2024). ONTbarcoder 2.0: rapid species discovery<br>"
            "and identification with real-time barcoding facilitated by Oxford<br>"
            "Nanopore R10.4. Cladistics, 40: 192–203.<br>"
            f'<a href="https://doi.org/10.1111/cla.12566" style="color:{TEXT_HINT};">'
            "https://doi.org/10.1111/cla.12566</a></span>"
        )
        cite_lbl.setTextFormat(QtCore.Qt.RichText)
        cite_lbl.setOpenExternalLinks(True)
        cite_lbl.setAlignment(QtCore.Qt.AlignCenter)
        cite_lbl.setWordWrap(True)
        layout.addWidget(cite_lbl)

        layout.addSpacing(14)

        # --- Separator ---
        line2 = QtWidgets.QFrame()
        line2.setFrameShape(QtWidgets.QFrame.HLine)
        line2.setFrameShadow(QtWidgets.QFrame.Sunken)
        line2.setFixedHeight(1)                        
        line2.setStyleSheet("background-color: #C5C3C3;")
        layout.addWidget(line2)

        layout.addSpacing(14)

        # --- Version 3 modifications ---
        mod_title = make_label("Version 3 Development", size=15, bold=True)
        mod_title.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(mod_title)

        layout.addSpacing(4)

        mod_author = make_label("Eduardo Tovar Luque", size=14, color=TEXT_SEC)
        mod_author.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(mod_author)

        layout.addSpacing(2)

        mod_email = make_label("edutovar@humboldt.org.co", size=13, color=TEXT_HINT)
        mod_email.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(mod_email)

        layout.addSpacing(12)

        # --- Copyright ---
        year_lbl = make_label("2026 Instituto Humboldt", size=13, color=TEXT_HINT)
        year_lbl.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(year_lbl)

        layout.addStretch()

        # --- Close button ---
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.setObjectName("primary_btn")
        close_btn.setFixedWidth(120)
        close_btn.setFixedHeight(34)
        close_btn.clicked.connect(self.accept)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)


# ═══════════════════════════════════════════════════════════════════════════
# TOPBAR
# ═══════════════════════════════════════════════════════════════════════════

class TopBar(QtWidgets.QWidget):
    languageChanged = QtCore.pyqtSignal(str)
    aboutRequested  = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("topbar")
        self.setFixedHeight(48)
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)

        logo = QtWidgets.QLabel("ONTbarcoder")
        logo.setObjectName("topbar_logo")
        badge = QtWidgets.QLabel("v3.1b")
        badge.setObjectName("topbar_badge")

        layout.addWidget(logo)
        layout.addSpacing(8)
        layout.addSpacing(20)
        layout.addWidget(badge)
        layout.addStretch()

        _lang_btn_style = """
            QPushButton {{
                color: {fg};
                background-color: {bg};
                border: 1px solid {WHITE};
                border-radius: 6px;
                padding: 3px 10px;
                font-size: 13px;
                font-weight: {fw};
                min-width: 32px;
            }}
            QPushButton:hover {{
                background-color: {GRAY_CARD};
                color: {BLUE};
                border-color: {BLUE};
            }}
        """

#        self._btn_es = QtWidgets.QPushButton("ES")
#        self._btn_es.setFixedHeight(26)
#        self._btn_es.setObjectName("lang_btn_es")
#        self._btn_es.clicked.connect(lambda: self._set_lang("es"))

#        self._btn_en = QtWidgets.QPushButton("EN")
#        self._btn_en.setFixedHeight(26)
#        self._btn_en.setObjectName("lang_btn_en")
#        self._btn_en.clicked.connect(lambda: self._set_lang("en"))

 #       layout.addSpacing(8)
 #       layout.addWidget(self._btn_es)
 #       layout.addWidget(self._btn_en)
 #       layout.addSpacing(8)

 #       self._update_lang_buttons()

        self._docs_btn = QtWidgets.QPushButton(_tr("TopBar", "Documentation"))
        self._docs_btn.setObjectName("secondary_btn")
        self._docs_btn.setFixedHeight(28)
        self._docs_btn.setStyleSheet(f"""
            QPushButton {{
                color: {WHITE};
                background-color: transparent;
                border: 1px solid {WHITE};
                border-radius: 8px;
                padding: 7px 16px;
                font-size: 15px;
            }}
            QPushButton:hover {{
                background-color: {GRAY_CARD};
                color: {BLUE};
                border-color: {BLUE};
            }}
            QPushButton:pressed {{
                background-color: {BLUE_MID};
                color: white;
            }}
        """)
        self._docs_btn.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl("https://github.com/asrivathsan/ONTbarcoder")
            )
        )
        layout.addWidget(self._docs_btn)
        layout.addSpacing(8)

        self._about_btn = QtWidgets.QPushButton("About")
        self._about_btn.setObjectName("secondary_btn")
        self._about_btn.setFixedHeight(28)
        self._about_btn.setStyleSheet(f"""
            QPushButton {{
                color: {WHITE};
                background-color: transparent;
                border: 1px solid {WHITE};
                border-radius: 8px;
                padding: 7px 16px;
                font-size: 15px;
            }}
            QPushButton:hover {{
                background-color: {GRAY_CARD};
                color: {BLUE};
                border-color: {BLUE};
            }}
            QPushButton:pressed {{
                background-color: {BLUE_MID};
                color: white;
            }}
        """)
        self._about_btn.clicked.connect(self.aboutRequested)
        layout.addWidget(self._about_btn)

    def _set_lang(self, lang):
        set_language(lang)
        _update_topbar = self
        self._update_lang_buttons()
        self.languageChanged.emit(lang)

    def _update_lang_buttons(self):
        if not hasattr(self, '_btn_es') or not hasattr(self, '_btn_en'):
            return
        active = _CURRENT_LANG[0]
        active_style = f"""
            QPushButton {{
                color: {BLUE};
                background-color: {WHITE};
                border: 2px solid {BLUE};
                border-radius: 6px;
                padding: 3px 10px;
                font-size: 13px;
                font-weight: bold;
                min-width: 32px;
            }}
        """
        inactive_style = f"""
            QPushButton {{
                color: {WHITE};
                background-color: transparent;
                border: 1px solid {WHITE};
                border-radius: 6px;
                padding: 3px 10px;
                font-size: 13px;
                min-width: 32px;
            }}
            QPushButton:hover {{
                background-color: {GRAY_CARD};
                color: {BLUE};
                border-color: {BLUE};
            }}
        """
        self._btn_es.setStyleSheet(active_style if active == "es" else inactive_style)
        self._btn_en.setStyleSheet(active_style if active == "en" else inactive_style)

    def retranslateUi(self):
        self._docs_btn.setText(_tr("TopBar", "Documentation"))

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)


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
# PANEL 1: SETUP
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
            urls = e.mimeData().urls()
            valid = True
            if urls and self._extensions:
                path = urls[0].toLocalFile()
                valid = any(path.lower().endswith(ext) for ext in self._extensions)
            self.setProperty("dragging", "true" if valid else "invalid")
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


class PathDropLineEdit(QtWidgets.QLineEdit):
    """QLineEdit that accepts a dragged-and-dropped path, filling it in.

    mode="dir"  (default): a dropped folder fills its path; a dropped file fills
                its containing folder (so any .fastq/.pod5 can be dragged in).
    mode="file": a dropped file fills its full path; folders are ignored
                (e.g. for the Dorado executable field)."""

    _HL_SS = (
        f"QLineEdit {{ border: 2px solid {BLUE}; border-radius: 4px;"
        f" background: {BLUE_LIGHT}; }}"
    )

    def __init__(self, *args, mode="dir", **kwargs):
        super().__init__(*args, **kwargs)
        self._drop_mode = mode
        self._base_ss = ""
        self.setAcceptDrops(True)

    def _set_highlight(self, on):
        if on:
            self._base_ss = self.styleSheet()
            self.setStyleSheet(self._base_ss + self._HL_SS)
        else:
            self.setStyleSheet(self._base_ss)

    def _resolve(self, e):
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            if not path:
                continue
            if self._drop_mode == "file":
                if os.path.isfile(path):
                    return path
            else:
                if os.path.isdir(path):
                    return path
                if os.path.isfile(path):
                    return os.path.dirname(path)
        return ""

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and self._resolve(e):
            self._set_highlight(True)
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls() and self._resolve(e):
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dragLeaveEvent(self, e):
        self._set_highlight(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._set_highlight(False)
        path = self._resolve(e)
        if path:
            self.setText(path)
            e.acceptProposedAction()
        else:
            super().dropEvent(e)


class ModeCard(QtWidgets.QFrame):
    clicked = QtCore.pyqtSignal()

    def __init__(self, title, description, icon_char="⬡", parent=None):
        super().__init__(parent)
        self.setObjectName("mode_card")
        self.setProperty("selected", "false")
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFixedHeight(164)
        self._src_title = title
        self._src_desc = description

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(16, 10, 16, 16)
        main_layout.setSpacing(8)

        header_layout = QtWidgets.QHBoxLayout()
        header_layout.setSpacing(12)
        header_layout.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)

        icon_lbl = make_label(icon_char, size=36)
        self._title_lbl = make_label(title, size=22, bold=True)

        header_layout.addWidget(icon_lbl)
        header_layout.addWidget(self._title_lbl)
        header_layout.addStretch()

        self._desc_lbl = make_label(description, size=18, color=TEXT_SEC)
        self._desc_lbl.setWordWrap(True)

        main_layout.addLayout(header_layout)
        main_layout.addWidget(self._desc_lbl)
        main_layout.addStretch()

    def retranslateUi(self):
        ctx = "ModeCard"
        self._title_lbl.setText(_tr(ctx, self._src_title))
        self._desc_lbl.setText(_tr(ctx, self._src_desc))

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def select(self, state: bool):
        self.setProperty("selected", "true" if state else "false")
        refresh_style(self)


class SetupPanel(QtWidgets.QWidget):
    readyToContinue = QtCore.pyqtSignal(str, str, str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pipeline = "conventional"

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Fixed header: title + mode cards ──────────────────
        fixed_header = QtWidgets.QWidget()
        fixed_header.setObjectName("setup_fixed_header")
        fixed_header.setStyleSheet(
            f"QWidget#setup_fixed_header {{ background:{GRAY_CARD}; border-bottom:1px solid {GRAY_LINE}; }}"
        )
        fh_layout = QtWidgets.QVBoxLayout(fixed_header)
        fh_layout.setContentsMargins(20, 16, 20, 12)
        fh_layout.setSpacing(10)

        self._lbl_mode_title = make_label("Analysis mode", size=19, bold=True)
        self._lbl_mode_desc = make_label(
            "Select if you have the complete files or if the sequencer is running now.",
            color=TEXT_SEC,
        )
        fh_layout.addWidget(self._lbl_mode_title)
        fh_layout.addWidget(self._lbl_mode_desc)

        pipe_row = QtWidgets.QWidget()
        pipe_layout = QtWidgets.QHBoxLayout(pipe_row)
        pipe_layout.setContentsMargins(0, 0, 0, 0)
        pipe_layout.setSpacing(12)

        self._conv_card = ModeCard(
            "Conventional",
            "A single complete FASTQ file (sequencing completed).",
            "▶",
        )
        self._live_card = ModeCard(
            "Real-Time",
            "FASTQ files being generated (sequencing in progress).",
            "⚡",
        )
        self._conv_card.clicked.connect(lambda: self._select_pipeline("conventional"))
        self._live_card.clicked.connect(lambda: self._select_pipeline("realtime"))
        self._conv_card.select(True)

        pipe_layout.addWidget(self._conv_card)
        pipe_layout.addWidget(self._live_card)
        fh_layout.addWidget(pipe_row)
        outer.addWidget(fixed_header)

        # ── Zona scrollable: contenido dinámico ─────────────────────────
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._inner = QtWidgets.QWidget()
        self._layout = QtWidgets.QVBoxLayout(self._inner)
        self._layout.setContentsMargins(20, 16, 20, 8)
        self._layout.setSpacing(16)
        scroll.setWidget(self._inner)
        outer.addWidget(scroll, 1)

        # Helper methods to match BasePanel interface used below
        def _add(w, stretch=0): self._layout.addWidget(w, stretch)
        def _add_stretch(): self._layout.addStretch()
        self.add = _add
        self.add_stretch = _add_stretch

        self._conv_block = QtWidgets.QWidget()
        conv_layout = QtWidgets.QVBoxLayout(self._conv_block)
        conv_layout.setContentsMargins(0, 0, 0, 0)
        conv_layout.setSpacing(12)

        self._lbl_conv_section = make_section_label("Input files — conventional mode")
        conv_layout.addWidget(self._lbl_conv_section)

        self._drop_fastq = DropZone(
            "Drag FASTQ file here",
            "Format: .fastq",
            extensions=[".fastq"],
        )
        self._drop_dem = DropZone(
            "Drag demultiplexing file here (.csv)",
            "sample, tag_f, tag_r, primer_f, primer_r [, primer_f2, primer_r2, ...]",
            extensions=[".csv", ".txt"],
        )
        self._drop_dem.setToolTip(
            "CSV format — one row per sample:\n"
            "  sample, tag_f, tag_r, primer_f, primer_r [, primer_f2, primer_r2, ...]\n\n"
            "Multiple primer pairs per sample are supported.\n"
            "Add primer_f2 / primer_r2, primer_f3 / primer_r3, etc. as extra columns."
        )
        conv_layout.addWidget(self._drop_fastq)
        conv_layout.addWidget(self._drop_dem)
        self.add(self._conv_block)

        self._live_block = QtWidgets.QWidget()
        live_layout = QtWidgets.QVBoxLayout(self._live_block)
        live_layout.setContentsMargins(0, 0, 0, 0)
        live_layout.setSpacing(16)

        self._lbl_live_section = make_section_label("Configuration — real time mode")
        live_layout.addWidget(self._lbl_live_section)

        # Step 1: CSV
        self._drop_dem_live = DropZone(
            "Drag demultiplexing file here (.csv)",
            "Same format as in conventional mode",
            extensions=[".csv", ".txt"],
        )
        self._drop_dem_live.setToolTip(
            "CSV format — one row per sample:\n"
            "  sample, tag_f, tag_r, primer_f, primer_r [, primer_f2, primer_r2, ...]\n\n"
            "Multiple primer pairs per sample are supported.\n"
            "Add primer_f2 / primer_r2, primer_f3 / primer_r3, etc. as extra columns."
        )
        self._drop_dem_live.fileDropped.connect(self._on_live_csv_loaded)
        live_layout.addWidget(self._drop_dem_live)

        # Basecalling block — always visible in RT mode (no device passthrough)
        _dev_style = (
            f"QPushButton {{ background-color: transparent; color: {TEXT_SEC}; "
            f"border: 1px solid {GRAY_LINE}; border-radius: 8px; padding: 7px 16px; font-size:18px; }}"
            f"QPushButton:hover {{ "
            f"border-color: {BLUE_MID}; "
            f"}}"
            f"QPushButton:checked {{ background-color: {BLUE_LIGHT}; "
            f"border: 1.5px solid {BLUE_MID}; color: {BLUE}; font-weight:500; }}"
        )

        self._live_bc_mode_block = QtWidgets.QWidget()
        # Visible directly when entering RT mode
        bc_outer = QtWidgets.QVBoxLayout(self._live_bc_mode_block)
        bc_outer.setContentsMargins(0, 0, 0, 0)
        bc_outer.setSpacing(8)
        self._lbl_bc_section = make_section_label("Basecalling mode")
        bc_outer.addWidget(self._lbl_bc_section)

        bc_mode_card = QtWidgets.QFrame()
        bc_mode_card.setObjectName("card")
        bc_mode_layout = QtWidgets.QVBoxLayout(bc_mode_card)
        bc_mode_layout.setSpacing(12)
        self._lbl_bc_question = make_label(
            "How are FASTQ files being generated?", size=17, color=TEXT_SEC
        )
        bc_mode_layout.addWidget(self._lbl_bc_question)

        bc_btn_row = QtWidgets.QWidget()
        bc_btn_layout = QtWidgets.QHBoxLayout(bc_btn_row)
        bc_btn_layout.setContentsMargins(0, 0, 0, 0)
        bc_btn_layout.setSpacing(10)

        self._bc_minkow_btn = QtWidgets.QPushButton("MinKNOW performing basecalling")
        self._bc_minkow_btn_src = "MinKNOW realizando el basecalling"
        self._bc_dorado_btn = QtWidgets.QPushButton("Use Dorado for basecalling")
        self._bc_dorado_btn_src = "Use Dorado for basecalling"
        self._bc_group = QtWidgets.QButtonGroup(self)
        self._bc_group.setExclusive(True)

        for btn in (self._bc_minkow_btn, self._bc_dorado_btn):
            btn.setCheckable(True)
            btn.setFixedHeight(42)
            btn.setProperty("class", "basecalling-btn")
            btn.setStyleSheet(_dev_style)
            self._bc_group.addButton(btn)
            bc_btn_layout.addWidget(btn)

        self._bc_minkow_btn.toggled.connect(lambda chk: self._on_bc_mode_selected("minkow", chk))
        self._bc_dorado_btn.toggled.connect(lambda chk: self._on_bc_mode_selected("dorado", chk))

        bc_mode_layout.addWidget(bc_btn_row)

        # Sub-block A: FASTQs folder (MinKNOW)
        self._live_fastq_dir_block = QtWidgets.QWidget()
        self._live_fastq_dir_block.hide()
        fq_layout = QtWidgets.QVBoxLayout(self._live_fastq_dir_block)
        fq_layout.setContentsMargins(0, 8, 0, 0)
        fq_layout.setSpacing(6)
        self._lbl_fastq_dir = make_label(
            "Output folder for .fastq files in MinKNOW:", size=17, color=TEXT_SEC
        )
        fq_layout.addWidget(self._lbl_fastq_dir)
        fq_dir_row = QtWidgets.QWidget()
        fq_dir_layout = QtWidgets.QHBoxLayout(fq_dir_row)
        fq_dir_layout.setContentsMargins(0, 0, 0, 0)
        fq_dir_layout.setSpacing(8)
        self._live_fastq_dir_edit = PathDropLineEdit()
        self._live_fastq_dir_edit.setPlaceholderText("Path to MinKNOW output folder… (or drag & drop folder)")
        self._fq_browse_btn = QtWidgets.QPushButton("Browse…")
        self._fq_browse_btn.setObjectName("secondary_btn")
        self._fq_browse_btn.setFixedWidth(120)
        self._fq_browse_btn.clicked.connect(self._browse_fastq_dir)
        fq_dir_layout.addWidget(self._live_fastq_dir_edit)
        fq_dir_layout.addWidget(self._fq_browse_btn)
        fq_layout.addWidget(fq_dir_row)
        bc_mode_layout.addWidget(self._live_fastq_dir_block)

        # Sub-block B: Dorado parameters
        self._live_dorado_block = QtWidgets.QWidget()
        self._live_dorado_block.hide()
        dorado_layout = QtWidgets.QVBoxLayout(self._live_dorado_block)
        dorado_layout.setContentsMargins(0, 8, 0, 0)
        dorado_layout.setSpacing(8)

        self._lbl_dorado_exe = make_label("Dorado executable:", size=17, color=TEXT_SEC)
        dorado_layout.addWidget(self._lbl_dorado_exe)
        dorado_exe_row = QtWidgets.QWidget()
        dorado_exe_layout = QtWidgets.QHBoxLayout(dorado_exe_row)
        dorado_exe_layout.setContentsMargins(0, 0, 0, 0)
        dorado_exe_layout.setSpacing(8)
        self._dorado_exe_edit = PathDropLineEdit(mode="file")
        self._dorado_exe_edit.setPlaceholderText("Path to dorado.exe / dorado… (or drag & drop executable)")
        self._dorado_exe_browse = QtWidgets.QPushButton("Browse…")
        self._dorado_exe_browse.setObjectName("secondary_btn")
        self._dorado_exe_browse.setFixedWidth(120)
        self._dorado_exe_browse.clicked.connect(self._browse_dorado_exe)
        dorado_exe_layout.addWidget(self._dorado_exe_edit)
        dorado_exe_layout.addWidget(self._dorado_exe_browse)
        dorado_layout.addWidget(dorado_exe_row)

        self._lbl_dorado_model = make_label("Dorado model:", size=17, color=TEXT_SEC)
        dorado_layout.addWidget(self._lbl_dorado_model)
        self._dorado_model_combo = QtWidgets.QComboBox()
        self._dorado_model_combo.setMinimumHeight(SPINBOX_MIN_HEIGHT)
        self._dorado_model_combo.addItems(["sup@latest", "hac@latest", "fast@latest"])
        self._dorado_model_combo.setFixedWidth(200)
        dorado_layout.addWidget(self._dorado_model_combo)

        self._lbl_dorado_indir = make_label(
            "Input folder (POD5/FAST5 files):", size=17, color=TEXT_SEC
        )
        dorado_layout.addWidget(self._lbl_dorado_indir)
        dorado_indir_row = QtWidgets.QWidget()
        dorado_indir_layout = QtWidgets.QHBoxLayout(dorado_indir_row)
        dorado_indir_layout.setContentsMargins(0, 0, 0, 0)
        dorado_indir_layout.setSpacing(8)
        self._dorado_indir_edit = PathDropLineEdit()
        self._dorado_indir_edit.setPlaceholderText("Folder where POD5/FAST5 files are saved… (or drag & drop folder)")
        self._dorado_indir_browse = QtWidgets.QPushButton("Browse…")
        self._dorado_indir_browse.setObjectName("secondary_btn")
        self._dorado_indir_browse.setFixedWidth(120)
        self._dorado_indir_browse.clicked.connect(self._browse_dorado_indir)
        dorado_indir_layout.addWidget(self._dorado_indir_edit)
        dorado_indir_layout.addWidget(self._dorado_indir_browse)
        dorado_layout.addWidget(dorado_indir_row)

        self._lbl_dorado_cmd = make_label("Generated command:", size=16, color=TEXT_HINT)
        dorado_layout.addWidget(self._lbl_dorado_cmd)
        self._dorado_cmd_preview = QtWidgets.QLineEdit()
        self._dorado_cmd_preview.setReadOnly(True)
        self._dorado_cmd_preview.setStyleSheet(
            f"color:{TEXT_HINT}; background:{GRAY_BG}; font-size:15px; font-family:monospace;"
        )
        dorado_layout.addWidget(self._dorado_cmd_preview)

        self._dorado_exe_edit.textChanged.connect(self._update_dorado_preview)
        self._dorado_model_combo.currentIndexChanged.connect(self._update_dorado_preview)

        bc_mode_layout.addWidget(self._live_dorado_block)
        bc_outer.addWidget(bc_mode_card)
        live_layout.addWidget(self._live_bc_mode_block)

        self._live_bc_mode = None

        self._live_block.hide()
        self.add(self._live_block)
        self.add_stretch()

        self._drop_fastq.fileDropped.connect(self._update_continue_btn)
        self._drop_dem.fileDropped.connect(self._on_csv_loaded)
        self._drop_dem_live.fileDropped.connect(self._on_csv_loaded)
        self._live_fastq_dir_edit.textChanged.connect(self._update_continue_btn)
        self._dorado_exe_edit.textChanged.connect(self._update_continue_btn)
        self._dorado_indir_edit.textChanged.connect(self._update_continue_btn)

        # — Footer with button anchored directly in the outer layout
        self._continue_btn = QtWidgets.QPushButton(_tr("SetupPanel", "Continue to parameters  →"))
        self._continue_btn.setObjectName("primary_btn")
        self._continue_btn.setFixedHeight(44)
        self._continue_btn.clicked.connect(self._on_continue)
        self._continue_btn.setEnabled(False)
        self._continue_btn.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )
        footer = QtWidgets.QWidget()
        footer.setObjectName("setup_footer")
        footer.setStyleSheet(
            f"QWidget#setup_footer {{ background:{GRAY_CARD}; border-top:1px solid {GRAY_CARD}; }}"
        )
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(20, 10, 20, 10)
        fl.addWidget(self._continue_btn)
        outer.addWidget(footer)   # ← directly on outer, always visible

        # Auto-select MinKNOW by default at the end, when all widgets already exist
        self._bc_minkow_btn.setChecked(True)

    def _update_continue_btn(self, _path=""):
        if self._pipeline == "conventional":
            ready = bool(self._drop_fastq.filepath and self._drop_dem.filepath)
        else:
            csv_ok = bool(self._drop_dem_live.filepath)
            mode = self._live_bc_mode
            if mode == "minkow":
                ready = csv_ok and bool(self._live_fastq_dir_edit.text().strip())
            elif mode == "dorado":
                ready = (csv_ok
                         and bool(self._dorado_exe_edit.text().strip())
                         and bool(self._dorado_indir_edit.text().strip()))
            else:
                ready = False

        if ready:
            self._continue_btn.setEnabled(True)
            self._continue_btn.setStyleSheet(
                f"QPushButton {{ background-color: {BLUE}; color: white; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
                f"QPushButton:hover {{ background-color: #0C4A82; }}"
            )
        else:
            self._continue_btn.setEnabled(False)
            self._continue_btn.setStyleSheet(
                f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
            )

    def _on_csv_loaded(self, path: str):
        """Count rows and pairs of CSV primers and update the DropZone hint."""
        self._update_continue_btn(path)
        ctx = "SetupPanel"
        if not path:
            self._drop_dem._hint.setText(_tr(ctx, "sample, tag_f, tag_r, primer_f, primer_r [, primer_f2, primer_r2, ...]"))
            self._drop_dem_live._hint.setText(_tr(ctx, "Same format as in conventional mode"))
            return
        try:
            n_samples = 0
            n_primer_pairs = 0
            n_gencode = 0          # named rows carrying a per-sample genetic code
            name_counts = Counter()  # to detect duplicate sample names
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                first_row_done = False
                for row in reader:
                    cells = [c.strip() for c in row]
                    if not any(cells):
                        continue
                    if not cells[0]:
                        continue
                    n_samples += 1
                    name_counts[cells[0]] += 1
                    # Trailing integer column (NCBI table 0–33) = genetic code; a
                    # primer is an IUPAC string and never a plain number.
                    last = cells[-1] if len(cells) >= 6 else ""
                    has_code = last.isdigit() and 0 <= int(last) <= 33
                    if has_code:
                        n_gencode += 1
                    if not first_row_done:
                        # Drop the genetic-code column before counting primer pairs.
                        ncols = len(cells) - (1 if has_code else 0)
                        n_primer_pairs = max(0, (ncols - 3) // 2)
                        first_row_done = True
            sample_word = _tr(ctx, "sample") if n_samples == 1 else _tr(ctx, "samples")
            pair_word = _tr(ctx, "primer pair") if n_primer_pairs == 1 else _tr(ctx, "primer pairs")
            hint_text = f"{n_samples} {sample_word}  ·  {n_primer_pairs} {pair_word}"
            if n_gencode == n_samples and n_samples > 0:
                hint_text += "  ·  " + _tr(ctx, "per-sample genetic code")
            elif n_gencode > 0:
                # Partial: per-sample mode requires a code on every row (strict).
                hint_text += ("  ·  ⚠ " + _tr(ctx, "genetic code on {0}/{1} samples")
                              .format(n_gencode, n_samples))
            _n_dup = sum(1 for c in name_counts.values() if c > 1)
            if _n_dup:
                hint_text += ("  ·  ⚠ " + _tr(ctx, "{0} duplicate name(s)")
                              .format(_n_dup))
            self._drop_dem._hint.setText(hint_text)
            self._drop_dem_live._hint.setText(hint_text)
        except Exception:
            pass
        if path and self._pipeline != "conventional":
            self._on_live_csv_loaded(path)

    def _on_live_csv_loaded(self, path: str):
        self._update_continue_btn(path)

    def _on_bc_mode_selected(self, key: str, checked: bool):
        if not checked:
            return
        self._live_bc_mode = key
        if key == "minkow":
            self._live_dorado_block.hide()
            self._live_fastq_dir_block.show()
        else:
            self._live_fastq_dir_block.hide()
            self._live_dorado_block.show()
            self._update_dorado_preview()
        self._update_continue_btn()

    def retranslateUi(self):
        ctx = "SetupPanel"
        self._lbl_mode_title.setText(_tr(ctx, "Analysis mode"))
        self._lbl_mode_desc.setText(_tr(ctx, "Select if you have the complete files or if the sequencer is running now."))
        self._lbl_conv_section.setText(("  " + _tr(ctx, "Input files — conventional mode")).upper())
        self._lbl_live_section.setText(("  " + _tr(ctx, "Configuration — real time mode")).upper())
        self._lbl_bc_section.setText(("  " + _tr(ctx, "Basecalling mode")).upper())
        self._lbl_bc_question.setText(_tr(ctx, "How are FASTQ files being generated?"))
        self._bc_minkow_btn.setText(_tr(ctx, self._bc_minkow_btn_src))
        self._bc_dorado_btn.setText(_tr(ctx, self._bc_dorado_btn_src))
        self._lbl_fastq_dir.setText(_tr(ctx, "Output folder for .fastq files in MinKNOW:"))
        self._live_fastq_dir_edit.setPlaceholderText(_tr(ctx, "Path to MinKNOW output folder…"))
        self._fq_browse_btn.setText(_tr(ctx, "Browse…"))
        self._lbl_dorado_exe.setText(_tr(ctx, "Dorado executable:"))
        self._dorado_exe_edit.setPlaceholderText(_tr(ctx, "Path to dorado.exe / dorado…"))
        self._dorado_exe_browse.setText(_tr(ctx, "Browse…"))
        self._lbl_dorado_model.setText(_tr(ctx, "Dorado model:"))
        self._lbl_dorado_indir.setText(_tr(ctx, "Input folder (POD5/FAST5 files):"))
        self._dorado_indir_edit.setPlaceholderText(_tr(ctx, "Folder where POD5/FAST5 files are saved…"))
        self._dorado_indir_browse.setText(_tr(ctx, "Browse…"))
        self._lbl_dorado_cmd.setText(_tr(ctx, "Generated command:"))
        self._continue_btn.setText(_tr(ctx, "Continue to parameters  →"))
        self._conv_card.retranslateUi()
        self._live_card.retranslateUi()
        self._drop_fastq.retranslateUi()
        self._drop_dem.retranslateUi()
        self._drop_dem_live.retranslateUi()

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def _browse_fastq_dir(self):
        ctx = "SetupPanel"
        path = QtWidgets.QFileDialog.getExistingDirectory(self, _tr(ctx, "Select FASTQs folder"))
        if path:
            self._live_fastq_dir_edit.setText(path)

    def _browse_dorado_exe(self):
        ctx = "SetupPanel"
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, _tr(ctx, "Select Dorado executable"), "",
            "Executable (dorado dorado.exe *);;All (*)"
        )
        if path:
            self._dorado_exe_edit.setText(path)

    def _browse_dorado_indir(self):
        ctx = "SetupPanel"
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, _tr(ctx, "Select input folder (POD5/FAST5)")
        )
        if path:
            self._dorado_indir_edit.setText(path)

    def _update_dorado_preview(self):
        exe = self._dorado_exe_edit.text() or "<dorado>"
        model = self._dorado_model_combo.currentText()
        dev = "cuda:all"
        cmd = f'"{exe}" basecaller --emit-fastq --device "{dev}" --chunksize 10000 --overlap 500 "{model}" "ignoreinputfolder"'
        self._dorado_cmd_preview.setText(cmd)

    def _select_pipeline(self, mode: str):
        self._pipeline = mode
        self._conv_card.select(mode == "conventional")
        self._live_card.select(mode == "realtime")
        self._conv_block.setVisible(mode == "conventional")
        self._live_block.setVisible(mode == "realtime")
        if mode == "realtime":
            # Show basecalling block directly, without prior device step
            self._live_bc_mode_block.show()
            # If no mode is selected, preselect MinKNOW
            if not self._live_bc_mode:
                self._bc_minkow_btn.setChecked(True)
        self._update_continue_btn()

    def _on_continue(self):
        if self._pipeline == "conventional":
            fastq = self._drop_fastq.filepath
            dem = self._drop_dem.filepath
            if not fastq:
                self._show_error("Load the FASTQ file before continuing.")
                return
            if not dem:
                self._show_error("Load demultiplexing file (.csv).")
                return
            self.readyToContinue.emit("1", fastq, dem, {})
        else:
            dem = self._drop_dem_live.filepath
            if not dem:
                self._show_error("Load demultiplexing file (.csv).")
                return
            # Read the mode directly from the button's visual state (more reliable than _live_bc_mode)
            if self._bc_minkow_btn.isChecked():
                self._live_bc_mode = "minkow"
            elif self._bc_dorado_btn.isChecked():
                self._live_bc_mode = "dorado"
            else:
                # None checked: force MinKNOW as default
                self._bc_minkow_btn.setChecked(True)
                self._live_bc_mode = "minkow"
            rt_extra = {"bc_mode": self._live_bc_mode}
            if self._live_bc_mode == "minkow":
                fastq_dir = self._live_fastq_dir_edit.text().strip()
                if not fastq_dir or not os.path.isdir(fastq_dir):
                    self._show_error("Indicate the folder where MinKNOW saves the files .fastq.")
                    return
                rt_extra["fastq_dir"] = fastq_dir
                runmode = "2"
            else:
                dorado_exe = self._dorado_exe_edit.text().strip()
                dorado_model = self._dorado_model_combo.currentText()
                dorado_indir = self._dorado_indir_edit.text().strip()
                if not dorado_exe:
                    self._show_error("Indicate the path to the Dorado executable.")
                    return
                if not dorado_model:
                    self._show_error("Indicate the path to the Dorado model folder.")
                    return
                if not dorado_indir or not os.path.isdir(dorado_indir):
                    self._show_error("Indicate the input folder with POD5/FAST5 files.")
                    return
                rt_extra.update({
                    "dorado_exe": dorado_exe,
                    "dorado_model": dorado_model,
                    "dorado_indir": dorado_indir,
                })
                runmode = "3"
            self.readyToContinue.emit(runmode, "", dem, rt_extra)

    def _show_error(self, msg):
        dlg = QtWidgets.QMessageBox(self)
        dlg.setWindowTitle(_tr("SetupPanel", "Missing data"))
        dlg.setText(msg)
        dlg.setIcon(QtWidgets.QMessageBox.Warning)
        dlg.setStyleSheet(f"QMessageBox {{ background-color: {GRAY_CARD}; }} QLabel {{ color: {TEXT_PRI}; }}")
        dlg.exec_()

    def full_reset(self):
        self._select_pipeline("conventional")
        self._drop_fastq.clear()
        self._drop_dem.clear()
        self._drop_dem_live.clear()
        self._live_bc_mode_block.hide()
        self._live_fastq_dir_block.hide()
        self._live_dorado_block.hide()
        self._live_bc_mode = None
        # Uncheck both so that MinKNOW is re-selected cleanly when you return to RT.
        # (the block is hidden so toggled does not cause incorrect visual effects)
        # setExclusive(False) is required: Qt silently ignores setChecked(False) on the
        # only-checked button in an exclusive group, leaving it checked=True and causing
        # toggled() not to fire when setChecked(True) is called later in _select_pipeline.
        self._bc_group.setExclusive(False)
        self._bc_minkow_btn.blockSignals(True)
        self._bc_dorado_btn.blockSignals(True)
        self._bc_minkow_btn.setChecked(False)
        self._bc_dorado_btn.setChecked(False)
        self._bc_minkow_btn.blockSignals(False)
        self._bc_dorado_btn.blockSignals(False)
        self._bc_group.setExclusive(True)
        self._live_fastq_dir_edit.clear()
        self._dorado_exe_edit.clear()
        self._dorado_model_combo.setCurrentIndex(0)
        self._dorado_indir_edit.clear()
        self._update_continue_btn()

    @property
    def pipeline(self):
        return self._pipeline


# ═══════════════════════════════════════════════════════════════════════════
# PANEL 2: PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════

# Uniform height for spin boxes and combo boxes (matches the 18px input font +
# padding from the global stylesheet). Set at the widget level so layouts reserve
# the space and never clip the value.
SPINBOX_MIN_HEIGHT = 36


def _spin(min_v, max_v, default, step=1, decimals=0):
    if decimals > 0:
        w = QtWidgets.QDoubleSpinBox()
        w.setDecimals(decimals)
        w.setSingleStep(step)
    else:
        w = QtWidgets.QSpinBox()
    w.setRange(min_v, max_v)
    w.setValue(default)
    # Real (widget-level) minimum so the layout reserves the height and never
    # clips the 18px value when neighbouring widgets appear (e.g. the genetic
    # code notice). QSS min-height alone is cosmetic and the layout ignores it.
    w.setMinimumHeight(SPINBOX_MIN_HEIGHT)
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
    widget.setMaximumWidth(400)
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


class ParamsPanel(BasePanel):
    runRequested = QtCore.pyqtSignal(dict)

    GENETIC_CODES = [
        "1. Standard code",
        "2. Vertebrate [mitochondrial]",
        "3. Yeast [mitochondrial]",
        "4. Mold/protozoan [mitochondrial]",
        "5. Invertebrate [mitochondrial]",
        "6. Ciliate/Dasycladacean",
        "9. Echinoderm/flatworm",
        "10. Euplotid [nuclear]",
        "11. Bacteria/Archaebacteria/Plastid",
        "12. Alternative yeast",
        "13. Ascidian [mitochondrial]",
        "14. Alternative flatworm",
        "16. Chlorophycean [mitochondrial]",
        "21. Trematode [mitochondrial]",
        "22. Scenedesmus obliquus",
        "23. Thraustochytrium",
        "24. Rhabdopleuridae",
        "25. SR1 and Gracilibacteria",
        "26. Pachysolen tannophilus",
    ]

    # Single source of truth for every parameter default (keyed by the profile
    # JSON key). Used by _build_tab_* (widget creation), _reset_to_defaults and
    # _load_profile (fallback for keys missing from an external/old profile), so
    # the three can never diverge. n_threads is overridden per-instance in
    # __init__ with the physical-core count (a runtime value).
    DEFAULTS = {
        "non_coi":             False,
        "resolve_mixed_on":    False,
        "resolve_secfrac":     20,
        "resolve_variant_tol": 10,
        "gencode_index":       4,
        "minlen":              658,
        "explen":              658,
        "demlen":              100,
        "primersearchlen":     100,
        "primermismatch":      10,
        "tagmm":               2,
        "consfreqfixed":       0.3,
        "consrange":           "0.2, 0.5",
        "consstep":            0.05,
        "mincoverage":         5,
        "coveragelist":        "25, 50, 100, 200",
        "lendev":              50,
        "minq":                0,
        "coverage2b":          100,
        "n_threads":           4,   # overridden at runtime with physical cores
        "run_phase1":          True,
        "run_phase2a":         True,
        "run_phase2b":         True,
        "run_phase3":          True,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._i18n_labels = []

        # Per-instance copy of the defaults, with n_threads resolved to the real
        # physical-core count. Built before the tabs so _build_tab_* can read it.
        self._default_nthreads = _ont_mp.optimal_worker_count()
        self._defaults = dict(self.DEFAULTS)
        self._defaults["n_threads"] = self._default_nthreads

        self._lbl_params_title = make_label("Configure parameters", size=19, bold=True)
        self.add(self._lbl_params_title)
        self._mode_subtitle_src = "Default values ​​are suitable for COI 658 bp"
        self._mode_subtitle = make_label(
            _tr("ParamsPanel", self._mode_subtitle_src), color=TEXT_SEC
        )
        self.add(self._mode_subtitle)

        self._tabs = QtWidgets.QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.setStyleSheet("QTabBar::tab { min-width: 120px; }")
        self.add(self._tabs)

        self._build_tab_general()
        self._build_tab_demultiplex()
        self._build_tab_consensus()
        self._build_tab_steps()

        self._run_btn = QtWidgets.QPushButton(_tr("ParamsPanel", "Start analysis  →"))
        self._run_btn.setObjectName("primary_btn")
        self._run_btn.setFixedHeight(44)
        self._run_btn.setEnabled(False)
        self._run_btn.setToolTip(_tr("ParamsPanel", "Load input files first"))
        self._run_btn.clicked.connect(self._on_run)
        self._run_btn.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )
        # ── Parameter profiles (visible in all tabs) ────────
        _profiles_block = QtWidgets.QWidget()
        _pb_layout = QtWidgets.QVBoxLayout(_profiles_block)
        _pb_layout.setContentsMargins(10, 15, 10, 0)
        _pb_layout.setSpacing(12)
        _pb_layout.addWidget(hline())

        _profile_header_row = QtWidgets.QWidget()
        _ph_layout = QtWidgets.QHBoxLayout(_profile_header_row)
        _ph_layout.setContentsMargins(0, 0, 0, 0)
        _ph_layout.setSpacing(12)
        self._lbl_profiles_title = make_label("Parameter profiles", size=17, bold=True)
        self._lbl_profiles_desc = make_label(
            "Save or load all parameters as a reusable profile (.json).",
            size=17, color=TEXT_SEC)
        _ph_layout.addWidget(self._lbl_profiles_title)
        _ph_layout.addWidget(self._lbl_profiles_desc)
        _ph_layout.addStretch()
        _pb_layout.addWidget(_profile_header_row)

        _profile_btn_style = (
            f"QPushButton {{"
            f"  background-color: {BLUE_LIGHT}; color: {BLUE};"
            f"  border: 1px solid {BLUE_MID}; border-radius: 8px;"
            f"  padding: 6px 14px; font-size: 18px; font-weight: 500;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background-color: {BLUE_MID}; color: white; border-color: {BLUE};"
            f"}}"
            f"QPushButton:pressed {{ background-color: {BLUE}; color: white; }}"
        )
        _profile_row_w = QtWidgets.QWidget()
        _pr_layout = QtWidgets.QHBoxLayout(_profile_row_w)
        _pr_layout.setContentsMargins(0, 0, 0, 4)
        _pr_layout.setSpacing(10)

        self._btn_save_profile = QtWidgets.QPushButton("💾  Save profile")
        self._btn_save_profile.setFixedHeight(42)
        self._btn_save_profile.setStyleSheet(_profile_btn_style)
        self._btn_save_profile.setToolTip("Save all current parameters to a .json file")
        self._btn_save_profile.clicked.connect(self._save_profile)

        self._btn_load_profile = QtWidgets.QPushButton("📂  Load profile")
        self._btn_load_profile.setFixedHeight(42)
        self._btn_load_profile.setStyleSheet(_profile_btn_style)
        self._btn_load_profile.setToolTip("Load parameters from a previously saved .json file")
        self._btn_load_profile.clicked.connect(self._load_profile)

        _pr_layout.addWidget(self._btn_save_profile)
        _pr_layout.addWidget(self._btn_load_profile)
        _pr_layout.addStretch()

        _profile_reset_style = (
            f"QPushButton {{"
            f"  background-color: #FFF3E0; color: #E65100;"
            f"  border: 1px solid #FFCC80; border-radius: 8px;"
            f"  padding: 6px 14px; font-size: 18px; font-weight: 500;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background-color: #FFE0B2; color: #BF360C; border-color: #E65100;"
            f"}}"
            f"QPushButton:pressed {{ background-color: #E65100; color: white; }}"
        )

        
        self._btn_reset_profile = QtWidgets.QPushButton("↺  Default values")
        self._btn_reset_profile.setFixedHeight(42)
        self._btn_reset_profile.setStyleSheet(_profile_reset_style)
        self._btn_reset_profile.setToolTip("Resets all parameters to their initial default values")
        self._btn_reset_profile.clicked.connect(self._reset_to_defaults)
        _pr_layout.addWidget(self._btn_reset_profile)

        _pb_layout.addWidget(_profile_row_w)
        self.add(_profiles_block)

        # Button pinned to the bottom, outside the scroll
        footer = QtWidgets.QWidget()
        footer.setStyleSheet(f"background:{GRAY_CARD}; border-top:1px solid {GRAY_CARD};")
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(20, 10, 20, 10)
        fl.addWidget(self._run_btn)
        self._footer_widget = footer

    def configure_for_mode(self, runmode: str):
        is_live = runmode != "1"

        if is_live:
            self._mode_subtitle_src = "Coding: all phases (1, 2a, 2b and 3) are carried out in each cycle. Non-Coding: phases 1 and 2a each cycle."
            self._mode_subtitle.setText(_tr("ParamsPanel", self._mode_subtitle_src))
            self._mode_subtitle.setStyleSheet(
                f"font-size:17px; color:{BLUE}; background:{BLUE_LIGHT}; "
                f"border-radius:6px; padding:6px 10px;"
            )
        else:
            self._mode_subtitle_src = "Default values ​​are suitable for COI 658 bp"
            self._mode_subtitle.setText(_tr("ParamsPanel", self._mode_subtitle_src))
            self._mode_subtitle.setStyleSheet(f"font-size:17px; color:{TEXT_SEC}; padding:6px 10px;")

        for w in (self.p_minlen, self.p_explen, self.p_demlen,
                  self.p_searchlen, self.p_primermm, self.p_tagmm):
            w.setEnabled(True)

        self.p_covlist.setEnabled(True)
        self.p_covlist.setReadOnly(False)
        self.p_covlist.setToolTip("Iterative coverages for phase 2a (ej: 25, 50, 100, 200)")
        self.p_cov2b.setEnabled(True)
        self.p_cov2b.setToolTip("Coverage for phase 2b")

        for key, cb in self._step_checks.items():
            cb.setEnabled(True)
            cb.setToolTip("")
            if is_live and key == "phase1":
                cb.setChecked(True)
                cb.setEnabled(False)
                cb.setToolTip("Always active in real time mode")

        if not hasattr(self, "_live_consensus_block"):
            self._live_consensus_block = QtWidgets.QFrame()
            self._live_consensus_block.setObjectName("card")
            lc_layout = QtWidgets.QVBoxLayout(self._live_consensus_block)
            lc_layout.setSpacing(10)
            lc_layout.addWidget(make_label(
                "Provisional consensus frequency", size=17, bold=True
            ))
            lc_layout.addWidget(make_label(
                "Consensus updates when either of these conditions is met:",
                size=17, color=TEXT_SEC
            ))
            _fr = QtWidgets.QWidget()
            _fl = QtWidgets.QHBoxLayout(_fr)
            _fl.setContentsMargins(0, 0, 0, 0)
            _fl.setSpacing(12)
            self._freq_cb_reads = QtWidgets.QCheckBox("Every N reads")
            self._freq_cb_time = QtWidgets.QCheckBox("Every N minutes")
            self._freq_cb_reads.setChecked(True)
            self._freq_cb_time.setChecked(True)
            _fl.addWidget(self._freq_cb_reads)
            _fl.addWidget(self._freq_cb_time)
            _fl.addStretch()
            lc_layout.addWidget(_fr)

            self.p_live_reads = _spin(500, 100000, 5000)
            self.p_live_reads.setSingleStep(500)
            self.p_live_mins = _spin(1, 120, 10)
            self._freq_reads_row = _grid(_field("Reads per cycle", self.p_live_reads))
            self._freq_mins_row = _grid(_field("Minutes per cycle", self.p_live_mins))
            lc_layout.addWidget(self._freq_reads_row)
            lc_layout.addWidget(self._freq_mins_row)

            def _upd_freq():
                r = self._freq_cb_reads.isChecked()
                m = self._freq_cb_time.isChecked()
                if not r and not m:
                    self._freq_cb_reads.setChecked(True)
                    r = True
                self._freq_reads_row.setVisible(r)
                self._freq_mins_row.setVisible(m)
            self._freq_cb_reads.toggled.connect(lambda _: _upd_freq())
            self._freq_cb_time.toggled.connect(lambda _: _upd_freq())
            self._tabs.widget(3).layout().insertWidget(0, self._live_consensus_block)

        self._live_consensus_block.setVisible(is_live)

    def _tf(self, ctx, label_text, widget, tooltip=""):
        row = _field(label_text, widget, tooltip)
        lbl = row.layout().itemAt(0).widget()
        self._i18n_labels.append((lbl, ctx, label_text))
        return row

    def _build_tab_demultiplex(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        ctx = "ParamsPanel"
        # Min. allowed value is 0 so the fields can be left "unset" (0) — used in
        # non-Coding mode, where the barcode length is marker-specific and must be
        # entered by the user instead of inheriting the COI default (658).
        d = self._defaults
        self.p_minlen = _spin(0, 5000, d["minlen"])
        self.p_explen = _spin(0, 5000, d["explen"])
        self.p_demlen = _spin(0, 500, d["demlen"])
        self.p_searchlen = _spin(10, 500, d["primersearchlen"])
        self.p_primermm = _spin(0, 30, d["primermismatch"])
        self.p_tagmm = _spin(0, 5, d["tagmm"])

        layout.addWidget(_grid(
            self._tf(ctx, "Minimum length (bp)", self.p_minlen),
            self._tf(ctx, "Barcode length (bp)", self.p_explen),
            self._tf(ctx, "Window of barcode length ± (bp)", self.p_demlen),
            self._tf(ctx, "Window for primer search (bp)", self.p_searchlen),
            self._tf(ctx, "Primer mismatches allowed", self.p_primermm),
            self._tf(ctx, "Tag mismatches allowed", self.p_tagmm),
        ))
        layout.addStretch()
        self._tabs.addTab(tab, "Demultiplexing")

        # Highlight Barcode/Minimum length in soft red while they are unset (0),
        # so the user notices they must be entered (e.g. after enabling non-Coding).
        self.p_minlen.valueChanged.connect(self._update_length_field_styles)
        self.p_explen.valueChanged.connect(self._update_length_field_styles)
        self._update_length_field_styles()

    def _update_length_field_styles(self, *args):
        """Tint Barcode length / Minimum length soft-red while they are 0 (unset),
        normal otherwise. 0 is an invalid value flagged again on Start analysis."""
        _missing_style = (
            "background-color: #FCE4E4; color: #B71C1C; "
            "border: 1px solid #E57373; border-radius: 6px; padding: 5px 9px;"
        )
        for w in (getattr(self, 'p_minlen', None), getattr(self, 'p_explen', None)):
            if w is not None:
                w.setStyleSheet(_missing_style if w.value() == 0 else "")

    def _build_tab_consensus(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        ctx = "ParamsPanel"
        d = self._defaults
        self.p_consfreq = _spin(0.05, 0.95, d["consfreqfixed"], step=0.05, decimals=2)
        self.p_consrange = QtWidgets.QLineEdit(d["consrange"])
        self.p_consstep = _spin(0.01, 0.5, d["consstep"], step=0.01, decimals=2)
        self.p_mincov = _spin(1, 5000, d["mincoverage"])

        layout.addWidget(_grid(
            self._tf(ctx, "Main consensus calling frequency", self.p_consfreq),
            cols=1,
        ))
        _freq_row = QtWidgets.QWidget()
        _freq_hlay = QtWidgets.QHBoxLayout(_freq_row)
        _freq_hlay.setContentsMargins(0, 0, 0, 0)
        _freq_hlay.setSpacing(12)
        _freq_hlay.addWidget(
            self._tf(ctx, "Range of frequencies to assess (min, max)", self.p_consrange),
            1, QtCore.Qt.AlignVCenter,
        )
        _freq_hlay.addWidget(
            self._tf(ctx, "Range step", self.p_consstep),
            1, QtCore.Qt.AlignVCenter,
        )
        layout.addWidget(_freq_row)

        layout.addWidget(hline())
        self._lbl_cons_by_len = make_label("Consensus by length", size=17, bold=True)
        layout.addWidget(self._lbl_cons_by_len)

        self.p_covlist = QtWidgets.QLineEdit(d["coveragelist"])
        self.p_covlist.setMaximumWidth(400)
        self.p_lendev = _spin(0, 500, d["lendev"])

        _cov_container = QtWidgets.QWidget()
        _cov_vlay = QtWidgets.QVBoxLayout(_cov_container)
        _cov_vlay.setContentsMargins(0, 0, 0, 0)
        _cov_vlay.setSpacing(3)
        _cov_header = QtWidgets.QWidget()
        _cov_header.setMaximumWidth(400)
        _cov_hlay = QtWidgets.QHBoxLayout(_cov_header)
        _cov_hlay.setContentsMargins(0, 0, 0, 0)
        _cov_hlay.setSpacing(6)
        self._lbl_covlist = make_label("Coverage for phase 2a", size=16, color=TEXT_SEC)
        self._i18n_labels.append((self._lbl_covlist, ctx, "Coverage for phase 2a"))
        self._btn_reverse_cov = QtWidgets.QPushButton("↕  Reverse order")
        self._btn_reverse_cov.setToolTip("Reverse the order of coverage values")
        self._btn_reverse_cov.setStyleSheet(f"""
            QPushButton {{
                font-size: 14px; color: {BLUE}; background: transparent;
                border: 1px solid {BLUE_LIGHT}; border-radius: 4px;
                padding: 1px 8px;
            }}
            QPushButton:hover {{ background: {BLUE_LIGHT}; }}
            QPushButton:pressed {{ background: {BLUE_MID}; color: {WHITE}; }}
        """)
        self._btn_reverse_cov.setCursor(QtCore.Qt.PointingHandCursor)
        self._btn_reverse_cov.setSizePolicy(
            QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        self._btn_reverse_cov.clicked.connect(self._reverse_covlist)
        _cov_hlay.addWidget(self._lbl_covlist)
        _cov_hlay.addStretch()
        _cov_hlay.addWidget(self._btn_reverse_cov)
        _cov_vlay.addWidget(_cov_header)
        _cov_vlay.addWidget(self.p_covlist)

        layout.addWidget(_grid(
            _cov_container,
            cols=1,
        ))
        layout.addWidget(_grid(
            self._tf(ctx, "Maximum read length deviation from barcode length (bp)", self.p_lendev),
            self._tf(ctx, "Minimum read coverage", self.p_mincov),
            cols=2,
        ))

        layout.addWidget(hline())
        self._lbl_cons_by_sim = make_label("Consensus by similarity", size=17, bold=True)
        layout.addWidget(self._lbl_cons_by_sim)

        self.p_cov2b = _spin(1, 5000, d["coverage2b"])

        layout.addWidget(_grid(
            self._tf(ctx, "Coverage phase 2b", self.p_cov2b),
            cols=1,
        ))

        layout.addStretch()
        self._tabs.addTab(tab, "Consensus")

    def _reverse_covlist(self):
        parts = [p.strip() for p in self.p_covlist.text().split(",") if p.strip()]
        self.p_covlist.setText(", ".join(reversed(parts)))

    def _build_tab_general(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        ctx = "ParamsPanel"

        # ── Mixed-sample / contamination resolution (dominant haplotype) ──────
        # Marker-agnostic: placed first, above the Non-Coding block, to make clear it
        # applies to Coding and non-Coding alike. Off by default; the threshold spinbox
        # and the description appear only when the option is enabled.
        self.p_resolve = QtWidgets.QCheckBox(
            "Detect intra-sample sequence variants (keep dominant)")
        self._p_resolve_src = "Detect intra-sample sequence variants (keep dominant)"
        self.p_resolve.setStyleSheet("font-size:17px; font-weight:bold;")
        self.p_resolve.setToolTip(
            "Applies to both Coding and non-Coding markers.\n"
            "When a sample carries more than one co-abundant template (cross-\n"
            "contamination, index hopping, paralog/numt, real allelic variants),\n"
            "cluster the reads into haplotypes and keep the consensus of the\n"
            "DOMINANT (most abundant) one instead of a single majority that flips\n"
            "with the parameters. The number of clusters is detected automatically.\n"
            "Secondary variants are exported to secondary_variants.fa and the per-\n"
            "sample breakdown is written to the 'Intra-sample variants' sheet of\n"
            "runsummary.xlsx. In Coding markers the cleanly-translating haplotype is preferred\n"
            "(discards numts); in non-Coding the dominant one is used. Off by default.")
        self.p_resolve.setChecked(self._defaults["resolve_mixed_on"])
        layout.addWidget(self.p_resolve)

        _tip_rsec = (
            "Minimum proportion of a secondary haplotype for it to be counted as a "
            "real variant (and for the sample to be declared mixed and resolved). "
            "Below this, the minority is treated as noise and the normal consensus "
            "is kept. The per-column polymorphism threshold used to find variant "
            "sites is derived from it automatically (and never set above it, so a "
            "variant at this fraction is always detectable). Default 20%; values "
            "<~10% approach the ONT noise floor and risk false positives.")
        self.p_resolve_secfrac = _spin(10, 50, self._defaults["resolve_secfrac"])
        self.p_resolve_secfrac.setToolTip(_tip_rsec)

        _tip_rtol = (
            "Variant tolerance: maximum percentage of the diagnostic (polymorphic) "
            "sites at which two reads of the SAME haplotype may disagree — due to "
            "sequencing error or intrinsic variation — before they are split into "
            "separate variants. Lower = stricter (more clusters); higher = lumps "
            "more reads together. Default 10%.")
        self.p_resolve_tol = _spin(0, 40, self._defaults["resolve_variant_tol"])
        self.p_resolve_tol.setToolTip(_tip_rtol)
        self._resolve_grid = _grid(
            self._tf(ctx, "Min. secondary variant fraction (%)",
                     self.p_resolve_secfrac, tooltip=_tip_rsec),
            self._tf(ctx, "Variant tolerance (% of diagnostic sites)",
                     self.p_resolve_tol, tooltip=_tip_rtol),
            cols=1,
        )
        self._resolve_grid.setContentsMargins(22, 0, 0, 0)
        layout.addWidget(self._resolve_grid)

        # Description shown only while this option is on.
        self._resolve_info = QtWidgets.QLabel(
            "  Mixed samples are not rejected: the dominant (most abundant) variant "
            "is kept as the barcode and the secondary one(s) are written to "
            "secondary_variants.fa. The per-sample cluster breakdown is reported in "
            "the 'Intra-sample variants' sheet of runsummary.xlsx.")
        self._resolve_info.setStyleSheet(
            f"color: {TEXT_HINT}; font-size:13px; margin-left:22px;")
        self._resolve_info.setWordWrap(True)
        layout.addWidget(self._resolve_info)

        self.p_resolve.toggled.connect(self._sync_resolve_controls)
        self._sync_resolve_controls()  # hidden at start (checkbox off by default)

        # Separator: Resolve above is an independent top-level option (Coding and
        # non-Coding); the Non-Coding marker block below is separate.
        layout.addSpacing(10)
        layout.addWidget(hline())
        layout.addSpacing(10)

        self.p_non_coi = QtWidgets.QCheckBox("Non-Coding marker (ITS, trnL, 16S, 12S, etc.)")
        self._p_non_coi_src = "Non-Coding marker (ITS, trnL, 16S, 12S, etc.)"
        self.p_non_coi.setStyleSheet("font-size:17px; font-weight:bold;")
        self.p_non_coi.setToolTip(
            "Activate this mode for non-coding markers (ITS, trnL, 16S, 12S, etc.).\n"
            "Genetic code validation is omitted; barcodes are accepted\n"
            "solely by correct length and absence of ambiguous bases.\n"
            "Only Phase 1 and Phase 2a are executed. Phases 2b and 3 are disabled\n"
            "automatically because they require translation validation."
        )
        self.p_non_coi.setChecked(self._defaults["non_coi"])
        layout.addWidget(self.p_non_coi)

        self._non_coi_info = QtWidgets.QLabel(
            "  Activating this option will automatically disable Phases 2b and 3."
        )
        self._non_coi_info.setStyleSheet(f"color: {TEXT_SEC}; font-size:17px; margin-left:22px;")
        self._non_coi_info.setWordWrap(True)
        layout.addWidget(self._non_coi_info)

        # Separator before the basic per-run parameters below.
        layout.addSpacing(10)
        layout.addWidget(hline())
        layout.addSpacing(10)

        # ── Genetic code ───────────────────────────────────────────────
        self.p_gencode = QtWidgets.QComboBox()
        self.p_gencode.addItems(self.GENETIC_CODES)
        self.p_gencode.setCurrentIndex(self._defaults["gencode_index"])
        self.p_gencode.setMinimumHeight(SPINBOX_MIN_HEIGHT)

        # ── Processing threads ────────────────────────────────────────
        # (p_lendev is created and shown in the Consensus tab, which is built
        # after this one; no duplicate is needed here.)
        import multiprocessing as _mp
        _max_cpu = _mp.cpu_count()
        # self._default_nthreads / self._defaults["n_threads"] already resolved
        # to the physical-core count in __init__.
        self.p_nthreads = _spin(1, _max_cpu, self._defaults["n_threads"])
        self.p_nthreads.setToolTip(
            f"Threads for parallel analysis. Recommended: {self._default_nthreads} "
            f"(physical cores). Higher values oversubscribe the CPU and run slower; "
            f"the analysis is internally capped at {self._default_nthreads}. "
            f"{_max_cpu} logical processors available.")

        # p_maxcov is maintained for compatibility with get_params but not shown
        self.p_maxcov = _spin(0, 100000, 0)

        # ── Read quality filter (applies to Coding and non-Coding) ───────────────
        self.p_minq = _spin(0, 50, self._defaults["minq"])
        self.p_minq.setToolTip(
            "Discard input reads whose mean quality is below this Q value before\n"
            "demultiplexing (e.g. 20 keeps only reads with Q ≥ 20).\n"
            "Mean read quality is computed the ONT/NanoPlot way: "
            "-10·log10(mean per-base error probability), not a plain average of Q.\n"
            "0 = no quality filter (default). Requires FASTQ input with quality.")

        self._nthreads_field_src = f"Number of processing threads (max. {_max_cpu})"

        # Genetic code field, with a notice underneath that reports whether the
        # loaded CSV carries a per-sample genetic code (and if it is complete).
        layout.addWidget(self._tf(ctx, "Genetic code", self.p_gencode))
        self._gencode_csv_warn = make_label("", size=14, color=TEXT_SEC)
        self._gencode_csv_warn.setWordWrap(True)
        self._gencode_csv_warn.setVisible(False)
        self._gencode_csv_warn.setMaximumWidth(400)
        layout.addWidget(self._gencode_csv_warn)
        self._gencode_csv_counts = (0, 0)  # (samples_with_code, total_named)
        self._gencode_csv_breakdown = {}   # {ncbi_table: n_samples}

        layout.addSpacing(8)
        layout.addWidget(_grid(
            self._tf(ctx, self._nthreads_field_src, self.p_nthreads),
            self._tf(ctx, "Minimum mean read quality (Q, 0 = off)",
                     self.p_minq,
                     tooltip="Discard reads with ONT mean quality below this Q "
                             "before demultiplexing. 0 = off."),
            cols=1,
        ))

        # Connect: When non-Coding is activated, adjust phases and lock genetic code
        self.p_non_coi.stateChanged.connect(self._on_non_coi_changed)

        layout.addStretch()

        self._tabs.addTab(tab, "General")

    def retranslateUi(self):
        ctx = "ParamsPanel"
        self._lbl_params_title.setText(_tr(ctx, "Configure parameters"))
        self._mode_subtitle.setText(_tr(ctx, self._mode_subtitle_src))
        self._lbl_profiles_title.setText(_tr(ctx, "Parameter profiles"))
        self._lbl_profiles_desc.setText(_tr(ctx, "Save or load all parameters as a reusable profile (.json)."))
        self._non_coi_info.setText("  " + _tr(ctx, "Activating this option will automatically disable Phases 2b and 3."))
        self.p_non_coi.setText(_tr(ctx, self._p_non_coi_src))
        self._btn_save_profile.setText(_tr(ctx, "💾  Save profile"))
        self._btn_load_profile.setText(_tr(ctx, "📂  Load profile"))
        self._btn_reset_profile.setText(_tr(ctx, "↺  Default values"))
        self._btn_reverse_cov.setText(_tr(ctx, "↕  Reverse order"))
        self._btn_reverse_cov.setToolTip(_tr(ctx, "Reverse the order of coverage values"))
        self._run_btn.setText(_tr(ctx, "Start analysis  →"))
        self._lbl_cons_by_len.setText(_tr(ctx, "Consensus by length"))
        self._lbl_cons_by_sim.setText(_tr(ctx, "Consensus by similarity"))
        self._lbl_select_phases.setText(_tr(ctx, "Select phases to execute:"))
        for key, src in self._step_check_srcs.items():
            self._step_checks[key].setText(_tr(ctx, src))
        for lbl, c, src in self._i18n_labels:
            lbl.setText(_tr(c, src))
        idx = self.p_gencode.currentIndex()
        self.p_gencode.blockSignals(True)
        self.p_gencode.clear()
        self.p_gencode.addItems([_tr(ctx, gc) for gc in self.GENETIC_CODES])
        self.p_gencode.setCurrentIndex(idx)
        self.p_gencode.blockSignals(False)
        tab_names = ["General", "Demultiplexing", "Consensus", "Phases"]
        for i, name in enumerate(tab_names):
            self._tabs.setTabText(i, _tr(ctx, name))
        if hasattr(self, "_gencode_csv_warn"):
            self._apply_gencode_csv_status()

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def _save_profile(self):
        """Save all current parameters to a JSON file."""
        import json
        ctx = "ParamsPanel"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, _tr(ctx, "Save parameter profile"),
            os.path.join(_profiles_dir(), "parameters_profile.json"),
            "JSON profiles (*.json)"
        )
        if not path:
            return
        try:
            params = self.get_params()
            # Also save the state of the non_coi checkbox and the phase checks
            profile = {
                "non_coi":              self.p_non_coi.isChecked(),
                "resolve_mixed_on":     self.p_resolve.isChecked(),
                "resolve_secfrac":      self.p_resolve_secfrac.value(),
                "resolve_variant_tol":  self.p_resolve_tol.value(),
                "gencode_index":   self.p_gencode.currentIndex(),
                "minlen":          self.p_minlen.value(),
                "explen":          self.p_explen.value(),
                "demlen":          self.p_demlen.value(),
                "primersearchlen": self.p_searchlen.value(),
                "primermismatch":  self.p_primermm.value(),
                "tagmm":           self.p_tagmm.value(),
                "consfreqfixed":   self.p_consfreq.value(),
                "consrange":       self.p_consrange.text(),
                "consstep":        self.p_consstep.value(),
                "mincoverage":     self.p_mincov.value(),
                "coveragelist":    self.p_covlist.text(),
                "lendev":          self.p_lendev.value(),
                "minq":            self.p_minq.value(),
                "coverage2b":      self.p_cov2b.value(),
                "n_threads":       self.p_nthreads.value(),
                "run_phase1":      self._step_checks["phase1"].isChecked(),
                "run_phase2a":     self._step_checks["phase2a"].isChecked(),
                "run_phase2b":     self._step_checks["phase2b"].isChecked(),
                "run_phase3":      self._step_checks["phase3"].isChecked(),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)
            QtWidgets.QMessageBox.information(
                self, _tr(ctx, "Saved profile"),
                _tr(ctx, "Parameters saved correctly in:") + f"\n{path}"
            )
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, _tr(ctx, "Error saving"),
                _tr(ctx, "Could not save profile:") + f"\n{e}"
            )

    def _load_profile(self):
        """Load parameters from a JSON file and update all controls."""
        import json
        ctx = "ParamsPanel"
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, _tr(ctx, "Load parameter profile"), _profiles_dir(),
            "JSON profiles (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                profile = json.load(f)

            # Fallbacks come from the single DEFAULTS source so a key missing from
            # an external/old profile lands on the real current default, never a
            # stale literal.
            d = self._defaults

            # Block signals during charging to avoid side effects
            for w in (self.p_non_coi, self.p_gencode, self.p_minlen, self.p_explen,
                      self.p_demlen, self.p_searchlen, self.p_primermm, self.p_tagmm,
                      self.p_consfreq, self.p_consrange, self.p_consstep,
                      self.p_mincov, self.p_covlist, self.p_lendev, self.p_minq,
                      self.p_cov2b, self.p_nthreads):
                w.blockSignals(True)

            self.p_non_coi.setChecked(profile.get("non_coi", d["non_coi"]))
            self.p_gencode.setCurrentIndex(profile.get("gencode_index", d["gencode_index"]))
            self.p_minlen.setValue(profile.get("minlen", d["minlen"]))
            self.p_explen.setValue(profile.get("explen", d["explen"]))
            self.p_demlen.setValue(profile.get("demlen", d["demlen"]))
            self.p_searchlen.setValue(profile.get("primersearchlen", d["primersearchlen"]))
            self.p_primermm.setValue(profile.get("primermismatch", d["primermismatch"]))
            self.p_tagmm.setValue(profile.get("tagmm", d["tagmm"]))
            self.p_consfreq.setValue(profile.get("consfreqfixed", d["consfreqfixed"]))
            self.p_consrange.setText(profile.get("consrange", d["consrange"]))
            self.p_consstep.setValue(profile.get("consstep", d["consstep"]))
            self.p_mincov.setValue(profile.get("mincoverage", d["mincoverage"]))
            self.p_covlist.setText(profile.get("coveragelist", d["coveragelist"]))
            self.p_lendev.setValue(profile.get("lendev", d["lendev"]))
            self.p_minq.setValue(profile.get("minq", d["minq"]))
            self.p_cov2b.setValue(profile.get("coverage2b", d["coverage2b"]))
            self.p_nthreads.setValue(profile.get("n_threads", d["n_threads"]))

            for w in (self.p_non_coi, self.p_gencode, self.p_minlen, self.p_explen,
                      self.p_demlen, self.p_searchlen, self.p_primermm, self.p_tagmm,
                      self.p_consfreq, self.p_consrange, self.p_consstep,
                      self.p_mincov, self.p_covlist, self.p_lendev, self.p_minq,
                      self.p_cov2b, self.p_nthreads):
                w.blockSignals(False)

            for _rw in (self.p_resolve, self.p_resolve_secfrac, self.p_resolve_tol):
                _rw.blockSignals(True)
            self.p_resolve.setChecked(profile.get("resolve_mixed_on", d["resolve_mixed_on"]))
            self.p_resolve_secfrac.setValue(profile.get("resolve_secfrac", d["resolve_secfrac"]))
            self.p_resolve_tol.setValue(profile.get("resolve_variant_tol", d["resolve_variant_tol"]))
            for _rw in (self.p_resolve, self.p_resolve_secfrac, self.p_resolve_tol):
                _rw.blockSignals(False)
            self._sync_resolve_controls()

            # Disparar manualmente para refrescar estados de fases y código genético
            self._on_non_coi_changed(2 if profile.get("non_coi", d["non_coi"]) else 0)

            # Restaurar fases (solo si no-Coding no las bloquea)
            non_coi = profile.get("non_coi", d["non_coi"])
            for key in ("phase1", "phase2a"):
                self._step_checks[key].setChecked(
                    profile.get(f"run_{key}", d[f"run_{key}"]))
            if not non_coi:
                self._step_checks["phase2b"].setChecked(
                    profile.get("run_phase2b", d["run_phase2b"]))
                self._step_checks["phase3"].setChecked(
                    profile.get("run_phase3", d["run_phase3"]))

            QtWidgets.QMessageBox.information(
                self, _tr(ctx, "Profile loaded"),
                _tr(ctx, "Parameters loaded correctly from:") + f"\n{path}"
            )
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, _tr(ctx, "Error loading"),
                _tr(ctx, "Profile could not be loaded:") + f"\n{e}"
            )

    def _reset_to_defaults(self):
        """Resets all parameters to their initial default values."""
        d = self._defaults
        for w in (self.p_non_coi, self.p_gencode, self.p_minlen, self.p_explen,
                  self.p_demlen, self.p_searchlen, self.p_primermm, self.p_tagmm,
                  self.p_consfreq, self.p_consrange, self.p_consstep,
                  self.p_mincov, self.p_covlist, self.p_lendev, self.p_minq,
                  self.p_cov2b, self.p_nthreads):
            w.blockSignals(True)

        self.p_non_coi.setChecked(d["non_coi"])
        self.p_gencode.setCurrentIndex(d["gencode_index"])
        self.p_minlen.setValue(d["minlen"])
        self.p_explen.setValue(d["explen"])
        self.p_demlen.setValue(d["demlen"])
        self.p_searchlen.setValue(d["primersearchlen"])
        self.p_primermm.setValue(d["primermismatch"])
        self.p_tagmm.setValue(d["tagmm"])
        self.p_consfreq.setValue(d["consfreqfixed"])
        self.p_consrange.setText(d["consrange"])
        self.p_consstep.setValue(d["consstep"])
        self.p_mincov.setValue(d["mincoverage"])
        self.p_covlist.setText(d["coveragelist"])
        self.p_lendev.setValue(d["lendev"])
        self.p_minq.setValue(d["minq"])
        self.p_cov2b.setValue(d["coverage2b"])
        self.p_nthreads.setValue(d["n_threads"])

        for w in (self.p_non_coi, self.p_gencode, self.p_minlen, self.p_explen,
                  self.p_demlen, self.p_searchlen, self.p_primermm, self.p_tagmm,
                  self.p_consfreq, self.p_consrange, self.p_consstep,
                  self.p_mincov, self.p_covlist, self.p_lendev, self.p_minq,
                  self.p_cov2b, self.p_nthreads):
            w.blockSignals(False)

        for _rw in (self.p_resolve, self.p_resolve_secfrac, self.p_resolve_tol):
            _rw.blockSignals(True)
        self.p_resolve.setChecked(d["resolve_mixed_on"])
        self.p_resolve_secfrac.setValue(d["resolve_secfrac"])
        self.p_resolve_tol.setValue(d["resolve_variant_tol"])
        for _rw in (self.p_resolve, self.p_resolve_secfrac, self.p_resolve_tol):
            _rw.blockSignals(False)
        self._sync_resolve_controls()

        self._on_non_coi_changed(0)
        for key in ("phase1", "phase2a", "phase2b", "phase3"):
            cb = self._step_checks.get(key)
            if cb:
                cb.setChecked(d[f"run_{key}"])

    def _build_tab_steps(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        ctx = "ParamsPanel"
        self._lbl_select_phases = make_label("Select phases to execute:", color=TEXT_SEC)
        layout.addWidget(self._lbl_select_phases)

        self._step_checks = {}
        self._step_check_srcs = {}
        steps = [
            ("phase1",  "Phase 1: Demultiplexing",                   True),
            ("phase2a", "Phase 2a: Consensus by length",            True),
            ("phase2b", "Phase 2b: Consensus by similarity (MSA)",     True),
            ("phase3",  "Phase 3: Correction by barcode comparisons",        True),
        ]
        for key, label, default in steps:
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(default)
            cb.setStyleSheet("font-size:17px;")
            layout.addWidget(cb)
            self._step_checks[key] = cb
            self._step_check_srcs[key] = label

        layout.addStretch()
        self._tabs.addTab(tab, "Phases")

    def _sync_resolve_controls(self, *args):
        """Show the mixed-resolution threshold spinbox and its description only
        while the feature is enabled; hide them otherwise to keep the panel clean."""
        on = bool(getattr(self, 'p_resolve', None) and self.p_resolve.isChecked())
        if getattr(self, '_resolve_grid', None) is not None:
            self._resolve_grid.setVisible(on)
        if getattr(self, '_resolve_info', None) is not None:
            self._resolve_info.setVisible(on)

    def set_gencode_csv_status(self, n_with_code: int, n_total_named: int,
                               breakdown=None):
        """Record how many samples in the loaded CSV carry a per-sample genetic
        code (and the per-table breakdown {ncbi_table: n_samples}) and refresh the
        notice shown under the genetic code combobox. Called by MainWindow after a
        CSV is loaded."""
        self._gencode_csv_counts = (n_with_code, n_total_named)
        self._gencode_csv_breakdown = dict(breakdown) if breakdown else {}
        self._apply_gencode_csv_status()

    def _apply_gencode_csv_status(self):
        """Render the per-sample genetic code notice from the stored counts and
        enable/disable the combobox accordingly: when the CSV provides a code for
        EVERY sample the menu is ignored, so it is disabled. Hidden/untouched while
        non-Coding mode is on (that mode owns the combobox and disables translation)."""
        lbl = getattr(self, "_gencode_csv_warn", None)
        if lbl is None:
            return
        gencode = getattr(self, "p_gencode", None)
        _disabled_style = (
            f"background-color: {GRAY_BG}; color: {TEXT_HINT}; "
            f"border: 1px solid {GRAY_LINE}; border-radius: 6px; padding: 5px 9px;")
        n_with, n_total = getattr(self, "_gencode_csv_counts", (0, 0))
        non_coi = bool(getattr(self, "p_non_coi", None) and self.p_non_coi.isChecked())
        ctx = "ParamsPanel"

        def _enable_menu(on: bool, tip: str = ""):
            if gencode is None:
                return
            gencode.setEnabled(on)
            gencode.setStyleSheet("" if on else _disabled_style)
            gencode.setToolTip(tip)

        # non-Coding mode owns the combobox enabled state → only hide the notice.
        if non_coi:
            lbl.setVisible(False)
            lbl.setText("")
            return
        # No per-sample codes: the menu is in charge, make sure it is usable.
        if n_total <= 0 or n_with == 0:
            _enable_menu(True)
            lbl.setVisible(False)
            lbl.setText("")
            return
        if n_with >= n_total:
            _bd = getattr(self, "_gencode_csv_breakdown", {}) or {}
            _brk = "; ".join(f"{_tr(ctx, 'table')} {t}: {n}"
                             for t, n in sorted(_bd.items()))
            _detail = f" ({_brk})" if _brk else ""
            lbl.setText("✓ " + _tr(ctx,
                "Per-sample genetic code detected in the CSV for {0} samples")
                .format(n_total) + _detail + " — "
                + _tr(ctx, "this menu is ignored."))
            lbl.setStyleSheet(
                f"color:{GREEN}; background-color:{GREEN_LT}; "
                f"border:1px solid {GREEN}; border-radius:6px; padding:6px 9px;")
            # Codes are complete → the menu has no effect, disable it.
            _enable_menu(False, _tr(ctx,
                "Disabled: per-sample genetic codes from the CSV are used."))
        else:
            lbl.setText("⚠ " + _tr(ctx,
                "Genetic code present on only {0} of {1} samples. Add it to every "
                "sample, or remove it from all, or the run will be blocked.")
                .format(n_with, n_total))
            lbl.setStyleSheet(
                f"color:{AMBER}; background-color:{AMBER_LT}; "
                f"border:1px solid {AMBER}; border-radius:6px; padding:6px 9px;")
            # Incomplete → leave the menu usable (the run is blocked anyway).
            _enable_menu(True)
        lbl.setVisible(True)

    def _on_non_coi_changed(self, state: int):
        """When non-Coding Marker is activated, disable phases 2b and 3,
        block genetic code and deactivate phase 2b coverage.
        Disabled widgets receive a distinctive visual style."""
        non_coi = bool(state)

        _disabled_style = (
            f"background-color: {GRAY_BG}; color: {TEXT_HINT}; "
            f"border: 1px solid {GRAY_LINE}; border-radius: 6px; padding: 5px 9px;"
        )
        _cb_disabled_style = f"color: {TEXT_HINT};"

        # Lock/unlock genetic code
        if hasattr(self, 'p_gencode'):
            self.p_gencode.setEnabled(not non_coi)
            self.p_gencode.setStyleSheet(_disabled_style if non_coi else "")
            self.p_gencode.setToolTip(
                "Disabled in non-Coding marker mode." if non_coi else ""
            )

        # Block/unlock phase 2b coverage
        if hasattr(self, 'p_cov2b'):
            self.p_cov2b.setEnabled(not non_coi)
            self.p_cov2b.setStyleSheet(_disabled_style if non_coi else "")
            self.p_cov2b.setToolTip(
                "Disabled in non-Coding marker mode." if non_coi else ""
            )

        for key in ("phase2b", "phase3"):
            cb = self._step_checks.get(key)
            if cb is None:
                continue
            if non_coi:
                cb.setChecked(False)
                cb.setEnabled(False)
                cb.setStyleSheet(_cb_disabled_style)
                cb.setToolTip(
                    "Disabled in non-Coding marker mode.\n"
                    "This phase requires validation of the genetic code."
                )
            else:
                cb.setEnabled(True)
                cb.setChecked(True)
                cb.setStyleSheet("")
                cb.setToolTip("")

        # Adjust default coverage according to mode
        if hasattr(self, 'p_covlist'):
            _COI_DEFAULT    = "25, 50, 100, 200"
            _NONCOI_DEFAULT = "1500, 1000, 500, 200, 100, 50, 25"
            current = self.p_covlist.text().strip()
            if non_coi and current == _COI_DEFAULT:
                self.p_covlist.setText(_NONCOI_DEFAULT)
            elif not non_coi and current == _NONCOI_DEFAULT:
                self.p_covlist.setText(_COI_DEFAULT)

        # Adjust consensus frequency defaults according to mode
        if hasattr(self, 'p_consfreq') and hasattr(self, 'p_consrange'):
            _COI_FREQ, _NONCOI_FREQ         = 0.3, 0.4
            _COI_RANGE, _NONCOI_RANGE       = "0.2, 0.5", "0.3, 0.5"
            if non_coi:
                if abs(self.p_consfreq.value() - _COI_FREQ) < 1e-9:
                    self.p_consfreq.setValue(_NONCOI_FREQ)
                if self.p_consrange.text().strip() == _COI_RANGE:
                    self.p_consrange.setText(_NONCOI_RANGE)
            else:
                if abs(self.p_consfreq.value() - _NONCOI_FREQ) < 1e-9:
                    self.p_consfreq.setValue(_COI_FREQ)
                if self.p_consrange.text().strip() == _NONCOI_RANGE:
                    self.p_consrange.setText(_COI_RANGE)

        # Adjust length-window defaults according to mode. Only switch a field
        # when it is still at the other mode's default, so manual edits by the
        # user are never overwritten. (widget, Coding default, non-Coding default)
        _len_defaults = [
            (getattr(self, 'p_lendev', None),    50,  200),  # Max read length deviation
            (getattr(self, 'p_demlen', None),    100, 200),  # Window of barcode length
            (getattr(self, 'p_searchlen', None), 100, 150),  # Window for primer search
        ]
        for w, coi_def, noncoi_def in _len_defaults:
            if w is None:
                continue
            if non_coi and w.value() == coi_def:
                w.setValue(noncoi_def)
            elif not non_coi and w.value() == noncoi_def:
                w.setValue(coi_def)

        # Barcode length depends on the marker, so on switching to non-Coding clear
        # the COI default (658) to 0 ("unset") to force the user to enter the
        # marker-specific value; switching back to COI restores 658 if still
        # unset. A 0 value is shown in soft red and re-checked on Start analysis.
        for w in (getattr(self, 'p_explen', None), getattr(self, 'p_minlen', None)):
            if w is None:
                continue
            if non_coi and w.value() == 658:
                w.setValue(0)
            elif not non_coi and w.value() == 0:
                w.setValue(658)
        self._update_length_field_styles()

        # Per-sample genetic code notice is meaningless in non-Coding mode → refresh.
        self._apply_gencode_csv_status()

    def get_params(self) -> dict:
        gencode_map = {
            0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 9,
            7: 10, 8: 11, 9: 12, 10: 13, 11: 14, 12: 16,
            13: 21, 14: 22, 15: 23, 16: 24, 17: 25, 18: 26,
        }
        try:
            cov_list = [int(x.strip()) for x in self.p_covlist.text().split(",") if x.strip()]
        except ValueError:
            cov_list = [25, 50, 100, 200]

        try:
            cons_range = [float(x.strip()) for x in self.p_consrange.text().split(",")]
            cons_range_min = cons_range[0] if len(cons_range) > 0 else 0.2
            cons_range_max = cons_range[1] if len(cons_range) > 1 else 0.5
        except (ValueError, IndexError):
            cons_range_min, cons_range_max = 0.2, 0.5

        non_coi = getattr(self, 'p_non_coi', None) and self.p_non_coi.isChecked()

        # Fracción mínima del contaminante (única perilla de la resolución de
        # mezcla). El umbral de polimorfismo por columna se deriva de ella abajo.
        _resolve_secfrac = ((self.p_resolve_secfrac.value() / 100.0)
                            if getattr(self, 'p_resolve_secfrac', None) else 0.2)
        _resolve_tol = ((self.p_resolve_tol.value() / 100.0)
                        if getattr(self, 'p_resolve_tol', None) else 0.10)

        return {
            "minlen": self.p_minlen.value(),
            "explen": self.p_explen.value(),
            "demlen": self.p_demlen.value(),
            "primersearchlen": self.p_searchlen.value(),
            "primermismatch": self.p_primermm.value(),
            "tagmm": self.p_tagmm.value(),
            "consfreqfixed": self.p_consfreq.value(),
            "consfreqmin": cons_range_min,
            "consfreqmax": cons_range_max,
            "consfreqstep": self.p_consstep.value(),
            "mincoverage": self.p_mincov.value(),
            "coveragelist": cov_list,
            "coverage2b": self.p_cov2b.value(),
            "n_threads": self.p_nthreads.value(),
            # For non-Coding we use gencode=0, which activates the special route in
            # _runconsensusparts_fn: validates only length and absence of Ns without
            # call translate_corframe (that's why there is no Biopython exception).
            "gencode": 0 if non_coi else gencode_map.get(self.p_gencode.currentIndex(), 5),
            "lendev": self.p_lendev.value(),
            "maxcoverage": self.p_maxcov.value(),
            "minq": (self.p_minq.value()
                     if getattr(self, 'p_minq', None) is not None else 0),
            "non_coi": bool(non_coi),
            # Resolución de mezcla/contaminación por haplotipo dominante.
            # Marca-agnóstica (Coding y no-Coding). On por defecto.
            # Una sola perilla: la fracción mínima del contaminante. El umbral de
            # polimorfismo por columna (minor_thresh) se DERIVA de ella y nunca se
            # fija por encima, de modo que una mezcla de ese tamaño siempre puede
            # detectarse (sin la "trampa del piso oculto"). Con el valor por defecto
            # (20%) el comportamiento es idéntico al anterior (minor_thresh = 0.2).
            "resolve_mixed": {
                "enabled": bool(getattr(self, 'p_resolve', None)
                                and self.p_resolve.isChecked()),
                "min_secondary_frac": _resolve_secfrac,
                "minor_thresh": min(0.20, _resolve_secfrac),
                "tolerance": _resolve_tol,
            },
            "run_phase1": self._step_checks["phase1"].isChecked(),
            "run_phase2a": self._step_checks["phase2a"].isChecked(),
            # In non-Coding mode, phases 2b and 3 always deactivated
            "run_phase2b": False if non_coi else self._step_checks["phase2b"].isChecked(),
            "run_phase3": False if non_coi else self._step_checks["phase3"].isChecked(),
            "live_consensus_reads": (
                self.p_live_reads.value()
                if getattr(self, '_freq_cb_reads', None) and self._freq_cb_reads.isChecked()
                else None),
            "live_consensus_minutes": (
                self.p_live_mins.value()
                if getattr(self, '_freq_cb_time', None) and self._freq_cb_time.isChecked()
                else None),
        }

    def _on_run(self):
        # Required length fields must be set (non-zero). In non-Coding mode they are
        # cleared to 0 on purpose so the user enters the marker-specific length.
        _missing = []
        if self.p_explen.value() == 0:
            _missing.append("Barcode length (bp)")
        if self.p_minlen.value() == 0:
            _missing.append("Minimum length (bp)")
        if _missing:
            self._update_length_field_styles()
            dlg = QtWidgets.QMessageBox(self)
            dlg.setWindowTitle(_tr("ParamsPanel", "Missing information"))
            dlg.setText(_tr(
                "ParamsPanel",
                "Please enter a value for: {fields}.\n\n"
                "The barcode length depends on the marker, so it must be set "
                "explicitly (no COI default is assumed in non-Coding mode)."
            ).format(fields=", ".join(_missing)))
            dlg.setIcon(QtWidgets.QMessageBox.Warning)
            dlg.setStyleSheet(
                f"QMessageBox {{ background-color: {GRAY_CARD}; }} "
                f"QLabel {{ color: {TEXT_PRI}; }}")
            dlg.exec_()
            return
        self.runRequested.emit(self.get_params())


# ═══════════════════════════════════════════════════════════════════════════
# PANEL 3: PROGRESS
# ═══════════════════════════════════════════════════════════════════════════

class PhaseRow(QtWidgets.QFrame):
    def __init__(self, phase_id, name, parent=None):
        super().__init__(parent)
        self.setObjectName("phase_row")
        self.setProperty("state", "pending")
        self.setFixedHeight(90)
        self._src_name = name

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(12)

        self._phase_id = phase_id
        self._icon = QtWidgets.QLabel(phase_id)
        self._icon.setFixedSize(28, 28)
        self._icon.setAlignment(QtCore.Qt.AlignCenter)

        info = QtWidgets.QWidget()
        info_layout = QtWidgets.QVBoxLayout(info)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(2)
        self._name_lbl = make_label(name, size=19, bold=True)
        self._detail_lbl = make_label("Waiting", size=16, color=TEXT_HINT)
        self._is_pending = True
        info_layout.addWidget(self._name_lbl)
        info_layout.addWidget(self._detail_lbl)

        self._bar = QtWidgets.QProgressBar()
        self._bar.setFixedWidth(180)
        self._bar.setFixedHeight(6)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)

        layout.addWidget(self._icon)
        layout.addWidget(info, 1)
        layout.addWidget(self._bar)

    def retranslateUi(self):
        self._name_lbl.setText(_tr("PhaseRow", self._src_name))
        if self._is_pending:
            self._detail_lbl.setText(_tr("PhaseRow", "Waiting"))

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def set_state(self, state: str, detail="", progress=0):
        self._is_pending = (state == "pending")
        self.setProperty("state", state)
        refresh_style(self)

        if state == "pending":
            self._icon.setText(self._phase_id)
            self._icon.setStyleSheet("")
            self._bar.setValue(0)
            self._detail_lbl.setText(_tr("PhaseRow", "Waiting"))
        elif state == "done":
            self._icon.setStyleSheet(
                f"border-radius:14px; background:{GREEN_LT}; color:{GREEN}; font-size:12px; font-weight:700;"
            )
            self._icon.setText("✓")
            self._bar.setValue(100)
        elif state == "current":
            self._icon.setStyleSheet(
                f"border-radius:14px; background:{BLUE}; color:white; font-size:11px; font-weight:600;"
            )
            if progress > 0:
                self._bar.setValue(int(progress))
        elif state == "error":
            self._icon.setStyleSheet(
                f"border-radius:14px; background:{RED_LT}; color:{RED}; font-size:12px; font-weight:700;"
            )
            self._icon.setText("✕")

        if detail:
            self._detail_lbl.setText(detail)

    def set_progress(self, value: int, total: int, extra: str = ""):
        if total > 0:
            pct = min(100, int(value / total * 100))
            self._bar.setValue(pct)
            self._detail_lbl.setText(f"{pct}%  ({extra})" if extra else f"{pct}%")


class StatCard(QtWidgets.QFrame):
    def __init__(self, label, value="—", color=TEXT_PRI, parent=None):
        super().__init__(parent)
        self.setObjectName("stat_card")
        self._src_label = label

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)

        self._lbl = make_label(label, size=17, color=TEXT_SEC)
        self._val = make_label(value, size=22, bold=True, color=color)
        for w in (self._lbl, self._val):
            w.setAlignment(QtCore.Qt.AlignCenter)

        layout.addWidget(self._lbl)
        layout.addWidget(self._val)

    def retranslateUi(self):
        self._lbl.setText(_tr("StatCard", self._src_label))

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def update_value(self, value):
        self._val.setText(str(value))


class ProgressPanel(QtWidgets.QWidget):
    stopRequested = QtCore.pyqtSignal()
    finalizeRequested = QtCore.pyqtSignal()

    PHASES = [
        ("1", "Phase 1 · Demultiplexing"),
        ("2a", "Phase 2a · Consensus by length"),
        ("2b", "Phase 2b · Consensus by similarity"),
        ("3", "Phase 3 · Correction by barcode comparisons"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._start_time = None
        self._timer_tick = None
        self._cycle_start_time = None
        self._cycle_timer_tick = None
        self._ok_current = 0   # only update if major
        self._mode = None      # "conventional" | "live" | None
        self._non_coi = False

        # ── Main layout: scroll up + buttons anchored below ──
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scrollable area
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._inner = QtWidgets.QWidget()
        self._layout = QtWidgets.QVBoxLayout(self._inner)
        self._layout.setContentsMargins(20, 20, 20, 8)
        self._layout.setSpacing(16)
        scroll.setWidget(self._inner)
        outer.addWidget(scroll, 1)

        # ── Header ──
        header = QtWidgets.QWidget()
        hlayout = QtWidgets.QHBoxLayout(header)
        hlayout.setContentsMargins(0, 0, 0, 0)

        left = QtWidgets.QWidget()
        llayout = QtWidgets.QVBoxLayout(left)
        llayout.setContentsMargins(0, 0, 0, 0)
        llayout.setSpacing(2)
        self._analysis_title_lbl = make_label("Analysis in progress", size=22, bold=True)
        llayout.addWidget(self._analysis_title_lbl)
        self._phase_lbl = make_label("Starting...", color=TEXT_SEC)
        llayout.addWidget(self._phase_lbl)

        # stop button with hover, disable finalize on stop
        self._stop_btn = QtWidgets.QPushButton("Stop")
        self._stop_btn.setObjectName("danger_btn")
        self._stop_btn.setFixedHeight(40)
        self._stop_btn.setFixedWidth(180)
        self._stop_btn.setStyleSheet(
            f"QPushButton {{ background:transparent; color:{RED}; border:1px solid #F09595; "
            f"border-radius:8px; padding:7px 16px; font-size:18px; }}"
            f"QPushButton:hover {{ background-color:{RED_LT}; }}"
            f"QPushButton:pressed {{ background-color:#EE5050; color:{WHITE}; }}"
            f"QPushButton:disabled {{ color:{TEXT_HINT}; border-color:{GRAY_LINE}; }}"
        )
        self._stop_btn.clicked.connect(self.stopRequested.emit)

        # finalize button: bigger padding, white square symbol
        self._finalize_btn = QtWidgets.QPushButton("■ Finalize RT")
        self._finalize_btn.setObjectName("primary_btn")
        self._finalize_btn.setFixedHeight(40)
        self._finalize_btn.setFixedWidth(180)
        self._finalize_btn.setStyleSheet(
            f"QPushButton {{ background-color:{BLUE}; color:white; border:none; "
            f"border-radius:8px; padding:9px 24px; font-size:18px; font-weight:500; }}"
            f"QPushButton:hover {{ background-color:#0C4A82; }}"
            f"QPushButton:pressed {{ background-color:#083460; }}"
            f"QPushButton:disabled {{ background-color:{GRAY_LINE}; color:{TEXT_HINT}; }}"
        )
        self._finalize_btn.setVisible(False)
        self._finalize_btn.clicked.connect(self.finalizeRequested.emit)

        btn_col = QtWidgets.QVBoxLayout()
        btn_col.setSpacing(6)
        btn_col.addWidget(self._stop_btn)
        btn_col.addWidget(self._finalize_btn)

        hlayout.addWidget(left, 1)
        hlayout.addLayout(btn_col)
        self._layout.addWidget(header)

        # ── Stat cards ──
        stats_container = QtWidgets.QWidget()
        stats_layout = QtWidgets.QHBoxLayout(stats_container)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(10)

        self.stat_total = StatCard("Total reads", "—")
        self.stat_dem   = StatCard("Assigned reads", "—", color=BLUE)
        self.stat_ok    = StatCard("QC barcodes", "—", color=GREEN)

        # ── Cycle counter card (nuevo) ──
        self._cycle_card = QtWidgets.QFrame()
        self._cycle_card.setObjectName("stat_card")
        _cc_layout = QtWidgets.QVBoxLayout(self._cycle_card)
        _cc_layout.setContentsMargins(12, 10, 12, 10)
        _cc_layout.setSpacing(2)
        self._lbl_cycle = make_label("Cycle", size=17, color=TEXT_SEC)
        self._cycle_lbl = make_label("0", size=22, bold=True, color=TEXT_PRI)
        for w in (self._lbl_cycle, self._cycle_lbl):
            w.setAlignment(QtCore.Qt.AlignCenter)
        _cc_layout.addWidget(self._lbl_cycle)
        _cc_layout.addWidget(self._cycle_lbl)
        self._cycle_card.setVisible(False)

        # timer card next to barcodes ok
        self._timer_card = QtWidgets.QFrame()
        self._timer_card.setObjectName("stat_card")
        _tc_layout = QtWidgets.QVBoxLayout(self._timer_card)
        _tc_layout.setContentsMargins(12, 10, 12, 10)
        _tc_layout.setSpacing(2)
        self._lbl_timer = make_label("Total time", size=17, color=TEXT_SEC)
        self._timer_lbl = make_label("00:00:00", size=22, bold=True, color=TEXT_PRI)
        for w in (self._lbl_timer, self._timer_lbl):
            w.setAlignment(QtCore.Qt.AlignCenter)
        _tc_layout.addWidget(self._lbl_timer)
        _tc_layout.addWidget(self._timer_lbl)

        self._cycle_timer_card = QtWidgets.QFrame()
        self._cycle_timer_card.setObjectName("stat_card")
        _ctc_layout = QtWidgets.QVBoxLayout(self._cycle_timer_card)
        _ctc_layout.setContentsMargins(12, 10, 12, 10)
        _ctc_layout.setSpacing(2)
        self._lbl_cycle_timer = make_label("Cycle time", size=17, color=TEXT_SEC)
        self._cycle_timer_lbl = make_label("00:00:00", size=22, bold=True, color=BLUE)
        for w in (self._lbl_cycle_timer, self._cycle_timer_lbl):
            w.setAlignment(QtCore.Qt.AlignCenter)
        _ctc_layout.addWidget(self._lbl_cycle_timer)
        _ctc_layout.addWidget(self._cycle_timer_lbl)
        self._cycle_timer_card.setVisible(False)

        for card in (self.stat_total, self.stat_dem, self.stat_ok):
            stats_layout.addWidget(card, 8)
        stats_layout.addStretch(1)
        stats_layout.addWidget(self._cycle_card, 3)
        stats_layout.addWidget(self._cycle_timer_card, 3)
        stats_layout.addWidget(self._timer_card, 3)
        self._layout.addWidget(stats_container)

        # ── Phase rows ──
        self._phase_rows = {}
        for pid, pname in self.PHASES:
            row = PhaseRow(pid, pname)
            self._layout.addWidget(row)
            self._phase_rows[pid] = row

        self._layout.addWidget(hline())
        self._lbl_log_section = make_section_label("Process log")
        self._layout.addWidget(self._lbl_log_section)

        self._log = QtWidgets.QTextEdit()
        self._log.setObjectName("log_area")
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(200)
        self._log.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding)
        self._log.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse | QtCore.Qt.TextSelectableByKeyboard)
        #self._layout.addWidget(self._log)
        #self._layout.addStretch()
        self._layout.addWidget(self._log, stretch=1)
        self._logfile_handle = None

    def retranslateUi(self):
        ctx = "ProgressPanel"
        if self._mode == "live":
            title = _tr(ctx, "Analysis in progress: ⚡ Real-Time")
            if self._non_coi:
                title += " (non-Coding)"
        elif self._mode == "conventional":
            title = _tr(ctx, "Analysis in progress: ▶ Conventional")
            if self._non_coi:
                title += " (non-Coding)"
        else:
            title = _tr(ctx, "Analysis in progress")
        self._analysis_title_lbl.setText(title)
        self._stop_btn.setText(_tr(ctx, "Stop"))
        self._finalize_btn.setText(_tr(ctx, "■ Finalize RT"))
        self._lbl_timer.setText(_tr(ctx, "Time"))
        self._lbl_cycle_timer.setText(_tr(ctx, "Cycle time"))
        self._lbl_log_section.setText(("  " + _tr(ctx, "Process log")).upper())
        self.stat_total.retranslateUi()
        self.stat_dem.retranslateUi()
        self.stat_ok.retranslateUi()
        for row in self._phase_rows.values():
            row.retranslateUi()

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def _start_timer(self):
        """Start the analysis duration timer."""
        self._start_time = time.time()
        if self._timer_tick:
            self._timer_tick.stop()
        self._timer_tick = QtCore.QTimer(self)
        self._timer_tick.timeout.connect(self._update_timer_display)
        self._timer_tick.start(1000)
        self._update_timer_display()

    def _update_timer_display(self):
        if self._start_time is None:
            return
        elapsed = int(time.time() - self._start_time)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        self._timer_lbl.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def _stop_timer(self):
        if self._timer_tick:
            self._timer_tick.stop()
            self._timer_tick = None

    def _start_cycle_timer(self):
        """Start/reset the current RT cycle length timer."""
        self._cycle_start_time = time.time()
        if self._cycle_timer_tick:
            self._cycle_timer_tick.stop()
        self._cycle_timer_tick = QtCore.QTimer(self)
        self._cycle_timer_tick.timeout.connect(self._update_cycle_timer_display)
        self._cycle_timer_tick.start(1000)
        self._update_cycle_timer_display()

    def _stop_cycle_timer(self):
        if self._cycle_timer_tick:
            self._cycle_timer_tick.stop()
            self._cycle_timer_tick = None

    def _update_cycle_timer_display(self):
        if self._cycle_start_time is None:
            return
        elapsed = int(time.time() - self._cycle_start_time)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        self._cycle_timer_lbl.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def update_cycle_number(self, cycle: int):
        self._cycle_lbl.setText(str(cycle))

    def reset(self):
        self._ok_current = 0
        self._stop_timer()
        self._timer_lbl.setText("00:00:00")
        self._stop_cycle_timer()
        self._cycle_timer_lbl.setText("00:00:00")
        self._cycle_timer_card.setVisible(False)
        self._cycle_card.setVisible(False)
        self._log.clear()
        self._mode = "conventional"
        self._non_coi = False
        ctx = "ProgressPanel"
        self._analysis_title_lbl.setText(_tr(ctx, "Analysis in progress: ▶ Conventional"))
        self._analysis_title_lbl.setStyleSheet(
            f"font-size:24px; font-weight:600; color:{TEXT_PRI};"
        )
        self._phase_lbl.setText(_tr(ctx, "Starting..."))
        self._phase_lbl.setStyleSheet(f"font-size:18px; color:{TEXT_SEC};")
        for row in self._phase_rows.values():
            row.set_state("pending")
            row.set_progress(0, 0)
            row.setVisible(True)   # all visible until configured_phases filters them out
        self._finalize_btn.setVisible(False)
        self._finalize_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)
        for card in (self.stat_total, self.stat_dem, self.stat_ok):
            card.update_value("—")

    def reset_soft(self):
        """Clear log, phases and stats without touching the current timer.
        Used when moving from RT -> final conventional analysis to preserve
        the accumulated time since the beginning of the entire process."""
        self._ok_current = 0
        self._log.clear()
        self._mode = "conventional"
        self._non_coi = False
        ctx = "ProgressPanel"
        self._analysis_title_lbl.setText(_tr(ctx, "Analysis in progress: ▶ Conventional"))
        self._analysis_title_lbl.setStyleSheet(
            f"font-size:24px; font-weight:600; color:{TEXT_PRI};"
        )
        self._phase_lbl.setText(_tr(ctx, "Starting..."))
        self._phase_lbl.setStyleSheet(f"font-size:18px; color:{TEXT_SEC};")
        for row in self._phase_rows.values():
            row.set_state("pending")
            row.set_progress(0, 0)
            row.setVisible(True)
        self._finalize_btn.setVisible(False)
        self._finalize_btn.setEnabled(True)
        self._stop_btn.setEnabled(True)
        for card in (self.stat_total, self.stat_dem, self.stat_ok):
            card.update_value("—")

    def configure_phases(self, run_phase1=True, run_phase2a=True, run_phase2b=True, run_phase3=True):
        """Shows or hides phase rows based on the phases selected by the user."""
        mapping = {
            "1": run_phase1,
            "2a": run_phase2a,
            "2b": run_phase2b,
            "3": run_phase3,
        }
        for pid, visible in mapping.items():
            if pid in self._phase_rows:
                self._phase_rows[pid].setVisible(visible)

    def configure_for_live(self, non_coi: bool = False):
        self._cycle_timer_card.setVisible(True)
        self._cycle_card.setVisible(True)
        self._mode = "live"
        self._non_coi = non_coi
        ctx = "ProgressPanel"
        title = _tr(ctx, "Analysis in progress: ⚡ Real-Time")
        if non_coi:
            title += " (non-Coding)"
        self._analysis_title_lbl.setText(title)
        self._analysis_title_lbl.setStyleSheet(
            f"font-size:24px; font-weight:600; color:{BLUE};"
        )
        # In non-Coding phases 2b and 3 do not exist: hide them
        for pid in ("2b", "3"):
            if pid in self._phase_rows:
                self._phase_rows[pid].setVisible(not non_coi)
        self._finalize_btn.setVisible(True)

    def configure_for_conventional(self, non_coi: bool = False):
        self._mode = "conventional"
        self._non_coi = non_coi
        ctx = "ProgressPanel"
        title = _tr(ctx, "Analysis in progress: ▶ Conventional")
        if non_coi:
            title += " (non-Coding)"
        self._analysis_title_lbl.setText(title)
        self._analysis_title_lbl.setStyleSheet(
            f"font-size:24px; font-weight:600; color:{TEXT_PRI};"
        )

    def disable_rt_controls(self):
        """Disable Finalize RT on Stop."""
        self._finalize_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)

    def show_post_processing_phases(self):
        pass

    def update_stat_ok(self, n: int, force: bool = False):
        """Updates the Barcodes OK card.

        By default the value only ever increases, to avoid downward flicker while
        a phase reports partial progress. With force=True it sets the exact value
        even if lower — used for the authoritative count after Phase 3, which can
        correct (reduce) the total (e.g. a barcode that passed 2b is rejected)."""
        if force or n > self._ok_current:
            self._ok_current = n
            self.stat_ok.update_value(str(n))

    def update_stat_dem(self, assigned: int, total: int, final: bool = False):
        """Shows assigned reads. The percentage is only shown when
        final=True, once the merge determines the actual count per sample."""
        if final and total > 0:
            pct = min(100, int(assigned / total * 100))
            self.stat_dem.update_value(f"{assigned:,} ({pct}%)")
        else:
            self.stat_dem.update_value(f"{assigned:,}")

    def set_phase(self, phase_id: str, detail=""):
        order = [p for p, _ in self.PHASES]
        idx = order.index(phase_id) if phase_id in order else -1
        for i, pid in enumerate(order):
            if i < idx:
                self._phase_rows[pid].set_state("done")
            elif i == idx:
                self._phase_rows[pid].set_state("current", detail)
            else:
                if self._phase_rows[pid].property("state") not in ("done", "current"):
                    self._phase_rows[pid].set_state("pending")
        self._phase_lbl.setText(f"Phase {phase_id} · {detail}" if detail else f"Phase {phase_id}")

    def update_phase_progress(self, phase_id, value, total, extra=""):
        if phase_id in self._phase_rows:
            self._phase_rows[phase_id].set_progress(value, total, extra)

    def mark_phase_done(self, phase_id, detail=""):
        if phase_id in self._phase_rows:
            self._phase_rows[phase_id].set_state("done", detail)

    def get_ok_value(self) -> int:
        try:
            return int(self.stat_ok._val.text().replace(",", "").strip())
        except (ValueError, AttributeError):
            return 0

    def append_log(self, message: str, level="info"):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        color_map = {"ok": GREEN, "error": RED, "warn": AMBER, "info": BLUE}
        color = color_map.get(level, TEXT_SEC)
        html = (
            f'<span style="color:{TEXT_HINT};">{ts}</span>'
            f'&nbsp;&nbsp;<span style="color:{color};">{message}</span>'
        )
        QtCore.QMetaObject.invokeMethod(
            self._log, "append",
            QtCore.Qt.QueuedConnection,
            QtCore.Q_ARG(str, html)
        )
        if getattr(self, "_logfile_handle", None):
            try:
                self._logfile_handle.write(f"[{ts}] [{level.upper():5s}] {message}\n")
                self._logfile_handle.flush()
            except Exception:
                pass

    @QtCore.pyqtSlot(int)
    def on_total_reads(self, n):
        self.stat_total.update_value(f"{n:,}")

    @QtCore.pyqtSlot(int, int)
    def on_demultiplex_progress(self, used, total):
        self.update_stat_dem(used, total)
        self.update_phase_progress("1", used, total)

    @QtCore.pyqtSlot(int)
    def on_barcode_count(self, n):
        self.update_stat_ok(n)

    @QtCore.pyqtSlot(str)
    def on_log_message(self, msg):
        self.append_log(msg)


# ═══════════════════════════════════════════════════════════════════════════
# PANEL 4: RESULTS
# ═══════════════════════════════════════════════════════════════════════════

class ResultsPanel(BasePanel):
    resetRequested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._outpath = ""

        self._lbl_results_title = make_label("Analysis results", size=19, bold=True)
        self.add(self._lbl_results_title)
        self._sub_lbl = make_label("—", color=TEXT_SEC)
        self.add(self._sub_lbl)

        stats_container = QtWidgets.QWidget()
        stats_layout = QtWidgets.QHBoxLayout(stats_container)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(10)

        self.stat_total = StatCard("Total barcodes", "—")
        self.stat_qc = StatCard("QC barcodes", "—", color=GREEN)
        self.stat_filt = StatCard("Filtered (≤1% N)", "—", color=BLUE)
        self.stat_unresl = StatCard("Unresolved", "—", color=AMBER)

        for card in (self.stat_total, self.stat_qc, self.stat_filt, self.stat_unresl):
            stats_layout.addWidget(card)
        self.add(stats_container)

        detail_row = QtWidgets.QWidget()
        detail_layout = QtWidgets.QHBoxLayout(detail_row)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(12)
        detail_layout.addWidget(self._build_phase_table())
        detail_layout.addWidget(self._build_quality_table())
        self.add(detail_row)

        self.add(hline())

        export_row = QtWidgets.QWidget()
        export_layout = QtWidgets.QHBoxLayout(export_row)
        export_layout.setContentsMargins(0, 0, 0, 0)
        export_layout.setSpacing(10)
        export_layout.setSpacing(12)

        self._btn_qc = QtWidgets.QPushButton("⬇  Open QC barcodes")
        self._btn_qc.setObjectName("secondary_btn")
        self._btn_qc.setFixedHeight(42)
        self._btn_qc.setEnabled(False)

        self._btn_all = QtWidgets.QPushButton("⬇  Open all barcodes")
        self._btn_all.setObjectName("secondary_btn")
        self._btn_all.setFixedHeight(42)
        self._btn_all.setEnabled(False)

        self._btn_xls = QtWidgets.QPushButton("📊  Open summary")
        self._btn_xls.setObjectName("secondary_btn")
        self._btn_xls.setFixedHeight(42)
        self._btn_xls.setEnabled(False)

        self._btn_html = QtWidgets.QPushButton("🌐  Open report")
        self._btn_html.setObjectName("secondary_btn")
        self._btn_html.setFixedHeight(42)
        self._btn_html.setEnabled(False)

        self._btn_folder = QtWidgets.QPushButton("📂  Open folder")
        self._btn_folder.setObjectName("secondary_btn")
        self._btn_folder.setFixedHeight(42)
        self._btn_folder.setEnabled(False)

        export_layout.addWidget(self._btn_qc)
        export_layout.addWidget(self._btn_all)
        export_layout.addWidget(self._btn_xls)
        export_layout.addWidget(self._btn_html)
        export_layout.addWidget(self._btn_folder)
        self.add(export_row)

        self.add_stretch()

        # Footer anchored below scroll area
        self._btn_reset = QtWidgets.QPushButton("🔄   New analysis (clear and restart)")
        self._btn_reset.setObjectName("danger_btn")
        self._btn_reset.setFixedHeight(52)
        self._btn_reset.setStyleSheet(
            f"QPushButton {{ background-color: transparent; color: {TEXT_SEC}; "
            f"border: 1.5px solid {GRAY_LINE}; border-radius: 10px; "
            f"padding: 10px 24px; font-size: 18px; font-weight: 500; }}"
            f"QPushButton:hover {{ background-color: {RED_LT}; color: {RED}; border-color: #F09595; }}"
        )
        self._btn_reset.setEnabled(False)
        footer = QtWidgets.QWidget()
        footer.setStyleSheet(f"background:{GRAY_CARD}; border-top:1px solid {GRAY_CARD};")
        _fl = QtWidgets.QHBoxLayout(footer)
        _fl.setContentsMargins(20, 10, 20, 10)
        _fl.addWidget(self._btn_reset)
        self._footer_widget = footer

    _TABLE_STYLE = f"""
        QTableWidget {{
            background-color: {WHITE};
            gridline-color: {GRAY_LINE};
            border: none;
            font-size: 16px;
            outline: none;
        }}
        QTableWidget::item {{
            padding: 9px 18px;
            color: {TEXT_PRI};
            border: none;
        }}
        QTableWidget::item:alternate {{
            background-color: {GRAY_BG};
        }}
        QTableWidget::item:selected {{
            background-color: {BLUE_LIGHT};
            color: {BLUE};
        }}
        QHeaderView::section {{
            background-color: {BLUE};
            color: {WHITE};
            font-weight: 700;
            font-size: 15px;
            padding: 11px 18px;
            border: none;
            border-right: 1px solid rgba(255,255,255,0.30);
        }}
        QHeaderView::section:first {{
            border-top-left-radius: 8px;
        }}
        QHeaderView::section:last {{
            border-top-right-radius: 8px;
            border-right: none;
        }}
        QHeaderView::section:only-one {{
            border-radius: 8px 8px 0 0;
        }}
    """

    @staticmethod
    def _make_table(rows, col_labels, style):
        t = QtWidgets.QTableWidget(rows, len(col_labels))
        t.setHorizontalHeaderLabels(col_labels)
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setStretchLastSection(True)
        t.horizontalHeader().setMinimumSectionSize(150)
        t.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setFrameShape(QtWidgets.QFrame.NoFrame)
        t.setShowGrid(False)
        t.setStyleSheet(style)
        t.setColumnWidth(0, 300) # column width
        t.verticalHeader().setDefaultSectionSize(36)
        return t

    def _build_phase_table(self):
        frame = QtWidgets.QFrame()
        frame.setObjectName("card")
        frame.setStyleSheet(
            f"QFrame#card {{ border: 1px solid {GRAY_LINE}; border-radius: 8px; }}")
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._phase_table = self._make_table(4, [_tr("ResultsPanel", "Per phase"), _tr("ResultsPanel", "Barcodes")], self._TABLE_STYLE)
        layout.addWidget(self._phase_table)
        return frame

    def _build_quality_table(self):
        frame = QtWidgets.QFrame()
        frame.setObjectName("card")
        frame.setStyleSheet(
            f"QFrame#card {{ border: 1px solid {GRAY_LINE}; border-radius: 8px; }}")
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        _q_style = self._TABLE_STYLE.replace(
            f"background-color: {BLUE};", f"background-color: {GREEN_MID};").replace(
            f"background-color: {BLUE_LIGHT};", f"background-color: {GREEN_LT};").replace(
            f"color: {BLUE};", f"color: {GREEN_MID};")
        self._qual_table = self._make_table(4, [_tr("ResultsPanel", "Quality"), _tr("ResultsPanel", "Seq. No.")], _q_style)
        layout.addWidget(self._qual_table)
        return frame

    def retranslateUi(self):
        ctx = "ResultsPanel"
        self._lbl_results_title.setText(_tr(ctx, "Analysis results"))
        self._btn_qc.setText(_tr(ctx, "⬇  Open QC barcodes"))
        self._btn_all.setText(_tr(ctx, "⬇  Open all barcodes"))
        self._btn_xls.setText(_tr(ctx, "📊  Open summary"))
        self._btn_html.setText(_tr(ctx, "🌐  Open report"))
        self._btn_folder.setText(_tr(ctx, "📂  Open folder"))
        self._btn_reset.setText(_tr(ctx, "🔄   New analysis (clear and restart)"))
        self._phase_table.setHorizontalHeaderLabels([_tr(ctx, "Per phase"), _tr(ctx, "Barcodes")])
        self._qual_table.setHorizontalHeaderLabels([_tr(ctx, "Quality"), _tr(ctx, "Seq. No.")])
        for card in (self.stat_total, self.stat_qc, self.stat_filt, self.stat_unresl):
            card.retranslateUi()

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def populate(self, outpath: str, summary: dict):
        self._outpath = outpath

        try:
            self._btn_qc.clicked.disconnect()
        except TypeError:
            pass
        try:
            self._btn_folder.clicked.disconnect()
        except TypeError:
            pass
        try:
            self._btn_all.clicked.disconnect()
        except TypeError:
            pass
        try:
            self._btn_xls.clicked.disconnect()
        except TypeError:
            pass
        try:
            self._btn_html.clicked.disconnect()
        except TypeError:
            pass

        self._btn_qc.clicked.connect(lambda: self._open_file("consensus_no_errors.fa"))
        self._btn_folder.clicked.connect(self._open_outpath_folder)
        self._btn_all.clicked.connect(lambda: self._open_file("consensus_all.fa"))
        self._btn_xls.clicked.connect(lambda: self._open_file("runsummary.xlsx"))
        self._btn_html.clicked.connect(lambda: self._open_file_root("report.html"))

        _active_style = (
            f"QPushButton {{ background-color: {BLUE_LIGHT}; color: {BLUE}; "
            f"border: 1.5px solid {BLUE_MID}; border-radius: 8px; "
            f"padding: 7px 16px; font-size: 15px; font-weight: 500; }}"
            f"QPushButton:hover {{ background-color: {BLUE_MID}; color: white; }}"
        )
        for btn in (self._btn_qc, self._btn_folder, self._btn_all, self._btn_xls, self._btn_html):
            btn.setEnabled(True)
            btn.setStyleSheet(_active_style)

        try:
            self._btn_reset.clicked.disconnect()
        except TypeError:
            pass
        self._btn_reset.clicked.connect(self._on_reset)
        self._btn_reset.setEnabled(True)

        elapsed = summary.get("elapsed", "—")
        run_name = os.path.basename(outpath) or "run"
        mode_str = summary.get("mode", "")
        mode_label = f" · {mode_str}" if mode_str else ""
        self._sub_lbl.setText(f"Output: {run_name}{mode_label} · completed in {elapsed}")

        _non_coi = summary.get("params", {}).get("non_coi", False)
        self.stat_total.update_value(str(summary.get("total", "—")))
        self.stat_qc.update_value(str(summary.get("qc_ok", "—")))
        self.stat_filt.update_value(str(summary.get("filtered", "—")))
        self.stat_unresl.update_value(str(summary.get("unresolved", "—")))
        self.stat_unresl.setVisible(not _non_coi)
        phase_data = summary.get("by_phase", [
            ("Consensus by length", summary.get("phase2a_n", "—")),
            ("Consensus by similarity", summary.get("phase2b_n", "—")),
            ("Correction by barcode comparisons", summary.get("phase3_n", "—")),
            ("Unresolved", summary.get("unresolved", "—")),
        ])
        if _non_coi:
            phase_data = [(n, v) for n, v in phase_data if n != "Unresolved"]
        self._phase_table.setRowCount(len(phase_data))
        for i, (name, val) in enumerate(phase_data):
            self._phase_table.setItem(i, 0, QtWidgets.QTableWidgetItem(name))
            _item = QtWidgets.QTableWidgetItem(str(val))
            _item.setTextAlignment(QtCore.Qt.AlignCenter)
            self._phase_table.setItem(i, 1, _item)

        qual_data = summary.get("quality", [
            ("No errors, no Ns", summary.get("perfect", "—")),
            ("1–5 indels", summary.get("few_indels", "—")),
            ("6–10 indels", summary.get("mid_indels", "—")),
            (">10 indels", summary.get("many_indels", "—")),
        ])
        if _non_coi:
            qual_data = qual_data[:1]
        self._qual_table.setRowCount(len(qual_data))
        for i, (name, val) in enumerate(qual_data):
            self._qual_table.setItem(i, 0, QtWidgets.QTableWidgetItem(name))
            _item = QtWidgets.QTableWidgetItem(str(val))
            _item.setTextAlignment(QtCore.Qt.AlignCenter)
            self._qual_table.setItem(i, 1, _item)

    def _on_reset(self):
        self._btn_reset.setEnabled(False)
        self._sub_lbl.setText("—")
        self.stat_unresl.setVisible(True)
        for card in (self.stat_total, self.stat_qc, self.stat_filt, self.stat_unresl):
            card.update_value("—")
        self._phase_table.setRowCount(0)
        self._qual_table.setRowCount(0)
        self._outpath = ""
        for btn in (self._btn_qc, self._btn_folder, self._btn_all, self._btn_xls, self._btn_html):
            btn.setEnabled(False)
            btn.setStyleSheet("QPushButton { background-color: transparent; }")
        self.resetRequested.emit()

    def _open_file(self, filename):
        if not self._outpath:
            return
        path = os.path.join(self._outpath, "Main_barcode_results", filename)
        if os.path.exists(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
        else:
            path = os.path.join(self._outpath, filename)
            if os.path.exists(path):
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _open_file_root(self, filename):
        """Open a file directly from the root of the outpath."""
        if not self._outpath:
            return
        path = os.path.join(self._outpath, filename)
        if os.path.exists(path):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _open_outpath_folder(self):
        if not self._outpath:
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._outpath))


# ═══════════════════════════════════════════════════════════════════════════
# LIVE CHART PANEL
# ═══════════════════════════════════════════════════════════════════════════

class _ChartWidget(QtWidgets.QWidget):
    def __init__(self, title, y_label, line_color, line_color2=None,
                 legend1=None, legend2=None, parent=None):
        super().__init__(parent)
        self._src_title = title
        self._src_y_label = y_label
        self._src_legend1 = legend1
        self._src_legend2 = legend2
        self.title = title
        self.y_label = y_label
        self.line_color = QtGui.QColor(line_color)
        self.line_color2 = QtGui.QColor(line_color2) if line_color2 else None
        self.legend1 = legend1
        self.legend2 = legend2
        self._points = []
        self._points2 = []
        self.setMinimumHeight(200)
        self.setMinimumWidth(300)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding)

    def retranslateUi(self):
        ctx = "_ChartWidget"
        self.title = _tr(ctx, self._src_title)
        self.y_label = _tr(ctx, self._src_y_label)
        self.legend1 = _tr(ctx, self._src_legend1) if self._src_legend1 else None
        self.legend2 = _tr(ctx, self._src_legend2) if self._src_legend2 else None
        self.update()

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def add_point(self, x_min: float, y_val: float):
        self._points.append((x_min, y_val))
        self.update()

    def add_point2(self, x_min: float, y_val: float):
        if self.line_color2:
            self._points2.append((x_min, y_val))
            self.update()

    def clear(self):
        self._points = []
        self._points2 = []
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        W, H = self.width(), self.height()
        PAD_L, PAD_R, PAD_T, PAD_B = 58, 18, 28, 38

        p.fillRect(0, 0, W, H, QtGui.QColor("#FFFFFF"))

        p.setPen(QtGui.QColor("#1A1A2E"))
        font_t = QtGui.QFont()
        font_t.setBold(True)
        font_t.setPointSize(9)
        p.setFont(font_t)
        p.drawText(QtCore.QRect(PAD_L, 4, W - PAD_L - PAD_R, PAD_T - 4),
                   QtCore.Qt.AlignCenter, self.title)

        chart_w = W - PAD_L - PAD_R
        chart_h = H - PAD_T - PAD_B

        p.setPen(QtGui.QPen(QtGui.QColor("#CCCCCC"), 1))
        p.drawRect(PAD_L, PAD_T, chart_w, chart_h)

        all_pts = self._points + self._points2
        if not all_pts:
            p.setPen(QtGui.QColor("#AAAAAA"))
            font_e = QtGui.QFont()
            font_e.setPointSize(8)
            p.setFont(font_e)
            p.drawText(QtCore.QRect(PAD_L, PAD_T, chart_w, chart_h),
                       QtCore.Qt.AlignCenter, "No data yet")
            return

        xs = [pt[0] for pt in all_pts]
        ys = [pt[1] for pt in all_pts]
        x_min_v, x_max_v = 0, max(xs) if xs else 1
        y_min_v, y_max_v = 0, max(ys) if max(ys) > 0 else 1
        x_range = x_max_v - x_min_v if x_max_v != x_min_v else 1.0
        y_range = y_max_v - y_min_v if y_max_v != y_min_v else 1.0

        def to_px(xv, yv):
            px = PAD_L + (xv - x_min_v) / x_range * chart_w
            py = PAD_T + chart_h - (yv - y_min_v) / y_range * chart_h
            return px, py

        font_s = QtGui.QFont()
        font_s.setPointSize(7)
        p.setFont(font_s)

        for i in range(5):
            yv = y_min_v + y_range * i / 4
            _, py = to_px(x_min_v, yv)
            p.setPen(QtGui.QPen(QtGui.QColor("#EEEEEE"), 1, QtCore.Qt.DashLine))
            p.drawLine(int(PAD_L), int(py), int(PAD_L + chart_w), int(py))
            p.setPen(QtGui.QColor("#666666"))
            p.drawText(QtCore.QRect(0, int(py) - 8, PAD_L - 4, 16),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, f"{int(yv):,}")

        n_xticks = min(5, max(len(self._points), 1))
        for i in range(n_xticks):
            xv = x_min_v + x_range * i / max(n_xticks - 1, 1)
            px, _ = to_px(xv, y_min_v)
            p.setPen(QtGui.QPen(QtGui.QColor("#EEEEEE"), 1, QtCore.Qt.DashLine))
            p.drawLine(int(px), int(PAD_T), int(px), int(PAD_T + chart_h))
            p.setPen(QtGui.QColor("#666666"))
            p.drawText(QtCore.QRect(int(px) - 20, H - PAD_B + 4, 40, 14),
                       QtCore.Qt.AlignCenter, f"{xv:.2f}h")

        def draw_series(points, color):
            if not points:
                return
            pen = QtGui.QPen(color, 2)
            p.setPen(pen)
            poly = QtGui.QPolygonF()
            for xv, yv in points:
                px, py = to_px(xv, yv)
                poly.append(QtCore.QPointF(px, py))
            p.drawPolyline(poly)
            p.setBrush(QtGui.QBrush(color))
            p.setPen(QtCore.Qt.NoPen)
            for xv, yv in points:
                px, py = to_px(xv, yv)
                p.drawEllipse(QtCore.QPointF(px, py), 3.5, 3.5)

        if self.line_color2:
            draw_series(self._points2, self.line_color2)
        draw_series(self._points, self.line_color)

        if self.legend1 or self.legend2:
            font_leg = QtGui.QFont()
            font_leg.setPointSize(7)
            p.setFont(font_leg)
            fm_leg = QtGui.QFontMetrics(font_leg)
            row_h  = max(fm_leg.height(), 10)
            lx     = PAD_L + 6
            ly     = PAD_T + 6
            dot_r  = 4
            gap    = row_h + 4
            for color, label in [
                (self.line_color, self.legend1),
                (self.line_color2, self.legend2),
            ]:
                if not label or not color:
                    continue
                cy = ly + row_h / 2
                p.setBrush(QtGui.QBrush(color))
                p.setPen(QtCore.Qt.NoPen)
                p.drawEllipse(
                    QtCore.QRectF(lx, cy - dot_r, dot_r * 2, dot_r * 2)
                )
                p.setPen(QtGui.QColor("#444444"))
                p.drawText(
                    QtCore.QRect(lx + dot_r * 2 + 4, ly, 130, row_h),
                    QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                    label
                )
                ly += gap

        p.save()
        p.translate(10, PAD_T + chart_h // 2)
        p.rotate(-90)
        p.setPen(QtGui.QColor("#444444"))
        font_ax = QtGui.QFont()
        font_ax.setPointSize(7)
        p.setFont(font_ax)
        p.drawText(QtCore.QRect(-50, -10, 100, 20),
                   QtCore.Qt.AlignCenter, self.y_label)
        p.restore()
        p.setPen(QtGui.QColor("#666666"))
        p.drawText(QtCore.QRect(PAD_L, H - 14, chart_w, 14),
                   QtCore.Qt.AlignCenter, "Time (h)")


class DetachedChartsWindow(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowTitle("Real-Time charts — ONTbarcoder")
        self.setMinimumSize(820, 560)
        self.resize(900, 640)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self._chart_reads = _ChartWidget(
            "Number of reads", "Reads", BLUE, BLUE_MID,
            legend1="Demultiplexed", legend2="Total")
        self._chart_reads.setMinimumHeight(220)
        self._chart_reads.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        sep.setStyleSheet(f"color:{GRAY_LINE};")

        self._chart_ok = _ChartWidget(
            "Number of barcodes", "QC barcodes", GREEN)
        self._chart_ok.setMinimumHeight(220)
        self._chart_ok.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

        layout.addWidget(self._chart_reads, 1)
        layout.addWidget(sep)
        layout.addWidget(self._chart_ok, 1)

    def sync_from(self, reads_widget, ok_widget):
        self._chart_reads._points  = list(reads_widget._points)
        self._chart_reads._points2 = list(reads_widget._points2)
        self._chart_ok._points     = list(ok_widget._points)
        self._chart_ok._points2    = list(ok_widget._points2)
        self._chart_reads.update()
        self._chart_ok.update()

    def add_point(self, x_min, n_dem, n_ok, n_total=0):
        self._chart_reads.add_point(x_min, n_dem)
        if n_total > 0:
            self._chart_reads.add_point2(x_min, n_total)
        self._chart_ok.add_point(x_min, n_ok)


class LiveChartPanel(BasePanel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._t0 = None
        self._timeline = []
        self._bar_window = None
        self._detach_window = None

        # ── Header: title + buttons in the same row ───────────────────────
        header_w = QtWidgets.QWidget()
        header_lay = QtWidgets.QHBoxLayout(header_w)
        header_lay.setContentsMargins(0, 0, 0, 0)
        header_lay.setSpacing(12)

        title_col = QtWidgets.QVBoxLayout()
        title_col.setSpacing(2)
        self._lbl_chart_title = make_label("Real-Time charts", size=19, bold=True)
        self._lbl_chart_desc = make_label(
            "The graphs update automatically while the analysis runs.",
            color=TEXT_SEC)
        title_col.addWidget(self._lbl_chart_title)
        title_col.addWidget(self._lbl_chart_desc)
        header_lay.addLayout(title_col, 1)

        # ── Self-styled button (soft green, darker hover) ───────────
        self._btn_bar_chart = QtWidgets.QPushButton("📊 Reads per sample")
        self._btn_bar_chart.setFixedHeight(34)
        self._btn_bar_chart.setToolTip(
            "Show or hide a floating window with the number of reads\n"
            "assigned to each sample, in descending order.")
        self._btn_bar_chart.setStyleSheet(f"""
            QPushButton {{
                background-color: {GREEN_LT};
                color: {GREEN};
                border: 1px solid {GREEN_MID};
                border-radius: 8px;
                padding: 6px 14px;
                font-size: 15px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {GREEN_MID};
                color: white;
                border-color: {GREEN};
            }}
            QPushButton:pressed {{
                background-color: {GREEN};
                color: white;
            }}
        """)
        self._btn_bar_chart.clicked.connect(self._toggle_bar_window)
        header_lay.addWidget(self._btn_bar_chart, 0, QtCore.Qt.AlignBottom)

        self._btn_detach = QtWidgets.QPushButton("⧉ Floating window")
        self._btn_detach.setFixedHeight(34)
        self._btn_detach.setToolTip(
            "Open the reads and barcodes graphs in a separate window.")
        self._btn_detach.setStyleSheet(f"""
            QPushButton {{
                background-color: {BLUE_LIGHT};
                color: {BLUE};
                border: 1px solid {BLUE_MID};
                border-radius: 8px;
                padding: 6px 14px;
                font-size: 15px;
                font-weight: 500;
            }}
            QPushButton:hover {{
                background-color: {BLUE_MID};
                color: white;
                border-color: {BLUE};
            }}
            QPushButton:pressed {{
                background-color: {BLUE};
                color: white;
            }}
        """)
        self._btn_detach.clicked.connect(self._toggle_detach)
        header_lay.addWidget(self._btn_detach, 0, QtCore.Qt.AlignBottom)

        self.add(header_w)

        # Internal bar widget (data source for floating window)
        self._bar_chart_widget = _SampleBarChartWidget()
        self._bar_chart_widget.hide()

        self.add(hline())

        self._chart_reads = _ChartWidget(
            "Number of reads",
            "Reads", BLUE, BLUE_MID,
            legend1="Demultiplexed", legend2="Total")
        self._chart_reads.setMinimumHeight(180)
        self._chart_reads.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding)
        self.add(self._chart_reads, stretch=1)

        self.add(hline())

        self._chart_ok = _ChartWidget(
            "Number of barcodes",
            "QC barcodes", GREEN)
        self._chart_ok.setMinimumHeight(180)
        self._chart_ok.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding)
        self.add(self._chart_ok, stretch=1)

    def retranslateUi(self):
        ctx = "LiveChartPanel"
        self._lbl_chart_title.setText(_tr(ctx, "Real-Time charts"))
        self._lbl_chart_desc.setText(_tr(ctx, "Graphs update automatically while the analysis runs."))
        self._btn_bar_chart.setText(_tr(ctx, "📊 Reads per sample"))
        self._btn_detach.setText(_tr(ctx, "⧉ Floating window"))
        self._chart_reads.retranslateUi()
        self._chart_ok.retranslateUi()

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def start_session(self):
        self._t0 = time.time()
        self._timeline = []
        self._chart_reads.clear()
        self._chart_ok.clear()
        # Origin point (0, 0) for the curve to start from the Y axis
        self._chart_reads.add_point(0.0, 0)
        self._chart_reads.add_point2(0.0, 0)
        self._chart_ok.add_point(0.0, 0)

    def record(self, n_dem: int, n_ok: int, n_total: int = 0, cycle: int = 0):
        if self._t0 is None:
            self._t0 = time.time()
        elapsed_min = (time.time() - self._t0) / 60.0
        # Charts plot the X axis in hours for easier reading on long runs;
        # the timeline record below keeps minutes for data fidelity.
        elapsed_hr = elapsed_min / 60.0
        self._chart_reads.add_point(elapsed_hr, n_dem)
        if n_total > 0:
            self._chart_reads.add_point2(elapsed_hr, n_total)
        self._chart_ok.add_point(elapsed_hr, n_ok)
        if self._detach_window and self._detach_window.isVisible():
            self._detach_window.add_point(elapsed_hr, n_dem, n_ok, n_total)
        self._timeline.append({
            "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "min": round(elapsed_min, 2),
            "total": n_total,
            "dem": n_dem,
            "ok": n_ok,
            "cycle": cycle,
        })

    def export_charts(self, outpath: str):
        os.makedirs(outpath, exist_ok=True)
        for name, widget in [
            ("chart_reads.png", self._chart_reads),
            ("chart_barcodes_ok.png", self._chart_ok),
        ]:
            px = widget.grab()
            px.save(os.path.join(outpath, name), "PNG")
        return self._timeline

    def update_sample_bar_chart(self, sampleids: dict):
        """Updates the per-sample bar graph with the data from the last cycle."""
        self._bar_chart_widget.set_data(sampleids)
        if self._bar_window and self._bar_window.isVisible():
            self._bar_window.update_chart(sampleids)

    def _toggle_detach(self):
        if self._detach_window is None:
            self._detach_window = DetachedChartsWindow(self)
        if self._detach_window.isVisible():
            self._detach_window.hide()
            self._btn_detach.setText("⧉ Floating window")
        else:
            self._detach_window.sync_from(self._chart_reads, self._chart_ok)
            self._detach_window.show()
            self._detach_window.raise_()
            self._btn_detach.setText("⧉ Floating window")

    def _toggle_bar_window(self):
        """Show or hide the bar chart floating window per sample."""
        if self._bar_window is None:
            self._bar_window = SampleBarChartWindow(self)
            self._bar_window.set_data(self._bar_chart_widget._data)

        if self._bar_window.isVisible():
            self._bar_window.hide()
            self._btn_bar_chart.setText("📊 Reads per sample")
        else:
            self._bar_window.set_data(self._bar_chart_widget._data)
            self._bar_window.show()
            self._bar_window.raise_()
            self._btn_bar_chart.setText("📊 Reads per sample")


# ═══════════════════════════════════════════════════════════════════════════
# BAR GRAPH BY SAMPLE
# ═══════════════════════════════════════════════════════════════════════════

class _SampleBarChartWidget(QtWidgets.QWidget):
    """
    HORIZONTAL bar graph per sample, ordered descending.
    Each row has a fixed height to ensure readability regardless of the
    number of samples. It is designed to live inside a QScrollArea.
    """

    ROW_H   = 28    # height of each row (bar + padding)
    PAD_T   = 8     # top margin
    PAD_B   = 8     # bottom margin
    PAD_L   = 16    # outer left margin
    PAD_R   = 80    # right margin for numerical value
    LBL_W   = 160   # reserved width for sample name
    GAP     = 6     # separation between label and bar

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = {}
        self._sorted_items = []
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed)
        self.setMinimumWidth(500)
        self.setMouseTracking(True)
        self._recompute_height()

    def _recompute_height(self):
        n = max(len(self._data), 1)
        h = self.PAD_T + n * self.ROW_H + self.PAD_B
        self.setFixedHeight(h)

    def set_data(self, sampleids: dict):
        self._data = dict(sampleids) if sampleids else {}
        self._sorted_items = sorted(self._data.items(), key=lambda x: -x[1])
        self._recompute_height()
        self.update()

    def mouseMoveEvent(self, event):
        y = event.y()
        idx = (y - self.PAD_T) // self.ROW_H
        if 0 <= idx < len(self._sorted_items):
            sample, _ = self._sorted_items[idx]
            fm = QtGui.QFontMetrics(QtGui.QFont())
            elided = fm.elidedText(sample, QtCore.Qt.ElideRight, self.LBL_W - 4)
            if elided != sample:
                QtWidgets.QToolTip.showText(event.globalPos(), sample, self)
            else:
                QtWidgets.QToolTip.hideText()
        else:
            QtWidgets.QToolTip.hideText()
        super().mouseMoveEvent(event)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, QtGui.QColor("#FFFFFF"))

        if not self._data:
            p.setPen(QtGui.QColor("#AAAAAA"))
            f = QtGui.QFont(); f.setPointSize(9); p.setFont(f)
            p.drawText(QtCore.QRect(0, 0, W, H),
                       QtCore.Qt.AlignCenter, "No data yet")
            return

        sorted_items = self._sorted_items
        max_val = max(v for _, v in sorted_items) if sorted_items else 1

        # Area available for the bar
        bar_area_x = self.PAD_L + self.LBL_W + self.GAP
        bar_area_w = W - bar_area_x - self.PAD_R

        # Fonts
        font_lbl = QtGui.QFont()
        font_lbl.setPointSize(9)
        font_lbl.setBold(False)

        font_val = QtGui.QFont()
        font_val.setPointSize(9)
        font_val.setBold(True)

        font_rank = QtGui.QFont()
        font_rank.setPointSize(7)
        font_rank.setBold(False)

        for idx, (sample, count) in enumerate(sorted_items):
            row_top = self.PAD_T + idx * self.ROW_H
            row_cy  = row_top + self.ROW_H // 2   # vertical center of row

            # Very soft alternating background
            if idx % 2 == 0:
                p.fillRect(0, row_top, W, self.ROW_H, QtGui.QColor("#F9F9F8"))

            # ── Sample label (aligned to the left of the label area) ──
            p.setFont(font_lbl)
            p.setPen(QtGui.QColor(TEXT_PRI))
            lbl_rect = QtCore.QRect(
                self.PAD_L, row_top,
                self.LBL_W, self.ROW_H
            )
            # Delete the name if it doesn't fit
            fm = QtGui.QFontMetrics(font_lbl)
            elided = fm.elidedText(sample, QtCore.Qt.ElideRight, self.LBL_W - 4)
            p.drawText(lbl_rect,
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                       elided)

            # ── Horizontal bar ─────────────────────────────────────────────
            bar_h_px  = max(1, self.ROW_H - 10)   # bar height within the row
            bar_w_px  = int((count / max_val) * bar_area_w) if max_val > 0 else 0
            bar_x     = bar_area_x
            bar_y     = row_cy - bar_h_px // 2

            # Horizontal gradient (lighter → darker)
            if bar_w_px > 0:
                grad = QtGui.QLinearGradient(bar_x, 0, bar_x + bar_w_px, 0)
                grad.setColorAt(0.0, QtGui.QColor(BLUE_LIGHT))
                grad.setColorAt(1.0, QtGui.QColor(BLUE_MID))
                p.setBrush(QtGui.QBrush(grad))
                p.setPen(QtCore.Qt.NoPen)
                p.drawRoundedRect(bar_x, bar_y, bar_w_px, bar_h_px, 3, 3)

            # Guide line to the edge of the bar area (very light gray)
            p.setPen(QtGui.QPen(QtGui.QColor("#EEEEEE"), 1))
            p.drawLine(bar_x, row_cy, bar_x + bar_area_w, row_cy)

            # ── Numeric value (to the right of the bar, always visible) ───
            val_x = bar_area_x + bar_area_w + 6
            val_rect = QtCore.QRect(val_x, row_top, self.PAD_R - 8, self.ROW_H)
            p.setFont(font_val)
            p.setPen(QtGui.QColor(BLUE))
            p.drawText(val_rect,
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                       f"{count:,}")

        p.end()


class SampleBarChartWindow(QtWidgets.QDialog):
    """Floating window with the bar graph of reads per sample."""

    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self.setWindowTitle("Reads assigned per sample")
        self.setMinimumSize(640, 420)
        self.resize(820, 600)
        self.setStyleSheet(f"QDialog {{ background-color: {GRAY_CARD}; }}")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        # ── Header ───────────────────────────── ─────────────────────────────
        header_row = QtWidgets.QHBoxLayout()
        title_lbl = make_label("Reads assigned per sample", size=16, bold=True)
        self._hint_lbl = make_label("", size=17, color=TEXT_SEC)
        header_row.addWidget(title_lbl)
        header_row.addStretch()
        header_row.addWidget(self._hint_lbl)
        layout.addLayout(header_row)

        # Descriptive subtag
        sub_lbl = make_label(
            "Ordered from largest to smallest. The bar is proportional to the number of reads.",
            size=15, color=TEXT_HINT)
        layout.addWidget(sub_lbl)

        layout.addWidget(hline())

        # ── Column header ─────────────────────── ───────────────────────
        col_header = QtWidgets.QWidget()
        col_header.setFixedHeight(22)
        col_header.setStyleSheet(f"background: {GRAY_BG};")
        col_lay = QtWidgets.QHBoxLayout(col_header)
        col_lay.setContentsMargins(
            _SampleBarChartWidget.PAD_L, 0,
            _SampleBarChartWidget.PAD_R, 0)
        col_lay.setSpacing(0)

        lbl_sample = make_label("Sample", size=11, color=TEXT_HINT)
        lbl_sample.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        lbl_sample.setFixedWidth(_SampleBarChartWidget.LBL_W)
        col_lay.addWidget(lbl_sample)
        col_lay.addSpacing(_SampleBarChartWidget.GAP)
        col_lay.addWidget(make_label("Reads", size=11, color=TEXT_HINT), 1)
        layout.addWidget(col_header)

        # ── Scroll area with the graph ──────────────────── ────────────────────
        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)

        self._chart = _SampleBarChartWidget()
        self._scroll.setWidget(self._chart)
        layout.addWidget(self._scroll, 1)

        layout.addWidget(hline())

        # ── Foot ───────────────────────────────────────────────────────────────
        foot_row = QtWidgets.QHBoxLayout()
        foot_row.addStretch()
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.setObjectName("secondary_btn")
        close_btn.setFixedHeight(32)
        close_btn.setFixedWidth(90)
        close_btn.clicked.connect(self.hide)
        foot_row.addWidget(close_btn)
        layout.addLayout(foot_row)

    def set_data(self, sampleids: dict):
        self._chart.set_data(sampleids)
        n     = len(sampleids) if sampleids else 0
        total = sum(sampleids.values()) if sampleids else 0
        self._hint_lbl.setText(
            f"{n} samples · {total:,} assigned reads" if n else "No data"
        )

    def update_chart(self, sampleids: dict):
        self.set_data(sampleids)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN WINDOW
# ═══════════════════════════════════════════════════════════════════════════

def _phase_callback(method):
    """Decorador para los callbacks de fin de fase del pipeline (QThread → señal).

    Estos callbacks hacen I/O y parsing pesado sin protección. Si uno lanza una
    excepción no capturada en modo RT (tiempo real), se propagaba al event-loop de
    Qt y NADIE reseteaba ``_live_consensus_running`` → todos los ciclos RT futuros
    quedaban congelados en silencio (``_live_maybe_run_consensus`` retornaba temprano
    para siempre) mientras la UI seguía aparentando estar activa.

    Este wrapper captura cualquier fallo del callback y:
      • RT (no finalizando): libera el guard del ciclo para que el SIGUIENTE ciclo
        pueda ejecutarse, registra el error y deja el sondeo de FASTQs vivo.
      • Convencional: marca el análisis como inactivo para que la UI no parezca
        colgada, y registra el error.
    El guard interno de cada callback (``if self._stopped: return``) se conserva.
    """
    @functools.wraps(method)
    def _wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            print(f"[ONTbarcoder] {method.__name__} falló:\n{tb}", file=sys.stderr)
            try:
                self._panel_progress.append_log(
                    f"  ✗ Error in {method.__name__}: {exc}", "error")
            except Exception:
                pass
            # Liberar el guard del ciclo (inocuo en modo convencional, donde nunca
            # se activa) para no congelar los ciclos RT posteriores.
            self._live_consensus_running = False
            try:
                if self._is_live() and not getattr(self, "_live_finalizing", False):
                    self._panel_progress._stop_cycle_timer()
                    self._panel_progress.set_phase("1", "Waiting new FASTQs…")
                else:
                    self._analysis_active = False
                    self._panel_progress.set_phase("1", "Stopped (error)")
            except Exception:
                pass
            return None
    return _wrapper


class MainWindow(QtWidgets.QMainWindow):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ONTbarcoder")
        # Preferred / minimum size, but never larger than what actually fits on
        # the current screen.  With UI auto-scaling the *logical* screen can be
        # smaller than these px values, so a fixed minimum would push the window
        # off-screen (behind the taskbar).  Clamp both to the available area.
        self._apply_initial_geometry(pref_w=1320, pref_h=1000,
                                     min_w=1280, min_h=920)

        # Icon in title bar and taskbar
        for icon_name in ("icon.ico",):
            icon_path = os.path.join(_get_base_dir(), icon_name)
            if os.path.isfile(icon_path):
                self.setWindowIcon(QtGui.QIcon(icon_path))
                break

        self._params = {}
        self._runmode = "1"
        self._fastq = ""
        self._demfile = ""
        self._outpath = ""
        self._live_params = {}
        self._run_start = None
        self._analysis_active = False

        # Variables for analysis
        self.worker_prep = None
        self.selectlenscounter = 0
        self.selectlens = []
        self._consensus_first_call = True
        self.con200trans = {}
        self.con200length = {}
        self.con200barcodes = {}
        self.mixinfo_all = {}
        self.con200cov = {}
        self.con200flags = {}
        self.ngoodbarcodescounter = 0
        self.con200goodn = 0
        self.con200errn = 0
        self.cov2a_counts = {}
        self.n90goodn = 0
        self.n90errn = 0
        self.nfixed = 0
        self.nfinal = 0
        self.nerr = 0
        self.nsinfinalbarcodes = 0
        self.nfilteredbarcodes = 0
        self.nperfectbarcodes = 0
        self.n1to5errbarcodes = 0
        self.n6to10errbarcodes = 0
        self.n11to15errbarcodes = 0
        self.nover16errbarcodes = 0
        self.sampleids = {}
        self.corlist = []
        self._live_ok_floor = 0
        self._live_finalizing = False
        self._live_provisional_good = set()
        self.n90trans = {}
        self.n90length = {}
        self.n90barcodes = {}
        self.n90cov = {}
        self.n90flags = {}
        self.errbarcodeset = {}
        self.hapiddict = {}
        self.seqdict = {}
        self._phase3_row = 0
        self.dirdict = {}
        self.nsampledemultiplexed5 = 0
        self.nseqspasslen = 0
        self.totalseqs = 0
        self.nseqsfordemultiplexing = 0
        self.resultlist = []
        self.queue = None
        self.pool = None
        self.timer = None
        self.sumprogress = [0, 0, 0, 0]
        self.mymergedatasets = None
        self.mycheckmsa = None
        self.mycounterdemreads = None
        self.wb = None
        
        # Variables for flow control in RT
        self._live_cycle_in_progress = False
        self._live_cycle_complete = False
        self._live_current_cycle = 0

        self._build_ui()
        self._connect_signals()
        self._restore_geometry()
        self.show()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        central.setObjectName("root_bg")
        self.setCentralWidget(central)

        root_layout = QtWidgets.QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._topbar = TopBar()
        root_layout.addWidget(self._topbar)

        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self._sidebar = SidebarWidget()
        content_layout.addWidget(self._sidebar)

        self._stack = QtWidgets.QStackedWidget()
        content_layout.addWidget(self._stack, 1)
        root_layout.addWidget(content, 1)

        self._panel_setup           = SetupPanel()
        self._panel_params          = ParamsPanel()
        self._panel_progress        = ProgressPanel()
        self._panel_live_chart      = LiveChartPanel()
        self._panel_results         = ResultsPanel()
        self._panel_compare         = ComparePanel()
        self._panel_blast           = BlastPanel()
        self._panel_fastq_inspector = FastqInspectorPanel()
        self._panel_fasta_tools     = FastaToolsPanel()
        self._panel_notes           = NotesPanel()

        # SetupPanel manages its own footer internally (outer layout)
        # ParamsPanel uses BasePanel (QScrollArea), that's why it needs external wrap
        def _wrap_with_footer(panel):
            container = QtWidgets.QWidget()
            vl = QtWidgets.QVBoxLayout(container)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(0)
            vl.addWidget(panel, 1)
            if hasattr(panel, '_footer_widget'):
                vl.addWidget(panel._footer_widget)
            return container

        self._setup_container   = self._panel_setup   # It already has a footer in its outer layout
        self._params_container  = _wrap_with_footer(self._panel_params)
        self._results_container = _wrap_with_footer(self._panel_results)

        for panel in (
            self._setup_container, self._params_container,
            self._panel_progress, self._panel_live_chart,
            self._results_container, self._panel_compare,
            self._panel_blast, self._panel_fastq_inspector,
            self._panel_fasta_tools, self._panel_notes,
        ):
            self._stack.addWidget(panel)

        self._panel_map = {
            "setup": 0, "params": 1, "progress": 2,
            "live_chart": 3, "results": 4, "compare": 5,
            "blast": 6, "fastq_inspector": 7, "fasta_tools": 8,
            "notes": 9,
        }

    def _connect_signals(self):
        self._sidebar.panelRequested.connect(self._switch_panel)
        self._panel_setup.readyToContinue.connect(self._on_setup_done)
        self._panel_params.runRequested.connect(self._start_analysis)
        self._panel_progress.stopRequested.connect(self._stop_analysis)
        self._panel_progress.finalizeRequested.connect(self._finalize_live)
        self._panel_compare.compareRequested.connect(self._start_comparison)
        self._panel_blast.blastRequested.connect(self._start_blast)
        self._panel_results.resetRequested.connect(self._on_reset_analysis)
        self._topbar.languageChanged.connect(self._on_language_changed)
        self._topbar.aboutRequested.connect(self._show_about)

        # Lock panels until user configures input files
        for key in ("params", "progress", "results"):
            self._sidebar.lock_item(key)

    def _on_language_changed(self, lang):
        panels = [
            self._sidebar,
            self._panel_setup,
            self._panel_params,
            self._panel_progress,
            self._panel_live_chart,
            self._panel_results,
            self._panel_compare,
            self._panel_blast,
            self._topbar,
        ]
        set_language(lang, panels)

    def retranslateUi(self):
        self.setWindowTitle(_tr("MainWindow", "ONTbarcoder"))

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def closeEvent(self, event):
        if self._analysis_active:
            reply = QtWidgets.QMessageBox.question(
                self, "Quit ONTbarcoder",
                "An analysis is currently running.\nAre you sure you want to quit?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
            # Stop the pipeline cleanly: terminates the process Pool, kills the
            # dorado subprocess tree and quits all running QThreads. Without this
            # the GUI would close but leave orphaned worker/dorado processes, and
            # destroying a running QThread can abort the interpreter.
            try:
                self._stop_analysis()
            except Exception:
                pass

        # Stop the BLAST / comparison workers too if they are still running, so
        # they are not destroyed mid-run.
        for attr in ("blast_worker", "comp_worker"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    if w.isRunning():
                        if hasattr(w, "stop"):
                            w.stop()
                        w.quit()
                        w.wait(500)
                except Exception:
                    pass

        QtCore.QSettings("Tovar", "ONTbarcoder").setValue(
            "geometry", self.saveGeometry()
        )
        event.accept()

    def _current_screen(self):
        scr = None
        try:
            scr = self.screen()
        except Exception:
            scr = None
        return scr or QtWidgets.QApplication.primaryScreen()

    def _apply_initial_geometry(self, pref_w, pref_h, min_w, min_h):
        """Size and centre the window so it always fits the available screen
        area (i.e. excluding the taskbar), even when UI scaling shrinks the
        usable logical resolution."""
        scr = self._current_screen()
        avail = scr.availableGeometry() if scr is not None else None
        if avail is not None:
            # The minimum must not exceed the available area or the window can
            # never shrink enough to fit.
            min_w = min(min_w, avail.width())
            min_h = min(min_h, avail.height())
            pref_w = max(min_w, min(pref_w, avail.width()))
            pref_h = max(min_h, min(pref_h, avail.height()))
        self.setMinimumSize(min_w, min_h)
        self.resize(pref_w, pref_h)
        if avail is not None:
            self.move(avail.x() + (avail.width()  - pref_w) // 2,
                      avail.y() + (avail.height() - pref_h) // 2)

    def _clamp_to_screen(self):
        """Shrink and nudge the window so it lies fully inside the available
        screen area.  Used after restoring a possibly stale saved geometry."""
        scr = self._current_screen()
        if scr is None:
            return
        avail = scr.availableGeometry()
        w = min(max(self.width(),  self.minimumWidth()),  avail.width())
        h = min(max(self.height(), self.minimumHeight()), avail.height())
        if w != self.width() or h != self.height():
            self.resize(w, h)
        frame = self.frameGeometry()
        x, y = frame.x(), frame.y()
        if frame.right()  > avail.right():
            x = avail.right()  - frame.width()
        if frame.bottom() > avail.bottom():
            y = avail.bottom() - frame.height()
        x = max(x, avail.left())
        y = max(y, avail.top())
        self.move(x, y)

    def _restore_geometry(self):
        geom = QtCore.QSettings("Tovar", "ONTbarcoder").value("geometry")
        if geom:
            self.restoreGeometry(geom)
        # A geometry saved on another monitor / scaling factor may no longer
        # fit; keep the window on-screen and clear of the taskbar.
        self._clamp_to_screen()

    def _switch_panel(self, key: str):
        if key in self._panel_map:
            # Do not navigate to locked panels from the sidebar
            if self._sidebar._states.get(key) == "locked":
                return
            self._stack.setCurrentIndex(self._panel_map[key])
            self._sidebar.set_active(key)

    def _on_setup_done(self, runmode: str, fastq: str, demfile: str, live_params: dict):
        if hasattr(self._panel_params, '_run_btn'):
            self._panel_params._run_btn.setEnabled(True)
            self._panel_params._run_btn.setToolTip("")
            self._panel_params._run_btn.setStyleSheet(
                f"QPushButton {{ background-color: {BLUE}; color: white; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
                f"QPushButton:hover {{ background-color: #0C4A82; }}"
            )
        # Unlock all panels upon completion of file setup
        for key in ("params", "progress", "results"):
            self._sidebar.unlock_item(key)
        self._runmode = runmode
        self._fastq = fastq
        self._demfile = demfile
        self._gencode_scan_cache = None  # rebuilt from this run's demfile
        self._live_params = live_params
        self._panel_params.configure_for_mode(runmode)
        # Surface any per-sample genetic code from the CSV next to the combobox.
        _gc = self._scan_demfile_gencodes()
        self._panel_params.set_gencode_csv_status(
            _gc["n_with_code"], _gc["total_named"], _gc["by_table"])
        self._sidebar.mark_done("setup")
        self._switch_panel("params")

    def _show_about(self):
        dlg = AboutDialog(self)
        dlg.exec_()

    def _is_live(self) -> bool:
        return self._runmode != "1"

    def _start_analysis(self, params: dict):
        self._params = params

        # ── Strict per-sample genetic code check ─────────────────────────────
        # If the CSV carries a per-sample genetic code (trailing column) for SOME
        # samples, it must carry one for ALL of them — otherwise the run is blocked
        # so the user does not unknowingly fall back to the global menu code. Skipped
        # in non-Coding mode (translation validation is disabled there).
        if not params.get("non_coi", False):
            _gc = self._scan_demfile_gencodes()
            if _gc["has_any"] and _gc["missing"]:
                _miss = _gc["missing"]
                _shown = ", ".join(_miss[:10]) + ("…" if len(_miss) > 10 else "")
                QtWidgets.QMessageBox.warning(
                    self,
                    _tr("MainWindow", "Incomplete genetic codes"),
                    _tr("MainWindow",
                        "The CSV assigns a genetic code to some samples but not all. "
                        "In per-sample mode every sample must have its own code.\n\n"
                        "{n} sample(s) without a code: {names}\n\n"
                        "Add the genetic code (last column) to those rows, or remove "
                        "it from all rows to use the global code from "
                        "Parameters → General.").format(n=len(_miss), names=_shown),
                )
                self._analysis_active = False
                return

        self._stopped = False
        self._analysis_active = True
        self._run_start = time.time()
        # Limita la concurrencia a los núcleos físicos (evita la caída de
        # rendimiento por sobre-suscripción). Se reescribe en params para que
        # tanto el demultiplexado (Pool de procesos) como las fases de consenso
        # (ThreadPool) usen el mismo valor efectivo.
        n_threads = _ont_mp.optimal_worker_count(params.get('n_threads', 4))
        params['n_threads'] = n_threads
        _ont_mp.set_threads(n_threads)

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        mode_prefix = "rt" if self._is_live() else "conv"
        default_folder_name = f"ont-barcoder_{ts}_{mode_prefix}"
        program_dir = _get_base_dir()
        default_outpath = os.path.join(program_dir, "output", default_folder_name)

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(_tr("MainWindow", "Output folder"))
        dlg.setMinimumWidth(480)
        dlg.setStyleSheet(f"""
            QDialog {{
                background-color: {GRAY_CARD};
            }}
            QLabel {{
                color: {TEXT_PRI};
                background-color: transparent;
            }}
            QRadioButton {{
                color: {TEXT_PRI};
                background-color: transparent;
                font-size: 15px;
                padding: 6px 0;
            }}
            QRadioButton::indicator {{
                width: 16px;
                height: 16px;
            }}
            QPushButton {{
                border-radius: 8px;
                padding: 8px 20px;
                font-size: 15px;
                font-weight: 500;
            }}
            #dlg_ok_btn {{
                background-color: {BLUE};
                color: white;
                border: none;
            }}
            #dlg_ok_btn:hover {{ background-color: #0C4A82; }}
            #dlg_cancel_btn {{
                background-color: transparent;
                color: {BLUE};
                border: 1px solid {BLUE};
            }}
            #dlg_cancel_btn:hover {{ background-color: {BLUE_LIGHT}; }}
        """)

        vlay = QtWidgets.QVBoxLayout(dlg)
        vlay.setSpacing(16)
        vlay.setContentsMargins(24, 24, 24, 20)

        _ctx = "MainWindow"
        title_lbl = QtWidgets.QLabel(_tr(_ctx, "Where to save the results?"))
        title_lbl.setStyleSheet(
            f"font-size:17px; font-weight:700; color:{TEXT_PRI};"
        )
        vlay.addWidget(title_lbl)

        _scan = self._scan_demfile_gencodes()
        if not params.get("non_coi", False):
            if _scan["has_any"]:
                # Per-sample codes from the CSV are in effect; the menu is ignored.
                _brk = ", ".join(f"{_tr(_ctx, 'table')} {t}: {n}"
                                 for t, n in sorted(_scan["by_table"].items()))
                warn_lbl = QtWidgets.QLabel(
                    _tr(_ctx, "Genetic codes detected per sample in the CSV:<br>")
                    + f"<b>{_brk}</b>"
                )
            else:
                gencode_name = self._panel_params.p_gencode.currentText()
                warn_lbl = QtWidgets.QLabel(
                    _tr(_ctx, "Genetic code selected for the analysis:<br>") + f"<b>{gencode_name}</b>"
                )
            warn_lbl.setTextFormat(QtCore.Qt.RichText)
            warn_lbl.setWordWrap(True)
            warn_lbl.setStyleSheet(
                f"font-size:15px; color:{AMBER}; background-color:{AMBER_LT}; "
                f"border:1px solid {AMBER}; border-radius:6px; padding:8px 10px;"
            )
            vlay.addWidget(warn_lbl)

        # Duplicate sample names: reads sharing a name are merged into one sample.
        _dups = _scan["duplicates"]
        if _dups:
            _dnames = ", ".join(sorted(_dups)[:8]) + ("…" if len(_dups) > 8 else "")
            dup_lbl = QtWidgets.QLabel(
                "⚠ " + _tr(_ctx,
                    "{0} duplicate sample name(s) in the CSV. Reads under the same "
                    "name will be merged into a single sample: {1}")
                .format(len(_dups), _dnames)
            )
            dup_lbl.setWordWrap(True)
            dup_lbl.setStyleSheet(
                f"font-size:15px; color:{RED}; background-color:{AMBER_LT}; "
                f"border:1px solid {RED}; border-radius:6px; padding:8px 10px;"
            )
            vlay.addWidget(dup_lbl)

        radio_default = QtWidgets.QRadioButton(
            f"{_tr(_ctx, 'Automatic folder (recommended)')}\n"
            f"  …/output/{default_folder_name}"
        )
        radio_default.setChecked(True)

        radio_custom = QtWidgets.QRadioButton(_tr(_ctx, "Select folder manually"))

        vlay.addWidget(radio_default)
        vlay.addWidget(radio_custom)
        vlay.addSpacing(8)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QtWidgets.QPushButton(_tr(_ctx, "Cancel"))
        btn_cancel.setObjectName("dlg_cancel_btn")
        btn_cancel.setFixedHeight(38)
        btn_ok = QtWidgets.QPushButton(_tr(_ctx, "Continue"))
        btn_ok.setObjectName("dlg_ok_btn")
        btn_ok.setFixedHeight(38)
        btn_ok.setDefault(True)
        btn_cancel.clicked.connect(dlg.reject)
        btn_ok.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addSpacing(8)
        btn_row.addWidget(btn_ok)
        vlay.addLayout(btn_row)

        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return

        if radio_default.isChecked():
            outpath = default_outpath
            os.makedirs(outpath, exist_ok=True)
        else:
            outpath = QtWidgets.QFileDialog.getExistingDirectory(
                self, _tr("MainWindow", "Select the output folder (it must be empty)")
            )
            if not outpath:
                return
            if os.listdir(outpath):
                QtWidgets.QMessageBox.warning(
                    self, _tr("MainWindow", "Folder not empty"),
                    _tr("MainWindow", "Please select a non-empty folder to avoid conflicts.")
                )
                return
        self._outpath = outpath

        is_live = self._is_live()
        non_coi = params.get("non_coi", False)

        os.makedirs(os.path.join(outpath, "barcodesets"), exist_ok=True)
        os.makedirs(os.path.join(outpath, "barcodesets", "consensus_by_length"), exist_ok=True)
        if not non_coi:
            os.makedirs(os.path.join(outpath, "barcodesets", "consensus_by_similarity"), exist_ok=True)
            os.makedirs(os.path.join(outpath, "barcodesets", "fixing"), exist_ok=True)
        os.makedirs(os.path.join(outpath, "barcodesets", "temps"), exist_ok=True)
        os.makedirs(os.path.join(outpath, "demultiplexingfiles"), exist_ok=True)
        os.makedirs(os.path.join(outpath, "demultiplexed"), exist_ok=True)
        os.makedirs(os.path.join(outpath, "2a_ConsensusByLength"), exist_ok=True)

        if not is_live:
            os.makedirs(os.path.join(outpath, "1_demultiplexing"), exist_ok=True)
            if not non_coi:
                os.makedirs(os.path.join(outpath, "2b_ConsensusBySimilarity"), exist_ok=True)
                os.makedirs(os.path.join(outpath, "3_ConsensusByBarcodeComparison"), exist_ok=True)
            else:
                os.makedirs(os.path.join(outpath, "Main_barcode_results"), exist_ok=True)
        else:
            # In RT mode these folders are also needed from the beginning,
            # since _live_clean_intermediate recreates them before each cycle
            # but the first cycle could start before they exist.
            os.makedirs(os.path.join(outpath, "1_demultiplexing"), exist_ok=True)
            if not non_coi:
                os.makedirs(os.path.join(outpath, "2b_ConsensusBySimilarity"), exist_ok=True)
                os.makedirs(os.path.join(outpath, "3_ConsensusByBarcodeComparison"), exist_ok=True)
            os.makedirs(os.path.join(outpath, "Main_barcode_results"), exist_ok=True)
            os.makedirs(os.path.join(outpath, "live_fastq_processed"), exist_ok=True)

        self._panel_progress.reset()
        self._sidebar.mark_done("params")
        self._switch_panel("progress")

        mode_str = "Real-Time" if self._is_live() else "Conventional"
        self._panel_progress.append_log(f"Mode: {mode_str}", "info")
        if params.get("non_coi", False):
            self._panel_progress.append_log(
                "⚠  Non-Coding marker active: genetic code validation disabled. "
                "Only phases 1 and 2a are executed.", "warn")
        elif self._scan_demfile_gencodes()["has_any"]:
            _byc = self._scan_demfile_gencodes()["by_table"]
            _brk = ", ".join(f"table {t}: {n} sample(s)" for t, n in sorted(_byc.items()))
            self._panel_progress.append_log(
                f"ℹ  Per-sample genetic code from CSV ({_brk}). The "
                "Parameters → General code is ignored.", "info")
        if params.get("resolve_mixed", {}).get("enabled", False):
            self._panel_progress.append_log(
                "⚠  Intra-sample variant detection ON: mixed samples keep the "
                "dominant haplotype; secondary variants exported to "
                "secondary_variants.fa.", "warn")
        self._panel_progress.append_log(f"Output: {outpath}", "info")

        self._run_full_pipeline()

    def _run_full_pipeline(self):
        params = self._params
        outpath = self._outpath

        self._consensus_first_call = True
        self.con200trans = {}
        self.con200length = {}
        self.con200barcodes = {}
        self.mixinfo_all = {}
        self.con200cov = {}
        self.con200flags = {}
        self.n90trans = {}
        self.n90length = {}
        self.n90barcodes = {}
        self.n90cov = {}
        self.n90flags = {}
        self.corlist = []
        self.errbarcodeset = {}
        self.hapiddict = {}
        self.seqdict = {}
        self._phase3_row = 0
        self.ngoodbarcodescounter = 0
        self.con200goodn = 0
        self.con200errn = 0
        self.cov2a_counts = {}
        self.n90goodn = 0
        self.n90errn = 0
        self.nfixed = 0
        self.nfinal = 0
        self.nerr = 0
        self.nsinfinalbarcodes = 0
        self.nfilteredbarcodes = 0
        self.nperfectbarcodes = 0
        self.n1to5errbarcodes = 0
        self.n6to10errbarcodes = 0
        self.n11to15errbarcodes = 0
        self.nover16errbarcodes = 0
        self.ndemultiplexed = 0
        self.nsampledemultiplexed5 = 0
        self.sampleids = {}
        self.selectlens = []
        self.selectlenscounter = 0
        self.inlistforconsensus = []

        self.wb = xlsxwriter.Workbook(os.path.join(outpath, "runsummary.xlsx"))
        self._logfile_path = os.path.join(outpath, "log.txt")
        logfile = open(self._logfile_path, 'w', encoding='utf-8')
        self._panel_progress._logfile_handle = logfile
        logfile.write(f"ONTbarcoder3 — Analysis log\n")
        logfile.write(f"Starting: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        logfile.write(f"Mode: {'Real-Time' if self._is_live() else 'Conventional'}\n")
        logfile.write(f"Output folder: {outpath}\n")
        logfile.write("\n--- Analysis parameters ---\n")
        logfile.write(f"  Non-Coding marker: {'YES' if params.get('non_coi', False) else 'NO'}\n")
        _rm = params.get('resolve_mixed', {})
        logfile.write(f"  Intra-sample variant detection (dominant haplotype): "
                      f"{'ON' if _rm.get('enabled', False) else 'OFF'}\n")
        if _rm.get('enabled', False):
            logfile.write(f"    · min secondary variant fraction: {_rm.get('min_secondary_frac', '?')} "
                          f"(derived per-column polymorphism threshold: {_rm.get('minor_thresh', '?')})\n")
            logfile.write(f"    · variant tolerance: {_rm.get('tolerance', '?')}\n")
        if not params.get("non_coi", False):
            _gcs = self._scan_demfile_gencodes()
            if _gcs["has_any"]:
                _byc = _gcs["by_table"]
                _brk = ", ".join(f"table {t}: {n}" for t, n in sorted(_byc.items()))
                logfile.write(f"  Genetic code: per-sample from CSV ({_brk})\n")
            else:
                logfile.write(f"  Genetic code: {params.get('gencode', 5)} (global)\n")
        logfile.write(f"  Minimum length (bp): {params.get('minlen', '?')}\n")
        logfile.write(f"  Barcode length (bp): {params.get('explen', '?')}\n")
        logfile.write(f"  Window of barcode length ± (bp): {params.get('demlen', '?')}\n")
        logfile.write(f"  Maximum read length deviation from barcode length: {params.get('lendev', '?')}\n")
        logfile.write(f"  Read quality filter (min mean Q): "
                      f"{params.get('minq', 0) if params.get('minq', 0) else 'OFF'}\n")
        logfile.write(f"  Primer mismatches allowed: {params.get('primermismatch', '?')}\n")
        logfile.write(f"  Tag mismatches allowed: {params.get('tagmm', '?')}\n")
        logfile.write(f"  Coverages phase 2a: {params.get('coveragelist', '?')}\n")
        logfile.write(f"  Main consensus calling frequency: {params.get('consfreqfixed', '?')}\n")
        logfile.write(f"  Range of frequencies to assess (min, max): {params.get('consfreqmin', '?')} – {params.get('consfreqmax', '?')} (paso {params.get('consfreqstep', '?')})\n")
        logfile.write(f"  Threads: {params.get('n_threads', '?')}\n")
        fases = [f"Phase {k.replace('run_','').upper()}" for k, v in params.items() if k.startswith('run_') and v]
        logfile.write(f"  Active phases: {', '.join(fases)}\n")
        logfile.write("=" * 60 + "\n\n")
        logfile.flush()

        # Show only selected phases in the progress panel
        self._panel_progress.configure_phases(
            run_phase1=params.get("run_phase1", True),
            run_phase2a=params.get("run_phase2a", True),
            run_phase2b=params.get("run_phase2b", True),
            run_phase3=params.get("run_phase3", True),
        )

        if self._is_live():
            self._panel_progress.configure_for_live(non_coi=params.get("non_coi", False))
            self._sidebar.show_item("live_chart")
            self._panel_live_chart.start_session()
            self._run_live_pipeline(params, outpath, logfile)
        else:
            self._sidebar.hide_item("live_chart")
            self._panel_progress.configure_for_conventional(non_coi=params.get("non_coi", False))
            self._run_conventional_pipeline(params, outpath, logfile)

    # ── Conventional pipeline ──────────────────────── ────────────────────────
    def _run_conventional_pipeline(self, params, outpath, logfile):
        self.worker_prep = prepdemultiplex(
            self._demfile, self._fastq,
            os.path.join(outpath, "1_demultiplexing"),
            params["minlen"], params["explen"], params["demlen"],
            logfile, minq=params.get("minq", 0),
            tagmm=params.get("tagmm", 2)
        )

        self.worker_prep.notifyMessage.connect(self._panel_progress.on_log_message, QtCore.Qt.QueuedConnection)
        self.worker_prep.taskFinished.connect(self._on_prep_demultiplex_done)
        self.worker_prep.start()

        self._panel_progress.set_phase("1", "Preparing demultiplexing...")
        if not self._is_live():
            self._panel_progress._start_timer()   # only in conventional mode; in RT the timer starts at the beginning of the analysis

    # ── Real time pipeline ──────────────────────── ─────────────────────────
    def _run_live_pipeline(self, params, outpath, logfile):
        """
        Redesigned real-time mode:
        -Monitor the FASTQs folder
        -Concatenate new FASTQs to the accumulated one (live_accumulated.fastq)
        -In each cycle it launches exactly the same conventional pipeline
        -At the end, copy input_files/and launch the final conventional pipeline
        """
        live_params = self._live_params
        bc_mode = live_params.get("bc_mode", "minkow")

        # Path to accumulated FASTQ that grows with each cycle
        self._live_accumulated_fastq = os.path.join(outpath, "live_accumulated.fastq")
        self._live_known_fastqs = set()
        self._live_consensus_running = False
        self._live_finalizing = False
        self._live_cycle_in_progress = False
        self._live_cycle_complete = False
        self._live_current_cycle = 0
        self._live_total_reads = 0      # exclusive RT counter, never stepped on by prepdemultiplex
        self._live_last_total_reads = None  # explicit reset for first cycle
        self._live_prev_best = 0        # best previous result, to show delta between cycles

        self._CONSENSUS_EVERY_N = params.get("live_consensus_reads")
        self._CONSENSUS_EVERY_MIN = params.get("live_consensus_minutes")

        if bc_mode == "minkow":
            fastq_dir = live_params.get("fastq_dir", "")
            self._live_fastq_dir = fastq_dir
            self._panel_progress.append_log(
                f"Real-Time mode — FASTQ folder: {fastq_dir}", "info"
            )
        else:
            pod5_dir = live_params.get("dorado_indir", "")
            self._live_pod5_dir = pod5_dir
            self._live_dorado_exe = live_params.get("dorado_exe", "dorado")
            self._live_dorado_model = live_params.get("dorado_model", "")
            self._live_fastq_dir = os.path.join(outpath, "live_fastq_processed")
            self._live_known_pod5s = set()
            self._panel_progress.append_log(
                f"Real-Time mode — POD5 folder: {pod5_dir}", "info"
            )
            self._panel_progress.append_log("  Checking CUDA availability…", "info")
            threading.Thread(
                target=self._check_cuda_then_start,
                daemon=True,
            ).start()
            return

        n_str = f"{self._CONSENSUS_EVERY_N:,}" if self._CONSENSUS_EVERY_N else "—"
        m_str = str(self._CONSENSUS_EVERY_MIN) if self._CONSENSUS_EVERY_MIN else "—"
        self._panel_progress.append_log(
            f"Provisional consensus every new {n_str} reads or {m_str} min", "info"
        )
        self._panel_progress.set_phase("1", "Scanning folder…")
        self._panel_progress._start_timer()   # total timer RT: starts here and only stops when interrupted

        # Timer A: detect new FASTQs/POD5s
        self._live_dem_poll_timer = QtCore.QTimer()
        if bc_mode == "minkow":
            self._live_dem_poll_timer.timeout.connect(self._live_poll_fastq_dir)
        else:
            self._live_dem_poll_timer.timeout.connect(self._live_poll_pod5_dir)
        self._live_dem_poll_timer.start(3000)

        # Timer B: consensus by time (if configured)
        self._live_consensus_timer = QtCore.QTimer()
        self._live_consensus_timer.timeout.connect(self._live_maybe_run_consensus)
        if self._CONSENSUS_EVERY_MIN:
            self._live_consensus_timer.start(self._CONSENSUS_EVERY_MIN * 60 * 1000)

        # Initial scan
        if bc_mode == "minkow":
            self._live_poll_fastq_dir()
        else:
            self._live_poll_pod5_dir()

    def _check_cuda_then_start(self):
        """Run in a background thread: verify CUDA via nvidia-smi before starting Dorado polling."""
        cuda_ok = False
        try:
            result = subprocess.run(
                ["nvidia-smi"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8
            )
            cuda_ok = (result.returncode == 0)
        except FileNotFoundError:
            pass
        except Exception:
            pass

        if cuda_ok:
            self._panel_progress.append_log(
                "  CUDA device detected — starting Dorado polling.", "ok"
            )
            QtCore.QMetaObject.invokeMethod(
                self, "_start_dorado_polling", QtCore.Qt.QueuedConnection
            )
        else:
            self._panel_progress.append_log(
                "  No CUDA device found (nvidia-smi not available or returned an error).", "error"
            )
            self._panel_progress.append_log(
                "  Dorado requires an NVIDIA GPU with CUDA. Analysis aborted.", "error"
            )
            QtCore.QMetaObject.invokeMethod(
                self, "_stop_analysis", QtCore.Qt.QueuedConnection
            )

    @QtCore.pyqtSlot()
    def _start_dorado_polling(self):
        """Start timers and initial POD5 scan for Dorado RT mode (called after CUDA check)."""
        n_str = f"{self._CONSENSUS_EVERY_N:,}" if self._CONSENSUS_EVERY_N else "—"
        m_str = str(self._CONSENSUS_EVERY_MIN) if self._CONSENSUS_EVERY_MIN else "—"
        self._panel_progress.append_log(
            f"Provisional consensus every new {n_str} reads or {m_str} min", "info"
        )
        self._panel_progress.set_phase("1", "Scanning folder…")
        self._panel_progress._start_timer()

        self._live_dem_poll_timer = QtCore.QTimer()
        self._live_dem_poll_timer.timeout.connect(self._live_poll_pod5_dir)
        self._live_dem_poll_timer.start(3000)

        self._live_consensus_timer = QtCore.QTimer()
        self._live_consensus_timer.timeout.connect(self._live_maybe_run_consensus)
        if self._CONSENSUS_EVERY_MIN:
            self._live_consensus_timer.start(self._CONSENSUS_EVERY_MIN * 60 * 1000)

        self._live_poll_pod5_dir()

    # ══════════════════════════════════════════════════════════════════════════
    # Timer A — Detection of new FASTQs and concatenation to the accumulated one
    # ══════════════════════════════════════════════════════════════════════════

    def _live_poll_fastq_dir(self):
        """Detects new FASTQs, concatenates them to the accumulated one and triggers a cycle if appropriate."""
        if self._live_finalizing:
            return
            
        try:
            current = set(
                f for f in os.listdir(self._live_fastq_dir)
                if f.endswith(".fastq") or f.endswith(".fastq.gz")
            )
        except OSError as e:
            self._panel_progress.append_log(f"Error reading folder: {e}", "error")
            return

        new_files = sorted(current - self._live_known_fastqs)
        if not new_files:
            return

        # IMPORTANT: update _live_known_fastqs AFTER concatenating,
        # and only for files that were actually processed successfully.
        # If you update before and _live_concatenate_fastqs partially fails,
        # Those files are marked as known but without their data
        # in the accumulated, and the concatenation is never retried.
        n_reads_added = self._live_concatenate_fastqs(new_files)
        # Only mark the files that were actually written to the accumulated as
        # known; empty/half-written or errored files stay unknown and are retried
        # on the next poll, so no reads are silently lost.
        processed = getattr(self, "_live_last_processed_fastqs", None)
        if processed is None:
            processed = set(new_files)
        self._live_known_fastqs |= processed
        if n_reads_added > 0:
            self._panel_progress.append_log(
                f"  +{len(new_files)} FASTQ file(s) — {n_reads_added:,} new reads "
                f"(Cumulative total: {self.totalseqs:,})", "info"
            )
            self._live_maybe_run_consensus()

    def _live_concatenate_fastqs(self, new_files: list) -> int:
        """
        Concatenates the new FASTQs to the accumulated file.

        CRITICAL — prepdemultiplex support:
        prepdemultiplex reads the accumulated with zip_longest(*[infile]*4), which
        consumes exactly 4 lines per iteration. Any empty line
        extra between files shifts the offset and corrupts all reads
        posteriores (line2 pasa a ser '+' en vez de la secuencia, etc.).

        Therefore, before writing each file to the accumulated:
        1. ALL trailing empty lines are removed (byte rstrip).
        2. Exactly ONE '\n' is added to the end.
        This ensures that the rollup is a perfectly aligned FASTQ
        to 4 lines regardless of how MinKNOW finishes its files.

        IMPORTANT: use _live_total_reads (own RT counter) instead of
        self.totalseqs, which can be overwritten by prepdemultiplex during
        pipeline cycles and break the detection of new FASTQs.
        """
        import gzip
        n_reads = 0
        # Files actually written to the accumulated this call. Only these may be
        # marked as "known" by the caller; empty/missing or errored files are left
        # unknown so they are retried on a later poll (otherwise a half-written or
        # transiently-unreadable FASTQ would be skipped forever and its reads lost).
        processed = set()

        with open(self._live_accumulated_fastq, "ab") as out_fh:
            for fname in new_files:
                fpath = os.path.join(self._live_fastq_dir, fname)
                if not os.path.isfile(fpath) or os.path.getsize(fpath) == 0:
                    continue
                try:
                    if fname.endswith(".gz"):
                        with gzip.open(fpath, "rb") as gz:
                            data = gz.read()
                    else:
                        with open(fpath, "rb") as fh:
                            data = fh.read()

                    # Remove ALL trailing \ns and add exactly one.
                    # This avoids empty lines between files that would misalign
                    # the zip_longest(*[infile]*4) of prepdemultiplex.
                    data = data.rstrip(b"\n") + b"\n"

                    out_fh.write(data)
                    processed.add(fname)

                    # Count reads per file (offset always from 0)
                    lines = data.split(b"\n")
                    if lines and lines[-1] == b"":
                        lines = lines[:-1]
                    file_reads = sum(
                        1 for i, ln in enumerate(lines)
                        if i % 4 == 0 and ln.startswith(b"@")
                    )
                    n_reads += file_reads
                    self._panel_progress.append_log(
                        f"    Concatenated: {fname} ({file_reads:,} reads)", "info")
                except Exception as e:
                    self._panel_progress.append_log(f"    Error reading {fname}: {e}", "warn")

        # Expose the successfully-processed files to the caller (poll loop) so it
        # only marks those as known; see _live_poll_fastq_dir.
        self._live_last_processed_fastqs = processed

        self._live_total_reads = getattr(self, '_live_total_reads', 0) + n_reads
        self.totalseqs = self._live_total_reads
        self._panel_progress.stat_total.update_value(f"{self._live_total_reads:,}")
        return n_reads

    def _live_poll_pod5_dir(self):
        if self._live_finalizing:
            return
            
        try:
            current = set(
                f for f in os.listdir(self._live_pod5_dir) if f.endswith(".pod5")
            )
        except OSError as e:
            self._panel_progress.append_log(f"Error reading POD5 folder: {e}", "error")
            return
        new_files = current - self._live_known_pod5s
        if not new_files:
            return

        # Guard: don't launch a new batch if one is still running
        active_proc = getattr(self, "_live_dorado_proc", None)
        if active_proc is not None and active_proc.poll() is None:
            self._panel_progress.append_log(
                f"  Dorado still running — {len(new_files)} new POD5 file(s) queued for next batch.",
                "info"
            )
            return

        self._live_known_pod5s = current
        self._live_dorado_iternum = getattr(self, "_live_dorado_iternum", 0)
        iternum = self._live_dorado_iternum
        self._live_dorado_iternum += 1
        out_fastq = os.path.join(self._live_fastq_dir, f"batch_{iternum}.fastq")
        new_paths = [os.path.join(self._live_pod5_dir, f) for f in sorted(new_files)]
        cmd = [
            self._live_dorado_exe, "basecaller", "--emit-fastq",
            "--device", "cuda:all", "--chunksize", "10000", "--overlap", "500",
            self._live_dorado_model, *new_paths,
        ]
        self._panel_progress.append_log(
            f"Dorado basecalling — batch {iternum} "
            f"({len(new_files)} new POD5 file{'s' if len(new_files) != 1 else ''} detected) …",
            "info"
        )
        try:
            with open(out_fastq, "wb") as fh:
                proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.PIPE)
            self._live_dorado_proc = proc
            threading.Thread(
                target=self._live_dorado_stream_stderr,
                args=(proc, iternum),
                daemon=True,
            ).start()
            QtCore.QTimer.singleShot(
                10000, lambda p=out_fastq, pr=proc: self._live_dorado_fastq_ready(p, pr)
            )
        except Exception as e:
            self._panel_progress.append_log(f"Error launching Dorado: {e}", "error")

    def _kill_dorado_proc(self, proc):
        """Kill a Dorado process and its entire child tree (e.g. dorado_basecalling_server)."""
        if proc is None or proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            else:
                import signal, os
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _live_dorado_stream_stderr(self, proc, batch_num: int):
        """Read Dorado stderr in a background thread and forward each line to the log."""
        import re
        _stack_re = re.compile(r"^\s*[0-9A-Fa-f]{16}")
        _level_re = re.compile(r"\[(info|warn(?:ing)?|error|critical)\]", re.IGNORECASE)
        _level_map = {"info": "info", "warn": "warn", "warning": "warn",
                      "error": "error", "critical": "error"}
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line or _stack_re.match(line):
                    continue
                m = _level_re.search(line)
                level = _level_map.get(m.group(1).lower(), "info") if m else "info"
                self._panel_progress.append_log(f"  [Dorado b{batch_num}] {line}", level)
        except Exception:
            pass
        rc = proc.wait()
        if rc != 0:
            self._panel_progress.append_log(
                f"  [Dorado b{batch_num}] Process exited with code {rc} — basecalling failed.",
                "error"
            )

    def _live_dorado_fastq_ready(self, fastq_path: str, proc=None):
        """When Dorado finishes writing the FASTQ, it concatenates it to the accumulated."""
        if getattr(self, "_stopped", False) or getattr(self, "_live_finalizing", False):
            return
        # Dorado streams the FASTQ progressively, so a non-empty file while the
        # process is still running is only a *partial* batch.  Consuming it now
        # would (a) drop every read Dorado writes afterwards — _live_concatenate_fastqs
        # reads the whole batch file once, from offset 0, and never revisits it —
        # and (b) append a half-written final record, shifting the 4-line FASTQ
        # frame of every later read.  Wait until the process has actually exited
        # (its output fully flushed) before reading the file.
        if proc is not None and proc.poll() is None:
            self._panel_progress.append_log(
                f"  Dorado still running — checking again in 30 s …", "info"
            )
            QtCore.QTimer.singleShot(
                30000, lambda p=fastq_path, pr=proc: self._live_dorado_fastq_ready(p, pr)
            )
            return
        if os.path.isfile(fastq_path) and os.path.getsize(fastq_path) > 0:
            n = self._live_concatenate_fastqs([os.path.basename(fastq_path)])
            if n > 0:
                self._panel_progress.append_log(
                    f"  Dorado: +{n:,} new reads (cumulative total: {self.totalseqs:,})", "info"
                )
                self._live_maybe_run_consensus()
            else:
                self._panel_progress.append_log(
                    f"  Dorado: output file empty or unreadable — skipping batch.", "warn"
                )
        else:
            self._panel_progress.append_log(
                f"  Dorado exited without producing output — batch skipped.", "warn"
            )

    # ══════════════════════════════════════════════════════════════════════════
    # RT cycle — same logic as conventional mode
    # ══════════════════════════════════════════════════════════════════════════

    def _live_maybe_run_consensus(self):
        """Triggers a full conventional cycle if one is not in progress."""
        if self._live_consensus_running or self._live_finalizing:
            return
        if not os.path.isfile(self._live_accumulated_fastq) or \
                os.path.getsize(self._live_accumulated_fastq) == 0:
            return

        # Use _live_total_reads (exclusive RT counter) instead of
        # self.totalseqs, which can be overridden by prepdemultiplex
        # during the pipeline and break the comparison with _live_last_total_reads.
        rt_total = getattr(self, '_live_total_reads', 0)

        # Check if the conditions are met
        should_run = False

        # Condition by number of reads
        if self._CONSENSUS_EVERY_N:
            last_total = getattr(self, "_live_last_total_reads", None)
            if last_total is None:
                should_run = rt_total > 0      # first cycle
            else:
                new_reads = rt_total - last_total
                if new_reads >= self._CONSENSUS_EVERY_N:
                    should_run = True

        # Condition by time
        if self._CONSENSUS_EVERY_MIN:
            last_time = getattr(self, "_live_last_cycle_time", None)
            if last_time is None or (time.time() - last_time) >= (self._CONSENSUS_EVERY_MIN * 60):
                if rt_total > 0:
                    should_run = True

        # If no condition is set: automatic first cycle
        if not self._CONSENSUS_EVERY_N and not self._CONSENSUS_EVERY_MIN:
            if getattr(self, "_live_last_total_reads", None) is None and rt_total > 0:
                should_run = True

        if not should_run:
            return

        self._live_consensus_running = True
        self._live_current_cycle += 1
        self._panel_progress.update_cycle_number(self._live_current_cycle)
        self._live_last_total_reads = rt_total   # save the RT counter, not totalseqs
        self._live_last_cycle_time = time.time()
        self._panel_progress._start_cycle_timer()
        
        n = self._live_current_cycle
        self._panel_progress.append_log(
            f"━━ RT cycle #{n} — launching conventional analysis "
            f"({self.totalseqs:,} accumulated reads)…", "info"
        )
        self._live_run_cycle()

    def _live_run_cycle(self):
        """
        Clean intermediate files and launch full pipeline.
        Preserve: live_accumulated.fastq, *.log, consensus_*.fa
        """
        outpath = self._outpath
        params = self._params

        # Terminate workers and pool from the previous cycle to avoid cross signals
        for attr in ('worker_prep', 'mymergedatasets', 'myconsensus1',
                     'mycheckmsa', 'myconsensus2', 'mycheckmsa2', 'myfix'):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.quit()
                    w.wait(500)
                except Exception:
                    pass
                setattr(self, attr, None)
        if self.pool:
            try:
                self.pool.terminate()
            except Exception:
                pass
            self.pool = None
        if self.timer:
            try:
                self.timer.stop()
            except Exception:
                pass
            self.timer = None

        # Clean intermediate directories
        self._live_clean_intermediate(outpath, params)

        # Reset Analysis State Variables
        # (identical to the _start_pipeline block to ensure equivalence)
        # Preserve QC=True results between RT cycles — do not degrade good results
        _frozen = {k for k, v in self.con200flags.items() if v}
        self.con200flags    = {k: True for k in _frozen}
        self.con200barcodes = {k: v for k, v in self.con200barcodes.items() if k in _frozen}
        self.con200length   = {k: v for k, v in self.con200length.items()   if k in _frozen}
        self.con200trans    = {k: v for k, v in self.con200trans.items()    if k in _frozen}
        self.con200cov      = {k: v for k, v in self.con200cov.items()      if k in _frozen}
        self.n90trans = {}
        self.n90length = {}
        self.n90barcodes = {}
        self.n90cov = {}
        self.n90flags = {}
        self.corlist = []
        self.ngoodbarcodescounter = 0
        self.con200goodn = 0
        self.con200errn = 0
        self.cov2a_counts = {}
        self.n90goodn = 0
        self.n90errn = 0
        self.nfixed = 0
        self.nfinal = 0
        self.nerr = 0
        self.nsinfinalbarcodes = 0
        self.nfilteredbarcodes = 0
        self.nperfectbarcodes = 0
        self.n1to5errbarcodes = 0
        self.n6to10errbarcodes = 0
        self.n11to15errbarcodes = 0
        self.nover16errbarcodes = 0
        self.errbarcodeset = {}
        self.hapiddict = {}
        self.seqdict = {}
        self._phase3_row = 0
        self.ndemultiplexed = 0
        self.nsampledemultiplexed5 = 0
        self.sampleids = {}
        self._consensus_first_call = True
        self.selectlens = []
        self.selectlenscounter = 0
        self.inlistforconsensus = []
        self._live_cycle_complete = False

        # Point _fastq to the accumulation and launch conventional pipeline
        self._fastq = self._live_accumulated_fastq
        logfile = getattr(self._panel_progress, "_logfile_handle", None)
        self._run_conventional_pipeline(params, outpath, logfile)

    def _live_clean_intermediate(self, outpath: str, params: dict):
        """
        Clean intermediate files but preserve:
        -live_accumulated.fastq
        - *.log
        -consensus_no_errors.fa and consensus_all.fa (from previous cycle)

        Note: the preserved files (live_accumulated.fastq, log.txt and the
        consensus_*.fa from the previous cycle) live directly in `outpath`, NOT
        inside any of the intermediate subfolders deleted below, so removing
        those folders never touches them. Keep any new must-preserve file in the
        output root for this guarantee to hold.
        """
        # Delete entire intermediate folders
        for folder in ["1_demultiplexing", "demultiplexed", "demultiplexingfiles",
                       "2a_ConsensusByLength", "2b_ConsensusBySimilarity",
                       "3_ConsensusByBarcodeComparison", "barcodesets",
                       "Main_barcode_results"]:
            d = os.path.join(outpath, folder)
            if os.path.isdir(d):
                try:
                    shutil.rmtree(d)
                except Exception:
                    pass
        
        # Recreate directories needed for the pipeline
        non_coi = params.get("non_coi", False)
        base_dirs = ["1_demultiplexing", "demultiplexed", "demultiplexingfiles",
                     "2a_ConsensusByLength"]
        if not non_coi:
            base_dirs += ["2b_ConsensusBySimilarity", "3_ConsensusByBarcodeComparison"]
        for d in base_dirs:
            os.makedirs(os.path.join(outpath, d), exist_ok=True)
        
        barcodes_subs = ["", "consensus_by_length", "temps"]
        if not non_coi:
            barcodes_subs += ["consensus_by_similarity", "fixing"]
        for sub in barcodes_subs:
            base = os.path.join(outpath, "barcodesets")
            if sub:
                os.makedirs(os.path.join(base, sub), exist_ok=True)
            else:
                os.makedirs(base, exist_ok=True)
        
        os.makedirs(os.path.join(outpath, "Main_barcode_results"), exist_ok=True)

    @_phase_callback
    def _on_prep_demultiplex_done(self, _=0):
        if getattr(self, '_stopped', False):
            return
        params = self._params
        outpath = self._outpath

        tagdict = self.worker_prep.tagdict
        muttags_fr = self.worker_prep.muttags_fr
        sampledict = self.worker_prep.sampledict
        # Full set of samples DEFINED in the input CSV (independent of how many
        # reads each got), so the Sample QC status sheet can also list the ones
        # that received 0 reads / were never demultiplexed.
        try:
            self._all_csv_samples = sorted(set(sampledict.values()))
        except Exception:
            self._all_csv_samples = []
        typedict = self.worker_prep.typedict
        primerfset = self.worker_prep.primerfset
        primerrset = self.worker_prep.primerrset
        taglen = self.worker_prep.taglen
        maxid = self.worker_prep.maxid
        lastbitn = self.worker_prep.lastbitn
        self.totalseqs = self.worker_prep.totalseqs
        self.nseqspasslen = self.worker_prep.nseqspasslen
        self.nseqsfordemultiplexing = self.worker_prep.nseqsfordemultiplexing
        self.sampleids = self.worker_prep.sampleids

        if self._is_live():
            # In RT mode self.totalseqs is managed by _live_concatenate_fastqs
            # via _live_total_reads. We don't overwrite it here to
            # not break the comparison with _live_last_total_reads that uses the
            # exclusive RT counter. We only update the display with the value
            # actual pipeline (nseqspasslen) as secondary information.
            pass
        else:
            self.totalseqs = self.worker_prep.totalseqs
            self._panel_progress.stat_total.update_value(f"{self.totalseqs:,}")

        basename = os.path.basename(self._fastq)

        prefix1 = basename + "_reformat_out_1pdt_p"
        prefix2 = basename + "_reformat_out_2pdt_p"

        partlist1 = sorted(fnmatch.filter(os.listdir(os.path.join(outpath, "1_demultiplexing")), prefix1 + "*"))
        partlist2 = sorted(fnmatch.filter(os.listdir(os.path.join(outpath, "1_demultiplexing")), prefix2 + "*"))

        typelist = []
        for each in partlist1:
            typelist.append((each, params["primersearchlen"]))
        for each in partlist2:
            typelist.append((each, params["primersearchlen"] * 2))

        partlist = partlist1 + partlist2
        n_threads = params.get('n_threads', 4)
        self._n_dem_chunks = n_threads

        def chunker_list(seq, size):
            return [seq[i::size] for i in range(size)]

        nparts = chunker_list(partlist, n_threads)
        nparts = [[x, os.path.join(outpath, "1_demultiplexing"), tagdict, muttags_fr,
                   sampledict, typedict, primerfset, primerrset, taglen, i, maxid, lastbitn,
                   typelist, params.get("primermismatch", 10)]
                  for i, x in enumerate(nparts)]

        self._n_dem_jobs = len(nparts)
        self.resultlist = []
        self._dem_errors = []

        def addresult(result):
            self.resultlist.append(result)

        def adderror(exc):
            self._dem_errors.append(exc)
            self.resultlist.append(None)
            self._panel_progress.append_log(
                f"  Error in demultiplexing worker: {exc}", "error")

        for i in range(n_threads):
            os.makedirs(os.path.join(outpath, "1_demultiplexing", str(i)), exist_ok=True)

        self.queue = multiprocessing.Queue()
        self.pool = multiprocessing.Pool(processes=n_threads, initializer=pool_init1, initargs=(self.queue,))

        for j in nparts:
            self.pool.apply_async(func=rundemultiplex, args=(j,),
                                  callback=addresult, error_callback=adderror)
        self.pool.close()

        self._panel_progress.append_log(f"Starting demultiplexing with {self.nseqsfordemultiplexing} reads...", "info")

        self.sumprogress = [0] * n_threads
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._update_demultiplex_progress)
        self.timer.start(100)

    def _update_demultiplex_progress(self):
        if len(self.resultlist) != self._n_dem_jobs:
            if self.queue is not None:
                # Drain a bounded batch with get_nowait(). multiprocessing.Queue's
                # empty() is unreliable, so the previous empty()+blocking get()
                # could stall the GUI thread; get_nowait() never blocks.
                import queue as _queue_mod
                updated = False
                for _ in range(64):
                    try:
                        num_row, progress, labeltext = self.queue.get_nowait()
                    except (_queue_mod.Empty, Exception):
                        break
                    try:
                        self.sumprogress[num_row] = progress
                        updated = True
                    except (IndexError, TypeError):
                        continue
                if updated:
                    current_total = int(sum(self.sumprogress))
                    self._panel_progress.update_phase_progress(
                        "1", current_total, self.nseqsfordemultiplexing)
        else:
            self.timer.stop()
            if self.pool:
                self.pool.terminate()
                self.pool = None
            self._panel_progress.update_phase_progress("1", self.nseqsfordemultiplexing, self.nseqsfordemultiplexing)
            self._panel_progress.append_log("Demultiplexing completed — merging files...", "ok")

            self.mymergedatasets = mergedemfiles(
                self.sampleids,
                os.path.join(self._outpath, "1_demultiplexing"),
                os.path.join(self._outpath, "demultiplexed"),
                getattr(self, '_n_dem_chunks', 4),
            )
            self.mymergedatasets.notifyProgress.connect(self._on_merge_progress)
            self.mymergedatasets.taskFinished.connect(self._on_merge_done)
            self.mymergedatasets.start()

            self._panel_progress.set_phase("1", "Merging demultiplexed files...")

    def _on_merge_progress(self, i):
        self._panel_progress.update_phase_progress("1", i, len(self.sampleids))

    @_phase_callback
    def _on_merge_done(self, _=0):
        if getattr(self, '_stopped', False):
            return
        self._panel_progress.mark_phase_done("1", "Demultiplexing completed")
        self._panel_progress.append_log("File join completed", "ok")

        self.ndemultiplexed = 0
        self.nsampledemultiplexed5 = 0
        self.sampleids = self.mymergedatasets.sampleids
        self.sampleids = {k: int(v) if v != '' else 0
                          for k, v in self.sampleids.items()}

        for sample, count in self.sampleids.items():
            self.ndemultiplexed += count
            if count >= 5:
                self.nsampledemultiplexed5 += 1

        self._panel_progress.append_log(f"Demultiplexed reads: {self.ndemultiplexed:,}", "ok")
        self._panel_progress.append_log(f"Samples with ≥5 reads: {self.nsampledemultiplexed5}", "ok")
        self._panel_progress.update_stat_dem(self.ndemultiplexed, getattr(self, 'totalseqs', 0), final=True)

        # Update bar chart per sample in RT panel
        if self._is_live():
            self._panel_live_chart.update_sample_bar_chart(self.sampleids)

        # In provisional RT cycles, writing to Excel is omitted;
        # it is only written in conventional analysis or in the final RT loop.
        if not self._is_live() or getattr(self, "_live_finalizing", False):
            try:
                demsheet = self.wb.add_worksheet("1. Demultiplexing")
                demsheet.write(0, 0, "SpecimenID")
                demsheet.write(0, 1, "Number of sequences demultiplexed")
                for i, (sample, count) in enumerate(self.sampleids.items()):
                    demsheet.write(i+1, 0, sample)
                    demsheet.write(i+1, 1, count)
            except Exception as e:
                self._panel_progress.append_log(
                    f"  Warning Excel 1. Demultiplexing: {e}", "warn")

        if self._params["run_phase2a"]:
            self._run_consensus_by_length()
        else:
            self._finish_analysis()

    def _scan_demfile_gencodes(self):
        """Scan the demultiplexing CSV for an optional per-sample genetic code so
        vertebrate (table 2) and invertebrate (table 5) samples multiplexed in one
        run can each be validated with their own code.

        A demfile row is: name, tagF, tagR, primerF, primerR [, primerF2, primerR2 ...]
        — one or more forward/reverse primer pairs (a primer cocktail). The optional
        genetic code, when present, is ALWAYS the last column, told apart from a
        primer by type: primers are IUPAC strings (A,C,G,T,Y,W,H,N...), never plain
        numbers, so a last column that is a small integer (NCBI table 0–33) is the
        per-sample code — independent of how many primers the cocktail has.

        Counts are ROW-based (not by unique name) so that duplicate sample names —
        e.g. replicates pooled under one name — never make a complete CSV look
        "incomplete": with N duplicated names, ``len(codes)`` (a name-keyed dict)
        would be N short of the row count and falsely report missing codes.

        Returns a summary dict (cached per run):
            codes       {sample_name: int}   name → table (duplicates: last wins)
            missing     [sample_name, ...]   names of rows WITHOUT a valid code
            total_named int                  named rows (non-empty first column)
            n_with_code int                  named rows WITH a valid code
            by_table    {table: n_rows}      per-table row counts
            has_any     bool                 at least one row carries a code
            duplicates  {name: n_rows}       sample names used by more than one row
        """
        if getattr(self, "_gencode_scan_cache", None) is not None:
            return self._gencode_scan_cache
        codes, missing = {}, []
        total_named = 0
        n_with_code = 0
        by_table = Counter()
        name_counts = Counter()
        demfile = getattr(self, "_demfile", "")
        if demfile and os.path.isfile(demfile):
            try:
                import csv as _csv
                with open(demfile, newline="", encoding="utf-8-sig") as _f:
                    for row in _csv.reader(_f):
                        cols = [c.strip() for c in row]
                        if not any(cols):
                            continue
                        name = cols[0]
                        if not name:
                            continue
                        total_named += 1
                        name_counts[name] += 1
                        last = cols[-1] if len(cols) >= 6 else ""
                        # An integer in the NCBI table range is a genetic code; a
                        # primer (IUPAC string) never is.
                        if last.isdigit() and 0 <= int(last) <= 33:
                            n_with_code += 1
                            by_table[int(last)] += 1
                            codes[name] = int(last)
                        else:
                            missing.append(name)
            except Exception:
                pass
        summary = {
            "codes": codes,
            "missing": missing,
            "total_named": total_named,
            "n_with_code": n_with_code,
            "by_table": dict(by_table),
            "has_any": bool(codes),
            "duplicates": {n: c for n, c in name_counts.items() if c > 1},
        }
        self._gencode_scan_cache = summary
        return summary

    def _gencode_by_sample(self):
        """Per-sample {sample_name: NCBI_table} passed to the consensus worker.
        Empty in non-Coding mode (translation validation is disabled there) and empty
        for CSVs without per-sample codes, so the global ``gencode`` parameter is
        used as before. Strict mode (all-or-nothing) is enforced earlier, in
        :meth:`_start_analysis`, so by the time this runs the map is complete."""
        if self._params.get("non_coi", False):
            return {}
        return self._scan_demfile_gencodes()["codes"]

    def _run_consensus_by_length(self):
        params = self._params
        outpath = self._outpath

        if not hasattr(self, '_consensus_first_call') or self._consensus_first_call:
            self.selectlens = params["coveragelist"]
            self.selectlenscounter = 0
            mincov = params.get("mincoverage", 1)
            # sorted() guarantees deterministic ordering independent of the filesystem,
            # making the result identical between conventional mode and RT cycle by cycle
            all_fa = sorted(fnmatch.filter(
                os.listdir(os.path.join(outpath, "demultiplexed")), "*_all.fa"
            ))
            if mincov > 1:
                # Use the full sample name (everything before "_all.fa"). A prior
                # ".split('.')[0]" truncated names containing a dot (e.g. "BC.01"),
                # so the sampleids lookup missed and the sample was wrongly dropped
                # when mincov > 1. The rest of the pipeline keys on the full name.
                all_fa = [f for f in all_fa
                          if self.sampleids.get(
                              f.split("_all.fa")[0], 0) >= mincov]
            self.inlistforconsensus = all_fa
            self._consensus_first_call = False

        current_cov = self.selectlens[self.selectlenscounter]
        self._panel_progress.set_phase("2a", f"Consensus by length-coverage {current_cov}...")

        if not self.inlistforconsensus:
            self._panel_progress.append_log("No _all.fa files found.", "error")
            self._finish_analysis()
            return

        n_samples = len(self.inlistforconsensus)
        n_good_so_far = sum(1 for v in self.con200flags.values() if v)
        self._panel_progress.append_log(
            f"Processing {n_samples} samples (coverage {current_cov})"
            + (f" — {n_good_so_far} barcodes without errors so far" if n_good_so_far > 0 else "")
            + "...", "info"
        )
        self._panel_progress.update_phase_progress("2a", 0, n_samples)

        cov_dir = os.path.join(outpath, "2a_ConsensusByLength",
                               f"demultiplexed_{current_cov}")
        mafft_dir = os.path.join(outpath, "2a_ConsensusByLength",
                                 f"demultiplexed_{current_cov}_mafft")
        os.makedirs(cov_dir, exist_ok=True)
        os.makedirs(mafft_dir, exist_ok=True)
        os.makedirs(os.path.join(outpath, "barcodesets", "consensus_by_length"),
                    exist_ok=True)

        if self.selectlenscounter == 0:
            os.makedirs(os.path.join(outpath, "barcodesets", "temps"), exist_ok=True)
            os.makedirs(os.path.join(outpath, "barcodesets", "consensus_by_length"),
                        exist_ok=True)
            # Truncate accumulated files at the beginning of the first coverage level
            for fname in ["consensusgood_temp.fa"]:
                open(os.path.join(outpath, "barcodesets", "temps", fname), 'w').close()
            for fname in ["consensus_all_step1.fa",
                          "consensus_all_prederr_barcodes.fa"]:
                open(os.path.join(outpath, "barcodesets", "consensus_by_length",
                                  fname), 'w').close()

        # Truncate files from this coverage at the beginning of each level,
        # ensuring they do not accumulate data from previous RT cycles
        open(os.path.join(outpath, "barcodesets", "consensus_by_length",
                          f"consensus_{current_cov}_barcodes.fa"), 'w').close()
        open(os.path.join(outpath, "barcodesets", "consensus_by_length",
                          f"consensus{current_cov}prederr_barcodes.fa"), 'w').close()

        job = [
            self.inlistforconsensus,
            outpath,
            "demultiplexed",
            os.path.join("2a_ConsensusByLength",
                         f"demultiplexed_{current_cov}"),
            current_cov,
            f"consensus_{current_cov}_barcodes.fa",
            self.selectlenscounter,
            params["explen"],
            0,
            params["lendev"],
            "consensus_by_length",
            params["consfreqfixed"],
            [params["consfreqmin"],
             params["consfreqmax"]],
            params["consfreqstep"],
            params["gencode"],
            params.get("resolve_mixed", {}),
            self._gencode_by_sample(),
        ]

        self.myconsensus1 = runconsensusparts(job)
        self.myconsensus1.notifyProgress.connect(self._on_consensus_progress)
        self.myconsensus1.taskFinished.connect(self._on_consensus_done)
        self.myconsensus1.start()

    def _on_consensus_progress(self, i):
        n = len(self.inlistforconsensus)
        _cov_now = self.selectlens[self.selectlenscounter] if self.selectlens else ""
        n_ok = sum(1 for v in self.con200flags.values() if v)
        floor = getattr(self, "_live_ok_floor", 0)
        _extra = (f"coverage {_cov_now} → {n} samples" if _cov_now else "")
        self._panel_progress.update_phase_progress("2a", i, n, extra=_extra)
        self._panel_progress.update_stat_ok(max(n_ok, floor))

    @_phase_callback
    def _on_consensus_done(self, _=0):
        if getattr(self, '_stopped', False):
            return
        params = self._params
        outpath = self._outpath
        current_cov = self.selectlens[self.selectlenscounter]

        self.corlist = []

        self.resultlist = [[
            self.myconsensus1.transcheck,
            self.myconsensus1.conseqs,
            self.myconsensus1.flags,
            self.myconsensus1.coverages,
        ]]
        # Accumulate mixed-sample / contamination info across coverage levels and
        # RT cycles (latest level wins per sample).
        if not hasattr(self, 'mixinfo_all'):
            self.mixinfo_all = {}
        _mix_now = getattr(self.myconsensus1, 'mixinfo', {}) or {}
        for _mk, _mv in _mix_now.items():
            self.mixinfo_all[_mk] = _mv
            try:
                _frac = float(_mv.get("frac", 0)) * 100.0
                _ncl = _mv.get("n_clusters", 2)
                _extra = _mv.get("n_variants_extra", 0)
                _extra_txt = (f"; {_extra} more variant(s) beyond cap not recovered"
                              if _extra else "")
                if _mv.get("needs_review"):
                    _why = ("chosen barcode is not the most abundant variant"
                            if not _mv.get("chosen_by_abundance", True)
                            else f"{_mv.get('n_pass_qc', 2)} variants pass QC")
                    self._panel_progress.append_log(
                        f"  🔶 {_mk} — {_ncl} variants, NEEDS REVIEW ({_why}); "
                        f"kept variant at {_frac:.0f}%{_extra_txt}", "warn")
                else:
                    self._panel_progress.append_log(
                        f"  🟣 {_mk} — {_ncl} variants: kept dominant haplotype "
                        f"({_frac:.0f}%), secondary variant(s) exported{_extra_txt}",
                        "warn")
            except Exception:
                pass
        if self.selectlenscounter == 0:
            self.sampleids = self.myconsensus1.sampleids

        _n_processed = len(self.inlistforconsensus)  # samples sent to worker in this cycle
        self.inlistforconsensus = []

        consensus_fa = os.path.join(outpath, "barcodesets", "consensus_by_length",
                                    f"consensus_{current_cov}_barcodes.fa")
        prederr_fa = os.path.join(outpath, "barcodesets", "consensus_by_length",
                                  f"consensus{current_cov}prederr_barcodes.fa")

        non_coi = params.get("non_coi", False)
        # Count directly in the loop how many are obtained in THIS coverage
        _n_this_good = 0  # Coding: translation validation passed
        _n_this_seq  = 0  # non-Coding: have non-empty sequence

        # Downward coverage (non-Coding): selectlens = [1000, 500, 200, ...]
        _is_descending = (len(self.selectlens) > 1 and
                          self.selectlens[0] > self.selectlens[-1])
        _next_idx = self.selectlenscounter + 1
        _next_cov = (self.selectlens[_next_idx]
                     if _next_idx < len(self.selectlens) else None)

        with open(consensus_fa, 'a') as outfile, \
             open(prederr_fa, 'a') as outfile3:
            for each in self.resultlist:
                for k in each[0]:
                    self.con200trans[k] = each[0][k]
                for k in each[1]:
                    # Don't overwrite if you already have QC=True; in descending order
                    # keep the result with fewer Ns.
                    if len(each[1][k]) != 0 and not self.con200flags.get(k, False):
                        new_n = each[1][k].count('N')
                        old_n = self.con200barcodes[k].count('N') if k in self.con200barcodes else new_n + 1
                        # Same-N replacement in ascending order only when the sample
                        # actually reached the requested coverage; if it fell short,
                        # the run included lower-quality reads and the existing barcode
                        # (built from better-filtered reads) should be kept.
                        _actual_cov = each[3].get(k, 0)
                        _reached_cov = (isinstance(_actual_cov, int)
                                        and _actual_cov >= current_cov)
                        if (k not in self.con200barcodes
                                or new_n < old_n
                                or (new_n == old_n and not _is_descending and _reached_cov)):
                            self.con200barcodes[k] = each[1][k]
                            self.con200length[k] = len(each[1][k])
                            # Keep coverage in lock-step with the sequence we just
                            # stored. Updating con200cov unconditionally (as before)
                            # could pair this level's coverage with a sequence kept
                            # from a previous level (fewer Ns), mislabelling the
                            # coverage of unresolved / prederr barcodes.
                            self.con200cov[k] = each[3].get(k, "NA")
                for k in each[2]:
                    # If I already reached QC=True in a previous cycle, don't downgrade.
                    if self.con200flags.get(k, False):
                        continue
                    seq = each[1].get(k, "")
                    cov = each[3].get(k, "NA")
                    slen = len(seq) if seq else 0
                    if each[2][k]:
                        # Coding: translation validation step -> resolved
                        self.con200flags[k] = True
                        outfile.write(f">{k}_all.fa;{slen};{cov}\n{seq}\n")
                        self._panel_progress.append_log(
                            f"  🟢 {k} — {slen} bp", "info")
                        _n_this_good += 1
                    else:
                        self.con200flags[k] = False
                        if slen != 0:
                            outfile.write(f">{k}_all.fa;{slen};{cov}\n{seq}\n")
                            outfile3.write(f">{k}_all.fa;{slen};{cov}\n{seq}\n")
                            if non_coi:
                                ambs = seq.count("N")
                                tag = "no N" if ambs == 0 else f"{ambs} N"
                                check = "🟢" if tag == "no N" else "🟠"
                                self._panel_progress.append_log(
                                    f"  {check} {k} — {slen} bp ({tag})", "info")
                                # In non-Coding: resolved = no Ns.
                                # With Ns -> next level of coverage.
                                if ambs == 0:
                                    self.con200flags[k] = True
                                    _n_this_good += 1
                                else:
                                    # It has Ns: go to the next level.
                                    # For descending order (non-Coding): always
                                    # try lower levels; with less
                                    # subselected readings can give 0 Ns.
                                    if _is_descending:
                                        if _next_cov is not None:
                                            self.inlistforconsensus.append(
                                                k + "_all.fa")
                                    else:
                                        if k in self.sampleids and \
                                           self.sampleids[k] >= current_cov:
                                            self.inlistforconsensus.append(
                                                k + "_all.fa")
                            else:
                                if k in self.sampleids and \
                                   self.sampleids[k] >= current_cov:
                                    self.inlistforconsensus.append(k + "_all.fa")

        self.selectlenscounter += 1

        # Same count as Coding: con200flags is now True for resolved ones
        # in both modes. _n_this_good counts those in this cycle.
        n_good_this = _n_this_good
        n_good_accum = sum(1 for v in self.con200flags.values() if v)
        self.cov2a_counts[current_cov] = n_good_this


        # Apply floor only in provisional RT cycles, never in the final analysis
        if self._is_live() and not getattr(self, "_live_finalizing", False):
            floor = getattr(self, "_live_ok_floor", 0)
            self._panel_progress.update_stat_ok(max(n_good_accum, floor))
        else:
            self._panel_progress.update_stat_ok(n_good_accum)
        if self._is_live():
            self._panel_live_chart.record(
                self.ndemultiplexed,
                self._panel_progress.get_ok_value(),
                n_total=getattr(self, "totalseqs", 0),
                cycle=getattr(self, "_live_current_cycle", 0))

        label_tipo = "resolved" if non_coi else "good"
        self._panel_progress.append_log(
            f"  Coverage {current_cov}: {n_good_this} {label_tipo} "
            f"(accumulated: {n_good_accum})", "ok")
        self._panel_progress.mark_phase_done(
            "2a",
            f"Coverage {current_cov}: {n_good_this} {label_tipo} — {n_good_accum} accumulated"
        )

        if len(self.inlistforconsensus) > 0 and \
                self.selectlenscounter < len(self.selectlens):
            self._run_consensus_by_length()
        else:
            all_step1 = os.path.join(outpath, "barcodesets", "consensus_by_length",
                                     "consensus_all_step1.fa")
            all_prederr = os.path.join(outpath, "barcodesets", "consensus_by_length",
                                       "consensus_all_prederr_barcodes.fa")
            os.makedirs(os.path.join(outpath, "barcodesets", "temps"), exist_ok=True)
            good_temp = os.path.join(outpath, "barcodesets", "temps",
                                     "consensusgood_temp.fa")

            with open(all_step1, 'a') as outfile, \
                 open(good_temp, 'a') as outfile2, \
                 open(all_prederr, 'a') as outfile3:
                # Sort so that consensusgood_temp.fa and consensus_all_prederr_barcodes.fa
                # have the same order in conventional and real time
                for k in sorted(self.con200barcodes.keys()):
                    seq = self.con200barcodes[k]
                    slen = self.con200length[k]
                    cov = self.con200cov.get(k, "NA")
                    outfile.write(f">{k}_all.fa;{slen};{cov}\n{seq}\n")
                    # con200flags is True for resolved in both modes
                    if self.con200flags.get(k, False):
                        outfile2.write(f">{k}_all.fa;{slen};{cov}\n{seq}\n")
                        self.corlist.append(k + "_all.fa")
                        self.ngoodbarcodescounter += 1
                    else:
                        outfile3.write(f">{k}_all.fa;{slen};{cov}\n{seq}\n")

            # ── Intra-sample variant resolution summary ───────────────────
            _resolve_enabled = bool(params.get("resolve_mixed", {}).get("enabled", False))
            _mixed = {k: v for k, v in getattr(self, 'mixinfo_all', {}).items()
                      if k in self.con200barcodes and v.get("secondary")}
            if _resolve_enabled:
                variants_fa = os.path.join(outpath, "barcodesets",
                                           "consensus_by_length",
                                           "secondary_variants.fa")
                # ALL secondary variants per sample are reported (not just one). No
                # cross-sample "source" is inferred: an analysis routinely contains
                # several samples of the same species, so a secondary matching
                # another sample's barcode does NOT imply it is the source.
                # Skip low-quality secondaries (≥5 Ns) to keep the file informative.
                _MAX_VARIANT_N = 5
                _n_written = 0
                with open(variants_fa, 'w') as cf:
                    for k in sorted(_mixed.keys()):
                        v = _mixed[k]
                        # Prefer the full per-cluster breakdown; fall back to the
                        # back-compat single 'secondary' field if absent.
                        _secs = v.get("secondaries")
                        if not _secs:
                            _secs = [{"frac": 1.0 - float(v.get("frac", 0)),
                                      "seq": v.get("secondary", ""),
                                      "translates": None}]
                        for _i, s in enumerate(_secs, start=1):
                            seq = s.get("seq", "")
                            if not seq or seq.count("N") >= _MAX_VARIANT_N:
                                continue
                            _tr = s.get("translates")
                            _tr_tag = ("yes" if _tr is True
                                       else "no" if _tr is False else "NA")
                            cf.write(
                                f">{k}_var{_i};frac={float(s.get('frac',0))*100:.0f}%;"
                                f"len={len(seq)};translates={_tr_tag}\n{seq}\n")
                            _n_written += 1
                # "Recovered" = mixed samples that became QC-compliant barcodes
                # thanks to resolution (they would otherwise fail on Ns / frame).
                _n_recovered = sum(1 for k in _mixed
                                   if self.con200flags.get(k, False))
                _n_review = sum(1 for v in _mixed.values() if v.get("needs_review"))
                self._resolve_stats = {"enabled": True, "mixed": len(_mixed),
                                       "recovered": _n_recovered,
                                       "needs_review": _n_review,
                                       "contaminants": _n_written}
                if _mixed:
                    _rev_txt = (f"; ⚠ {_n_review} need manual review"
                                if _n_review else "")
                    self._panel_progress.append_log(
                        f"  Intra-sample variant resolution: {len(_mixed)} sample(s) "
                        f"resolved ({_n_recovered} now QC-compliant){_rev_txt}; "
                        f"{_n_written} secondary variant(s) written to "
                        f"secondary_variants.fa.", "warn")
            else:
                self._resolve_stats = {"enabled": False, "mixed": 0,
                                       "recovered": 0, "needs_review": 0,
                                       "contaminants": 0}

            # EXECUTE ALL PHASES in each cycle (not just up to 2a)
            if params.get("run_phase2b", True):
                self._run_consensus_by_similarity()
            elif params.get("run_phase3", True):
                self._run_msa_correction()
            elif params.get("non_coi", False):
                # Non-Coding mode: phases 2b and 3 disabled.
                # Build Final_all_combined_barcodes.fa directly from
                # good phase 2a barcodes and pass to _printoutputs().
                self._build_final_for_non_coi()
            else:
                self._finish_analysis()

    # ──────────────────────────────────────────────────────────────────────────
    # NON-CODING MODE: Build final files from phase 2a (without 2b or 3)
    # ──────────────────────────────────────────────────────────────────────────

    def _build_final_for_non_coi(self):
        """
        In non-Coding Marker mode, phases 2b and 3 are not executed and the
        translation validation (con200flags) NOT used for ranking
        barcodes — because that flag reflects whether the sequence translates well
        in the COI genetic code, which makes no sense for STI,
        rbcL, matK, etc.

        Instead, they are accepted as 'good' (→ Final_all /QC_Compliant)
        all barcodes that have non-empty sequence AND no bases
        ambiguous (N).  Those with Ns go to Remaining.fa.

        Files it builds (required for _printoutputs):
          • barcodesets/Final_all_combined_barcodes.fa — all with sequence
          • barcodesets/Final_predgood_combined_barcodes.fa — without Ns
          • barcodesets/temps/consensus_90perc_prederr_combined_barcodes.fa
            — with Ns or empty → reloaded by _printoutputs → Remaining.fa
        """
        outpath = self._outpath

        self._panel_progress.append_log(
            "  Non-Coding mode — generating final files from phase 2a...", "info")

        # Excel sheet "2a Consensus by length" (in Coding mode it is generated
        # _on_msa1_done_with_similarity, which is not called on non-Coding)
        if not self._is_live() or getattr(self, "_live_finalizing", False):
            try:
                demsheet = self.wb.add_worksheet("2a Consensus by length")
                headers = ["SpecimenID", "Demultiplexed Seqs", "Phase",
                           "Length", "Barcode", "Ambiguities"]
                for c, h in enumerate(headers):
                    demsheet.write(0, c, h)
                for i, j in enumerate(sorted(self.sampleids.keys())):
                    demsheet.write(i+1, 0, j)
                    demsheet.write(i+1, 1, self.sampleids[j])
                    demsheet.write(i+1, 2, "Consensus by length (non-Coding)")
                    try:
                        seq = self.con200barcodes[j]
                        demsheet.write(i+1, 3, self.con200length[j])
                        demsheet.write(i+1, 4, seq)
                        demsheet.write(i+1, 5, seq.count("N"))
                    except KeyError:
                        demsheet.write(i+1, 3, "NA")
                        demsheet.write(i+1, 4, "NA")
                        demsheet.write(i+1, 5, "NA")
            except Exception as e:
                self._panel_progress.append_log(
                    f"  Warning Excel 2a no-Coding: {e}", "warn")


        os.makedirs(os.path.join(outpath, "barcodesets"), exist_ok=True)
        os.makedirs(os.path.join(outpath, "barcodesets", "temps"), exist_ok=True)
        os.makedirs(os.path.join(outpath, "Main_barcode_results"), exist_ok=True)

        final_all_path  = os.path.join(outpath, "barcodesets",
                                       "Final_all_combined_barcodes.fa")
        final_good_path = os.path.join(outpath, "barcodesets",
                                       "Final_predgood_combined_barcodes.fa")
        prederr_path = os.path.join(outpath, "barcodesets", "temps",
                                    "consensus_90perc_prederr_combined_barcodes.fa")

        n_good = 0
        n_err  = 0

        with open(final_all_path,  'w') as f_all, \
             open(final_good_path, 'w') as f_good, \
             open(prederr_path,    'w') as f_err:

            for k in sorted(self.con200barcodes.keys()):
                seq  = self.con200barcodes.get(k, "")
                if not seq:
                    # No sequence: completely ignore
                    continue
                slen = self.con200length.get(k, len(seq))
                cov  = self.con200cov.get(k, "NA")
                ambs = seq.count("N")

                # Complete header for Final_all (format that _printoutputs expects)
                header_full = f">{k}_all.fa;{slen};{cov};ambs={ambs};estgaps=0\n"
                # Simple header for prederr (format that _printoutputs reloads)
                header_simple = f">{k}_all.fa;{slen};{cov}\n"

                # In non-Coding: "good" = unambiguous (N).
                # With Ns → Remaining.fa (same as in the original Coding pipeline).
                if ambs == 0:
                    f_all.write(header_full + seq + "\n")
                    f_good.write(header_full + seq + "\n")
                    n_good += 1
                else:
                    # Has Ns: goes to Allbarcodes but not QC_Compliant or predgood
                    f_all.write(header_full + seq + "\n")
                    f_err.write(header_simple + seq + "\n")
                    n_err += 1

        self._panel_progress.append_log(
            f"  Non-Coding mode: {n_good} barcodes without N | {n_err} with ambiguities (Remaining)",
            "ok")

        # Mark phase 2a as completed
        self._panel_progress.mark_phase_done(
            "2a",
            f"Phase 2a completed (non-Coding) — {n_good} without N / {n_err} with N")

        # Go directly to _printoutputs (without going through 2b or 3)
        self._printoutputs()

    # ──────────────────────────────────────────────────────────────────────────
    # PHASE 2b: Consensus by Similarity
    # ──────────────────────────────────────────────────────────────────────────

    def _run_consensus_by_similarity(self):
        params = self._params
        outpath = self._outpath

        self._panel_progress.set_phase("2b", "Phase 2b: Consensus by similarity (MSA check)...")

        os.makedirs(os.path.join(outpath, "2b_ConsensusBySimilarity"), exist_ok=True)
        os.makedirs(os.path.join(outpath, "barcodesets", "consensus_by_similarity"), exist_ok=True)

        n90subset = params.get("coverage2b", 200)
        dirdict = {}
        mode, indir = 0, ""
        consensus90stat = params.get("run_phase2b", True)
        task = 1 if consensus90stat else 0

        self.mycheckmsa = MSAcheck(
            outpath,
            params["explen"],
            "consensus_all_step1.fa",
            task,
            "90perc",
            mode,
            indir,
            "consensusgood_temp.fa",
            "consensus_all_prederr_barcodes.fa",
            n90subset,
            "consensus_by_length",
            self.ngoodbarcodescounter,
            self.corlist,
            "consensusgood",
            dirdict,
        )

        self.mycheckmsa.notifyProgress1.connect(self._on_msa1_progress1)
        self.mycheckmsa.notifyProgress2.connect(self._on_msa1_progress2)
        self.mycheckmsa.notifyProgress3.connect(
            lambda _: self._panel_progress.append_log("  MSA built", "info"))
        self.mycheckmsa.notifyProgress4.connect(
            lambda _: self._panel_progress.append_log("  Classifying barcodes...", "info"))

        if consensus90stat:
            self.mycheckmsa.taskFinished.connect(self._on_msa1_done_with_similarity)
        else:
            self.mycheckmsa.taskFinished.connect(self._on_msa_to_phase3)

        self.mycheckmsa.start()

    def _on_msa1_progress1(self, vals):
        self._panel_progress.update_phase_progress("2b", vals[0], vals[1])

    def _on_msa1_progress2(self, vals):
        self._panel_progress.update_phase_progress("2b", vals[0], vals[1])

    @_phase_callback
    def _on_msa1_done_with_similarity(self, ngoodbarcodes):
        if getattr(self, '_stopped', False):
            return
        self.con200goodn = ngoodbarcodes
        outpath = self._outpath
        params = self._params

        self._panel_progress.append_log(
            f"  Good barcodes MSA phase 1: {ngoodbarcodes}", "info")

        if not self._is_live() or getattr(self, "_live_finalizing", False):
            try:
                demsheet = self.wb.add_worksheet("2a Consensus by length")
                trans_header = "Marker (non-Coding)" if params.get("non_coi", False) else "Translation"
                headers = ["SpecimenID", "Demultiplexed Seqs", "Phase", "Length", "Barcode", trans_header]
                for c, h in enumerate(headers):
                    demsheet.write(0, c, h)
                for i, j in enumerate(sorted(self.sampleids.keys())):
                    demsheet.write(i+1, 0, j)
                    demsheet.write(i+1, 1, self.sampleids[j])
                    demsheet.write(i+1, 2, "Consensus by length")
                    try:
                        demsheet.write(i+1, 3, self.con200length[j])
                        demsheet.write(i+1, 4, self.con200barcodes[j])
                        demsheet.write(i+1, 5, self.con200trans[j])
                    except KeyError:
                        demsheet.write(i+1, 3, "NA")
                        demsheet.write(i+1, 4, "NA")
                        demsheet.write(i+1, 5, "NA")
            except Exception as e:
                self._panel_progress.append_log(f"  Warning Excel 2a: {e}", "warn")

        sim_dir = os.path.join(outpath, "2b_ConsensusBySimilarity", "90perc")
        if not os.path.isdir(sim_dir):
            self._panel_progress.append_log(
                "  There is no 90perc folder. — skipping phase 2b.", "warn")
            self._on_msa_to_phase3(0)
            return

        partlist = sorted(fnmatch.filter(os.listdir(sim_dir), "*"))
        if not partlist:
            self._panel_progress.append_log(
                "  There is no 90perc folder. — skipping phase 2b.", "warn")
            self._on_msa_to_phase3(0)
            return

        self._panel_progress.append_log(
            f"  Launching consensus by similarity ({len(partlist)} samples)...", "info")
        self._panel_progress.update_phase_progress("2b", 0, len(partlist))

        os.makedirs(os.path.join(outpath, "2b_ConsensusBySimilarity", "90perc_mafft"),
                    exist_ok=True)
        os.makedirs(os.path.join(outpath, "barcodesets", "consensus_by_similarity"),
                    exist_ok=True)

        job = [
            partlist,
            outpath,
            os.path.join("2b_ConsensusBySimilarity", "90perc"),
            os.path.join("2b_ConsensusBySimilarity", "90perc"),
            0,
            "90perc_barcodes.fa",
            0,
            params["explen"],
            0,
            params["lendev"],
            "consensus_by_similarity",
            params["consfreqfixed"],
            [params["consfreqmin"], params["consfreqmax"]],
            params["consfreqstep"],
            params["gencode"],
            params.get("resolve_mixed", {}),
            self._gencode_by_sample(),
        ]

        self.myconsensus2 = runconsensusparts(job)
        self.myconsensus2.notifyProgress.connect(
            lambda i: (
                self._panel_progress.update_phase_progress("2b", i, len(partlist)),
                self._panel_progress.update_stat_ok(self.ngoodbarcodescounter)
            ))
        self.myconsensus2.taskFinished.connect(self._on_consensus2_done)
        self.myconsensus2.start()

    @_phase_callback
    def _on_consensus2_done(self, _=0):
        if getattr(self, '_stopped', False):
            return
        outpath = self._outpath

        self.n90trans = {}
        self.n90length = {}
        self.n90barcodes = {}
        self.n90cov = {}
        self.n90flags = {}
        self.corlist = []

        sim_dir = os.path.join(outpath, "2b_ConsensusBySimilarity", "90perc")
        if not os.path.isdir(sim_dir):
            self._on_msa_to_phase3(0)
            return

        resultlist = [[
            self.myconsensus2.transcheck,
            self.myconsensus2.conseqs,
            self.myconsensus2.flags,
            self.myconsensus2.coverages,
        ]]

        os.makedirs(os.path.join(outpath, "barcodesets", "temps"), exist_ok=True)

        # Truncate similarity files before writing,
        # ensuring they do not accumulate data from previous RT cycles
        open(os.path.join(outpath, "barcodesets", "consensus_by_similarity",
                          "90perc_barcodes.fa"), 'w').close()
        open(os.path.join(outpath, "barcodesets", "consensus_by_similarity",
                          "90perc_prederr_barcodes.fa"), 'w').close()

        with open(os.path.join(outpath, "barcodesets", "consensus_by_similarity",
                               "90perc_barcodes.fa"), 'a') as outfile, \
             open(os.path.join(outpath, "barcodesets", "temps",
                               "consensusgood_temp.fa"), 'a') as outfile2, \
             open(os.path.join(outpath, "barcodesets", "consensus_by_similarity",
                               "90perc_prederr_barcodes.fa"), 'a') as outfile3:
            for each in resultlist:
                for k in each[0]:
                    self.n90trans[k] = each[0][k]
                for k in each[1]:
                    if len(each[1][k]) != 0:
                        self.n90barcodes[k] = each[1][k]
                        self.n90length[k] = len(each[1][k])
                for k in each[3]:
                    self.n90cov[k] = each[3][k]
                for k in each[2]:
                    seq = each[1].get(k, "")
                    cov = each[3].get(k, "NA")
                    slen = len(seq) if seq else 0
                    self.n90flags[k] = each[2][k]
                    if each[2][k]:
                        outfile.write(f">{k}_all.fa;{slen};{cov}\n{seq}\n")
                        outfile2.write(f">{k}_all.fa;{slen};{cov}\n{seq}\n")
                        self.ngoodbarcodescounter += 1
                        self.corlist.append(k + "_all.fa")
                        self._panel_progress.append_log(f"  🟢 2b {k} — {slen} bp", "info")
                    else:
                        if slen != 0:
                            outfile.write(f">{k}_all.fa;{slen};{cov}\n{seq}\n")
                            outfile3.write(f">{k}_all.fa;{slen};{cov}\n{seq}\n")

        # ── Excel Sheet 2b Consensus by similarity ───────────────────────────
        if not self._is_live() or getattr(self, "_live_finalizing", False):
            try:
                sheet2b = self.wb.add_worksheet("2b Consensus by similarity")
                headers2b = ["SpecimenID", "Coverage for closest sequences(90% cutoff)",
                             "stage", "length", "barcode", "translation check"]
                for col2b, h2b in enumerate(headers2b):
                    sheet2b.write(0, col2b, h2b)
                for row2b, sample2b in enumerate(sorted(self.sampleids.keys())):
                    sheet2b.write(row2b+1, 0, sample2b)
                    cov2b = self.n90cov.get(sample2b, "NA")
                    sheet2b.write(row2b+1, 1, cov2b)
                    if sample2b in self.n90barcodes and self.n90barcodes[sample2b]:
                        sheet2b.write(row2b+1, 2, "Consensus by similarity")
                        sheet2b.write(row2b+1, 3, self.n90length.get(sample2b, "NA"))
                        sheet2b.write(row2b+1, 4, self.n90barcodes[sample2b])
                        sheet2b.write(row2b+1, 5, self.n90trans.get(sample2b, "NA"))
                    else:
                        for nc2b in range(2, 6):
                            sheet2b.write(row2b+1, nc2b, "NA")
            except Exception as e:
                self._panel_progress.append_log(f"  Warning Excel 2b: {e}", "warn")

        self._msacheck2()
        self._panel_progress.update_stat_ok(self.ngoodbarcodescounter)
        if self._is_live():
            self._panel_live_chart.record(
                self.ndemultiplexed,
                self._panel_progress.get_ok_value(),
                n_total=getattr(self, "totalseqs", 0),
                cycle=getattr(self, "_live_current_cycle", 0))

    def _msacheck2(self):
        outpath = self._outpath
        params = self._params

        self._panel_progress.append_log("  Phase 2b MSA verification...", "info")

        self.mycheckmsa2 = MSAcheck(
            outpath,
            params["explen"],
            "90perc_barcodes.fa",
            2,
            "90perc",
            0,
            "",
            "consensusgood_temp.fa",
            "90perc_prederr_barcodes.fa",
            0,
            "consensus_by_similarity",
            self.ngoodbarcodescounter,
            self.corlist,
            "90perc",
            {},
        )
        self.mycheckmsa2.notifyProgress2.connect(
            lambda v: self._panel_progress.update_phase_progress("2b", v[0], v[1]))
        self.mycheckmsa2.notifyProgress3.connect(
            lambda _: self._panel_progress.append_log("  MSA 2b built", "info"))
        self.mycheckmsa2.taskFinished.connect(self._on_msa_to_phase3)
        self.mycheckmsa2.start()

    @_phase_callback
    def _on_msa_to_phase3(self, n90goodn=0):
        if getattr(self, '_stopped', False):
            return
        outpath = self._outpath
        params = self._params

        self.n90goodn = n90goodn
        self._panel_progress.mark_phase_done(
            "2b", f"Phase 2b completed — {n90goodn} good barcodes by similarity")

        def builddict_sequences(infile):
            seqdict = {}
            try:
                with open(infile) as f:
                    l = f.readlines()
                    for i, j in enumerate(l):
                        if ">" in j:
                            seqdict[j.strip().replace(">", "")] = \
                                l[i+1].strip().replace("-", "").upper()
            except FileNotFoundError:
                pass
            return seqdict

        os.makedirs(os.path.join(outpath, "barcodesets", "temps"), exist_ok=True)

        consensus90stat = params.get("run_phase2b", True)
        con90done = False

        sim_dir = os.path.join(outpath, "2b_ConsensusBySimilarity", "90perc")
        if consensus90stat and os.path.isdir(sim_dir):
            try:
                rset = builddict_sequences(os.path.join(outpath, "barcodesets",
                    "consensus_by_length", "consensus_all_step1.fa"))
                pset = builddict_sequences(os.path.join(outpath, "barcodesets",
                    "consensus_by_similarity", "90perc_barcodes.fa"))
                rgoodset = builddict_sequences(os.path.join(outpath, "barcodesets",
                    "consensus_by_length", "consensusgood_predgood_barcodes.fa"))
                pgoodset = builddict_sequences(os.path.join(outpath, "barcodesets",
                    "consensus_by_similarity", "90perc_predgood_barcodes.fa"))

                gooddict = {}
                donelist = []
                with open(os.path.join(outpath, "barcodesets", "temps",
                                       "consensus_90perc_predgood_combined_barcodes.fa"), 'w') as outfile:
                    for k in pgoodset:
                        outfile.write(f">{k}\n{pgoodset[k]}\n")
                        gooddict[k.split(";")[0]] = pgoodset[k]
                        donelist.append(k.split(";")[0])
                    for k in rgoodset:
                        if k.split(";")[0] not in donelist:
                            outfile.write(f">{k}\n{rgoodset[k]}\n")
                            gooddict[k.split(";")[0]] = rgoodset[k]

                psetids = {k.split(';')[0]: k for k in pset}
                with open(os.path.join(outpath, "barcodesets", "temps",
                                       "consensus_90perc_prederr_combined_barcodes.fa"), 'w') as outfile:
                    for k in rset:
                        id_ = k.split(';')[0]
                        if id_ not in gooddict:
                            self.con200errn += 1
                            if id_ not in psetids:
                                outfile.write(f">{k};200random\n{rset[k]}\n")
                            else:
                                pid = psetids[id_]
                                outfile.write(f">{pid};90perc\n{pset[pid]}\n")
                con90done = True
            except Exception as e:
                self._panel_progress.append_log(f"  Warning 2b combine: {e}", "warn")

        if not con90done:
            rset = builddict_sequences(os.path.join(outpath, "barcodesets",
                "consensus_by_length", "consensus_all_step1.fa"))
            rgoodset = builddict_sequences(os.path.join(outpath, "barcodesets",
                "consensus_by_length", "consensusgood_predgood_barcodes.fa"))
            gooddict = {}
            donelist = []
            with open(os.path.join(outpath, "barcodesets", "temps",
                                   "consensus_90perc_predgood_combined_barcodes.fa"), 'w') as outfile:
                for k in rgoodset:
                    if k.split(";")[0] not in donelist:
                        outfile.write(f">{k}\n{rgoodset[k]}\n")
                        gooddict[k.split(";")[0]] = rgoodset[k]
            with open(os.path.join(outpath, "barcodesets", "temps",
                                   "consensus_90perc_prederr_combined_barcodes.fa"), 'w') as outfile:
                for k in rset:
                    id_ = k.split(';')[0]
                    if id_ not in gooddict:
                        self.con200errn += 1
                        outfile.write(f">{k};200random\n{rset[k]}\n")

        # CONTINUE TO PHASE 3 (MSA Correction)
        if params.get("run_phase3", True):
            self._run_msa_correction()
        else:
            self._printoutputs()

    # ──────────────────────────────────────────────────────────────────────────
    # PHASE 3: Correction by Barcode Comparison (runtoptwenty)
    # ──────────────────────────────────────────────────────────────────────────

    def _run_msa_correction(self):
        outpath = self._outpath
        params = self._params

        self._panel_progress.set_phase("3", "Phase 3: fixing barcodes by conparison...")
        os.makedirs(os.path.join(outpath, "3_ConsensusByBarcodeComparison"), exist_ok=True)
        os.makedirs(os.path.join(outpath, "barcodesets", "fixing"), exist_ok=True)

        def builddict_sequences(infile):
            seqdict = {}
            try:
                with open(infile) as f:
                    l = f.readlines()
                    for i, j in enumerate(l):
                        if ">" in j:
                            seqdict[j.strip().replace(">", "")] = \
                                l[i+1].strip().replace("-", "").upper()
            except FileNotFoundError:
                pass
            return seqdict

        musclergoodset = builddict_sequences(os.path.join(
            outpath, "barcodesets", "consensus_by_length",
            "consensusgood_predgood_barcodes.fa"))
        musclepgoodset = builddict_sequences(os.path.join(
            outpath, "barcodesets", "consensus_by_similarity",
            "90perc_predgood_barcodes.fa"))

        if not musclergoodset:
            self._panel_progress.append_log(
                "  empty predgood — reconstruction from phase 2a flags...", "warn")
            predgood_path = os.path.join(outpath, "barcodesets", "consensus_by_length",
                                         "consensusgood_predgood_barcodes.fa")
            with open(predgood_path, "w") as pgf:
                for k, flag in self.con200flags.items():
                    if flag and k in self.con200barcodes:
                        seq = self.con200barcodes[k]
                        cov = self.con200cov.get(k, "NA")
                        slen = len(seq)
                        pgf.write(f">{k}_all.fa;{slen};{cov};estgaps=0\n{seq}\n")
            musclergoodset = builddict_sequences(predgood_path)
            self._panel_progress.append_log(
                f"  Good barcodes rebuilt: {len(musclergoodset)}.", "info")

        gooddict = {}

        # ── Same as original (try/except IOError in fixbarcodessetup):
        # if musclepgoodset is empty (phase 2b did not run or did not produce file),
        # only musclergoodset barcodes are written, identical to the block
        # except IOError del original.
        with open(os.path.join(outpath, "barcodesets",
                               "Final_predgood_combined_barcodes.fa"), "w") as outfile, \
             open(os.path.join(outpath, "barcodesets",
                               "Final_all_combined_barcodes.fa"), "w") as outfile2:
            for k in musclergoodset:
                sample = k.split("_all.fa")[0]
                ambs = self.con200barcodes.get(sample, "").count("N")
                estg = k.split(";estgaps=")[1] if ";estgaps=" in k else "0"
                base = k.split(";estgaps=")[0]
                header = f">{base};ambs={ambs};estgaps={estg}\n"
                outfile.write(header + musclergoodset[k] + "\n")
                outfile2.write(header + musclergoodset[k] + "\n")
                gooddict[k.split(";")[0]] = musclergoodset[k]
            # Only add musclepgoodset if it exists (phase 2b file present)
            for k in musclepgoodset:
                sample = k.split("_all.fa")[0]
                if not self.n90barcodes.get(sample):
                    continue  # sample has no phase 2b data — ignore
                ambs = self.n90barcodes[sample].count("N")
                estg = k.split(";estgaps=")[1] if ";estgaps=" in k else "0"
                base = k.split(";estgaps=")[0]
                header = f">{base};ambs={ambs};estgaps={estg}\n"
                outfile.write(header + musclepgoodset[k] + "\n")
                outfile2.write(header + musclepgoodset[k] + "\n")
                gooddict[k.split(";")[0]] = musclepgoodset[k]

        self._n_predgood = len(gooddict)
        self._panel_progress.append_log(
            f"  Good barcodes (2a+2b): {self._n_predgood}", "info")

        self.seqdict = {}
        self.hapiddict = {}
        self.errbarcodeset = {}
        seqdict2 = {}
        refseqdict = {}
        refseqdict2 = {}

        prederr_file = os.path.join(outpath, "barcodesets", "temps",
                                    "consensus_90perc_prederr_combined_barcodes.fa")
        try:
            with open(prederr_file) as infile:
                l = infile.readlines()
                for i, j in enumerate(l):
                    if ">" in j:
                        samplid = j[1:].split(";")[0]
                        self.errbarcodeset[samplid] = j + l[i+1]
                        if samplid not in gooddict:
                            seq = l[i+1].strip()
                            if seq in seqdict2:
                                seqdict2[seq].append(j.strip()[1:])
                            else:
                                seqdict2[seq] = [j.strip()[1:]]
                            self.hapiddict[j.strip()[1:]] = seqdict2[seq][0]
        except FileNotFoundError:
            self._panel_progress.append_log(
                "  No incorrect barcodes for phase 3.", "info")

        try:
            with open(os.path.join(outpath, "barcodesets",
                                   "Final_predgood_combined_barcodes.fa")) as infile:
                l = infile.readlines()
                for i, j in enumerate(l):
                    if ">" in j:
                        seq = l[i+1].strip()
                        if seq in refseqdict2:
                            refseqdict2[seq].append(j.strip()[1:])
                        else:
                            refseqdict2[seq] = [j.strip()[1:]]
        except FileNotFoundError:
            pass

        for each in seqdict2:
            m = sorted(seqdict2[each])
            m = ["tofix-" + m[0]] + m
            self.seqdict[each] = m
        for each in refseqdict2:
            refseqdict[each] = sorted(refseqdict2[each])

        partlist = sorted(self.seqdict.keys())
        if not partlist:
            self._panel_progress.append_log(
                "  No incorrect barcodes for phase 3.", "info")
            self._panel_progress.mark_phase_done("3", "Phase 3 completed (no errors)")
            self._printoutputs()
            return

        self._panel_progress.update_phase_progress("3", 0, len(partlist))
        self._panel_progress.append_log(
            f"  Correcting {len(partlist)} erroneus barcodes...", "info")

        nparts = [[partlist, self.seqdict, refseqdict,
                   os.path.join(outpath, "3_ConsensusByBarcodeComparison"), 0]]
        self.myfix = runtoptwenty(nparts[0])
        self.myfix.notifyProgress.connect(
            lambda i: self._panel_progress.update_phase_progress(
                "3", i, len(partlist)))
        self.myfix.taskFinished.connect(self._on_fix_done)
        self.myfix.start()

    @_phase_callback
    def _on_fix_done(self, _=0):
        if getattr(self, '_stopped', False):
            return
        self._panel_progress.append_log(
            "  Alignment correction completed.", "info")
        self._fixbarcodes()

    def _fixbarcodes(self):
        outpath = self._outpath
        params = self._params

        from collections import Counter as _Counter
        from Bio.Seq import Seq

        def consensus(indict, perc_thresh):
            if not indict:
                return ""
            vals = list(indict.values())
            poslist = []
            n = 0
            while n < len(vals[0]):
                newlist = []
                for each in indict:
                    try:
                        newlist.append(indict[each][n])
                    except IndexError:
                        break
                poslist.append(newlist)
                n += 1
            sequence = []
            for character in poslist:
                cc = _Counter(character)
                baseset = {k: v for k, v in cc.items()
                           if float(v)/float(len(character)) > perc_thresh}
                if len(baseset) == 0:
                    bp = "N"
                elif len(baseset) == 1:
                    bp = list(baseset.keys())[0]
                else:
                    bp = "N"
                sequence.append(bp)
            return "".join(sequence)

        def callconsensus(filepath, perc_thresh):
            with open(filepath) as infile:
                l = infile.readlines()
            seqdict, poslist = {}, []
            for i, j in enumerate(l):
                if ">" in j:
                    poslist.append(i)
            refseq = ""
            for i, j in enumerate(poslist):
                k01 = l[j].strip().split(">")[1]
                k3 = l[j+1:poslist[i+1]] if i != len(poslist)-1 else l[j+1:]
                k4 = "".join(k3).replace("\n", "")
                if "tofix-" not in k01:
                    seqdict[k01] = k4
                else:
                    refseq = k4
            return consensus(seqdict, perc_thresh), seqdict, refseq

        def get_cor_frame(seq, gencode):
            seqset = [Seq(seq), Seq(seq[1:]), Seq(seq[2:]),
                      Seq(seq).reverse_complement(),
                      Seq(seq[:-1]).reverse_complement(),
                      Seq(seq[:-2]).reverse_complement()]
            maxlen, corframe = 0, 0
            for i, s in enumerate(seqset):
                a = s.translate(table=gencode, to_stop=True).__str__()
                if len(a) > maxlen:
                    maxlen, corframe = len(a), i + 1
            return corframe

        def translate_corframe(seq, gencode):
            seq = seq.replace("-", "")
            if not seq:
                return "0"
            cf = get_cor_frame(seq, gencode)
            seqset = [seq, seq[1:], seq[2:],
                      Seq(seq).reverse_complement().__str__(),
                      Seq(seq[:-1]).reverse_complement().__str__(),
                      Seq(seq[:-2]).reverse_complement().__str__()]
            s = seqset[cf - 1]
            t = Seq(s).translate(table=gencode, to_stop=True).__str__()
            return "1" if len(t) == int(len(s) / 3) else "0"

        def orf_trim(seq, gencode):
            """ORF en marco más largo sin paro interno. Si ya traduce limpio se
            devuelve intacto (COI); si hay un paro interno + cola 3' no codificante
            (amplicón que abarca el codón de terminación del gen, p. ej. CytB) se
            recorta el paro y el 3'. Devuelve (seq_recortada, n_aa)."""
            s = seq.replace("-", "")
            if not s:
                return "", 0
            frames = [s, s[1:], s[2:],
                      Seq(s).reverse_complement().__str__(),
                      Seq(s[:-1]).reverse_complement().__str__(),
                      Seq(s[:-2]).reverse_complement().__str__()]
            best_fs, best_aa = s, 0
            for fs in frames:
                aa = Seq(fs).translate(table=gencode, to_stop=True).__str__()
                if len(aa) > best_aa:
                    best_aa, best_fs = len(aa), fs
            if best_aa * 3 >= len(s) - 2:
                return s, best_aa
            return best_fs[:best_aa * 3], best_aa

        def change_ext_gaps(sequence):
            bps_base = ["A", "T", "G", "C", "N"]
            start_pos, end_pos = 0, 0
            for i, bp in enumerate(sequence):
                if bp in bps_base:
                    start_pos = i - 1
                    break
            for i, bp in enumerate(sequence[::-1]):
                if bp in bps_base:
                    end_pos = len(sequence) - i
                    break
            return ("?" * (start_pos + 1) +
                    sequence[start_pos + 1:end_pos] +
                    "?" * (len(sequence) - end_pos))

        fix_dir = os.path.join(outpath, "3_ConsensusByBarcodeComparison")
        fixed_fa = os.path.join(outpath, "barcodesets", "fixing", "fixedbarcodes.fa")
        final_all = os.path.join(outpath, "barcodesets", "Final_all_combined_barcodes.fa")
        gencode = params["gencode"]
        dirlist = sorted(fnmatch.filter(os.listdir(fix_dir), "*aln.fa"))
        self.nfixed = 0

        with open(fixed_fa, "w") as gfile:
            for each in dirlist:
                try:
                    conseq, seqdict, refseq = callconsensus(
                        os.path.join(fix_dir, each), 0.5)
                    if not seqdict or not conseq:
                        continue
                    errcount = 0
                    newseq = ""
                    refseq = change_ext_gaps(refseq.upper())
                    conseq = conseq.upper()
                    for i, j in enumerate(refseq):
                        if j == "-":
                            if conseq[i] != "-":
                                errcount += 1
                                newseq += "N"
                        elif j != "?":
                            if conseq[i] == "-":
                                errcount += 1
                            else:
                                newseq += j
                    newseq = newseq.upper()
                    # Recorta el ORF limpio (quita el codón de paro del gen + cola
                    # 3'); acepta si cubre ≥95% de la reconstrucción. El barcode
                    # corregido se escribe ya recortado (sin paro → válido BOLD).
                    _orf_bp = newseq.replace("-", "").replace("?", "")
                    _orf, _aalen = orf_trim(_orf_bp, gencode)
                    if _orf_bp and _aalen * 3 >= len(_orf_bp) * 0.95:
                        newseq = _orf
                        key = refseq.replace("-", "").replace("?", "")
                        for barcode in self.seqdict[key][1:]:
                            sample = barcode.split("_all.fa")[0]
                            cov = self.n90cov.get(sample,
                                  self.con200cov.get(sample, "NA"))
                            gfile.write(
                                f">{barcode.split(';')[0]};{len(newseq)};"
                                f"{cov};ambs={newseq.count('N')};"
                                f"estgaps={errcount}\n{newseq}\n")
                            self.nfixed += 1
                except Exception as e:
                    self._panel_progress.append_log(
                        f"  Phase 3 fix skipped for {each}: "
                        f"{type(e).__name__}: {e}", "warn")

        with open(fixed_fa) as infile2, open(final_all, "a") as outfile:
            for line in infile2:
                outfile.write(line)

        self._panel_progress.mark_phase_done(
            "3", f"Phase 3: {self.nfixed} barcodes with indels corrected")
        self._panel_progress.append_log(
            f"  Barcodes with indels corrected: {self.nfixed}", "ok")

        self._printoutputs()

    def _printoutputs(self):
        outpath = self._outpath
        explen = self._params["explen"]

        os.makedirs(os.path.join(outpath, "Main_barcode_results"), exist_ok=True)
        os.makedirs(os.path.join(outpath, "barcodesets", "temps"), exist_ok=True)

        self.nfinal = 0
        self.nsinfinalbarcodes = 0
        self.nperfectbarcodes = 0
        self.nfilteredbarcodes = 0
        self.n1to5errbarcodes = 0
        self.n6to10errbarcodes = 0
        self.n11to15errbarcodes = 0
        self.nover16errbarcodes = 0
        self.nerr = 0
        # Per-sample barcode info (len / Ns / indels) for the Sample QC status sheet.
        self._final_bc_info = {}

        # ── Same as printoutputs1 of original: reload errbarcodeset from
        # the file, ensuring it is always in the correct state
        # regardless of the path by which this method was arrived at.
        self.errbarcodeset = {}
        prederr_combined = os.path.join(outpath, "barcodesets", "temps",
                                        "consensus_90perc_prederr_combined_barcodes.fa")
        if os.path.isfile(prederr_combined):
            with open(prederr_combined) as infile:
                l = infile.readlines()
                for i, j in enumerate(l):
                    if ">" in j:
                        samplid = j[1:].split(";")[0]
                        self.errbarcodeset[samplid] = j + l[i+1]

        perfectlist = []
        filteredlist = []
        n1to5errlist = []
        n6to10errlist = []
        n11to15errlist = []
        nover16list = []
        alllist = []

        final_all = os.path.join(outpath, "barcodesets", "Final_all_combined_barcodes.fa")

        if not os.path.isfile(final_all):
            self._panel_progress.append_log(
                "  Final_all_combined_barcodes.fa not found", "warn")
            self._finish_analysis()
            return

        main = os.path.join(outpath, "Main_barcode_results")
        file_map = {
            "QC_Compliant_barcodes_noamb_noerr.fa": open(os.path.join(main, "QC_Compliant_barcodes_noamb_noerr.fa"), 'w'),
            "Filtered_barcodes_1percamb_upto5err.fa": open(os.path.join(main, "Filtered_barcodes_1percamb_upto5err.fa"), 'w'),
            "Allbarcodes.fa": open(os.path.join(main, "Allbarcodes.fa"), 'w'),
            "Fixed_barcodes_1to5err.fa": open(os.path.join(main, "Fixed_barcodes_1to5err.fa"), 'w'),
            "Fixed_barcodes_6to10err.fa": open(os.path.join(main, "Fixed_barcodes_6to10err.fa"), 'w'),
            "Fixed_barcodes_11to15err.fa": open(os.path.join(main, "Fixed_barcodes_11to15err.fa"), 'w'),
            "Fixed_barcodes_16to20err.fa": open(os.path.join(main, "Fixed_barcodes_16to20err.fa"), 'w'),
        }

        try:
            with open(final_all) as infile:
                l = infile.readlines()
                for i, j in enumerate(l):
                    if ">" in j:
                        next_line = l[i+1] if i + 1 < len(l) else ""
                        self.nfinal += 1
                        seq = next_line.strip()
                        self.nsinfinalbarcodes += seq.count("N")
                        file_map["Allbarcodes.fa"].write(j + next_line)
                        alllist.append(j.split(";")[0][1:])

                        estgaps = int(j.split("estgaps=")[1].strip()) if "estgaps=" in j else 0
                        n_ambs = seq.count("N")

                        _skey = j.split(";")[0][1:].replace("_all.fa", "")
                        self._final_bc_info[_skey] = {
                            "len": len(seq), "ambs": n_ambs, "estgaps": estgaps}

                        if n_ambs == 0 and estgaps == 0:
                            file_map["QC_Compliant_barcodes_noamb_noerr.fa"].write(j + next_line)
                            self.nperfectbarcodes += 1
                            perfectlist.append(j.split(";")[0][1:])

                        if n_ambs <= explen * 0.01 and estgaps <= 5:
                            file_map["Filtered_barcodes_1percamb_upto5err.fa"].write(j + next_line)
                            self.nfilteredbarcodes += 1
                            if j.split(";")[0][1:] not in perfectlist:
                                filteredlist.append(j.split(";")[0][1:])

                        if estgaps > 0 and estgaps <= 5:
                            file_map["Fixed_barcodes_1to5err.fa"].write(j + next_line)
                            self.n1to5errbarcodes += 1
                            n1to5errlist.append(j.split(";")[0][1:])
                        if estgaps > 5 and estgaps <= 10:
                            file_map["Fixed_barcodes_6to10err.fa"].write(j + next_line)
                            self.n6to10errbarcodes += 1
                            n6to10errlist.append(j.split(";")[0][1:])
                        if estgaps > 10 and estgaps <= 15:
                            file_map["Fixed_barcodes_11to15err.fa"].write(j + next_line)
                            self.n11to15errbarcodes += 1
                            n11to15errlist.append(j.split(";")[0][1:])
                        if estgaps > 15:
                            file_map["Fixed_barcodes_16to20err.fa"].write(j + next_line)
                            self.nover16errbarcodes += 1
                            nover16list.append(j.split(";")[0][1:])
        finally:
            for fh in file_map.values():
                fh.close()

        # Authoritative QC count after Phase 3: force it so the card reflects
        # corrections that reduce the total (e.g. 80 after 2b → 79 after Phase 3).
        self._panel_progress.update_stat_ok(self.nperfectbarcodes, force=True)
        non_coi = self._params.get("non_coi", False)
        if non_coi:
            self._panel_progress.mark_phase_done(
                "2a", f"Phase 2a completed (non-Coding) — "
                      f"{self.nperfectbarcodes} QC Compliant / {self.nfinal} total")
        else:
            self._panel_progress.mark_phase_done(
                "3", f"Phase 3: {getattr(self, 'nfixed', 0)} corrected — "
                     f"{self.nperfectbarcodes} QC Compliant / {self.nfinal} total")

        # ── Excel Sheet 3.Final barcodes ─────────────────────────────────────
        if not self._is_live() or getattr(self, "_live_finalizing", False):
            try:
                sheet3 = self.wb.add_worksheet("3.Final barcodes")
                non_coi_xl = self._params.get("non_coi", False)
                trans_col = "length check" if non_coi_xl else "translation check"
                headers3 = ["SpecimenID", "Number of sequences demultiplexed",
                             "Number used for generating barcodes", "stage", "type",
                             "length", "barcode", trans_col, "#ambiguities"]
                for c, h in enumerate(headers3):
                    sheet3.write(0, c, h)
                row3 = 1
                if os.path.isfile(final_all):
                    with open(final_all) as fa_in:
                        lines3 = fa_in.readlines()
                    for i3, j3 in enumerate(lines3):
                        if ">" in j3:
                            seq3 = lines3[i3+1].strip() if i3+1 < len(lines3) else ""
                            parts3 = j3.strip().lstrip(">").split(";")
                            sample3 = parts3[0].replace("_all.fa", "")
                            slen3 = parts3[1] if len(parts3) > 1 else "NA"
                            cov3 = parts3[2] if len(parts3) > 2 else "NA"
                            ambs3 = int(parts3[3].split("=")[1]) if len(parts3) > 3 and "=" in parts3[3] else seq3.count("N")
                            estgaps3 = int(parts3[4].split("=")[1]) if len(parts3) > 4 and "=" in parts3[4] else 0
                            # determine type (stage)
                            if sample3 in self.n90barcodes:
                                stage3 = "Consensus by similarity"
                                if estgaps3 > 0:
                                    stage3 += ", fixed indel"
                                btype3 = f"removed {estgaps3} indels" if estgaps3 > 0 else "correct"
                            else:
                                stage3 = "Consensus by length"
                                if estgaps3 > 0:
                                    stage3 += ", fixed indel"
                                btype3 = f"removed {estgaps3} indels" if estgaps3 > 0 else "correct"
                            cov_num3 = self.n90cov.get(sample3, self.con200cov.get(sample3, "NA"))
                            trans3 = 1 if (ambs3 == 0 and estgaps3 == 0) else 0
                            sheet3.write(row3, 0, sample3)
                            sheet3.write(row3, 1, self.sampleids.get(sample3, "NA"))
                            sheet3.write(row3, 2, cov_num3)
                            sheet3.write(row3, 3, stage3)
                            sheet3.write(row3, 4, btype3)
                            sheet3.write(row3, 5, int(slen3) if str(slen3).isdigit() else slen3)
                            sheet3.write(row3, 6, seq3)
                            sheet3.write(row3, 7, trans3)
                            sheet3.write(row3, 8, ambs3)
                            row3 += 1
            except Exception as e:
                self._panel_progress.append_log(f"  Warning Excel 3: {e}", "warn")

            # ── Sheet: Sample QC status ──────────────────────────────────────
            # One row per INITIAL (demultiplexed) sample with its outcome, so the
            # failures and over-threshold barcodes can be spotted/filtered at a
            # glance. "Accepted" = Filtered threshold (≤1% Ns and ≤5 indels).
            try:
                max_amb = explen * 0.01
                ws = self.wb.add_worksheet("Sample QC status")
                try:
                    fmt_hdr = self.wb.add_format({"bold": True})
                    fmt_green = self.wb.add_format({"bg_color": "#C6EFCE"})
                    fmt_amber = self.wb.add_format({"bg_color": "#FFEB9C"})
                    fmt_red = self.wb.add_format({"bg_color": "#FFC7CE"})
                except Exception:
                    fmt_hdr = fmt_green = fmt_amber = fmt_red = None
                hdrs = ["SpecimenID", "Reads demultiplexed", "Barcode produced",
                        "Status", "Length (bp)", "#Ns", "#indels"]
                for c, h in enumerate(hdrs):
                    ws.write(0, c, h, fmt_hdr)
                # Every sample DEFINED in the CSV (incl. those with 0 reads), not
                # only the ones that were demultiplexed.
                all_samples = sorted(set(getattr(self, "_all_csv_samples", []) or [])
                                     | set(self.sampleids.keys()))
                r = 1
                for skey in all_samples:
                    reads = self.sampleids.get(skey, 0)
                    info = self._final_bc_info.get(skey)
                    if info is None:
                        produced = "no"
                        status = ("No reads (not demultiplexed)"
                                  if not reads else "No barcode (unresolved)")
                        blen, nns, ngaps, rowfmt = "", "", "", fmt_red
                    else:
                        blen = info["len"]
                        nns = info["ambs"]
                        ngaps = info["estgaps"]
                        produced = "yes"
                        if nns == 0 and ngaps == 0:
                            status, rowfmt = "QC-compliant", fmt_green
                        elif nns <= max_amb and ngaps <= 5:
                            status, rowfmt = ("Filtered (within accepted errors)",
                                              fmt_amber)
                        else:
                            status, rowfmt = ("Exceeds accepted errors", fmt_red)
                    rowvals = [skey, reads, produced, status, blen, nns, ngaps]
                    for c, val in enumerate(rowvals):
                        ws.write(r, c, val, rowfmt)
                    r += 1
                # Filterable + frozen header for quick scanning.
                if r > 1:
                    ws.autofilter(0, 0, r - 1, len(hdrs) - 1)
                ws.freeze_panes(1, 0)
            except Exception as e:
                self._panel_progress.append_log(
                    f"  Warning Excel Sample QC status: {e}", "warn")

        with open(os.path.join(main, "Remaining.fa"), 'w') as outfile1:
            for each in self.errbarcodeset:
                if each not in alllist:
                    outfile1.write(self.errbarcodeset[each])
                    self.nerr += 1

        for subdir in ["QC_Compliant", "Filtered", "1to5errors", "6to10errors",
                       "11to15errors", "Over16errors", "Remaining"]:
            os.makedirs(os.path.join(main, subdir), exist_ok=True)

        self._panel_progress.append_log(
            f"  Final results: {self.nfinal} barcodes | "
            f"{self.nperfectbarcodes} no errors | "
            f"{self.nfilteredbarcodes} filtered | "
            f"{self.nerr} unresolved", "info")

        # Write .fa files at the end of the loop (if RT provisional, they are overwritten)
        self._live_write_consensus_files_from_main(main)

        self._finish_analysis()

    # ══════════════════════════════════════════════════════════════════════════
    # HTML REPORT
    # ══════════════════════════════════════════════════════════════════════════

    def _generate_html_report(self, outpath: str, summary: dict, timeline: list):
        """
        Generate report.html in outpath.
        It works for both conventional mode (timeline=[]) and RT (timeline with data).
        The graphics are embedded in base64 so that the HTML is completely autonomous.
        """
        import base64, json

        mode      = summary.get("mode", "Conventional")
        is_rt     = bool(timeline)
        run_name  = os.path.basename(outpath)
        ts_now    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elapsed   = summary.get("elapsed", "—")
        non_coi   = summary.get("params", {}).get("non_coi", False)

        # ── Read PNG graphics and convert to base64 ──────────────────────────
        def _png_b64(fname):
            p = os.path.join(outpath, fname)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    return base64.b64encode(f.read()).decode()
            return ""

        b64_reads = _png_b64("chart_reads.png")
        b64_ok    = _png_b64("chart_barcodes_ok.png")

        # ── Data for JS inline graphs (if there is a timeline) ─────────────────
        # Force point (0,0) at the beginning of the graphs
        if timeline and len(timeline) > 0:
            # Crear una copia del timeline con un punto inicial en (0,0)
            extended_timeline = [{"min": 0, "total": 0, "dem": 0, "ok": 0, "ts": "Start", "cycle": 0}]
            extended_timeline.extend(timeline)
        else:
            extended_timeline = timeline if timeline else []

        # X axis in hours for easier reading on long runs (timeline keeps minutes).
        tl_labels = json.dumps([round(r.get("min", 0) / 60.0, 2) for r in extended_timeline])
        tl_total  = json.dumps([r.get("total", 0) for r in extended_timeline])
        tl_dem    = json.dumps([r.get("dem", 0) for r in extended_timeline])
        tl_ok     = json.dumps([r.get("ok", 0) for r in extended_timeline])
        tl_ts     = json.dumps([r.get("ts", "") for r in extended_timeline])
        tl_cycle  = json.dumps([r.get("cycle", 0) for r in extended_timeline])

        # ── Timeline table ──────────────────────── ────────────────────────
        # record() fires several times per cycle (after phase 2a, after 2b and
        # at the end of the cycle), so the raw timeline holds several rows per
        # cycle. For the table we keep only the LAST record of each cycle — its
        # final state. The full timeline is left untouched for the charts so they
        # keep their fine resolution. dict() preserves first-seen cycle order and
        # the overwrite keeps the latest values, which is what we want here.
        _last_by_cycle = {}
        for r in timeline:
            _last_by_cycle[r.get("cycle", 0)] = r
        tl_rows_html = ""
        for r in _last_by_cycle.values():
            tl_rows_html += (
                f"<tr><td>{r.get('cycle', '—')}</td>"
                f"<td>{r.get('ts','')}</td>"
                f"<td>{r.get('min',0):.1f}</td>"
                f"<td>{r.get('total',0):,}</td>"
                f"<td>{r.get('dem',0):,}</td>"
                f"<td>{r.get('ok',0)}</td></tr>\n"
            )

        # ── Sample table (from sampleids) ─────────────────────────────
        sample_rows = ""
        _con200bc  = getattr(self, 'con200barcodes', {})
        _n90bc     = getattr(self, 'n90barcodes', {})
        _con200cov = getattr(self, 'con200cov', {})
        _n90cov    = getattr(self, 'n90cov', {})
        for sid, cnt in sorted(getattr(self, 'sampleids', {}).items(),
                                key=lambda x: -x[1]):
            if sid in _con200bc:
                has_bc = "✓"
                cov_val = _con200cov.get(sid, "—")
                ambs_val = _con200bc[sid].count("N")
            elif sid in _n90bc:
                has_bc = "✓"
                cov_val = _n90cov.get(sid, "—")
                ambs_val = _n90bc[sid].count("N")
            else:
                has_bc, cov_val, ambs_val = "—", "—", "—"
            sample_rows += (
                f"<tr><td>{sid}</td><td style='text-align:center'>{cnt:,}</td><td style='text-align:center'>{has_bc}</td>"
                f"<td style='text-align:center'>{cov_val}</td><td style='text-align:center'>{ambs_val}</td></tr>\n"
            )

        # ── Phase 2a breakdown by coverage ───────────────────────────────
        _cov_breakdown = summary.get("cov2a_counts", {})

        _covlist_str = summary.get("params", {}).get("coveragelist", "")
        _covlist = []
        if _covlist_str:
            try:
                _covlist = sorted(
                    [int(x.strip()) for x in str(_covlist_str).split(",") if x.strip()])
            except Exception:
                pass
        if not _covlist and _cov_breakdown:
            _covlist = sorted(_cov_breakdown.keys())

        if _cov_breakdown and _covlist:
            _cov_rows = "".join(
                f"<tr><td>{c}x</td><td>{_cov_breakdown.get(c, 0)}</td></tr>"
                for c in _covlist
            )
            phase2a_cov_html = f"""
    <div style="margin-top:14px;">
      <div style="font-family:var(--mono);font-size:12px;color:var(--muted);
                  letter-spacing:.5px;text-transform:uppercase;margin-bottom:8px;">
        Breakdown by coverage — Phase 2a
      </div>
      <div class="table-wrap" style="max-width:260px;">
        <table>
          <thead><tr><th>Coverage</th><th>Barcodes</th></tr></thead>
          <tbody>{_cov_rows}</tbody>
        </table>
      </div>
    </div>"""
        else:
            phase2a_cov_html = ""

        # ──Graphics section ─────────────────────── ───────────────────────
        if is_rt and timeline:
            charts_section = f"""
            <section class="section" id="sec-charts">
              <h2 class="section-title">Real-Time charts</h2>
              <div class="charts-grid">
                <div class="chart-card">
                  <div class="chart-label">Demultiplexed and total reads</div>
                  <canvas id="chartReads"></canvas>
                </div>
                <div class="chart-card">
                  <div class="chart-label">Accumulated QC Compliant Barcodes</div>
                  <canvas id="chartOk"></canvas>
                </div>
              </div>
            </section>"""
            charts_js = f"""
            <script>
            const tl_labels = {tl_labels};
            const tl_total  = {tl_total};
            const tl_dem    = {tl_dem};
            const tl_ok     = {tl_ok};
            const tl_ts     = {tl_ts};

            function mkChart(id, datasets, yLabel) {{
              const ctx = document.getElementById(id);
              if (!ctx) return;
              new Chart(ctx, {{
                type: 'line',
                data: {{ labels: tl_labels, datasets }},
                options: {{
                  responsive: true,
                  animation: {{ duration: 600, easing: 'easeInOutQuart' }},
                  interaction: {{ mode: 'index', intersect: false }},
                  plugins: {{
                    legend: {{ labels: {{
                      color: '#e2e8f0', font: {{ family: 'DM Mono', size: 11 }},
                      boxWidth: 12, boxHeight: 12,
                      generateLabels: function(chart) {{
                        const orig = Chart.defaults.plugins.legend.labels.generateLabels(chart);
                        orig.forEach(l => {{ l.fillStyle = l.strokeStyle; l.lineWidth = 0; }});
                        return orig;
                      }}
                    }} }},
                    tooltip: {{
                      backgroundColor: 'rgba(15,23,42,0.92)',
                      titleColor: '#94a3b8',
                      bodyColor: '#e2e8f0',
                      borderColor: '#334155',
                      borderWidth: 1,
                      callbacks: {{
                        title: items => `t = ${{items[0].label}} h`,
                        afterBody: items => `${{tl_ts[items[0].dataIndex]}}`
                      }}
                    }}
                  }},
                  scales: {{
                    x: {{
                      title: {{ display: true, text: 'Time (h)', color: '#64748b' }},
                      ticks: {{ color: '#64748b', font: {{ family: 'DM Mono', size: 10 }} }},
                      grid: {{ color: 'rgba(100,116,139,0.15)' }},
                      min: 0,
                      suggestedMin: 0
                    }},
                    y: {{
                      title: {{ display: true, text: yLabel, color: '#64748b' }},
                      ticks: {{ color: '#64748b', font: {{ family: 'DM Mono', size: 10 }} }},
                      grid: {{ color: 'rgba(100,116,139,0.15)' }},
                      beginAtZero: true,
                      min: 0,
                      suggestedMin: 0
                    }}
                  }}
                }}
              }});
            }}

            mkChart('chartReads', [
              {{ label: 'Total reads',        data: tl_total, borderColor: '#38bdf8',
                backgroundColor: 'rgba(56,189,248,0.08)', borderWidth: 2,
                pointRadius: 3, fill: true, tension: 0 }},
              {{ label: 'Demultiplexed',       data: tl_dem,   borderColor: '#818cf8',
                backgroundColor: 'rgba(129,140,248,0.08)', borderWidth: 2,
                pointRadius: 3, fill: true, tension: 0 }}
            ], 'Reads');

            mkChart('chartOk', [
              {{ label: 'QC Compliant barcodes', data: tl_ok,   borderColor: '#34d399',
                backgroundColor: 'rgba(52,211,153,0.12)', borderWidth: 2,
                pointRadius: 4, fill: true, tension: 0 }}
            ], 'QC barcodes');
            </script>
            <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
            """
        elif b64_reads or b64_ok:
            # Conventional or RT mode without data: show PNGs if they exist
            img_reads = f'<img src="data:image/png;base64,{b64_reads}" alt="Reads chart">' if b64_reads else ""
            img_ok    = f'<img src="data:image/png;base64,{b64_ok}" alt="QC barcodes chart">' if b64_ok else ""
            charts_section = f"""
            <section class="section" id="sec-charts">
              <h2 class="section-title">Analysis charts</h2>
              <div class="charts-grid">
                {"<div class='chart-card'>" + img_reads + "</div>" if img_reads else ""}
                {"<div class='chart-card'>" + img_ok + "</div>" if img_ok else ""}
              </div>
            </section>""" if (img_reads or img_ok) else ""
            charts_js = ""
        else:
            charts_section = ""
            charts_js = ""

        # ── Timeline table section ───────────────────── ──────────────────────
        timeline_section = ""
        if tl_rows_html:
            timeline_section = f"""
            <section class="section" id="sec-timeline">
              <h2 class="section-title">Timeline RT</h2>
              <div class="table-wrap" style="max-height:400px; overflow-y:auto;">
                <table style="position: relative;">
                    <thead style="position: sticky; top: 0; background: var(--bg3); z-index: 10;">
                        <tr>
                        <th>Cycle</th><th>Date / Time</th><th>Min</th>
                        <th>Total reads</th><th>Demultiplexed</th>
                        <th>QC barcodes</th>
                        </tr>
                  </thead>
                  <tbody>{tl_rows_html}</tbody>
                </table>
              </div>
            </section>"""

        # ── Parameters section ───────────────────────── ──────────────────────────
        p = summary.get("params", {})
        if p:
            non_coi_lbl = "Yes" if p.get("non_coi") else "No"
            gc_lbl = "N/A (no-Coding)" if p.get("non_coi") else str(p.get("gencode", "?"))
            fases = [k.replace("run_","").upper() for k, v in p.items() if k.startswith("run_") and v]
            # Intra-sample variant detection (marker-agnostic)
            _rm = p.get("resolve_mixed", {}) or {}
            _rm_enabled = bool(_rm.get("enabled", False))
            _rm_lbl = "On" if _rm_enabled else "Off"
            if _rm_enabled:
                _rm_lbl += (f" (min secondary variant fraction: {_rm.get('min_secondary_frac','?')}; "
                            f"variant tolerance: {_rm.get('tolerance','?')}; "
                            f"derived polymorphism threshold: {_rm.get('minor_thresh','?')})")
                _rstats = getattr(self, "_resolve_stats", {}) or {}
                if _rstats.get("enabled"):
                    _rm_lbl += (f" — {_rstats.get('mixed',0)} mixed sample(s) resolved, "
                                f"{_rstats.get('recovered',0)} now QC-compliant")
                    if _rstats.get("needs_review"):
                        _rm_lbl += (f", {_rstats.get('needs_review')} need manual "
                                    f"review")
                    if _rstats.get("recovered_variants"):
                        _rm_lbl += (f"; {_rstats.get('recovered_variants')} secondary "
                                    f"variant(s) recovered "
                                    f"({_rstats.get('recovered_valid',0)} valid)")
            _rm_row = (f"<tr><td>Intra-sample variant detection (dominant haplotype)</td>"
                       f"<td>{_rm_lbl}</td></tr>")
            params_rows = (
                f"<tr><td>Non-Coding marker</td><td>{non_coi_lbl}</td></tr>"
                f"{_rm_row}"
                f"<tr><td>Genetic code</td><td>{gc_lbl}</td></tr>"
                f"<tr><td>Minimum length (bp)</td><td>{p.get('minlen','?')}</td></tr>"
                f"<tr><td>Barcode length (bp)</td><td>{p.get('explen','?')}</td></tr>"
                f"<tr><td>Window of barcode length ± (bp)</td><td>{p.get('demlen','?')}</td></tr>"
                f"<tr><td>Maximum read length deviation from barcode length</td><td>{p.get('lendev','?')}</td></tr>"
                f"<tr><td>Read quality filter (min mean Q)</td><td>{('Q ≥ ' + str(p.get('minq'))) if p.get('minq') else 'Off'}</td></tr>"
                f"<tr><td>Minimum read coverage</td><td>{p.get('mincoverage','?')}</td></tr>"
                f"<tr><td>Primer mismatches allowed</td><td>{p.get('primermismatch','?')}</td></tr>"
                f"<tr><td>Tag mismatches allowed</td><td>{p.get('tagmm','?')}</td></tr>"
                f"<tr><td>Phase 2a coverages</td><td>{p.get('coveragelist','?')}</td></tr>"
                f"<tr><td>Main consensus calling frequency</td><td>{p.get('consfreqfixed','?')} (rango {p.get('consfreqmin','?')}–{p.get('consfreqmax','?')})</td></tr>"
                f"<tr><td>Threads</td><td>{p.get('n_threads','?')}</td></tr>"
                f"<tr><td>Active phases</td><td>{', '.join(fases)}</td></tr>"
            )
            params_section = f"""
            <section class="section" id="sec-params">
              <h2 class="section-title">Analysis parameters</h2>
              <div class="table-wrap">
                <table>
                  <thead><tr><th style="width:40%">Parameter</th><th>Value</th></tr></thead>
                  <tbody>{params_rows}</tbody>
                </table>
              </div>
            </section>"""
        else:
            params_section = ""

        # ── Sample table section ─────────────────────────────────────────────
        samples_section = ""
        if sample_rows:
            samples_section = f"""
            <section class="section" id="sec-samples">
              <h2 class="section-title">Demultiplexed samples</h2>
              <div class="table-wrap" style="max-height:650px; overflow-y:auto;">
                <table style="position: relative;">
                    <thead style="position: sticky; top: 0; background: var(--bg3); z-index: 10;">
                        <tr><th style="width:20%">Sample ID</th><th style="text-align:center">Assigned reads</th><th style="text-align:center">Barcode</th><th style="text-align:center">Coverage</th><th style="text-align:center">Ambiguities</th></tr>
                    </thead>
                  <tbody>{sample_rows}</tbody>
                </table>
              </div>
            </section>"""

        # ── Quality metrics ─────────────────────── ───────────────────────
        n_cycles   = summary.get("cycles", 0)
        n_total    = summary.get("total", 0)
        n_qc       = summary.get("qc_ok", 0)
        n_filt     = summary.get("filtered", 0)
        n_unres    = summary.get("unresolved", 0)
        n_2a       = summary.get("phase2a_n", 0)
        n_2b       = summary.get("phase2b_n", 0)
        n_3        = summary.get("phase3_n", 0)
        n_few      = summary.get("few_indels", 0)
        n_mid      = summary.get("mid_indels", 0)
        n_many     = summary.get("many_indels", 0)
        total_reads = getattr(self, 'totalseqs', '—')
        n_dem       = getattr(self, 'ndemultiplexed', '—')
        n_samples_dem = len(getattr(self, 'sampleids', {}))
        n_sam5      = getattr(self, 'nsampledemultiplexed5', '—')

        # Count total samples defined in the input CSV
        n_samples = n_samples_dem  # fallback if CSV is not readable
        _demfile = getattr(self, '_demfile', '')
        if _demfile and os.path.isfile(_demfile):
            try:
                import csv as _csv
                with open(_demfile, newline='', encoding='utf-8-sig') as _f:
                    n_samples = sum(1 for row in _csv.reader(_f)
                                    if any(c.strip() for c in row))
            except Exception:
                pass

        pct_qc   = f"{n_qc/n_total*100:.1f}%" if n_total else "—"
        pct_filt = f"{n_filt/n_total*100:.1f}%" if n_total else "—"

        total_reads_fmt = f"{total_reads:,}" if isinstance(total_reads, int) else str(total_reads)
        n_dem_fmt = f"{n_dem:,}" if isinstance(n_dem, int) else str(n_dem)
        pct_dem = (f"{n_dem/total_reads*100:.1f}%"
                   if isinstance(n_dem, int) and isinstance(total_reads, int) and total_reads > 0
                   else "—")

        mode_badge_cls = "badge-rt" if is_rt else "badge-conv"
        mode_badge_txt = "⚡ Real-Time" if is_rt else "▶ Conventional"

        # ── Chart.js script tag (only if RT with timeline) ───────────────────
        chartjs_cdn = ('<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0'
                       '/dist/chart.umd.min.js"></script>') if (is_rt and timeline) else ""

        # ── Input file labels for the hero section ───────────────────────────
        _fastq_name = os.path.basename(getattr(self, '_fastq', ''))
        _csv_name   = os.path.basename(getattr(self, '_demfile', ''))
        if is_rt:
            input_files_html = (
                f'<span>📄 CSV: <strong>{_csv_name}</strong></span>'
                if _csv_name else ''
            )
        else:
            parts = []
            if _fastq_name:
                parts.append(f'<span>🧬 FASTQ: <strong>{_fastq_name}</strong></span>')
            if _csv_name:
                parts.append(f'<span>📄 CSV: <strong>{_csv_name}</strong></span>')
            input_files_html = ''.join(parts)

        # ── RT cycles stat card (only for RT mode) ───────────────────────────
        rt_cycles_card = (
            f'  <div class="stat blue">\n'
            f'    <div class="stat-label">RT Cycles</div>\n'
            f'    <div class="stat-value" style="font-size:32px">{n_cycles}</div>\n'
            f'    <div class="stat-sub">Completed analysis cycles</div>\n'
            f'  </div>'
        ) if is_rt else ""

        html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ONTbarcoder — {run_name}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
{chartjs_cdn}
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg:        #0b1120;
    --bg2:       #111827;
    --bg3:       #1e2d40;
    --border:    rgba(148,163,184,0.12);
    --border2:   rgba(148,163,184,0.22);
    --text:      #e2e8f0;
    --muted:     #94a3b8;
    --accent1:   #38bdf8;
    --accent2:   #818cf8;
    --green:     #34d399;
    --amber:     #fbbf24;
    --red:       #f87171;
    --serif:     'DM Serif Display', Georgia, serif;
    --mono:      'DM Mono', 'Courier New', monospace;
    --sans:      'DM Sans', system-ui, sans-serif;
  }}

  html {{ scroll-behavior: smooth; }}

  body {{
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.6;
  }}

  /* Scroll adjustment to compensate for sticky navigation bar */
  .stats-grid,
  .section,
  [id^="sec-"] {{
    scroll-margin-top: 15px;
  }}

  /* ── NAV ── */
  nav {{
    position: sticky; top: 0; z-index: 100;
    background: rgba(11,17,32,0.85);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 24px;
    padding: 0 40px; height: 52px;
  }}
  nav .brand {{ font-family: var(--serif); font-size: 24px; color: var(--accent1);
                letter-spacing: -0.3px; white-space: nowrap; }}
  nav a {{ font-size: 18px; color: var(--muted); text-decoration: none;
           transition: color .2s; white-space: nowrap; }}
  nav a:hover {{ color: var(--text); }}
  nav .spacer {{ flex: 1; }}
  .badge-rt   {{ background: rgba(56,189,248,.15); color: var(--accent1);
                 border: 1px solid rgba(56,189,248,.3); }}
  .badge-conv {{ background: rgba(129,140,248,.15); color: var(--accent2);
                 border: 1px solid rgba(129,140,248,.3); }}
  .badge-rt, .badge-conv {{
    font-family: var(--mono); font-size: 16px; font-weight: 500;
    padding: 2px 10px; border-radius: 20px; letter-spacing: .3px;
  }}

  /* ── HERO ── */
  .hero {{
    padding: 24px 40px 48px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(160deg, rgba(56,189,248,.04) 0%, transparent 60%);
    position: relative; overflow: hidden;
  }}
  .hero::before {{
    content: ''; position: absolute; inset: 0;
    background: radial-gradient(ellipse 70% 60% at 80% 50%,
                rgba(129,140,248,.06) 0%, transparent 70%);
    pointer-events: none;
  }}
  .hero-eyebrow {{ font-family: var(--mono); font-size: 11px; color: var(--muted);
                   letter-spacing: 1.5px; text-transform: uppercase; margin-bottom: 12px; }}
  .hero h1 {{ font-family: var(--serif); font-size: clamp(28px,4vw,46px);
              line-height: 1.1; color: var(--text); margin-bottom: 16px; }}
  .hero h1 span {{ color: var(--accent1); }}
  .hero-meta {{ display: flex; flex-wrap: wrap; gap: 20px; align-items: center;
                font-size: 13px; color: var(--muted); margin-top: 20px; }}
  .hero-meta strong {{ color: var(--text); }}

  /* ── STATS GRID ── */
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 16px; padding: 40px 40px 0;
  }}
  .stat {{
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px 22px;
    position: relative; overflow: hidden;
    transition: border-color .2s, transform .2s;
  }}
  .stat:hover {{ border-color: var(--border2); transform: translateY(-2px); }}
  .stat::after {{
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    border-radius: 12px 12px 0 0;
  }}
  .stat.green::after  {{ background: var(--green); }}
  .stat.blue::after   {{ background: var(--accent1); }}
  .stat.indigo::after {{ background: var(--accent2); }}
  .stat.amber::after  {{ background: var(--amber); }}
  .stat.red::after    {{ background: var(--red); }}
  .stat-label {{ font-size: 14px; color: var(--muted); font-family: var(--mono);
                 letter-spacing: .5px; text-transform: uppercase; margin-bottom: 8px; }}
  .stat-value {{ font-size: 32px; font-family: var(--serif); color: var(--text);
                 line-height: 1; }}
  .stat-sub   {{ font-size: 13px; color: var(--muted); margin-top: 4px; }}

  /* ── SECTIONS ── */
  .section {{ padding: 40px 40px 0; }}
  .section:last-child {{ padding-bottom: 64px; }}
  .section-title {{
    font-family: var(--serif); font-size: 22px; color: var(--text);
    margin-bottom: 20px; padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 10px;
  }}
  .section-title::before {{ content: ''; display: inline-block;
    width: 3px; height: 20px; border-radius: 2px;
    background: linear-gradient(to bottom, var(--accent1), var(--accent2)); }}

  /* ── PHASE CARDS ── */
  .phase-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px,1fr));
    gap: 14px;
  }}
  .phase-card {{
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px 20px;
  }}
  .phase-card .phase-name {{ font-size: 14px; color: var(--muted);
    font-family: var(--mono); margin-bottom: 6px; }}
  .phase-card .phase-val {{ font-size: 30px; font-family: var(--serif);
    color: var(--text); }}
  .phase-card .phase-sub {{ font-size: 13px; color: var(--muted); font-family: var(--mono);
    margin-top: 2px; }}
  .phase-card .phase-bar {{ margin-top: 10px; height: 3px; border-radius: 2px;
    background: var(--border2); position: relative; }}
  .phase-card .phase-bar-fill {{ position: absolute; left: 0; top: 0; bottom: 0;
    border-radius: 2px; background: linear-gradient(90deg, var(--accent1), var(--accent2)); }}

  /* ── QUALITY ROW ── */
  .quality-row {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr));
    gap: 14px; margin-top: 0;
  }}
  .qual-card {{
    background: var(--bg3); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px 18px;
    display: flex; align-items: center; gap: 14px;
  }}
  .qual-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
  .qual-info .qual-label {{ font-size: 14px; color: var(--muted); }}
  .qual-info .qual-val   {{ font-size: 24px; font-family: var(--serif); }}

  /* ── CHARTS ── */
  .charts-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
    gap: 20px;
  }}
  .chart-card {{
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px;
  }}
  .chart-card img {{ width: 100%; height: auto; border-radius: 6px; }}
  .chart-label {{ font-size: 12px; color: var(--muted); font-family: var(--mono);
                  margin-bottom: 14px; letter-spacing: .3px; }}

  /* ── TABLES ── */
  .table-wrap {{ overflow-x: auto; border-radius: 10px;
                 border: 1px solid var(--border); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  thead th {{ background: var(--bg3); color: var(--muted); font-family: var(--mono);
              font-size: 11px; letter-spacing: .5px; text-transform: uppercase;
              padding: 10px 14px; text-align: left; white-space: nowrap; }}
  tbody tr {{ border-top: 1px solid var(--border); transition: background .15s; }}
  tbody tr:hover {{ background: rgba(148,163,184,.04); }}
  tbody td {{ padding: 9px 14px; color: var(--text); font-family: var(--mono);
              font-size: 13px; white-space: nowrap; }}

  /* ── FOOTER ── */
  footer {{
    margin-top: 64px; border-top: 1px solid var(--border);
    padding: 24px 40px; font-size: 12px; color: var(--muted);
    font-family: var(--mono); display: flex; justify-content: space-between;
    flex-wrap: wrap; gap: 8px;
  }}

  @media (max-width: 600px) {{
    nav, .hero, .stats-grid, .section {{ padding-left: 20px; padding-right: 20px; }}
  }}
</style>
</head>
<body>

<nav>
  <span class="brand">ONTbarcoder</span>
  <a href="#sec-summary">Summary</a>
  <a href="#sec-phases">Phases</a>
  {"<a href='#sec-charts'>Graphs</a>" if charts_section else ""}
  {"<a href='#sec-timeline'>Timeline</a>" if timeline_section else ""}
  {"<a href='#sec-params'>Parameters</a>" if params_section else ""}
  {"<a href='#sec-samples'>Samples</a>" if sample_rows else ""}
  <span class="spacer"></span>
  <span class="{mode_badge_cls}">{mode_badge_txt}</span>
</nav>

<header class="hero">
  <div class="hero-eyebrow">ONTbarcoder v3.1b · Analysis report</div>
  <h1>Run <span>{run_name}</span></h1>
  <div class="hero-meta">
    <span>📅 <strong>{ts_now}</strong></span>
    <span>⏱ Duration: <strong>{elapsed}</strong></span>
    <span>📂 <strong>{outpath}</strong></span>
    {input_files_html}
  </div>
</header>

<!-- STATS PRINCIPALES -->
<div class="stats-grid" id="sec-summary">
  <div class="stat green">
    <div class="stat-label">QC Compliant</div>
    <div class="stat-value">{n_qc}</div>
    <div class="stat-sub">{pct_qc} of total</div>
  </div>
  <div class="stat blue">
    <div class="stat-label">Total barcodes</div>
    <div class="stat-value">{n_total}</div>
    <div class="stat-sub">Including corrected</div>
  </div>
  <div class="stat indigo">
    <div class="stat-label">Filtered ≤1% N</div>
    <div class="stat-value">{n_filt}</div>
    <div class="stat-sub">{pct_filt} of total</div>
  </div>
  <div class="stat blue">
    <div class="stat-label">Total reads</div>
    <div class="stat-value" style="font-size:32px">{total_reads_fmt}</div>
    <div class="stat-sub">Total processed in the run</div>
  </div>
  <div class="stat indigo">
    <div class="stat-label">Assigned reads</div>
    <div class="stat-value" style="font-size:32px">{n_dem_fmt}</div>
    <div class="stat-sub">{pct_dem} of total</div>
  </div>
  <div class="stat indigo">
    <div class="stat-label">Samples</div>
    <div class="stat-value" style="font-size:32px">{n_samples}</div>
    <div class="stat-sub">Demultiplexed: {n_samples_dem} · ≥5 reads: {n_sam5}</div>
  </div>
  {rt_cycles_card}
</div>

<!-- PHASES -->
<section class="section" id="sec-phases">
  <h2 class="section-title">Phased pipeline</h2>
  <div class="phase-grid"{' style="max-width:400px"' if non_coi else ''}>
    <div class="phase-card">
      <div class="phase-name">PHASE 2A · Consensus by length</div>
      <div class="phase-val">{n_2a}</div>
      <div class="phase-sub">barcodes obtained</div>
      <div class="phase-bar"><div class="phase-bar-fill" style="width:{min(100, int(n_2a/max(n_total,1)*100))}%"></div></div>
    </div>
    {'<div class="phase-card"><div class="phase-name">PHASE 2B &middot; Consensus by similarity</div><div class="phase-val">' + str(n_2b) + '</div><div class="phase-sub">barcodes obtained</div><div class="phase-bar"><div class="phase-bar-fill" style="width:' + str(min(100, int(n_2b/max(n_total,1)*100))) + '%"></div></div></div>' if not non_coi else ''}
    {'<div class="phase-card"><div class="phase-name">PHASE 3 &middot; Correction by comparisons</div><div class="phase-val">' + str(n_3) + '</div><div class="phase-sub">barcodes corrected</div><div class="phase-bar"><div class="phase-bar-fill" style="width:' + str(min(100, int(n_3/max(n_total,1)*100))) + '%"></div></div></div>' if not non_coi else ''}
    {'<div class="phase-card"><div class="phase-name">UNRESOLVED</div><div class="phase-val">' + str(n_unres) + '</div><div class="phase-sub">uncorrectable</div><div class="phase-bar"><div class="phase-bar-fill" style="width:' + str(min(100, int(n_unres/max(n_total,1)*100))) + '%; background:var(--red);"></div></div></div>' if not non_coi else ''}
  </div>
  {phase2a_cov_html}

  {'' if non_coi else '''<div style="margin-top:20px;">
    <h3 style="font-family:var(--mono);font-size:12px;color:var(--muted);
               letter-spacing:.5px;text-transform:uppercase;margin-bottom:12px;">
      Quality distribution
    </h3>
    <div class="quality-row">
      <div class="qual-card">
        <div class="qual-dot" style="background:var(--green)"></div>
        <div class="qual-info">
          <div class="qual-label">No errors · no Ns</div>
          <div class="qual-val">''' + str(n_qc) + '''</div>
        </div>
      </div>
      <div class="qual-card">
        <div class="qual-dot" style="background:var(--accent1)"></div>
        <div class="qual-info">
          <div class="qual-label">1–5 indels</div>
          <div class="qual-val">''' + str(n_few) + '''</div>
        </div>
      </div>
      <div class="qual-card">
        <div class="qual-dot" style="background:var(--amber)"></div>
        <div class="qual-info">
          <div class="qual-label">6–10 indels</div>
          <div class="qual-val">''' + str(n_mid) + '''</div>
        </div>
      </div>
      <div class="qual-card">
        <div class="qual-dot" style="background:var(--red)"></div>
        <div class="qual-info">
          <div class="qual-label">&gt;10 indels</div>
          <div class="qual-val">''' + str(n_many) + '''</div>
        </div>
      </div>
    </div>
  </div>'''}
</section>

{params_section}
{charts_section}
{timeline_section}
        {samples_section}

<footer>
  <span>ONTbarcoder v3.1b — generated {ts_now}</span>
  <span>{outpath}</span>
</footer>

{charts_js}
</body>
</html>"""

        out_html = os.path.join(outpath, "report.html")
        with open(out_html, "w", encoding="utf-8") as f:
            f.write(html)

    def _organize_output_folder(self, outpath: str, is_live: bool = False):
        """
        Cleaning and final organization of the results folder:
        1. Remove the graphics PNGs (already included in report.html).
        2. If it is RT mode, remove live_accumulated.fastq.
        3. Compress each relevant subfolder into its own .zip (fast compression)
           and moves them to a 'analysis/' subfolder.
        """
        import zipfile

        # Delete PNGs from graphics
        for png in ("chart_reads.png", "chart_barcodes_ok.png"):
            p = os.path.join(outpath, png)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    self._panel_progress.append_log(f"  Deleted: {png}", "info")
                except Exception as e:
                    self._panel_progress.append_log(f"  Warning deleting {png}: {e}", "warn")

        # Remove live_accumulated.fastq in RT mode
        if is_live:
            acc = os.path.join(outpath, "live_accumulated.fastq")
            if os.path.isfile(acc):
                try:
                    os.remove(acc)
                    self._panel_progress.append_log("  Deleting: live_accumulated.fastq", "info")
                except Exception as e:
                    self._panel_progress.append_log(
                        f"  Warning deleting  live_accumulated.fastq: {e}", "warn")

        # Compress subfolders in 'analysis/'
        analisis_dir = os.path.join(outpath, "intermediate_files")
        os.makedirs(analisis_dir, exist_ok=True)

        # Subfolders to compress (those that exist)
        target_folders = [
            "1_demultiplexing",
            "demultiplexed",
            "demultiplexingfiles",
            "2a_ConsensusByLength",
            "2b_ConsensusBySimilarity",
            "3_ConsensusByBarcodeComparison",
            "3b_VariantRecovery",
            "barcodesets",
            "Main_barcode_results",
            "live_fastq_processed",
        ]

        for folder_name in target_folders:
            folder_path = os.path.join(outpath, folder_name)
            if not os.path.isdir(folder_path):
                continue
            zip_path = os.path.join(analisis_dir, f"{folder_name}.zip")
            try:
                with zipfile.ZipFile(
                    zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
                ) as zf:
                    for root, dirs, files in os.walk(folder_path):
                        for fname in files:
                            abs_path = os.path.join(root, fname)
                            arcname = os.path.relpath(abs_path, outpath)
                            zf.write(abs_path, arcname)
                # Delete the original folder after successfully compressing
                shutil.rmtree(folder_path, ignore_errors=True)
                self._panel_progress.append_log(
                    f"  Compressed: {folder_name}.zip → analysis/", "info")
            except Exception as e:
                self._panel_progress.append_log(
                    f"  Warning compressing {folder_name}: {e}", "warn")

    def _write_variants_sheet(self):
        """Write the 'Intra-sample variants' sheet to runsummary.xlsx: one row per
        (sample × detected haplotype cluster) with the read counts, fractions,
        translation status (Coding) and role. Only when intra-sample variant detection
        was enabled and at least one mixed sample was found. Marker-agnostic."""
        if not getattr(self, "wb", None):
            return
        stats = getattr(self, "_resolve_stats", {}) or {}
        if not stats.get("enabled"):
            return
        mixed = {k: v for k, v in getattr(self, "mixinfo_all", {}).items()
                 if k in getattr(self, "con200barcodes", {}) and v.get("secondary")}
        if not mixed:
            return
        try:
            ws = self.wb.add_worksheet("Intra-sample variants")
            hdrs = ["Sample", "#variants", "#noise reads", "Needs review",
                    "Variants beyond cap (not recovered)",
                    "Cluster rank", "Reads", "Fraction (%)", "Length (bp)",
                    "Ns", "Translates (Coding)", "Role"]
            for c, h in enumerate(hdrs):
                ws.write(0, c, h)
            r = 1
            for k in sorted(mixed.keys()):
                v = mixed[k]
                clusters = v.get("clusters") or []
                if not clusters:
                    continue
                n_var = v.get("n_clusters", len(clusters))
                n_noise = v.get("n_noise", 0)
                review_txt = "yes" if v.get("needs_review") else "no"
                n_extra = v.get("n_variants_extra", 0)
                for ci, c in enumerate(clusters):
                    _tr = c.get("translates")
                    tr_txt = ("yes" if _tr is True
                              else "no" if _tr is False else "NA")
                    # Dominant length = the final (corrected) barcode; secondaries
                    # report the raw phase-2a cluster length carried in 'len'.
                    if c.get("role") == "dominant":
                        seqlen = len(self.con200barcodes.get(k, "")) or c.get("len", "")
                    else:
                        seqlen = c.get("len", "")
                    ws.write(r, 0, k)
                    ws.write(r, 1, n_var)
                    ws.write(r, 2, n_noise)
                    ws.write(r, 3, review_txt)
                    ws.write(r, 4, n_extra)
                    ws.write(r, 5, c.get("rank", ci + 1))
                    ws.write(r, 6, c.get("size", ""))
                    ws.write(r, 7, round(float(c.get("frac", 0)) * 100, 1))
                    ws.write(r, 8, seqlen)
                    ws.write(r, 9, c.get("nN", ""))
                    ws.write(r, 10, tr_txt)
                    ws.write(r, 11, c.get("role", ""))
                    r += 1
        except Exception as e:
            self._panel_progress.append_log(
                f"Warning Excel Intra-sample variants: {e}", "warn")

    def _recover_secondary_variants(self):
        """Compact phase-3 recovery of secondary variants.

        Takes each eligible secondary consensus already computed (from
        mixinfo_all; <max_variant_Ns Ns, cap max_variants per sample) and corrects
        it against the good-barcode reference panel using the SAME edlib + MSA
        comparison as phase 3 (`runtoptwenty` + the `_fixbarcodes` reconstruction),
        writing the corrected variants to secondary_variants_recovered.fa.

        Isolated / best-effort: it runs only at finalization, reuses the existing
        good-barcode panel (no reads needed), and is fully wrapped in try/except so
        that ANY failure is logged and never affects the main barcode results.
        """
        try:
            outpath = self._outpath
            params = self._params
            _rm = params.get("resolve_mixed", {}) or {}
            if not _rm.get("enabled", False):
                return
            if not _rm.get("recover_secondaries", True):
                return
            gencode = params.get("gencode", 5)
            is_coi = (gencode != 0)   # no-Coding (gencode 0): aceptación por 0 Ns
            maxn = int(_rm.get("max_variant_Ns", 5))
            maxvar = int(_rm.get("max_variants", 3))
            # Honour the user's "Minimum read coverage": a secondary cluster
            # whose read count (size) is below mincov is dropped, exactly like a
            # main barcode below coverage is not produced.
            try:
                mincov = int(params.get("mincoverage", 1))
            except (TypeError, ValueError):
                mincov = 1

            # Eligible secondaries: < maxn Ns, ≥ mincov reads, the maxvar most
            # abundant per sample.
            candidates = []  # (varname, seq)
            cov_map = {}      # varname -> size (nº reads = cobertura del cluster)
            frac_map = {}     # varname -> frac (proporción del cluster, 0..1)
            for k, v in getattr(self, "mixinfo_all", {}).items():
                if k not in getattr(self, "con200barcodes", {}):
                    continue
                secs = v.get("secondaries") or []
                elig = [s for s in secs
                        if s.get("seq") and s["seq"].count("N") < maxn
                        and (s.get("size") is None
                             or int(s.get("size")) >= mincov)]
                for idx, s in enumerate(elig[:maxvar], start=1):
                    vname = f"{k}__var{idx}"
                    candidates.append((vname, s["seq"].replace("-", "").upper()))
                    cov_map[vname] = s.get("size")
                    frac_map[vname] = s.get("frac")
            candidates = [(n, s) for n, s in candidates if s]
            if not candidates:
                return

            # Reference panel = good barcodes from this run (no reads needed).
            ref_path = None
            for cand in (
                os.path.join(outpath, "barcodesets",
                             "Final_predgood_combined_barcodes.fa"),
                os.path.join(outpath, "Main_barcode_results",
                             "QC_Compliant_barcodes_noamb_noerr.fa"),
                os.path.join(outpath, "consensus_no_errors.fa"),
            ):
                if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                    ref_path = cand
                    break
            if not ref_path:
                self._panel_progress.append_log(
                    "  Variant recovery skipped: no good-barcode panel found.", "warn")
                return

            refseqdict = {}
            with open(ref_path) as fh:
                rl = fh.readlines()
            for i, j in enumerate(rl):
                if j.startswith(">") and i + 1 < len(rl):
                    seq = rl[i + 1].strip().replace("-", "").upper()
                    if seq:
                        refseqdict.setdefault(seq, []).append(j.strip()[1:])
            if not refseqdict:
                return

            # runtoptwenty input: seqdict {seq: ["tofix-"+name]} (the query marker
            # the reconstruction uses to tell query from references).
            seqdict, seqlist = {}, []
            for vname, seq in candidates:
                if seq not in seqdict:
                    seqdict[seq] = ["tofix-" + vname]
                    seqlist.append(seq)
            rec_dir = os.path.join(outpath, "3b_VariantRecovery")
            os.makedirs(rec_dir, exist_ok=True)

            self._panel_progress.append_log(
                f"  Recovering {len(seqlist)} secondary variant(s) "
                f"(compact phase 3 vs good panel)...", "info")
            # Synchronous: a handful of queries; runs MAFFT comparisons in-thread.
            runtoptwenty([seqlist, seqdict, refseqdict, rec_dir, 0]).run()

            # ── Reconstruction: same logic as _fixbarcodes (indels snapped to the
            #    reference-consensus frame; query substitutions preserved). ──────
            from collections import Counter as _Counter
            from Bio.Seq import Seq

            def _consensus(indict, perc):
                if not indict:
                    return ""
                vals = list(indict.values())
                out = []
                for n in range(len(vals[0])):
                    col = []
                    for e in indict:
                        try:
                            col.append(indict[e][n])
                        except IndexError:
                            break
                    cc = _Counter(col)
                    bs = {b: c for b, c in cc.items()
                          if float(c) / float(len(col)) > perc}
                    out.append(next(iter(bs)) if len(bs) == 1 else "N")
                return "".join(out)

            def _callcons(path, perc):
                with open(path) as fh:
                    l = fh.readlines()
                pos = [i for i, j in enumerate(l) if ">" in j]
                sd, q = {}, ""
                qname = ""
                for i, jp in enumerate(pos):
                    hdr = l[jp].strip().split(">")[1]
                    blk = (l[jp + 1:pos[i + 1]] if i != len(pos) - 1
                           else l[jp + 1:])
                    seq = "".join(blk).replace("\n", "")
                    if "tofix-" in hdr:
                        q = seq
                        qname = hdr.split(";")[0].replace("tofix-", "")
                    else:
                        sd[hdr] = seq
                return _consensus(sd, perc), q, qname

            def _cor_frame(seq):
                ss = [Seq(seq), Seq(seq[1:]), Seq(seq[2:]),
                      Seq(seq).reverse_complement(),
                      Seq(seq[:-1]).reverse_complement(),
                      Seq(seq[:-2]).reverse_complement()]
                ml, cf = 0, 0
                for i, s in enumerate(ss):
                    a = s.translate(table=gencode, to_stop=True).__str__()
                    if len(a) > ml:
                        ml, cf = len(a), i + 1
                return cf

            def _translates(seq):
                seq = seq.replace("-", "")
                if not seq:
                    return False
                cf = _cor_frame(seq)
                ss = [seq, seq[1:], seq[2:],
                      Seq(seq).reverse_complement().__str__(),
                      Seq(seq[:-1]).reverse_complement().__str__(),
                      Seq(seq[:-2]).reverse_complement().__str__()]
                s = ss[cf - 1]
                t = Seq(s).translate(table=gencode, to_stop=True).__str__()
                return len(t) == int(len(s) / 3)

            def _orf_trim(seq):
                """ORF en marco más largo sin paro interno; recorta el codón de paro
                del gen + cola 3' si los hay (CytB), intacto si ya traduce (COI).
                Devuelve (seq_recortada, n_aa)."""
                s = seq.replace("-", "")
                if not s:
                    return "", 0
                frames = [s, s[1:], s[2:],
                          Seq(s).reverse_complement().__str__(),
                          Seq(s[:-1]).reverse_complement().__str__(),
                          Seq(s[:-2]).reverse_complement().__str__()]
                best_fs, best_aa = s, 0
                for fs in frames:
                    aa = Seq(fs).translate(table=gencode, to_stop=True).__str__()
                    if len(aa) > best_aa:
                        best_aa, best_fs = len(aa), fs
                if best_aa * 3 >= len(s) - 2:
                    return s, best_aa
                return best_fs[:best_aa * 3], best_aa

            def _ext_gaps(sequence):
                bps = ("A", "T", "G", "C", "N")
                sp, ep = 0, 0
                for i, bp in enumerate(sequence):
                    if bp in bps:
                        sp = i - 1
                        break
                for i, bp in enumerate(sequence[::-1]):
                    if bp in bps:
                        ep = len(sequence) - i
                        break
                return ("?" * (sp + 1) + sequence[sp + 1:ep]
                        + "?" * (len(sequence) - ep))

            recovered = []  # (vname, newseq, translates, estgaps)
            for aln in sorted(fnmatch.filter(os.listdir(rec_dir), "*aln.fa")):
                try:
                    conseq, query, qname = _callcons(
                        os.path.join(rec_dir, aln), 0.5)
                    if not query or not conseq:
                        continue
                    query = _ext_gaps(query.upper())
                    conseq = conseq.upper()
                    newseq, errcount = "", 0
                    for i, j in enumerate(query):
                        if j == "-":
                            if i < len(conseq) and conseq[i] != "-":
                                errcount += 1
                                newseq += "N"
                        elif j != "?":
                            if i < len(conseq) and conseq[i] == "-":
                                errcount += 1
                            else:
                                newseq += j
                    newseq = newseq.upper()
                    if newseq:
                        # En marcador codificante, recortar al ORF limpio (quita el
                        # codón de paro del gen + cola 3'); si cubre ≥95% se usa el
                        # ORF recortado (sin paro → válido BOLD).
                        if is_coi:
                            _orf, _aalen = _orf_trim(newseq)
                            if _aalen * 3 >= len(newseq.replace("-", "")) * 0.95:
                                newseq = _orf
                        # En no-Coding no hay traducción (gencode 0): translates=None.
                        _tr = _translates(newseq) if is_coi else None
                        recovered.append((qname, newseq, _tr, errcount))
                except Exception:
                    # Best-effort per-alignment recovery; the outer try/except
                    # logs and guarantees main results are never affected.
                    continue

            if not recovered:
                self._panel_progress.append_log(
                    "  Variant recovery: no variants could be corrected.", "info")
                return

            out_fa = os.path.join(outpath, "barcodesets", "consensus_by_length",
                                  "secondary_variants_recovered.fa")
            n_ok = 0
            with open(out_fa, "w") as of:
                for vname, seq, tr, gaps in sorted(recovered):
                    # no-Coding: translates=NA; "válido" = 0 Ns (sin criterio de marco).
                    tr_tag = ("yes" if tr is True
                              else "no" if tr is False else "NA")
                    # Cobertura con la que se obtuvo el consenso = nº de reads del
                    # cluster secundario (análogo a con200cov en consensus_no_errors.fa).
                    _size = cov_map.get(vname)
                    cov_val = _size if _size is not None else "NA"
                    # Fracción que representa el cluster (igual que secondary_variants.fa).
                    _frac = frac_map.get(vname)
                    frac_tag = (f"{float(_frac) * 100:.0f}%"
                                if _frac is not None else "NA")
                    of.write(f">{vname};frac={frac_tag};len={len(seq)};"
                             f"coverage={cov_val};"
                             f"translates={tr_tag};"
                             f"fixed_indels={gaps};Ns={seq.count('N')}\n{seq}\n")
                    _valid = (seq.count("N") == 0) if not is_coi else (
                        tr and seq.count("N") == 0)
                    if _valid:
                        n_ok += 1
            # Surface in the output root next to the other consensus files.
            try:
                import shutil as _sh
                _sh.copyfile(out_fa, os.path.join(outpath,
                             "secondary_variants_recovered.fa"))
            except OSError:
                pass
            self._resolve_stats = getattr(self, "_resolve_stats", {}) or {}
            self._resolve_stats["recovered_variants"] = len(recovered)
            self._resolve_stats["recovered_valid"] = n_ok
            _ok_desc = ("translate cleanly with 0 Ns" if is_coi
                        else "with 0 Ns")
            self._panel_progress.append_log(
                f"  Variant recovery: {len(recovered)} corrected "
                f"({n_ok} now {_ok_desc}) → "
                f"secondary_variants_recovered.fa", "ok")
        except Exception as e:
            try:
                self._panel_progress.append_log(
                    f"  Variant recovery skipped (error, main results unaffected): "
                    f"{e}", "warn")
            except Exception:
                pass

    def _finish_analysis(self):
        # ── Provisional RT cycle: complete and listen again ──────────
        if self._is_live() and not getattr(self, "_live_finalizing", False):
            n_good = getattr(self, 'nperfectbarcodes', 0) or \
                     sum(1 for v in self.con200flags.values() if v)
            n = self._live_current_cycle
            prev_best = getattr(self, '_live_prev_best', 0)
            delta = n_good - prev_best
            delta_str = (f" (+{delta})" if delta > 0
                         else f" ({delta})" if delta < 0
                         else " (=)")
            if delta < 0:
                note = " — normal decline: subsampling with more reads can change consensus"
            else:
                note = ""
            self._panel_progress.append_log(
                f"  RT cycle #{n} completed — {n_good} QC Compliant barcodes"
                f"{delta_str} ({self.ndemultiplexed:,} demultiplexed reads){note}", "ok"
            )
            self._live_prev_best = n_good
            # Registrar valor real en gráfica (sin floor)
            self._panel_live_chart.record(
                self.ndemultiplexed,
                n_good,
                n_total=getattr(self, '_live_total_reads', self.totalseqs),
                cycle=self._live_current_cycle)
            self._live_consensus_running = False
            self._panel_progress._stop_cycle_timer()   # freezes completed cycle time
            # Record cycle duration in the log
            cycle_elapsed = int(time.time() - getattr(self._panel_progress, '_cycle_start_time', time.time()))
            ch = cycle_elapsed // 3600
            cm = (cycle_elapsed % 3600) // 60
            cs = cycle_elapsed % 60
            self._panel_progress.append_log(
                f"  Cycle duration #{n}: {ch:02d}:{cm:02d}:{cs:02d}", "info")
            self._panel_progress.set_phase("1", "Waiting new FASTQs…")
            return

        # ── Final analysis (conventional or completed RT) ────────────────────
        for _tmp in ("pre", "trace", "order"):
            _tmp_path = os.path.join(_ont_mp.SCRIPT_DIR, _tmp)
            try:
                if os.path.isfile(_tmp_path):
                    os.remove(_tmp_path)
            except OSError:
                pass

        # Borra los directorios temporales por-hilo de los workers MAFFT (uno por
        # hilo × fase). Todas las fases han terminado aquí, así que ningún worker
        # los está usando. Evita la acumulación en el temp del sistema entre runs
        # de la misma sesión (atexit es solo la red de seguridad al cerrar la app).
        _ont_mp.cleanup_worker_tmpdirs()

        elapsed = time.time() - self._run_start
        params = self._params
        outpath = self._outpath

        n_good_2a = sum(1 for v in self.con200flags.values() if v)
        n_qc = getattr(self, 'nperfectbarcodes', None)
        if not n_qc:
            n_qc = n_good_2a

        # Final authoritative count: force so a Phase-3 reduction is reflected.
        self._panel_progress.update_stat_ok(n_qc, force=True)

        if self._is_live():
            try:
                timeline = self._panel_live_chart.export_charts(outpath)
            except Exception as e:
                self._panel_progress.append_log(f"  Warning export graphs: {e}", "warn")
                timeline = getattr(self._panel_live_chart, "_timeline", [])
        else:
            timeline = []

        lf = getattr(self._panel_progress, "_logfile_handle", None)
        if lf:
            try:
                lf.write(f"\n{'=' * 60}\n")
                lf.write(f"  FINAL SUMMARY\n")
                lf.write(f"  Total time: {int(elapsed//60)} min {int(elapsed%60)} s\n")
                lf.write(f"  Total reads           : {getattr(self, 'totalseqs', 'N/A')}\n")
                lf.write(f"  Demultiplexed reads   : {getattr(self, 'ndemultiplexed', 'N/A')}\n")
                lf.write(f"  QC Compliant barcodes : {getattr(self, 'nperfectbarcodes', n_good_2a)}\n")
                lf.write(f"  Filtered barcodes     : {getattr(self, 'nfilteredbarcodes', 0)}\n")
                lf.write(f"  Unresolved            : {getattr(self, 'nerr', 0)}\n")
                lf.write("=" * 60 + "\n")
                lf.flush()
                lf.close()
                self._panel_progress._logfile_handle = None
            except Exception:
                pass

        # Compact phase-3 recovery of secondary variants (best-effort, isolated).
        # Runs only here at finalization (conventional + live finalization), never
        # in provisional RT cycles, and never affects the main barcode results.
        self._recover_secondary_variants()

        try:
            demsheet = self.wb.add_worksheet("Final results")
            demsheet.write(0, 0, "Metric")
            demsheet.write(0, 1, "Value")
            rows = [
                ("Total reads", getattr(self, 'totalseqs', 'N/A')),
                ("Demultiplexed reads", getattr(self, 'ndemultiplexed', 'N/A')),
                ("Total samples", len(self.sampleids)),
                ("Samples with ≥5 reads", getattr(self, 'nsampledemultiplexed5', 0)),
                ("Barcodes good 2a", n_good_2a),
                ("Barcodes good 2b", getattr(self, 'n90goodn', 0)),
                ("Barcodes corrected F3", getattr(self, 'nfixed', 0)),
                ("Final total barcodes", getattr(self, 'nfinal', n_good_2a)),
                ("QC Compliant", getattr(self, 'nperfectbarcodes', 0)),
                ("Filtered", getattr(self, 'nfilteredbarcodes', 0)),
                ("Unresolved", getattr(self, 'nerr', 0)),
                ("Total time", f"{int(elapsed//60)} min {int(elapsed%60)} s"),
            ]
            for i, (m, v) in enumerate(rows):
                demsheet.write(i+1, 0, m)
                demsheet.write(i+1, 1, v)
            self._write_variants_sheet()
            if timeline:
                tws = self.wb.add_worksheet("RT Timeline")
                hdrs = ["Cycle", "Date/Hour", "Minutes", "Total reads",
                        "Demultiplexed reads", "Barcodes OK"]
                for c, h in enumerate(hdrs):
                    tws.write(0, c, h)
                # One row per cycle: keep the final (last) record of each cycle,
                # since record() fires several times within a cycle.
                _last_by_cycle = {}
                for row in timeline:
                    _last_by_cycle[row.get("cycle", 0)] = row
                for r, row in enumerate(_last_by_cycle.values(), start=1):
                    tws.write(r, 0, row.get("cycle", 0))
                    tws.write(r, 1, row.get("ts", ""))
                    tws.write(r, 2, row.get("min", 0))
                    tws.write(r, 3, row.get("total", 0))
                    tws.write(r, 4, row.get("dem", 0))
                    tws.write(r, 5, row.get("ok", 0))
        except Exception as e:
            self._panel_progress.append_log(f"Warning Excel Final results: {e}", "warn")
        finally:
            # Always close the workbook so that the file is written to disk
            try:
                self.wb.close()
            except Exception as e_close:
                self._panel_progress.append_log(f"Warning al cerrar Excel: {e_close}", "warn")

        n_final_val = getattr(self, 'nfinal', n_good_2a)
        n_qc_val = getattr(self, 'nperfectbarcodes', 0) if getattr(self, 'nperfectbarcodes', None) is not None else n_good_2a
        n_filtered_val = getattr(self, 'nfilteredbarcodes', 0)
        n_unres_val = getattr(self, 'nerr', 0)

        summary = {
            "elapsed": f"{int(elapsed//60)} min {int(elapsed%60)} s",
            "mode": "Real-Time" if self._is_live() else "Conventional",
            "total": n_final_val,
            "qc_ok": n_qc_val,
            "filtered": n_filtered_val,
            "unresolved": n_unres_val,
            "phase2a_n": n_good_2a,
            "phase2b_n": getattr(self, 'n90goodn', 0),
            "phase3_n": getattr(self, 'nfixed', 0),
            "cov2a_counts": dict(getattr(self, 'cov2a_counts', {})),
            "perfect": n_qc_val,
            "by_phase": (
                [("Consensus by length", n_good_2a),
                 ("Unresolved", getattr(self, 'nerr', 0))]
                if params.get("non_coi") else
                [("Consensus by length", n_good_2a),
                 ("Consensus by similarity", getattr(self, 'n90goodn', 0)),
                 ("Correction by barcode comparisons", getattr(self, 'nfixed', 0)),
                 ("Unresolved", getattr(self, 'nerr', 0))]
            ),
            "few_indels": getattr(self, 'n1to5errbarcodes', 0),
            "mid_indels": getattr(self, 'n6to10errbarcodes', 0),
            "many_indels": getattr(self, 'n11to15errbarcodes', 0),
            "cycles": getattr(self, '_live_current_cycle', 0),
            "params": self._params,
        }

        self._panel_progress.append_log(
            f"✓ Analysis completed in {summary['elapsed']} — "
            f"{summary['qc_ok']} barcodes OK, {summary['unresolved']} unresolved", "ok")

        # Generate professional HTML report
        try:
            self._generate_html_report(outpath, summary, timeline)
            self._panel_progress.append_log("  Generated HTML report: report.html", "ok")
        except Exception as e:
            self._panel_progress.append_log(f"  Warning HTML report: {e}", "warn")

        # Organize results folder
        try:
            self._organize_output_folder(outpath, is_live=self._is_live())
        except Exception as e:
            self._panel_progress.append_log(f"  Warning folder organization: {e}", "warn")

        self._sidebar.mark_done("progress")
        self._panel_progress._stop_timer()   # freeze final screen time
        self._panel_progress._stop_btn.setEnabled(False)
        self._analysis_active = False
        self._panel_results.populate(self._outpath, summary)
        self._switch_panel("results")
        self._sidebar.mark_done("results")

    # ══════════════════════════════════════════════════════════════════════════
    # END REAL TIME MODE
    # ══════════════════════════════════════════════════════════════════════════

    def _finalize_live(self):
        # Pause the poll BEFORE showing the dialog. Thus, if MinKNOW generates
        # new files while the user decides, the poll will not mark them
        # as "known" without having concatenated them yet.
        self._live_finalizing = True

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Finalize RT")
        dlg.setMinimumWidth(420)
        dlg.setStyleSheet(f"""
            QDialog {{ background-color: {GRAY_CARD}; }}
            QLabel {{ color: {TEXT_PRI}; background-color: transparent; }}
            QPushButton {{ border-radius:8px; padding:8px 20px; font-size:15px; font-weight:500; }}
            #dlg_ok_btn {{ background-color:{BLUE}; color:white; border:none; }}
            #dlg_ok_btn:hover {{ background-color:#0C4A82; }}
            #dlg_cancel_btn {{ background-color:transparent; color:{TEXT_SEC}; border:1px solid {GRAY_LINE}; }}
            #dlg_cancel_btn:hover {{ background-color:{GRAY_BG}; }}
        """)
        vlay = QtWidgets.QVBoxLayout(dlg)
        vlay.setSpacing(16)
        vlay.setContentsMargins(24, 24, 24, 20)
        vlay.addWidget(make_label("Is sequencing finished?", bold=True))
        vlay.addWidget(make_label(
            "The remaining FASTQs will be concatenated and executed\n"
            + ("the final analysis (phases 1 → 2a).\n\n"
               if self._params.get("non_coi") else
               "the complete final analysis (phases 1 → 2a → 2b → 3).\n\n"),
            color=TEXT_SEC))
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QtWidgets.QPushButton("Cancel")
        btn_cancel.setObjectName("dlg_cancel_btn")
        btn_ok = QtWidgets.QPushButton("OK")
        btn_ok.setObjectName("dlg_ok_btn")
        btn_ok.setDefault(True)
        btn_cancel.clicked.connect(dlg.reject)
        btn_ok.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addSpacing(8)
        btn_row.addWidget(btn_ok)
        vlay.addLayout(btn_row)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            self._live_finalizing = False   # resume poll if user cancels
            return

        # Stop monitoring timers
        for attr in ("_live_dem_poll_timer", "_live_consensus_timer"):
            timer = getattr(self, attr, None)
            if timer:
                try:
                    timer.stop()
                except Exception:
                    pass

        proc = getattr(self, "_live_dorado_proc", None)
        if proc is not None:
            self._kill_dorado_proc(proc)
            self._panel_progress.append_log("  Dorado process tree terminated.", "warn")
            self._live_dorado_proc = None

        # Wait for the current RT cycle to finish (if any)
        if getattr(self, "_live_consensus_running", False):
            self._panel_progress.append_log(
                "  Waiting for the current RT cycle to finish…", "info")
            self._finalize_wait_timer = QtCore.QTimer()
            self._finalize_wait_timer.timeout.connect(self._finalize_wait_and_run)
            self._finalize_wait_timer.start(500)
        else:
            self._finalize_run_final()

    def _finalize_wait_and_run(self):
        """Wait for the provisional RT cycle to finish and then launch the final analysis."""
        if getattr(self, "_live_consensus_running", False):
            return  # still in progress, try again
        self._finalize_wait_timer.stop()
        self._finalize_run_final()

    def _finalize_run_final(self):
        """
        RT completion logic:
        1. Concatenate ALL the FASTQs in the folder that are not yet in the accumulation
        2. Copy live_accumulated.fastq and CSV to input_files/
        3. Ask the user where to save the final conventional analysis
        4. Launch complete conventional pipeline in the new folder,
           with the same parameters, as if the user had started it manually
        """
        outpath = self._outpath
        params = self._params

        self._panel_progress.append_log("━━ Finalizing RT — preparing final analysis…", "info")

        # Concatenate ALL the FASTQs in the folder to the accumulated one.
        # We do NOT use (current -_live_known_fastqs) because _live_known_fastqs can
        # have marked files that arrived during the confirmation dialog
        # sin que fueran concatenados (poll pausado por _live_finalizing=True).
        # Instead, we rebuild the entire rollup from scratch to
        # ensure that no files are lost.
        try:
            all_fastqs = sorted(
                f for f in os.listdir(self._live_fastq_dir)
                if f.endswith(".fastq") or f.endswith(".fastq.gz")
            )
            if all_fastqs:
                self._panel_progress.append_log(
                    f"  Rebuilding accumulated since {len(all_fastqs)} FASTQ file(s)…", "info")
                import gzip as _gzip
                n_total = 0
                with open(self._live_accumulated_fastq, "wb") as out_fh:
                    for fname in all_fastqs:
                        fpath = os.path.join(self._live_fastq_dir, fname)
                        if not os.path.isfile(fpath) or os.path.getsize(fpath) == 0:
                            continue
                        try:
                            if fname.endswith(".gz"):
                                with _gzip.open(fpath, "rb") as gz:
                                    data = gz.read()
                            else:
                                with open(fpath, "rb") as fh:
                                    data = fh.read()
                            # Remove all trailing \ns and add exactly one
                            # so that zip_longest(*[infile]*4) does not lose the offset.
                            data = data.rstrip(b"\n") + b"\n"
                            out_fh.write(data)
                            lines = data.split(b"\n")
                            if lines and lines[-1] == b"":
                                lines = lines[:-1]
                            n_file = sum(
                                1 for i, ln in enumerate(lines)
                                if i % 4 == 0 and ln.startswith(b"@")
                            )
                            n_total += n_file
                            self._panel_progress.append_log(
                                f"    {fname} ({n_file:,} reads)", "info")
                        except Exception as e:
                            self._panel_progress.append_log(
                                f"    Error reading {fname}: {e}", "warn")
                self._panel_progress.append_log(
                    f"  Reconstructed cumulative: {n_total:,} reads ✓", "ok")
        except Exception as e:
            self._panel_progress.append_log(f"  Warning cumulative reconstruction: {e}", "warn")

        if not os.path.isfile(self._live_accumulated_fastq) or \
                os.path.getsize(self._live_accumulated_fastq) == 0:
            self._panel_progress.append_log(
                "  No accumulated data — cannot run final analysis.", "error")
            return

        # Copy input files to input_files/inside the RT outpath
        input_dir = os.path.join(outpath, "input_files")
        os.makedirs(input_dir, exist_ok=True)
        _csv_stem = os.path.splitext(os.path.basename(self._demfile))[0]
        final_fastq = os.path.join(input_dir, f"{_csv_stem}.fastq")
        final_csv = os.path.join(input_dir, os.path.basename(self._demfile))
        try:
            shutil.copyfile(self._live_accumulated_fastq, final_fastq)
            shutil.copyfile(self._demfile, final_csv)
            self._panel_progress.append_log(
                f"  Input files saved in: input_files/", "info")
        except Exception as e:
            self._panel_progress.append_log(f"  Warning copying input_files: {e}", "warn")
            return

        # ── Ask the user where to save the final conventional analysis ──
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_folder_name = f"ont-barcoder_{ts}_rt-final"
        program_dir = _get_base_dir()
        default_outpath = os.path.join(program_dir, "output", default_folder_name)

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Folder for final conventional analysis")
        dlg.setMinimumWidth(500)
        dlg.setStyleSheet(f"""
            QDialog {{ background-color: {GRAY_CARD}; }}
            QLabel {{ color: {TEXT_PRI}; background-color: transparent; }}
            QRadioButton {{
                color: {TEXT_PRI}; background-color: transparent;
                font-size: 15px; padding: 6px 0;
            }}
            QRadioButton::indicator {{ width: 16px; height: 16px; }}
            QPushButton {{
                border-radius: 8px; padding: 8px 20px;
                font-size: 15px; font-weight: 500;
            }}
            #dlg_ok_btn {{ background-color: {BLUE}; color: white; border: none; }}
            #dlg_ok_btn:hover {{ background-color: #0C4A82; }}
            #dlg_cancel_btn {{
                background-color: transparent; color: {TEXT_SEC};
                border: 1px solid {GRAY_LINE};
            }}
            #dlg_cancel_btn:hover {{ background-color: {GRAY_BG}; }}
        """)
        vlay = QtWidgets.QVBoxLayout(dlg)
        vlay.setSpacing(16)
        vlay.setContentsMargins(24, 24, 24, 20)

        title_lbl = QtWidgets.QLabel("Where to save the final conventional analysis?")
        title_lbl.setStyleSheet(f"font-size:17px; font-weight:700; color:{TEXT_PRI};")
        vlay.addWidget(title_lbl)

        info_lbl = QtWidgets.QLabel(
            ("Analysis will be run (phases 1 → 2a)\n"
               if self._params.get("non_coi") else
               "Complete analysis will be run (phases 1 → 2a → 2b → 3)\n")
            + "using the files accumulated during the RT session,\n"
            + "with the same configured parameters."
        )
        info_lbl.setStyleSheet(f"font-size:14px; color:{TEXT_SEC};")
        vlay.addWidget(info_lbl)

        radio_default = QtWidgets.QRadioButton(
            f"Automatic folder (recommended)\n"
            f"  …/output/{default_folder_name}"
        )
        radio_default.setChecked(True)
        radio_custom = QtWidgets.QRadioButton("Select folder manually")
        vlay.addWidget(radio_default)
        vlay.addWidget(radio_custom)
        vlay.addSpacing(8)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QtWidgets.QPushButton("Cancel")
        btn_cancel.setObjectName("dlg_cancel_btn")
        btn_cancel.setFixedHeight(38)
        btn_ok = QtWidgets.QPushButton("Start final analysis")
        btn_ok.setObjectName("dlg_ok_btn")
        btn_ok.setFixedHeight(38)
        btn_ok.setDefault(True)
        btn_cancel.clicked.connect(dlg.reject)
        btn_ok.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addSpacing(8)
        btn_row.addWidget(btn_ok)
        vlay.addLayout(btn_row)

        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            self._panel_progress.append_log(
                "  Final conventional analysis canceled by user.", "warn")
            return

        if radio_default.isChecked():
            new_outpath = default_outpath
            os.makedirs(new_outpath, exist_ok=True)
        else:
            new_outpath = QtWidgets.QFileDialog.getExistingDirectory(
                self, _tr("MainWindow", "Select the output directory (must be empty)")
            )
            if not new_outpath:
                self._panel_progress.append_log(
                    "  Final conventional analysis canceled — no folder selected.", "warn")
                return
            if os.listdir(new_outpath):
                QtWidgets.QMessageBox.warning(
                    self, _tr("MainWindow", "Non-empty directory"),
                    _tr("MainWindow", "Please select an empty directory to avoid conflicts.")
                )
                return

        # ── Terminate all active RT workers and processes ───────────────

        # Residual timers (in case they didn't stop earlier)
        for attr in ('timer', '_live_dem_poll_timer', '_live_consensus_timer',
                     '_finalize_wait_timer'):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
                setattr(self, attr, None)

        # Workers Qt (QThread) from the RT pipeline
        for attr in ('worker_prep', 'mymergedatasets', 'myconsensus1',
                     'mycheckmsa', 'myconsensus2', 'mycheckmsa2', 'myfix'):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.quit()
                    w.wait(500)
                except Exception:
                    pass
                setattr(self, attr, None)

        # Demultiplex multiprocessing pool
        if self.pool is not None:
            try:
                self.pool.terminate()
                self.pool.join()
            except Exception:
                pass
            self.pool = None

        # Demultiplex queue
        if self.queue is not None:
            try:
                self.queue.close()
                self.queue.join_thread()
            except Exception:
                pass
            self.queue = None

        # ── Close RT session: HTML report, Excel, organize folder, log ──
        if outpath and os.path.isdir(outpath):
            elapsed = time.time() - self._run_start
            n_good_2a = sum(1 for v in getattr(self, 'con200flags', {}).values() if v)
            n_qc = getattr(self, 'nperfectbarcodes', 0) or n_good_2a
            timeline = getattr(self._panel_live_chart, "_timeline", [])
            rt_summary = {
                "elapsed": f"{int(elapsed // 60)} min {int(elapsed % 60)} s",
                "mode": "Real-Time (finalized)",
                "total": getattr(self, 'nfinal', n_good_2a),
                "qc_ok": n_qc,
                "filtered": getattr(self, 'nfilteredbarcodes', 0),
                "unresolved": getattr(self, 'nerr', 0),
                "phase2a_n": n_good_2a,
                "phase2b_n": getattr(self, 'n90goodn', 0),
                "phase3_n": getattr(self, 'nfixed', 0),
                "cov2a_counts": dict(getattr(self, 'cov2a_counts', {})),
                "perfect": n_qc,
                "by_phase": (
                    [("Consensus by length", n_good_2a),
                     ("Unresolved", getattr(self, 'nerr', 0))]
                    if params.get("non_coi") else
                    [("Consensus by length", n_good_2a),
                     ("Consensus by similarity", getattr(self, 'n90goodn', 0)),
                     ("Correction by barcode comparisons", getattr(self, 'nfixed', 0)),
                     ("Unresolved", getattr(self, 'nerr', 0))]
                ),
                "few_indels": getattr(self, 'n1to5errbarcodes', 0),
                "mid_indels": getattr(self, 'n6to10errbarcodes', 0),
                "many_indels": getattr(self, 'n11to15errbarcodes', 0),
                "cycles": getattr(self, '_live_current_cycle', 0),
                "params": params,
            }
            if timeline:
                try:
                    self._panel_live_chart.export_charts(outpath)
                except Exception:
                    pass
            try:
                self._generate_html_report(outpath, rt_summary, timeline)
                self._panel_progress.append_log("  HTML report generated: report.html", "ok")
            except Exception as e:
                self._panel_progress.append_log(f"  Warning HTML report: {e}", "warn")
            try:
                self._write_excel_on_stop()
            except Exception as e:
                self._panel_progress.append_log(f"  Warning Excel RT: {e}", "warn")
            try:
                if self.wb is not None:
                    self.wb.close()
                    self.wb = None
            except Exception:
                pass
            try:
                self._organize_output_folder(outpath, is_live=True)
                self._panel_progress.append_log("  RT output folder organized.", "ok")
            except Exception as e:
                self._panel_progress.append_log(
                    f"  Warning organizing RT folder: {e}", "warn")
            lf = getattr(self._panel_progress, "_logfile_handle", None)
            if lf:
                try:
                    lf.write("\n[FINALIZED — FINAL CONVENTIONAL ANALYSIS STARTED]\n")
                    lf.flush()
                    lf.close()
                    self._panel_progress._logfile_handle = None
                except Exception:
                    pass

        # ── Prepare state to run as pure conventional mode ──────────

        # Switch to conventional mode and point to the files in input_files/
        self._runmode = "1"
        self._fastq  = final_fastq
        self._demfile = final_csv
        self._outpath = new_outpath
        self._stopped = False
        self._run_start = time.time()

        # Create folder structure according to mode (non_coi skips 2b and 3)
        _non_coi_final = params.get("non_coi", False)
        _final_dirs = [
            "barcodesets", "barcodesets/consensus_by_length",
            "barcodesets/temps", "demultiplexingfiles", "demultiplexed",
            "2a_ConsensusByLength", "1_demultiplexing",
        ]
        if not _non_coi_final:
            _final_dirs += [
                "barcodesets/consensus_by_similarity", "barcodesets/fixing",
                "2b_ConsensusBySimilarity", "3_ConsensusByBarcodeComparison",
            ]
        for d in _final_dirs:
            os.makedirs(os.path.join(new_outpath, d), exist_ok=True)

        # Reset all state variables (same as _live_run_cycle)
        self.con200trans = {}
        self.con200length = {}
        self.con200barcodes = {}
        self.mixinfo_all = {}
        self.con200cov = {}
        self.con200flags = {}
        self.n90trans = {}
        self.n90length = {}
        self.n90barcodes = {}
        self.n90cov = {}
        self.n90flags = {}
        self.corlist = []
        self.ngoodbarcodescounter = 0
        self.con200goodn = 0
        self.con200errn = 0
        self.cov2a_counts = {}
        self.n90goodn = 0
        self.n90errn = 0
        self.nfixed = 0
        self.nfinal = 0
        self.nerr = 0
        self.nsinfinalbarcodes = 0
        self.nfilteredbarcodes = 0
        self.nperfectbarcodes = 0
        self.n1to5errbarcodes = 0
        self.n6to10errbarcodes = 0
        self.n11to15errbarcodes = 0
        self.nover16errbarcodes = 0
        self.errbarcodeset = {}
        self.hapiddict = {}
        self.seqdict = {}
        self._phase3_row = 0
        self.totalseqs = 0
        self.ndemultiplexed = 0
        self.nsampledemultiplexed5 = 0
        self.sampleids = {}
        self._consensus_first_call = True
        self.selectlens = []
        self.selectlenscounter = 0
        self.inlistforconsensus = []
        self._live_finalizing = False
        self._live_consensus_running = False
        self._live_ok_floor = 0   # Reset the "floor" of the RT display so that it does not
                                  # contaminate the actual count of the final analysis

        # Clean Excel Workbook in the new folder
        try:
            if self.wb is not None:
                try:
                    self.wb.close()
                except Exception:
                    pass
        except Exception:
            pass
        self.wb = xlsxwriter.Workbook(os.path.join(new_outpath, "runsummary.xlsx"))

        # Clear progress panel preserving current timer
        self._panel_progress.reset_soft()
        _non_coi_final = params.get("non_coi", False)
        self._panel_progress.configure_for_conventional(non_coi=_non_coi_final)
        if _non_coi_final:
            self._panel_progress.configure_phases(run_phase2b=False, run_phase3=False)
        self._switch_panel("progress")
        self._panel_progress.append_log(
            "━━ Final conventional analysis — starting pipeline…", "info")
        self._panel_progress.append_log(
            f"  FASTQ : {final_fastq}", "info")
        self._panel_progress.append_log(
            f"  CSV   : {final_csv}", "info")
        self._panel_progress.append_log(
            f"  Output: {new_outpath}", "info")

        # Open log in the new folder
        logfile_path = os.path.join(new_outpath, "log.txt")
        logfile = open(logfile_path, 'w', encoding='utf-8')
        self._panel_progress._logfile_handle = logfile
        logfile.write("ONTbarcoder3 — Final conventional analysis (post-RT)\n")
        logfile.write(f"Starting: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        logfile.write(f"Mode: Conventional (post-RT end)\n")
        logfile.write(f"FASTQ : {final_fastq}\n")
        logfile.write(f"CSV   : {final_csv}\n")
        logfile.write(f"Output: {new_outpath}\n")
        logfile.write("\n--- Analysis parameters ---\n")
        logfile.write(f"  Non-Coding marker: {'SI' if params.get('non_coi', False) else 'NO'}\n")
        _rm = params.get('resolve_mixed', {})
        logfile.write(f"  Intra-sample variant detection (dominant haplotype): "
                      f"{'ON' if _rm.get('enabled', False) else 'OFF'}\n")
        if _rm.get('enabled', False):
            logfile.write(f"    · min secondary variant fraction: {_rm.get('min_secondary_frac', '?')} "
                          f"(derived per-column polymorphism threshold: {_rm.get('minor_thresh', '?')})\n")
            logfile.write(f"    · variant tolerance: {_rm.get('tolerance', '?')}\n")
        if not params.get("non_coi", False):
            _gcs = self._scan_demfile_gencodes()
            if _gcs["has_any"]:
                _byc = _gcs["by_table"]
                _brk = ", ".join(f"table {t}: {n}" for t, n in sorted(_byc.items()))
                logfile.write(f"  Genetic code: per-sample from CSV ({_brk})\n")
            else:
                logfile.write(f"  Genetic code: {params.get('gencode', 5)} (global)\n")
        logfile.write(f"  Minimum length (bp): {params.get('minlen', '?')}\n")
        logfile.write(f"  Barcode length (bp): {params.get('explen', '?')}\n")
        logfile.write(f"  Window of barcode length ± (bp): {params.get('demlen', '?')}\n")
        logfile.write(f"  Maximum read length deviation from barcode length: {params.get('lendev', '?')}\n")
        logfile.write(f"  Read quality filter (min mean Q): "
                      f"{params.get('minq', 0) if params.get('minq', 0) else 'OFF'}\n")
        logfile.write(f"  Primer mismatches allowed: {params.get('primermismatch', '?')}\n")
        logfile.write(f"  Tag mismatches allowed: {params.get('tagmm', '?')}\n")
        logfile.write(f"  Coverages phase 2a: {params.get('coveragelist', '?')}\n")
        logfile.write(f"  Main consensus calling frequency: {params.get('consfreqfixed', '?')}\n")
        logfile.write(f"  Range of frequencies to assess (min, max): {params.get('consfreqmin', '?')} – {params.get('consfreqmax', '?')} (paso {params.get('consfreqstep', '?')})\n")
        logfile.write(f"  Threads: {params.get('n_threads', '?')}\n")
        _fases = [k.replace('run_','').upper() for k, v in params.items() if k.startswith('run_') and v]
        logfile.write(f"  Active phases: {', '.join(_fases)}\n")
        logfile.write("=" * 60 + "\n\n")
        logfile.flush()

        # Launch full conventional pipeline
        self._run_conventional_pipeline(params, new_outpath, logfile)

    def _live_clean_intermediate_final(self, outpath: str, params: dict):
        """Clear EVERYTHING except input_files/and logs for final analysis."""
        import glob
        
        # Delete entire intermediate folders
        for folder in ["1_demultiplexing", "demultiplexed", "demultiplexingfiles",
                       "2a_ConsensusByLength", "2b_ConsensusBySimilarity",
                       "3_ConsensusByBarcodeComparison", "barcodesets",
                       "Main_barcode_results", "live_fastq_processed"]:
            d = os.path.join(outpath, folder)
            if os.path.isdir(d):
                try:
                    shutil.rmtree(d)
                except Exception:
                    pass
        
        # Delete temporary .fa files from root (but preserve the ones that will be generated)
        for f in glob.glob(os.path.join(outpath, "consensus_*.fa")):
            try:
                os.remove(f)
            except Exception:
                pass
        
        # Recreate required directories
        for d in ["1_demultiplexing", "demultiplexed", "demultiplexingfiles",
                  "2a_ConsensusByLength", "2b_ConsensusBySimilarity",
                  "3_ConsensusByBarcodeComparison"]:
            os.makedirs(os.path.join(outpath, d), exist_ok=True)
        
        for sub in ["", "consensus_by_length", "consensus_by_similarity", "fixing", "temps"]:
            base = os.path.join(outpath, "barcodesets")
            if sub:
                os.makedirs(os.path.join(base, sub), exist_ok=True)
            else:
                os.makedirs(base, exist_ok=True)
        
        os.makedirs(os.path.join(outpath, "Main_barcode_results"), exist_ok=True)

    def _live_write_consensus_files_from_main(self, main_dir: str):
        """
        Copy consensus_no_errors.fa, consensus_filtered.fa and consensus_all.fa
        from Main_barcode_results to the root of outpath.
        Used at the end of each RT cycle (provisional or final) and in conventional.
        Completely replace existing files.
          consensus_no_errors.fa → QC_Compliant (0 Ns, 0 indels)
          consensus_filtered.fa → Filtered (≤1% N, ≤5 indels)
          consensus_all.fa → Allbarcodes (all)
        """
        outpath = self._outpath
        good_fa     = os.path.join(outpath, "consensus_no_errors.fa")
        filtered_fa = os.path.join(outpath, "consensus_filtered.fa")
        all_fa      = os.path.join(outpath, "consensus_all.fa")
        try:
            src_good     = os.path.join(main_dir, "QC_Compliant_barcodes_noamb_noerr.fa")
            src_filtered = os.path.join(main_dir, "Filtered_barcodes_1percamb_upto5err.fa")
            src_all      = os.path.join(main_dir, "Allbarcodes.fa")
            if os.path.isfile(src_good):
                shutil.copyfile(src_good, good_fa)
            if os.path.isfile(src_filtered):
                shutil.copyfile(src_filtered, filtered_fa)
            if os.path.isfile(src_all):
                shutil.copyfile(src_all, all_fa)
            # Surface the secondary-variants file (intra-sample secondary
            # haplotypes) in the output root, next to the consensus files, so it
            # ships with the other results.
            src_contam = os.path.join(outpath, "barcodesets",
                                      "consensus_by_length", "secondary_variants.fa")
            dst_contam = os.path.join(outpath, "secondary_variants.fa")
            try:
                if os.path.isfile(src_contam) and os.path.getsize(src_contam) > 0:
                    shutil.copyfile(src_contam, dst_contam)
            except OSError:
                pass
            n_good = sum(1 for line in open(good_fa) if line.startswith(">")) \
                if os.path.isfile(good_fa) else 0
            n_filt = sum(1 for line in open(filtered_fa) if line.startswith(">")) \
                if os.path.isfile(filtered_fa) else 0
            n_all = sum(1 for line in open(all_fa) if line.startswith(">")) \
                if os.path.isfile(all_fa) else 0
            self._panel_progress.append_log(
                f"  consensus_no_errors.fa: {n_good} | "
                f"consensus_filtered.fa: {n_filt} | "
                f"consensus_all.fa: {n_all} barcodes", "ok")
        except Exception as e:
            self._panel_progress.append_log(
                f"  Warning writing consensus_*.fa: {e}", "warn")

    def _stop_blast_worker(self):
        """Fully stop the BLAST QThread (if any) so it releases its NCBI
        connections. A lingering worker keeps its own rate limiter, so a new run
        started alongside it would double the request rate and trigger 429s."""
        w = getattr(self, "blast_worker", None)
        if w is None:
            return
        try:
            if w.isRunning():
                w.stop()                 # sets the cooperative stop flag
                if not w.wait(8000):     # let blocking urlopen calls unwind
                    w.terminate()        # last resort if still stuck
                    w.wait(2000)
        except Exception:
            pass
        self.blast_worker = None

    def _on_reset_analysis(self):
        self._runmode = "1"
        self._fastq = ""
        self._demfile = ""
        self._live_params = {}
        self._params = {}
        self._outpath = ""
        self._live_finalizing = False
        self._live_consensus_running = False

        for attr in (
            'con200trans', 'con200length', 'con200barcodes', 'con200cov', 'con200flags',
            'n90trans', 'n90length', 'n90barcodes', 'n90cov', 'n90flags',
            'corlist', 'errbarcodeset', 'hapiddict', 'seqdict', 'sampleids',
        ):
            setattr(self, attr, {} if attr != 'corlist' else [])

        # Tell any in-flight callbacks to ignore pending results.
        self._stopped = True
        self._analysis_active = False
        self._live_finalizing = True

        for attr in ('timer', '_consensus_timer', '_live_dem_poll_timer',
                     '_live_consensus_timer', '_finalize_wait_timer'):
            t = getattr(self, attr, None)
            if t:
                try:
                    t.stop()
                except Exception:
                    pass

        # Stop conventional/RT QThread workers in progress.
        for attr in ('worker_prep', 'mymergedatasets', 'myconsensus1',
                     'mycheckmsa', 'myconsensus2', 'mycheckmsa2', 'myfix'):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.quit()
                    w.wait(300)
                except Exception:
                    pass

        # Workers MAFFT detenidos: borra sus directorios temporales por-hilo
        # (best-effort; rmtree ignora los que un disttbfast aún en marcha pueda
        # retener en Windows — atexit los recogerá al cerrar la app).
        try:
            _ont_mp.cleanup_worker_tmpdirs()
        except Exception:
            pass

        # Stop the BLAST worker so it releases its NCBI connections.
        self._stop_blast_worker()

        # Fully tear down the multiprocessing pool/queue (terminate + join +
        # close), not just terminate(): otherwise child processes and the Queue
        # feeder thread linger in this process and degrade later work (incl. the
        # BLAST network polling).
        if getattr(self, 'pool', None):
            try:
                self.pool.terminate()
                self.pool.join()
            except Exception:
                pass
            self.pool = None
        q = getattr(self, 'queue', None)
        if q is not None:
            try:
                q.close()
            except Exception:
                pass
            self.queue = None

        # Terminate a live Dorado basecaller process tree if one is running.
        proc = getattr(self, "_live_dorado_proc", None)
        if proc is not None:
            try:
                self._kill_dorado_proc(proc)
            except Exception:
                pass
            self._live_dorado_proc = None

        self._panel_setup.full_reset()
        self._panel_progress.reset()
        self._panel_results._btn_reset.setEnabled(False)
        self._panel_results._sub_lbl.setText("—")
        for card in (self._panel_results.stat_total, self._panel_results.stat_qc,
                     self._panel_results.stat_filt, self._panel_results.stat_unresl):
            card.update_value("—")
        self._panel_results._phase_table.setRowCount(0)
        self._panel_results._qual_table.setRowCount(0)
        for btn in (self._panel_results._btn_qc, self._panel_results._btn_folder, self._panel_results._btn_all,
                    self._panel_results._btn_xls, self._panel_results._btn_html):
            btn.setEnabled(False)
            btn.setStyleSheet("QPushButton { background-color: transparent; }")
        self._panel_results._outpath = ""

        for key in ("setup", "params", "progress", "results", "live_chart", "compare"):
            self._sidebar._states[key] = "pending"
            if key in self._sidebar._buttons:
                self._sidebar._buttons[key].setProperty("state", "pending")
                refresh_style(self._sidebar._buttons[key])
        self._sidebar.hide_item("live_chart")

        # Re-lock panels until new file configuration
        for key in ("params", "progress", "results"):
            self._sidebar.lock_item(key)

        self._switch_panel("setup")

    @QtCore.pyqtSlot()
    def _stop_analysis(self):
        self._stopped = True   # tells all callbacks to ignore pending results
        self._analysis_active = False

        # In RT mode disable End RT and stop searching for FASTQs
        self._panel_progress.disable_rt_controls()
        self._panel_progress._stop_timer()
        self._panel_progress._stop_cycle_timer()

        # Stop RT poll timers
        for attr in ('timer', '_consensus_timer', '_live_dem_poll_timer',
                     '_live_consensus_timer', '_finalize_wait_timer'):
            t = getattr(self, attr, None)
            if t:
                try:
                    t.stop()
                except Exception:
                    pass

        # Mark as ending so that workers in progress finish cleanly
        self._live_finalizing = True

        # Stop QThread workers in progress
        for attr in ('worker_prep', 'mymergedatasets', 'myconsensus1',
                     'mycheckmsa', 'myconsensus2', 'mycheckmsa2', 'myfix'):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.quit()
                    w.wait(300)
                except Exception:
                    pass

        if self.pool:
            try:
                self.pool.terminate()
                self.pool.join()
            except Exception:
                pass
            self.pool = None

        # Workers MAFFT detenidos: borra sus directorios temporales por-hilo
        # (best-effort; atexit recoge lo que un disttbfast aún en marcha retenga).
        try:
            _ont_mp.cleanup_worker_tmpdirs()
        except Exception:
            pass

        proc = getattr(self, "_live_dorado_proc", None)
        if proc is not None:
            self._kill_dorado_proc(proc)
            self._panel_progress.append_log("  Dorado process tree terminated.", "warn")
            self._live_dorado_proc = None

        self._panel_progress.append_log("━━ Analysis stopped by user.", "warn")
        self._panel_progress.set_phase("1", "Stopped")

        # Generate HTML report with data from the last completed cycle
        elapsed = time.time() - self._run_start
        n_good_2a = sum(1 for v in getattr(self, 'con200flags', {}).values() if v)
        n_qc  = getattr(self, 'nperfectbarcodes', 0) or n_good_2a
        timeline = getattr(self._panel_live_chart, "_timeline", []) if self._is_live() else []

        summary = {
            "elapsed": f"{int(elapsed//60)} min {int(elapsed%60)} s",
            "mode": "Real-Time (stopped)" if self._is_live() else "Conventional (stopped)",
            "total": getattr(self, 'nfinal', n_good_2a),
            "qc_ok": n_qc,
            "filtered": getattr(self, 'nfilteredbarcodes', 0),
            "unresolved": getattr(self, 'nerr', 0),
            "phase2a_n": n_good_2a,
            "phase2b_n": getattr(self, 'n90goodn', 0),
            "phase3_n": getattr(self, 'nfixed', 0),
            "cov2a_counts": dict(getattr(self, 'cov2a_counts', {})),
            "perfect": n_qc,
            "by_phase": (
                [("Consensus by length", n_good_2a),
                 ("Unresolved", getattr(self, 'nerr', 0))]
                if self._params.get("non_coi") else
                [("Consensus by length", n_good_2a),
                 ("Consensus by similarity", getattr(self, 'n90goodn', 0)),
                 ("Correction by barcode comparisons", getattr(self, 'nfixed', 0)),
                 ("Unresolved", getattr(self, 'nerr', 0))]
            ),
            "few_indels": getattr(self, 'n1to5errbarcodes', 0),
            "mid_indels": getattr(self, 'n6to10errbarcodes', 0),
            "many_indels": getattr(self, 'n11to15errbarcodes', 0),
            "cycles": getattr(self, '_live_current_cycle', 0),
            "params": self._params,
        }

        outpath = getattr(self, '_outpath', "")
        if outpath and os.path.isdir(outpath):
            # Export RT graphs if any
            if self._is_live() and timeline:
                try:
                    self._panel_live_chart.export_charts(outpath)
                except Exception:
                    pass
            # Generate HTML
            try:
                self._generate_html_report(outpath, summary, timeline)
                self._panel_progress.append_log("  HTML report generated: report.html", "ok")
            except Exception as e:
                self._panel_progress.append_log(f"  Warning HTML report: {e}", "warn")
            # Write Excel sheets before organizing (barcodesets/ is still present)
            try:
                self._write_excel_on_stop()
            except Exception as e:
                self._panel_progress.append_log(f"  Warning Excel on stop: {e}", "warn")
            try:
                if self.wb is not None:
                    self.wb.close()
            except Exception:
                pass
            # Organize results folder (zips and deletes subfolders including barcodesets/)
            try:
                self._organize_output_folder(outpath, is_live=self._is_live())
            except Exception as e:
                self._panel_progress.append_log(f"  Warning folder organization: {e}", "warn")
            # Close log
            lf = getattr(self._panel_progress, "_logfile_handle", None)
            if lf:
                try:
                    lf.write("\n[STOPPED BY USER]\n")
                    lf.flush()
                    lf.close()
                    self._panel_progress._logfile_handle = None
                except Exception:
                    pass
            # Populate results panel and navigate
            self._panel_results.populate(outpath, summary)
            self._panel_progress._stop_timer()
            self._sidebar.mark_done("progress")
            self._sidebar.mark_done("results")
            self._switch_panel("results")
        else:
            # No output folder — analysis was stopped too early
            self._panel_progress.append_log(
                "  Analysis stopped before generating results.", "warn")
            self._panel_progress._stop_timer()

    def _write_excel_on_stop(self):
        """Write the available sheets with data from the interrupted RT cycle into the workbook."""
        if self.wb is None:
            return
        outpath  = getattr(self, '_outpath', '')
        params   = getattr(self, '_params', {})
        non_coi  = params.get('non_coi', False)
        sids     = getattr(self, 'sampleids', {})

        # Sheet 1: Demultiplexing
        if sids:
            try:
                sh1 = self.wb.add_worksheet("1. Demultiplexing")
                sh1.write(0, 0, "SpecimenID")
                sh1.write(0, 1, "Number of sequences demultiplexed")
                for i, (sample, count) in enumerate(sids.items()):
                    sh1.write(i + 1, 0, sample)
                    sh1.write(i + 1, 1, count)
            except Exception as e:
                self._panel_progress.append_log(
                    f"  Warning Excel stop (Hoja 1): {e}", "warn")

        # Sheet 2a: Consensus by length
        con200bc  = getattr(self, 'con200barcodes', {})
        con200len = getattr(self, 'con200length', {})
        con200tr  = getattr(self, 'con200trans', {})
        if sids and con200bc:
            try:
                sh2a = self.wb.add_worksheet("2a Consensus by length")
                trans_hdr = "Ambiguities" if non_coi else "Translation"
                headers2a = ["SpecimenID", "Demultiplexed seqs", "Stage",
                             "Length", "Barcode", trans_hdr]
                for c, h in enumerate(headers2a):
                    sh2a.write(0, c, h)
                etapa2a = "Consensus by length (non-Coding)" if non_coi else "Consensus by length"
                for i, j in enumerate(sorted(sids.keys())):
                    sh2a.write(i + 1, 0, j)
                    sh2a.write(i + 1, 1, sids[j])
                    sh2a.write(i + 1, 2, etapa2a)
                    try:
                        seq = con200bc[j]
                        sh2a.write(i + 1, 3, con200len.get(j, "NA"))
                        sh2a.write(i + 1, 4, seq)
                        sh2a.write(i + 1, 5, seq.count("N") if non_coi else con200tr.get(j, "NA"))
                    except KeyError:
                        for col in range(3, 6):
                            sh2a.write(i + 1, col, "NA")
            except Exception as e:
                self._panel_progress.append_log(
                    f"  Warning Excel stop (Sheet 2a): {e}", "warn")

        # Sheet 2b: Consensus by similarity (Coding only, if phase 2b ran at least once)
        n90bc  = getattr(self, 'n90barcodes', {})
        n90cov = getattr(self, 'n90cov', {})
        if sids and n90bc and not non_coi:
            try:
                sh2b = self.wb.add_worksheet("2b Consensus by similarity")
                headers2b = ["SpecimenID", "Coverage for closest sequences(90% cutoff)",
                             "stage", "length", "barcode", "translation check"]
                for c, h in enumerate(headers2b):
                    sh2b.write(0, c, h)
                for row2b, sample in enumerate(sorted(sids.keys())):
                    sh2b.write(row2b + 1, 0, sample)
                    sh2b.write(row2b + 1, 1, n90cov.get(sample, "NA"))
                    if n90bc.get(sample):
                        sh2b.write(row2b + 1, 2, "Consensus by similarity")
                        sh2b.write(row2b + 1, 3,
                                   getattr(self, 'n90length', {}).get(sample, "NA"))
                        sh2b.write(row2b + 1, 4, n90bc[sample])
                        sh2b.write(row2b + 1, 5,
                                   getattr(self, 'n90trans', {}).get(sample, "NA"))
                    else:
                        for col in range(2, 6):
                            sh2b.write(row2b + 1, col, "NA")
            except Exception as e:
                self._panel_progress.append_log(
                    f"  Warning Excel stop (Sheet 2b): {e}", "warn")

        # Sheet 3: Final barcodes (only if merged file exists)
        final_all = os.path.join(outpath, "barcodesets",
                                 "Final_all_combined_barcodes.fa") if outpath else ""
        con200cov = getattr(self, 'con200cov', {})
        if sids and final_all and os.path.isfile(final_all):
            try:
                sh3 = self.wb.add_worksheet("3.Final barcodes")
                trans_col3 = "length check" if non_coi else "translation check"
                headers3 = ["SpecimenID", "Number of sequences demultiplexed",
                            "Number used for generating barcodes", "stage", "type",
                            "length", "barcode", trans_col3, "#ambiguities"]
                for c, h in enumerate(headers3):
                    sh3.write(0, c, h)
                row3 = 1
                with open(final_all) as fa_in:
                    lines3 = fa_in.readlines()
                for i3, j3 in enumerate(lines3):
                    if ">" in j3:
                        seq3   = lines3[i3 + 1].strip() if i3 + 1 < len(lines3) else ""
                        parts3 = j3.strip().lstrip(">").split(";")
                        smp3   = parts3[0].replace("_all.fa", "")
                        slen3  = parts3[1] if len(parts3) > 1 else "NA"
                        ambs3  = int(parts3[3].split("=")[1]) if len(parts3) > 3 and "=" in parts3[3] else seq3.count("N")
                        gaps3  = int(parts3[4].split("=")[1]) if len(parts3) > 4 and "=" in parts3[4] else 0
                        if smp3 in n90bc:
                            stage3 = "Consensus by similarity" + (", fixed indel" if gaps3 > 0 else "")
                        else:
                            stage3 = "Consensus by length" + (", fixed indel" if gaps3 > 0 else "")
                        btype3   = f"removed {gaps3} indels" if gaps3 > 0 else "correct"
                        cov_num3 = n90cov.get(smp3, con200cov.get(smp3, "NA"))
                        trans3   = 1 if (ambs3 == 0 and gaps3 == 0) else 0
                        sh3.write(row3, 0, smp3)
                        sh3.write(row3, 1, sids.get(smp3, "NA"))
                        sh3.write(row3, 2, cov_num3)
                        sh3.write(row3, 3, stage3)
                        sh3.write(row3, 4, btype3)
                        sh3.write(row3, 5, int(slen3) if str(slen3).isdigit() else slen3)
                        sh3.write(row3, 6, seq3)
                        sh3.write(row3, 7, trans3)
                        sh3.write(row3, 8, ambs3)
                        row3 += 1
            except Exception as e:
                self._panel_progress.append_log(
                    f"  Warning Excel stop (Hoja 3): {e}", "warn")

        # Final results sheet: summary of metrics (always try to write)
        try:
            elapsed_s = time.time() - getattr(self, '_run_start', time.time())
            n_good_2a = sum(1 for v in getattr(self, 'con200flags', {}).values() if v)
            sh_fr = self.wb.add_worksheet("Final results")
            sh_fr.write(0, 0, "Metric")
            sh_fr.write(0, 1, "Value")
            rows_fr = [
                ("Total reads",             getattr(self, 'totalseqs', 'N/A')),
                ("Demultiplexed reads",     getattr(self, 'ndemultiplexed', 'N/A')),
                ("Total samples",           len(sids)),
                ("Samples with ≥5 reads",   getattr(self, 'nsampledemultiplexed5', 0)),
                ("Good barcodes 2a",        n_good_2a),
                ("Good barcodes 2b",        getattr(self, 'n90goodn', 0)),
                ("Corrected barcodes F3",   getattr(self, 'nfixed', 0)),
                ("Total final barcodes",    getattr(self, 'nfinal', n_good_2a)),
                ("QC Compliant",            getattr(self, 'nperfectbarcodes', 0)),
                ("Filtered",                getattr(self, 'nfilteredbarcodes', 0)),
                ("Unresolved",              getattr(self, 'nerr', 0)),
                ("Total time",              f"{int(elapsed_s//60)} min {int(elapsed_s%60)} s"),
                ("State",                   "Stopped by user"),
            ]
            for i, (m, v) in enumerate(rows_fr):
                sh_fr.write(i + 1, 0, m)
                sh_fr.write(i + 1, 1, v)
            self._write_variants_sheet()
            # RT Timeline if there is data
            timeline = (getattr(self._panel_live_chart, "_timeline", [])
                        if self._is_live() else [])
            if timeline:
                tws = self.wb.add_worksheet("RT Timeline")
                hdrs = ["Cycle", "Date/Time", "Minutes", "Total reads",
                        "Demultiplexed reads", "Barcodes OK"]
                for c, h in enumerate(hdrs):
                    tws.write(0, c, h)
                # One row per cycle: keep the final (last) record of each cycle,
                # since record() fires several times within a cycle.
                _last_by_cycle = {}
                for row in timeline:
                    _last_by_cycle[row.get("cycle", 0)] = row
                for r, row in enumerate(_last_by_cycle.values(), start=1):
                    tws.write(r, 0, row.get("cycle", 0))
                    tws.write(r, 1, row.get("ts", ""))
                    tws.write(r, 2, row.get("min", 0))
                    tws.write(r, 3, row.get("total", 0))
                    tws.write(r, 4, row.get("dem", 0))
                    tws.write(r, 5, row.get("ok", 0))
        except Exception as e:
            self._panel_progress.append_log(
                f"  Warning Excel stop (Final results): {e}", "warn")

    def _start_comparison(self, file_list: list,
                          mode: str, ref_path: str, custom_outdir: str,
                          extract_cfg: dict = None):
        if len(file_list) < 2:
            self._panel_compare._result_lbl.setText(
                "At least 2 files are needed to compare.")
            return

        if mode == "ref" and not ref_path:
            self._panel_compare._result_lbl.setText(
                "Select a reference file.")
            return

        # ── Output Folder Dialog ────────────────────────────────────
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        folder_name = f"ont-barcoder_{ts}_comp"
        program_dir = _get_base_dir()
        default_outpath = os.path.join(program_dir, "output", folder_name)

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(_tr("MainWindow", "Output folder"))
        dlg.setMinimumWidth(480)
        dlg.setStyleSheet(f"""
            QDialog {{ background-color: {GRAY_CARD}; }}
            QLabel {{ color: {TEXT_PRI}; background-color: transparent; }}
            QRadioButton {{
                color: {TEXT_PRI}; background-color: transparent;
                font-size: 15px; padding: 6px 0;
            }}
            QRadioButton::indicator {{ width: 16px; height: 16px; }}
            QPushButton {{
                border-radius: 8px; padding: 8px 20px;
                font-size: 15px; font-weight: 500;
            }}
            #dlg_ok_btn {{ background-color: {BLUE}; color: white; border: none; }}
            #dlg_ok_btn:hover {{ background-color: #0C4A82; }}
            #dlg_cancel_btn {{
                background-color: transparent; color: {BLUE};
                border: 1px solid {BLUE};
            }}
            #dlg_cancel_btn:hover {{ background-color: {BLUE_LIGHT}; }}
        """)

        vlay = QtWidgets.QVBoxLayout(dlg)
        vlay.setSpacing(16)
        vlay.setContentsMargins(24, 24, 24, 20)

        title_lbl = QtWidgets.QLabel("Where to save the results?")
        title_lbl.setStyleSheet(f"font-size:17px; font-weight:700; color:{TEXT_PRI};")
        vlay.addWidget(title_lbl)

        radio_default = QtWidgets.QRadioButton(
            f"Automatic folder (recommended)\n  …/output/{folder_name}"
        )
        radio_default.setChecked(True)
        radio_custom = QtWidgets.QRadioButton("Select folder manually")
        vlay.addWidget(radio_default)
        vlay.addWidget(radio_custom)
        vlay.addSpacing(8)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QtWidgets.QPushButton("Cancel")
        btn_cancel.setObjectName("dlg_cancel_btn")
        btn_cancel.setFixedHeight(38)
        btn_ok = QtWidgets.QPushButton("Continue")
        btn_ok.setObjectName("dlg_ok_btn")
        btn_ok.setFixedHeight(38)
        btn_ok.setDefault(True)
        btn_cancel.clicked.connect(dlg.reject)
        btn_ok.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addSpacing(8)
        btn_row.addWidget(btn_ok)
        vlay.addLayout(btn_row)

        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return

        if radio_default.isChecked():
            outdir = default_outpath
        else:
            parent_dir = QtWidgets.QFileDialog.getExistingDirectory(
                self, _tr("MainWindow", "Select the output folder")
            )
            if not parent_dir:
                return
            outdir = os.path.join(parent_dir, folder_name)

        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            self._panel_compare._result_lbl.setText(f"Error creating output folder: {e}")
            return

        # Show route in output field (internal reference)
        self._panel_compare._outdir_edit.setText(outdir)

        self._panel_compare._result_lbl.setText(_tr("ComparePanel", "Comparison in progress…"))
        self._panel_compare._comp_bar.setRange(0, 0)
        self._panel_compare._comp_bar.show()
        self._panel_compare._compare_btn_ref.setEnabled(False)

        if mode == "ref":
            self.comp_worker = _PairCompareWorker(file_list, ref_path, outdir, extract_cfg)
        else:
            self.comp_worker = _CompareWorker(file_list, outdir, extract_cfg)

        self.comp_worker.notifyProgress.connect(self._panel_compare.update_progress)
        self.comp_worker.taskFinished.connect(self._panel_compare.show_results)
        self.comp_worker.start()

    @QtCore.pyqtSlot(list, dict)
    def _start_blast(self, files: list, cfg: dict):
        # ── Output folder dialog (same pattern as _start_comparison) ──
        ts          = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        folder_name = f"ont-barcoder_{ts}_blast"
        program_dir = _get_base_dir()
        default_out = os.path.join(program_dir, "output", folder_name)

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Output folder")
        dlg.setMinimumWidth(480)
        dlg.setStyleSheet(f"""
            QDialog {{ background-color: {GRAY_CARD}; }}
            QLabel {{ color: {TEXT_PRI}; background-color: transparent; }}
            QRadioButton {{
                color: {TEXT_PRI}; background-color: transparent;
                font-size: 15px; padding: 6px 0;
            }}
            QRadioButton::indicator {{ width: 16px; height: 16px; }}
            QPushButton {{
                border-radius: 8px; padding: 8px 20px;
                font-size: 15px; font-weight: 500;
            }}
            #dlg_ok_btn {{ background-color: {BLUE}; color: white; border: none; }}
            #dlg_ok_btn:hover {{ background-color: #0C4A82; }}
            #dlg_cancel_btn {{
                background-color: transparent; color: {BLUE};
                border: 1px solid {BLUE};
            }}
            #dlg_cancel_btn:hover {{ background-color: {BLUE_LIGHT}; }}
        """)
        vlay = QtWidgets.QVBoxLayout(dlg)
        vlay.setSpacing(16)
        vlay.setContentsMargins(24, 24, 24, 20)
        title_lbl = QtWidgets.QLabel("Where to save the results?")
        title_lbl.setStyleSheet(
            f"font-size:17px; font-weight:700; color:{TEXT_PRI};"
        )
        vlay.addWidget(title_lbl)
        radio_default = QtWidgets.QRadioButton(
            f"Automatic folder (recommended)\n  …/output/{folder_name}"
        )
        radio_default.setChecked(True)
        radio_custom = QtWidgets.QRadioButton("Select folder manually")
        vlay.addWidget(radio_default)
        vlay.addWidget(radio_custom)
        vlay.addSpacing(8)
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QtWidgets.QPushButton("Cancel")
        btn_cancel.setObjectName("dlg_cancel_btn")
        btn_cancel.setFixedHeight(38)
        btn_ok = QtWidgets.QPushButton("Continue")
        btn_ok.setObjectName("dlg_ok_btn")
        btn_ok.setFixedHeight(38)
        btn_ok.setDefault(True)
        btn_cancel.clicked.connect(dlg.reject)
        btn_ok.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_cancel)
        btn_row.addSpacing(8)
        btn_row.addWidget(btn_ok)
        vlay.addLayout(btn_row)

        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return

        if radio_default.isChecked():
            outdir = default_out
        else:
            parent_dir = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Select the output folder"
            )
            if not parent_dir:
                return
            outdir = os.path.join(parent_dir, folder_name)

        try:
            os.makedirs(outdir, exist_ok=True)
        except Exception as e:
            self._panel_blast.on_error(f"Could not create output folder: {e}")
            return

        cfg["outdir"] = outdir

        self._panel_blast.set_running(True)
        self._panel_blast.update_status("result", f"Output    │ {outdir}")

        # Disconnect any leftover stop/signal connections from a previous run
        try:
            self._panel_blast.stopRequested.disconnect()
        except (RuntimeError, TypeError):
            pass
        if hasattr(self, "blast_worker") and self.blast_worker is not None:
            try:
                self.blast_worker.statusUpdated.disconnect()
                self.blast_worker.progressUpdated.disconnect()
                self.blast_worker.taskFinished.disconnect()
                self.blast_worker.taskError.disconnect()
            except RuntimeError:
                pass
            # Make sure a previous BLAST worker is fully stopped before starting a
            # new one: two live workers each have their own NCBI rate limiter, so
            # together they exceed NCBI's req/s limit → 429s and endless retries.
            self._stop_blast_worker()

        self.blast_worker = _BlastWorker(files, cfg)
        self._panel_blast.stopRequested.connect(self.blast_worker.stop)
        self.blast_worker.statusUpdated.connect(self._panel_blast.update_status)
        self.blast_worker.progressUpdated.connect(self._panel_blast.set_progress)
        self.blast_worker.taskFinished.connect(self._panel_blast.on_finished)
        self.blast_worker.taskError.connect(self._panel_blast.on_error)
        self.blast_worker.start()


# ═══════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════

def _write_crash_log(exc_type, exc_value, exc_tb):
    import traceback
    log_path = os.path.join(os.path.expanduser("~"), "ONTbarcoder_crash.log")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"{datetime.datetime.now()}\n")
        f.write("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))


def main():
    import traceback
    log_path = os.path.join(os.path.expanduser("~"), "ONTbarcoder_crash.log")

    # Catch unhandled exceptions in any Python thread
    sys.excepthook = _write_crash_log

    # Catch exceptions in Qt slots/signals
    def _qt_exception_hook(exc_type, exc_value, exc_tb):
        _write_crash_log(exc_type, exc_value, exc_tb)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    try:
        multiprocessing.freeze_support()

        # Register AppUserModelID before creating QApplication
        # This causes Windows to associate the correct icon on the taskbar
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Tovar.ONTbarcoder.3"
            )
        except Exception:
            pass

        # Apply the UI scale override before Qt reads the DPI from Windows.
        # QT_SCALE_FACTOR multiplies Qt's own DPI-derived scale factor.  With
        # UI_FIT_SCREEN on, the factor is chosen so the design canvas fits the
        # available screen area, so a single build adapts to any resolution
        # without manual editing (see _compute_ui_scale_factor()).
        _ui_factor = _compute_ui_scale_factor()
        if abs(_ui_factor - 1.0) > 1e-3:
            os.environ["QT_SCALE_FACTOR"] = f"{_ui_factor:.4f}"

        # High-DPI support: must be set before QApplication is created.
        # Qt will scale all logical pixel values (fonts, widgets) by the
        # device pixel ratio reported by Windows (e.g. 1.5× at 150% scaling).
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
        QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
        try:
            # Smooth fractional scaling (e.g. 1.5×) — available in Qt ≥ 5.14
            QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
                QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
        except AttributeError:
            pass

        app = QtWidgets.QApplication(sys.argv)
        app.setStyleSheet(STYLESHEET)
        app.setApplicationName("ONTbarcoder")
        app.setApplicationVersion("3.1b")

        icon = QtGui.QIcon()
        for icon_name in ("icon.ico",):
            icon_path = os.path.join(_get_base_dir(), icon_name)
            if os.path.isfile(icon_path):
                icon = QtGui.QIcon(icon_path)
                app.setWindowIcon(icon)
                break

        sys.excepthook = _qt_exception_hook

        window = MainWindow()
        window.setWindowIcon(icon)
        window.show()
        sys.exit(app.exec_())
    except Exception:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"{datetime.datetime.now()}\n")
            f.write(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
