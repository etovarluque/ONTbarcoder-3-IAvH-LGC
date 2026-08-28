#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
ONTbarcoder_multiprocessing.py - VERSIÓN PATCHADA
==================================================
Modificaciones para garantizar resultados determinísticos e idénticos
a la versión original ONTbarcoder2.py, mientras se mantiene la
paralelización para máximo rendimiento.
"""

# ============================================================
# IMPORTS COMPLETOS
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

# Probabilidad de error por valor ASCII de calidad (Phred+33). Se usa para el
# filtro opcional de calidad por-read: la calidad media de un read ONT se calcula
# como -10*log10(media de probabilidades de error), no como media aritmética de Q.
_PHRED_ERR = [10.0 ** (-(q - 33) / 10.0) for q in range(256)]
# Acceso ligado a nivel de módulo: permite sumar las probabilidades de error
# iterando los bytes de la línea de calidad con map()/sum() a velocidad de C,
# ~8x más rápido que un bucle Python con ord() por carácter.
_PHRED_ERR_GET = _PHRED_ERR.__getitem__

# ============================================================
# FUNCIONES AUXILIARES PARA DETERMINISMO
# ============================================================

def deterministic_sort(items):
    """
    Ordena cualquier colección de forma determinística.
    Garantiza que el orden sea reproducible entre ejecuciones.
    """
    if not items:
        return items
    # Convertir a string para ordenamiento consistente
    return sorted(items, key=lambda x: str(x) if x is not None else "")



def resolve_ties_by_name(items):
    """
    Resuelve empates en listas de tuplas (item, score) ordenando
    alfabéticamente por el nombre del item cuando los scores son iguales.
    """
    if not items or len(items) <= 1:
        return items
    
    result = []
    last_score = None
    group = []
    
    for item, score in items:
        if score != last_score:
            if group:
                # Ordenar el grupo alfabéticamente por nombre
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
# CONSTANTES GLOBALES
# ============================================================

AMBIGUITY_CODES = [
    ("R", "A"), ("R", "G"), ("M", "A"), ("M", "C"), ("S", "C"), ("S", "G"),
    ("Y", "C"), ("Y", "T"), ("K", "G"), ("K", "T"), ("W", "A"), ("W", "T"),
    ("V", "A"), ("V", "C"), ("V", "G"), ("H", "A"), ("H", "C"), ("H", "T"),
    ("D", "A"), ("D", "G"), ("D", "T"), ("B", "C"), ("B", "G"), ("B", "T"),
    ("N", "A"), ("N", "G"), ("N", "C"), ("N", "T"),
]

# Configuración de rutas
def _get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # Go up one level from _utilities/ to the project root
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT_DIR = _get_base_dir()
parfilepath = os.path.join(SCRIPT_DIR, "_mafftfiles", "parfile")
disttbpath  = os.path.join(SCRIPT_DIR, "_mafftfiles", "disttbfast.exe")
MAFFT_DIR   = os.path.join(SCRIPT_DIR, "_mafftfiles")

# Número de hilos (puede ser sobreescrito por la GUI)
N_THREADS = 4

# En Windows, shell=True crea cmd.exe como intermediario.
# Con 20+ workers simultáneos, 20 cmd.exe peleando por el console host de Windows
# bloquea el SO. CREATE_NO_WINDOW + shell=False elimina ese intermediario.
_CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def set_threads(n):
    """Actualiza N_THREADS."""
    global N_THREADS
    N_THREADS = max(1, n)


_PHYSICAL_CORES = None

# Directorio de trabajo reutilizable POR HILO.
# disttbfast deposita archivos de nombre fijo (conflicts, pre, trace, order) en su cwd.
# En lugar de mkdtemp+copy(_aamtx)+rmtree por llamada, se crea el dir una sola vez al
# primer uso del hilo y se limpian solo esos archivos entre invocaciones.
#
# IMPORTANTE: runconsensusparts/runtoptwenty ejecutan los workers con ThreadPool
# (varios hilos en el MISMO proceso), no con procesos. Un único directorio global
# compartido provocaría una condición de carrera: _reset_worker_tmpdir de un hilo
# borraría los archivos de nombre fijo que otro hilo está usando en el mismo cwd.
# Por eso el directorio es thread-local: cada hilo trabaja en su propio cwd aislado.
_worker_tls = threading.local()

# Registro global (thread-safe) de TODOS los directorios temporales creados, para
# poder borrarlos. Como el dir es thread-local, no se puede alcanzar el TLS de cada
# hilo desde fuera; este registro permite limpiarlos en bloque al terminar un
# pipeline (cleanup_worker_tmpdirs) y, como red de seguridad, al salir (atexit).
_worker_dirs = []
_worker_dirs_lock = threading.Lock()


def _get_worker_tmpdir():
    """Crea (o recupera) el directorio temporal aislado de ESTE hilo."""
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
    """Borra todos los directorios temporales de workers MAFFT creados hasta ahora.

    Seguro de llamar entre fases/pipelines: cada fase de consenso abre un ThreadPool
    con hilos nuevos (TLS nuevo), así que los directorios de fases previas ya no están
    en uso. También se registra con atexit como red de seguridad al cerrar la app.
    Tras la limpieza, el siguiente hilo que llame a _get_worker_tmpdir creará uno nuevo.
    """
    with _worker_dirs_lock:
        for d in _worker_dirs:
            shutil.rmtree(d, ignore_errors=True)
        _worker_dirs.clear()
    # Olvidar la referencia TLS del hilo actual (su dir ya fue borrado); cualquier
    # otro hilo revalida con os.path.isdir() en _get_worker_tmpdir y recreará el suyo.
    if getattr(_worker_tls, "dir", None) is not None:
        _worker_tls.dir = None


atexit.register(cleanup_worker_tmpdirs)


def _reset_worker_tmpdir(work_dir):
    """
    Elimina todos los archivos del directorio de trabajo excepto _aamtx.
    Equivalente a crear un directorio nuevo desde el punto de vista de disttbfast.
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
    Núcleos FÍSICOS (best-effort, cacheado). El trabajo pesado son procesos
    disttbfast.exe forzados a 1 hilo (-C 1-1); su concurrencia óptima ≈ núcleos
    físicos. Superarlos sobre-suscribe la CPU y ralentiza (p.ej. 20 workers en
    16 físicos / 24 lógicos rinde peor que 12). En Windows se consulta la API
    GetLogicalProcessorInformation; si falla, se cae a la mitad de los lógicos
    (heurística conservadora para CPUs con hyperthreading).
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
            glpi(None, ctypes.byref(length))  # 1ra llamada: obtiene el tamaño
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
    """Concurrencia recomendada: nunca por encima de los núcleos físicos.
    Sin argumento devuelve el óptimo; con `requested` lo limita a ese tope."""
    phys = physical_core_count()
    if requested is None:
        return phys
    return max(1, min(int(requested), phys))


