# -*- mode: python ; coding: utf-8 -*-
# PyInstaller配置文件 - 视频Web服务器

# 分析Python脚本及其依赖
a = Analysis(
    ['super_badass_videos_web_server.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['waitress', 'psutil', 'pystray', 'PIL', 'PIL.Image', 'PIL.ImageDraw'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# 创建Python字节码存档
pyz = PYZ(a.pure)

# 创建可执行文件
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='VideoWebServer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
