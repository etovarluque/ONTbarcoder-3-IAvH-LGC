from __future__ import annotations
import os
import datetime
import math
import random
from typing import Dict, List, Optional, Tuple
from array import array
from collections import Counter
from PyQt5 import QtCore, QtGui, QtWidgets
from .shared import *
from .shared import _get_base_dir, _tr, _fmt_num


# ═══════════════════════════════════════════════════════════════════════════
# FASTQ INSPECTOR – chart widgets
# ═══════════════════════════════════════════════════════════════════════════

# Chart frame/separator color (only affects these charts, independent of
# GRAY_LINE for the rest of the UI). Change it here.
CHART_FRAME_COLOR = "#B7BABA"


class _HistWidget(QtWidgets.QWidget):
    """Histogram drawn with QPainter.
    Bins are pre-computed in set_data() so paintEvent is O(n_bins), not O(n_values).
    render_to_pixmap() draws at any resolution with scaled fonts for crisp PDF export.
    """

    def __init__(self, title="", x_label="", color=BLUE_MID, n_bins=60, parent=None):
        super().__init__(parent)
        self._title   = title
        self._x_label = x_label
        self._color   = QtGui.QColor(color)
        self._n_bins  = n_bins
        self._counts: List[int] = []
        self._v_min   = 0.0
        self._v_max   = 1.0
        self._max_cnt = 1
        self.setMinimumHeight(210)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    def set_data(self, values, display_min=None, display_max=None):
        """Pre-compute histogram bins. display_min/max clip the x-axis without
        affecting statistics — only the visible range of the chart changes."""
        if not values:
            self._counts = []
            self.update()
            return

        v_min = display_min if display_min is not None else min(values)
        v_max = display_max if display_max is not None else max(values)
        if v_min >= v_max:
            v_max = v_min + 1

        self._v_min = v_min
        self._v_max = v_max
        span = v_max - v_min

        counts = [0] * self._n_bins
        for v in values:
            if v < v_min or v > v_max:
                continue
            idx = int((v - v_min) / span * self._n_bins)
            if idx >= self._n_bins:
                idx = self._n_bins - 1
            counts[idx] += 1

        self._counts  = counts
        self._max_cnt = max(counts) or 1
        self.update()

    def set_precomputed(self, counts: list, v_min: float, v_max: float):
        """Accept bins already computed in the worker thread (avoids O(n) on main thread)."""
        if not counts:
            self._counts = []
            self.update()
            return
        self._v_min   = v_min
        self._v_max   = v_max if v_max > v_min else v_min + 1
        self._counts  = counts
        self._max_cnt = max(counts) or 1
        self.update()

    def render_to_pixmap(self, w: int, h: int, scale: float = 1.0) -> QtGui.QPixmap:
        pix = QtGui.QPixmap(w, h)
        pix.fill(QtCore.Qt.white)
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing)
        self._draw(p, w, h, scale)
        p.end()
        return pix

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        self._draw(p, self.width(), self.height(), 1.0)
        p.end()

    def _draw(self, p: QtGui.QPainter, W: int, H: int, s: float = 1.0):
        PL = int(58 * s); PR = int(14 * s); PT = int(28 * s); PB = int(44 * s)

        p.fillRect(0, 0, W, H, QtGui.QColor("#FFFFFF"))

        font_t = QtGui.QFont()
        font_t.setBold(True)
        font_t.setPointSizeF(9 * s)
        p.setFont(font_t)
        p.setPen(QtGui.QColor(TEXT_PRI))
        p.drawText(QtCore.QRect(PL, 4, W - PL - PR, PT - 4),
                   QtCore.Qt.AlignCenter, self._title)

        cw, ch = W - PL - PR, H - PT - PB
        p.setPen(QtGui.QPen(QtGui.QColor(CHART_FRAME_COLOR), max(1, int(s))))
        p.drawRect(PL, PT, cw, ch)

        if not self._counts:
            font_e = QtGui.QFont()
            font_e.setPointSizeF(8 * s)
            p.setFont(font_e)
            p.setPen(QtGui.QColor(TEXT_HINT))
            p.drawText(QtCore.QRect(PL, PT, cw, ch), QtCore.Qt.AlignCenter, "No data")
            return

        gap   = max(1, int(s))
        bar_w = cw / self._n_bins
        fill  = QtGui.QColor(self._color)
        fill.setAlpha(190)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(fill)
        for i, cnt in enumerate(self._counts):
            bh = int(cnt / self._max_cnt * ch)
            bx = PL + int(i * bar_w)
            p.drawRect(bx + gap, PT + ch - bh, max(1, int(bar_w) - gap), bh)

        span = self._v_max - self._v_min
        lw   = int(52 * s); lh = int(14 * s)
        font_s = QtGui.QFont()
        font_s.setPointSizeF(7 * s)
        p.setFont(font_s)
        p.setPen(QtGui.QColor(TEXT_SEC))
        for i in range(5):
            xv = self._v_min + span * i / 4
            px = PL + int(cw * i / 4)
            p.drawText(QtCore.QRect(px - lw // 2, PT + ch + int(4 * s), lw, lh),
                       QtCore.Qt.AlignCenter, _fmt_num(xv))
        for i in range(4):
            yv = int(self._max_cnt * i / 3)
            py = PT + ch - int(ch * i / 3)
            p.drawText(QtCore.QRect(1, py - lh // 2, PL - int(4 * s), lh),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, _fmt_num(yv))

        if self._x_label:
            font_xl = QtGui.QFont()
            font_xl.setPointSizeF(8 * s)
            p.setFont(font_xl)
            p.setPen(QtGui.QColor(TEXT_SEC))
            p.drawText(QtCore.QRect(PL, H - int(16 * s), cw, int(16 * s)),
                       QtCore.Qt.AlignCenter, self._x_label)


class _ScatterWidget(QtWidgets.QWidget):
    """Scatter plot drawn with QPainter.
    Points are pre-filtered to the display range in set_data() so paintEvent
    only iterates the visible subset.
    render_to_pixmap() draws at any resolution with scaled fonts for crisp PDF export.
    """

    def __init__(self, title="", x_label="", y_label="", color=BLUE_MID, parent=None):
        super().__init__(parent)
        self._title   = title
        self._x_label = x_label
        self._y_label = y_label
        self._color   = QtGui.QColor(color)
        self._pts: List[Tuple[float, float]] = []
        self._x_min = 0.0
        self._x_max = 1.0
        self._y_min = 0.0
        self._y_max = 1.0
        self.setMinimumHeight(210)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    def set_data(self, xs, ys, x_min=None, x_max=None, y_min=None, y_max=None):
        """Pre-filter points to the display range. x_min/x_max clip the x-axis
        without affecting statistics — only the visible range of the chart changes.
        y_min/y_max let the caller pin the Y axis (e.g. the full-data Q range) so the
        scatter lines up with its companion histogram instead of deriving the range
        from the sampled subset."""
        if not xs:
            self._pts = []
            self.update()
            return

        xlo = x_min if x_min is not None else min(xs)
        xhi = x_max if x_max is not None else max(xs)
        if xlo >= xhi:
            xhi = xlo + 1

        pts = [(x, y) for x, y in zip(xs, ys) if xlo <= x <= xhi]

        self._x_min = xlo
        self._x_max = xhi
        self._y_min = y_min if y_min is not None else min((pt[1] for pt in pts), default=0.0)
        self._y_max = y_max if y_max is not None else max((pt[1] for pt in pts), default=1.0)
        if self._y_min >= self._y_max:
            self._y_max = self._y_min + 1
        self._pts = pts
        self.update()

    def render_to_pixmap(self, w: int, h: int, scale: float = 1.0) -> QtGui.QPixmap:
        pix = QtGui.QPixmap(w, h)
        pix.fill(QtCore.Qt.white)
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing)
        self._draw(p, w, h, scale)
        p.end()
        return pix

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        self._draw(p, self.width(), self.height(), 1.0)
        p.end()

    def _draw(self, p: QtGui.QPainter, W: int, H: int, s: float = 1.0):
        PL = int(58 * s); PR = int(14 * s); PT = int(28 * s); PB = int(44 * s)

        p.fillRect(0, 0, W, H, QtGui.QColor("#FFFFFF"))

        font_t = QtGui.QFont()
        font_t.setBold(True)
        font_t.setPointSizeF(9 * s)
        p.setFont(font_t)
        p.setPen(QtGui.QColor(TEXT_PRI))
        p.drawText(QtCore.QRect(PL, 4, W - PL - PR, PT - 4),
                   QtCore.Qt.AlignCenter, self._title)

        cw, ch = W - PL - PR, H - PT - PB
        p.setPen(QtGui.QPen(QtGui.QColor(CHART_FRAME_COLOR), max(1, int(s))))
        p.drawRect(PL, PT, cw, ch)

        if not self._pts:
            font_e = QtGui.QFont()
            font_e.setPointSizeF(8 * s)
            p.setFont(font_e)
            p.setPen(QtGui.QColor(TEXT_HINT))
            p.drawText(QtCore.QRect(PL, PT, cw, ch), QtCore.Qt.AlignCenter, "No data")
            return

        xr = self._x_max - self._x_min
        yr = self._y_max - self._y_min

        dot = QtGui.QColor(self._color)
        dot.setAlpha(60)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(dot)
        r = max(2, int(2 * s))
        for x, y in self._pts:
            px = PL + int((x - self._x_min) / xr * cw)
            py = PT + ch - int((y - self._y_min) / yr * ch)
            p.drawEllipse(QtCore.QPoint(px, py), r, r)

        lw   = int(52 * s); lh = int(14 * s)
        font_s = QtGui.QFont()
        font_s.setPointSizeF(7 * s)
        p.setFont(font_s)
        p.setPen(QtGui.QColor(TEXT_SEC))
        for i in range(5):
            xv = self._x_min + xr * i / 4
            px = PL + int(cw * i / 4)
            p.drawText(QtCore.QRect(px - lw // 2, PT + ch + int(4 * s), lw, lh),
                       QtCore.Qt.AlignCenter, _fmt_num(xv))
        for i in range(4):
            yv = self._y_min + yr * i / 3
            py = PT + ch - int(ch * i / 3)
            p.drawText(QtCore.QRect(1, py - lh // 2, PL - int(4 * s), lh),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, _fmt_num(yv))

        if self._x_label:
            font_xl = QtGui.QFont()
            font_xl.setPointSizeF(8 * s)
            p.setFont(font_xl)
            p.setPen(QtGui.QColor(TEXT_SEC))
            p.drawText(QtCore.QRect(PL, H - int(16 * s), cw, int(16 * s)),
                       QtCore.Qt.AlignCenter, self._x_label)

        if self._y_label:
            p.save()
            p.translate(int(11 * s), PT + ch // 2)
            p.rotate(-90)
            font_yl = QtGui.QFont()
            font_yl.setPointSizeF(8 * s)
            p.setFont(font_yl)
            p.setPen(QtGui.QColor(TEXT_SEC))
            p.drawText(QtCore.QRect(-ch // 2, -lh // 2, ch, lh),
                       QtCore.Qt.AlignCenter, self._y_label)
            p.restore()


# ═══════════════════════════════════════════════════════════════════════════
# FASTQ INSPECTOR – module-level helpers for multiprocessing
# (must be at module level so they are picklable on Windows)
# ═══════════════════════════════════════════════════════════════════════════

def _fq_sync_record(fh_bin) -> bool:
    """Advance a binary file handle to the start of the next valid FASTQ record.
    Needed when seeking to an arbitrary byte offset inside the file.
    Returns True if a record was found, False on EOF."""
    for _ in range(16):
        line = fh_bin.readline()
        if not line:
            return False
        if not line.startswith(b'@'):
            continue
        # Verify: next line is seq, then '+', then qual of matching length
        pos   = fh_bin.tell()
        seq   = fh_bin.readline()
        plus  = fh_bin.readline()
        qual  = fh_bin.readline()
        if plus.startswith(b'+') and len(seq.strip()) == len(qual.strip()):
            fh_bin.seek(pos - len(line))   # rewind to the '@' header
            return True
        # False positive (@ in a quality line) — rewind past the 3 look-ahead
        # lines so the next scan re-examines them; otherwise a real header
        # consumed during verification would be skipped.
        fh_bin.seek(pos)
    return False


# Phred+33 byte -> error probability. A read's mean Q is -10*log10 of the mean
# error probability (ONT / NanoPlot convention), exactly as the analysis
# quality filter computes it (pipeline._PHRED_ERR), so the numbers shown here
# predict what that filter keeps. The arithmetic mean of the Phred values
# overestimates quality because a few bad bases barely move it.
import gzip
import zlib

_PHRED_ERR = [10.0 ** (-(q - 33) / 10.0) for q in range(256)]
_PHRED_ERR_GET = _PHRED_ERR.__getitem__

FASTQ_EXTS = (".fastq", ".fq", ".fastq.gz", ".fq.gz")


def is_fastq_path(path: str) -> bool:
    return path.lower().endswith(FASTQ_EXTS)


def collect_fastq_paths(paths) -> List[str]:
    """Expand dropped/selected paths into FASTQ files: folders are walked
    recursively (e.g. fastq_pass/ with barcode sub-folders)."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                out.extend(os.path.join(root, f) for f in files if is_fastq_path(f))
        elif os.path.isfile(p) and is_fastq_path(p):
            out.append(p)
    seen = set()
    uniq = []
    for p in sorted(out, key=lambda x: x.lower()):
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _natural_key(text: str):
    """barcode2 < barcode10."""
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def group_fastq_paths(paths) -> Tuple[List[str], Dict[str, str], str]:
    """Expand the input like collect_fastq_paths() and assign every file to a
    group for the per-group breakdown. Returns (files, {file: group}, kind):
      • one folder dropped  → its first-level sub-folders (barcode01/, …);
        files directly inside it form a '(folder root)' group;
      • several items       → one group per dropped folder / per dropped file;
      • a single file       → a single group (no breakdown shown).
    kind is "sub-folder", "folder / file" or "" and titles the table."""
    group_of: Dict[str, str] = {}
    if len(paths) == 1 and os.path.isdir(paths[0]):
        root = paths[0]
        files = collect_fastq_paths([root])
        for f in files:
            parts = os.path.relpath(f, root).split(os.sep)
            group_of[f] = parts[0] if len(parts) > 1 else "(folder root)"
        return files, group_of, "sub-folder"
    files: List[str] = []
    for p in paths:
        sub = collect_fastq_paths([p])
        if os.path.isdir(p):
            name = os.path.basename(os.path.normpath(p)) + "/"
        else:
            name = os.path.basename(p)
        for f in sub:
            if f not in group_of:
                group_of[f] = name
                files.append(f)
    files.sort(key=lambda x: x.lower())
    return files, group_of, ("folder / file" if len(paths) > 1 else "")


def _read_q(qual: bytes) -> float:
    """Per-read mean Q from the mean error probability."""
    mean_err = sum(map(_PHRED_ERR_GET, qual)) / len(qual)
    return -10.0 * math.log10(mean_err) if mean_err > 0 else 60.0


class _FqAcc:
    """Per-read values in compact arrays (4 bytes each instead of ~30 for a
    Python number in a list) plus running counters. Arrays also pickle as raw
    bytes, which keeps the parallel path cheap."""

    def __init__(self):
        self.lengths = array("I")
        self.q_scores = array("f")
        self.gc_pcts = array("f")
        self.total_bases = 0
        self.q_sum = self.gc_sum = self.gc_sq_sum = 0.0
        self.len_lt500 = self.len_500_1k = self.len_gt1k = 0
        self.q_cnt_10 = self.q_cnt_15 = self.q_cnt_20 = 0
        # Bases carried by reads at or above each Q: the usable yield after a
        # quality filter (long bad reads weigh more than their read count says)
        self.bases_q10 = self.bases_q15 = self.bases_q20 = 0

    def add(self, seq: bytes, qual: bytes):
        L = len(seq)
        gc = (seq.count(b"G") + seq.count(b"C") +
              seq.count(b"g") + seq.count(b"c")) / L * 100.0
        q = _read_q(qual) if qual else 0.0
        self.lengths.append(L)
        self.q_scores.append(q)
        self.gc_pcts.append(gc)
        self.total_bases += L
        self.q_sum += q
        self.gc_sum += gc
        self.gc_sq_sum += gc * gc
        if L < 500:
            self.len_lt500 += 1
        elif L <= 1000:
            self.len_500_1k += 1
        else:
            self.len_gt1k += 1
        if q >= 10:
            self.q_cnt_10 += 1
            self.bases_q10 += L
        if q >= 15:
            self.q_cnt_15 += 1
            self.bases_q15 += L
        if q >= 20:
            self.q_cnt_20 += 1
            self.bases_q20 += L

    _COUNTERS = ("total_bases", "q_sum", "gc_sum", "gc_sq_sum", "len_lt500", "len_500_1k",
                 "len_gt1k", "q_cnt_10", "q_cnt_15", "q_cnt_20",
                 "bases_q10", "bases_q15", "bases_q20")

    def snapshot(self) -> tuple:
        """Counters used for the per-group breakdown (diffed around each file)."""
        return (len(self.lengths), self.total_bases, self.q_sum,
                self.q_cnt_10, self.bases_q10)

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self._COUNTERS}
        d.update(lengths=self.lengths, q_scores=self.q_scores, gc_pcts=self.gc_pcts)
        return d

    @staticmethod
    def dict_snapshot(d: dict) -> tuple:
        """snapshot() of a to_dict() result (a task's partial accumulator)."""
        return (len(d["lengths"]), d["total_bases"], d["q_sum"],
                d["q_cnt_10"], d["bases_q10"])

    def merge(self, d: dict):
        self.lengths.extend(d["lengths"])
        self.q_scores.extend(d["q_scores"])
        self.gc_pcts.extend(d["gc_pcts"])
        for k in self._COUNTERS:
            setattr(self, k, getattr(self, k) + d[k])


def _fq_chunk_task(path: str, start: int, end: int) -> dict:
    """Process reads in byte range [start, end) of an UNCOMPRESSED FASTQ file.
    Returns a partial _FqAcc as a dict that the worker merges afterwards.
    Defined at module level so ProcessPoolExecutor can pickle it on Windows."""
    acc = _FqAcc()
    try:
        with open(path, "rb", buffering=1 << 20) as fh_bin:
            if start > 0:
                fh_bin.seek(start)
                if not _fq_sync_record(fh_bin):
                    return acc.to_dict()

            # Partition records by their START offset: this worker owns every
            # record beginning in [start, end). Using readline() + an explicit
            # record-start offset (instead of `for line in fh` + tell(), whose
            # read-ahead buffer makes tell() inaccurate) avoids both dropping the
            # record that straddles the boundary and double-counting it — the next
            # worker resyncs to the first record starting at/after `end`.
            while True:
                rec_start = fh_bin.tell()
                if end > 0 and rec_start >= end:
                    break
                raw_header = fh_bin.readline()
                if not raw_header:
                    break
                if not raw_header.startswith(b"@"):
                    continue                          # skip malformed / mid-record
                seq = fh_bin.readline().rstrip(b"\r\n")
                fh_bin.readline()                     # '+'
                qual = fh_bin.readline().rstrip(b"\r\n")
                if seq:
                    acc.add(seq, qual)
    except Exception as exc:
        # The reads read so far are kept, but the gap must be reported:
        # silently missing part of a file would make every statistic wrong.
        d = acc.to_dict()
        d["warning"] = (f"{os.path.basename(path)}: part of the file could not be "
                        f"read ({type(exc).__name__}: {exc}) — statistics are incomplete")
        return d
    return acc.to_dict()


# Errors of a damaged / still-being-written gzip stream: the reads decoded
# before the damage are valid and are kept.
_TRUNCATED_ERRORS = (EOFError, zlib.error, getattr(gzip, "BadGzipFile", OSError))


def _read_fastq_file(path: str, acc: "_FqAcc", on_record=None, should_stop=None):
    """Read every record of one FASTQ / FASTQ.gz into `acc`. Returns a warning
    string when the file is truncated or damaged (reads before that point are
    kept), else "". `on_record(n, raw_pos)` is called every 5000 reads and
    `should_stop()` is polled there (in-thread use); both are optional so the
    same reader serves the process-pool task."""
    is_gz = path.lower().endswith(".gz")
    # Binary mode so quality bytes map straight onto the error table.
    # For gzip, a 4 MB BufferedReader feeds decompression in large chunks.
    raw = open(path, "rb", buffering=4 * 1024 * 1024)
    fh = gzip.GzipFile(fileobj=raw) if is_gz else raw
    n_local = 0
    try:
        it = iter(fh)
        for header in it:
            # Skip malformed / mid-record lines so this path matches the
            # chunked one (_fq_chunk_task), which also resyncs on non-'@' headers.
            if not header.startswith(b"@"):
                continue
            try:
                seq = next(it).rstrip(b"\r\n")
                next(it)                           # '+' line
                qual = next(it).rstrip(b"\r\n")
            except StopIteration:
                break
            if not seq:
                continue
            acc.add(seq, qual)
            n_local += 1
            if n_local % 5000 == 0:
                if should_stop is not None and should_stop():
                    return ""
                if on_record is not None:
                    on_record(n_local, raw.tell())
    except _TRUNCATED_ERRORS as exc:
        return (f"{os.path.basename(path)}: truncated or damaged ({exc}) — "
                f"the {n_local:,} reads before that point were used")
    finally:
        fh.close()
        if is_gz:
            raw.close()
    return ""


def _fq_file_task(path: str) -> dict:
    """One whole file in a worker process (many small files in parallel).
    Defined at module level so ProcessPoolExecutor can pickle it on Windows."""
    acc = _FqAcc()
    try:
        warning = _read_fastq_file(path, acc)
    except Exception as exc:
        warning = (f"{os.path.basename(path)}: could not be read "
                   f"({type(exc).__name__}: {exc}) — statistics are incomplete")
    d = acc.to_dict()
    if warning:
        d["warning"] = warning
    return d


# ═══════════════════════════════════════════════════════════════════════════
# FASTQ INSPECTOR – background worker
# ═══════════════════════════════════════════════════════════════════════════

class _FastqInspectorWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, int)   # reads processed, % of input bytes read
    # Not named "finished": that would shadow QThread's own finished() signal.
    done     = QtCore.pyqtSignal(dict)
    error    = QtCore.pyqtSignal(str)

    _SCATTER_MAX = 10_000

    # ── minimum file size to justify splitting ONE file across processes ──
    _PARALLEL_THRESHOLD = 50 * 1024 * 1024   # 50 MB uncompressed
    # ── minimum input to justify a process pool across files ──────────────
    # (each process imports this module once: ~0.5 s on Windows)
    _POOL_MIN_BYTES = 16 * 1024 * 1024

    def __init__(self, paths, groups: Optional[Dict[str, str]] = None, parent=None):
        super().__init__(parent)
        self._paths = [paths] if isinstance(paths, str) else list(paths)
        self._groups = groups or {}   # file -> group name (per-group breakdown)
        self._stop = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _n_procs() -> int:
        return max(1, min((os.cpu_count() or 2) - 1, 12))

    def run(self):
        try:
            self._warnings: List[str] = []
            # A file can vanish between selection and analysis (MinKNOW moves
            # files): skip it, say so, and report only what was analyzed.
            paths, sizes = [], []
            for p in self._paths:
                try:
                    sizes.append(os.path.getsize(p))
                    paths.append(p)
                except OSError:
                    self._warnings.append(f"{os.path.basename(p)}: no longer exists — skipped")
            self._n_files, self._n_bytes = len(paths), sum(sizes)
            self._total_bytes = max(self._n_bytes, 1)
            # group -> [files, reads, bases, q_sum, reads_q10, bases_q10]
            self._group_stats: Dict[str, list] = {}
            acc = _FqAcc()
            if not paths:
                raise ValueError("None of the selected FASTQ files exists any more.")

            if len(paths) > 1 and self._n_bytes >= self._POOL_MIN_BYTES and self._n_procs() > 1:
                self._run_pool(paths, sizes, acc)
            else:
                done = 0
                for path, size in zip(paths, sizes):
                    if self._stop:
                        return
                    before = acc.snapshot()
                    if not path.lower().endswith(".gz") and size >= self._PARALLEL_THRESHOLD:
                        self._run_chunked(path, size, acc, done)
                    else:
                        self._run_sequential(path, acc, done)
                    done += size
                    self._add_group(path, before, acc.snapshot())
            if self._stop:
                return
            self._compute_and_emit(acc)
        except ValueError as exc:
            self.error.emit(str(exc))
        except Exception as exc:
            import traceback
            self.error.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")

    def _add_group(self, path: str, before: tuple, after: tuple, new_file: bool = True):
        g = self._group_stats.setdefault(
            self._groups.get(path, os.path.basename(path)), [0, 0, 0, 0.0, 0, 0])
        if new_file:
            g[0] += 1
        for i, (b, a) in enumerate(zip(before, after), start=1):
            g[i] += a - b

    def _emit_progress(self, acc: _FqAcc, bytes_done: int):
        pct = min(100, int(bytes_done * 100 / self._total_bytes))
        self.progress.emit(len(acc.lengths), pct)

    def _run_sequential(self, path: str, acc: _FqAcc, done_before: int):
        """Read all records of one file in this thread. Progress is measured on
        the bytes read from disk — for gzip that is the compressed position, so
        the percentage is meaningful for .gz too."""
        warning = _read_fastq_file(
            path, acc,
            on_record=lambda _n, pos: self._emit_progress(acc, done_before + pos),
            should_stop=lambda: self._stop)
        if warning:
            self._warnings.append(warning)
        self._emit_progress(acc, done_before + os.path.getsize(path))

    def _run_chunked(self, path: str, file_size: int, acc: _FqAcc, done_before: int):
        """Split ONE large uncompressed FASTQ into byte ranges processed in
        parallel (one Python process per chunk, bypassing the GIL)."""
        from concurrent.futures import ProcessPoolExecutor, as_completed

        n_workers = min(self._n_procs(), 8)
        chunk = file_size // n_workers
        # Byte ranges — end=0 means "read until EOF"
        boundaries = [(i * chunk, (i + 1) * chunk if i < n_workers - 1 else 0)
                      for i in range(n_workers)]

        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = [pool.submit(_fq_chunk_task, path, s, e) for s, e in boundaries]
            for k, future in enumerate(as_completed(futures), start=1):
                if self._stop:
                    pool.shutdown(wait=False, cancel_futures=True)
                    return
                d = future.result()
                if d.get("warning"):
                    self._warnings.append(d["warning"])
                acc.merge(d)
                self._emit_progress(acc, done_before + file_size * k // n_workers)

    def _run_pool(self, paths: List[str], sizes: List[int], acc: _FqAcc):
        """Many files: one task per file (a large plain FASTQ is still split
        into byte ranges), all sharing one process pool. ONT output is usually
        hundreds of small .fastq.gz, which a single thread reads one by one."""
        from concurrent.futures import ProcessPoolExecutor, as_completed

        n_workers = self._n_procs()
        bytes_done = 0
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {}
            for path, size in zip(paths, sizes):
                if not path.lower().endswith(".gz") and size >= self._PARALLEL_THRESHOLD:
                    n_chunks = min(n_workers, 8)
                    chunk = size // n_chunks
                    for i in range(n_chunks):
                        s = i * chunk
                        e = (i + 1) * chunk if i < n_chunks - 1 else 0
                        fut = pool.submit(_fq_chunk_task, path, s, e)
                        futures[fut] = (path, size // n_chunks, i == 0)
                else:
                    futures[pool.submit(_fq_file_task, path)] = (path, size, True)
            for future in as_completed(futures):
                if self._stop:
                    pool.shutdown(wait=False, cancel_futures=True)
                    return
                path, share, first = futures[future]
                d = future.result()
                if d.get("warning"):
                    self._warnings.append(d["warning"])
                acc.merge(d)
                self._add_group(path, (0, 0, 0, 0.0, 0), _FqAcc.dict_snapshot(d),
                                new_file=first)
                bytes_done += share
                self._emit_progress(acc, bytes_done)

    def _compute_and_emit(self, acc: _FqAcc):
        """Compute final statistics and histogram bins, then emit finished.
        Length statistics come from a length->count table instead of sorting
        every length, which keeps millions of reads cheap."""
        N_BINS = 60
        lengths, q_scores, gc_pcts = acc.lengths, acc.q_scores, acc.gc_pcts
        n = len(lengths)
        if n == 0:
            self.error.emit("No reads found in the input.")
            return

        len_counts = Counter(lengths)
        keys = sorted(len_counts)

        def _nth(rank):
            """Length at 0-based rank in the sorted order."""
            cum = 0
            for k in keys:
                cum += len_counts[k]
                if cum > rank:
                    return k
            return keys[-1]

        total_bases = acc.total_bases
        mean_len = total_bases / n
        median_len = ((_nth(n // 2 - 1) + _nth(n // 2)) / 2 if n % 2 == 0
                      else _nth(n // 2))
        min_len, max_len = keys[0], keys[-1]

        # N50
        half, cumsum, n50 = total_bases / 2, 0, max_len
        for k in reversed(keys):
            cumsum += k * len_counts[k]
            if cumsum >= half:
                n50 = k
                break

        mean_q = acc.q_sum / n
        mean_gc = acc.gc_sum / n
        # Sample std from running sums (no second pass over every read).
        var_gc = (acc.gc_sq_sum - n * mean_gc * mean_gc) / max(n - 1, 1)
        std_gc = math.sqrt(max(var_gc, 0.0))

        len_p01 = _nth(max(0, int(n * 0.01)))
        len_p99 = _nth(min(n - 1, int(n * 0.99)))

        def _make_bins(values, v_min, v_max):
            span = v_max - v_min if v_max > v_min else 1.0
            bins = [0] * N_BINS
            for v in values:
                if v_min <= v <= v_max:
                    bins[min(int((v - v_min) / span * N_BINS), N_BINS - 1)] += 1
            return bins

        q_bin_min = min(q_scores)
        q_bin_max = max(q_scores)
        # Lengths: binned from the length -> count table (a few thousand
        # distinct lengths) instead of every read.
        len_bins = [0] * N_BINS
        len_span = len_p99 - len_p01 if len_p99 > len_p01 else 1.0
        for k in keys:
            if len_p01 <= k <= len_p99:
                len_bins[min(int((k - len_p01) / len_span * N_BINS), N_BINS - 1)] += len_counts[k]
        q_bins = _make_bins(q_scores, q_bin_min, q_bin_max)
        gc_bins = _make_bins(gc_pcts, 0.0, 100.0)

        if n > self._SCATTER_MAX:
            idx = random.sample(range(n), self._SCATTER_MAX)
            sc_len = [lengths[i] for i in idx]
            sc_q = [q_scores[i] for i in idx]
        else:
            sc_len = list(lengths)
            sc_q = list(q_scores)

        groups = []
        for name in sorted(getattr(self, "_group_stats", {}), key=_natural_key):
            nf, gr, gb, gq, gr10, gb10 = self._group_stats[name]
            groups.append({
                "name": name, "files": nf, "reads": gr, "bases": gb,
                "reads_pct": gr / n * 100,
                "mean_len": gb / gr if gr else 0.0,
                "mean_q": gq / gr if gr else 0.0,
                "q10_pct": gr10 / gr * 100 if gr else 0.0,
                "bases_q10_pct": gb10 / gb * 100 if gb else 0.0,
            })

        self.done.emit({
            "warnings":   list(getattr(self, "_warnings", [])),
            "n_files":    getattr(self, "_n_files", len(self._paths)),
            "n_bytes":    getattr(self, "_n_bytes", 0),
            "groups":     groups,
            "bases_q10_pct": acc.bases_q10 / total_bases * 100 if total_bases else 0.0,
            "bases_q15_pct": acc.bases_q15 / total_bases * 100 if total_bases else 0.0,
            "bases_q20_pct": acc.bases_q20 / total_bases * 100 if total_bases else 0.0,
            "n_reads":    n,
            "total_bases": total_bases,
            "mean_len":   mean_len,
            "median_len": median_len,
            "min_len":    min_len,
            "max_len":    max_len,
            "n50":        n50,
            "len_lt500":  acc.len_lt500,
            "len_500_1k": acc.len_500_1k,
            "len_gt1k":   acc.len_gt1k,
            "mean_q":     mean_q,
            "q10_pct":    acc.q_cnt_10 / n * 100,
            "q15_pct":    acc.q_cnt_15 / n * 100,
            "q20_pct":    acc.q_cnt_20 / n * 100,
            "mean_gc":    mean_gc,
            "std_gc":     std_gc,
            "len_bins":   len_bins,
            "q_bins":     q_bins,
            "gc_bins":    gc_bins,
            "q_min":      q_bin_min,   # full-data range used to build q_bins
            "q_max":      q_bin_max,
            "sc_len":     sc_len,
            "sc_q":       sc_q,
            "len_p01":    len_p01,
            "len_p99":    len_p99,
        })


# ═══════════════════════════════════════════════════════════════════════════
# FASTQ INSPECTOR – panel
# ═══════════════════════════════════════════════════════════════════════════

class _FastqDropZone(QtWidgets.QFrame):
    """Drop zone for one or several FASTQ files and/or folders (walked
    recursively, e.g. ONT fastq_pass/). Emits the expanded FASTQ list."""
    filesChosen = QtCore.pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drop_zone")
        self.setAcceptDrops(True)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(4)
        lay.setAlignment(QtCore.Qt.AlignCenter)
        lbl = make_label("Drag FASTQ files or a folder here", color=TEXT_SEC)
        lbl.setAlignment(QtCore.Qt.AlignCenter)
        self.setMinimumHeight(180)
        hint = make_label(
            "Accepted: .fastq · .fq · .fastq.gz · .fq.gz  —  a folder is searched "
            "recursively (e.g. fastq_pass/)", size=15, color=TEXT_HINT)
        hint.setAlignment(QtCore.Qt.AlignCenter)
        row = QtWidgets.QHBoxLayout()
        row.setAlignment(QtCore.Qt.AlignCenter)
        row.setSpacing(8)
        for text, slot in (("Browse files…", self._browse_files),
                           ("Browse folder…", self._browse_folder)):
            b = QtWidgets.QPushButton(text)
            b.setObjectName("secondary_btn")
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addWidget(lbl)
        lay.addWidget(hint)
        lay.addSpacing(8)
        lay.addLayout(row)

    def _browse_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Select FASTQ files", "",
            "FASTQ (*.fastq *.fq *.fastq.gz *.fq.gz);;All files (*)")
        if paths:
            self.filesChosen.emit(paths)

    def _browse_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Select a folder with FASTQ files")
        if path:
            self.filesChosen.emit([path])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setProperty("dragging", "true")
            refresh_style(self)

    def dragLeaveEvent(self, e):
        self.setProperty("dragging", "false")
        refresh_style(self)

    def dropEvent(self, e):
        self.setProperty("dragging", "false")
        refresh_style(self)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.filesChosen.emit(paths)


class _NumItem(QtWidgets.QTableWidgetItem):
    """Table cell that shows formatted text but sorts by its numeric UserRole."""

    def __lt__(self, other):
        a = self.data(QtCore.Qt.UserRole)
        b = other.data(QtCore.Qt.UserRole)
        if a is not None and b is not None:
            return a < b
        return super().__lt__(other)


def _fmt_size(b):
    if b >= 1_073_741_824: return f"{b/1_073_741_824:.2f} GB"
    if b >= 1_048_576:     return f"{b/1_048_576:.1f} MB"
    return f"{b/1_024:.1f} KB"


class FastqInspectorPanel(QtWidgets.QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Scroll area ──────────────────────────────────────────────────
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        inner = QtWidgets.QWidget()
        self._layout = QtWidgets.QVBoxLayout(inner)
        self._layout.setContentsMargins(20, 20, 20, 20)
        self._layout.setSpacing(16)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # ── Title ──
        self._layout.addWidget(make_label("FASTQ Inspector", size=19, bold=True))
        desc = make_label(
            "Descriptive report of one or several FASTQ files (or a whole folder). "
            "All reads are analyzed; only the scatter plot uses a random sample of 10 000 points.",
            color=TEXT_SEC,
        )
        desc.setWordWrap(True)
        self._layout.addWidget(desc)

        # ── Drop zone (collapses to a compact row once input is chosen) ──
        self._drop = _FastqDropZone()
        self._drop.filesChosen.connect(self._on_files)
        self._layout.addWidget(self._drop)

        self._file_row = QtWidgets.QFrame()
        self._file_row.setObjectName("fq_file_row")
        self._file_row.setStyleSheet(
            f"QFrame#fq_file_row {{ background:{WHITE}; border:1px solid {GRAY_LINE};"
            f" border-radius:8px; }}")
        fr = QtWidgets.QHBoxLayout(self._file_row)
        fr.setContentsMargins(14, 8, 10, 8)
        self._file_row_lbl = make_label("", size=16, bold=True, color=GREEN)
        self._file_row_lbl.setWordWrap(True)
        fr.addWidget(self._file_row_lbl, 1)
        change_btn = QtWidgets.QPushButton("Change…")
        change_btn.setObjectName("secondary_btn")
        change_btn.clicked.connect(self._show_drop)
        fr.addWidget(change_btn)
        self._file_row.hide()
        self._layout.addWidget(self._file_row)

        # ── Status + progress bar ──
        self._status_lbl = make_label("", color=TEXT_SEC)
        self._layout.addWidget(self._status_lbl)

        self._progress_bar = QtWidgets.QProgressBar()
        self._progress_bar.setRange(0, 0)
        # Taller than the global 6 px bar: this one carries a real percentage
        self._progress_bar.setFixedHeight(22)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("%p%")
        self._progress_bar.setStyleSheet(
            f"QProgressBar {{ background-color:{GRAY_LINE}; border:none;"
            f" border-radius:6px; color:{TEXT_PRI}; font-size:13px; font-weight:600;"
            f" text-align:center; }}"
            f"QProgressBar::chunk {{ background-color:{BLUE_MID}; border-radius:6px; }}")
        self._progress_bar.hide()
        self._layout.addWidget(self._progress_bar)

        # ── Stats section ──
        # Local helper: section label in BLUE for this panel
        def _sec(text):
            lbl = make_section_label(text)
            lbl.setStyleSheet(
                f"font-size:18px; font-weight:600; color:{BLUE}; letter-spacing:0.5px;"
            )
            return lbl

        self._stats_w = QtWidgets.QWidget()
        self._stats_w.hide()
        sl = QtWidgets.QVBoxLayout(self._stats_w)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(10)

        # Every card row sits on the same 5-column grid so columns line up
        # across sections instead of some rows stretching and others not.
        def _grid(cards):
            g = QtWidgets.QGridLayout()
            g.setHorizontalSpacing(8)
            for col in range(5):
                g.setColumnStretch(col, 1)
            for col, c in enumerate(cards):
                g.addWidget(c, 0, col)
            sl.addLayout(g)

        sl.addWidget(_sec("General"))
        self._sc_reads    = self._card("Total reads")
        self._sc_bases    = self._card("Total bases")
        self._sc_filesize = self._card("Input size")
        self._sc_nfiles   = self._card("Files")
        _grid([self._sc_reads, self._sc_bases, self._sc_filesize, self._sc_nfiles])

        sl.addWidget(_sec("Read length"))
        self._sc_min    = self._card("Min")
        self._sc_max    = self._card("Max")
        self._sc_mean   = self._card("Mean")
        self._sc_median = self._card("Median")
        self._sc_n50    = self._card("N50", color=BLUE)
        _grid([self._sc_min, self._sc_max, self._sc_mean, self._sc_median, self._sc_n50])

        self._sc_lt500  = self._card("< 500 bp")
        self._sc_500_1k = self._card("500–1000 bp")
        self._sc_gt1k   = self._card("> 1000 bp")
        _grid([self._sc_lt500, self._sc_500_1k, self._sc_gt1k])

        sl.addWidget(_sec("Base quality (Phred)"))
        q_note = make_label(
            "Per-read Q = −10·log10(mean error probability): the same measure the "
            "analysis quality filter uses (ONT convention).", size=14, color=TEXT_SEC)
        q_note.setWordWrap(True)
        sl.addWidget(q_note)
        self._sc_meanq = self._card("Mean Q-score", color=GREEN)
        self._sc_q10   = self._card("Reads Q≥10")
        self._sc_q15   = self._card("Reads Q≥15")
        self._sc_q20   = self._card("Reads Q≥20")
        _grid([self._sc_meanq, self._sc_q10, self._sc_q15, self._sc_q20])
        # Same thresholds weighted by bases: usable yield after a quality filter
        self._sc_bq10 = self._card("Bases in reads Q≥10")
        self._sc_bq15 = self._card("Bases in reads Q≥15")
        self._sc_bq20 = self._card("Bases in reads Q≥20")
        for c in (self._sc_bq10, self._sc_bq15, self._sc_bq20):
            c.setToolTip("Share of all bases carried by reads at or above this Q:\n"
                         "the sequence that survives an equivalent quality filter.")
        _grid([QtWidgets.QWidget(), self._sc_bq10, self._sc_bq15, self._sc_bq20])

        sl.addWidget(_sec("GC content"))
        self._sc_meangc = self._card("Mean %GC")
        self._sc_stdgc  = self._card("Std dev %GC")
        _grid([self._sc_meangc, self._sc_stdgc])

        # ── Per-group breakdown (sub-folders such as barcode01/, or the
        #    dropped files/folders); hidden when there is a single group ──
        self._groups_title = _sec("By sub-folder")
        sl.addWidget(self._groups_title)
        self._groups_table = QtWidgets.QTableWidget(0, 8)
        self._groups_table.setHorizontalHeaderLabels(
            ["Group", "Files", "Reads", "% of reads", "Bases",
             "Mean length", "Mean Q", "Reads Q≥10"])
        self._groups_table.verticalHeader().setVisible(False)
        self._groups_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._groups_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._groups_table.setAlternatingRowColors(True)
        self._groups_table.setStyleSheet(
            "QTableWidget::item { padding: 0 12px; }"
            "QHeaderView::section { padding: 4px 12px; font-weight: 600; }")
        hh = self._groups_table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for col in range(1, 8):
            hh.setSectionResizeMode(col, QtWidgets.QHeaderView.ResizeToContents)
        self._groups_table.setSortingEnabled(True)
        sl.addWidget(self._groups_table)
        self._groups_title.hide()
        self._groups_table.hide()

        self._layout.addWidget(self._stats_w)

        # ── Charts section ──
        self._charts_w = QtWidgets.QWidget()
        self._charts_w.hide()
        cl = QtWidgets.QVBoxLayout(self._charts_w)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(12)

        cl.addWidget(_sec("Distributions"))

        row_c1 = QtWidgets.QHBoxLayout()
        row_c1.setSpacing(12)
        self._hist_len = _HistWidget("Read length distribution", "Length (bp)",  color=BLUE_MID)
        self._hist_q   = _HistWidget("Mean Q-score per read",    "Mean Q-score", color=GREEN_MID)
        row_c1.addWidget(self._hist_len)
        row_c1.addWidget(self._hist_q)
        cl.addLayout(row_c1)

        row_c2 = QtWidgets.QHBoxLayout()
        row_c2.setSpacing(12)
        self._hist_gc = _HistWidget("GC content per read",              "% GC",        color=AMBER)
        self._scatter  = _ScatterWidget("Read length vs. mean Q-score", "Length (bp)", "Mean Q-score", color=BLUE_MID)
        row_c2.addWidget(self._hist_gc)
        row_c2.addWidget(self._scatter)
        cl.addLayout(row_c2)

        self._layout.addWidget(self._charts_w)
        self._layout.addStretch()

        # ── Footer ──────────────────────────────────────────────────────
        footer = QtWidgets.QWidget()
        footer.setObjectName("fqins_footer")
        footer.setStyleSheet(f"""
            QWidget#fqins_footer {{
                background: {GRAY_CARD};
                border-top: 1px solid {GRAY_LINE};
            }}
        """)
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(20, 10, 20, 10)
        fl.setSpacing(8)

        self._clear_btn = QtWidgets.QPushButton("Clear")
        self._clear_btn.setObjectName("danger_btn")
        self._clear_btn.setFixedHeight(44)
        self._clear_btn.setFixedWidth(140)
        self._clear_btn.clicked.connect(self._clear)
        fl.addWidget(self._clear_btn)

        # Cancels the running analysis but keeps the loaded input
        self._stop_btn = QtWidgets.QPushButton("Stop")
        self._stop_btn.setObjectName("danger_btn")
        self._stop_btn.setFixedHeight(44)
        self._stop_btn.setFixedWidth(120)
        self._stop_btn.clicked.connect(self._stop_analysis)
        self._stop_btn.hide()
        fl.addWidget(self._stop_btn)

        self._pdf_btn = QtWidgets.QPushButton("Export PDF  ↓")
        self._pdf_btn.setObjectName("secondary_btn")
        self._pdf_btn.setFixedHeight(44)
        self._pdf_btn.setFixedWidth(190)
        self._pdf_btn.hide()
        self._pdf_btn.clicked.connect(self._on_pdf_clicked)
        fl.addWidget(self._pdf_btn)

        fl.addStretch()

        self._analyze_btn = QtWidgets.QPushButton("Analyze  →")
        self._analyze_btn.setObjectName("primary_btn")
        self._analyze_btn.setFixedHeight(44)
        self._analyze_btn.setFixedWidth(300)
        self._analyze_btn.setEnabled(False)
        self._analyze_btn.clicked.connect(self._start)
        self._analyze_btn.setStyleSheet(
            f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
            f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
        )
        fl.addWidget(self._analyze_btn)
        outer.addWidget(footer)

        self._worker: Optional[_FastqInspectorWorker] = None
        # Detached-but-maybe-running workers, kept referenced so they aren't GC'd
        # mid-run (which crashes Qt). Purged in _retire_worker.
        self._retired_workers: set = set()
        self._files: List[str] = []
        self._file_size   = 0
        self._input_label = ""   # shown in the file row and the PDF
        self._input_stem  = ""   # base of the PDF file name
        self._last_result: Optional[dict] = None
        self._last_pdf    = ""
        self._pdf_ready   = False   # True after first successful export

    # ── helpers ──────────────────────────────────────────────────────────

    def _card(self, label: str, color: str = TEXT_PRI) -> QtWidgets.QFrame:
        frame = QtWidgets.QFrame()
        frame.setObjectName("stat_card")
        vl = QtWidgets.QVBoxLayout(frame)
        vl.setContentsMargins(10, 8, 10, 8)
        vl.setSpacing(2)
        vl.addWidget(make_label(label, size=15, color=TEXT_SEC))
        val_lbl = make_label("—", size=18, bold=True, color=color)
        vl.addWidget(val_lbl)
        frame._val = val_lbl   # type: ignore[attr-defined]
        return frame

    def _set_analyze_enabled(self, enabled: bool):
        self._analyze_btn.setEnabled(enabled)
        if enabled:
            self._analyze_btn.setStyleSheet(
                f"QPushButton {{ background-color: {BLUE}; color: white; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
                f"QPushButton:hover {{ background-color: #0C4A82; }}"
            )
        else:
            self._analyze_btn.setStyleSheet(
                f"QPushButton {{ background-color: {GRAY_LINE}; color: {TEXT_HINT}; border:none; "
                f"border-radius:8px; padding:9px 20px; font-size:18px; font-weight:500; }}"
            )

    # PDF display size per chart (matches HTML img width/height)
    _PDF_CHART_W = 520
    _PDF_CHART_H = 280
    _PDF_SCALE   = 3   # oversample factor → renders at 1110×690, shown at 370×230

    def _chart_to_b64(self, widget) -> str:
        """Render at 3× for crisp fonts, then downscale to the exact display size.
        The embedded PNG is already at _PDF_CHART_W × _PDF_CHART_H so Qt needs
        no scaling and the image occupies exactly the expected space in the PDF."""
        w   = self._PDF_CHART_W * self._PDF_SCALE
        h   = self._PDF_CHART_H * self._PDF_SCALE
        pix = widget.render_to_pixmap(w, h, float(self._PDF_SCALE))
        pix = pix.scaled(self._PDF_CHART_W, self._PDF_CHART_H,
                         QtCore.Qt.IgnoreAspectRatio,
                         QtCore.Qt.SmoothTransformation)
        buf = QtCore.QBuffer()
        buf.open(QtCore.QIODevice.WriteOnly)
        pix.save(buf, "PNG")
        return bytes(buf.data().toBase64()).decode()

    def _retire_worker(self):
        """Detach the current worker: disconnect its slots so stale results never
        reach the UI, and keep a reference until its thread exits so it is never
        garbage-collected while still running (which crashes Qt)."""
        self._retired_workers = {w for w in self._retired_workers if w.isRunning()}
        w = self._worker
        self._worker = None
        if w is None:
            return
        for sig in (w.progress, w.done, w.error):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
        if w.isRunning():
            w.stop()
            self._retired_workers.add(w)

    # ── slots ─────────────────────────────────────────────────────────────

    def _on_files(self, paths: list):
        """Files and/or folders chosen: expand to FASTQ files and show them as a
        compact row (the large drop zone would push the results below the fold)."""
        files, self._group_of, self._group_kind = group_fastq_paths(paths)
        # Cancel any running analysis — new input supersedes the old one
        self._retire_worker()
        self._stats_w.hide()
        self._charts_w.hide()
        self._pdf_btn.hide()
        self._pdf_btn.setText("Export PDF  ↓")
        self._pdf_ready = False
        self._status_lbl.setStyleSheet("")
        if not files:
            self._status_lbl.setStyleSheet(f"color:{RED};")
            self._status_lbl.setText(
                "No FASTQ files found (.fastq, .fq, .fastq.gz, .fq.gz).")
            return
        self._files = files
        self._file_size = sum(os.path.getsize(f) for f in files)
        if len(paths) == 1 and os.path.isdir(paths[0]):
            base = os.path.basename(os.path.normpath(paths[0]))
            self._input_label = f"{base}/ ({len(files)} files)"
            self._input_stem = base
        elif len(files) == 1:
            self._input_label = os.path.basename(files[0])
            self._input_stem = self._stem_of(files[0])
        else:
            self._input_label = f"{len(files)} FASTQ files"
            self._input_stem = f"{self._stem_of(files[0])}_and_{len(files) - 1}_more"
        icon = "📁" if len(files) > 1 else "📄"
        self._file_row_lbl.setText(
            f"{icon}  {self._input_label}  ·  {_fmt_size(self._file_size)}")
        self._file_row_lbl.setToolTip("\n".join(files[:30]) +
                                      ("\n…" if len(files) > 30 else ""))
        self._drop.hide()
        self._file_row.show()
        self._status_lbl.setText("")
        self._set_analyze_enabled(True)

    @staticmethod
    def _stem_of(path: str) -> str:
        name = os.path.basename(path)
        for ext in (".fastq.gz", ".fq.gz", ".fastq", ".fq"):
            if name.lower().endswith(ext):
                return name[:-len(ext)]
        return os.path.splitext(name)[0]

    def _show_drop(self):
        """'Change…': show the drop zone again; results stay until new input."""
        self._file_row.hide()
        self._drop.show()

    def _clear(self):
        self._retire_worker()
        self._files       = []
        self._file_size   = 0
        self._input_label = ""
        self._input_stem  = ""
        self._last_result = None
        self._last_pdf    = ""
        self._pdf_ready   = False
        self._file_row.hide()
        self._drop.show()
        self._status_lbl.setStyleSheet("")
        self._status_lbl.setText("")
        self._progress_bar.hide()
        self._stop_btn.hide()
        self._stats_w.hide()
        self._charts_w.hide()
        self._groups_title.hide()
        self._groups_table.hide()
        self._group_of = {}
        self._group_kind = ""
        self._pdf_btn.setText("Export PDF  ↓")
        self._pdf_btn.hide()
        self._set_analyze_enabled(False)

    def _start(self):
        files = [f for f in self._files if os.path.isfile(f)]
        if not files:
            return
        # Stop any previous worker before starting a new one
        self._retire_worker()
        self._set_analyze_enabled(False)
        self._status_lbl.setStyleSheet("")
        self._status_lbl.setText("Analyzing…  reading all reads")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.show()
        self._stop_btn.show()
        self._stats_w.hide()
        self._charts_w.hide()
        self._pdf_btn.hide()

        self._worker = _FastqInspectorWorker(files, getattr(self, "_group_of", {}))
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop_analysis(self):
        """Cancel the running analysis; the loaded input stays ready to re-run."""
        self._retire_worker()
        self._progress_bar.hide()
        self._stop_btn.hide()
        self._status_lbl.setStyleSheet(f"color:{AMBER};")
        self._status_lbl.setText("Stopped — press Analyze to run again.")
        self._set_analyze_enabled(bool(self._files))

    def _on_progress(self, n: int, pct: int):
        self._progress_bar.setValue(pct)
        self._status_lbl.setText(f"Analyzing…  {pct}%  ·  {n:,} reads processed")

    def _on_finished(self, r: dict):
        self._progress_bar.hide()
        self._stop_btn.hide()
        self._last_result = r
        n = r["n_reads"]
        warnings = r.get("warnings") or []
        if warnings:
            # Partial input (truncated .gz, vanished file, unreadable chunk):
            # the numbers are valid for what was read, but say so clearly.
            self._status_lbl.setStyleSheet(f"color:{AMBER};")
            self._status_lbl.setText(
                f"Done — {n:,} reads analyzed  ·  ⚠ {len(warnings)} file problem(s): "
                + warnings[0] + ("  (hover for all)" if len(warnings) > 1 else ""))
            self._status_lbl.setToolTip("\n".join(warnings))
        else:
            self._status_lbl.setStyleSheet("")
            self._status_lbl.setToolTip("")
            self._status_lbl.setText(f"Done — {n:,} reads analyzed")

        def _fmt_bp(v):
            if v >= 1_000_000_000: return f"{v/1_000_000_000:.2f} Gbp"
            if v >= 1_000_000:     return f"{v/1_000_000:.1f} Mbp"
            if v >= 1_000:         return f"{v/1_000:.1f} Kbp"
            return f"{v} bp"

        self._sc_reads._val.setText(f"{n:,}")
        self._sc_bases._val.setText(_fmt_bp(r["total_bases"]))
        # What was actually analyzed (files can vanish before the run starts)
        self._sc_filesize._val.setText(_fmt_size(r.get("n_bytes") or self._file_size))
        self._sc_nfiles._val.setText(f"{r.get('n_files', len(self._files)):,}")

        self._sc_min._val.setText(f"{r['min_len']:,} bp")
        self._sc_max._val.setText(f"{r['max_len']:,} bp")
        self._sc_mean._val.setText(f"{r['mean_len']:,.0f} bp")
        self._sc_median._val.setText(f"{r['median_len']:,.0f} bp")
        self._sc_n50._val.setText(f"{r['n50']:,} bp")

        self._sc_lt500._val.setText(f"{r['len_lt500']:,}  ({r['len_lt500']/n*100:.1f}%)")
        self._sc_500_1k._val.setText(f"{r['len_500_1k']:,}  ({r['len_500_1k']/n*100:.1f}%)")
        self._sc_gt1k._val.setText(f"{r['len_gt1k']:,}  ({r['len_gt1k']/n*100:.1f}%)")

        self._sc_meanq._val.setText(f"Q{r['mean_q']:.1f}")
        self._sc_q10._val.setText(f"{r['q10_pct']:.1f}%")
        self._sc_q15._val.setText(f"{r['q15_pct']:.1f}%")
        self._sc_q20._val.setText(f"{r['q20_pct']:.1f}%")
        self._sc_bq10._val.setText(f"{r['bases_q10_pct']:.1f}%")
        self._sc_bq15._val.setText(f"{r['bases_q15_pct']:.1f}%")
        self._sc_bq20._val.setText(f"{r['bases_q20_pct']:.1f}%")
        self._fill_groups_table(r.get("groups", []))

        self._sc_meangc._val.setText(f"{r['mean_gc']:.1f}%")
        self._sc_stdgc._val.setText(f"± {r['std_gc']:.1f}%")

        # Bins were pre-computed in the worker thread — O(1) on main thread
        self._hist_len.set_precomputed(r["len_bins"], r["len_p01"], r["len_p99"])
        self._hist_q.set_precomputed(r["q_bins"],   r["q_min"],   r["q_max"])
        self._hist_gc.set_precomputed(r["gc_bins"],  0.0,          100.0)
        self._scatter.set_data(r["sc_len"], r["sc_q"],
                               x_min=r["len_p01"], x_max=r["len_p99"],
                               y_min=r["q_min"], y_max=r["q_max"])

        self._stats_w.show()
        self._charts_w.show()
        self._set_analyze_enabled(False)
        self._pdf_btn.setText("Export PDF  ↓")
        self._pdf_ready = False
        self._pdf_btn.show()

    def _fill_groups_table(self, groups: list):
        """Per-group table; shown only when the input has 2+ groups."""
        show = len(groups) > 1
        self._groups_title.setVisible(show)
        self._groups_table.setVisible(show)
        if not show:
            return
        kind = getattr(self, "_group_kind", "") or "file"
        self._groups_title.setText(f"BY {kind.upper()}")

        def _num_item(value, text):
            # Sort numerically while displaying formatted text
            it = _NumItem(text)
            it.setData(QtCore.Qt.UserRole, value)
            it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            return it

        t = self._groups_table
        t.setSortingEnabled(False)
        t.setRowCount(len(groups))
        for row, g in enumerate(groups):
            t.setItem(row, 0, QtWidgets.QTableWidgetItem(g["name"]))
            cells = [
                (g["files"], f"{g['files']:,}"),
                (g["reads"], f"{g['reads']:,}"),
                (g["reads_pct"], f"{g['reads_pct']:.1f}%"),
                (g["bases"], f"{g['bases']:,}"),
                (g["mean_len"], f"{g['mean_len']:,.0f} bp"),
                (g["mean_q"], f"Q{g['mean_q']:.1f}"),
                (g["q10_pct"], f"{g['q10_pct']:.1f}%"),
            ]
            for col, (val, text) in enumerate(cells, start=1):
                t.setItem(row, col, _num_item(val, text))
        # Height fits up to ~14 rows, then the table scrolls
        rows_h = sum(t.rowHeight(i) for i in range(min(len(groups), 14)))
        t.setFixedHeight(t.horizontalHeader().height() + rows_h + 4)

    def _on_error(self, msg: str):
        self._progress_bar.hide()
        self._stop_btn.hide()
        self._set_analyze_enabled(bool(self._files))
        self._status_lbl.setStyleSheet(f"color:{RED};")
        self._status_lbl.setText(f"Error: {error_summary(msg)}")
        if "\n" in msg:
            show_error_dialog(self, "FASTQ Inspector error", msg)

    # ── PDF export ────────────────────────────────────────────────────────

    def _on_pdf_clicked(self):
        if self._pdf_ready and self._last_pdf:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._last_pdf))
        else:
            self._export_pdf()

    def _export_pdf(self):
        if not self._last_result:
            return

        ts          = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        folder_name = f"ont-barcoder_{ts}_fastq_ins"
        pdf_name    = f"{self._input_stem or 'fastq'}_stats.pdf"
        prog_dir    = _get_base_dir()
        auto_path   = os.path.join(prog_dir, "output", folder_name, pdf_name)

        # ── "Where to save results?" dialog — same style as other panels ──
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
        title_lbl.setStyleSheet(f"font-size:17px; font-weight:700; color:{TEXT_PRI};")
        vlay.addWidget(title_lbl)

        radio_auto = QtWidgets.QRadioButton(
            f"Automatic folder (recommended)\n  …/output/{folder_name}/{pdf_name}"
        )
        radio_auto.setChecked(True)
        radio_custom = QtWidgets.QRadioButton("Select folder manually")
        vlay.addWidget(radio_auto)
        vlay.addWidget(radio_custom)
        vlay.addSpacing(8)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QtWidgets.QPushButton("Cancel")
        btn_cancel.setObjectName("dlg_cancel_btn")
        btn_cancel.setFixedHeight(38)
        btn_ok = QtWidgets.QPushButton("Export PDF")
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

        if radio_auto.isChecked():
            out_path = auto_path
        else:
            parent_dir = QtWidgets.QFileDialog.getExistingDirectory(
                self, "Select the output folder"
            )
            if not parent_dir:
                return
            out_path = os.path.join(parent_dir, folder_name, pdf_name)

        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
        except Exception:
            pass

        try:
            from PyQt5.QtPrintSupport import QPrinter
            from PyQt5.QtGui import QTextDocument

            printer = QPrinter(QPrinter.HighResolution)
            printer.setOutputFormat(QPrinter.PdfFormat)
            printer.setOutputFileName(out_path)
            printer.setPageSize(QPrinter.Letter)
            printer.setOrientation(QPrinter.Portrait)
            printer.setPageMargins(15, 15, 15, 15, QPrinter.Millimeter)

            doc = QTextDocument()
            doc.setHtml(self._build_pdf_html())
            doc.print_(printer)

            self._last_pdf  = out_path
            self._pdf_ready = True
            self._pdf_btn.setText("Open PDF  ↗")

        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "PDF export", f"Could not generate PDF:\n{exc}")

    def _build_pdf_html(self) -> str:
        r = self._last_result
        n = r["n_reads"]
        fname = self._input_label
        ts = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M")

        def _fmt_bp(v):
            if v >= 1_000_000_000: return f"{v/1_000_000_000:.2f} Gbp"
            if v >= 1_000_000:     return f"{v/1_000_000:.1f} Mbp"
            if v >= 1_000:         return f"{v/1_000:.1f} Kbp"
            return f"{v} bp"

        def _fmt_size(b):
            if b >= 1_073_741_824: return f"{b/1_073_741_824:.2f} GB"
            if b >= 1_048_576:     return f"{b/1_048_576:.1f} MB"
            return f"{b/1_024:.1f} KB"

        # Render charts
        imgs = {k: self._chart_to_b64(w) for k, w in (
            ("len", self._hist_len),
            ("q",   self._hist_q),
            ("gc",  self._hist_gc),
            ("sc",  self._scatter),
        )}

        css = """
        @page {
            size: Letter;
            margin: 1.5cm;
        }
        body {
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
            background: white;
            color: #2C3E50;
            margin: 0;
            padding: 0;
            font-size: 10pt;
            line-height: 1.5;
        }
        /* Header — solid background + explicit white text for Qt (no gradients or negative margins) */
        .header {
            background: #1a365d;
            color: white;
            padding: 22px 25px;
            margin-bottom: 20px;
            text-align: center;
        }
        .header h1 {
            font-size: 28pt;
            font-weight: 700;
            margin: 0 0 6px 0;
            color: white;
        }
        .header h1 strong {
            font-weight: 700;
        }
        .header .subtitle {
            font-size: 9pt;
            color: #90CDF4;
            margin-top: 8px;
        }
        .file-info {
            background: #EBF4FF;
            border-left: 4px solid #3182CE;
            padding: 12px 18px;
            margin: 20px 0;
            border-radius: 8px;
            font-family: 'Courier New', monospace;
            font-size: 9pt;
        }
        /* Metric cards */
        .metrics-grid {
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
            margin: 20px 0;
            page-break-inside: avoid;
        }
        .metric-card {
            flex: 1;
            min-width: 110px;
            background: #F7FAFC;
            border: 1px solid #E2E8F0;
            border-radius: 12px;
            padding: 12px;
            text-align: center;
            page-break-inside: avoid;
        }
        .metric-card .icon {
            font-size: 22px;
            margin-bottom: 6px;
        }
        .metric-card .label {
            font-size: 8pt;
            color: #718096;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
        }
        .metric-card .value {
            font-size: 16pt;
            font-weight: 700;
            color: #2D3748;
        }
        .metric-card .unit {
            font-size: 8pt;
            color: #A0AEC0;
        }
        /* Sections */
        .section { margin: 15px 0; }
        /* Title inside the table's thead — page-break-inside:avoid on the table
           is the only reliable way to keep title and data together in QTextDocument */
        .section-title-row {
            font-size: 13pt;
            font-weight: 600;
            color: #2C5282;
            text-align: left;
            padding: 7px 12px 5px 12px;
            background: white;
            border-bottom: 2px solid #CBD5E0;
        }
        .section-title-row i { font-weight: 400; color: #718096; }
        /* Standalone title (charts section only) */
        .section-title {
            font-size: 14pt;
            font-weight: 600;
            color: #2C5282;
            border-bottom: 2px solid #CBD5E0;
            padding-bottom: 6px;
            margin: 0 0 10px 0;
        }
        .section-title i { font-weight: 400; color: #718096; }
        /* Modern tables */
        .data-table {
            /* QTextDocument doesn't honor margin:auto (it pushes the table right).
               Centered with an EXPLICIT left margin instead:
                 width        = table width
                 margin-left  = left gap (≈ (100-width)/2 to center;
                                with 80% → ~70px on Letter).
               Increase margin-left to move it right, decrease it to move it
               toward the left edge. */
            width: 60%;
            border-collapse: collapse;
            margin: 12px 0 12px 70px;
            font-size: 9.5pt;
            page-break-inside: avoid;
        }
        .data-table th {
            background: #EDF2F7;
            color: #2D3748;
            font-weight: 600;
            padding: 7px 12px;
            text-align: left;
            border-bottom: 2px solid #CBD5E0;
        }
        .data-table td {
            padding: 5px 12px;
            border-bottom: 1px solid #E2E8F0;
        }
        .data-table td.value {
            font-weight: 600;
            color: #3182CE;
            text-align: right;
            font-family: 'Courier New', monospace;
        }
        /* Notes below the charts */
        .chart-note {
            font-size: 8pt;
            color: #A0AEC0;
            text-align: center;
            margin-top: 4px;
        }
        /* Footer */
        .footer {
            margin-top: 35px;
            padding-top: 15px;
            text-align: center;
            font-size: 8pt;
            color: #A0AEC0;
            border-top: 1px solid #E2E8F0;
        }
        /* Quality badges */
        .badge-good { background: #C6F6D5; color: #22543D; padding: 2px 8px; border-radius: 20px; font-size: 8pt; font-weight: 600; }
        .badge-warning { background: #FEEBC8; color: #7B341E; padding: 2px 8px; border-radius: 20px; font-size: 8pt; font-weight: 600; }
        .badge-info { background: #BEE3F8; color: #1A365D; padding: 2px 8px; border-radius: 20px; font-size: 8pt; font-weight: 600; }
        """

        # Compute quality indicators
        q_mean = r['mean_q']
        q_badge = 'badge-good' if q_mean >= 20 else ('badge-warning' if q_mean >= 15 else 'badge-info')
        q_badge_text = 'Excellent' if q_mean >= 20 else ('Good' if q_mean >= 15 else 'Fair')

        # Metric cards
        metrics = f"""
        <div class="metrics-grid">
            <div class="metric-card">
                <div class="icon">📖</div>
                <div class="label">Total Reads</div>
                <div class="value">{n:,}</div>
            </div>
            <div class="metric-card">
                <div class="icon">🧬</div>
                <div class="label">Total Bases</div>
                <div class="value">{_fmt_bp(r['total_bases'])}</div>
            </div>
            <div class="metric-card">
                <div class="icon">📏</div>
                <div class="label">N50</div>
                <div class="value">{r['n50']:,}</div>
                <div class="unit">bp</div>
            </div>
            <div class="metric-card">
                <div class="icon">⭐</div>
                <div class="label">Mean Quality</div>
                <div class="value">Q{r['mean_q']:.1f}</div>
                <div class="unit"><span class="{q_badge}">{q_badge_text}</span></div>
            </div>
            <div class="metric-card">
                <div class="icon">🧪</div>
                <div class="label">GC Content</div>
                <div class="value">{r['mean_gc']:.1f}%</div>
            </div>
        </div>
        """

        # Length table — title inside thead so page-break-inside:avoid on the
        # table keeps title and rows together (QTextDocument doesn't honor
        # page-break on <div>, but it does on <table> elements)
        length_table = f"""
        <table class="data-table" style="page-break-before: always; margin-top: 0;">
            <thead>
                <tr><th colspan="3" class="section-title-row">📏 Length Distribution <i>— read length statistics</i></th></tr>
                <tr><th>Metric</th><th>Value</th><th></th></tr>
            </thead>
            <tbody>
                <tr><td>Minimum</td><td class="value">{r['min_len']:,} bp</td><td></td></tr>
                <tr><td>Maximum</td><td class="value">{r['max_len']:,} bp</td><td></td></tr>
                <tr><td>Mean</td><td class="value">{r['mean_len']:,.0f} bp</td><td></td></tr>
                <tr><td>Median</td><td class="value">{r['median_len']:,.0f} bp</td><td></td></tr>
                <tr><td>N50</td><td class="value">{r['n50']:,} bp</td><td><span class="badge-info">Assembly standard</span></td></tr>
                <tr><td>&lt; 500 bp</td><td class="value">{r['len_lt500']:,}</td><td>({r['len_lt500']/n*100:.1f}%)</td></tr>
                <tr><td>500–1000 bp</td><td class="value">{r['len_500_1k']:,}</td><td>({r['len_500_1k']/n*100:.1f}%)</td></tr>
                <tr><td>&gt; 1000 bp</td><td class="value">{r['len_gt1k']:,}</td><td>({r['len_gt1k']/n*100:.1f}%)</td></tr>
            </tbody>
        </table>
        """

        # Quality table
        quality_table = f"""
        <table class="data-table">
            <thead>
                <tr><th colspan="3" class="section-title-row">⭐ Base Quality <i>— Phred score distribution</i></th></tr>
                <tr><th>Metric</th><th>Value</th><th>Quality assessment</th></tr>
            </thead>
            <tbody>
                <tr><td>Mean Q-score</td><td class="value">Q{r['mean_q']:.1f}</td><td><span class="{q_badge}">{q_badge_text}</span></td></tr>
                <tr><td>Reads ≥ Q10</td><td class="value">{r['q10_pct']:.1f}%</td><td></td></tr>
                <tr><td>Reads ≥ Q15</td><td class="value">{r['q15_pct']:.1f}%</td><td></td></tr>
                <tr><td>Reads ≥ Q20</td><td class="value">{r['q20_pct']:.1f}%</td><td><span class="badge-good">High confidence</span></td></tr>
                <tr><td>Bases in reads ≥ Q10</td><td class="value">{r.get('bases_q10_pct', 0):.1f}%</td><td>Yield kept by a Q10 filter</td></tr>
                <tr><td>Bases in reads ≥ Q15</td><td class="value">{r.get('bases_q15_pct', 0):.1f}%</td><td></td></tr>
                <tr><td>Bases in reads ≥ Q20</td><td class="value">{r.get('bases_q20_pct', 0):.1f}%</td><td></td></tr>
            </tbody>
        </table>
        """

        # Per-group table (only with 2+ sub-folders / dropped items)
        groups_table = ""
        groups = r.get("groups", [])
        if len(groups) > 1:
            kind = getattr(self, "_group_kind", "") or "file"
            rows = "".join(
                f"<tr><td>{g['name']}</td><td class='value'>{g['reads']:,}</td>"
                f"<td>{g['reads_pct']:.1f}%</td><td>{g['mean_len']:,.0f} bp</td>"
                f"<td>Q{g['mean_q']:.1f}</td><td>{g['q10_pct']:.1f}%</td></tr>"
                for g in groups)
            groups_table = f"""
        <table class="data-table">
            <thead>
                <tr><th colspan="6" class="section-title-row">🗂 By {kind} <i>— reads per group</i></th></tr>
                <tr><th>Group</th><th>Reads</th><th>% of reads</th><th>Mean length</th><th>Mean Q</th><th>Reads ≥ Q10</th></tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        """

        # GC table
        gc_table = f"""
        <table class="data-table">
            <thead>
                <tr><th colspan="3" class="section-title-row">🧬 GC Content <i>— nucleotide composition</i></th></tr>
                <tr><th>Metric</th><th>Value</th><th>Expected range</th></tr>
            </thead>
            <tbody>
                <tr><td>Mean GC content</td><td class="value">{r['mean_gc']:.1f}%</td><td>40–60% for most organisms</td></tr>
                <tr><td>Standard deviation</td><td class="value">±{r['std_gc']:.1f}%</td><td>Lower = more homogeneous</td></tr>
            </tbody>
        </table>
        """

        # The two charts on each sheet go in a SINGLE <table>, and each image's
        # cell carries line-height:100% (see _img_row). Without that, the body's
        # line-height:1.5 inflated each image ~1.5× and only one chart fit per
        # sheet. Result: two charts per sheet.
        _cw = self._PDF_CHART_W   # PNG already embedded at this exact size
        _ch = self._PDF_CHART_H

        def _title_row(title, top_border=False):
            bt = "border-top:1px solid #FBFBFB;" if top_border else ""
            return (
                f'<tr><td style="padding:5px 8px; font-size:10pt; font-weight:600;'
                f' color:#4A5568; border-bottom:1px solid #E2E8F0;{bt}">{title}</td></tr>'
            )

        # ▼▼ Space (px) BELOW each chart, before the next title.
        #    Raise/lower this number to adjust that gap. Careful: if you raise it
        #    too much, the 2nd chart might not fit on the sheet.
        _gap_below_chart = 40

        def _img_row(b64):
            # line-height:100% is key: QTextDocument applies the line-height (1.5
            # inherited from body) MULTIPLICATIVELY to the line containing the
            # image, so each 280px chart occupied ~420px, leaving ~140px of extra
            # space below (which pushed the 2nd chart onto another sheet). At 100%
            # the line is exactly the height of the image.
            return (
                f'<tr><td style="padding:4px 8px {_gap_below_chart}px; line-height:100%;">'
                f'<img src="data:image/png;base64,{b64}" width="{_cw}" height="{_ch}"/>'
                f'</td></tr>'
            )

        _header_row = (
            '<tr><td style="padding:2px 8px 8px; font-size:14pt; font-weight:600;'
            ' color:#2C5282; border-bottom:2px solid #CBD5E0;">'
            '📈 Visual Summary <i style="font-weight:400; color:#718096;">'
            '— distribution plots</i></td></tr>'
        )
        _page_open = ('<table width="100%" cellspacing="0" cellpadding="0"'
                      ' style="page-break-before:always; border:1px solid #E2E8F0;'
                      ' border-collapse:collapse; margin:0;">')

        charts = f"""
    {_page_open}
        {_header_row}
        {_title_row("📊 Read length distribution")}
        {_img_row(imgs['len'])}
        {_title_row("📈 Mean Q-score per read", top_border=True)}
        {_img_row(imgs['q'])}
</table>
    {_page_open}
        {_title_row("🧬 GC content per read")}
        {_img_row(imgs['gc'])}
        {_title_row("📐 Length vs. Quality", top_border=True)}
        {_img_row(imgs['sc'])}
</table>
        """

        return f"""<!DOCTYPE html>
    <html><head><meta charset="utf-8"><style>{css}</style></head>
    <body>

    <div class="header">
        <h1><strong>FASTQ</strong> Inspector Report</h1>
        <div class="subtitle">Generated: {ts}</div>
    </div>

    <div class="file-info">
        📁 <strong>Input file:</strong> {fname}<br>
        💾 <strong>Input size:</strong> {_fmt_size(self._file_size)} · {len(self._files)} file(s)
    </div>

    {metrics}
    {length_table}
    {quality_table}
    {gc_table}
    {groups_table}
    {charts}

    </body></html>"""
