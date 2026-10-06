"""A/B 对比：split_short_lines 开关对「该换行的地方有没有换行」的实际效果。

用法：
    python build/ab_split_test.py <输入PDF> <API_KEY> [页码]

分别用 关闭 / 开启 各翻译一次，然后把同一页渲染成 PNG，并打印译文段落结构对比。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
PDF = Path(sys.argv[1]).resolve()
KEY = sys.argv[2]
PAGES = sys.argv[3] if len(sys.argv) > 3 else "3"


async def translate(split: bool, out_dir: Path) -> Path:
    from pdf2zh_next.config.model import PDFSettings, SettingsModel, TranslationSettings
    from pdf2zh_next.config.translate_engine_model import DeepSeekSettings
    from pdf2zh_next.high_level import do_translate_async_stream

    out_dir.mkdir(parents=True, exist_ok=True)
    settings = SettingsModel(
        basic={"input_files": [], "debug": False, "gui": False, "warmup": False},
        translation=TranslationSettings(
            lang_in="en", lang_out="zh-CN", output=str(out_dir), qps=4,
            no_auto_extract_glossary=True,
        ),
        pdf=PDFSettings(
            pages=PAGES,
            no_dual=False, no_mono=True,
            watermark_output_mode="no_watermark",
            skip_scanned_detection=True,
            split_short_lines=split,
            short_line_split_factor=0.8,
        ),
        translate_engine_settings=DeepSeekSettings(deepseek_api_key=KEY),
    )
    settings.validate_settings()
    tag = "开启短行切分" if split else "关闭短行切分"
    print(f"\n>>> 翻译中（{tag}）…", flush=True)
    async for event in do_translate_async_stream(settings, str(PDF)):
        if event.get("type") == "error":
            raise RuntimeError(event["error"])
        if event.get("type") == "progress_update":
            stage = event.get("stage")
            if stage in ("Translate Paragraphs", "Typesetting") and event.get("overall_progress", 0) % 20 < 1:
                print(f"    {stage} {event.get('overall_progress', 0):.0f}%", flush=True)
        if event.get("type") == "finish":
            r = event["translate_result"]
            return Path(r.no_watermark_dual_pdf_path or r.dual_pdf_path)
    raise RuntimeError("未得到结果")


def render(pdf: Path, index: int, out: Path, dpi: int = 150) -> None:
    import fitz

    with fitz.open(pdf) as doc:
        page = doc.load_page(min(index, doc.page_count - 1))
        page.get_pixmap(dpi=dpi).save(str(out))


def lines_of(pdf: Path, index: int) -> list[str]:
    """按 y 坐标把某一页译文区域的文本行还原出来（只看右半边=译文）。"""
    import fitz

    with fitz.open(pdf) as doc:
        page = doc.load_page(min(index, doc.page_count - 1))
        w = page.rect.width
        blocks = page.get_text("dict")["blocks"]
        rows = []
        for b in blocks:
            for line in b.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", []))
                if not text.strip():
                    continue
                x0 = line["bbox"][0]
                if x0 > w * 0.45 and any("\u4e00" <= c <= "\u9fff" for c in text):
                    rows.append((round(line["bbox"][1], 1), text.strip()))
        rows.sort()
        return [t for _, t in rows]


async def main() -> int:
    base = ROOT / "jobs" / "_ab_split"
    off_dir, on_dir = base / "off", base / "on"

    off_pdf = await translate(False, off_dir)
    on_pdf = await translate(True, on_dir)
    print(f"\n关闭: {off_pdf}\n开启: {on_pdf}")

    page_index = int(PAGES.split("-")[0].split(",")[0]) - 1
    out_off = base / "对比-关闭短行切分.png"
    out_on = base / "对比-开启短行切分.png"
    render(off_pdf, page_index, out_off)
    render(on_pdf, page_index, out_on)
    print(f"预览图: {out_off}\n预览图: {out_on}")

    def merged_count(rows: list[str]) -> int:
        # 统计"把 a) b) c) d) 并进同一行"的情况
        return sum(1 for r in rows if r.count("a)") + r.count("b)") + r.count("c)") + r.count("d)") >= 2)

    for tag, pdf in (("关闭", off_pdf), ("开启", on_pdf)):
        rows = lines_of(pdf, page_index)
        print(f"\n===== {tag}短行切分：译文共 {len(rows)} 行，其中「选项被并进一行」的有 {merged_count(rows)} 行 =====")
        for r in rows[:16]:
            print(f"  {r[:96]}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        import multiprocessing as mp

        mp.freeze_support()
        mp.set_start_method("spawn", force=True)
    asyncio.run(main())
