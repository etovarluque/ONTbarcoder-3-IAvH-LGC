from __future__ import annotations
import os
import datetime
import math
import random
from typing import Dict, List, Optional, Tuple
from PyQt5 import QtCore, QtGui, QtWidgets
from .shared import *
from .shared import _get_base_dir, _tr, _fmt_num


# ═══════════════════════════════════════════════════════════════════════════
# FASTQ INSPECTOR – chart widgets
# ═══════════════════════════════════════════════════════════════════════════

# Color del marco/separador de las gráficas (solo afecta a estos gráficos,
# independiente de GRAY_LINE del resto de la interfaz). Cámbialo aquí.
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


def _fq_chunk_task(path: str, start: int, end: int) -> dict:
    """Process reads in byte range [start, end) of an UNCOMPRESSED FASTQ file.
    Returns a partial-stats dict that _FastqInspectorWorker aggregates afterward.
    Defined at module level so ProcessPoolExecutor can pickle it on Windows."""
    lengths:  List[int]   = []
    q_scores: List[float] = []
    gc_pcts:  List[float] = []
    total_bases = 0
    q_sum = gc_sum = 0.0
    len_lt500 = len_500_2k = len_gt2k = 0
    q_cnt_10 = q_cnt_15 = q_cnt_20 = 0

    _EMPTY = {"n_reads": 0, "lengths": [], "q_scores": [], "gc_pcts": [],
              "total_bases": 0, "q_sum": 0.0, "gc_sum": 0.0,
              "len_lt500": 0, "len_500_2k": 0, "len_gt2k": 0,
              "q_cnt_10": 0, "q_cnt_15": 0, "q_cnt_20": 0}

    try:
        with open(path, "rb", buffering=1 << 20) as fh_bin:
            if start > 0:
                fh_bin.seek(start)
                if not _fq_sync_record(fh_bin):
                    return _EMPTY

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
                if not raw_header.startswith(b'@'):
                    continue                          # skip malformed / mid-record
                raw_seq  = fh_bin.readline()
                fh_bin.readline()                    # '+'
                raw_qual = fh_bin.readline()

                seq  = raw_seq.rstrip(b'\r\n')
                qual = raw_qual.rstrip(b'\r\n')
                L    = len(seq)
                ql   = len(qual) or 1
                if L == 0:
                    continue

                # Fast GC — str.count() is C-speed; avoids per-char Python loop
                gc_cnt = (seq.count(b'G') + seq.count(b'C') +
                          seq.count(b'g') + seq.count(b'c'))
                gc     = gc_cnt / L * 100.0

                # Fast Q-mean — bytes iterate as ints; no ord() call needed
                q_mean = (sum(qual) - 33 * ql) / ql

                lengths.append(L)
                q_scores.append(q_mean)
                gc_pcts.append(gc)

                total_bases += L
                q_sum       += q_mean
                gc_sum      += gc

                if L < 500:      len_lt500  += 1
                elif L <= 2000:  len_500_2k += 1
                else:            len_gt2k   += 1

                if q_mean >= 10: q_cnt_10 += 1
                if q_mean >= 15: q_cnt_15 += 1
                if q_mean >= 20: q_cnt_20 += 1

    except Exception:
        pass   # partial result still aggregated

    return {"n_reads": len(lengths), "lengths": lengths, "q_scores": q_scores,
            "gc_pcts": gc_pcts, "total_bases": total_bases,
            "q_sum": q_sum, "gc_sum": gc_sum,
            "len_lt500": len_lt500, "len_500_2k": len_500_2k, "len_gt2k": len_gt2k,
            "q_cnt_10": q_cnt_10, "q_cnt_15": q_cnt_15, "q_cnt_20": q_cnt_20}


# ═══════════════════════════════════════════════════════════════════════════
# FASTQ INSPECTOR – background worker
# ═══════════════════════════════════════════════════════════════════════════

