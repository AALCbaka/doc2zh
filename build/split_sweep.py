"""验证 split_short_lines 在不同阈值下的实际切分效果（只翻指定页）。

用法：python build/split_sweep.py <PDF> <API_KEY> <页码> <factor1,factor2,...>
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
PDF = Path(sys.argv[1]).resolve()
KEY = sys.argv[2]
PAGE = sys.argv[3]
FACTORS = [float(x) for x in (sys.argv[4] if len(sys.argv) > 4 else "0.8,0.5,0.2").split(",")]


async def run(factor: float) -> Path:
    from pdf2zh_next.config.model import PDFSettings, SettingsModel, TranslationSettings
    from pdf2zh_next.config.translate_engine_model import DeepSeekSettings
    from pdf2zh_next.high_level import do_translate_async_stream

    out = ROOT / "jobs" / "_sweep" / f"f{factor}"
    out.mkdir(parents=True, exist_ok=True)
    settings = SettingsModel(
        basic={"input_files": [], "debug": False, "gui": False, "warmup": False},
        translation=TranslationSettings(
            lang_in="en", lang_out="zh-CN", output=str(out), qps=4, no_auto_extract_glossary=True,
        ),
        pdf=PDFSettings(
            pages=PAGE, no_dual=False, no_mono=True,
            watermark_output_mode="no_watermark", skip_scanned_detection=True,
            split_short_lines=True, short_line_split_factor=factor,
        ),
        translate_engine_settings=DeepSeekSettings(deepseek_api_key=KEY),
    )
    settings.validate_settings()
    print(f"\n>>> factor={factor} 翻译中…", flush=True)
    async with asyncio.timeout(900):
        async for e in do_translate_async_stream(settings, str(PDF)):
            if e.get("type") == "error":
                raise RuntimeError(e["error"])
            if e.get("type") == "finish":
                r = e["translate_result"]
                return Path(r.no_watermark_dual_pdf_path or r.dual_pdf_path)
    raise RuntimeError("超时")


def right_lines(pdf: Path, page_no: int) -> list[str]:
    import fitz

    with fitz.open(pdf) as doc:
        page = doc.load_page(page_no - 1)
        w = page.rect.width
        rows = []
        for b in page.get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", []))
                if not text.strip():
                    continue
                if line["bbox"][0] > w * 0.45 and any("\u4e00" <= c <= "\u9fff" for c in text):
                    rows.append((round(line["bbox"][1], 1), text.strip()))
        rows.sort()
        return [t for _, t in rows]


async def main() -> int:
    page_no = int(PAGE.split("-")[0].split(",")[0])
    for factor in FACTORS:
        pdf = await run(factor)
        rows = right_lines(pdf, page_no)
        merged = sum(1 for r in rows if sum(r.count(m) for m in ("a)", "a）", "b)", "b）", "c)", "c）", "d)", "d）")) >= 2)
        print(f"\n===== factor={factor}：译文 {len(rows)} 行，仍并行的 {merged} 行 =====")
        for r in rows:
            if any(k in r for k in ("确定", "识别", "7.", "8.", "指出")):
                print(f"  {r[:110]}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        import multiprocessing as mp

        mp.freeze_support()
        mp.set_start_method("spawn", force=True)
    asyncio.run(main())
