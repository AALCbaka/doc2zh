"""装配可分发产物：打包产物 + 修复原生依赖 + 官方离线资源包 + 合规文件。

用法（先跑 PyInstaller，再跑本脚本）：
    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean build\\pdf-translator.spec
    .venv\\Scripts\\python.exe build\\assemble_dist.py [--skip-assets]

为什么需要修复原生依赖：
    PyInstaller 会把带哈希名的 MSVC 运行库（如 msvcp140-a4c22...dll）收进
    numpy.libs/ 之类的子目录。而 hyperscan 的扩展 _hs_ext.pyd 是按**完整路径**加载的，
    Windows 此时只在「该 .pyd 所在目录」和 PATH 中查找依赖，于是报
    "DLL load failed while importing _hs_ext"。
    解决：把这些运行库复制到 _internal 根目录与 hyperscan/ 目录各一份。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = "PDF中英对照翻译"
DIST = ROOT / "dist" / DIST_NAME
INTERNAL = DIST / "_internal"
OFFLINE_DIR = ROOT / "build" / "offline"
COMPLIANCE_FILES = ["LICENSE", "THIRD-PARTY-NOTICES.md", "README.md"]


def log(msg: str) -> None:
    print(f"[assemble] {msg}")


def copy_native_runtimes() -> int:
    """把散落在 *.libs 里的 MSVC 运行库补到扩展模块能找到的位置。"""
    if not INTERNAL.exists():
        log("!! 找不到 _internal 目录，跳过原生依赖修复")
        return 0
    candidates: list[Path] = []
    for libs in INTERNAL.glob("*.libs"):
        candidates += [p for p in libs.glob("*.dll") if p.is_file()]
    # PyInstaller 也可能放在 _internal 根目录
    candidates += [p for p in INTERNAL.glob("msvc*.dll") if p.is_file()]
    if not candidates:
        log("未发现需要搬运的 MSVC 运行库")
        return 0

    targets = [INTERNAL]
    hyp = INTERNAL / "hyperscan"
    if hyp.is_dir():
        targets.append(hyp)
    # 其它含 .pyd 的包目录也放一份（onnxruntime/cv2 等各自带依赖，通常无需处理）
    for pkg in ("pymupdf", "onnxruntime", "cv2", "scipy", "skimage"):
        d = INTERNAL / pkg
        if d.is_dir():
            targets.append(d)

    copied = 0
    for src in candidates:
        for dst_dir in targets:
            dst = dst_dir / src.name
            if dst.exists():
                continue
            try:
                shutil.copy2(src, dst)
                copied += 1
            except OSError:
                pass
    log(f"原生运行库补位完成，复制 {copied} 份（去重后源文件 {len({c.name for c in candidates})} 个）")
    return copied


def copy_offline_assets() -> bool:
    zips = sorted(OFFLINE_DIR.glob("offline_assets_*.zip"))
    if not zips:
        log("!! 未找到离线资源包。先执行：python -c \"from babeldoc.assets import assets; assets.generate_offline_assets_package()\"")
        return False
    dst = DIST / zips[-1].name
    if not dst.exists():
        shutil.copy2(zips[-1], dst)
    log(f"离线资源包：{dst.name}（{dst.stat().st_size / 1048576:.1f} MB）")
    return True


def copy_compliance() -> None:
    for name in COMPLIANCE_FILES:
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, DIST / name)
    log("合规文件已随包：" + "、".join(n for n in COMPLIANCE_FILES if (DIST / n).exists()))


def write_使用说明() -> None:
    text = """PDF 中英对照翻译 · 免安装版
================================

一、使用方法
    1. 双击「PDF中英对照翻译.exe」（不要把它单独拖出来，要连同 _internal 文件夹一起）
    2. 程序会打开一个独立的应用窗口；首次启动会先恢复内置的版面模型与字体（约 1-3 分钟）
    3. 在窗口里填入你的 DeepSeek API Key（platform.deepseek.com 申请），上传英文 PDF 即可

二、界面模式
    * 默认：原生应用窗口（基于系统自带的 WebView2 渲染，不弹浏览器）
    * 想用浏览器打开：命令行加 --browser，或设置环境变量 PDF_TRANSLATOR_UI=browser
    * 换端口：设置环境变量，例如  set PORT=9000
    * 若系统缺少 WebView2 运行时，程序会自动回退到浏览器，不会报错卡死

三、关于联网
    * 本版本已内置「离线资源包」，**不需要**联网下载模型与字体
    * 只有翻译本身需要联网（访问 api.deepseek.com）

四、常见问题
    * 首次启动较慢：在解压 340MB 模型与字体，属正常现象
    * 窗口打不开：在命令行运行  PDF中英对照翻译.exe --selftest  查看诊断
    * 遇到安全软件拦截：本程序为开源软件（AGPL-3.0），源码见 README

