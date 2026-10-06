"""PyInstaller 打包入口：一个 exe 同时承担「启动应用窗口」和「执行单个翻译任务」两种角色。

为什么要一个 exe 扮两个角色：
    打包后 sys.executable 指向 exe 本身，不再是 python.exe。
    任务调度需要用子进程隔离单次翻译（可取消、崩溃不拖垮服务），
    所以子进程用 `本exe --run-job ...` 的方式重新拉起自己，在进程内分发到翻译逻辑。

用法：
    PDF中英对照翻译.exe                 启动应用（默认：原生窗口，走系统 WebView2）
    PDF中英对照翻译.exe --browser       改用系统默认浏览器打开
    PDF中英对照翻译.exe --run-job ...   执行单个翻译任务（由程序内部调用）
    PDF中英对照翻译.exe --warmup        只下载模型资源
    PDF中英对照翻译.exe --selftest      打包产物自检（模块导入 + 模型加载）
    PDF中英对照翻译.exe --window-test   窗口自检（渲染若干秒后自动关闭）

环境变量：
    PORT                 服务端口（默认 8848）
    HOST                 监听地址（默认 127.0.0.1）
    PDF_TRANSLATOR_UI    window（默认）| browser
    SOURCE_URL           源码地址，用于界面与启动横幅的法律声明
    DEEPSEEK_API_KEY     预置 API Key，界面里可留空
"""
from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path

# 对外展示的源码地址（AGPL-3.0 第 13 条：网络交互界面须提供取得对应源码的途径）
SOURCE_URL = os.environ.get("SOURCE_URL", "https://github.com/AALCbaka/doc2zh")
LICENSE_NAME = "GNU Affero General Public License v3.0 (AGPL-3.0)"
READY_THRESHOLD_MB = 200

LEGAL_NOTICE = f"""\
本程序为自由软件，按 {LICENSE_NAME} 发布，不提供任何担保。
源码地址：{SOURCE_URL}
第三方组件许可：见随附 THIRD-PARTY-NOTICES.md
核心上游：BabelDOC 与 PDFMathTranslate-next（均为 AGPL-3.0，Copyright (C) funstory.ai limited 等）"""


def resource_root() -> Path:
    """静态资源根目录（源码运行=仓库根；打包运行=解包目录）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """程序所在目录（打包后=exe 同级目录；源码=仓库根）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def setup_frozen_dll_paths() -> None:
    """打包运行时修正原生扩展的 DLL 搜索路径。

    背景：hyperscan 的 _hs_ext.pyd 依赖带哈希名的 MSVC 运行库
    （msvcp140-a4c22...dll），PyInstaller 把它收进了 numpy.libs/ 之类
    的子目录，而这些目录不在 DLL 搜索路径里，导致 ImportError。
    """
    if not getattr(sys, "frozen", False):
        return
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    dirs = [base]
    try:
        dirs += [d for d in base.iterdir() if d.is_dir() and (d.name.endswith(".libs") or d.name in {"hyperscan", "pymupdf", "onnxruntime", "cv2", "scipy.libs"})]
    except OSError:
        pass
    for d in dirs:
        try:
            os.add_dll_directory(str(d))
        except (OSError, AttributeError):
            pass
    os.environ["PATH"] = os.pathsep.join([str(d) for d in dirs] + [os.environ.get("PATH", "")])


def ensure_utf8() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def cache_size_mb() -> float:
    cache = Path.home() / ".cache" / "babeldoc"
    if not cache.exists():
        return 0.0
    return sum(f.stat().st_size for f in cache.rglob("*") if f.is_file()) / 1024 / 1024


def find_offline_assets() -> Path | None:
    """在上游官方约定的位置查找 offline_assets_*.zip（文件名含内容哈希，不可改名）。"""
    candidates = [
        app_dir(),
        app_dir() / "assets",
        app_dir() / "offline_assets",
        resource_root(),
        resource_root() / "assets",
    ]
    for d in candidates:
        try:
            if d.is_dir():
                for f in sorted(d.glob("offline_assets_*.zip")):
                    return f
        except OSError:
            pass
    return None


def restore_offline_assets() -> bool:
    """按官方机制恢复离线资源包。已就绪时直接跳过。"""
    if cache_size_mb() > READY_THRESHOLD_MB:
        return True
    bundle = find_offline_assets()
    if bundle is None:
        return False
    print(f"[资源] 发现离线资源包：{bundle.name}")
    print("[资源] 正在恢复版面模型与字体（约 340MB，首次启动需要 1-3 分钟）…")
    try:
        from babeldoc.assets import assets

        assets.restore_offline_assets_package(bundle)
    except Exception as exc:  # noqa: BLE001
        print(f"[资源] 离线恢复失败：{type(exc).__name__}: {exc}")
        print("[资源] 程序会尝试联网下载所需资源。")
        return False
    mb = cache_size_mb()
    if mb > READY_THRESHOLD_MB:
        print(f"[资源] 离线资源就绪（{mb:.0f}MB）")
        return True
    print(f"[资源] 恢复后仍不完整（{mb:.0f}MB），将尝试联网补齐。")
    return False


