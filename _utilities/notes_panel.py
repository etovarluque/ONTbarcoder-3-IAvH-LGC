#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Notes panel for ONTbarcoder3.

A simple, file-based notebook for the researcher. Each note is stored as a
plain Markdown file inside the project's ``_notes/`` folder, with a small
frontmatter block holding the title, timestamps and tags. Because notes are
ordinary text files:

  * a colleague can drop a ``.md`` / ``.txt`` file into ``_notes/`` and it is
    auto-detected the next time the panel is shown (no import step required);
  * the Import / Export buttons simply copy files in and out of ``_notes/`` for
    convenience when sharing between computers.
"""
from __future__ import annotations

import os
import re
import shutil
import datetime
import unicodedata

from PyQt5 import QtCore, QtGui, QtWidgets

from .shared import (
    _get_base_dir,
    make_label,
    make_section_label,
    hline,
    TEXT_PRI,
    TEXT_SEC,
    TEXT_HINT,
    GRAY_LINE,
    GRAY_CARD,
    WHITE,
    BLUE,
    AMBER,
    GREEN,
)

# Accepted extensions for notes detected in / imported into the folder.
_NOTE_EXTS = (".md", ".txt", ".markdown")

# Point-size added to the base body size for each Markdown heading level;
# shared by the source-editor highlighter and the rendered-preview styling
# so both views scale headings the same way.
_HEADING_SIZE_DELTA = {1: 8, 2: 5, 3: 3, 4: 1, 5: 1, 6: 1}
_MONO_FAMILIES = {"courier new", "consolas"}


def _notes_dir() -> str:
    """Return the ``_notes`` folder, creating it if needed."""
    path = os.path.join(_get_base_dir(), "_notes")
    os.makedirs(path, exist_ok=True)
    return path


def _slugify(text: str, max_len: int = 48) -> str:
    """Turn a title into a filesystem-safe slug for the note filename.

    Strips accents (é→e, ñ→n), drops anything that isn't a word character,
    turns runs of whitespace into single underscores and truncates to
    ``max_len`` characters. Returns ``"note"`` if nothing usable remains.
    """
    # Decompose accented characters and drop the combining marks.
    text = unicodedata.normalize("NFKD", (text or "").strip())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Keep only ASCII word characters and spaces; everything else is dropped.
    text = re.sub(r"[^\w\s]", "", text, flags=re.ASCII)
    # Collapse whitespace / underscore runs into a single underscore.
    text = re.sub(r"[\s_]+", "_", text).strip("_")
    return text[:max_len].strip("_") or "note"


def _now_iso() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _fmt_stamp(iso: str) -> str:
    """Human-friendly rendering of an ISO timestamp; tolerant of junk."""
    if not iso:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso


class _Note:
    """In-memory representation of a single note file."""

    def __init__(self, path: str, title: str, body: str,
                 tags: str, created: str, updated: str):
        self.path = path
        self.title = title
        self.body = body
        self.tags = tags
        self.created = created
        self.updated = updated

    # ── (de)serialization ────────────────────────────────────────────────
    @classmethod
    def load(cls, path: str) -> "_Note":
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()

        title = tags = created = updated = ""
        body = raw

        # Parse an optional leading frontmatter block delimited by '---' lines.
        if raw.startswith("---"):
            lines = raw.splitlines()
            end = None
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    end = i
                    break
            if end is not None:
                for line in lines[1:end]:
                    if ":" not in line:
                        continue
                    key, _, val = line.partition(":")
                    key, val = key.strip().lower(), val.strip()
                    if key == "title":
                        title = val
                    elif key == "tags":
                        tags = val
                    elif key == "created":
                        created = val
                    elif key == "updated":
                        updated = val
                body = "\n".join(lines[end + 1:]).lstrip("\n")

        if not title:
            title = os.path.splitext(os.path.basename(path))[0]
        if not created:
            try:
                ts = os.path.getctime(path)
                created = datetime.datetime.fromtimestamp(ts).replace(
                    microsecond=0).isoformat()
            except OSError:
                created = _now_iso()
        if not updated:
            try:
                ts = os.path.getmtime(path)
                updated = datetime.datetime.fromtimestamp(ts).replace(
                    microsecond=0).isoformat()
            except OSError:
                updated = created

        return cls(path, title, body, tags, created, updated)

    def save(self) -> None:
        self.updated = _now_iso()
        frontmatter = (
            "---\n"
            f"title: {self.title}\n"
            f"created: {self.created}\n"
            f"updated: {self.updated}\n"
            f"tags: {self.tags}\n"
            "---\n\n"
        )
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(frontmatter + self.body)


class _MarkdownHighlighter(QtGui.QSyntaxHighlighter):
    """Lightweight Markdown syntax colouring for the source editor."""

    def __init__(self, document, base_pt: int):
        super().__init__(document)
        self._base_pt = base_pt

        def make(color=None, bold=False, italic=False, strike=False,
                 underline=False, mono=False):
            f = QtGui.QTextCharFormat()
            if color:
                f.setForeground(QtGui.QColor(color))
            if bold:
                f.setFontWeight(QtGui.QFont.Bold)
            if italic:
                f.setFontItalic(True)
            if strike:
                f.setFontStrikeOut(True)
            if underline:
                f.setFontUnderline(True)
            if mono:
                f.setFontFamily("Consolas")
            return f

        # Inline spans: (regex, content format, delimiter length). The
        # delimiter on each side is dimmed grey so the content stands out.
        self._inline_rules = [
            (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), make(bold=True), 2),
            (re.compile(r"(?<!\*)\*(?!\*)(?=\S)([^*\n]+?)(?<=\S)\*(?!\*)"),
             make(italic=True), 1),
            (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"), make(color=TEXT_SEC, strike=True), 2),
            (re.compile(r"`([^`\n]+)`"), make(color=AMBER, mono=True), 1),
        ]
        self._link_re = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")
        self._link_fmt = make(color=BLUE, underline=True)
        # Underline is HTML (<u>…</u>) — asymmetric markers, handled separately.
        self._underline_re = re.compile(r"(<u>)(.+?)(</u>)", re.IGNORECASE)
        self._underline_fmt = make(underline=True)
        self._code_fmt = make(color=GREEN, mono=True)
        self._dim_fmt = make(color=TEXT_HINT)      # grey syntax markers
        self._quote_fmt = make(color=TEXT_SEC, italic=True)
        self._hr_fmt = make(color=TEXT_HINT, bold=True)

    def highlightBlock(self, text: str):
        prev_in_code = self.previousBlockState() == 1

        # Fenced code blocks (``` … ```): track state across lines.
        if text.lstrip().startswith("```"):
            self.setFormat(0, len(text), self._code_fmt)
            self.setCurrentBlockState(0 if prev_in_code else 1)
            return
        if prev_in_code:
            self.setFormat(0, len(text), self._code_fmt)
            self.setCurrentBlockState(1)
            return
        self.setCurrentBlockState(0)

        # Headings (#, ##, …) — colour the line, scale the size, dim the #'s.
        hm = re.match(r"^(#{1,6})\s", text)
        if hm:
            level = len(hm.group(1))
            size = self._base_pt + _HEADING_SIZE_DELTA[level]
            f = QtGui.QTextCharFormat()
            f.setForeground(QtGui.QColor(BLUE))
            f.setFontWeight(QtGui.QFont.Bold)
            f.setFontPointSize(size)
            self.setFormat(0, len(text), f)
            d = QtGui.QTextCharFormat()
            d.setForeground(QtGui.QColor(TEXT_HINT))
            d.setFontPointSize(size)   # keep size so the text stays aligned
            self.setFormat(0, hm.end(1), d)
            return

        # Horizontal rule (---, ***, ___).
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", text):
            self.setFormat(0, len(text), self._hr_fmt)
            return

        # Blockquote (whole line); inline spans still apply on top.
        qm = re.match(r"^(\s*>\s?)", text)
        if qm:
            self.setFormat(0, len(text), self._quote_fmt)
            self.setFormat(qm.start(1), qm.end(1) - qm.start(1), self._dim_fmt)

        # List / task markers ("- ", "1. ", "- [ ] ") dimmed grey.
        lm = re.match(r"^(\s*)([-*+]|\d+\.)\s(\[[ xX]\]\s)?", text)
        if lm:
            self.setFormat(lm.start(2), lm.end(2) - lm.start(2), self._dim_fmt)
            if lm.group(3):
                self.setFormat(lm.start(3), lm.end(3) - lm.start(3), self._dim_fmt)

        for pat, fmt, mlen in self._inline_rules:
            for m in pat.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)
                self.setFormat(m.start(), mlen, self._dim_fmt)
                self.setFormat(m.end() - mlen, mlen, self._dim_fmt)

        # Underline (<u>…</u>): underline the content, dim the HTML tags.
        for m in self._underline_re.finditer(text):
            self.setFormat(m.start(2), m.end(2) - m.start(2), self._underline_fmt)
            self.setFormat(m.start(1), len(m.group(1)), self._dim_fmt)
            self.setFormat(m.start(3), len(m.group(3)), self._dim_fmt)

        # Links: dim the brackets/URL, colour only the visible text.
        for m in self._link_re.finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self._dim_fmt)
            self.setFormat(m.start(1), m.end(1) - m.start(1), self._link_fmt)


class _MarkdownEditor(QtWidgets.QPlainTextEdit):
    """Source editor that auto-continues lists, tasks and blockquotes on Enter."""

    _LIST_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s(\[[ xX]\]\s)?(.*)$")
    _QUOTE_RE = re.compile(r"^(\s*>\s?)(.*)$")

    def keyPressEvent(self, event):
        is_enter = event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter)
        plain = not (event.modifiers() & (QtCore.Qt.ShiftModifier
                                          | QtCore.Qt.ControlModifier))
        cursor = self.textCursor()
        if is_enter and plain and not cursor.hasSelection():
            line = cursor.block().text()
            lm = self._LIST_RE.match(line)
            if lm:
                indent, marker, task, content = lm.groups()
                if content.strip() == "":
                    self._clear_current_block()   # empty item → end the list
                    return
                if marker[:-1].isdigit():
                    marker = f"{int(marker[:-1]) + 1}."
                super().keyPressEvent(event)
                self.textCursor().insertText(
                    f"{indent}{marker} {'[ ] ' if task else ''}")
                return
            qm = self._QUOTE_RE.match(line)
            if qm:
                prefix, content = qm.groups()
                if content.strip() == "":
                    self._clear_current_block()
                    return
                super().keyPressEvent(event)
                self.textCursor().insertText(prefix)
                return
        super().keyPressEvent(event)

    def _clear_current_block(self):
        cur = self.textCursor()
        cur.movePosition(QtGui.QTextCursor.StartOfBlock)
        cur.movePosition(QtGui.QTextCursor.EndOfBlock, QtGui.QTextCursor.KeepAnchor)
        cur.removeSelectedText()


class NotesPanel(QtWidgets.QWidget):
    """Side-panel utility to write, store and share researcher notes."""

    _BASE_PT = 12  # base body font size; headings scale up from here

    def __init__(self, parent=None):
        super().__init__(parent)
        self._notes: list[_Note] = []
        self._current: _Note | None = None
        self._dirty = False
        self._loading = False  # guards editor signals during programmatic load

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(12)

        outer.addWidget(make_label("Notes", size=19, bold=True))
        desc = make_label(
            "Write and store research notes. Each note is saved as a Markdown "
            "file in the _notes folder, so you can sync or share them simply by "
            "copying the file. Notes copied into _notes folder are detected automatically.",
            color=TEXT_SEC,
        )
        desc.setWordWrap(True)
        outer.addWidget(desc)

        # ── Body: list (left) + editor (right) ───────────────────────────
        split = QtWidgets.QHBoxLayout()
        split.setSpacing(14)
        outer.addLayout(split, 1)

        split.addLayout(self._build_left(), 0)
        split.addLayout(self._build_right(), 1)

        self.refresh()

    # ── UI construction ──────────────────────────────────────────────────
    def _build_left(self) -> QtWidgets.QVBoxLayout:
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(8)

        col.addWidget(make_section_label("My notes"))

        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("Search notes…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        col.addWidget(self._search)

        self._list = QtWidgets.QListWidget()
        self._list.setFixedWidth(260)
        self._list.setStyleSheet(
            f"QListWidget {{ background:{WHITE}; border:1px solid {GRAY_LINE};"
            f" border-radius:8px; padding:4px; font-size:16px; }}"
            f"QListWidget::item {{ padding:7px 6px; border-radius:6px; }}"
            f"QListWidget::item:selected {{ background:{BLUE}; color:{WHITE}; }}"
        )
        self._list.currentItemChanged.connect(self._on_selection_changed)
        col.addWidget(self._list, 1)

        # New / Delete
        row1 = QtWidgets.QHBoxLayout()
        row1.setSpacing(8)
        self._new_btn = QtWidgets.QPushButton("+ New")
        self._new_btn.setObjectName("primary_btn")
        self._new_btn.setFixedHeight(40)
        self._new_btn.clicked.connect(self._on_new)
        row1.addWidget(self._new_btn)

        self._del_btn = QtWidgets.QPushButton("Delete")
        self._del_btn.setObjectName("secondary_btn")
        self._del_btn.setFixedHeight(40)
        self._del_btn.clicked.connect(self._on_delete)
        row1.addWidget(self._del_btn)
        col.addLayout(row1)

        # Import / Export / Folder
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(8)
        self._import_btn = QtWidgets.QPushButton("Import…")
        self._import_btn.setObjectName("secondary_btn")
        self._import_btn.setFixedHeight(36)
        self._import_btn.setToolTip("Copy one or more note files into the _notes folder")
        self._import_btn.clicked.connect(self._on_import)
        row2.addWidget(self._import_btn)

        self._export_btn = QtWidgets.QPushButton("Export…")
        self._export_btn.setObjectName("secondary_btn")
        self._export_btn.setFixedHeight(36)
        self._export_btn.setToolTip("Save a copy of the selected note elsewhere")
        self._export_btn.clicked.connect(self._on_export)
        row2.addWidget(self._export_btn)
        col.addLayout(row2)

        self._folder_btn = QtWidgets.QPushButton("Open _notes folder  📂")
        self._folder_btn.setObjectName("secondary_btn")
        self._folder_btn.setFixedHeight(36)
        self._folder_btn.clicked.connect(self._open_folder)
        col.addWidget(self._folder_btn)

        return col

    def _build_right(self) -> QtWidgets.QVBoxLayout:
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(8)

        self._title_edit = QtWidgets.QLineEdit()
        self._title_edit.setPlaceholderText("Title")
        self._title_edit.setStyleSheet(
            "QLineEdit { font-size:21px; font-weight:600; padding:8px 10px; }"
        )
        self._title_edit.textEdited.connect(self._on_edited)
        col.addWidget(self._title_edit)

        self._tags_edit = QtWidgets.QLineEdit()
        self._tags_edit.setPlaceholderText("Tags (comma-separated)")
        self._tags_edit.textEdited.connect(self._on_edited)
        col.addWidget(self._tags_edit)

        self._format_bar = self._build_toolbar()
        col.addWidget(self._format_bar)

        self._find_bar = self._build_find_bar()
        col.addWidget(self._find_bar)
        self._find_bar.hide()

        # Source editor: the note is plain Markdown. The toolbar inserts
        # Markdown syntax around the selection so the file stays faithful.
        self._editor = _MarkdownEditor()
        self._editor.setPlaceholderText(
            "Write in Markdown — use the toolbar, or type **bold**, *italic*, # Heading"
        )
        mono = QtGui.QFont("Consolas")
        mono.setStyleHint(QtGui.QFont.Monospace)
        mono.setPointSize(self._BASE_PT)
        self._editor.setFont(mono)
        self._editor.setStyleSheet(
            f"QPlainTextEdit {{ background:{WHITE}; border:1px solid {GRAY_LINE};"
            f" border-radius:8px; padding:10px; color:{TEXT_PRI}; }}"
        )
        self._editor.textChanged.connect(self._on_edited)
        self._highlighter = _MarkdownHighlighter(self._editor.document(),
                                                 self._BASE_PT)
        col.addWidget(self._editor, 1)

        # Read-only rendered preview, toggled by the Preview button. Qt renders
        # Markdown correctly via setMarkdown (only its *writer* drops emphasis,
        # which is why we store the source text rather than round-tripping it).
        self._preview = QtWidgets.QTextEdit()
        self._preview.setReadOnly(True)
        # Resolve relative image paths (e.g. _assets/img.png) against _notes/.
        self._preview.document().setBaseUrl(
            QtCore.QUrl.fromLocalFile(os.path.join(_notes_dir(), "")))
        self._preview.setStyleSheet(
            f"QTextEdit {{ background:{WHITE}; border:1px solid {GRAY_LINE};"
            f" border-radius:8px; padding:10px; color:{TEXT_PRI}; font-size:17px; }}"
        )
        self._preview.hide()
        col.addWidget(self._preview, 1)

        # Keyboard shortcuts to wrap the selection in bold / italic.
        QtWidgets.QShortcut(QtGui.QKeySequence.Bold, self._editor,
                            activated=self._wrap_bold)
        QtWidgets.QShortcut(QtGui.QKeySequence.Italic, self._editor,
                            activated=self._wrap_italic)
        QtWidgets.QShortcut(QtGui.QKeySequence.Underline, self._editor,
                            activated=self._wrap_underline)
        QtWidgets.QShortcut(QtGui.QKeySequence.Find, self._editor,
                            activated=self._show_find)
        esc = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape),
                                  self._find_bar)
        esc.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        esc.activated.connect(self._hide_find)

        # Footer: status + save
        foot = QtWidgets.QHBoxLayout()
        self._status_lbl = make_label("", size=15, color=TEXT_HINT)
        foot.addWidget(self._status_lbl)
        foot.addStretch()

        self._save_btn = QtWidgets.QPushButton("Save")
        self._save_btn.setObjectName("primary_btn")
        self._save_btn.setFixedHeight(40)
        self._save_btn.setFixedWidth(160)
        self._save_btn.clicked.connect(self._save_current)
        foot.addWidget(self._save_btn)
        col.addLayout(foot)

        self._set_editor_enabled(False)
        return col

    # ── Formatting toolbar ───────────────────────────────────────────────
    def _build_toolbar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        def make_btn(text, tip, slot, *, bold=False, italic=False,
                     strike=False, underline=False, checkable=False):
            b = QtWidgets.QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setFixedSize(34, 32)
            f = b.font()
            f.setPointSize(12)
            f.setBold(bold)
            f.setItalic(italic)
            f.setStrikeOut(strike)
            f.setUnderline(underline)
            b.setFont(f)
            b.setStyleSheet(
                f"QToolButton {{ border:1px solid {GRAY_LINE}; border-radius:6px;"
                f" background:{WHITE}; color:{TEXT_PRI}; }}"
                f"QToolButton:hover {{ background:{GRAY_CARD}; }}"
                f"QToolButton:checked {{ background:{BLUE}; color:{WHITE};"
                f" border-color:{BLUE}; }}"
                f"QToolButton:disabled {{ background:{GRAY_CARD}; color:{TEXT_HINT};"
                f" border-color:{GRAY_LINE}; }}"
            )
            b.clicked.connect(slot)
            return b

        bold_btn = make_btn("B", "Bold (Ctrl+B)", self._wrap_bold, bold=True)
        italic_btn = make_btn("I", "Italic (Ctrl+I)", self._wrap_italic, italic=True)
        underline_btn = make_btn("U", "Underline (Ctrl+U)", self._wrap_underline,
                                 underline=True)
        strike_btn = make_btn("S", "Strikethrough", self._wrap_strike, strike=True)
        lay.addWidget(bold_btn)
        lay.addWidget(italic_btn)
        lay.addWidget(underline_btn)
        lay.addWidget(strike_btn)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.VLine)
        sep.setStyleSheet(f"color:{GRAY_LINE};")
        lay.addWidget(sep)

        # Text size = Markdown heading level (Normal / H1 / H2 / H3).
        self._heading_combo = QtWidgets.QComboBox()
        self._heading_combo.addItems(["Normal text", "Heading 1 (large)",
                                      "Heading 2 (medium)", "Heading 3 (small)"])
        self._heading_combo.setFixedHeight(32)
        self._heading_combo.setToolTip("Text size — applied as a Markdown heading on the current line")
        self._heading_combo.activated.connect(self._on_heading_changed)
        lay.addWidget(self._heading_combo)

        bullet_btn = make_btn("•", "Bulleted list", self._insert_bullet_list)
        number_btn = make_btn("1.", "Numbered list", self._insert_numbered_list)
        lay.addWidget(bullet_btn)
        lay.addWidget(number_btn)

        sep2 = QtWidgets.QFrame()
        sep2.setFrameShape(QtWidgets.QFrame.VLine)
        sep2.setStyleSheet(f"color:{GRAY_LINE};")
        lay.addWidget(sep2)

        find_btn = make_btn("🔍", "Find & replace (Ctrl+F)", self._toggle_find)
        find_btn.setFixedWidth(40)
        lay.addWidget(find_btn)

        # "Insert" menu keeps the bar tidy for the less-frequent constructs.
        insert_btn = QtWidgets.QToolButton()
        insert_btn.setText("＋ Insert")
        insert_btn.setToolTip("Insert link, code, quote, rule, table…")
        insert_btn.setCursor(QtCore.Qt.PointingHandCursor)
        insert_btn.setFixedHeight(32)
        insert_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        insert_btn.setStyleSheet(
            f"QToolButton {{ border:1px solid {GRAY_LINE}; border-radius:6px;"
            f" background:{WHITE}; color:{TEXT_PRI}; padding:0 8px; }}"
            f"QToolButton:hover {{ background:{GRAY_CARD}; }}"
            f"QToolButton:disabled {{ background:{GRAY_CARD}; color:{TEXT_HINT};"
            f" border-color:{GRAY_LINE}; }}"
            f"QToolButton::menu-indicator {{ width:0px; }}"
        )
        menu = QtWidgets.QMenu(insert_btn)
        menu.addAction("Link…", self._insert_link)
        menu.addAction("Image…", self._insert_image)
        menu.addAction("Inline code", self._wrap_code)
        menu.addAction("Code block", self._insert_code_block)
        menu.addSeparator()
        menu.addAction("Blockquote", self._insert_quote)
        menu.addAction("Checklist item", self._insert_task)
        menu.addAction("Horizontal rule", self._insert_hr)
        menu.addAction("Table", self._insert_table)
        insert_btn.setMenu(menu)
        lay.addWidget(insert_btn)

        lay.addStretch()

        # Editing controls are disabled while the rendered preview is shown.
        self._edit_only = [bold_btn, italic_btn, underline_btn, strike_btn,
                           self._heading_combo, bullet_btn, number_btn,
                           insert_btn, find_btn]

        self._preview_btn = make_btn("👁", "Toggle rendered preview",
                                     self._toggle_preview, checkable=True)
        self._preview_btn.setFixedWidth(40)
        lay.addWidget(self._preview_btn)

        return bar

    # ── inline / block Markdown helpers ──────────────────────────────────
    def _wrap_selection(self, marker: str, close: str | None = None) -> None:
        # ``close`` lets asymmetric wrappers (e.g. <u>…</u>) reuse this logic;
        # it defaults to ``marker`` for the symmetric Markdown spans.
        close = marker if close is None else close
        cur = self._editor.textCursor()
        if cur.hasSelection():
            start, end = cur.selectionStart(), cur.selectionEnd()
            text = cur.selectedText()
            cur.insertText(f"{marker}{text}{close}")
            cur.setPosition(start + len(marker))
            cur.setPosition(end + len(marker), QtGui.QTextCursor.KeepAnchor)
        else:
            pos = cur.position()
            cur.insertText(f"{marker}{close}")
            cur.setPosition(pos + len(marker))
        self._editor.setTextCursor(cur)
        self._editor.setFocus()

    def _wrap_bold(self):
        self._wrap_selection("**")

    def _wrap_italic(self):
        self._wrap_selection("*")

    def _wrap_underline(self):
        # Markdown has no underline; <u> is the portable, widely-rendered form.
        self._wrap_selection("<u>", "</u>")

    def _wrap_strike(self):
        self._wrap_selection("~~")

    def _on_heading_changed(self, level: int):
        cur = self._editor.textCursor()
        cur.beginEditBlock()
        cur.movePosition(QtGui.QTextCursor.StartOfBlock)
        cur.movePosition(QtGui.QTextCursor.EndOfBlock,
                         QtGui.QTextCursor.KeepAnchor)
        line = re.sub(r"^#{1,6}\s*", "", cur.selectedText())
        if level:
            line = "#" * level + " " + line
        cur.insertText(line)
        cur.endEditBlock()
        self._editor.setFocus()

    def _prefix_lines(self, numbered: bool) -> None:
        cur = self._editor.textCursor()
        start, end = cur.selectionStart(), cur.selectionEnd()
        cur.beginEditBlock()
        cur.setPosition(start)
        cur.movePosition(QtGui.QTextCursor.StartOfBlock)
        cur.setPosition(end, QtGui.QTextCursor.KeepAnchor)
        cur.movePosition(QtGui.QTextCursor.EndOfBlock, QtGui.QTextCursor.KeepAnchor)
        lines = cur.selectedText().split(" ")
        out = []
        for i, ln in enumerate(lines, 1):
            stripped = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", ln)
            out.append((f"{i}. " if numbered else "- ") + stripped)
        cur.insertText("\n".join(out))
        cur.endEditBlock()
        self._editor.setFocus()

    def _insert_bullet_list(self):
        self._prefix_lines(numbered=False)

    def _insert_numbered_list(self):
        self._prefix_lines(numbered=True)

    def _transform_lines(self, fn) -> None:
        """Apply ``fn(index, line)`` to each selected (or current) line."""
        cur = self._editor.textCursor()
        start, end = cur.selectionStart(), cur.selectionEnd()
        cur.beginEditBlock()
        cur.setPosition(start)
        cur.movePosition(QtGui.QTextCursor.StartOfBlock)
        cur.setPosition(end, QtGui.QTextCursor.KeepAnchor)
        cur.movePosition(QtGui.QTextCursor.EndOfBlock, QtGui.QTextCursor.KeepAnchor)
        # selectedText() joins blocks with U+2029, not "\n".
        lines = cur.selectedText().split(" ")
        cur.insertText("\n".join(fn(i, ln) for i, ln in enumerate(lines)))
        cur.endEditBlock()
        self._editor.setFocus()

    def _insert_quote(self):
        self._transform_lines(
            lambda i, ln: ln if ln.startswith("> ") else "> " + ln)

    def _insert_task(self):
        strip = lambda ln: re.sub(
            r"^\s*([-*+]\s+(\[[ xX]\]\s+)?|\d+\.\s+)", "", ln)
        self._transform_lines(lambda i, ln: "- [ ] " + strip(ln))

    def _wrap_code(self):
        self._wrap_selection("`")

    def _insert_code_block(self):
        cur = self._editor.textCursor()
        sel = cur.selectedText().replace(" ", "\n") if cur.hasSelection() else ""
        cur.insertText("```\n" + sel + "\n```\n")
        self._editor.setFocus()

    def _insert_link(self):
        cur = self._editor.textCursor()
        text = cur.selectedText() or "link text"
        url, ok = QtWidgets.QInputDialog.getText(
            self, "Insert link", "URL:", text="https://")
        if not ok:
            return
        cur.insertText(f"[{text}]({url.strip() or 'https://'})")
        self._editor.setFocus()

    def _insert_hr(self):
        cur = self._editor.textCursor()
        cur.movePosition(QtGui.QTextCursor.EndOfBlock)
        cur.insertText("\n\n---\n")
        self._editor.setTextCursor(cur)
        self._editor.setFocus()

    def _insert_table(self):
        cur = self._editor.textCursor()
        cur.movePosition(QtGui.QTextCursor.EndOfBlock)
        cur.insertText("\n\n| Column 1 | Column 2 |\n| --- | --- |\n|  |  |\n")
        self._editor.setFocus()

    def _insert_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Insert image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp *.svg)",
        )
        if not path:
            return
        # Copy the image into _notes/_assets so it travels with the note.
        assets = os.path.join(_notes_dir(), "_assets")
        os.makedirs(assets, exist_ok=True)
        dst = os.path.join(assets, os.path.basename(path))
        base, ext = os.path.splitext(dst)
        n = 1
        while os.path.exists(dst) and not os.path.samefile(path, dst):
            dst = f"{base}-{n}{ext}"
            n += 1
        try:
            if not os.path.exists(dst):
                shutil.copy2(path, dst)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Insert image failed", str(exc))
            return
        rel = "_assets/" + os.path.basename(dst)
        alt = os.path.splitext(os.path.basename(dst))[0]
        self._editor.textCursor().insertText(f"![{alt}]({rel})")
        self._editor.setFocus()

    # ── Find & replace ───────────────────────────────────────────────────
    def _build_find_bar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._find_edit = QtWidgets.QLineEdit()
        self._find_edit.setPlaceholderText("Find")
        self._find_edit.returnPressed.connect(self._find_next)
        self._replace_edit = QtWidgets.QLineEdit()
        self._replace_edit.setPlaceholderText("Replace with")
        self._replace_edit.returnPressed.connect(self._replace_one)
        lay.addWidget(self._find_edit, 1)
        lay.addWidget(self._replace_edit, 1)

        def small(text, slot, tip=""):
            b = QtWidgets.QPushButton(text)
            b.setObjectName("secondary_btn")
            b.setFixedHeight(32)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        lay.addWidget(small("Find", self._find_next, "Find next (Enter)"))
        lay.addWidget(small("Replace", self._replace_one))
        lay.addWidget(small("All", self._replace_all, "Replace all"))
        lay.addWidget(small("✕", self._hide_find, "Close (Esc)"))
        return bar

    def _toggle_find(self):
        if self._find_bar.isVisible():
            self._hide_find()
        else:
            self._show_find()

    def _show_find(self):
        self._find_bar.show()
        cur = self._editor.textCursor()
        if cur.hasSelection():
            self._find_edit.setText(cur.selectedText())
        self._find_edit.setFocus()
        self._find_edit.selectAll()

    def _hide_find(self):
        self._find_bar.hide()
        self._editor.setFocus()

    def _find_next(self):
        text = self._find_edit.text()
        if not text:
            return
        if not self._editor.find(text):
            # Wrap around to the top and try once more.
            cur = self._editor.textCursor()
            cur.movePosition(QtGui.QTextCursor.Start)
            self._editor.setTextCursor(cur)
            self._editor.find(text)

    def _replace_one(self):
        text = self._find_edit.text()
        if not text:
            return
        cur = self._editor.textCursor()
        # find() is case-insensitive, so the current match may differ in case
        # from the query; compare case-insensitively so it still gets replaced.
        if cur.hasSelection() and cur.selectedText().lower() == text.lower():
            cur.insertText(self._replace_edit.text())
        self._find_next()

    def _replace_all(self):
        text = self._find_edit.text()
        if not text:
            return
        repl = self._replace_edit.text()
        doc = self._editor.toPlainText()
        # Case-insensitive to match find()/Replace; the lambda keeps the
        # replacement literal (no backreference expansion of \1, \g<…>, …).
        new = re.sub(re.escape(text), lambda _m: repl, doc, flags=re.IGNORECASE)
        if new != doc:
            cur = self._editor.textCursor()
            cur.beginEditBlock()
            cur.select(QtGui.QTextCursor.Document)
            cur.insertText(new)
            cur.endEditBlock()

    def _toggle_preview(self):
        on = self._preview_btn.isChecked()
        if on:
            self._find_bar.hide()
            source = self._preview_markdown(self._editor.toPlainText())
            self._preview.setMarkdown(source)
            self._style_preview()
        self._editor.setVisible(not on)
        self._preview.setVisible(on)
        for w in self._edit_only:
            w.setEnabled(not on)

    @staticmethod
    def _preview_markdown(text: str) -> str:
        """Rewrite the note so the preview honours line breaks the way a
        plain-text editor would, rather than by strict Markdown rules:

          * a single newline puts the next line directly below (Markdown would
            instead merge the two into one wrapped line);
          * every blank line becomes a blank line of vertical space, and N
            blank lines give N blanks (Markdown collapses them all into one).

        Fenced code blocks and table rows are passed through untouched so the
        rest of the Markdown (headings, lists, tables, code, …) still parses.
        """
        lines = text.split("\n")
        out: list[str] = []
        in_fence = False
        n = len(lines)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                out.append(line)
                continue
            if in_fence:
                out.append(line)
                continue
            if stripped == "":
                # An explicit non-breaking-space paragraph survives Markdown's
                # blank-line collapsing (a plain space gets trimmed away), so
                # each blank line keeps its own gap.
                out.extend(("", " ", ""))
                continue
            out.append(line)
            nxt = lines[i + 1].strip() if i + 1 < n else ""
            # Append a hard break so the next line stacks directly below,
            # unless this is a table row (whose rows must stay contiguous).
            if (nxt and not nxt.startswith("```") and "|" not in line
                    and not line.endswith("  ")):
                out[-1] = line + "  "
        return "\n".join(out)

    def _style_preview(self) -> None:
        """Dress up the plain result of QTextEdit.setMarkdown(): it parses
        Markdown correctly but renders it with a tiny generic font, unstyled
        code fences and bare HTML tables. Walk the resulting QTextDocument
        and apply the app's look on top."""
        doc = self._preview.document()

        body_font = QtGui.QFont("Segoe UI")
        body_font.setPointSize(self._BASE_PT + 2)
        doc.setDefaultFont(body_font)

        cursor = QtGui.QTextCursor(doc)
        block = doc.begin()
        while block.isValid():
            bf = block.blockFormat()
            frag = block.begin()
            family = (frag.fragment().charFormat().fontFamily().lower()
                      if not frag.atEnd() else "")
            is_code = family in _MONO_FAMILIES
            is_quote = (not is_code and bf.leftMargin() > 0
                       and bf.rightMargin() > 0)
            level = bf.headingLevel()

            new_bf = QtGui.QTextBlockFormat(bf)
            # Vertical spacing is driven entirely by the explicit blank
            # paragraphs that _preview_markdown() inserts, so ordinary lines
            # get zero margin and stack directly on top of one another. Only
            # headings keep a little breathing room of their own.
            if level:
                new_bf.setTopMargin(max(bf.topMargin(), 12))
                new_bf.setBottomMargin(max(bf.bottomMargin(), 4))
            elif is_code:
                new_bf.setBackground(QtGui.QColor(GRAY_CARD))
                new_bf.setLeftMargin(14)
                new_bf.setRightMargin(14)
                new_bf.setTopMargin(0)
                new_bf.setBottomMargin(0)
            elif is_quote:
                new_bf.setBackground(QtGui.QColor(GRAY_CARD))
                new_bf.setLeftMargin(18)
                new_bf.setTopMargin(0)
                new_bf.setBottomMargin(0)
            else:
                new_bf.setTopMargin(0)
                new_bf.setBottomMargin(0)
            cursor.setPosition(block.position())
            cursor.setBlockFormat(new_bf)

            if level:
                size = self._BASE_PT + 2 + _HEADING_SIZE_DELTA.get(level, 1)
                hcur = QtGui.QTextCursor(doc)
                hcur.setPosition(block.position())
                hcur.setPosition(block.position() + max(block.length() - 1, 0),
                                 QtGui.QTextCursor.KeepAnchor)
                hfmt = QtGui.QTextCharFormat()
                hfmt.setFontPointSize(size)
                hfmt.setForeground(QtGui.QColor(BLUE))
                hcur.mergeCharFormat(hfmt)

            block = block.next()

        self._style_preview_tables(doc.rootFrame())

    def _style_preview_tables(self, frame: QtGui.QTextFrame) -> None:
        for obj in frame.childFrames():
            if isinstance(obj, QtGui.QTextTable):
                fmt = obj.format()
                fmt.setBorder(1)
                fmt.setBorderStyle(QtGui.QTextFrameFormat.BorderStyle_Solid)
                fmt.setBorderBrush(QtGui.QColor(GRAY_LINE))
                fmt.setCellPadding(6)
                fmt.setCellSpacing(0)
                obj.setFormat(fmt)
                header_fmt = QtGui.QTextCharFormat()
                header_fmt.setFontWeight(QtGui.QFont.Bold)
                header_fmt.setBackground(QtGui.QColor(GRAY_CARD))
                for col in range(obj.columns()):
                    cell = obj.cellAt(0, col)
                    cc = cell.firstCursorPosition()
                    cc.setPosition(cell.lastCursorPosition().position(),
                                   QtGui.QTextCursor.KeepAnchor)
                    cc.mergeCharFormat(header_fmt)
            else:
                self._style_preview_tables(obj)

    # ── Detection / listing ──────────────────────────────────────────────
    def showEvent(self, event):
        """Re-scan the folder each time the panel becomes visible so notes
        copied in by a colleague (while the app was running) show up."""
        super().showEvent(event)
        self.refresh(keep_current=True)

    def refresh(self, keep_current: bool = False) -> None:
        current_path = self._current.path if (keep_current and self._current) else None

        notes = []
        for name in os.listdir(_notes_dir()):
            path = os.path.join(_notes_dir(), name)
            if not os.path.isfile(path):
                continue
            if os.path.splitext(name)[1].lower() not in _NOTE_EXTS:
                continue
            try:
                notes.append(_Note.load(path))
            except OSError:
                continue

        notes.sort(key=lambda n: n.updated, reverse=True)
        self._notes = notes
        self._rebuild_list(select_path=current_path)

    def _rebuild_list(self, select_path: str | None = None) -> None:
        self._loading = True
        self._list.clear()
        target_row = -1
        for i, note in enumerate(self._notes):
            item = QtWidgets.QListWidgetItem(note.title or "(untitled)")
            sub = _fmt_stamp(note.updated)
            if note.tags:
                sub += f"  ·  {note.tags}"
            item.setToolTip(sub)
            item.setData(QtCore.Qt.UserRole, note.path)
            self._list.addItem(item)
            if note.path == select_path:
                target_row = i
        self._loading = False

        self._apply_filter()
        if target_row >= 0:
            # Restoring the row for the note already shown in the editor
            # (a keep_current refresh, e.g. on showEvent or after Save). Guard
            # the selection so it doesn't re-enter _on_selection_changed, which
            # would reload — and, if the editor is dirty, silently auto-save —
            # the note already on screen.
            keep = (self._current is not None
                    and select_path == self._current.path)
            self._loading = keep
            self._list.setCurrentRow(target_row)
            self._loading = False
        elif self._list.count() and not self._current:
            self._list.setCurrentRow(0)
        else:
            # No note to show (e.g. the last note was just deleted): reset the
            # editor so it doesn't keep a stale title/body with Save enabled.
            if self._current is None:
                self._clear_editor()
                self._set_editor_enabled(False)
            self._update_status()

    def _apply_filter(self) -> None:
        needle = self._search.text().strip().lower()
        by_path = {n.path: n for n in self._notes}
        for i in range(self._list.count()):
            item = self._list.item(i)
            note = by_path.get(item.data(QtCore.Qt.UserRole))
            haystack = ""
            if note:
                haystack = f"{note.title}\n{note.tags}\n{note.body}".lower()
            item.setHidden(bool(needle) and needle not in haystack)

    # ── Editing / selection ──────────────────────────────────────────────
    def _on_selection_changed(self, current, _previous):
        if self._loading:
            return
        # Capture the clicked note's path *now*: if there are unsaved changes,
        # the save below calls refresh() which clears and rebuilds the list,
        # destroying the ``current`` item. Touching it afterwards raises
        # "wrapped C/C++ object of type QListWidgetItem has been deleted".
        path = current.data(QtCore.Qt.UserRole) if current is not None else None
        if self._dirty:
            self._save_current()
        if path is None:
            self._current = None
            self._clear_editor()
            self._set_editor_enabled(False)
            self._update_status()
            return
        note = next((n for n in self._notes if n.path == path), None)
        self._load_into_editor(note)
        # A save above re-selects the *previous* note's row inside refresh(),
        # leaving the list highlighting a different note than the editor shows.
        # Restore the highlight to the note actually loaded, guarded so this
        # programmatic selection change doesn't re-enter this slot.
        self._loading = True
        self._select_path(path)
        self._loading = False

    def _load_into_editor(self, note: _Note | None) -> None:
        self._current = note
        self._loading = True
        self._reset_preview()
        if note is None:
            self._clear_editor()
            self._set_editor_enabled(False)
        else:
            self._title_edit.setText(note.title)
            self._tags_edit.setText(note.tags)
            self._editor.setPlainText(note.body)
            self._set_editor_enabled(True)
        self._loading = False
        self._dirty = False
        self._update_status()

    def _reset_preview(self) -> None:
        """Always return to the source-edit view when switching notes."""
        if self._preview_btn.isChecked():
            self._preview_btn.setChecked(False)
            self._toggle_preview()

    def _clear_editor(self) -> None:
        self._loading = True
        self._title_edit.clear()
        self._tags_edit.clear()
        self._editor.clear()
        self._loading = False

    def _set_editor_enabled(self, on: bool) -> None:
        for w in (self._title_edit, self._tags_edit, self._editor,
                  self._format_bar, self._save_btn, self._export_btn,
                  self._del_btn):
            w.setEnabled(on)

    def _on_edited(self, *_):
        if self._loading or self._current is None:
            return
        self._dirty = True
        self._update_status()

    def _update_status(self) -> None:
        if self._current is None:
            self._status_lbl.setText("No note selected")
            return
        if self._dirty:
            self._status_lbl.setText("● Unsaved changes")
        else:
            self._status_lbl.setText(
                f"Saved · updated {_fmt_stamp(self._current.updated)}"
            )

    # ── Actions ──────────────────────────────────────────────────────────
    def _on_new(self):
        if self._dirty:
            self._save_current()
        now = _now_iso()
        # Unique filename so two quick "New" clicks don't collide.
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(_notes_dir(), f"untitled-{stamp}.md")
        n = 1
        while os.path.exists(path):
            path = os.path.join(_notes_dir(), f"untitled-{stamp}-{n}.md")
            n += 1
        note = _Note(path, "Untitled note", "", "", now, now)
        note.save()
        self.refresh()
        self._select_path(path)
        self._title_edit.setFocus()
        self._title_edit.selectAll()

    def _on_delete(self):
        if self._current is None:
            return
        resp = QtWidgets.QMessageBox.question(
            self, "Delete note",
            f"Delete “{self._current.title}”?\nThe file will be removed from _notes.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if resp != QtWidgets.QMessageBox.Yes:
            return
        try:
            os.remove(self._current.path)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Delete failed", str(exc))
            return
        self._current = None
        self._dirty = False
        self.refresh()

    def _maybe_rename_to_title(self) -> None:
        """Rename an auto-named ``untitled-*`` file after its title, once.

        Only files still carrying the generated ``untitled-`` name are
        renamed; notes already given a meaningful name (typed here earlier, or
        imported by a colleague) are left untouched so their filename stays
        stable for syncing/sharing."""
        note = self._current
        old_name = os.path.basename(note.path)
        if not old_name.startswith("untitled-"):
            return
        title = self._title_edit.text().strip()
        if not title:
            return
        ext = os.path.splitext(old_name)[1] or ".md"
        slug = _slugify(title)
        dst = os.path.join(_notes_dir(), slug + ext)
        n = 1
        while os.path.exists(dst):
            dst = os.path.join(_notes_dir(), f"{slug}-{n}{ext}")
            n += 1
        try:
            os.rename(note.path, dst)
            note.path = dst
        except OSError:
            pass  # keep the old name; renaming is best-effort, not critical

    def _save_current(self):
        if self._current is None:
            return
        self._current.title = self._title_edit.text().strip() or "Untitled note"
        self._current.tags = self._tags_edit.text().strip()
        self._current.body = self._editor.toPlainText().strip() + "\n"
        self._maybe_rename_to_title()
        try:
            self._current.save()
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Save failed", str(exc))
            return
        self._dirty = False
        # Refresh list label/ordering but keep the current note selected.
        self.refresh(keep_current=True)
        self._update_status()

    def _on_import(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Import notes", "",
            "Note files (*.md *.markdown *.txt);;All files (*.*)",
        )
        if not paths:
            return
        imported = 0
        for src in paths:
            dst = os.path.join(_notes_dir(), os.path.basename(src))
            base, ext = os.path.splitext(dst)
            n = 1
            while os.path.exists(dst):
                dst = f"{base}-{n}{ext}"
                n += 1
            try:
                shutil.copy2(src, dst)
                imported += 1
            except OSError as exc:
                QtWidgets.QMessageBox.warning(self, "Import failed", str(exc))
        if imported:
            self.refresh()
            self._status_lbl.setText(f"Imported {imported} note(s)")

    def _on_export(self):
        if self._current is None:
            return
        if self._dirty:
            self._save_current()
        suggested = os.path.basename(self._current.path)
        dst, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export note", suggested,
            "Markdown (*.md);;Text (*.txt);;All files (*.*)",
        )
        if not dst:
            return
        try:
            shutil.copy2(self._current.path, dst)
            self._status_lbl.setText(f"Exported to {dst}")
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(exc))

    def _open_folder(self):
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(_notes_dir())
        )

    # ── helpers ──────────────────────────────────────────────────────────
    def _select_path(self, path: str) -> None:
        for i in range(self._list.count()):
            if self._list.item(i).data(QtCore.Qt.UserRole) == path:
                self._list.setCurrentRow(i)
                return