五、许可
    本程序以 AGPL-3.0 发布，不提供任何担保。
    完整许可证见 LICENSE；第三方组件与模型/字体来源见 THIRD-PARTY-NOTICES.md。
    核心上游：BabelDOC 与 PDFMathTranslate-next（均为 AGPL-3.0）。

六、卸载
    本程序为绿色软件：删除整个文件夹即可。
    运行时会在用户目录生成缓存 ~/.cache/babeldoc（约 340MB），可一并删除。
"""
    # 带 BOM 保存：Windows 记事本等工具才不会把 UTF-8 误判成 ANSI 而显示乱码
    (DIST / "使用说明.txt").write_text(text, encoding="utf-8-sig")
    log("已生成 使用说明.txt（UTF-8 with BOM）")


def seed_glossaries() -> None:
    """把示例术语集放到 exe 同级目录，让打包版开箱就有可用术语表。

    打包版的术语表数据目录是 **exe 同级的 glossaries/**（可写）；
    包内 _internal 是只读的，不能往那里找。
    """
    import json

    src_dir = ROOT / "glossaries"
    dst_dir = DIST / "glossaries"
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in src_dir.glob("*.csv"):
        dst = dst_dir / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            copied += 1
    idx_dst = dst_dir / "index.json"
    if not idx_dst.exists():
        idx_src = src_dir / "index.json"
        if idx_src.exists():
            shutil.copy2(idx_src, idx_dst)
        else:
            idx_dst.write_text(json.dumps({"sets": []}, ensure_ascii=False, indent=2), encoding="utf-8")
        copied += 1
    log(f"术语表数据目录已就绪（exe 同级的 glossaries/，含 {copied} 个示例文件）")


def migrate_data_dirs() -> None:
    """把源码目录下已有的任务记录/术语集搬到发行目录，避免「历史任务消失」。

    打包版的数据目录是 **exe 同级的 jobs/ 与 glossaries/**（可写、跨版本稳定）。
    升级时把旧数据带过去，用户不会以为文件丢了。
    """
    moved: list[str] = []
    for name in ("jobs", "glossaries"):
        src = ROOT / name
        dst = DIST / name
        if not src.is_dir():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.name.startswith("_"):        # 内部临时目录不搬
                continue
            target = dst / item.name
            if target.exists():
                continue
            try:
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
                moved.append(f"{name}/{item.name}")
            except OSError as exc:
                log(f"  跳过 {item.name}：{exc}")
    log(f"数据迁移完成：{len(moved)} 项" + (f"（示例：{', '.join(moved[:3])}）" if moved else ""))


LAUNCHER_VBS = r"""' PDF 中英对照翻译 —— 启动器（供桌面快捷方式调用）
'
' 要点（踩过的坑，改这个脚本前务必看）：
'   1) 不能用 Run(exe, 0, ...) 隐藏启动：那会把程序自己的窗口也藏掉，看起来像没启动。
'      用 1（正常显示）。exe 是控制台程序，会短暂闪一下黑窗口，随后窗口出现。
'   2) WMI 查询结果集合**没有 .Count 属性**，必须用 For Each 遍历计数，
'      否则脚本会在那一行静默失败 —— 表现就是"双击快捷方式没反应"。
'   3) 本文件必须保存为 **UTF-16LE + BOM**：VBScript 按 ANSI 读取 .vbs，
'      UTF-8 的中文会被截断，直接导致语法错误。
'   4) 本模板是 raw 字符串：若改成普通字符串，Python 会把 WMI 路径里的双反斜杠
'      当转义处理，写出来只剩一个反斜杠，脚本立刻语法错误。
Option Explicit

Dim fso, shell, baseDir, exePath, wmi, procs, running, obj

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = fso.BuildPath(baseDir, "PDF中英对照翻译.exe")

If Not fso.FileExists(exePath) Then
    MsgBox "找不到程序文件：" & vbCrLf & exePath & vbCrLf & vbCrLf & _
           "请确认这个启动器与「PDF中英对照翻译.exe」在同一个文件夹里。", _
           16, "PDF 中英对照翻译"
    WScript.Quit 1
End If

running = 0
On Error Resume Next
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name='PDF中英对照翻译.exe'")
If Err.Number = 0 And Not procs Is Nothing Then
    For Each obj In procs
        running = running + 1
    Next
End If
Err.Clear
On Error GoTo 0

If running > 0 Then
    MsgBox "程序已经在运行了（检测到 " & running & " 个进程）。" & vbCrLf & vbCrLf & _
           "如果找不到窗口：看任务栏，或直接在浏览器打开 http://127.0.0.1:8848", _
           64, "PDF 中英对照翻译"
    WScript.Quit 0
End If

shell.CurrentDirectory = baseDir
' 直接执行 exe（路径含空格时 Run 也能正确处理，不必自己拼双引号，
' 从而避免在多层转义里出错 —— 这是前面反复踩坑的地方）
shell.Run exePath, 1, False
"""


def write_launcher() -> None:
    """生成启动器（UTF-16LE+BOM，VBScript 才能正确读中文）+ 图标 + 桌面快捷方式。"""
    import subprocess

    vbs = DIST / "启动（双击这里）.vbs"
    vbs.write_text(LAUNCHER_VBS, encoding="utf-16")   # utf-16 → 自动写 BOM(FF FE)
    log("已生成启动器：启动（双击这里）.vbs（UTF-16LE + BOM）")

    try:
        subprocess.run([sys.executable, str(ROOT / "build" / "make_icon.py")],
                       check=False, capture_output=True, timeout=180)
    except Exception:  # noqa: BLE001
        pass

    try:
        desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            return
        icon = DIST / "app.ico"
        exe = DIST / "PDF中英对照翻译.exe"
        lnk = desktop / "PDF 中英对照翻译.lnk"
        ps = (
            "$s=New-Object -ComObject WScript.Shell;"
            f"$l=$s.CreateShortcut('{lnk}');"
            "$l.TargetPath='C:\\Windows\\System32\\wscript.exe';"
            f"$l.Arguments='\"{vbs}\"';"
            f"$l.WorkingDirectory='{DIST}';"
            f"$l.IconLocation='{icon if icon.exists() else exe},0';"
            "$l.Description='PDF / PPT / Word 中英对照翻译（DeepSeek + BabelDOC）';"
            "$l.Save()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       check=False, capture_output=True, timeout=180)
        log(f"已创建桌面快捷方式：{lnk}")
    except Exception as exc:  # noqa: BLE001
        log(f"桌面快捷方式创建失败（不影响使用）：{exc}")


def trim_unused() -> int:
    """删掉确认用不到的重量级文件，压缩体积。

    只处理两类，都验证过：
      1) opencv 的 opencv_videoio_ffmpeg*.dll（约 29MB）——视频编解码后端。
         BabelDOC 用 cv2 只做读图/缩放/灰度/形态学，不碰视频；而且 OpenCV 对它
         是惰性加载，删掉后不影响 import 与图像处理。
      2) _internal/samples——构建时 datadirs 把示例文件一起打进去了，属误打包。
    """
    removed: list[str] = []
    total = 0

    cv2_dir = INTERNAL / "cv2"
    if cv2_dir.is_dir():
        for dll in cv2_dir.glob("opencv_videoio_ffmpeg*.dll"):
            total += dll.stat().st_size
            removed.append(dll.name)
            dll.unlink(missing_ok=True)

    samples = INTERNAL / "samples"
    if samples.is_dir():
        total += sum(f.stat().st_size for f in samples.rglob("*") if f.is_file())
        removed.append("_internal/samples/")
        shutil.rmtree(samples, ignore_errors=True)

    if removed:
        log(f"体积裁剪：删除 {len(removed)} 项，省下 {total / 1048576:.1f} MB（{', '.join(removed[:3])}）")
    else:
        log("体积裁剪：没有可删项")
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-assets", action="store_true",
                        help="轻量版：不把离线资源包放进发行目录，改放到 dist/ 供单独分发")
    parser.add_argument("--no-migrate", action="store_true", help="不迁移已有的任务/术语表数据")
    parser.add_argument("--no-trim", action="store_true", help="不做体积裁剪")
    args = parser.parse_args()

    if not DIST.exists():
        log(f"!! 未找到打包产物 {DIST}，请先运行 PyInstaller")
        return 1

    copy_native_runtimes()
    if not args.no_trim:
        trim_unused()
    if args.skip_assets:
        # 轻量版：资源包不放进程序目录，而是放到 dist/ 顶层作为独立下载物
        src_zip = sorted(OFFLINE_DIR.glob("offline_assets_*.zip"))
        if src_zip:
            out = ROOT / "dist" / src_zip[-1].name
            if not out.exists():
                shutil.copy2(src_zip[-1], out)
            log(f"轻量版：资源包单独放在 {out.name}（{out.stat().st_size / 1048576:.0f} MB），不随程序分发")
        else:
            log("!! 未找到离线资源包，轻量版将只能联网下载模型")
    else:
        copy_offline_assets()
    copy_compliance()
    seed_glossaries()
    if not args.no_migrate:
        migrate_data_dirs()
    write_使用说明()
    write_launcher()

    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    log(f"完成：{DIST}")
    log(f"发行包体积 {total / 1048576:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