def _run_disttbfast(cmd_args, timeout=120):
    """
    Ejecuta disttbfast.exe en el directorio de trabajo de este worker-proceso.
    disttbfast crea archivos con nombres fijos (conflicts, pre, trace, order) en su
    cwd. _reset_worker_tmpdir los elimina antes de cada invocación, garantizando un
    cwd limpio sin el coste de mkdtemp+copy(_aamtx)+rmtree por llamada.
    Reintenta ante fallos transitorios; TimeoutExpired se propaga sin reintento.
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
# FUNCIONES UTILITARIAS
# ============================================================

def revcomp(seq: str) -> str:
    """Calcula el reverse complement de una secuencia de ADN."""
    seq = seq.upper()
    comp_table = str.maketrans(
        "ACGTRYMKSWBDHVN",
        "TGCAYRKMWSVHDBN"
    )
    return seq.translate(comp_table)[::-1]


def resource_path(relative_path):
    """Obtiene la ruta correcta para recursos (compatible con PyInstaller)."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


# ============================================================
# CLASE: prepdemultiplex
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
        # Filtro opcional de calidad media por-read (Q). 0 = desactivado.
        self.minq = minq
        # Nº de errores (sustitución/indel) tolerados en los tags al demultiplexar:
        # genera los mutantes de tag hasta esta profundidad (0 = solo exacto).
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
            # Pares de primers desde columna 3: (primer_f, primer_r), (primer_f2, primer_r2)...
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

        # ── PASADA ÚNICA: las tres fases originales (filtro → split → chunk)
        # se fusionan en un solo recorrido del FASTQ para evitar tres lecturas
        # secuenciales de disco. Los reads se clasifican y acumulan en memoria
        # por lotes de CHUNK_LINES líneas antes de escribir, reduciendo las
        # llamadas a write() sin aumentar el uso de RAM de forma significativa.
        # ────────────────────────────────────────────────────────────────────
        self.nseqsfordemultiplexingpresplit1 = 0
        self.nseqsfordemultiplexingpresplit2 = 0
        self.nwronglengthwindow = 0
        self.nseqsfordemultiplexing = 0
        self.maxid = 0

        CHUNK_LINES = 80000          # 40 000 reads × 2 líneas por read (id + seq)
        SPLIT_CHUNK = 40000          # líneas por archivo de partición (igual que antes)

        # Buffers en memoria para los dos destinos principales
        buf_1pdt = []                # reads de longitud correcta (un barcode)
        buf_2pdt = []                # reads de longitud doble (dos barcodes ligados)
        buf_write_threshold = 4096   # líneas acumuladas antes de flush parcial

        # Contadores de chunk para los archivos particionados
        chunk_1pdt_lines = 0
        chunk_1pdt_idx   = 1
        chunk_2pdt_lines = 0
        chunk_2pdt_idx   = 1

        # Límites de longitud calculados una sola vez
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
            """Escribe líneas al chunk actual; abre el siguiente si se llena."""
            fh = _get_chunk(prefix, idx_ref[0])
            pos = 0
            while pos < len(lines_buf):
                space = SPLIT_CHUNK - lines_ref[0]
                batch = lines_buf[pos: pos + space]
                fh.writelines(batch)
                lines_ref[0] += len(batch)
                pos += len(batch)
                if lines_ref[0] >= SPLIT_CHUNK and pos < len(lines_buf):
                    # Solo abrir el siguiente chunk si aún quedan líneas por escribir
                    fh.flush()
                    idx_ref[0] += 1
                    lines_ref[0] = 0
                    fh = _get_chunk(prefix, idx_ref[0])

        idx1_ref   = [chunk_1pdt_idx]
        lines1_ref = [chunk_1pdt_lines]
        idx2_ref   = [chunk_2pdt_idx]
        lines2_ref = [chunk_2pdt_lines]

        # Leer el FASTQ con un buffer de I/O grande (8 MB) para minimizar
        # las llamadas al sistema operativo en archivos de varios GB.
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

                # Filtro opcional de calidad: descarta reads cuya calidad media
                # ONT (-10*log10(media de prob. de error)) cae por debajo de minq.
                # Se itera sobre los bytes de la línea de calidad con sum()/map()
                # (velocidad de C) para minimizar el overhead en archivos grandes.
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
                    # Producto simple (un barcode)
                    buf_1pdt.append(seqid)
                    buf_1pdt.append(line2 if line2.endswith("\n") else line2 + "\n")
                    self.nseqsfordemultiplexing += 1
                    self.nseqsfordemultiplexingpresplit1 += 1

                elif len_2pdt_min < slen < len_2pdt_max:
                    # Producto doble (dos barcodes ligados): dividir en dos
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

                # Flush parcial cuando los buffers crecen mucho
                if len(buf_1pdt) >= buf_write_threshold:
                    _flush_chunk("reformat_out_1pdt", buf_1pdt, idx1_ref, lines1_ref)
                    buf_1pdt = []
                if len(buf_2pdt) >= buf_write_threshold:
                    _flush_chunk("reformat_out_2pdt", buf_2pdt, idx2_ref, lines2_ref)
                    buf_2pdt = []

        # Flush final de los buffers restantes
        if buf_1pdt:
            _flush_chunk("reformat_out_1pdt", buf_1pdt, idx1_ref, lines1_ref)
        if buf_2pdt:
            _flush_chunk("reformat_out_2pdt", buf_2pdt, idx2_ref, lines2_ref)

        # Cerrar todos los file handles abiertos
        for fh in _open_chunks.values():
            try:
                fh.close()
            except Exception:
                pass

        # maxid = nombre del último chunk _1pdt escrito.
        # Si lines1_ref[0] > 0: el último chunk activo es idx1_ref[0].
        # Si lines1_ref[0] == 0: el último chunk lleno fue idx1_ref[0] (puede haber
        #   llegado exactamente a SPLIT_CHUNK sin abrir uno nuevo).
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
        # Reads en el último chunk _1pdt. Cada chunk tiene SPLIT_CHUNK líneas
        # (= SPLIT_CHUNK/2 reads, 2 líneas por read). lines1_ref[0] es el nº de
        # líneas escritas en el chunk actual (el último); vale SPLIT_CHUNK si se
        # llenó exacto. rundemultiplex suma maxv (este valor) para el chunk máximo
        # y SPLIT_CHUNK//2 para el resto, así que maxv debe estar en READS — no en
        # líneas ni sobre nseqspasslen (que incluye 2pdt y ventana incorrecta).
        _last_1pdt_lines = lines1_ref[0] if lines1_ref[0] > 0 else SPLIT_CHUNK
        self.lastbitn = _last_1pdt_lines // 2

        self.tagdict, self.muttags_fr, self.sampledict, self.typedict = crmutant_m2(self.demfile, self.tagmm)

        self.taskFinished.emit(0)