def save_selftest() -> int:
    """自检「下载→保存」链路中 Python 侧的编解码与写盘（不弹对话框）。"""
    import base64
    import tempfile

    api = DesktopApi()
    payload = b"%PDF-1.7 fake pdf for save-path selftest"
    b64 = base64.b64encode(payload).decode()
    try:
        n = api._decode_check(b64)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ base64 解码失败：{exc}")
        return 1
    print(f"  ✓ base64 解码正常（{n} 字节）")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "测试 输出.pdf"
            target.write_bytes(base64.b64decode(b64))
            ok = target.read_bytes() == payload
            print(f"  ✓ 写盘正常（含空格与非 ASCII 文件名）：{ok}")
            return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 写盘失败：{exc}")
        return 1


def selftest() -> int:
    """打包产物自检：逐个导入关键模块，把「运行时才炸」的问题提前暴露。"""
    print(f"[自检] 运行模式：{'打包' if getattr(sys, 'frozen', False) else '源码'}")
    print(f"[自检] 解释器：{sys.executable}")
    print(f"[自检] 资源根目录：{resource_root()}")
    critical = [
        ("bitstring", "bitstring"),
        ("hyperscan", "hyperscan"),
        ("pymupdf", "fitz"),
        ("onnxruntime", "onnxruntime"),
        ("babeldoc 主流程", "babeldoc.format.pdf.high_level"),
        ("pdf2zh-next 入口", "pdf2zh_next.high_level"),
        ("pdf2zh-next 配置", "pdf2zh_next.config.model"),
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("表单解析", "multipart"),
        ("原生窗口 (pywebview)", "webview"),
        ("窗口 Windows 后端", "webview.platforms.winforms"),
        ("窗口 WebView2 后端", "webview.platforms.edgechromium"),
    ]
    failed = []
    for label, mod in critical:
        try:
            __import__(mod)
            print(f"  ✓ {label}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {label}: {type(exc).__name__}: {exc}")
            failed.append(label)

    # WebView2 运行时检测（原生窗口依赖它；Windows 11 通常自带）
    try:
        import winreg

        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        ]
        version = None
        for hive, path in keys:
            try:
                with winreg.OpenKey(hive, path) as key:
                    version = winreg.QueryValueEx(key, "pv")[0]
                    break
            except OSError:
                continue
        if version:
            print(f"  ✓ WebView2 运行时 {version}")
        else:
            print("  ! 未检测到 WebView2 运行时：原生窗口不可用，程序会自动回退到浏览器")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! WebView2 检测跳过：{exc}")

    try:
        from babeldoc.assets import assets
        from babeldoc.docvision.doclayout import DocLayoutModel

        model = DocLayoutModel.load_onnx()
        print(f"  ✓ 版面模型加载（{type(model).__name__}）")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 版面模型加载：{type(exc).__name__}: {exc}")
        failed.append("版面模型")
    print(f"[自检] 资源缓存：{cache_size_mb():.0f} MB")

    # Office 转换能力自检：PPT/Word 能否自动转 PDF，取决于本机 Office
    try:
        from webapp.office_convert import office_available

        info = office_available()
        if info.get("powerpoint") and info.get("word"):
            print("  ✓ Office 转换：PowerPoint + Word 可用（PPT/Word 可直接丢进来翻译）")
        elif info.get("powerpoint") or info.get("word"):
            have = "、".join("PowerPoint" if k == "powerpoint" else "Word"
                            for k in ("powerpoint", "word") if info.get(k))
            print(f"  ! Office 转换：仅检测到 {have}（另一类文档需先手动转 PDF）")
        elif info.get("libreoffice"):
            print(f"  ! 未检测到 Microsoft Office，将用 LibreOffice：{info['libreoffice']}")
        else:
            print("  ! 未检测到 Microsoft Office / LibreOffice：PPT、Word 无法自动转 PDF")
        print(f"    Microsoft Print to PDF 打印机：{'有' if info.get('printer') else '无'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! Office 转换不可用：{type(exc).__name__}: {exc}")

    # 数据目录自检：任务记录与术语表都必须落在可写位置
    try:
        from webapp.jobs import JOBS_DIR

        print(f"  ✓ 任务数据目录：{JOBS_DIR}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 任务数据目录不可用：{type(exc).__name__}: {exc}")
        failed.append("任务数据目录")

    try:
        from webapp.glossary import GLOSSARY_DIR, INDEX_FILE, store

        sets = store.list_sets()
        print(f"  ✓ 术语表目录：{GLOSSARY_DIR}")
        print(f"    索引存在={INDEX_FILE.exists()}，已有术语集 {len(sets)} 个"
              + (f"：{', '.join(s['name'] for s in sets)}" if sets else ""))
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 术语表目录不可用：{type(exc).__name__}: {exc}")
        failed.append("术语表目录")

    if failed:
        print(f"[自检] 失败项：{', '.join(failed)}")
        return 1
    print("[自检] 全部通过")
    return 0


