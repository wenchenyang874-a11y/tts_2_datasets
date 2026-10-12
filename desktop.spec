# Native one-directory bundle: shared Qt libraries remain replaceable.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH)
data = collect_data_files('imageio_ffmpeg')
data += [(str(root / 'desktop/assets'), 'desktop/assets')]
data += [(str(root / name), '.') for name in ['LICENSE', 'THIRD_PARTY_NOTICES.md', 'DISCLAIMER.md']]
a = Analysis([str(root / 'desktop_main.py')], pathex=[str(root)], binaries=[], datas=data,
             hiddenimports=['app', 'desktop.smoke', 'imageio_ffmpeg'],
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQml',
                       'PySide6.QtQuick', 'tkinter', 'pytest'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='TTS Dataset Studio',
          icon=str(root / 'build/app.ico'),
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='TTS Dataset Studio')
