# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for ONTbarcoder3 (Linux, onedir build).
#
# Build with:  ./build_linux.sh      (or: pyinstaller ONTbarcoder3_linux.spec)
#
# The Windows build uses ONTbarcoder3.spec instead; both live in the repository
# root next to the shared sources they package.
#
# NOTE: this produces a *onedir* bundle at dist/ONTbarcoder3/.  The application
# locates its runtime data (the MAFFT binaries under _mafftfiles/, the guide,
# translations, the _ui_cache it draws at startup, and the _profiles/_notes it
# writes) relative to os.path.dirname(sys.executable) — i.e. the folder that
# holds the executable.  Therefore those data folders are copied *next to* the
# executable by build_linux.sh AFTER PyInstaller runs; they are deliberately
# NOT bundled into _internal/, or the base-dir lookup would not find them.

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Every module under _utilities is collected as a package rather than listed one
# by one: the panels are imported at module level by ONTbarcoder3.py, and an
# explicit list silently goes stale each time a panel is added (best_seq_panel
# was missing from it), producing a bundle that dies on startup.
_utilities_mods = collect_submodules('_utilities')

# openpyxl is imported lazily inside blast_panel.py and best_seq_panel.py (XLSX
# export of BLAST hits and of the best-sequence report) within a try/except that
# degrades silently, so a missed bundle would disable the feature with no error.
# Collect all of its submodules to guarantee it ships.
# (It must also be listed in requirements.txt so the build venv installs it.)
try:
    _openpyxl = collect_submodules('openpyxl')
except Exception:
    _openpyxl = ['openpyxl']

a = Analysis(
    ['ONTbarcoder3.py'],
    pathex=['_utilities'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'Bio', 'Bio.Seq',
        'edlib', 'xlsxwriter',
    ] + _utilities_mods + _openpyxl,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy.testing'],
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
    console=False,          # GUI app: no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico' if __import__('os').path.isfile('icon.ico') else None,
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
