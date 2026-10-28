# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Zotero PDF2zh Server (thin-exe plan).

- Entry: launcher.py (Apple-style desktop UI, manages Flask in-process).
- Mode: onedir (NOT onefile). Reason: onefile extracts bundled libs to
  system %TEMP% on every launch, which violates the requirement that
  nothing be written outside the exe folder. onedir keeps everything
  next to the exe.
- Data: runtime bootstrap / config/*.example / notice.json
  plus _tools/uv.exe (bundled by build_exe.ps1).
- Kernel (pdf2zh-next etc.) is downloaded on first use into exe/core/.
"""
import os
import json
import zipfile
import runpy
from PyInstaller.utils.win32.versioninfo import (VSVersionInfo, FixedFileInfo,
    StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct)

SPECDIR = os.path.dirname(os.path.abspath(SPEC))
BRANDING = os.path.join(SPECDIR, 'assets', 'branding')
release_version = runpy.run_path(os.path.join(SPECDIR, 'utils', 'version.py'))['VERSION']
release_tuple = tuple(map(int, release_version.split('.'))) + (0,)

# Every EXE includes our compiled plugin. Reject stale/unrelated artifacts.
plugin_dir = os.path.join(os.path.dirname(SPECDIR), 'Plugin')
plugin_xpi = os.path.join(plugin_dir, 'build', 'pdf-2-zh-for-zotero.xpi')
with open(os.path.join(plugin_dir, 'package.json'), encoding='utf-8') as stream:
    plugin_package = json.load(stream)
if not os.path.isfile(plugin_xpi):
    raise RuntimeError('Build the Zotero plugin first: cd plugin && npm ci && npm run build')
with zipfile.ZipFile(plugin_xpi) as archive:
    plugin_manifest = json.loads(archive.read('manifest.json'))
    if plugin_manifest['version'] != plugin_package['version'] or plugin_manifest['applications']['zotero']['id'] != plugin_package['config']['addonID']:
        raise RuntimeError('The compiled Zotero plugin does not match plugin/package.json; rebuild it.')

datas = [
    (os.path.join(BRANDING, 'app.ico'), 'branding'),
    (os.path.join(BRANDING, 'manifest.json'), 'branding'),
    (plugin_xpi, 'plugins'),
    (os.path.join(SPECDIR, "llm_catalog.json"), "."),
    (os.path.join(SPECDIR, "runtime_progress.py"), "."),
    (os.path.join(SPECDIR, "runtime_isolation.py"), "."),
    (os.path.join(SPECDIR, "runtime_bootstrap.py"), "."),
    (os.path.join(SPECDIR, "reference_layout.py"), "."),
    (os.path.join(SPECDIR, "layout_integrity.py"), "."),
    (os.path.join(SPECDIR, "favicon.svg"), "."),
    (os.path.join(SPECDIR, "notice.json"), "."),
    (os.path.join(SPECDIR, "config", "config.json.example"), "config"),
    (os.path.join(SPECDIR, "config", "config.toml.example"), "config"),
    (os.path.join(SPECDIR, "config", "venv.json.example"), "config"),
]
datas += [(os.path.join(BRANDING, f'logo-{size}.png'), 'branding') for size in (16,32,48,64,96,128,256)]

# _tools/uv.exe is optional at spec time (downloaded by build_exe.ps1).
_tools = os.path.join(SPECDIR, "_tools", "uv.exe")
if os.path.exists(_tools):
    datas.append((_tools, "_tools"))

hiddenimports = [
    "app_paths",
    "launcher",
    "server",
    "flask",
    "werkzeug",
    "werkzeug.serving",
    "pypdf",
    "fitz",
    "pymupdf",
    "toml",
    "packaging",
    "packaging.requirements",
    "utils",
    "utils.venv",
    "utils.environment_lifecycle",
    "utils.config",
    "utils.config_map",
    "utils.config_migration",
    "utils.cropper",
    "utils.task_manager",
    "utils.execute",
    "utils.auto_update",
    "utils.package_network",
    "utils.record",
    "utils.deepseek_thinking",
    "utils.app_logging",
    "utils.process_utils",
    "utils.gpu_runtime",
    "utils.display",
    "utils.ui_jobs",
    "utils.translation_ui",
    "utils.llm_ui",
    "utils.llm_api",
    "utils.backend_settings",
    "utils.reasoning",
    "utils.links",
    "utils.version",
    "utils.wheel_focus",
    "utils.checkmarks",
    "utils.plugin_install",
    "utils.windows_drag",
    "utils.translation_quality",
    "utils.desktop_translation",
    "utils.task_control",
    "runtime_progress",
    "runtime_isolation",
]

a = Analysis(
    [os.path.join(SPECDIR, "launcher.py")],
    pathex=[SPECDIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["conda", "matplotlib", "numpy.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="zotero-pdf2zh-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed: logs go to the GUI; flip to True for debugging
    icon=os.path.join(BRANDING, 'app.ico'),
    version=VSVersionInfo(ffi=FixedFileInfo(filevers=release_tuple, prodvers=release_tuple,
        mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0,0)),
        kids=[StringFileInfo([StringTable('040904B0', [
            StringStruct('CompanyName', 'erxinzi'), StringStruct('FileDescription', 'PDF2zh'),
            StringStruct('ProductName', 'PDF2zh'), StringStruct('InternalName', 'PDF2zh'),
            StringStruct('OriginalFilename', 'zotero-pdf2zh-server.exe'),
            StringStruct('FileVersion', release_version), StringStruct('ProductVersion', release_version)])]),
            VarFileInfo([VarStruct('Translation', [0x409, 1200])])]),
    manifest=os.path.join(SPECDIR, "windows.manifest"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="zotero-pdf2zh-server",
)
