"""把发行目录打包成 zip，供 GitHub Release 使用。

要点：
  * 排除个人数据与运行痕迹：jobs/（任务记录）、启动日志.txt、__pycache__
  * zip 内保留顶层目录名，用户解压后得到一个干净的文件夹
  * 用标准 ZIP 格式（兼容 Windows 自带解压，且支持 >4GB）
  * DEFLATE 压缩，exe 与 dll 本身压不动多少，但能省下可观的体积

用法：python build/make_release_zip.py [版本号]
产物：dist/doc2zh-v<版本>-win64.zip
"""
from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "PDF中英对照翻译"
TOP = "PDF中英对照翻译"                     # zip 内的顶层目录名
EXCLUDE_DIRS = {"jobs", "__pycache__", ".cache"}
EXCLUDE_FILES = {"启动日志.txt", ".write_test"}


def iter_files():
    for p in sorted(DIST.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(DIST)
        if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]) or rel.parts[0] in EXCLUDE_DIRS:
            continue
        if p.name in EXCLUDE_FILES or p.suffix in {".pyc", ".pyo"}:
            continue
        yield p, rel


def human(n: float) -> str:
    return f"{n / 1048576:.1f} MB"


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else "1.0.0"

    if not DIST.is_dir():
        print(f"!! 没找到发行目录：{DIST}")
        return 1

    # 打包前清理：个人数据不随发布包走
    for name in ("jobs", "启动日志.txt"):
        target = DIST / name
        if target.is_dir():
            import shutil

            shutil.rmtree(target, ignore_errors=True)
            print(f"[清理] 已移除 {name}/（个人任务数据）")
        elif target.exists():
            target.unlink()
            print(f"[清理] 已移除 {name}（运行日志）")

    out = ROOT / "dist" / f"doc2zh-v{version}-win64.zip"
    if out.exists():
        out.unlink()

    files = list(iter_files())
    raw = sum(p.stat().st_size for p, _ in files)
    print(f"[打包] {len(files)} 个文件，原始 {human(raw)}")
    print(f"[打包] 输出：{out.name}")

    started = time.time()
    done = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        for path, rel in files:
            zf.write(path, arcname=f"{TOP}/{rel.as_posix()}")
            done += 1
            if done % 200 == 0:
                print(f"    {done}/{len(files)} …", flush=True)

    size = out.stat().st_size
    print(f"[完成] {out.name}  {human(size)}（压缩率 {size / raw * 100:.0f}%，用时 {time.time() - started:.0f}s）")
    print(f"[完成] 路径：{out}")

    # 自检：确认 zip 完好、关键文件在
    with zipfile.ZipFile(out) as zf:
        bad = zf.testzip()
        names = zf.namelist()
    must = [f"{TOP}/PDF中英对照翻译.exe", f"{TOP}/启动（双击这里）.vbs",
            f"{TOP}/LICENSE", f"{TOP}/THIRD-PARTY-NOTICES.md"]
    missing = [m for m in must if m not in names]
    leaked = [n for n in names if "/jobs/" in n or n.endswith("启动日志.txt")]
    print(f"[自检] CRC {'通过' if bad is None else '失败：' + str(bad)}，共 {len(names)} 项")
    print(f"[自检] 关键文件 {'齐全' if not missing else '缺失：' + str(missing)}")
    print(f"[自检] 个人数据 {'未泄漏' if not leaked else '泄漏：' + str(leaked)}")
    return 0 if (bad is None and not missing and not leaked) else 1


if __name__ == "__main__":
    sys.exit(main())
