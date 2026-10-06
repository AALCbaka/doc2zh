# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把服务 + 翻译引擎打成一个免安装目录。

构建：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm build/pdf-translator.spec
产物：
    dist/PDF中英对照翻译/PDF中英对照翻译.exe
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent      # noqa: F821  (PyInstaller 注入)
while ROOT.name and not (ROOT / "launcher.py").exists():
    ROOT = ROOT.parent

datas = [
    (str(ROOT / "webapp" / "static"), "webapp/static"),
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD-PARTY-NOTICES.md"), "."),
]
for optional in ("docs", "samples"):
    p = ROOT / optional
    if p.exists():
        datas.append((str(p), optional))

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "multipart",
    "python_multipart",
    "peewee",
    "tenacity",
    "tiktoken",
    "tiktoken_ext",
    "tiktoken_ext.openai_public",
    "onnxruntime",
    "fitz",
    "pymupdf",
    "pdf2zh_next",
    "pdf2zh_next.high_level",
    "pdf2zh_next.config.model",
    "pdf2zh_next.config.translate_engine_model",
    "babeldoc",
    "babeldoc.format.pdf.high_level",
    "babeldoc.format.pdf.translation_config",
    "babeldoc.assets.assets",
    "babeldoc.docvision.doclayout",
    "babeldoc.translator.translator",
    "webapp.server",
    "webapp.jobs",
    "webapp.translate_job",
    "webapp.prepare_assets",
    "webapp.pdf_preview",
    "webapp.glossary",
    "webapp.office_convert",
    "pymupdf",
    "fitz",
    # Office → PDF：调用本机 Office 的 COM 接口（pywin32）
    "win32com",
    "win32com.client",
    "win32com.client.dynamic",
    "pythoncom",
    "pywintypes",
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
    # 原生窗口（pywebview）：Windows 后端走 .NET + WebView2，这些是动态加载的
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr_loader",
    "pythonnet",
    "clr",
]

# 这些包用 importlib 动态导入子模块，静态分析抓不到，必须整包收集：
#   bitstring    -> importlib.import_module('bitstring.bitstore_bitarray')
#   pdf2zh_next  -> importlib.import_module('pdf2zh_next.translator.translator_impl.<引擎名>')
#   babeldoc     -> 各类后端/模型按名字动态导入
# 漏掉任何一个都会在翻译启动瞬间 ModuleNotFoundError。
DYNAMIC_PACKAGES = [
    "bitstring",
    "charset_normalizer",
    "tiktoken_ext",
    "pdf2zh_next",
    "pdf2zh_next.translator",
    "pdf2zh_next.translator.translator_impl",  # 命名空间包，必须显式指定
    "babeldoc",
]
for pkg in DYNAMIC_PACKAGES:
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:  # noqa: BLE001
        pass

# 守护检查：确认关键动态模块确实被收集到，否则静默漏收会让 exe 到了翻译那一步才炸
MUST_COLLECT = [
    "bitstring.bitstore_bitarray",
    "pdf2zh_next.translator.translator_impl.openai",
    "pdf2zh_next.translator.utils",
]
_missing = [m for m in MUST_COLLECT if m not in hiddenimports]
if _missing:
    raise SystemExit(
        "打包前置检查失败：以下动态导入模块未被收集，请检查 collect_submodules 配置，"
        f"否则打包产物会在运行时崩溃 -> {_missing}"
    )

# 体积控制：这些包在翻译链路里用不到
# 注意：不要加 sklearn —— 实测 babeldoc 在 import 阶段就需要它（自检会直接报错）
excludes = [
    "gradio", "gradio_client", "gradio_pdf", "gradio_i18n",
    "matplotlib", "IPython", "notebook", "jupyter", "pytest",
    "PyQt5", "PySide2", "PySide6", "tkinter",
    "torch", "torchvision", "tensorflow",
    "ollama", "xinference_client", "deepl", "azure",
    "tencentcloud", "pandas", "sqlalchemy",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDF中英对照翻译",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PDF中英对照翻译",
)
