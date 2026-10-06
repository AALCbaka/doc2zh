"""Office 文档 → PDF 转换（PPT/Word 等），供翻译链路直接吃进去。

为什么不直接用「Microsoft Print to PDF」当主路线
------------------------------------------------
两条路都能得到 PDF，但差别很大：

| | Office 原生导出（默认） | Microsoft Print to PDF |
|---|---|---|
| 调用方式 | 纯 API（`SaveAs`/`ExportAsFixedFormat`） | 打印 + 拦截「另存为」对话框 |
| 输出路径 | 确定，代码直接给 | 要靠窗口自动化填对话框 |
| 对话框 | 不弹 | 每次弹一个，需前台交互 |
| 无人值守/最小化 | 可用 | 容易失败（多屏、远程会话、被遮挡） |
| 结果质量 | 保留文字可选中、书签、超链接 | 走打印驱动，可能丢超链接 |

所以 `mode="auto"` 最终落到原生导出；打印机路线保留为**显式可选**
（`mode="printer"`，界面里对应「打印成 PDF（兼容模式）」），用于原生导出不可用的场景。

支持格式：.ppt .pptx .pps .ppsx .pot .potx .pptm（PowerPoint）
          .doc .docx .docm .rtf（Word）
"""
from __future__ import annotations

import ctypes
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Office 常量
PP_SAVE_AS_PDF = 32          # ppSaveAsPDF
WD_EXPORT_FORMAT_PDF = 17    # wdExportFormatPDF
WD_DO_NOT_SAVE_CHANGES = 0
MSO_AUTOMATION = 2           # msoAutomationSecurityForceDisable：禁用宏

POWERPOINT_EXTS = {".ppt", ".pptx", ".pps", ".ppsx", ".pot", ".potx", ".pptm", ".ppsm"}
WORD_EXTS = {".doc", ".docx", ".docm", ".dot", ".dotx", ".rtf"}
OFFICE_EXTS = POWERPOINT_EXTS | WORD_EXTS


class ConversionError(RuntimeError):
    pass


def is_office_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in OFFICE_EXTS


def office_available() -> dict:
    """探测本机可用的转换能力（供健康检查/界面提示用）。"""
    info = {"powerpoint": False, "word": False, "libreoffice": None, "printer": False}
    try:
        import win32com.client  # noqa: F401

        info["win32com"] = True
    except Exception:  # noqa: BLE001
        info["win32com"] = False

    if info.get("win32com"):
        for key, prog in (("powerpoint", "PowerPoint.Application"), ("word", "Word.Application")):
            try:
                import winreg

                winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog).Close()
                info[key] = True
            except OSError:
                info[key] = False
            except Exception:  # noqa: BLE001
                info[key] = False

    soffice = shutil.which("soffice") or glob.glob(r"C:\Program Files\LibreOffice\program\soffice.exe")
    if soffice:
        info["libreoffice"] = soffice[0] if isinstance(soffice, list) else soffice

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Printer | Where-Object {$_.Name -like '*Print to PDF*'}).Name"],
            capture_output=True, text=True, timeout=20,
        )
        info["printer"] = "Print to PDF" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        info["printer"] = False
    return info


