# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the portable Windows build:
#     pyinstaller walleye.spec
# The optional voice engines (Kokoro, faster-whisper) are bundled because a
# frozen app cannot pip-install extras later. Model files still download at
# runtime, next to the exe.
from PyInstaller.utils.hooks import collect_all

# shipped files paths.py falls back to when they aren't next to the exe
datas = [("config.example.yaml", "."), ("icon.ico", ".")]
binaries = []
hiddenimports = ["winotify", "pystray._win32"]
for pkg in ("kokoro_onnx", "espeakng_loader", "phonemizer", "language_tags",
            "faster_whisper", "sounddevice", "ruamel", "pyttsx3"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
        "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
        "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtQml", "PySide6.QtQuickWidgets", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtMultimedia",
        "PySide6.QtPdf", "PySide6.QtPositioning", "PySide6.QtBluetooth",
        "PySide6.QtDesigner", "tests",
        # unrelated packages from the build machine's shared site-packages
        "torch", "torchvision", "torchaudio", "polars", "transformers",
        "sklearn", "scikit-learn", "pandas", "matplotlib", "botocore",
        "boto3", "s3transfer", "yt_dlp", "mutagen", "babel", "curl_cffi",
        "secretstorage", "IPython", "jedi", "notebook",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Wall-Eye",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Wall-Eye",
)
