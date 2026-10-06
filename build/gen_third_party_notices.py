"""生成 THIRD-PARTY-NOTICES.md 的依赖清单（数据来自已安装包的元数据，不靠人工记忆）。

用法（构建前执行）：
    .venv\\Scripts\\python.exe build\\gen_third_party_notices.py
"""
from __future__ import annotations

import importlib.metadata as md
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_MARKER_START = "<!-- BEGIN AUTO-GENERATED PACKAGE LIST -->"
OUT_MARKER_END = "<!-- END AUTO-GENERATED PACKAGE LIST -->"

# 这些包会被打进分发产物（PyInstaller 会把依赖一起收集），逐个列出更清楚
KEY_PACKAGES = [
    "babeldoc", "pdf2zh-next", "pymupdf", "onnxruntime", "onnx",
    "opencv-python-headless", "numpy", "scipy", "scikit-image", "scikit-learn",
    "fastapi", "uvicorn", "starlette", "pydantic", "pydantic-settings",
    "openai", "httpx", "tiktoken", "peewee", "tenacity", "rtree", "hyperscan",
    "uharfbuzz", "freetype-py", "pyzstd", "msgpack", "orjson", "Levenshtein",
    "huggingface-hub", "xsdata", "cryptography", "rich", "tqdm", "toml",
    "configargparse", "bitstring", "psutil", "chardet", "charset-normalizer",
    "python-multipart", "sse-starlette", "pillow", "joblib", "lxml",
]

# 元数据里写法不规范、已人工核实过 LICENSE 文件原文的包
MANUAL_LICENSE = {
    "scipy": "BSD-3-Clause",
    "scikit-image": "BSD-3-Clause",
    "peewee": "MIT",
    "pymupdf": "AGPL-3.0",
    "PyMuPDF": "AGPL-3.0",
}


def license_of(name: str) -> str:
    if name in MANUAL_LICENSE:
        return MANUAL_LICENSE[name]
    try:
        meta = md.metadata(name)
    except md.PackageNotFoundError:
        return "（未安装）"
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    cls = [c.split("::")[-1].strip() for c in (meta.get_all("Classifier") or []) if c.startswith("License")]
    if cls:
        return cls[0]
    raw = (meta.get("License") or "").strip()
    if raw:
        first = raw.splitlines()[0].strip()
        if re.fullmatch(r"[A-Za-z0-9.\-+ ]{2,40}", first):
            return first
        if "MIT" in raw[:200]:
            return "MIT"
        if "Apache" in raw[:200]:
            return "Apache-2.0"
        if "BSD" in raw[:200]:
            return "BSD-3-Clause"
        if "GNU AFFERO" in raw[:200].upper():
            return "AGPL-3.0"
    return "未标注（见包内 LICENSE 文件）"


def main() -> int:
    rows = []
    for name in KEY_PACKAGES:
        try:
            version = md.version(name)
        except md.PackageNotFoundError:
            continue
        rows.append((name, version, license_of(name)))

    lines = [OUT_MARKER_START, "", "| 组件 | 版本 | 许可证 |", "|---|---|---|"]
    for name, version, lic in rows:
        lines.append(f"| {name} | {version} | {lic} |")
    lines += ["", OUT_MARKER_END, ""]
    block = "\n".join(lines)

    target = ROOT / "THIRD-PARTY-NOTICES.md"
    content = target.read_text(encoding="utf-8") if target.exists() else f"# 第三方组件许可声明\n\n{OUT_MARKER_START}\n{OUT_MARKER_END}\n"
    if OUT_MARKER_START in content and OUT_MARKER_END in content:
        start = content.index(OUT_MARKER_START)
        end = content.index(OUT_MARKER_END) + len(OUT_MARKER_END)
        content = content[:start] + block.rstrip("\n") + content[end:]
    else:
        content = content.rstrip("\n") + "\n\n" + block
    target.write_text(content, encoding="utf-8")
    print(f"已更新 {target.name}：{len(rows)} 个组件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
