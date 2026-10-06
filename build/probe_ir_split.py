"""直接验证 IR 拆分：解析 PDF → 跑 ParagraphFinder → 看段落是否被按行拆开。

不调用翻译 API，秒级出结果。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PDF = Path(sys.argv[1] if len(sys.argv) > 1
           else "jobs/8cca76c57d38/input/2002_Ch1 Handout.pdf").resolve()
PAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 4

from webapp import ir_split  # noqa: E402

print("补丁安装:", ir_split.install())


async def main() -> int:
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    from pdf2zh_next.config.model import PDFSettings, SettingsModel, TranslationSettings
    from pdf2zh_next.config.translate_engine_model import DeepSeekSettings
    from pdf2zh_next.high_level import do_translate_async_stream

    # 统计：monkeypatch process_page 看它是否被调用、每页段落数
    calls = []
    real = ParagraphFinder.process_page

    def spy(self, page):
        before = len(getattr(page, "pdf_paragraph", []) or [])
        r = real(self, page)
        after = len(getattr(page, "pdf_paragraph", []) or [])
        calls.append((before, after))
        return r

    ParagraphFinder.process_page = spy
    ir_split.STATS.update(pages=0, paragraphs_split=0, blocks_created=0, failures=0)

    settings = SettingsModel(
        basic={"input_files": [], "debug": False, "gui": False, "warmup": False},
        translation=TranslationSettings(
            lang_in="en", lang_out="zh-CN", output="jobs/_ir_probe", qps=4,
            no_auto_extract_glossary=True,
        ),
        pdf=PDFSettings(pages=str(PAGE), no_dual=False, no_mono=True,
                        watermark_output_mode="no_watermark", skip_scanned_detection=True,
                        skip_translation=True),
        translate_engine_settings=DeepSeekSettings(deepseek_api_key="dummy"),
    )
    settings.validate_settings()
    try:
        async for e in do_translate_async_stream(settings, str(PDF)):
            if e.get("type") in ("finish", "error"):
                if e.get("type") == "error":
                    print("事件错误（可能是禁用翻译导致，不影响统计）:", str(e.get("error"))[:120])
                break
    except Exception as exc:  # noqa: BLE001
        print(f"（流程异常，通常是 skip_translation 不被支持：{type(exc).__name__}）")

    print("\n=== process_page 调用情况 ===")
    for i, (before, after) in enumerate(calls, 1):
        flag = f"  ← 拆出 {after - before} 段" if after != before else ""
        print(f"  第{i}页: 段落 {before} -> {after}{flag}")
    print("\n=== 拆分统计 ===")
    print(" ", ir_split.STATS)
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        import multiprocessing as mp

        mp.freeze_support()
        mp.set_start_method("spawn", force=True)
    asyncio.run(main())
