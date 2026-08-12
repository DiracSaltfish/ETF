# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["etf_remote_client.py"],
    pathex=["."],
    binaries=[],
    datas=[("assets/sounds", "assets/sounds")],
    hiddenimports=[
        "PyQt6.QtMultimedia",
        "PyQt6.QtNetwork",
        "PyQt6.QtWebSockets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "fastapi",
        "uvicorn",
        "websockets",
        "pydantic",
        "PyQt5",
        "PySide2",
        "PySide6",
        "IPython",
        "matplotlib",
        "numpy",
        "pytest",
        "tkinter",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ETFRemoteClient",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ETFRemoteClient",
)
app = BUNDLE(
    coll,
    name="ETF远程监控.app",
    icon=None,
    bundle_identifier="com.etfdelivery.remote-client",
    info_plist={
        "CFBundleDisplayName": "ETF远程监控",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "NSLocalNetworkUsageDescription": "用于连接内网 Mac-home ETF 监控服务。",
        "LSMinimumSystemVersion": "13.0",
    },
)