# ============================================================
# CLASE: mergedemfiles
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
        # Ordenar claves para determinismo
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
# CLASE: calculatecoverage
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
                            self.counter[fname.split("_all.fa")[0].split(".")[0]] += 1
                        except KeyError:
                            self.counter[fname.split("_all.fa")[0].split(".")[0]] = 1
            self.notifyProgress.emit(c+1)
        
        self.taskFinished.emit(0)


# ============================================================
# FUNCIONES PARA MULTIPROCESSING (con ordenamiento determinístico)
# ============================================================

# Variables globales para colas de progreso (usadas por pool_init)
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
    """Consenso por mayoría de columna (alineado, conserva gaps/Ns) para una
    lista de secuencias alineadas de igual longitud. Replica la lógica de la
    función interna `consensus()` del worker, para reutilizarla en el resolutor
    de haplotipo dominante."""
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
    Resolutor de variantes intra-muestra (marca-agnóstico) para las reads
    MSA-alineadas de UNA muestra. A partir de las columnas polimórficas agrupa los
    reads en N haplotipos por clustering greedy por abundancia (no asume sólo dos
    templates) y devuelve el consenso del haplotipo DOMINANTE (el más abundante)
    junto con el desglose de TODOS los clusters. Esto evita que una mayoría única
    "voltee" con los parámetros cuando coexisten varios templates co-abundantes
    (contaminación cruzada, parálogo/numt, variantes alélicas, mezcla de muestras).

    No depende de traducción ni de longitud: opera solo sobre las columnas del
    alineamiento, así que sirve igual para Coding y no-Coding. La traducción (Coding) se
    puede usar aparte como desempate cuando hay clusters co-abundantes.

    Parámetros:
      minor_thresh: frecuencia mínima del segundo alelo para considerar una
                    columna 'polimórfica' (filtra el ruido aleatorio de ONT, que
                    reparte <~5% por alternativa).
      min_secondary_frac: fracción mínima de un cluster para considerarlo un
                    haplotipo 'real' (y para marcar la muestra como 'mixta').
      tolerance:    fracción de las columnas polimórficas en que un read puede
                    discrepar del centroide de su cluster y aún pertenecer a él.
                    Absorbe el error de secuenciación / variación intrínseca para
                    no fragmentar un mismo haplotipo en muchos clusters espurios.

    Devuelve dict:
      mixed:               bool   (≥2 clusters reales)
      dominant_frac:       float  (proporción del haplotipo dominante)
      n_poly:              int    (nº de columnas que distinguen los haplotipos)
      dominant_consensus:  str|None  (consenso alineado del elegido, con gaps)
      secondary_consensus: str|None  (back-compat: top secundario, con gaps)
      n_clusters:          int    (nº de clusters reales)
      n_noise:             int    (reads en clusters sub-umbral)
      clusters:            list de dicts ordenados desc. por tamaño:
                           {rank, size, frac, consensus(alineado), translates(None),
                            role ∈ {dominant, secondary, noise}}
    """
    seqs = list(aligned_seqs)
    n = len(seqs)
    _none = {"mixed": False, "dominant_frac": 1.0, "n_poly": 0,
             "dominant_consensus": None, "secondary_consensus": None,
             "n_clusters": 1, "n_noise": 0, "clusters": []}
    if n < 4:
        return _none
    L = len(seqs[0])

    # 1) Columnas polimórficas: las que tienen un segundo alelo (no gap/N) por
    #    encima de minor_thresh. En una muestra de un solo template no hay (o casi).
    poly_cols = []  # índices de columna
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
    if not poly_cols:
        return _none

    # 2) Firma por read = bases en las columnas polimórficas. N/'-' = comodín:
    #    no cuentan como discrepancia en la distancia (errores/cobertura parcial).
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

    # Firmas únicas ordenadas por (frecuencia desc, firma) → determinístico.
    sig_counts = Counter(_sig(s) for s in seqs)
    ordered_sigs = sorted(sig_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    # 3) Clustering greedy por abundancia: la firma más frecuente no asignada se
    #    vuelve semilla; absorbe toda firma a distancia ≤ tol_pos; se refina el
    #    centroide a la base mayoritaria por columna (una pasada).
    assigned = set()                # firmas ya asignadas
    cluster_members = []            # lista de listas de reads (secuencias alineadas)
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
        # Refinar centroide y reasignar firmas aún libres más cercanas a él.
        member_reads = [r for sig in members_sigs for r in reads_by_sig[sig]]
        centroid = tuple(_consensus_columns([_sig(r) for r in member_reads], 0.5))
        extra = [sig for sig, _ in ordered_sigs
                 if sig not in assigned and _dist(sig, centroid) <= tol_pos]
        if extra:
            assigned.update(extra)
            member_reads += [r for sig in extra for r in reads_by_sig[sig]]
        cluster_members.append(member_reads)

    # 4) Ordenar clusters por tamaño (desempate determinístico por consenso).
    cluster_members.sort(key=lambda m: (-len(m), ''.join(_consensus_columns(m, 0.5))))

    # Clusters reales = fracción ≥ min_secondary_frac; el resto es ruido.
    real, noise_reads = [], []
    for m in cluster_members:
        if len(m) / n >= min_secondary_frac:
            real.append(m)
        else:
            noise_reads.extend(m)
    if not real:                       # nada supera el umbral: todo es un cluster
        real = [cluster_members[0]] if cluster_members else [seqs]
        noise_reads = []

    clusters = []
    for rank, m in enumerate(real):
        clusters.append({
            "rank": rank + 1,
            "size": len(m),
            "frac": len(m) / n,
            "consensus": ''.join(_consensus_columns(m, 0.5)),
            "translates": None,        # lo rellena callconsensus en modo Coding
            "role": "dominant" if rank == 0 else "secondary",
            # Reads alineadas del cluster (con gaps). Se usan localmente en
            # callconsensus para volcar las reads por cluster a disco y poder
            # recuperar las variantes secundarias (re-consenso 2a + Fase 3). NO se
            # propaga en el dict 'mix' que vuelve al hilo principal (sería pesado).
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
    Función worker para consenso por partes.
    Modificada: ordena la lista de entrada deterministicamente.
    """
    # --- PATCH DETERMINISTA: ordenar lista de archivos ---
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
    # Código genético POR MUESTRA (multiplexado de vertebrados/invertebrados en un
    # mismo FASTQ): {sample_id: tabla_NCBI}. Las muestras sin entrada usan el código
    # global ``ingencode``. Backwards-compatible (len<=16 → dict vacío).
    gencode_by_sample = inlist[16] if len(inlist) > 16 else {}

    def gencode_for(name):
        """Resuelve la tabla de traducción para una muestra a partir de su nombre
        de archivo (``<sample>_all.fa``), con ``ingencode`` como respaldo."""
        sid = name.split("_all.fa")[0].split(".")[0]
        return gencode_by_sample.get(sid, ingencode)

    # Config de resolución de mezcla/contaminación (haplotipo dominante).
    # Marca-agnóstica: aplica a Coding y no-Coding. Backwards-compatible (len<=15).
    resolve_cfg = inlist[15] if len(inlist) > 15 else {}
    _resolve_on = bool(resolve_cfg.get("enabled"))
    _resolve_minor = float(resolve_cfg.get("minor_thresh", 0.2))
    _resolve_secfrac = float(resolve_cfg.get("min_secondary_frac", 0.2))
    _resolve_tol = float(resolve_cfg.get("tolerance", 0.10))
    # Recuperación de variantes secundarias (2a-consenso + Fase 3): siempre activa
    # cuando la detección está encendida. Se guardan las reads de cada cluster
    # secundario elegible (< _resolve_maxn Ns en su consenso) para re-procesarlas,
    # con un tope de _resolve_maxvar por muestra (las más abundantes).
    _resolve_recover = bool(resolve_cfg.get("recover_secondaries", True))
    _resolve_maxvar = int(resolve_cfg.get("max_variants", 3))
    _resolve_maxn = int(resolve_cfg.get("max_variant_Ns", 5))

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

    # Cobertura mínima del ORF limpio respecto al amplicón para aceptar el barcode
    # tras recortar el codón de paro terminal del gen. ≥0.95 conserva COI intacto
    # (sin paro interno → no se recorta) y CytB de longitud completa (paro a ~97%),
    # y sigue descartando NUMTs/pseudogenes (paros dispersos tempranos → ORF corto).
    ORF_MIN_COVERAGE = 0.95

    def orf_trim(seq, gencode):
        """Devuelve (seq_recortada, n_aa): el ORF en marco más largo SIN codones de
        paro internos. Si la secuencia ya traduce sin paro interno (fragmento Folmer
        de COI) se devuelve intacta. Si contiene un paro interno seguido de cola 3'
        no codificante (p. ej. un amplicón que abarca el codón de terminación del gen
        y arrastra parte del tRNA siguiente, como CytB completo) se recorta el paro y
        todo lo 3' de él, dejando un barcode que traduce limpio y es válido para BOLD."""
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
        # Sin paro interno (a lo sumo un codón parcial al final): no recortar, para
        # preservar la longitud convencional del barcode (p. ej. COI 658 bp).
        if best_aa * 3 >= len(s) - 2:
            return s, best_aa
        # Paro interno + cola: recortar al ORF limpio (quita el paro y el extremo 3').
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
        # Código genético de ESTA muestra (vert=2 / invert=5 / 0=no-Coding...).
        _gc = gencode_for(name)
        conseq_aln = consensus(seqdict, perc_thresh, abs_thresh)
        conseq = conseq_aln.replace("-", "")
        flag = False
        mix = None
        if conseq:
            coverage = len(seqdict)

            # --- Resolución de mezcla / contaminación (marca-agnóstica) ---
            # Si la muestra tiene dos haplotipos co-abundantes, se sustituye el
            # consenso por el del haplotipo DOMINANTE (evita que "voltee" con los
            # parámetros). Desempate: en Coding se prefiere el haplotipo que traduce
            # limpio (descarta numts/pseudogenes); en no-Coding se usa el dominante.
            if _resolve_on:
                _r = _dominant_haplotype(list(seqdict.values()),
                                         _resolve_minor, _resolve_secfrac,
                                         _resolve_tol)
                if _r["mixed"] and _r["clusters"]:
                    cl = _r["clusters"]
                    # Longitud (sin gaps) y nº de ambigüedades (Ns) por cluster.
                    for c in cl:
                        _seq = c["consensus"].replace("-", "")
                        c["len"] = len(_seq)
                        c["nN"] = _seq.count("N")
                    # Selección con rigor de barcoding: la ABUNDANCIA es una señal
                    # débil (PCR/secuenciación sobre-amplifican el template
                    # equivocado), así que prima la calidad/validez del consenso.
                    #   Coding: TRADUCE LIMPIO → MENOS Ns → LONGITUD más cercana a
                    #            plen → ABUNDANCIA.
                    #   no-Coding:  MENOS Ns → ABUNDANCIA (la aceptación no-Coding es por
                    #            ausencia de Ns; la longitud puede variar).
                    # Así, una variante minoritaria que traduce, limpia (0 Ns) y de
                    # longitud canónica le gana a un dominante con Ns o de longitud
                    # incorrecta (numt / contaminante / parálogo).
                    if _gc != 0:
                        for c in cl:
                            # "Traduce" = traduce limpio TRAS recortar el codón de paro
                            # terminal del gen + cola 3' (mismo criterio que el QC
                            # principal). Así un haplotipo real con paro terminal (p. ej.
                            # CytB completo) se reconoce como válido y se distingue de un
                            # NUMT/parálogo (paros tempranos → ORF corto → no traduce).
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
                    # 'needs review': el elegido NO es el más abundante (cl[0]) — se
                    # priorizó calidad sobre abundancia — o varios clusters pasan el
                    # QC de barcode (traduce + longitud dentro de ventana + 0 Ns):
                    # posibles variantes alélicas reales que conviene inspeccionar.
                    def _passes_qc(c):
                        if c.get("nN", 0) > 0:
                            return False
                        if _gc == 0:
                            # no-Coding: aceptación por 0 Ns; la longitud puede variar
                            # entre especies (ITS, etc.), no se exige len≈plen.
                            return True
                        return (c.get("translates")
                                and abs(c["len"] - plen) <= postdemlen)
                    _n_pass = sum(1 for c in cl if _passes_qc(c))
                    needs_review = (chosen_idx != 0) or (_n_pass > 1)
                    # Reasignar roles: el elegido es 'dominant', el resto 'secondary'.
                    for i, c in enumerate(cl):
                        c["role"] = "dominant" if i == chosen_idx else "secondary"
                    conseq_aln = cl[chosen_idx]["consensus"]
                    conseq = conseq_aln.replace("-", "")
                    # Secundarios (todos los no elegidos) en orden de abundancia.
                    _secs = [{"frac": c["frac"],
                              "size": c["size"],
                              "seq": c["consensus"].replace("-", ""),
                              "translates": c["translates"]}
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
                        # back-compat: 'secondary' = top secundario (cadena).
                        "secondary": (_secs[0]["seq"] if _secs else ""),
                        "secondaries": _secs,
                        "clusters": [{"rank": c["rank"], "size": c["size"],
                                      "frac": c["frac"], "len": c["len"],
                                      "nN": c["nN"], "translates": c["translates"],
                                      "role": c["role"]} for c in cl],
                    }

                    # ── Guardar reads de los clusters secundarios elegibles ──
                    # para la recuperación posterior (re-consenso 2a + Fase 3).
                    # Elegibles = secundarios con < _resolve_maxn Ns en su consenso;
                    # se guardan los _resolve_maxvar más abundantes y se registra
                    # cuántos quedaron fuera (para avisar al usuario).
                    if _resolve_recover:
                        _skey = name.split("_all.fa")[0].split(".")[0]
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
                # Modo Marcador no-Coding: la aceptación es por AUSENCIA DE Ns; la
                # longitud del consenso puede variar entre especies (p. ej. ITS),
                # por eso NO se exige len==plen aquí (la GUI acepta por 0 Ns).
                transcheck = "non-Coding"
                if conseq.count("N") == 0:
                    if len(conseq) == plen:
                        flag = True
                    if mix is not None:
                        # Mezcla resuelta explícitamente al haplotipo dominante.
                        transcheck = "non-Coding-mixed"
            else:
                # Marcador codificante. Se valida que el consenso COMPLETO mida plen
                # (producto correcto) y no tenga Ns; luego se recorta al ORF limpio
                # (elimina el codón de paro del gen + cola 3' no codificante) y se
                # exige que ese ORF cubra ≥ORF_MIN_COVERAGE del amplicón. El barcode
                # de salida es el ORF recortado, que traduce sin paro (válido BOLD).
                if len(conseq) == plen and conseq.count("N") == 0:
                    _orf, _aalen = orf_trim(conseq, _gc)
                    if _aalen * 3 >= plen * ORF_MIN_COVERAGE:
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
        samplesize = 0
        entries = []
        current_header = None
        with open(infile) as fulldata:
            for line in fulldata:
                line = line.rstrip()
                if line.startswith('>'):
                    current_header = line
                    samplesize += 1
                elif current_header is not None:
                    seq = line
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
        return samplesize

    transcheck = {}
    conseqs = {}
    flags = {}
    coverages = {}
    sampleids = {}
    mixinfo = {}
    parstring = ''

    try:
        with open(parfilepath) as parfile:
            l = parfile.readlines()
            parstring = l[0].strip() if l else ''
    except (FileNotFoundError, OSError):
        pass  # parstring queda '', subprocess fallará y será capturado por el try-except interno

    # En contexto de pool paralelo cada worker usa 1 hilo de disttbfast.
    # La paralelización la gestiona el Pool; multiplicar hilos internos
    # de disttbfast por el número de workers satura la CPU.
    parstring = re.sub(r'-C\s+\d+-\d+', '-C 1-1', parstring)

    for c, name in enumerate(inlist1):
        cov = "NA"
        try:
            if subsetval != 0:
                if v == 0:
                    sampleids[name.split("_all.fa")[0].split(".")[0]] = subset_bylength(
                        os.path.join(outpath, indir, name),
                        os.path.join(outpath, indir2, name),
                        subsetval, plen, postdemlen)
                if v == 1:
                    sampleids[name.split("_all.fa")[0].split(".")[0]] = subset_bylength(
                        os.path.join(indir, name),
                        os.path.join(outpath, indir2, name),
                        subsetval, plen, postdemlen)
            cmd_args = [disttbpath] + parstring.split() + ['-i', os.path.join(outpath, indir2, name)]

            stdout = _run_disttbfast(cmd_args)

            with open(os.path.join(outpath, indir2 + '_mafft', name + '_aln.fasta'), "wb") as handle:
                handle.write(stdout)

            aln_path = os.path.join(outpath, indir2 + "_mafft", name + "_aln.fasta")
            _sd = parse_aln_fasta(aln_path)
            transcheckeach, conseq, flag, cov, mixeach = callconsensus(aln_path, fixthresh, 5, name, _seqdict=_sd)
            if mixeach is not None:
                mixinfo[name.split("_all.fa")[0].split(".")[0]] = mixeach
            otherconseqs = []

            if flag:
                transcheck[name.split("_all.fa")[0].split(".")[0]] = transcheckeach
                conseqs[name.split("_all.fa")[0].split(".")[0]] = conseq
                flags[name.split("_all.fa")[0].split(".")[0]] = flag
                if cov != "NA":
                    coverages[name.split("_all.fa")[0].split(".")[0]] = cov
            elif mixeach is not None:
                # Mezcla resuelta al haplotipo dominante: NO se corre el fallback
                # de umbrales (re-derivaría sobre el set completo y reintroduciría
                # la mezcla). El bloque final almacena el consenso dominante.
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
                                    if len(conseq2) == plen and conseq2.count("N") == 0:
                                        _orf2, _aal2 = orf_trim(conseq2, gencode_for(name))
                                        if _aal2 * 3 >= plen * ORF_MIN_COVERAGE:
                                            conseq2 = _orf2   # barcode = ORF recortado
                                            flag2 = True
                                if flag2 and conseq2 not in otherconseqs:
                                    otherconseqs.append(conseq2)
                        n -= stepsize
                    
            if len(otherconseqs) == 1:
                if gencode_for(name) == 0:
                    _t = "non-Coding"
                else:
                    _t = "1"
                transcheck[name.split("_all.fa")[0].split(".")[0]] = _t
                conseqs[name.split("_all.fa")[0].split(".")[0]] = otherconseqs[0]
                flags[name.split("_all.fa")[0].split(".")[0]] = True
                if cov != "NA":
                    coverages[name.split("_all.fa")[0].split(".")[0]] = cov
            else:
                transcheck[name.split("_all.fa")[0].split(".")[0]] = transcheckeach
                conseqs[name.split("_all.fa")[0].split(".")[0]] = conseq
                flags[name.split("_all.fa")[0].split(".")[0]] = flag
                if cov != "NA":
                    coverages[name.split("_all.fa")[0].split(".")[0]] = cov
                    
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, Exception) as _exc:
            _key = name.split("_all.fa")[0].split(".")[0]
            import sys as _sys
            print(f"[ONTbarcoder DEBUG] fase 2a — barcode {_key} falló: "
                  f"{type(_exc).__name__}: {_exc}", file=_sys.stderr)
            transcheck[_key] = "NA"
            conseqs[_key] = ""
            flags[_key] = False
            if cov != 'NA':
                coverages[_key] = "NA"

    result = [transcheck, conseqs, flags, coverages, mixinfo]
    return result


