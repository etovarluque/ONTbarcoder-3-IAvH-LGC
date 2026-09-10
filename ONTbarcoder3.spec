# -*- mode: python ; coding: utf-8 -*-
import shutil, os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Collect all submodules inside _utilities so PyInstaller bundles them
hidden = collect_submodules('_utilities')
hidden += [
    'Bio.Seq',
    'Bio.SeqIO',
    'Bio.SeqRecord',
    'Bio.Align',
    'edlib',
    'xlsxwriter',
    'PyQt5',
    'PyQt5.QtCore',
    'PyQt5.QtGui',
    'PyQt5.QtWidgets',
    'PyQt5.QtPrintSupport',
    'multiprocessing',
    'concurrent.futures',
]

# openpyxl is imported lazily inside blast_panel.py and best_seq_panel.py (XLSX
# export of BLAST hits and of the best-sequence report)
# within a try/except that degrades silently, so a missed bundle would disable
# the feature with no error. Pull in all of its submodules to guarantee it ships.
try:
    hidden += collect_submodules('openpyxl')
except Exception:
    hidden += ['openpyxl']

# Pillow is never imported by this project. It only gets pulled in because
# collect_submodules('openpyxl') above reaches openpyxl.drawing.image, which
# imports PIL inside a try/except and falls back to PILImage = False. That path
# is only used to embed images into a workbook, which no panel does — we write
# cells, styles and filters. Excluding it keeps ~10 MB out of the bundle and is
# safe even if Pillow is installed in the build environment.
excluded = ['PIL', 'PIL.Image', 'Pillow']

a = Analysis(
    ['ONTbarcoder3.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ONTbarcoder3',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ONTbarcoder3',
)

# Copy _mafftfiles next to the .exe (same level as _internal), not inside it.
# The app resolves paths via os.path.dirname(sys.executable), so it must live here.
_dist_root = os.path.join(DISTPATH, 'ONTbarcoder3')

# Both MAFFT binaries live in the repository (the Linux build shares these
# sources), so the one for the other platform is skipped here.
_skip = shutil.ignore_patterns('disttbfast')

for _folder in ('_mafftfiles', '_notes', 'guide'):
    _src = os.path.join(SPECPATH, _folder)
    _dst = os.path.join(_dist_root, _folder)
    if os.path.exists(_dst):
        shutil.rmtree(_dst)
    shutil.copytree(_src, _dst, ignore=_skip if _folder == '_mafftfiles' else None)

shutil.copy(os.path.join(SPECPATH, 'icon.ico'), os.path.join(_dist_root, 'icon.ico'))
