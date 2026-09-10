#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
pipeline.py - worker pipeline (was ONTbarcoder3_multiprocessing.py)
==================================================================
Demultiplexing, consensus calling and barcode-comparison workers run by
the process Pool of ONTbarcoder3.py.

Modifications to guarantee deterministic results identical
to the original ONTbarcoder2.py, while keeping the
parallelization for maximum performance.

NOTE: this module must never be named after a standard-library module
(multiprocessing, queue, types...): _utilities is placed at the front of
sys.path, so such a name would shadow the real one and break the Pool.
"""

# ============================================================
# FULL IMPORTS
# ============================================================
import sys
import os
import re
import atexit
import time
import math
import hashlib
import fnmatch
import shutil
import tempfile
import threading
import multiprocessing
import xlsxwriter
import fileinput
import itertools
import subprocess
from collections import Counter, defaultdict
from itertools import zip_longest, combinations
import edlib
from Bio.Seq import Seq
from PyQt5 import QtCore

# Error probability per quality ASCII value (Phred+33). Used for the
# optional per-read quality filter: the mean quality of an ONT read is calculated
# as -10*log10(mean of error probabilities), not as the arithmetic mean of Q.
_PHRED_ERR = [10.0 ** (-(q - 33) / 10.0) for q in range(256)]
# Module-level bound access: allows summing the error probabilities
# by iterating the bytes of the quality line with map()/sum() at C speed,
# ~8x faster than a Python loop with ord() per character.
_PHRED_ERR_GET = _PHRED_ERR.__getitem__

# ============================================================
# HELPER FUNCTIONS FOR DETERMINISM
# ============================================================

def deterministic_sort(items):
    """
    Sorts any collection deterministically.
    Guarantees that the order is reproducible across runs.
    """
    if not items:
        return items
    # Convert to string for consistent sorting
    return sorted(items, key=lambda x: str(x) if x is not None else "")



def resolve_ties_by_name(items):
    """
    Resolves ties in lists of (item, score) tuples by sorting
    alphabetically by item name when scores are equal.
    """
    if not items or len(items) <= 1:
        return items

    result = []
    last_score = None
    group = []

    for item, score in items:
        if score != last_score:
            if group:
                # Sort the group alphabetically by name
                group.sort(key=lambda x: str(x[0]))
                result.extend(group)
                group = []
            last_score = score
        group.append((item, score))

    if group:
        group.sort(key=lambda x: str(x[0]))
        result.extend(group)

    return result


# ============================================================
# GLOBAL CONSTANTS
# ============================================================

AMBIGUITY_CODES = [
    ("R", "A"), ("R", "G"), ("M", "A"), ("M", "C"), ("S", "C"), ("S", "G"),
    ("Y", "C"), ("Y", "T"), ("K", "G"), ("K", "T"), ("W", "A"), ("W", "T"),
    ("V", "A"), ("V", "C"), ("V", "G"), ("H", "A"), ("H", "C"), ("H", "T"),
    ("D", "A"), ("D", "G"), ("D", "T"), ("B", "C"), ("B", "G"), ("B", "T"),
    ("N", "A"), ("N", "G"), ("N", "C"), ("N", "T"),
]

# Path configuration
def _get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # Go up one level from _utilities/ to the project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT_DIR = _get_base_dir()
parfilepath = os.path.join(SCRIPT_DIR, "_mafftfiles", "parfile")
# The bundled binary is "disttbfast.exe" on Windows and "disttbfast" (no
# extension) on Linux — see build_linux.sh, which ships the Linux binary
# under that name.
_DISTTBFAST_NAME = "disttbfast.exe" if sys.platform == "win32" else "disttbfast"
disttbpath  = os.path.join(SCRIPT_DIR, "_mafftfiles", _DISTTBFAST_NAME)
MAFFT_DIR   = os.path.join(SCRIPT_DIR, "_mafftfiles")

# Number of threads (can be overridden by the GUI)
N_THREADS = 4

# On Windows, shell=True creates cmd.exe as an intermediary.
# With 20+ concurrent workers, 20 cmd.exe processes fighting over the Windows console host
# stalls the OS. CREATE_NO_WINDOW + shell=False removes that intermediary.
_CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def set_threads(n):
    """Updates N_THREADS."""
    global N_THREADS
    N_THREADS = max(1, n)


_PHYSICAL_CORES = None

# Reusable working directory PER THREAD.
# disttbfast drops fixed-name files (conflicts, pre, trace, order) into its cwd.
# Instead of mkdtemp+copy(_aamtx)+rmtree on every call, the dir is created once on
# the thread's first use, and only those files are cleaned up between invocations.
#
# IMPORTANT: runconsensusparts/runtoptwenty run the workers with a ThreadPool
# (several threads in the SAME process), not with separate processes. A single
# shared global directory would cause a race condition: one thread's
# _reset_worker_tmpdir would delete the fixed-name files another thread is using
# in that same cwd. That's why the directory is thread-local: each thread works
# in its own isolated cwd.
_worker_tls = threading.local()

# Global (thread-safe) registry of ALL temp directories created, so they can be
# deleted later. Since the dir is thread-local, each thread's TLS entry can't be
# reached from outside; this registry allows bulk cleanup at the end of a
# pipeline (cleanup_worker_tmpdirs) and, as a safety net, on exit (atexit).
_worker_dirs = []
_worker_dirs_lock = threading.Lock()


def _get_worker_tmpdir():
    """Creates (or retrieves) THIS thread's isolated temp directory."""
    work_dir = getattr(_worker_tls, "dir", None)
    if work_dir is None or not os.path.isdir(work_dir):
        work_dir = tempfile.mkdtemp(prefix="ontbc_mafft_")
        aamtx_src = os.path.join(MAFFT_DIR, "_aamtx")
        if os.path.exists(aamtx_src):
            shutil.copy2(aamtx_src, work_dir)
        _worker_tls.dir = work_dir
        with _worker_dirs_lock:
            _worker_dirs.append(work_dir)
    return work_dir


def cleanup_worker_tmpdirs():
    """Deletes all MAFFT worker temp directories created so far.

    Safe to call between phases/pipelines: each consensus phase opens a ThreadPool
    with new threads (fresh TLS), so previous phases' directories are no longer in
    use. Also registered with atexit as a safety net when the app closes.
    After cleanup, the next thread that calls _get_worker_tmpdir will create a new one.
    """
    with _worker_dirs_lock:
        for d in _worker_dirs:
            shutil.rmtree(d, ignore_errors=True)
        _worker_dirs.clear()
    # Forget the current thread's TLS reference (its dir was already deleted); any
    # other thread revalidates with os.path.isdir() in _get_worker_tmpdir and recreates its own.
    if getattr(_worker_tls, "dir", None) is not None:
        _worker_tls.dir = None


atexit.register(cleanup_worker_tmpdirs)


