"""决定性实验：预处理源 PDF（清单块按行拆开 + 拉开行距）→ 真跑翻译 → 看选项是否分行。

对比三组：
  A. 原始文件（基线）
  B. 预拆分，行距不变
  C. 预拆分，行距 ×1.5
判定：译文里「一行含 ≥2 个选项」的行数。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_presplit import MARKER_RE, is_list_block, line_text  # noqa: E402

SRC = Path(sys.argv[1]).resolve()
KEY = sys.argv[2]
PAGE = int(sys.argv[3]) if len(sys.argv) > 3 else 4


def preprocess(dst: Path, spacing_mult: float) -> dict:
    """把清单块按行重排；line spacing 乘以 spacing_mult。"""
    doc = fitz.open(SRC)
    page = doc.load_page(PAGE - 1)
    targets = [b for b in page.get_text("dict")["blocks"] if b.get("type") == 0 and is_list_block(b)]
    for b in targets:
        page.add_redact_annot(fitz.Rect(b["bbox"]), fill=None)
    if targets:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    lines_done = 0
    for b in targets:
        spans = [sp for ln in b["lines"] for sp in ln.get("spans", []) if sp.get("text", "").strip()]
        if not spans:
            continue
        size = round(spans[0]["size"], 2)
        color = spans[0]["color"]
        rgb = ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255
        base_y = b["lines"][0]["spans"][0]["origin"][1] if b["lines"][0].get("spans") else b["lines"][0]["bbox"][3]
        orig_step = (b["lines"][1]["bbox"][1] - b["lines"][0]["bbox"][1]) if len(b["lines"]) > 1 else size * 1.2
        new_step = orig_step * spacing_mult
        for i, ln in enumerate(b["lines"]):
            txt = line_text(ln)
            if not txt:
                continue
            page.insert_text(fitz.Point(ln["bbox"][0], base_y + i * new_step), txt,
                             fontname="helv", fontsize=size, color=rgb)
            lines_done += 1
    doc.save(dst, garbage=3, deflate=True)
    doc.close()
    return {"blocks": len(targets), "lines": lines_done}


def run_translate(pdf: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(ROOT / "webapp" / "translate_job.py"),
           "--input", str(pdf), "--output", str(out_dir), "--workdir", str(out_dir / "w"),
           "--api-key", KEY, "--pages", str(PAGE), "--skip-scanned-detection",
           "--lang-out", "zh-CN", "--qps", "4"]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    print(f"    运行翻译: {pdf.name}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=str(ROOT))
    for line in (proc.stdout or "").splitlines():
        if line.startswith("{"):
            e = json.loads(line)
            if e.get("type") == "finish" and e.get("dual_pdf"):
                return Path(e["dual_pdf"])
            if e.get("type") == "error":
                raise RuntimeError(e.get("message")[:200])
    raise RuntimeError("未得到输出：" + (proc.stderr or "")[-300:])


def merged_count(pdf: Path) -> tuple[int, int, list[str]]:
    M = ("a)", "b)", "c)", "d)")
    with fitz.open(pdf) as doc:
        pg = doc.load_page(PAGE - 1)
        w = pg.rect.width
        rows = []
        for b in pg.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = "".join(s.get("text", "") for s in ln.get("spans", []))
                if not t.strip() or ln["bbox"][0] <= w * 0.45:
                    continue
                if not any("\u4e00" <= c <= "\u9fff" for c in t):
                    continue
                rows.append(t.strip())
    merged = [r for r in rows if sum(r.count(m) for m in M) >= 2]
    return len(rows), len(merged), merged


def main() -> int:
    work = ROOT / "jobs" / "_pre"
    work.mkdir(parents=True, exist_ok=True)
    cases = [("A-原始", None, None), ("B-拆分", 1.0, work / "B.pdf"), ("C-拆分+行距1.5", 1.5, work / "C.pdf")]

    results = {}
    for name, mult, dst in cases:
        print(f"\n=== {name} ===")
        pdf = SRC
        if dst is not None:
            st = preprocess(dst, mult)
            print(f"    预处理: {st}")
            pdf = dst
        try:
            out = run_translate(pdf, work / name.replace("+", "_"))
            total, merged, samples = merged_count(out)
            results[name] = (total, merged)
            print(f"    译文 {total} 行，一行含≥2选项: {merged}")
            for s in samples[:4]:
                print(f"      - {s[:88]}")
        except Exception as exc:  # noqa: BLE001
            print(f"    失败: {exc}")
            results[name] = ("失败", "失败")

    print("\n===== 汇总 =====")
    for k, v in results.items():
        print(f"  {k:16s} 译文行数={v[0]}  并行(含≥2选项)={v[1]}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        import multiprocessing as mp

        mp.freeze_support()
    sys.exit(main())