def _runtoptwenty_worker(args):
    """
    Worker para runtoptwenty con ordenamiento determinístico.
    Resuelve empates por nombre cuando las distancias son iguales.
    """
    i, each, seq_info, refseqdict, outpath = args

    # El orden de iteración no afecta el resultado: 'dists' es un dict y más abajo
    # se re-ordena de forma determinista con sorted() + resolve_ties_by_name().
    # Por eso se itera refseqdict directamente, sin ordenar las claves (lo que
    # ahorra un sort O(R log R) por cada secuencia query).
    ambiguity_codes = AMBIGUITY_CODES
    dists = {}

    for other in refseqdict:
        if each != other:
            k = edlib.align(each, other, mode='NW', task='distance',
                           additionalEqualities=ambiguity_codes)
            dists[other] = k['editDistance']
    
    sorted_d = sorted(dists.items(), key=lambda x: x[1])
    
    # --- PATCH DETERMINISTA: resolver empates por nombre ---
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
        print(f"[ONTbarcoder DEBUG] fase 3 — {os.path.basename(outfile_path)} falló: "
              f"{type(_exc).__name__}: {_exc}", file=_sys.stderr)
        try:
            open(outfile_path + "_aln.fa", 'wb').close()
        except OSError:
            pass

    return i


def _runconsensusparts_indexed(args):
    """Wrapper para runconsensusparts con índice."""
    idx, job = args
    return idx, _runconsensusparts_fn(job)