def _reset_worker_tmpdir(work_dir):
    """
    Removes all files from the working directory except _aamtx.
    Equivalent to creating a fresh directory from disttbfast's point of view.
    """
    try:
        for entry in os.scandir(work_dir):
            if entry.name == "_aamtx":
                continue
            try:
                if entry.is_file(follow_symlinks=False) or entry.is_symlink():
                    os.unlink(entry.path)
                else:
                    shutil.rmtree(entry.path, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def physical_core_count():
    """
    PHYSICAL core count (best-effort, cached). The heavy work is disttbfast.exe
    processes forced to 1 thread (-C 1-1); their optimal concurrency ≈ physical
    cores. Exceeding it oversubscribes the CPU and slows things down (e.g. 20
    workers on 16 physical / 24 logical cores performs worse than 12). On Windows,
    the GetLogicalProcessorInformation API is queried; if it fails, falls back to
    half the logical cores (a conservative heuristic for hyperthreaded CPUs).
    """
    global _PHYSICAL_CORES
    if _PHYSICAL_CORES is not None:
        return _PHYSICAL_CORES
    logical = os.cpu_count() or 2
    result = max(1, logical // 2)
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes

            class _SLPI(ctypes.Structure):
                _fields_ = [("ProcessorMask", ctypes.c_size_t),
                            ("Relationship", ctypes.c_uint32),
                            ("Union", ctypes.c_ulonglong * 2)]

            glpi = ctypes.windll.kernel32.GetLogicalProcessorInformation
            length = wintypes.DWORD(0)
            glpi(None, ctypes.byref(length))  # 1st call: gets the size
            n = length.value // ctypes.sizeof(_SLPI)
            if n > 0:
                buf = (_SLPI * n)()
                if glpi(buf, ctypes.byref(length)):
                    phys = sum(1 for x in buf if x.Relationship == 0)  # RelationProcessorCore
                    if phys >= 1:
                        result = phys
        except Exception:
            pass
    _PHYSICAL_CORES = result
    return result


def optimal_worker_count(requested=None):
    """Recommended concurrency: never above the physical core count.
    With no argument, returns the optimum; with `requested`, caps it at that limit."""
    phys = physical_core_count()
    if requested is None:
        return phys
    return max(1, min(int(requested), phys))


def _run_disttbfast(cmd_args, timeout=120):
    """
    Runs disttbfast.exe in this worker's working directory.
    disttbfast creates fixed-name files (conflicts, pre, trace, order) in its
    cwd. _reset_worker_tmpdir deletes them before each invocation, guaranteeing a
    clean cwd without the cost of mkdtemp+copy(_aamtx)+rmtree on every call.
    Retries on transient failures; TimeoutExpired propagates without retrying.
    """
    work_dir = _get_worker_tmpdir()
    last_exc = None
    for attempt in range(3):
        _reset_worker_tmpdir(work_dir)
        try:
            return subprocess.check_output(
                cmd_args, shell=False,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
                cwd=work_dir,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            raise
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))  # 50 ms, 100 ms
    raise last_exc


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def revcomp(seq: str) -> str:
    """Computes the reverse complement of a DNA sequence."""
    seq = seq.upper()
    comp_table = str.maketrans(
        "ACGTRYMKSWBDHVN",
        "TGCAYRKMWSVHDBN"
    )
    return seq.translate(comp_table)[::-1]


def resource_path(relative_path):
    """Gets the correct path for resources (PyInstaller-compatible)."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


# ============================================================
# CLASS: prepdemultiplex
# ============================================================

class prepdemultiplex(QtCore.QThread):
    taskFinished = QtCore.pyqtSignal(int)
    notifyProgress = QtCore.pyqtSignal(int)
    notifyMessage = QtCore.pyqtSignal(str)

    def __init__(self, demfile, infastq, outdir, minlen, explen, demlen, logfile,
                 start_id=1, minq=0, tagmm=2, parent=None):
        super(prepdemultiplex, self).__init__(parent)
        self.demfile = demfile
        self.infastq = infastq
        self.outdir = outdir
        self.minlen = minlen
        self.explen = explen
        self.demlen = demlen
        self.logfile = logfile
        self.start_id = start_id
        # Optional per-read mean quality (Q) filter. 0 = disabled.
        self.minq = minq
        # Number of errors (substitution/indel) tolerated in tags when demultiplexing:
        # generates tag mutants up to this depth (0 = exact match only).
        self.tagmm = tagmm

    def run(self):
        def crmutant_m2(tfile, nbp):
            tagdict = {}
            sampledict = {}
            with open(tfile) as tagfile:
                t = tagfile.readlines()
                for each in t:
                    tagdict[each.split(',')[1]] = ''
                    tagdict[each.split(',')[2]] = ''
            counter = 1
            typedict = {}
            for each in tagdict.keys():
                tagdict[each] = "t" + str(counter)
                typedict[each] = 0
                counter += 1
            with open(tfile) as tagfile:
                t = tagfile.readlines()
                for each in t:
                    sampledict[(tagdict[each.split(',')[1]], tagdict[each.split(',')[2]])] = each.split(',')[0]
            n = 1
            muttags_fr = {}
            # Frontier BFS: each round expands only the NEWLY added tags from the
            # previous round, not the entire tagdict. Combined with early-skip of
            # already-resolved entries, this reduces dict operations by ~90% per round.
            frontier = dict(tagdict)
            while n <= nbp:
                muttags_fr, newtags_fr = create_all_mutants(frontier, tagdict)
                with open(os.path.join(self.outdir, "conflicts"), 'w') as conflictfile:
                    for k in list(newtags_fr.keys()):
                        if len(newtags_fr[k]) > 1:
                            conflictfile.write(k + '\t' + ",".join(newtags_fr[k]) + '\n')
                            del muttags_fr[k]
                # early-skip guarantees muttags_fr keys are not in tagdict already
                new_frontier = {}
                for k, v in muttags_fr.items():
                    tagdict[k] = v
                    typedict[k] = n
                    new_frontier[k] = v
                frontier = new_frontier
                n += 1
            with open(self.outdir + "/temp1.fas", 'w') as outfile:
                for k in tagdict.keys():
                    outfile.write(k + '\t' + tagdict[k] + '\n')
            return tagdict, muttags_fr, sampledict, typedict

        def create_all_mutants(frontier, tagdict):
            muttags, newtags = {}, {}
            for tag in frontier.keys():
                orig = frontier[tag]
                mutantset = set(createsubmutant(tag) + createdelmutant(tag) + createinsmutant(tag))
                mutantset.discard(tag)
                for mutant in mutantset:
                    # Skip entries already resolved in a previous round — they keep
                    # their existing (closer-distance) assignment.
                    if mutant in tagdict:
                        continue
                    if mutant in newtags:
                        if orig not in newtags[mutant]:
                            newtags[mutant].append(orig)
                    else:
                        newtags[mutant] = [orig]
                    muttags[mutant] = orig
            return muttags, newtags

        def createsubmutant(tag):
            newlist = []
            bpset = ["A", "T", "G", "C"]
            for i, v in enumerate(tag):
                for bp in [x for x in bpset if x != v]:
                    if i == 0:
                        newlist.append(bp + tag[i+1:])
                    if i > 0 and i < len(tag)-1:
                        newlist.append(tag[0:i] + bp + tag[i+1:])
                    if i == len(tag)-1:
                        newlist.append(tag[0:i] + bp)
            return newlist

        def createdelmutant(tag):
            newlist = []
            bpset = ["A", "T", "G", "C"]
            for i, v in enumerate(tag):
                for bp in bpset:
                    if i > 0 and i < len(tag)-1:
                        newlist.append(bp + tag[0:i] + tag[i+1:])
                    if i == len(tag)-1:
                        newlist.append(bp + tag[0:i])
            return newlist

        def createinsmutant(tag):
            newlist = []
            bpset = ["A", "T", "G", "C"]
            for i, v in enumerate(tag):
                for bp in bpset:
                    if i > 0 and i < len(tag)-1:
                        newlist.append(tag[1:i] + bp + tag[i:])
                    if i == len(tag)-1:
                        newlist.append(tag[1:] + bp)
            return newlist

        basename = os.path.basename(self.infastq)
        self.sampleids = {}
        primerlensum = 0
        with open(self.demfile) as demulfile:
            demullines = demulfile.readlines()
            for each in demullines:
                self.sampleids[each.split(',')[0]] = ''
            first_cols = [c.strip() for c in demullines[0].rstrip('\n').split(',')]
            self.taglen = len(first_cols[1])
            # Primer pairs from column 3: (primer_f, primer_r), (primer_f2, primer_r2)...
            self.primerfset = []
            self.primerrset = []
            i = 3
            while i + 1 < len(first_cols):
                if first_cols[i]:
                    self.primerfset.append(first_cols[i])
                if first_cols[i + 1]:
                    self.primerrset.append(first_cols[i + 1])
                i += 2
            avg_flen = sum(len(p) for p in self.primerfset) / len(self.primerfset) if self.primerfset else 0
            avg_rlen = sum(len(p) for p in self.primerrset) / len(self.primerrset) if self.primerrset else 0
            primerlensum += int(round(avg_flen + avg_rlen))

        self.nseqspasslen = 0
        self.totalseqs = 0
        self.nseqsfailqual = 0
        self.logfile.write("<br><br>Read demultiplexing file. There are " + str("{:,}".format(len(self.sampleids))) + " in your experiment.\n")

        # ── SINGLE PASS: the three original phases (filter → split → chunk)
        # are merged into a single FASTQ traversal to avoid three sequential
        # disk reads. Reads are classified and buffered in memory in batches
        # of CHUNK_LINES lines before writing, reducing write() calls without
        # significantly increasing RAM usage.
        # ────────────────────────────────────────────────────────────────────
        self.nseqsfordemultiplexingpresplit1 = 0
        self.nseqsfordemultiplexingpresplit2 = 0
        self.nwronglengthwindow = 0
        self.nseqsfordemultiplexing = 0
        self.maxid = 0

        CHUNK_LINES = 80000          # 40,000 reads × 2 lines per read (id + seq)
        SPLIT_CHUNK = 40000          # lines per partition file (same as before)

        # In-memory buffers for the two main destinations
        buf_1pdt = []                # reads of correct length (one barcode)
        buf_2pdt = []                # double-length reads (two ligated barcodes)
        buf_write_threshold = 4096   # lines accumulated before a partial flush

        # Chunk counters for the partitioned files
        chunk_1pdt_lines = 0
        chunk_1pdt_idx   = 1
        chunk_2pdt_lines = 0
        chunk_2pdt_idx   = 1

        # Length limits computed once
        len_1pdt_max = self.explen + self.taglen * 2 + primerlensum + self.demlen
        len_2pdt_min = (self.explen + self.taglen * 2 + primerlensum) * 2 - self.demlen
        len_2pdt_max = (self.explen + self.taglen * 2 + primerlensum + self.demlen) * 2

        n = self.start_id

        # Archivos de salida de chunk abiertos de forma lazy
        _open_chunks = {}   # key → file handle

        def _chunk_path(prefix, idx):
            return os.path.join(self.outdir, f"{basename}_{prefix}_p{idx * SPLIT_CHUNK}")

        def _get_chunk(prefix, idx):
            key = (prefix, idx)
            if key not in _open_chunks:
                _open_chunks[key] = open(_chunk_path(prefix, idx), 'w', buffering=65536)
            return _open_chunks[key]

        def _flush_chunk(prefix, lines_buf, idx_ref, lines_ref):
            """Writes lines to the current chunk; opens the next one if it fills up."""
            fh = _get_chunk(prefix, idx_ref[0])
            pos = 0
            while pos < len(lines_buf):
                space = SPLIT_CHUNK - lines_ref[0]
                batch = lines_buf[pos: pos + space]
                fh.writelines(batch)
                lines_ref[0] += len(batch)
                pos += len(batch)
                if lines_ref[0] >= SPLIT_CHUNK and pos < len(lines_buf):
                    # Only open the next chunk if there are still lines left to write
                    fh.flush()
                    idx_ref[0] += 1
                    lines_ref[0] = 0
                    fh = _get_chunk(prefix, idx_ref[0])

        idx1_ref   = [chunk_1pdt_idx]
        lines1_ref = [chunk_1pdt_lines]
        idx2_ref   = [chunk_2pdt_idx]
        lines2_ref = [chunk_2pdt_lines]

        # Read the FASTQ with a large I/O buffer (8 MB) to minimize
        # system calls on multi-GB files.
        with open(self.infastq, buffering=8 * 1024 * 1024) as infile:
            for line1, line2, line3, line4 in zip_longest(*[infile] * 4):
                if not line1 or not line1.strip():
                    continue
                sequence = line2.strip() if line2 else ""
                slen = len(sequence)

                if slen <= self.minlen:
                    n += 1
                    self.totalseqs += 1
                    continue

                # Optional quality filter: discards reads whose mean ONT quality
                # (-10*log10(mean error probability)) falls below minq.
                # Iterates over the quality line's bytes with sum()/map()
                # (C speed) to minimize overhead on large files.
                if self.minq > 0 and line4:
                    qb = line4.strip().encode()
                    if qb:
                        mean_err = sum(map(_PHRED_ERR_GET, qb)) / len(qb)
                        mean_q = -10.0 * math.log10(mean_err) if mean_err > 0 else 99.0
                        if mean_q < self.minq:
                            n += 1
                            self.totalseqs += 1
                            self.nseqsfailqual += 1
                            continue

                seqid = ">" + str(n) + "\n"
                self.nseqspasslen += 1
                n += 1
                self.totalseqs += 1

                if slen < len_1pdt_max:
                    # Simple product (one barcode)
                    buf_1pdt.append(seqid)
                    buf_1pdt.append(line2 if line2.endswith("\n") else line2 + "\n")
                    self.nseqsfordemultiplexing += 1
                    self.nseqsfordemultiplexingpresplit1 += 1

                elif len_2pdt_min < slen < len_2pdt_max:
                    # Double product (two ligated barcodes): split into two
                    cut = self.explen + self.taglen * 2 + primerlensum + self.demlen
                    ovlp = self.explen + self.taglen * 2 + primerlensum - self.demlen
                    buf_2pdt.append(seqid.strip() + "p1\n")
                    buf_2pdt.append(sequence[:cut] + "\n")
                    buf_2pdt.append(seqid.strip() + "p2\n")
                    buf_2pdt.append(sequence[ovlp:] + "\n")
                    self.nseqsfordemultiplexing += 2
                    self.nseqsfordemultiplexingpresplit2 += 1

                else:
                    self.nwronglengthwindow += 1

                # Partial flush when the buffers grow too large
                if len(buf_1pdt) >= buf_write_threshold:
                    _flush_chunk("reformat_out_1pdt", buf_1pdt, idx1_ref, lines1_ref)
                    buf_1pdt = []
                if len(buf_2pdt) >= buf_write_threshold:
                    _flush_chunk("reformat_out_2pdt", buf_2pdt, idx2_ref, lines2_ref)
                    buf_2pdt = []

        # Final flush of the remaining buffers
        if buf_1pdt:
            _flush_chunk("reformat_out_1pdt", buf_1pdt, idx1_ref, lines1_ref)
        if buf_2pdt:
            _flush_chunk("reformat_out_2pdt", buf_2pdt, idx2_ref, lines2_ref)

        # Close all open file handles
        for fh in _open_chunks.values():
            try:
                fh.close()
            except Exception:
                pass

        # maxid = name of the last _1pdt chunk written.
        # If lines1_ref[0] > 0: the last active chunk is idx1_ref[0].
        # If lines1_ref[0] == 0: the last full chunk was idx1_ref[0] (it may have
        #   landed exactly on SPLIT_CHUNK without opening a new one).
        last_1pdt_idx = idx1_ref[0]
        self.maxid = f"{basename}_reformat_out_1pdt_p{last_1pdt_idx * SPLIT_CHUNK}"

        self.notifyMessage.emit("<br>Your raw file contains " + str("{:,}".format(self.totalseqs)) + " reads.<br>You have " + str("{:,}".format(self.nseqspasslen)) + " reads passing the length filter. <br> Of these, " + str("{:,}".format(self.nseqsfordemultiplexingpresplit1)) + " reads are of the correct length for containing one barcode, while " + str("{:,}".format(self.nseqsfordemultiplexingpresplit2)) + " may contain two ligated  barcodes<br>Total reads of expected length =" + str("{:,}".format(self.nseqsfordemultiplexingpresplit1+self.nseqsfordemultiplexingpresplit2)) + "<br>After splitting the long products, a total of " + str("{:,}".format(self.nseqsfordemultiplexing)) + " reads are used for demultiplexing.")
        if self.minq > 0:
            self.notifyMessage.emit(
                "Quality filter Q≥" + str(self.minq) + ": removed "
                + "{:,}".format(self.nseqsfailqual)
                + " reads below the mean-quality threshold.")
            self.logfile.write(
                str(round(time.time())) + ": Quality filter Q>=" + str(self.minq)
                + " removed " + "{:,}".format(self.nseqsfailqual) + " reads.\n")
        self.logfile.write(str(round(time.time())) + ": Split files into correct product length. There are " + str("{:,}".format(self.nseqspasslen)) + " single product sequences.\n")
        # Reads in the last _1pdt chunk. Each chunk has SPLIT_CHUNK lines
        # (= SPLIT_CHUNK/2 reads, 2 lines per read). lines1_ref[0] is the number of
        # lines written in the current (last) chunk; it equals SPLIT_CHUNK if it
        # filled exactly. rundemultiplex adds maxv (this value) for the max chunk
        # and SPLIT_CHUNK//2 for the rest, so maxv must be in READS — not in
        # lines nor over nseqspasslen (which includes 2pdt and the wrong-length window).
        _last_1pdt_lines = lines1_ref[0] if lines1_ref[0] > 0 else SPLIT_CHUNK
        self.lastbitn = _last_1pdt_lines // 2

        self.tagdict, self.muttags_fr, self.sampledict, self.typedict = crmutant_m2(self.demfile, self.tagmm)

        self.taskFinished.emit(0)


# ============================================================
# CLASS: mergedemfiles
# ============================================================

class mergedemfiles(QtCore.QThread):
    taskFinished = QtCore.pyqtSignal(int)
    notifyProgress = QtCore.pyqtSignal(int)

    def __init__(self, sampleids, indir, outdir, n_chunks=4, parent=None):
        super(mergedemfiles, self).__init__(parent)
        self.sampleids = sampleids
        self.indir = indir
        self.outdir = outdir
        self.n_chunks = n_chunks

    def run(self):
        def mergefiles(inlist, outfilename):
            nseqs = 0
            with open(outfilename, 'wb') as outf:
                for each in inlist:
                    with open(each, 'rb') as inf:
                        data = inf.read()
                    nseqs += data.count(b'>')
                    outf.write(data)
            return nseqs

        c = 1
        # Sort keys for determinism
        sample_keys = deterministic_sort(list(self.sampleids.keys()))

        for each in sample_keys:
            eachlist = []
            for n in range(self.n_chunks):
                candidate = os.path.join(self.indir, str(n), each + "_all.fa")
                if os.path.isfile(candidate):
                    eachlist.append(candidate)
            if eachlist:
                nseqs = mergefiles(eachlist, os.path.join(self.outdir, each + "_all.fa"))
                self.sampleids[each] = nseqs
            self.notifyProgress.emit(c)
            c += 1
        
        shutil.rmtree(self.indir)
        self.taskFinished.emit(0)


# ============================================================
# CLASS: calculatecoverage
# ============================================================

class calculatecoverage(QtCore.QThread):
    taskFinished = QtCore.pyqtSignal(int)
    notifyProgress = QtCore.pyqtSignal(int)

    def __init__(self, indir, parent=None):
        super(calculatecoverage, self).__init__(parent)
        self.indir = indir

    def run(self):
        dirlist = deterministic_sort(os.listdir(self.indir))
        self.dirdict = {}
        self.counter = {}
        
        for c, fname in enumerate(dirlist):
            with open(os.path.join(self.indir, fname)) as infile:
                if fname.endswith("_all.fa"):
                    self.dirdict[fname] = fname
                else:
                    self.dirdict[fname.split(".")[0] + "_all.fa"] = fname
                l = infile.readlines()
                for i, j in enumerate(l):
                    if ">" in j:
                        try:
                            self.counter[fname.split("_all.fa")[0]] += 1
                        except KeyError:
                            self.counter[fname.split("_all.fa")[0]] = 1
            self.notifyProgress.emit(c+1)
        
        self.taskFinished.emit(0)


# ============================================================
# FUNCTIONS FOR MULTIPROCESSING (with deterministic ordering)
# ============================================================

# Global variables for progress queues (used by pool_init)
_queue_for_progress = None


def pool_init(queue):
    global _queue_for_progress
    _queue_for_progress = queue


def pool_init1(queue):
    global _queue_for_progress
    _queue_for_progress = queue


def pool_init2(queue):
    global _queue_for_progress
    _queue_for_progress = queue


def _consensus_columns(seqs, perc_thresh):
    """Column-majority consensus (aligned, preserves gaps/Ns) for a
    list of equal-length aligned sequences. Replicates the logic of the
    worker's internal `consensus()` function, so it can be reused in the
    dominant-haplotype resolver."""
    out = []
    n = len(seqs)
    if n == 0:
        return out
    for col in zip(*seqs):
        cnt = Counter(col)
        baseset = {b: c for b, c in cnt.items() if float(c) / n > perc_thresh}
        if not baseset:
            bp = 'N'
        elif len(baseset) == 1:
            bp = next(iter(baseset))
        else:
            baseset.pop('-', None)
            bp = next(iter(baseset)) if len(baseset) == 1 else 'N'
        out.append(bp)
    return out


def _dominant_haplotype(aligned_seqs, minor_thresh=0.2, min_secondary_frac=0.2,
                        tolerance=0.10):
    """
    Intra-sample variant resolver (marker-agnostic) for the MSA-aligned
    reads of ONE sample. From the polymorphic columns, it groups the
    reads into N haplotypes via greedy abundance-based clustering (it does not
    assume only two templates) and returns the consensus of the DOMINANT
    haplotype (the most abundant one) along with the breakdown of ALL clusters.
    This prevents a simple majority from "flipping" with the parameters when
    several co-abundant templates coexist (cross-contamination, paralog/NUMT,
    allelic variants, sample mixture).

    Does not depend on translation or length: it operates only on the columns
    of the alignment, so it works equally for Coding and non-Coding markers.
    Translation (Coding) can be used separately as a tie-break when there are
    co-abundant clusters.

    Parameters:
      minor_thresh: minimum frequency of the second allele for a column to be
                    considered 'polymorphic' (filters out random ONT noise,
                    which typically spreads <~5% per alternative).
      min_secondary_frac: minimum fraction of a cluster for it to be
                    considered a 'real' haplotype (and to flag the sample as 'mixed').
      tolerance:    fraction of the polymorphic columns in which a read may
                    disagree with its cluster's centroid and still belong to it.
                    Absorbs sequencing error / intrinsic variation so as not to
                    fragment a single haplotype into many spurious clusters.

    Returns dict:
      mixed:               bool   (>=2 real clusters)
      dominant_frac:       float  (proportion of the dominant haplotype)
      n_poly:              int    (number of columns distinguishing the haplotypes)
      dominant_consensus:  str|None  (aligned consensus of the chosen one, with gaps)
      secondary_consensus: str|None  (back-compat: top secondary, with gaps)
      n_clusters:          int    (number of real clusters)
      n_noise:             int    (reads in sub-threshold clusters)
      clusters:            list of dicts sorted desc. by size:
                           {rank, size, frac, consensus(aligned), translates(None),
                            role in {dominant, secondary, noise}}
    """
    seqs = list(aligned_seqs)
    n = len(seqs)
    _none = {"mixed": False, "dominant_frac": 1.0, "n_poly": 0,
             "dominant_consensus": None, "secondary_consensus": None,
             "n_clusters": 1, "n_noise": 0, "clusters": []}
    if n < 4:
        return _none
    L = len(seqs[0])

    # 1) Polymorphic columns: those with a second allele (not gap/N) above
    #    minor_thresh. In a single-template sample there are none (or almost none).
    poly_cols = []  # column indices
    for j in range(L):
        cnt = Counter(s[j] for s in seqs)
        cnt.pop('-', None)
        cnt.pop('N', None)
        if len(cnt) < 2:
            continue
        common = cnt.most_common(2)
        total = sum(cnt.values())
        mf = common[1][1] / total if total else 0.0
        if mf >= minor_thresh:
            poly_cols.append(j)
    n_poly = len(poly_cols)
    # Anti-hotspot guard: a single diagnostic column cannot be phased, so a
    # reproducible ONT error hotspot (e.g. next to a homopolymer) could
    # fabricate a "cluster" with correlated errors at just ONE site.
    # At least 2 linked polymorphic columns are required to declare a mixture.
    # 1-SNP conspecific mixtures are deliberately ignored: they are indistinguishable
    # from heteroplasmia/error, and the dominant haplotype still identifies the same species.
    if n_poly < 2:
        return _none

    # 2) Per-read signature = bases at the polymorphic columns. N/'-' = wildcard:
    #    they don't count as a mismatch in the distance (errors/partial coverage).
    _WILD = ('-', 'N')

    def _sig(s):
        return tuple(s[j] for j in poly_cols)

    def _dist(sig, centroid):
        d = 0
        for a, b in zip(sig, centroid):
            if a in _WILD or b in _WILD:
                continue
            if a != b:
                d += 1
        return d

    tol_pos = int(round(max(0.0, float(tolerance)) * n_poly))

    # Unique signatures sorted by (frequency desc, signature) -> deterministic.
    sig_counts = Counter(_sig(s) for s in seqs)
    ordered_sigs = sorted(sig_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    # 3) Greedy abundance-based clustering: the most frequent unassigned signature
    #    becomes a seed; it absorbs every signature at distance <= tol_pos; the
    #    centroid is refined to the per-column majority base (one pass).
    assigned = set()                # signatures already assigned
    cluster_members = []            # list of lists of reads (aligned sequences)
    reads_by_sig = {}
    for s in seqs:
        reads_by_sig.setdefault(_sig(s), []).append(s)

    for seed_sig, _cnt in ordered_sigs:
        if seed_sig in assigned:
            continue
        members_sigs = [sig for sig, _ in ordered_sigs
                        if sig not in assigned and _dist(sig, seed_sig) <= tol_pos]
        if not members_sigs:
            continue
        assigned.update(members_sigs)
        # Refine the centroid and reassign still-free signatures closest to it.
        member_reads = [r for sig in members_sigs for r in reads_by_sig[sig]]
        centroid = tuple(_consensus_columns([_sig(r) for r in member_reads], 0.5))
        extra = [sig for sig, _ in ordered_sigs
                 if sig not in assigned and _dist(sig, centroid) <= tol_pos]
        if extra:
            assigned.update(extra)
            member_reads += [r for sig in extra for r in reads_by_sig[sig]]
        cluster_members.append(member_reads)

    # 4) Sort clusters by size (deterministic tie-break by consensus).
    cluster_members.sort(key=lambda m: (-len(m), ''.join(_consensus_columns(m, 0.5))))

    # Real clusters = fraction >= min_secondary_frac AND absolute size >= 3
    # reads (at low coverage, 2 reads sharing a correlated error are not
    # sufficient evidence of a real haplotype); the rest is noise.
    real, noise_reads = [], []
    for m in cluster_members:
        if len(m) / n >= min_secondary_frac and len(m) >= 3:
            real.append(m)
        else:
            noise_reads.extend(m)
    if not real:                       # nothing clears the threshold: treat it all as one cluster
        real = [cluster_members[0]] if cluster_members else [seqs]
        noise_reads = []

    clusters = []
    for rank, m in enumerate(real):
        clusters.append({
            "rank": rank + 1,
            "size": len(m),
            "frac": len(m) / n,
            "consensus": ''.join(_consensus_columns(m, 0.5)),
            "translates": None,        # filled in by callconsensus in Coding mode
            "role": "dominant" if rank == 0 else "secondary",
            # Aligned reads of the cluster (with gaps). Used locally in
            # callconsensus to dump the per-cluster reads to disk so that
            # secondary variants can be recovered (re-consensus 2a + Phase 3). NOT
            # propagated in the 'mix' dict returned to the main thread (would be heavy).
            "members": m,
        })

    dominant_frac = len(real[0]) / n
    dom_cons = clusters[0]["consensus"] if clusters else None
    sec_cons = clusters[1]["consensus"] if len(clusters) > 1 else None
    return {
        "mixed": len(real) >= 2,
        "dominant_frac": dominant_frac,
        "n_poly": n_poly,
        "dominant_consensus": dom_cons,
        "secondary_consensus": sec_cons,
        "n_clusters": len(real),
        "n_noise": len(noise_reads),
        "clusters": clusters,
    }


def _runconsensusparts_fn(inlist):
    """
    Worker function for partial consensus calling.
    Modified: sorts the input list deterministically.
    """
    # --- DETERMINISTIC PATCH: sort file list ---
    inlist1 = inlist[0]
    if isinstance(inlist1, list):
        inlist1 = deterministic_sort(inlist1)
    # ----------------------------------------------------
    
    outpath = inlist[1]
    indir = inlist[2]
    indir2 = inlist[3]
    subsetval = inlist[4]
    outname = inlist[5]
    num_row = inlist[6]
    plen = inlist[7]
    v = inlist[8]
    outpath2 = inlist[10]
    
    postdemlen = int(inlist[9])
    fixthresh = float(inlist[11])
    rangefreq = inlist[12]
    stepsize = float(inlist[13])
    ingencode = int(inlist[14])
    # PER-SAMPLE genetic code (vertebrates/invertebrates multiplexed in the
    # same FASTQ): {sample_id: NCBI table}. Samples without an entry use the
    # global ``ingencode``. Backwards-compatible (len<=16 -> empty dict).
    gencode_by_sample = inlist[16] if len(inlist) > 16 else {}
    # QC length tolerance (± bp) for coding markers: a consensus is length-
    # eligible when |len(conseq) - plen| <= qclentol. 0 (default) reproduces the
    # classic exact-length rule (right for COI, whose 658 bp are invariant).
    # Translation validation applies unchanged afterwards, so an off-length
    # consensus caused by an indel ERROR still fails (frameshift -> early stop ->
    # short ORF); only legitimate in-frame length variants can pass.
    # Backwards-compatible: absent (len<=17) -> 0.
    try:
        qclentol = int(inlist[17]) if len(inlist) > 17 else 0
    except (TypeError, ValueError):
        qclentol = 0

    def gencode_for(name):
        """Resolves the translation table for a sample from its file name
        (``<sample>_all.fa``), falling back to ``ingencode``.
        The key is the FULL sample name (may contain dots)."""
        sid = name.split("_all.fa")[0]
        return gencode_by_sample.get(sid, ingencode)

    # Mixture/contamination resolution config (dominant haplotype).
    # Marker-agnostic: applies to Coding and non-Coding. Backwards-compatible (len<=15).
    resolve_cfg = inlist[15] if len(inlist) > 15 else {}
    _resolve_on = bool(resolve_cfg.get("enabled"))
    _resolve_minor = float(resolve_cfg.get("minor_thresh", 0.2))
    _resolve_secfrac = float(resolve_cfg.get("min_secondary_frac", 0.2))
    _resolve_tol = float(resolve_cfg.get("tolerance", 0.10))
    # Secondary-variant recovery (2a-consensus + Phase 3): always active
    # when detection is turned on. The reads of each eligible secondary
    # cluster (< _resolve_maxn Ns in its consensus) are saved for reprocessing,
    # capped at _resolve_maxvar per sample (the most abundant ones).
    _resolve_recover = bool(resolve_cfg.get("recover_secondaries", True))
    _resolve_maxvar = int(resolve_cfg.get("max_variants", 3))
    _resolve_maxn = int(resolve_cfg.get("max_variant_Ns", 5))
    # Dominant<->secondary divergence that forces 'needs review'. Below
    # the threshold (~conspecific variation: another individual of the same
    # species, heteroplasmia, alleles) the mixture is informative; above it
    # (heterospecific level) it suggests cross-contamination or sample mixing.
    _resolve_divrev = float(resolve_cfg.get("divergence_review", 0.03))

    def consensus(indict, perc_thresh, abs_thresh):
        seqs = list(indict.values())
        if len(seqs) < abs_thresh:
            return ''
        sequence = []
        for col in zip(*seqs):
            n_col = len(col)
            cnt = Counter(col)
            baseset = {b: c for b, c in cnt.items()
                       if float(c) / n_col > perc_thresh}
            if not baseset:
                bp = 'N'
            elif len(baseset) == 1:
                bp = next(iter(baseset))
            else:
                baseset.pop('-', None)
                bp = next(iter(baseset)) if len(baseset) == 1 else 'N'
            sequence.append(bp)
        return ''.join(sequence)

    def _seq_divergence(a, b):
        """Divergence fraction between two consensuses (gap-free): global edit
        distance / length of the longer one. Robust to differing lengths.
        IUPAC codes (incl. N) count as a match — Ns are consensus
        uncertainty, not biological divergence."""
        a = a.replace("-", "").upper()
        b = b.replace("-", "").upper()
        if not a or not b:
            return 0.0
        try:
            d = edlib.align(a, b, mode="NW", task="distance",
                            additionalEqualities=AMBIGUITY_CODES)["editDistance"]
        except Exception:
            d = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
        return d / float(max(len(a), len(b)))

    def translate_corframe(seq, gencode):
        each = seq
        corframe = get_cor_frame(each.replace("-", ""), gencode)
        if corframe == 0:
            return "0"
        if corframe == 1:
            translation = Seq(each.replace('-', '')).translate(table=gencode, to_stop=True).__str__()
            explen = len(each.replace('-', ''))
        if corframe == 2:
            translation = Seq(each[1:].replace('-', '')).translate(table=gencode, to_stop=True).__str__()
            explen = len(each[1:].replace('-', ''))
        if corframe == 3:
            translation = Seq(each[2:].replace('-', '')).translate(table=gencode, to_stop=True).__str__()
            explen = len(each[2:].replace('-', ''))
        if corframe == 4:
            translation = Seq(each.replace('-', '')).reverse_complement().translate(table=gencode, to_stop=True).__str__()
            explen = len(Seq(each.replace('-', '')).__str__())
        if corframe == 5:
            translation = Seq(each[:-1].replace('-', '')).reverse_complement().translate(table=gencode, to_stop=True).__str__()
            explen = len(Seq(each[:-1].replace('-', '')).__str__())
        if corframe == 6:
            translation = Seq(each[:-2].replace('-', '')).reverse_complement().translate(table=gencode, to_stop=True).__str__()
            explen = len(Seq(each[:-2].replace('-', '')).__str__())

        if len(translation) < int(explen/3):
            return "0"
        elif len(translation) == int(explen/3):
            return "1"
        else:
            return "0"

    def get_cor_frame(seq, gencode):
        s = seq.replace('-', '')
        s_obj = Seq(s)
        rc = s_obj.reverse_complement()
        frames = [s_obj, Seq(s[1:]), Seq(s[2:]), rc, Seq(s[:-1]).reverse_complement(), Seq(s[:-2]).reverse_complement()]
        maxlen, corframe = 0, 0
        for i, f in enumerate(frames):
            n = len(f.translate(table=gencode, to_stop=True))
            if n > maxlen:
                maxlen = n
                corframe = i + 1
        return corframe

    # Minimum coverage of the clean ORF relative to the amplicon for the barcode to
    # be accepted after trimming the gene's terminal stop codon. >=0.95 keeps COI intact
    # (no internal stop -> not trimmed) and full-length CytB (stop at ~97%),
    # while still rejecting NUMTs/pseudogenes (scattered early stops -> short ORF).
    ORF_MIN_COVERAGE = 0.95

    def orf_trim(seq, gencode):
        """Returns (trimmed_seq, n_aa): the longest in-frame ORF WITHOUT internal
        stop codons. If the sequence already translates without an internal stop
        (COI Folmer fragment) it is returned intact. If it contains an internal stop
        followed by a non-coding 3' tail (e.g. an amplicon that spans the gene's
        termination codon and carries part of the following tRNA, like full-length
        CytB) the stop and everything 3' of it is trimmed off, leaving a barcode
        that translates cleanly and is valid for BOLD."""
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
        # No internal stop (at most one partial codon at the end): don't trim, to
        # preserve the conventional barcode length (e.g. COI 658 bp).
        if best_aa * 3 >= len(s) - 2:
            return s, best_aa
        # Internal stop + tail: trim to the clean ORF (removes the stop and the 3' end).
        return best_fs[:best_aa * 3], best_aa

    def parse_aln_fasta(path):
        seqdict = {}
        header = None
        parts = []
        with open(path) as fh:
            for line in fh:
                line = line.rstrip()
                if line.startswith('>'):
                    if header is not None:
                        seqdict[header] = (''.join(parts)
                                          .replace("E", "A").replace("F", "G")
                                          .replace("Q", "C").replace("P", "T"))
                    header = line[1:]
                    parts = []
                elif header is not None:
                    parts.append(line)
        if header is not None:
            seqdict[header] = (''.join(parts)
                              .replace("E", "A").replace("F", "G")
                              .replace("Q", "C").replace("P", "T"))
        return seqdict

    def callconsensus(i, perc_thresh, abs_thresh, name, _seqdict=None):
        seqdict = _seqdict if _seqdict is not None else parse_aln_fasta(i)
        # Genetic code for THIS sample (vert=2 / invert=5 / 0=no-Coding...).
        _gc = gencode_for(name)
        conseq_aln = consensus(seqdict, perc_thresh, abs_thresh)
        conseq = conseq_aln.replace("-", "")
        flag = False
        mix = None
        if conseq:
            coverage = len(seqdict)

            # --- Mixture / contamination resolution (marker-agnostic) ---
            # If the sample has two co-abundant haplotypes, the consensus is
            # replaced with that of the DOMINANT haplotype (prevents it from
            # "flipping" with the parameters). Tie-break: in Coding markers the
            # haplotype that translates cleanly is preferred (discards numts/
            # pseudogenes); in non-Coding markers the dominant one is used.
            if _resolve_on:
                _r = _dominant_haplotype(list(seqdict.values()),
                                         _resolve_minor, _resolve_secfrac,
                                         _resolve_tol)
                if _r["mixed"] and _r["clusters"]:
                    cl = _r["clusters"]
                    # Length (gap-free) and number of ambiguities (Ns) per cluster.
                    for c in cl:
                        _seq = c["consensus"].replace("-", "")
                        c["len"] = len(_seq)
                        c["nN"] = _seq.count("N")
                    # Selection with barcoding rigor: ABUNDANCE is a weak signal
                    # (PCR/sequencing can over-amplify the wrong template),
                    # so consensus quality/validity takes priority.
                    #   Coding: TRANSLATES CLEANLY -> FEWER Ns -> LENGTH closer to
                    #            plen -> ABUNDANCE.
                    #   non-Coding: FEWER Ns -> ABUNDANCE (non-Coding acceptance is by
                    #            absence of Ns; length may vary).
                    # This way, a minority variant that translates, is clean (0 Ns) and
                    # of canonical length beats a dominant one with Ns or incorrect
                    # length (numt / contaminant / paralog).
                    if _gc != 0:
                        for c in cl:
                            # "Translates" = translates cleanly AFTER trimming the
                            # gene's terminal stop codon + 3' tail (same criterion as
                            # the main QC). This way a real haplotype with a terminal
                            # stop (e.g. full-length CytB) is recognized as valid and
                            # is distinguished from a NUMT/paralog (early stops -> short
                            # ORF -> doesn't translate).
                            _cs = c["consensus"].replace("-", "")
                            _orf_c, _aa_c = orf_trim(_cs, _gc)
                            c["translates"] = bool(_cs) and (
                                _aa_c * 3 >= len(_cs) * ORF_MIN_COVERAGE)
                        _cand = [i for i, c in enumerate(cl) if c["translates"]]
                        if _cand:
                            chosen_idx = min(
                                _cand,
                                key=lambda i: (cl[i]["nN"], abs(cl[i]["len"] - plen),
                                               -cl[i]["size"]))
                        else:
                            chosen_idx = 0
                    else:
                        chosen_idx = min(
                            range(len(cl)),
                            key=lambda i: (cl[i]["nN"], -cl[i]["size"]))
                    # 'needs review': the chosen one is NOT the most abundant (cl[0])
                    # — quality was prioritized over abundance — or several clusters
                    # pass barcode QC (translates + length within window + 0 Ns):
                    # possible real allelic variants worth inspecting.
                    def _passes_qc(c):
                        if c.get("nN", 0) > 0:
                            return False
                        if _gc == 0:
                            # non-Coding: accepted by 0 Ns; length may vary
                            # between species (ITS, etc.), len~=plen is not required.
                            return True
                        return (c.get("translates")
                                and abs(c["len"] - plen) <= postdemlen)
                    _n_pass = sum(1 for c in cl if _passes_qc(c))
                    needs_review = (chosen_idx != 0) or (_n_pass > 1)
                    # Reassign roles: the chosen one is 'dominant', the rest 'secondary'.
                    for i, c in enumerate(cl):
                        c["role"] = "dominant" if i == chosen_idx else "secondary"
                    conseq_aln = cl[chosen_idx]["consensus"]
                    conseq = conseq_aln.replace("-", "")
                    # Divergence of each cluster relative to the chosen dominant one:
                    # the key discriminator on plates with many conspecific
                    # samples. <~2% ~= another individual of the same species /
                    # heteroplasmia (harmless); heterospecific level suggests
                    # contamination or sample mixing.
                    _dom_seq = cl[chosen_idx]["consensus"].replace("-", "")
                    for i, c in enumerate(cl):
                        c["divergence"] = (0.0 if i == chosen_idx else
                                           _seq_divergence(_dom_seq,
                                                           c["consensus"]))
                    _max_div = max((c["divergence"] for c in cl), default=0.0)
                    _div_review = _max_div >= _resolve_divrev
                    needs_review = needs_review or _div_review
                    # Secundarios (todos los no elegidos) en orden de abundancia.
                    _secs = [{"rank": c["rank"],
                              "frac": c["frac"],
                              "size": c["size"],
                              "seq": c["consensus"].replace("-", ""),
                              "translates": c["translates"],
                              "divergence": c["divergence"]}
                             for i, c in enumerate(cl) if i != chosen_idx]
                    mix = {
                        "frac": cl[chosen_idx]["frac"],
                        "n_poly": _r["n_poly"],
                        "n_clusters": _r["n_clusters"],
                        "n_noise": _r["n_noise"],
                        "coverage": coverage,
                        "needs_review": needs_review,
                        "chosen_by_abundance": (chosen_idx == 0),
                        "n_pass_qc": _n_pass,
                        "max_divergence": _max_div,
                        "review_divergence": _div_review,
                        # back-compat: 'secondary' = top secondary (string).
                        "secondary": (_secs[0]["seq"] if _secs else ""),
                        "secondaries": _secs,
                        "clusters": [{"rank": c["rank"], "size": c["size"],
                                      "frac": c["frac"], "len": c["len"],
                                      "nN": c["nN"], "translates": c["translates"],
                                      "divergence": c["divergence"],
                                      "role": c["role"]} for c in cl],
                    }

                    # ── Save reads of the eligible secondary clusters ──
                    # for later recovery (2a-consensus + Phase 3).
                    # Eligible = secondaries with < _resolve_maxn Ns in their consensus;
                    # the _resolve_maxvar most abundant ones are saved, and the number
                    # left out is recorded (to notify the user).
                    if _resolve_recover:
                        _skey = name.split("_all.fa")[0]
                        _secs_cl = [c for i, c in enumerate(cl) if i != chosen_idx]
                        _eligible = [c for c in _secs_cl if c["nN"] < _resolve_maxn]
                        _to_save = _eligible[:_resolve_maxvar]
                        _vdir = os.path.join(outpath, "barcodesets", "variant_reads")
                        try:
                            os.makedirs(_vdir, exist_ok=True)
                            saved = []
                            for c in _to_save:
                                vname = f"{_skey}__var{c['rank']}"
                                with open(os.path.join(_vdir, vname + "_all.fa"),
                                          "w") as vf:
                                    for ri, aread in enumerate(c.get("members", [])):
                                        rseq = aread.replace("-", "")
                                        if rseq:
                                            vf.write(f">{vname}_read{ri}\n{rseq}\n")
                                saved.append({"name": vname, "rank": c["rank"],
                                              "size": c["size"], "frac": c["frac"]})
                            mix["recover_saved"] = saved
                            mix["n_variants_extra"] = len(_eligible) - len(_to_save)
                        except OSError:
                            mix["recover_saved"] = []
                            mix["n_variants_extra"] = 0

            if _gc == 0:
                # non-Coding marker mode: acceptance is by ABSENCE OF Ns; the
                # consensus length may vary between species (e.g. ITS),
                # so len==plen is NOT required here (the GUI accepts by 0 Ns).
                transcheck = "non-Coding"
                if conseq.count("N") == 0:
                    if len(conseq) == plen:
                        flag = True
                    if mix is not None:
                        # Mixture explicitly resolved to the dominant haplotype.
                        transcheck = "non-Coding-mixed"
            else:
                # Coding marker. Validates that the FULL consensus is plen long
                # (± qclentol; 0 = exact length, classic behavior) and has no
                # Ns; it is then trimmed to the clean ORF (removes the gene's
                # stop codon + non-coding 3' tail) and that ORF is required to
                # cover >=ORF_MIN_COVERAGE of the consensus itself. The output
                # barcode is the trimmed ORF, which translates without a stop
                # (BOLD-valid). Translation remains mandatory: a length deviation
                # caused by an indel ERROR produces a frameshift and does NOT pass.
                if abs(len(conseq) - plen) <= qclentol and conseq.count("N") == 0:
                    _orf, _aalen = orf_trim(conseq, _gc)
                    if _aalen * 3 >= len(conseq) * ORF_MIN_COVERAGE:
                        conseq = _orf
                        transcheck = "1"
                        flag = True
                    else:
                        transcheck = "0"
                else:
                    transcheck = translate_corframe(conseq, _gc)
        else:
            transcheck = "NA"
            coverage = "NA"
        return transcheck, conseq, flag, coverage, mix

    def subset_bylength(infile, outfile, n, plen, windowlen):
        """Selects the n reads closest to plen. Also examines the length
        distribution of ALL reads in the sample (before the window filter):
        a second mode separated by >=30 bp with >=20% of the reads suggests
        a mixture of products of different length (possible heterospecific
        contamination) that subsampling by closeness to plen would hide from
        the haplotype resolver. Returns (n_reads_total, lenwarn) with
        lenwarn = None or (mode1_bp, mode2_bp, mode2_fraction)."""
        samplesize = 0
        entries = []
        len_hist = {}
        current_header = None
        with open(infile) as fulldata:
            for line in fulldata:
                line = line.rstrip()
                if line.startswith('>'):
                    current_header = line
                    samplesize += 1
                elif current_header is not None:
                    seq = line
                    _bin = len(seq) // 10
                    len_hist[_bin] = len_hist.get(_bin, 0) + 1
                    dev = abs(plen - len(seq))
                    if dev <= windowlen:
                        seq_hash = hashlib.md5(seq.encode()).hexdigest()
                        entries.append((dev, current_header, seq_hash, seq))
                    current_header = None
        entries.sort(key=lambda x: (x[0], x[1], x[2]))
        ntosubset = min(len(entries), n)
        with open(outfile, 'w') as subsetdata:
            for k in range(ntosubset):
                subsetdata.write(entries[k][1] + '\n' + entries[k][3] + '\n')
        lenwarn = None
        if samplesize >= 10 and len_hist:
            b1 = max(len_hist, key=lambda b: (len_hist[b], -b))
            far = {b: c for b, c in len_hist.items() if abs(b - b1) >= 3}
            if far:
                b2 = max(far, key=lambda b: (far[b], -b))
                frac2 = far[b2] / float(samplesize)
                if frac2 >= 0.2:
                    lenwarn = (b1 * 10, b2 * 10, frac2)
        return samplesize, lenwarn

    transcheck = {}
    conseqs = {}
    flags = {}
    coverages = {}
    sampleids = {}
    mixinfo = {}
    lenwarns = {}
    parstring = ''

    try:
        with open(parfilepath) as parfile:
            l = parfile.readlines()
            parstring = l[0].strip() if l else ''
    except (FileNotFoundError, OSError):
        pass  # parstring stays '', subprocess will fail and be caught by the inner try-except

    # In the parallel-pool context, each worker uses 1 disttbfast thread.
    # The Pool handles the parallelization; multiplying disttbfast's internal
    # threads by the number of workers would saturate the CPU.
    parstring = re.sub(r'-C\s+\d+-\d+', '-C 1-1', parstring)

    for c, name in enumerate(inlist1):
        cov = "NA"
        try:
            if subsetval != 0:
                _skey_sub = name.split("_all.fa")[0]
                if v == 0:
                    _ss, _lw = subset_bylength(
                        os.path.join(outpath, indir, name),
                        os.path.join(outpath, indir2, name),
                        subsetval, plen, postdemlen)
                    sampleids[_skey_sub] = _ss
                    if _lw:
                        lenwarns[_skey_sub] = _lw
                if v == 1:
                    _ss, _lw = subset_bylength(
                        os.path.join(indir, name),
                        os.path.join(outpath, indir2, name),
                        subsetval, plen, postdemlen)
                    sampleids[_skey_sub] = _ss
                    if _lw:
                        lenwarns[_skey_sub] = _lw
            cmd_args = [disttbpath] + parstring.split() + ['-i', os.path.join(outpath, indir2, name)]

            stdout = _run_disttbfast(cmd_args)

            with open(os.path.join(outpath, indir2 + '_mafft', name + '_aln.fasta'), "wb") as handle:
                handle.write(stdout)

            aln_path = os.path.join(outpath, indir2 + "_mafft", name + "_aln.fasta")
            _sd = parse_aln_fasta(aln_path)
            transcheckeach, conseq, flag, cov, mixeach = callconsensus(aln_path, fixthresh, 5, name, _seqdict=_sd)
            if mixeach is not None:
                mixinfo[name.split("_all.fa")[0]] = mixeach
            otherconseqs = []

            if flag:
                transcheck[name.split("_all.fa")[0]] = transcheckeach
                conseqs[name.split("_all.fa")[0]] = conseq
                flags[name.split("_all.fa")[0]] = flag
                if cov != "NA":
                    coverages[name.split("_all.fa")[0]] = cov
            elif mixeach is not None:
                # Mixture resolved to the dominant haplotype: do NOT run the
                # threshold fallback (it would re-derive over the full set and
                # reintroduce the mixture). The final block stores the dominant consensus.
                pass
            else:
                seqs_for_col = list(_sd.values())
                if len(seqs_for_col) >= 5:
                    col_stats = []
                    for col in zip(*seqs_for_col):
                        n_col = len(col)
                        cnt = Counter(col)
                        col_stats.append({b: c / n_col for b, c in cnt.items()})
                    n = rangefreq[1]
                    while n >= rangefreq[0]:
                        if n != fixthresh:
                            sequence = []
                            for freq_dict in col_stats:
                                baseset = {b: f for b, f in freq_dict.items() if f > n}
                                if not baseset:
                                    bp = 'N'
                                elif len(baseset) == 1:
                                    bp = next(iter(baseset))
                                else:
                                    d = dict(baseset)
                                    d.pop('-', None)
                                    bp = next(iter(d)) if len(d) == 1 else 'N'
                                sequence.append(bp)
                            conseq2 = ''.join(sequence).replace("-", "")
                            if conseq2:
                                if gencode_for(name) == 0:
                                    flag2 = len(conseq2) == plen and conseq2.count("N") == 0
                                else:
                                    flag2 = False
                                    if abs(len(conseq2) - plen) <= qclentol and conseq2.count("N") == 0:
                                        _orf2, _aal2 = orf_trim(conseq2, gencode_for(name))
                                        if _aal2 * 3 >= len(conseq2) * ORF_MIN_COVERAGE:
                                            conseq2 = _orf2   # barcode = trimmed ORF
                                            flag2 = True
                                if flag2 and conseq2 not in otherconseqs:
                                    otherconseqs.append(conseq2)
                        n -= stepsize
                    
            if len(otherconseqs) == 1:
                if gencode_for(name) == 0:
                    _t = "non-Coding"
                else:
                    _t = "1"
                transcheck[name.split("_all.fa")[0]] = _t
                conseqs[name.split("_all.fa")[0]] = otherconseqs[0]
                flags[name.split("_all.fa")[0]] = True
                if cov != "NA":
                    coverages[name.split("_all.fa")[0]] = cov
            else:
                transcheck[name.split("_all.fa")[0]] = transcheckeach
                conseqs[name.split("_all.fa")[0]] = conseq
                flags[name.split("_all.fa")[0]] = flag
                if cov != "NA":
                    coverages[name.split("_all.fa")[0]] = cov
                    
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception) as _exc:
            _key = name.split("_all.fa")[0]
            import sys as _sys
            print(f"[ONTbarcoder DEBUG] phase 2a — barcode {_key} failed: "
                  f"{type(_exc).__name__}: {_exc}", file=_sys.stderr)
            transcheck[_key] = "NA"
            conseqs[_key] = ""
            flags[_key] = False
            if cov != 'NA':
                coverages[_key] = "NA"

    result = [transcheck, conseqs, flags, coverages, mixinfo, lenwarns]
    return result


def _runtoptwenty_worker(args):
    """
    Worker for runtoptwenty with deterministic ordering.
    Resolves ties by name when distances are equal.
    """
    i, each, seq_info, refseqdict, outpath = args

    # Iteration order does not affect the result: 'dists' is a dict and below
    # it is deterministically re-sorted with sorted() + resolve_ties_by_name().
    # That's why refseqdict is iterated directly, without sorting the keys (which
    # saves an O(R log R) sort per query sequence).
    ambiguity_codes = AMBIGUITY_CODES
    dists = {}

    for other in refseqdict:
        if each != other:
            k = edlib.align(each, other, mode='NW', task='distance',
                           additionalEqualities=ambiguity_codes)
            dists[other] = k['editDistance']
    
    sorted_d = sorted(dists.items(), key=lambda x: x[1])
    
    # --- DETERMINISTIC PATCH: resolve ties by name ---
    sorted_d = resolve_ties_by_name(sorted_d)
    
    outfile_path = os.path.join(outpath, seq_info[0].split(";")[0])
    
    with open(outfile_path, 'w') as outfile:
        outfile.write(">" + seq_info[0] + '\n' + each + '\n')
        for other in sorted_d[:20]:
            outfile.write(">" + refseqdict[other[0]][0] +
                         ";dist=" + str(float(other[1]) / float(len(other[0]) or 1) * 100) +
                         '\n' + other[0] + '\n')
    
    cmd_args = [
        disttbpath, '-q', '0', '-E', '2', '-V', '-1.53', '-s', '0.0',
        '-W', '6', '-O', '-C', '1-1',
        '-b', '62', '-g', '0', '-f', '-1.53', '-Q', '100.0', '-h', '0',
        '-F', '-X', '0.1', '-x', '1000', '-i', outfile_path
    ]

    try:
        stdout = _run_disttbfast(cmd_args)
        with open(outfile_path + "_aln.fa", "wb") as handle:
            handle.write(stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as _exc:
        import sys as _sys
        print(f"[ONTbarcoder DEBUG] phase 3 — {os.path.basename(outfile_path)} failed: "
              f"{type(_exc).__name__}: {_exc}", file=_sys.stderr)
        try:
            open(outfile_path + "_aln.fa", 'wb').close()
        except OSError:
            pass

    return i


def _runconsensusparts_indexed(args):
    """Wrapper for runconsensusparts with an index."""
    idx, job = args
    return idx, _runconsensusparts_fn(job)


def rundemultiplex(inlist):
    """
    Worker function for demultiplexing.
    Modified: sorts the input list and all dictionary iterations
    to guarantee identical results on every run.
    """
    # --- 1. Sort the input file list (deterministic) ---
    inlist1 = inlist[0]
    inlist1 = deterministic_sort(inlist1)
    
    outpath = inlist[1]
    tagdict = inlist[2]
    muttags_fr = inlist[3]
    sampledict = inlist[4]
    typedict = inlist[5]
    primerfset = inlist[6]
    taglen = inlist[8]
    primerrset = inlist[7]
    num_row = inlist[9]
    maxid = inlist[10]
    maxv = inlist[11]
    typelist = inlist[12]
    # Max. edit distance tolerated when locating a primer (forward/reverse)
    # within the search window. Used to be hardcoded to 10; now comes from the
    # GUI ("Primer mismatches allowed"). Falls back to 10 for backward compatibility.
    primermm = inlist[13] if len(inlist) > 13 else 10

    # --- 2. Definition of findmatch_m2 with ordered iteration ---
    def findmatch_m2(taglist, muttags_fr, indict, seqdict, sampledict, typedict, num_row):
        buffer = defaultdict(list)
        n_match = 0
        # Sort the 'indict' dictionary's keys for deterministic traversal
        for each in sorted(indict.keys()):
            try:
                idcomb = (taglist[indict[each][0]], taglist[indict[each][1]])
                cumscore = typedict[indict[each][0]] + typedict[indict[each][1]]
                scores = [str(typedict[indict[each][0]]),
                          str(typedict[indict[each][1]]), str(cumscore)]
                sample = sampledict[idcomb]
                buffer[sample].append(">" + each + "_" + "_".join(scores) + "\n" + seqdict[each] + "\n")
                n_match += 1
            except KeyError:
                pass
        out_dir = os.path.join(outpath, str(num_row))
        # Write samples in alphabetical order for stronger determinism
        for sample in sorted(buffer.keys()):
            with open(os.path.join(out_dir, sample + "_all.fa"), 'a') as fh:
                fh.writelines(buffer[sample])

    # --- 3. Internal helper functions (unchanged) ---
    def readprimertagfasta(filef, filer):
        indict2, indict1, indict = {}, {}, {}
        with open(filef) as infile1:
            with open(filer) as infile2:
                l1 = infile1.readlines()
                l2 = infile2.readlines()
                for i, j in enumerate(l1):
                    if ">" in j and i + 1 < len(l1):
                        indict1[j.strip().replace(">", "")] = l1[i+1].strip()
                        indict[j.strip().replace(">", "")] = [0, 0]
                for i, j in enumerate(l2):
                    if ">" in j and i + 1 < len(l2):
                        indict2[j.strip().replace(">", "")] = l2[i+1].strip()
                        indict[j.strip().replace(">", "")] = [0, 0]
        return indict1, indict2, indict

    def builddict_sequences(infile):
        seqdict = {}
        with open(infile) as inseqs:
            header = None
            for line in inseqs:
                line = line.strip()
                if ">" in line:
                    header = line.replace(">", "")
                elif header is not None:
                    seqdict[header] = line
                    header = None
        return seqdict

    def demultiplexfunc(pf, pr, seqdict, tagdict, muttags_fr, sampledict, typedict, num_row):
        indict1, indict2, indict = readprimertagfasta(pf, pr)
        for each in list(indict.keys()):
            try:
                indict[each][0] = indict1[each]
                indict[each][1] = indict2[each]
            except KeyError:
                del indict[each]
        findmatch_m2(tagdict, muttags_fr, indict, seqdict, sampledict, typedict, num_row)

    # --- 4. nseqs calculation (unchanged) ---
    labeltext = ""
    c = 0
    nseqs = 0
    for infile in inlist1:
        if infile == maxid:
            nseqs += maxv
        else:
            nseqs += 20000

    typedict2 = {}
    for each in typelist:
        typedict2[each[0]] = each[1]

    # --- 5. Main loop over each partial file ---
    for infile in inlist1:
        ambiguity_codes = AMBIGUITY_CODES
        inputseqs = builddict_sequences(os.path.join(outpath, infile))

        # --- 5a. Sort the sequence dictionary's keys (deterministic) ---
        sorted_seq_keys = sorted(inputseqs.keys())

        with open(os.path.join(outpath, infile + "_all_glsearch1.parsed.lencutoff5parsed_f"), 'w') as tagfoutfile:
            with open(outpath + "/" + infile + "_all_glsearchR.parsed.lencutoff5parsed_r", 'w') as tagroutfile:
                with open(outpath + "/" + infile + "_all_glsearchR.parsed.lencutoff5endr", 'w') as fprimercleanfile:
                    # Pre-compute reverse complements of all reverse primers (once)
                    primerrset_rc = [revcomp(pr) for pr in primerrset]

                    # Best match per read: for each sequence, try every
                    # (pf x pr) combination and keep the one with the lowest total distance.
                    # Guarantees each read is written exactly once,
                    # avoiding duplicates when the cocktail has multiple primers.
                    for n, inseq in enumerate(sorted_seq_keys):
                        c += 1
                        if n % 1000 == 0:
                            progress = float(c)
                            if _queue_for_progress:
                                _queue_for_progress.put((num_row, progress, labeltext))

                        best_dist   = 999
                        best_tag_f  = None
                        best_tag_r  = None
                        best_clean  = None
                        compseq     = None  # computed once per read, only if needed

                        for pf in primerfset:
                            k1 = edlib.align(pf, inputseqs[inseq][:typedict2[infile]], mode='HW', task='locations', additionalEqualities=ambiguity_codes)
                            d1 = k1['editDistance']
                            if d1 != 0:
                                if compseq is None:
                                    compseq = revcomp(inputseqs[inseq])
                                k2 = edlib.align(pf, compseq[:typedict2[infile]], mode='HW', task='locations', additionalEqualities=ambiguity_codes)
                                d2 = k2['editDistance']
                            else:
                                d2 = 1

                            orient     = 0
                            revseq     = ''
                            tag_f      = ''
                            d_forward  = 999

                            if d1 <= d2:
                                if d1 <= primermm:
                                    loc = k1['locations'][0]
                                    # Require taglen full bases before the primer;
                                    # loc[0] < taglen would make loc[0]-taglen a
                                    # negative slice index and yield a wrong tag.
                                    tag_f = (inputseqs[inseq][loc[0]-taglen:loc[0]]
                                             if loc[0] >= taglen else "")
                                    if tag_f:
                                        revseq    = inputseqs[inseq][loc[1]+1:]
                                        d_forward = d1
                            else:
                                if d2 <= primermm:
                                    loc = k2['locations'][0]
                                    tag_f = (compseq[loc[0]-taglen:loc[0]]
                                             if loc[0] >= taglen else "")
                                    if tag_f:
                                        revseq    = compseq[loc[1]+1:]
                                        orient    = 1
                                        d_forward = d2

                            if not revseq:
                                continue

                            for pr_rc in primerrset_rc:
                                k = edlib.align(pr_rc, revseq[-typedict2[infile]:], mode='HW', task='locations', additionalEqualities=ambiguity_codes)
                                d = k['editDistance']
                                if d <= primermm:
                                    loc        = k['locations'][0]
                                    startpoint = len(revseq) - typedict2[infile]
                                    # On short reads startpoint can be negative;
                                    # only extract when a full taglen window is
                                    # available, otherwise the slice wraps around.
                                    _rt = startpoint + loc[1] + 1
                                    # Start of the clean segment (everything before the reverse
                                    # primer). On reads shorter than the search window,
                                    # startpoint is negative and clean_end can end up
                                    # < 0 even when _rt is valid (loc[0] <= loc[1]);
                                    # in that case revseq[:clean_end] would trim from the end,
                                    # yielding a wrong clean sequence, so clean_end >= 0
                                    # is required to accept the candidate.
                                    clean_end = startpoint + loc[0]
                                    tag_r = (revcomp(revseq[_rt:_rt+taglen])
                                             if (0 <= _rt and _rt + taglen <= len(revseq)
                                                 and clean_end >= 0)
                                             else "")
                                    if tag_r:
                                        total_dist = d_forward + d
                                        if total_dist < best_dist:
                                            best_dist  = total_dist
                                            best_tag_f = tag_f
                                            best_tag_r = tag_r
                                            if orient == 0:
                                                best_clean = revseq[:clean_end]
                                            else:
                                                best_clean = revseq[:clean_end].replace("A", "E").replace("G", "F").replace("C", "Q").replace("T", "P")
                                            if best_dist == 0:
                                                break
                            if best_dist == 0:
                                break

                        # Only write if the full optimal combination was found
                        if best_tag_f is not None:
                            tagfoutfile.write(">" + inseq + '\n' + best_tag_f + '\n')
                            tagroutfile.write(">" + inseq + '\n' + best_tag_r + '\n')
                            fprimercleanfile.write(">" + inseq + '\n' + best_clean + '\n')

        # --- 5b. Second pass: demultiplexfunc with sorted input ---
        inputseqs2 = builddict_sequences(outpath + "/" + infile + "_all_glsearchR.parsed.lencutoff5endr")
        demultiplexfunc(outpath + "/" + infile + "_all_glsearch1.parsed.lencutoff5parsed_f",
                       outpath + "/" + infile + "_all_glsearchR.parsed.lencutoff5parsed_r",
                       inputseqs2, tagdict, muttags_fr, sampledict, typedict, num_row)

    # --- 6. Notify completion of this batch ---
    if _queue_for_progress:
        _queue_for_progress.put((num_row, nseqs, labeltext))


# ============================================================
# CLASS: runconsensusparts (QThread)
# ============================================================

class runconsensusparts(QtCore.QThread):
    taskFinished = QtCore.pyqtSignal(int)
    notifyProgress = QtCore.pyqtSignal(int)

    def __init__(self, inlist, parent=None):
        super(runconsensusparts, self).__init__(parent)
        self.inlist = inlist
        self.transcheck = {}
        self.conseqs = {}
        self.flags = {}
        self.coverages = {}
        self.sampleids = {}
        self.mixinfo = {}
        self.lenwarns = {}

    def run(self):
        inlist = self.inlist
        inlist1 = inlist[0]
        outpath = inlist[1]
        indir = inlist[2]

        # --- DETERMINISTIC PATCH: sort file list ---
        inlist1_sorted = deterministic_sort(inlist1)
        
        _resolve = inlist[15] if len(inlist) > 15 else {}
        _gencode_by_sample = inlist[16] if len(inlist) > 16 else {}
        _qclentol = inlist[17] if len(inlist) > 17 else 0
        jobs = [
            [[fname], inlist[1], inlist[2], inlist[3], inlist[4],
             inlist[5], inlist[6], inlist[7], inlist[8], inlist[9],
             inlist[10], inlist[11], inlist[12], inlist[13], inlist[14],
             _resolve, _gencode_by_sample, _qclentol]
            for fname in inlist1_sorted
        ]

        _use_serial = N_THREADS <= 1 or len(jobs) <= 1

        if not _use_serial:
            try:
                from multiprocessing.pool import ThreadPool as _ThreadPool
                n_workers = min(N_THREADS, len(jobs))
                indexed_jobs = [(i, job) for i, job in enumerate(jobs)]
                completed = 0
                with _ThreadPool(n_workers) as pool:
                    for orig_i, res in pool.imap_unordered(_runconsensusparts_indexed, indexed_jobs):
                        self.transcheck.update(res[0])
                        self.conseqs.update(res[1])
                        self.flags.update(res[2])
                        self.coverages.update(res[3])
                        if len(res) > 4:
                            self.mixinfo.update(res[4])
                        if len(res) > 5:
                            self.lenwarns.update(res[5])
                        fname = inlist1_sorted[orig_i]
                        key = fname.split('_all.fa')[0]
                        fa = os.path.join(outpath, indir, fname)
                        try:
                            with open(fa) as f:
                                self.sampleids[key] = sum(1 for l in f if l.startswith('>'))
                        except (FileNotFoundError, OSError):
                            self.sampleids[key] = 0
                        completed += 1
                        self.notifyProgress.emit(completed)
            except Exception:
                # Pool failed (console-less compiled exe, worker crash, etc.) -> serial mode
                _use_serial = True
                self.transcheck = {}
                self.conseqs = {}
                self.flags = {}
                self.coverages = {}
                self.sampleids = {}
                self.mixinfo = {}
                self.lenwarns = {}

        if _use_serial:
            for i, job in enumerate(jobs):
                try:
                    res = _runconsensusparts_fn(job)
                except Exception:
                    res = [{}, {}, {}, {}, {}]
                self.transcheck.update(res[0])
                self.conseqs.update(res[1])
                self.flags.update(res[2])
                self.coverages.update(res[3])
                if len(res) > 4:
                    self.mixinfo.update(res[4])
                if len(res) > 5:
                    self.lenwarns.update(res[5])
                fname = inlist1_sorted[i]
                key = fname.split('_all.fa')[0]
                fa = os.path.join(outpath, indir, fname)
                try:
                    with open(fa) as f:
                        self.sampleids[key] = sum(1 for l in f if l.startswith('>'))
                except (FileNotFoundError, OSError):
                    self.sampleids[key] = 0
                self.notifyProgress.emit(i + 1)

        self.taskFinished.emit(0)


# ============================================================
# CLASE: runtoptwenty (QThread)
# ============================================================

class runtoptwenty(QtCore.QThread):
    taskFinished = QtCore.pyqtSignal(int)
    notifyProgress = QtCore.pyqtSignal(int)

    def __init__(self, inlist, parent=None):
        super(runtoptwenty, self).__init__(parent)
        self.inlist = inlist

    def run(self):
        inlist = self.inlist
        seqlist = inlist[0]
        seqdict = inlist[1]
        refseqdict = inlist[2]
        outpath = inlist[3]

        # --- PATCH DETERMINISTA: ordenar lista de secuencias ---
        seqlist_sorted = deterministic_sort(seqlist)
        
        # Crear mapeo para preservar correspondencia con diccionarios
        seqinfo_list = [seqdict[each] for each in seqlist_sorted]

        jobs_tt = [
            (i, each, seqinfo_list[i], refseqdict, outpath)
            for i, each in enumerate(seqlist_sorted)
        ]

        completed = 0
        _use_serial_tt = N_THREADS <= 1 or len(jobs_tt) <= 1

        if not _use_serial_tt:
            try:
                from multiprocessing.pool import ThreadPool as _ThreadPool
                n_workers = min(N_THREADS, len(jobs_tt))
                with _ThreadPool(n_workers) as pool:
                    for _ in pool.imap_unordered(_runtoptwenty_worker, jobs_tt):
                        completed += 1
                        self.notifyProgress.emit(completed)
            except Exception:
                _use_serial_tt = True
                completed = 0

        if _use_serial_tt:
            for job in jobs_tt:
                try:
                    _runtoptwenty_worker(job)
                except Exception:
                    pass
                completed += 1
                self.notifyProgress.emit(completed)

        self.taskFinished.emit(0)


# ============================================================
# CLASE: MSAcheck
# ============================================================

class MSAcheck(QtCore.QThread):
    taskFinished = QtCore.pyqtSignal(int)
    notifyProgress1 = QtCore.pyqtSignal(list)
    notifyProgress2 = QtCore.pyqtSignal(list)
    notifyProgress3 = QtCore.pyqtSignal(str)
    notifyProgress4 = QtCore.pyqtSignal(str)

    def __init__(self, outpath, plen, filename, task, dirname, mode, indir, goodfile, errfile, n90subset, outdir, ngood, corlist, prefix, dirdict, parent=None):
        super(MSAcheck, self).__init__(parent)
        self.outpath = outpath
        self.plen = plen
        self.filename = filename
        self.task = task
        self.dirname = dirname
        self.mode = mode
        self.indir = indir
        self.goodfile = goodfile
        self.errfile = errfile
        self.n90subset = n90subset
        self.outdir = outdir
        self.ngood = ngood
        self.tocorlist = corlist
        self.prefix = prefix
        self.dirdict = dirdict

    def builddict_sequences(self, infile):
        seqdict = {}
        with open(infile) as inseqs:
            header = None
            for line in inseqs:
                line = line.strip()
                if ">" in line:
                    header = line.replace(">", "")
                elif header is not None:
                    seqdict[header] = line
                    header = None
        return seqdict

    def consensus(self, indict, perc_thresh):
        seqs = list(indict.values())
        if not seqs:
            return ''
        sequence = []
        for col in zip(*seqs):
            n_col = len(col)
            cnt = Counter(col)
            baseset = {b: c for b, c in cnt.items()
                       if float(c) / n_col > perc_thresh}
            if not baseset:
                bp = 'N'
            elif len(baseset) == 1:
                bp = next(iter(baseset))
            else:
                bp = 'N'
            sequence.append(bp)
        return ''.join(sequence)

    def callconsensus(self, i, perc_thresh):
        seqdict = {}
        header = None
        parts = []
        with open(i) as fh:
            for line in fh:
                line = line.rstrip()
                if line.startswith('>'):
                    if header is not None:
                        seqdict[header] = ''.join(parts)
                    header = line[1:]
                    parts = []
                elif header is not None:
                    parts.append(line)
        if header is not None:
            seqdict[header] = ''.join(parts)
        return self.consensus(seqdict, perc_thresh), seqdict

    def run(self):
        filename = self.filename
        ambiguity_codes = AMBIGUITY_CODES
        
        if self.ngood >= 3:
            cmd_args = [
                disttbpath, '-q', '0', '-E', '2', '-V', '-1.53', '-s', '0.0',
                '-W', '6', '-O', '-C', '1-1',
                '-b', '62', '-g', '0', '-f', '-1.53', '-Q', '100.0', '-h', '0',
                '-F', '-X', '0.1', '-x', '1000',
                '-i', os.path.join(self.outpath, "barcodesets", "temps", self.goodfile)
            ]
            stdout = _run_disttbfast(cmd_args, timeout=1800)
            with open(os.path.join(self.outpath, "barcodesets", "temps", self.goodfile.split(".")[0] + "_aln.fa"), "wb") as handle:
                handle.write(stdout)
            conseq, seqdict = self.callconsensus(os.path.join(self.outpath, "barcodesets", "temps", self.goodfile.split(".")[0] + "_aln.fa"), 0.5)
            self.notifyProgress3.emit("done")

        badbarcodes = {}

        with open(os.path.join(self.outpath, "barcodesets", self.outdir, self.errfile)) as bfile:
            l = bfile.readlines()
            for i, j in enumerate(l):
                if ">" in j and i + 1 < len(l):
                    badbarcodes[j[1:].split(";")[0]] = l[i+1].strip()

        ngoodbarcodes = 0
        
        # --- DETERMINISTIC PATCH: sort lists for iteration ---
        tocorlist_sorted = deterministic_sort(self.tocorlist) if self.tocorlist else []

        with open(os.path.join(self.outpath, "barcodesets", self.outdir, self.prefix + "_predgood_barcodes.fa"), 'w') as gfile:
            with open(os.path.join(self.outpath, "barcodesets", self.outdir, self.errfile), 'a') as bfile:
                if self.ngood >= 3:
                    # Sort the seqdict dictionary's keys
                    seqdict_keys = deterministic_sort(list(seqdict.keys()))
                    
                    for n, each in enumerate(seqdict_keys):
                        if each.split(";")[0] in tocorlist_sorted:
                            flag = True
                            errcount = 0
                            for i, j in enumerate(seqdict[each]):
                                if j == "-":
                                    if conseq[i] != "-":
                                        errcount += 1
                                        flag = False
                                else:
                                    if conseq[i] == "-":
                                        errcount += 1
                                        flag = False
                            if flag:
                                gfile.write(">" + each + ";estgaps=" + str(errcount) + '\n' + seqdict[each].replace("-", "").upper() + '\n')
                                ngoodbarcodes += 1
                            else:
                                bfile.write(">" + each + ";estgaps=" + str(errcount) + '\n' + seqdict[each].replace("-", "").upper() + '\n')
                                badbarcodes[each + ";estgaps=" + str(errcount)] = seqdict[each]
                            self.notifyProgress2.emit([n+1, len(tocorlist_sorted)])
                else:
                    seqdict = self.builddict_sequences(os.path.join(self.outpath, "barcodesets", "temps", self.goodfile))
                    seqdict_keys = deterministic_sort(list(seqdict.keys()))
                    
                    for n, each in enumerate(seqdict_keys):
                        if each.split(";")[0] in tocorlist_sorted:
                            bfile.write(">" + each + '\n' + seqdict[each].replace("-", "").upper() + '\n')
                            badbarcodes[each] = seqdict[each]
                            self.notifyProgress2.emit([n+1, len(tocorlist_sorted)])

        self.notifyProgress4.emit('done')

        if self.task == 1:
            try:
                os.mkdir(os.path.join(self.outpath, "2b_ConsensusBySimilarity", self.dirname))
            except OSError:
                pass

            if self.mode == 0:
                dirlist = os.listdir(os.path.join(self.outpath, "demultiplexed"))
            else:
                dirlist = os.listdir(os.path.join(self.indir))

            refdict = {}
            for each in badbarcodes.keys():
                refdict[each.split(';')[0]] = badbarcodes[each].upper().replace("-", "")

            with open(os.path.join(self.outpath, "2b_ConsensusBySimilarity", "summary"), 'w') as outfile2:
                refdict_keys = deterministic_sort(list(refdict.keys()))
                
                for i, f in enumerate(refdict_keys):
                    with open(os.path.join(self.outpath, "2b_ConsensusBySimilarity", self.dirname, f), 'w') as outfile:
                        ddict = {}
                        if self.mode == 0:
                            seqdict = self.builddict_sequences(os.path.join(self.outpath, "demultiplexed", f))
                        else:
                            seqdict = self.builddict_sequences(os.path.join(self.indir, self.dirdict[f]))
                        
                        for seqid in seqdict.keys():
                            try:
                                k = edlib.align(seqdict[seqid].replace("P", "T").replace("E", "A").replace("F", "G").replace("Q", "C"),
                                               refdict[f], mode='NW', task='distance', additionalEqualities=ambiguity_codes)
                                d = k['editDistance']
                                if d < self.plen * 0.1:
                                    ddict[seqid] = d
                            except KeyError:
                                pass

                        # Deterministic tie-break: by distance, then by the
                        # sequence and the id. Without this, reads tied in
                        # distance right at the truncation cutoff (n90subset) were
                        # selected based on the demultiplexed file's order,
                        # which depends on the number of threads -> the 2b barcode
                        # count varied with N_THREADS. (Phase 3 already uses resolve_ties_by_name.)
                        sorted_d = sorted(ddict.items(),
                                          key=lambda x: (x[1], seqdict[x[0]], x[0]))
                        outfile2.write(f + '\t' + str(len(sorted_d)) + '\n')
                        for k in sorted_d[:self.n90subset]:
                            outfile.write(">" + k[0] + '\n' + seqdict[k[0]] + '\n')
                    self.notifyProgress1.emit([i+1, len(refdict_keys)])

        self.taskFinished.emit(ngoodbarcodes)


# ============================================================
# CLASE: copyfiles
# ============================================================

class copyfiles(QtCore.QThread):
    taskFinished = QtCore.pyqtSignal(int)

    def __init__(self, indir, outdir, inlist, indir2, dirdict, inputmode, parent=None):
        super(copyfiles, self).__init__(parent)
        self.indir = indir
        self.outdir = outdir
        self.inlists = inlist
        self.dirdict = dirdict
        self.indir2 = indir2
        self.inputmode = inputmode

    def run(self):
        indir2 = self.indir2
        for i, inlist in enumerate(self.inlists):
            if len(inlist) > 0:
                # Ordenar lista para determinismo
                inlist_sorted = deterministic_sort(inlist)
                for fname in inlist_sorted:
                    if self.inputmode == 1:
                        shutil.copyfile(os.path.join(self.indir, fname), os.path.join(self.outdir[i], fname))
                    else:
                        shutil.copyfile(os.path.join(self.indir, self.dirdict[fname]), os.path.join(self.outdir[i], self.dirdict[fname]))
            shutil.make_archive(self.outdir[i], 'zip', self.outdir[i])
            shutil.rmtree(self.outdir[i])
        
        # Non-Coding runs skip phases 2b/3, so those folders may not exist; only
        # archive+remove the ones actually present to avoid a FileNotFoundError.
        for _sub in ("2a_ConsensusByLength", "2b_ConsensusBySimilarity",
                     "3_ConsensusByBarcodeComparison"):
            _p = os.path.join(indir2, _sub)
            if os.path.isdir(_p):
                shutil.make_archive(_p, 'zip', _p)
                shutil.rmtree(_p)
        self.taskFinished.emit(1)

