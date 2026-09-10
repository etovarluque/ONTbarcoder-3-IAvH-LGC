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

a = Analysis(
    ['ONTbarcoder3.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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

for _folder in ('_mafftfiles', '_notes', 'guide'):
    _src = os.path.join(SPECPATH, _folder)
    _dst = os.path.join(_dist_root, _folder)
    if os.path.exists(_dst):
        shutil.rmtree(_dst)
    shutil.copytree(_src, _dst)

shutil.copy(os.path.join(SPECPATH, 'icon.ico'), os.path.join(_dist_root, 'icon.ico'))