def main() -> int:
    multiprocessing.freeze_support()
    ensure_utf8()
    setup_frozen_dll_paths()

    argv = sys.argv[1:]

    if "--selftest" in argv:
        return selftest()

    if "--save-selftest" in argv:
        return save_selftest()

    if "--warmup" in argv:
        root = resource_root()
        sys.path.insert(0, str(root))
        sys.path.insert(0, str(root / "webapp"))
        from webapp.prepare_assets import main as warmup_main

        return warmup_main()

    if "--run-job" in argv:
        root = resource_root()
        sys.path.insert(0, str(root))
        sys.path.insert(0, str(root / "webapp"))
        from webapp import translate_job

        idx = argv.index("--run-job")
        sys.argv = [sys.argv[0], *argv[idx + 1:]]
        import asyncio

        parsed = translate_job.build_parser().parse_args()
        if sys.platform == "win32":
            multiprocessing.set_start_method("spawn", force=True)
        return asyncio.run(translate_job.run(parsed))

    # 默认：启动网页服务
    root = resource_root()
    sys.path.insert(0, str(root))
    return run_server(root)


def _boot_log(msg: str) -> None:
    """把启动过程写进日志文件。

    打包成 GUI 程序后 stdout 会被缓冲，出错时看不到任何输出，
    排查"卡住不动"只能靠这个文件。
    """
    try:
        import time as _t

        with (app_dir() / "启动日志.txt").open("a", encoding="utf-8") as fh:
            fh.write(f"{_t.strftime('%H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def run_server(root: Path) -> int:
    import threading
    import time
    import urllib.request

    _boot_log("=== 启动开始 ===")
    port = int(os.environ.get("PORT", "8848"))
    host = os.environ.get("HOST", "127.0.0.1")
    ui_mode = (os.environ.get("PDF_TRANSLATOR_UI") or "window").lower()
    if "--browser" in sys.argv:
        ui_mode = "browser"

    print("=" * 62)
    print("  PDF 中英对照翻译  ·  DeepSeek + BabelDOC")
    print("=" * 62)
    print(LEGAL_NOTICE)
    print("=" * 62)

    # 资源准备：优先用随包附带的官方离线资源包，否则联网下载
    if cache_size_mb() <= READY_THRESHOLD_MB:
        _boot_log(f"资源检查：{cache_size_mb():.0f}MB，尝试恢复")
        if not restore_offline_assets():
            print(f"\n[提示] 版面模型与字体资源尚未就绪（当前 {cache_size_mb():.0f}MB，需要约 340MB）。")
            print("       在界面里点「下载模型资源」即可，或用 --warmup 参数启动本程序自动下载。")
    else:
        print(f"\n[资源] 版面模型已就绪（{cache_size_mb():.0f}MB）")
    _boot_log("资源检查完成")

    url = f"http://{host}:{port}"

    # 服务跑在后台线程，主线程留给窗口
    import uvicorn

    _boot_log("uvicorn 已导入")

    from webapp.server import app

    _boot_log("FastAPI 应用已导入")
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    _boot_log("uvicorn 线程已启动")

    def wait_ready(timeout: float = 60.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{url}/api/health", timeout=2):
                    return True
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
        return False

    _boot_log("等待服务就绪…")
    if not wait_ready():
        _boot_log("!! 服务就绪等待超时")
        print("[错误] 本地服务启动超时，改用浏览器打开并保留日志。", file=sys.stderr)
    else:
        _boot_log("服务已就绪")

    print(f"[服务] {url}")
    print("[提示] 首次使用请在界面中填写 DeepSeek API Key（或设置环境变量 DEEPSEEK_API_KEY）")

    if ui_mode == "browser":
        print("[界面] 浏览器模式（--browser）")
        return _open_browser(url) and _block_forever()

    auto_close = 0.0
    if "--window-test" in sys.argv:
        try:
            auto_close = float(os.environ.get("WINDOW_TEST_SECONDS", "8"))
        except ValueError:
            auto_close = 8.0

    opened = _open_native_window(url, auto_close_seconds=auto_close)
    if opened:
        return 0
    if auto_close > 0:
        print("[自检] 原生窗口不可用")
        return 1
    print("[界面] 原生窗口不可用，回退到浏览器。")
    _open_browser(url)
    return _block_forever()


def _block_forever() -> int:
    """浏览器模式下保持进程存活（Ctrl+C 退出）。"""
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[停止] 已退出")
    return 0


def _open_browser(url: str) -> bool:
    try:
        import webbrowser

        return webbrowser.open(url)
    except Exception:  # noqa: BLE001
        return False


class DesktopApi:
    """暴露给界面 JS 的接口：让「下载」走系统原生「另存为」对话框。

    背景：WebView2 对内嵌页面的 <a download> 支持不稳定——用户点了下载没反应。
    这里把文件内容通过 JS 传进来，由 Python 弹系统保存框并写盘，行为可预期。
    """

    def __init__(self) -> None:
        self.window = None

    def set_window(self, window) -> None:
        self.window = window

    def save_file(self, filename: str, base64_data: str, subdir: str = "") -> dict:
        import base64
        import time

        if not self.window:
            return {"ok": False, "message": "窗口未就绪"}
        safe = "".join(ch for ch in (filename or "output.pdf") if ch not in '\\/:*?"<>|').strip()
        if not safe:
            safe = "output.pdf"
        try:
            result = self.window.create_file_dialog(
                30,  # webview.FileDialog.SAVE
                directory=str(Path.home() / "Downloads"),
                save_filename=safe,
                file_types=("PDF 文件 (*.pdf)", "CSV 文件 (*.csv)", "所有文件 (*.*)"),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"无法打开保存对话框：{exc}"}

        target = result[0] if isinstance(result, (list, tuple)) and result else result
        if not target:
            return {"ok": False, "cancelled": True, "message": "已取消保存"}

        try:
            data = base64.b64decode(base64_data)
            out = Path(target)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            print(f"[下载] 已保存 {len(data) / 1024:.0f} KB -> {out}")
            return {"ok": True, "path": str(out), "bytes": len(data)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"写入失败：{exc}"}

    def describe(self) -> dict:
        return {"mode": "window", "can_save": True}

    def _decode_check(self, base64_data: str) -> int:
        """自检用：只验证 base64 能否正确解码，不涉及文件对话框。"""
        import base64

        return len(base64.b64decode(base64_data))


DESKTOP_API: DesktopApi | None = None


def _open_native_window(url: str, auto_close_seconds: float = 0.0) -> bool:
    """用系统 WebView2 打开原生窗口；失败返回 False 由调用方降级。

    auto_close_seconds > 0 时，窗口显示指定秒数后自动关闭（用于 --window-test 自检）。
    """
    global DESKTOP_API
    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        print(f"[界面] pywebview 不可用：{type(exc).__name__}: {exc}")
        return False

    loading = (
        "<!doctype html><meta charset='utf-8'>"
        "<body style=\"margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
        "font-family:'Segoe UI','Microsoft YaHei',system-ui;background:#0f172a;color:#cbd5e1\">"
        "<div style='text-align:center'>"
        "<div style='font-size:15px;letter-spacing:.5px'>正在启动本地服务…</div>"
        "<div style='margin-top:10px;font-size:12px;color:#64748b'>首次启动需要恢复版面模型（约 340MB），请稍候</div>"
        "</div></body>"
    )

    try:
        DESKTOP_API = DesktopApi()
        window = webview.create_window(
            "PDF 中英对照翻译 · DeepSeek + BabelDOC",
            html=loading,
            width=1320,
            height=880,
            min_size=(960, 640),
            text_select=True,
            confirm_close=False,
            background_color="#f5f7fb",
            js_api=DESKTOP_API,
        )
        DESKTOP_API.set_window(window)

        def navigate_when_ready(win):
            import time
            import urllib.request

            deadline = time.time() + 180
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"{url}/api/health", timeout=2):
                        break
                except Exception:  # noqa: BLE001
                    time.sleep(0.5)
            try:
                win.load_url(url)  # 服务就绪后切到真实界面
            except Exception as exc:  # noqa: BLE001
                print(f"[界面] 页面跳转失败：{exc}", file=sys.stderr)

            if auto_close_seconds > 0:
                time.sleep(auto_close_seconds)
                try:
                    print("[自检] 窗口已正常渲染，自动关闭")
                    win.destroy()
                except Exception as exc:  # noqa: BLE001
                    print(f"[自检] 关闭窗口失败：{exc}", file=sys.stderr)

        print("[界面] 原生应用窗口（WebView2）")
        webview.start(navigate_when_ready, window, debug=False, private_mode=False)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[界面] 原生窗口启动失败：{type(exc).__name__}: {exc}")
        return False


if __name__ == "__main__":
    sys.exit(main())