class _FastqInspectorWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int)   # reads processed so far
    finished = QtCore.pyqtSignal(dict)
    error    = QtCore.pyqtSignal(str)

    _SCATTER_MAX = 10_000

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path
        self._stop = False

    def stop(self):
        self._stop = True

    # ── minimum file size to justify spawning worker processes ──────────────
    _PARALLEL_THRESHOLD = 50 * 1024 * 1024   # 50 MB uncompressed

    def run(self):
        try:
            path  = self._path
            is_gz = path.lower().endswith(".gz")
            size  = os.path.getsize(path)
            # Gzip streams cannot be split; only parallelize plain FASTQ files
            # large enough to amortize process-spawn overhead (~200 ms on Windows).
            if not is_gz and size >= self._PARALLEL_THRESHOLD:
                self._run_parallel(path, size)
            else:
                self._run_sequential(path, is_gz)
        except Exception as exc:
            self.error.emit(str(exc))

    def _run_sequential(self, path: str, is_gz: bool):
        """Read all records in one thread.  Used for gzip files and small plain FASTQ."""
        import gzip, io

        lengths:  List[int]   = []
        q_scores: List[float] = []
        gc_pcts:  List[float] = []
        total_bases = 0
        q_sum = gc_sum = 0.0
        len_lt500 = len_500_2k = len_gt2k = 0
        q_cnt_10 = q_cnt_15 = q_cnt_20 = 0

        # Open in binary mode so quality bytes can be summed directly (no ord() call).
        # For gzip, wrap the raw file in a 4 MB BufferedReader so compressed data
        # is read in large chunks before decompression — reduces I/O syscall overhead.
        if is_gz:
            raw = open(path, "rb", buffering=4 * 1024 * 1024)
            fh  = gzip.GzipFile(fileobj=raw)
        else:
            fh  = open(path, "rb", buffering=4 * 1024 * 1024)
            raw = fh

        try:
            it = iter(fh)
            for header in it:
                if self._stop:
                    return
                # Skip malformed / mid-record lines so this path matches the parallel
                # one (_fq_chunk_task), which also resyncs on non-'@' headers.
                if not header.startswith(b"@"):
                    continue
                try:
                    seq  = next(it).rstrip(b"\r\n")
                    next(it)                           # '+' line
                    qual = next(it).rstrip(b"\r\n")
                except StopIteration:
                    break

                L  = len(seq)
                ql = len(qual) or 1
                if L == 0:
                    continue

                # C-speed GC count — str.count() / bytes.count() implemented in C
                gc_cnt = (seq.count(b"G") + seq.count(b"C") +
                          seq.count(b"g") + seq.count(b"c"))
                gc     = gc_cnt / L * 100.0

                # Fast Q-mean — bytes iterate as integers; no ord() needed
                q_mean = (sum(qual) - 33 * ql) / ql

                lengths.append(L)
                q_scores.append(q_mean)
                gc_pcts.append(gc)

                total_bases += L
                q_sum       += q_mean
                gc_sum      += gc

                if L < 500:      len_lt500  += 1
                elif L <= 2000:  len_500_2k += 1
                else:            len_gt2k   += 1

                if q_mean >= 10: q_cnt_10 += 1
                if q_mean >= 15: q_cnt_15 += 1
                if q_mean >= 20: q_cnt_20 += 1

                if len(lengths) % 5000 == 0:
                    self.progress.emit(len(lengths))
        finally:
            fh.close()
            if is_gz:
                raw.close()

        if self._stop:
            return

        self._compute_and_emit(
            lengths, q_scores, gc_pcts,
            total_bases, q_sum, gc_sum,
            len_lt500, len_500_2k, len_gt2k,
            q_cnt_10, q_cnt_15, q_cnt_20,
        )

    def _run_parallel(self, path: str, file_size: int):
        """Split an uncompressed FASTQ file into chunks and process them in parallel
        using ProcessPoolExecutor.  Each worker runs _fq_chunk_task() in its own
        Python process, bypassing the GIL for true CPU-level parallelism."""
        import random
        from concurrent.futures import ProcessPoolExecutor, as_completed

        n_workers = min(os.cpu_count() or 1, 8)
        chunk     = file_size // n_workers

        # Build (start, end) byte ranges — end=0 means "read until EOF"
        boundaries = [(i * chunk, (i + 1) * chunk if i < n_workers - 1 else 0)
                      for i in range(n_workers)]

        all_lengths:  List[int]   = []
        all_q_scores: List[float] = []
        all_gc_pcts:  List[float] = []
        total_bases = 0
        q_sum = gc_sum = 0.0
        len_lt500 = len_500_2k = len_gt2k = 0
        q_cnt_10 = q_cnt_15 = q_cnt_20 = 0

        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_fq_chunk_task, path, s, e): i
                       for i, (s, e) in enumerate(boundaries)}
            for future in as_completed(futures):
                if self._stop:
                    pool.shutdown(wait=False, cancel_futures=True)
                    return
                p = future.result()
                all_lengths.extend(p["lengths"])
                all_q_scores.extend(p["q_scores"])
                all_gc_pcts.extend(p["gc_pcts"])
                total_bases  += p["total_bases"]
                q_sum        += p["q_sum"]
                gc_sum       += p["gc_sum"]
                len_lt500    += p["len_lt500"]
                len_500_2k   += p["len_500_2k"]
                len_gt2k     += p["len_gt2k"]
                q_cnt_10     += p["q_cnt_10"]
                q_cnt_15     += p["q_cnt_15"]
                q_cnt_20     += p["q_cnt_20"]
                self.progress.emit(len(all_lengths))

        if self._stop:
            return

        self._compute_and_emit(
            all_lengths, all_q_scores, all_gc_pcts,
            total_bases, q_sum, gc_sum,
            len_lt500, len_500_2k, len_gt2k,
            q_cnt_10, q_cnt_15, q_cnt_20,
        )

    def _compute_and_emit(
        self,
        lengths, q_scores, gc_pcts,
        total_bases, q_sum, gc_sum,
        len_lt500, len_500_2k, len_gt2k,
        q_cnt_10, q_cnt_15, q_cnt_20,
    ):
        """Compute final statistics and histogram bins, then emit finished signal.
        Shared by both sequential and parallel paths."""
        import math, random

        N_BINS = 60
        n = len(lengths)
        if n == 0:
            self.error.emit("No reads found in file.")
            return

        mean_len    = total_bases / n
        sorted_lens = sorted(lengths)
        median_len  = (sorted_lens[n // 2 - 1] + sorted_lens[n // 2]) / 2 \
                      if n % 2 == 0 else sorted_lens[n // 2]
        min_len = sorted_lens[0]
        max_len = sorted_lens[-1]

        # N50
        half, cumsum, n50 = total_bases / 2, 0, sorted_lens[-1]
        for l in reversed(sorted_lens):
            cumsum += l
            if cumsum >= half:
                n50 = l
                break

        mean_q  = q_sum  / n
        mean_gc = gc_sum / n
        std_gc  = math.sqrt(sum((x - mean_gc) ** 2 for x in gc_pcts) / max(n - 1, 1))

        len_p01 = sorted_lens[max(0,     int(n * 0.01))]
        len_p99 = sorted_lens[min(n - 1, int(n * 0.99))]

        def _make_bins(values, v_min, v_max):
            span = v_max - v_min if v_max > v_min else 1.0
            bins = [0] * N_BINS
            for v in values:
                if v_min <= v <= v_max:
                    bins[min(int((v - v_min) / span * N_BINS), N_BINS - 1)] += 1
            return bins

        q_bin_min = min(q_scores)
        q_bin_max = max(q_scores)
        len_bins = _make_bins(lengths,  len_p01, len_p99)
        q_bins   = _make_bins(q_scores, q_bin_min, q_bin_max)
        gc_bins  = _make_bins(gc_pcts,  0.0, 100.0)

        if n > self._SCATTER_MAX:
            idx    = random.sample(range(n), self._SCATTER_MAX)
            sc_len = [lengths[i]  for i in idx]
            sc_q   = [q_scores[i] for i in idx]
        else:
            sc_len = lengths[:]
            sc_q   = q_scores[:]

        del lengths, q_scores, gc_pcts, sorted_lens

        self.finished.emit({
            "n_reads":    n,
            "total_bases": total_bases,
            "mean_len":   mean_len,
            "median_len": median_len,
            "min_len":    min_len,
            "max_len":    max_len,
            "n50":        n50,
            "len_lt500":  len_lt500,
            "len_500_2k": len_500_2k,
            "len_gt2k":   len_gt2k,
            "mean_q":     mean_q,
            "q10_pct":    q_cnt_10 / n * 100,
            "q15_pct":    q_cnt_15 / n * 100,
            "q20_pct":    q_cnt_20 / n * 100,
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
            "Drag a FASTQ file (.fastq or .fastq.gz) to get a full descriptive report. "
            "All reads are analyzed; only the scatter plot uses a random sample of 10 000 points.",
            color=TEXT_SEC,
        )
        desc.setWordWrap(True)
        self._layout.addWidget(desc)

        # ── Drop zone ──
        self._drop = DropZone(
            "Drag FASTQ file here",
            "Accepted: .fastq  |  .fastq.gz",
            extensions=[".fastq", ".gz"],
        )
        self._drop.fileDropped.connect(self._on_file)
        self._layout.addWidget(self._drop)

        # ── Status + progress bar ──
        self._status_lbl = make_label("", color=TEXT_SEC)
        self._layout.addWidget(self._status_lbl)

        self._progress_bar = QtWidgets.QProgressBar()
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setFixedHeight(6)
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

        sl.addWidget(_sec("General"))
        r = QtWidgets.QHBoxLayout()
        r.setSpacing(8)
        self._sc_reads    = self._card("Total reads")
        self._sc_bases    = self._card("Total bases")
        self._sc_filesize = self._card("File size")
        for c in (self._sc_reads, self._sc_bases, self._sc_filesize):
            r.addWidget(c)
        r.addStretch()
        sl.addLayout(r)

        sl.addWidget(_sec("Read length"))
        r2 = QtWidgets.QHBoxLayout()
        r2.setSpacing(8)
        self._sc_min    = self._card("Min")
        self._sc_max    = self._card("Max")
        self._sc_mean   = self._card("Mean")
        self._sc_median = self._card("Median")
        self._sc_n50    = self._card("N50", color=BLUE)
        for c in (self._sc_min, self._sc_max, self._sc_mean, self._sc_median, self._sc_n50):
            r2.addWidget(c)
        sl.addLayout(r2)

        r3 = QtWidgets.QHBoxLayout()
        r3.setSpacing(8)
        self._sc_lt500  = self._card("< 500 bp")
        self._sc_500_2k = self._card("500–2000 bp")
        self._sc_gt2k   = self._card("> 2000 bp")
        for c in (self._sc_lt500, self._sc_500_2k, self._sc_gt2k):
            r3.addWidget(c)
        r3.addStretch(2)
        sl.addLayout(r3)

        sl.addWidget(_sec("Base quality (Phred)"))
        r4 = QtWidgets.QHBoxLayout()
        r4.setSpacing(8)
        self._sc_meanq = self._card("Mean Q-score", color=GREEN)
        self._sc_q10   = self._card("Reads Q≥10")
        self._sc_q15   = self._card("Reads Q≥15")
        self._sc_q20   = self._card("Reads Q≥20")
        for c in (self._sc_meanq, self._sc_q10, self._sc_q15, self._sc_q20):
            r4.addWidget(c)
        r4.addStretch()
        sl.addLayout(r4)

        sl.addWidget(_sec("GC content"))
        r5 = QtWidgets.QHBoxLayout()
        r5.setSpacing(8)
        self._sc_meangc = self._card("Mean %GC")
        self._sc_stdgc  = self._card("Std dev %GC")
        for c in (self._sc_meangc, self._sc_stdgc):
            r5.addWidget(c)
        r5.addStretch(3)
        sl.addLayout(r5)

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
        self._file_path   = ""
        self._file_size   = 0
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
        for sig in (w.progress, w.finished, w.error):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
        if w.isRunning():
            w.stop()
            self._retired_workers.add(w)

    # ── slots ─────────────────────────────────────────────────────────────

    def _on_file(self, path: str):
        # Cancel any running analysis — new file supersedes the old one
        self._retire_worker()
        self._file_path = path
        self._set_analyze_enabled(bool(path))
        self._status_lbl.setStyleSheet("")
        self._status_lbl.setText("")
        self._stats_w.hide()
        self._charts_w.hide()
        self._pdf_btn.hide()
        self._pdf_btn.setText("Export PDF  ↓")
        self._pdf_ready = False

    def _clear(self):
        self._retire_worker()
        self._drop.clear()
        self._file_path   = ""
        self._file_size   = 0
        self._last_result = None
        self._last_pdf    = ""
        self._pdf_ready   = False
        self._status_lbl.setStyleSheet("")
        self._status_lbl.setText("")
        self._progress_bar.hide()
        self._stats_w.hide()
        self._charts_w.hide()
        self._pdf_btn.setText("Export PDF  ↓")
        self._pdf_btn.hide()
        self._set_analyze_enabled(False)

    def _start(self):
        if not self._file_path or not os.path.isfile(self._file_path):
            return
        # Stop any previous worker before starting a new one
        self._retire_worker()
        self._file_size = os.path.getsize(self._file_path)
        self._set_analyze_enabled(False)
        self._status_lbl.setStyleSheet("")
        self._status_lbl.setText("Analyzing…  reading all reads")
        self._progress_bar.show()
        self._stats_w.hide()
        self._charts_w.hide()
        self._pdf_btn.hide()

        self._worker = _FastqInspectorWorker(self._file_path)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_progress(self, n: int):
        self._status_lbl.setText(f"Analyzing…  {n:,} reads processed")

    def _on_finished(self, r: dict):
        self._progress_bar.hide()
        self._last_result = r
        n = r["n_reads"]
        self._status_lbl.setText(f"Done — {n:,} reads analyzed")

        def _fmt_bp(v):
            if v >= 1_000_000_000: return f"{v/1_000_000_000:.2f} Gbp"
            if v >= 1_000_000:     return f"{v/1_000_000:.1f} Mbp"
            if v >= 1_000:         return f"{v/1_000:.1f} Kbp"
            return f"{v} bp"

        def _fmt_size(b):
            if b >= 1_073_741_824: return f"{b/1_073_741_824:.2f} GB"
            if b >= 1_048_576:     return f"{b/1_048_576:.1f} MB"
            return f"{b/1_024:.1f} KB"

        self._sc_reads._val.setText(f"{n:,}")
        self._sc_bases._val.setText(_fmt_bp(r["total_bases"]))
        self._sc_filesize._val.setText(_fmt_size(self._file_size))

        self._sc_min._val.setText(f"{r['min_len']:,} bp")
        self._sc_max._val.setText(f"{r['max_len']:,} bp")
        self._sc_mean._val.setText(f"{r['mean_len']:,.0f} bp")
        self._sc_median._val.setText(f"{r['median_len']:,.0f} bp")
        self._sc_n50._val.setText(f"{r['n50']:,} bp")

        self._sc_lt500._val.setText(f"{r['len_lt500']:,}  ({r['len_lt500']/n*100:.1f}%)")
        self._sc_500_2k._val.setText(f"{r['len_500_2k']:,}  ({r['len_500_2k']/n*100:.1f}%)")
        self._sc_gt2k._val.setText(f"{r['len_gt2k']:,}  ({r['len_gt2k']/n*100:.1f}%)")

        self._sc_meanq._val.setText(f"Q{r['mean_q']:.1f}")
        self._sc_q10._val.setText(f"{r['q10_pct']:.1f}%")
        self._sc_q15._val.setText(f"{r['q15_pct']:.1f}%")
        self._sc_q20._val.setText(f"{r['q20_pct']:.1f}%")

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

    def _on_error(self, msg: str):
        self._progress_bar.hide()
        self._set_analyze_enabled(True)
        self._status_lbl.setStyleSheet(f"color:{RED};")
        self._status_lbl.setText(f"Error: {msg}")

    # ── PDF export ────────────────────────────────────────────────────────

    def _on_pdf_clicked(self):
        if self._pdf_ready and self._last_pdf:
            os.startfile(self._last_pdf)
        else:
            self._export_pdf()

    def _export_pdf(self):
        if not self._last_result:
            return

        ts          = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        folder_name = f"ont-barcoder_{ts}_fastq_ins"
        fastq_stem  = os.path.splitext(os.path.basename(self._file_path))[0]
        # strip .fastq.gz double extension if present
        if fastq_stem.endswith(".fastq"):
            fastq_stem = fastq_stem[:-6]
        pdf_name    = f"{fastq_stem}_stats.pdf"
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
        fname = os.path.basename(self._file_path)
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
        /* Header — fondo sólido + texto blanco explícito para Qt (sin gradientes ni márgenes negativos) */
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
        /* Tarjetas de métricas */
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
        /* Secciones */
        .section { margin: 15px 0; }
        /* Título dentro del thead de la tabla — page-break-inside:avoid en la tabla
           es la única forma fiable de mantener título y datos juntos en QTextDocument */
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
        /* Título suelto (solo sección de gráficas) */
        .section-title {
            font-size: 14pt;
            font-weight: 600;
            color: #2C5282;
            border-bottom: 2px solid #CBD5E0;
            padding-bottom: 6px;
            margin: 0 0 10px 0;
        }
        .section-title i { font-weight: 400; color: #718096; }
        /* Tablas modernas */
        .data-table {
            /* QTextDocument no respeta margin:auto (empuja la tabla a la derecha).
               Se centra con margen izquierdo EXPLÍCITO:
                 width        = ancho de la tabla
                 margin-left  = hueco a la izquierda (≈ (100-width)/2 para centrar;
                                con 80% → ~70px en Letter).
               Sube margin-left para moverla a la derecha, bájalo para acercarla
               al borde izquierdo. */
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
        /* Notas bajo las gráficas */
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
        /* Badges de calidad */
        .badge-good { background: #C6F6D5; color: #22543D; padding: 2px 8px; border-radius: 20px; font-size: 8pt; font-weight: 600; }
        .badge-warning { background: #FEEBC8; color: #7B341E; padding: 2px 8px; border-radius: 20px; font-size: 8pt; font-weight: 600; }
        .badge-info { background: #BEE3F8; color: #1A365D; padding: 2px 8px; border-radius: 20px; font-size: 8pt; font-weight: 600; }
        """

        # Calcular indicadores de calidad
        q_mean = r['mean_q']
        q_badge = 'badge-good' if q_mean >= 20 else ('badge-warning' if q_mean >= 15 else 'badge-info')
        q_badge_text = 'Excellent' if q_mean >= 20 else ('Good' if q_mean >= 15 else 'Fair')

        # Tarjetas de métricas
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

        # Tabla de longitud — título dentro de thead para que page-break-inside:avoid
        # en la tabla mantenga título y filas juntos (QTextDocument no respeta el
        # page-break en <div>, pero sí en elementos <table>)
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
                <tr><td>500–2000 bp</td><td class="value">{r['len_500_2k']:,}</td><td>({r['len_500_2k']/n*100:.1f}%)</td></tr>
                <tr><td>&gt; 2000 bp</td><td class="value">{r['len_gt2k']:,}</td><td>({r['len_gt2k']/n*100:.1f}%)</td></tr>
            </tbody>
        </table>
        """

        # Tabla de calidad
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
            </tbody>
        </table>
        """

        # Tabla de GC
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

        # Las dos gráficas de cada hoja van en UNA sola <table> y la celda de cada
        # imagen lleva line-height:100% (ver _img_row). Sin eso, el line-height:1.5
        # del body inflaba cada imagen ~1.5× y solo entraba una gráfica por hoja.
        # Resultado: dos gráficas por hoja.
        _cw = self._PDF_CHART_W   # PNG ya embebido a este tamaño exacto
        _ch = self._PDF_CHART_H

        def _title_row(title, top_border=False):
            bt = "border-top:1px solid #FBFBFB;" if top_border else ""
            return (
                f'<tr><td style="padding:5px 8px; font-size:10pt; font-weight:600;'
                f' color:#4A5568; border-bottom:1px solid #E2E8F0;{bt}">{title}</td></tr>'
            )

        # ▼▼ Espacio (px) DEBAJO de cada gráfica, antes del título de la siguiente.
        #    Sube/baja este número para ajustar ese hueco. Ojo: si lo subes mucho,
        #    la 2ª gráfica podría no caber en la hoja.
        _gap_below_chart = 40

        def _img_row(b64):
            # line-height:100% es clave: QTextDocument aplica el line-height (1.5
            # heredado del body) de forma MULTIPLICATIVA a la línea que contiene la
            # imagen, así que cada gráfica de 280px ocupaba ~420px y sobraban ~140px
            # de hueco debajo (lo que empujaba la 2ª gráfica a otra hoja). Con 100%
            # la línea mide exactamente la altura de la imagen.
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
        💾 <strong>File size:</strong> {_fmt_size(self._file_size)}
    </div>

    {metrics}
    {length_table}
    {quality_table}
    {gc_table}
    {charts}

    </body></html>"""
