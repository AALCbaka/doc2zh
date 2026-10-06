"""校验发布 zip：README 是否为最新、是否含个人数据、关键文件是否齐全。

用法：python build/verify_release_zip.py [zip 路径]
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
zips = sorted((ROOT / "dist").glob("doc2zh-v*.zip"))
target = Path(sys.argv[1]) if len(sys.argv) > 1 else (zips[-1] if zips else None)
if not target or not target.exists():
    print("没找到 zip")
    sys.exit(1)

print(f"检查：{target.name}  ({target.stat().st_size / 1048576:.1f} MB)")

with zipfile.ZipFile(target) as z:
    names = z.namelist()
    # 注意取**顶层**的 README：包内 _internal/ 下还可能有一份旧的（PyInstaller 打包时塞进去的）
    top = names[0].split("/")[0]
    md_name = f"{top}/README.md"
    md = z.read(md_name).decode("utf-8") if md_name in names else ""

disk_md = (ROOT / "dist" / "PDF中英对照翻译" / "README.md").read_text(encoding="utf-8")
src_md = (ROOT / "README.md").read_text(encoding="utf-8")

checks: list[tuple[str, bool, str]] = []

# 1) README 必须与源码一致
checks.append(("README 与源码一致", md == src_md,
               f"zip {len(md)} 字 / 源码 {len(src_md)} 字 / 磁盘 {len(disk_md)} 字"))

# 2) 上游名笔误：pdf2zh 后面必须是 -next 或 _next
bad = [m.group(0) for m in re.finditer(r"pdf2zh(?!-next|_next)", md)]
checks.append(("上游名无笔误", not bad, f"命中 {bad}" if bad else "pdf2zh-next 正确"))

# 3) 发布包文件名与实际产物一致
checks.append(("发布包文件名正确", "doc2zh-vX.Y.Z-win64.zip" in md,
               "含占位文件名" if "doc2zh-vX.Y.Z-win64.zip" in md else "未见"))
checks.append(("无旧的文件名", "PDF中英对照翻译-vX.Y.Z.zip" not in md, ""))

# 4) 许可段已改写（去掉 AI 腔）
checks.append(("许可段已改写", "不是选的" not in md, ""))

# 5) 关键文件齐全
must = ["PDF中英对照翻译.exe", "启动（双击这里）.vbs", "LICENSE",
        "THIRD-PARTY-NOTICES.md", "使用说明.txt", "app.ico"]
missing = [m for m in must if not any(n.endswith("/" + m) for n in names)]
checks.append(("关键文件齐全", not missing, f"缺 {missing}" if missing else f"{len(must)} 项都在"))

# 6) 不含个人数据
leaked = [n for n in names if "/jobs/" in n or n.endswith("启动日志.txt") or "__pycache__" in n]
checks.append(("无个人数据/缓存", not leaked, f"泄漏 {leaked[:3]}" if leaked else "干净"))

# 7) zip 完好
with zipfile.ZipFile(target) as z:
    bad_crc = z.testzip()
checks.append(("CRC 校验通过", bad_crc is None, str(bad_crc) if bad_crc else f"{len(names)} 项"))

# 8) 轻量版不应含离线模型包（base_library.zip 是 PyInstaller 运行必需的，不算）
asset_zips = [n for n in names if "offline_assets" in n]
checks.append(("轻量版不含模型包", not asset_zips, f"含 {asset_zips}" if asset_zips else "正确（模型单独分发）"))

print()
for name, ok, extra in checks:
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + (f"   —— {extra}" if extra else ""))
print()
failed = [c[0] for c in checks if not c[1]]
print("结论：" + ("全部通过" if not failed else f"{len(failed)} 项未通过：{failed}"))
sys.exit(0 if not failed else 1)
