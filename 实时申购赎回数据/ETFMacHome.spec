# -*- mode: python ; coding: utf-8 -*-

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "websockets.legacy.server",
    "PyQt6.QtMultimedia",
]

a = Analysis(
    ["etf_mac_home_app.py"],
    pathex=["."],
    binaries=[("libwind_tbapi_runtime_probe.dylib", ".")],
    datas=[
        ("web/monitor.html", "web"),
        ("wind_tbapi_runtime_probe.c", "."),
        ("assets/sounds", "assets/sounds"),
    ],
    hiddenimports=hiddenimports,
    hookspath=["pyinstaller_hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
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
    name="ETFMonitorHost",
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
    name="ETFMonitorHost",
)
app = BUNDLE(
    coll,
    name="ETF监控主机.app",
    icon=None,
    bundle_identifier="com.etfdelivery.mac-home",
    info_plist={
        "CFBundleDisplayName": "ETF监控主机",
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "NSLocalNetworkUsageDescription": "用于在内网向授权设备分发 ETF 监控数据。",
        "LSMinimumSystemVersion": "13.0",
    },
)