def rundemultiplex(inlist):
    """
    Función worker para demultiplexado.
    Modificada: ordena la lista de entrada y todas las iteraciones sobre diccionarios
    para garantizar resultados idénticos en cada ejecución.
    """
    # --- 1. Ordenar lista de archivos de entrada (determinista) ---
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
    # Máx. distancia de edición tolerada al localizar un primer (forward/reverse)
    # dentro de la ventana de búsqueda. Antes estaba fijado a 10; ahora viene de la
    # GUI ("Primer mismatches allowed"). Fallback 10 por retrocompatibilidad.
    primermm = inlist[13] if len(inlist) > 13 else 10

    # --- 2. Definición de findmatch_m2 con iteración ordenada ---
    def findmatch_m2(taglist, muttags_fr, indict, seqdict, sampledict, typedict, num_row):
        buffer = defaultdict(list)
        n_match = 0
        # Ordenar las claves del diccionario 'indict' para recorrido determinista
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
        # Escribir las muestras en orden alfabético para mayor determinismo
        for sample in sorted(buffer.keys()):
            with open(os.path.join(out_dir, sample + "_all.fa"), 'a') as fh:
                fh.writelines(buffer[sample])

    # --- 3. Funciones auxiliares internas (sin cambios) ---
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

    # --- 4. Cálculo de nseqs (sin cambios) ---
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

    # --- 5. Bucle principal sobre cada archivo parcial ---
    for infile in inlist1:
        ambiguity_codes = AMBIGUITY_CODES
        inputseqs = builddict_sequences(os.path.join(outpath, infile))

        # --- 5a. Ordenar las claves del diccionario de secuencias (determinista) ---
        sorted_seq_keys = sorted(inputseqs.keys())

        with open(os.path.join(outpath, infile + "_all_glsearch1.parsed.lencutoff5parsed_f"), 'w') as tagfoutfile:
            with open(outpath + "/" + infile + "_all_glsearchR.parsed.lencutoff5parsed_r", 'w') as tagroutfile:
                with open(outpath + "/" + infile + "_all_glsearchR.parsed.lencutoff5endr", 'w') as fprimercleanfile:
                    # Pre-computar reverse complements de todos los primers reverse (una sola vez)
                    primerrset_rc = [revcomp(pr) for pr in primerrset]

                    # Best-match por lectura: para cada secuencia probar todas las
                    # combinaciones (pf × pr) y conservar la de menor distancia total.
                    # Garantiza que cada lectura se escribe exactamente una vez,
                    # evitando duplicados cuando hay múltiples primers en el cocktail.
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
                        compseq     = None  # se calcula una sola vez por lectura si hace falta

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
                                    # Inicio del segmento limpio (todo lo previo al primer
                                    # reverse). En reads más cortos que la ventana de
                                    # búsqueda startpoint es negativo y clean_end puede
                                    # quedar < 0 aunque _rt sea válido (loc[0] <= loc[1]);
                                    # entonces revseq[:clean_end] recortaría desde el final
                                    # dando una secuencia limpia errónea, así que se exige
                                    # clean_end >= 0 para aceptar el candidato.
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

                        # Escribir solo si se encontró la combinación óptima completa
                        if best_tag_f is not None:
                            tagfoutfile.write(">" + inseq + '\n' + best_tag_f + '\n')
                            tagroutfile.write(">" + inseq + '\n' + best_tag_r + '\n')
                            fprimercleanfile.write(">" + inseq + '\n' + best_clean + '\n')

        # --- 5b. Segundo pase: demultiplexfunc con entrada ordenada ---
        inputseqs2 = builddict_sequences(outpath + "/" + infile + "_all_glsearchR.parsed.lencutoff5endr")
        demultiplexfunc(outpath + "/" + infile + "_all_glsearch1.parsed.lencutoff5parsed_f",
                       outpath + "/" + infile + "_all_glsearchR.parsed.lencutoff5parsed_r",
                       inputseqs2, tagdict, muttags_fr, sampledict, typedict, num_row)

    # --- 6. Notificar fin del procesamiento de este lote ---
    if _queue_for_progress:
        _queue_for_progress.put((num_row, nseqs, labeltext))


