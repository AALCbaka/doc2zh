"""验证清单行拆分效果：同一页，关闭 vs 开启（走正式翻译任务入口，确保用的是生产代码路径）。

用法：python build/verify_list_split.py <PDF> <API_KEY> <页码>
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PDF = Path(sys.argv[1]).resolve()
KEY = sys.argv[2]
PAGE = sys.argv[3] if len(sys.argv) > 3 else "4"


def run_job(tag: str, extra: list[str]) -> Path:
    out = ROOT / "jobs" / f"_lst_{tag}"
    work = ROOT / "jobs" / f"_lst_{tag}_w"
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(ROOT / "webapp" / "translate_job.py"),
        "--input", str(PDF), "--output", str(out), "--workdir", str(work),
        "--lang-out", "zh-CN", "--engine", "deepseek", "--model", "deepseek-chat",
        "--api-key", KEY, "--qps", "4", "--pages", PAGE, "--skip-scanned-detection",
        *extra,
    ]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    print(f"\n>>> 运行（{tag}）…", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(ROOT))
    result = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("{"):
            import json

            e = json.loads(line)
            if e.get("type") == "finish":
                result = e
            if e.get("type") == "error":
                print("  ERROR:", e.get("message")[:200])
    if not result or not result.get("dual_pdf"):
        print("  stderr 尾部:", "\n".join((proc.stderr or "").splitlines()[-6:]))
        raise SystemExit(f"{tag} 未产出结果")
    return Path(result["dual_pdf"])


def rows(pdf: Path, page_no: int) -> list[str]:
    import fitz

    with fitz.open(pdf) as doc:
        page = doc.load_page(page_no - 1)
        w = page.rect.width
        out = []
        for b in page.get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                t = "".join(s.get("text", "") for s in line.get("spans", []))
                if t.strip() and line["bbox"][0] > w * 0.45 and any("\u4e00" <= c <= "\u9fff" for c in t):
                    out.append((round(line["bbox"][1], 1), t.strip()))
        out.sort()
        return [t for _, t in out]


def merged_ratio(lines: list[str]) -> tuple[int, int]:
    """统计把多个选项并进同一行的情况。"""
    marks = ("a)", "a）", "b)", "b）", "c)", "c）", "d)", "d）")
    bad = sum(1 for r in lines if sum(r.count(m) for m in marks) >= 2)
    return bad, len(lines)


def main() -> int:
    off = run_job("off", ["--no-list-split"])
    on = run_job("on", [])
    page_no = int(PAGE.split("-")[0].split(",")[0])

    print("\n" + "=" * 96)
    for tag, pdf in (("关闭清单拆分", off), ("开启清单拆分", on)):
        lines = rows(pdf, page_no)
        bad, total = merged_ratio(lines)
        print(f"\n===== {tag}：译文 {total} 行，「多个选项挤在一行」的有 {bad} 行 =====")
        for r in lines:
            if any(k in r for k in ("确定", "识别", "指出", "选择题")):
                print(f"  {r[:104]}")

    import fitz

    for tag, pdf in (("off", off), ("on", on)):
        with fitz.open(pdf) as d:
            d.load_page(page_no - 1).get_pixmap(dpi=150).save(str(ROOT / "jobs" / f"预览-{tag}.png"))
    print(f"\n预览图：{ROOT / 'jobs' / '预览-off.png'}  /  {ROOT / 'jobs' / '预览-on.png'}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        import multiprocessing as mp

        mp.freeze_support()
    sys.exit(main())