# --------------------------------------------------------------- 主入口
def convert_to_pdf(src: str | Path, dst: str | Path, *, mode: str = "auto",
                   timeout: int = 300, on_log=None) -> Path:
    """把 Office 文档转成 PDF。返回输出路径。

    mode: auto（默认，优先 Office 原生导出，失败再试打印机）
          native（只用 Office 原生导出）
          printer（用 Microsoft Print to PDF 虚拟打印机）
          libreoffice（用 LibreOffice，无 Office 时的退路）
    """
    src = Path(src).resolve()
    dst = Path(dst).resolve()
    if not src.exists():
        raise ConversionError(f"源文件不存在：{src}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    ext = src.suffix.lower()
    if ext not in OFFICE_EXTS:
        raise ConversionError(f"不支持的格式：{ext}（支持 {', '.join(sorted(OFFICE_EXTS))}）")

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    attempts: list[str] = []
    if mode == "native":
        attempts = ["native"]
    elif mode == "printer":
        attempts = ["printer"]
    elif mode == "libreoffice":
        attempts = ["libreoffice"]
    else:
        # auto：原生导出优先（可靠），失败再试打印机，最后 LibreOffice
        attempts = ["native", "printer", "libreoffice"]

    last_err: Exception | None = None
    for attempt in attempts:
        try:
            log(f"转换方式：{attempt}")
            if attempt == "native":
                _convert_native(src, dst, timeout=timeout, on_log=log)
            elif attempt == "printer":
                _convert_via_printer(src, dst, timeout=timeout, on_log=log)
            else:
                _convert_libreoffice(src, dst, timeout=timeout, on_log=log)
            if dst.exists() and dst.stat().st_size > 0:
                log(f"已生成 PDF：{dst.name}（{dst.stat().st_size / 1024:.0f} KB）")
                return dst
            raise ConversionError("转换命令结束但没有产出文件")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log(f"{attempt} 失败：{type(exc).__name__}: {str(exc)[:160]}")
    raise ConversionError(f"全部转换方式都失败：{last_err}")


# --------------------------------------------------------------- Office 原生导出
def _convert_native(src: Path, dst: Path, *, timeout: int, on_log) -> None:
    import pythoncom

    ext = src.suffix.lower()
    pythoncom.CoInitialize()
    try:
        if ext in POWERPOINT_EXTS:
            _ppt_native(src, dst, on_log)
        else:
            _word_native(src, dst, on_log)
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def _ppt_native(src: Path, dst: Path, on_log) -> None:
    import win32com.client

    app = win32com.client.DispatchEx("PowerPoint.Application")
    presentation = None
    try:
        try:
            app.DisplayAlerts = 1  # ppAlertsNone 的兼容写法：1=ppAlertsNone? 见下
        except Exception:  # noqa: BLE001
            pass
        app.DisplayAlerts = 1
        # PowerPoint 不允许把主窗口设为不可见，只能在最小化状态下工作
        try:
            app.WindowState = 2  # ppWindowMinimized
        except Exception:  # noqa: BLE001
            pass
        presentation = app.Presentations.Open(
            str(src), ReadOnly=True, Untitled=False, WithWindow=False
        )
        presentation.SaveAs(str(dst), PP_SAVEAS_PDF)
    finally:
        if presentation is not None:
            try:
                presentation.Close()
            except Exception:  # noqa: BLE001
                pass
        try:
            app.Quit()
        except Exception:  # noqa: BLE001
            pass
        _safe_uninit()


PP_SAVEAS_PDF = PP_SAVE_AS_PDF


def _word_native(src: Path, dst: Path, on_log) -> None:
    import win32com.client

    app = win32com.client.DispatchEx("Word.Application")
    doc = None
    try:
        app.Visible = False
        app.DisplayAlerts = 0
        try:
            app.AutomationSecurity = MSO_AUTOMATION
        except Exception:  # noqa: BLE001
            pass
        doc = app.Documents.Open(
            str(src), ConfirmConversions=False, ReadOnly=True,
            AddToRecentFiles=False, Visible=False,
            PasswordDocument="\x00", WritePasswordDocument="\x00",
        )
        doc.ExportAsFixedFormat(str(dst), WD_EXPORT_FORMAT_PDF)
    finally:
        if doc is not None:
            try:
                doc.Close(WD_DO_NOT_SAVE_CHANGES)
            except Exception:  # noqa: BLE001
                pass
        try:
            app.Quit()
        except Exception:  # noqa: BLE001
            pass
        _safe_uninit()


def _safe_uninit() -> None:
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------- 虚拟打印机路线
def _convert_via_printer(src: Path, dst: Path, *, timeout: int, on_log) -> None:
    """用 Microsoft Print to PDF：打印 → 在弹出的「另存为」对话框里填路径。

    需要交互桌面会话；对话框标题随 Office 语言变化，这里用模糊匹配 + 兜底 SendKeys。
    """
    import threading

    if not _printer_available():
        raise ConversionError("系统里没有 'Microsoft Print to PDF' 打印机")

    printer_target = dst
    watcher_error: list[Exception] = []

    def watcher() -> None:
        """后台线程：等待「将打印输出另存为」对话框并完成它。"""
        import time as _t

        import win32con
        import win32gui

        deadline = _t.time() + timeout
        titles = ("将打印输出另存为", "Save Print Output As", "打印输出另存为")
        while _t.time() < deadline:
            hit = []

            def cb(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                text = win32gui.GetWindowText(hwnd)
                if any(t in text for t in titles):
                    hit.append(hwnd)
                return True

            try:
                win32gui.EnumWindows(cb, None)
            except Exception as exc:  # noqa: BLE001
                watcher_error.append(exc)
                return
            if hit:
                hwnd = hit[0]
                try:
                    # 编辑框里填完整目标路径，然后确认
                    edit = _find_edit(hwnd)
                    if edit:
                        win32gui.SendMessage(edit, win32con.WM_SETTEXT, 0, str(printer_target))
                    win32gui.SetForegroundWindow(hwnd)
                    _t.sleep(0.3)
                    _click_save(hwnd)
                    return
                except Exception as exc:  # noqa: BLE001
                    watcher_error.append(exc)
                    return
            _t.sleep(0.4)

    thread = threading.Thread(target=watcher, daemon=True)
    thread.start()

    try:
        _print_via_com(src, on_log)
    finally:
        thread.join(timeout=min(timeout, 120))

    if watcher_error:
        raise ConversionError(f"打印对话框处理失败：{watcher_error[0]}")
    if not dst.exists():
        raise ConversionError("打印机未产出文件（可能被对话框拦截或已取消）")


def _find_edit(hwnd: int) -> int | None:
    import win32gui

    result = []

    def cb(child, _):
        cls = win32gui.GetClassName(child)
        if "Edit" in cls:
            result.append(child)
        return True

    win32gui.EnumChildWindows(hwnd, cb, None)
    return result[0] if result else None


def _click_save(hwnd: int) -> None:
    """点「保存」按钮：优先找按钮控件，找不到就用回车兜底。"""
    import win32con
    import win32gui

    buttons = []

    def cb(child, _):
        if win32gui.GetClassName(child) == "Button":
            buttons.append((child, win32gui.GetWindowText(child)))
        return True

    win32gui.EnumChildWindows(hwnd, cb, None)
    for child, text in buttons:
        if text.strip() in ("保存", "Save", "&Save", "保存(S)"):
            win32gui.SendMessage(child, win32con.BM_CLICK, 0, 0)
            return
    # 兜底：回车
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)


def _print_via_com(src: Path, on_log) -> None:
    """用 Office 的 PrintOut 让文档走虚拟打印机。

    注意：Word 的 PrintOut 参数极多且只能按位置传，这里用「打印到文件」的方式
    （PrintToFile=True + OutputFileName），比依赖对话框更可靠。
    """
    import win32com.client

    ext = src.suffix.lower()
    if ext in POWERPOINT_EXTS:
        app = win32com.client.DispatchEx("PowerPoint.Application")
        pres = None
        try:
            try:
                app.WindowState = 2  # 最小化
            except Exception:  # noqa: BLE001
                pass
            pres = app.Presentations.Open(str(src), ReadOnly=True, WithWindow=False)
            # PrintOut(From, To, PrintToFile, Copies, Collate)
            pres.PrintOut(1, 9999, "Microsoft Print to PDF", 1)
            time.sleep(3)
        finally:
            if pres is not None:
                try:
                    pres.Close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass
            _safe_uninit()
    else:
        app = win32com.client.DispatchEx("Word.Application")
        doc = None
        try:
            # 实测：Word 的 PrintOut 要求「文档窗口处于活动状态」，
            # 因此这里必须把 Word 显示出来（会闪一下窗口），
            # 这也是不把 printer 作为默认路线的原因之一。
            app.Visible = True
            app.DisplayAlerts = 0
            doc = app.Documents.Open(str(src), ReadOnly=True, Visible=True,
                                     PasswordDocument="\x00", WritePasswordDocument="\x00")
            try:
                doc.Activate()
            except Exception:  # noqa: BLE001
                pass
            doc.PrintOut(
                False,          # Background
                False,          # Append
                -1,             # Range: wdPrintAllDocument
                "",             # OutputFileName
                "Microsoft Print to PDF",  # Item
            )
            time.sleep(3)
        finally:
            if doc is not None:
                try:
                    doc.Close(WD_DO_NOT_SAVE_CHANGES)
                except Exception:  # noqa: BLE001
                    pass
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass
            _safe_uninit()


def _printer_available() -> bool:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Printer | Where-Object {$_.Name -like '*Print to PDF*'}).Name"],
            capture_output=True, text=True, timeout=20,
        )
        return "Print to PDF" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------- LibreOffice 退路
def _convert_libreoffice(src: Path, dst: Path, *, timeout: int, on_log) -> None:
    soffice = office_available().get("libreoffice")
    if not soffice:
        raise ConversionError("未安装 LibreOffice")
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", tmp, str(src)],
            capture_output=True, text=True, timeout=timeout,
        )
        produced = list(Path(tmp).glob("*.pdf"))
        if not produced:
            raise ConversionError(f"LibreOffice 未产出 PDF：{(proc.stderr or proc.stdout or '')[:200]}")
        shutil.move(str(produced[0]), str(dst))