# ============================================================
# CLASE: runconsensusparts (QThread)
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

    def run(self):
        inlist = self.inlist
        inlist1 = inlist[0]
        outpath = inlist[1]
        indir = inlist[2]

        # --- PATCH DETERMINISTA: ordenar lista de archivos ---
        inlist1_sorted = deterministic_sort(inlist1)
        
        _resolve = inlist[15] if len(inlist) > 15 else {}
        _gencode_by_sample = inlist[16] if len(inlist) > 16 else {}
        jobs = [
            [[fname], inlist[1], inlist[2], inlist[3], inlist[4],
             inlist[5], inlist[6], inlist[7], inlist[8], inlist[9],
             inlist[10], inlist[11], inlist[12], inlist[13], inlist[14],
             _resolve, _gencode_by_sample]
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
                        fname = inlist1_sorted[orig_i]
                        key = fname.split('_all.fa')[0].split('.')[0]
                        fa = os.path.join(outpath, indir, fname)
                        try:
                            with open(fa) as f:
                                self.sampleids[key] = sum(1 for l in f if l.startswith('>'))
                        except (FileNotFoundError, OSError):
                            self.sampleids[key] = 0
                        completed += 1
                        self.notifyProgress.emit(completed)
            except Exception:
                # Pool falló (exe compilado sin consola, worker crash, etc.) → modo serie
                _use_serial = True
                self.transcheck = {}
                self.conseqs = {}
                self.flags = {}
                self.coverages = {}
                self.sampleids = {}
                self.mixinfo = {}

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
                fname = inlist1_sorted[i]
                key = fname.split('_all.fa')[0].split('.')[0]
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
        
        # --- PATCH DETERMINISTA: ordenar listas para iteración ---
        tocorlist_sorted = deterministic_sort(self.tocorlist) if self.tocorlist else []
        
        with open(os.path.join(self.outpath, "barcodesets", self.outdir, self.prefix + "_predgood_barcodes.fa"), 'w') as gfile:
            with open(os.path.join(self.outpath, "barcodesets", self.outdir, self.errfile), 'a') as bfile:
                if self.ngood >= 3:
                    # Ordenar claves del diccionario seqdict
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

                        # Desempate determinista: por distancia, luego por la
                        # secuencia y el id. Sin esto, las lecturas que empatan en
                        # distancia justo en el límite de truncado (n90subset) se
                        # seleccionaban según el orden del archivo demultiplexado,
                        # que depende del número de hilos -> el conteo de barcodes
                        # 2b variaba con N_THREADS. (Fase 3 ya usa resolve_ties_by_name.)
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

