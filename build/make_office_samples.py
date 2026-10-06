"""造测试用 PPT/Word 文件，并验证两种转换方式。

用法：python build/make_office_samples.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"
OUT.mkdir(parents=True, exist_ok=True)

SLIDES = [
    ("Attention Is All You Need", ["Vaswani et al., 2017", "Google Brain"]),
    ("The Transformer Architecture", [
        "The dominant sequence transduction models are based on complex recurrent or",
        "convolutional neural networks that include an encoder and a decoder.",
        "We propose a new simple network architecture, the Transformer, based solely",
        "on attention mechanisms, dispensing with recurrence and convolutions entirely.",
    ]),
    ("Self-Attention", [
        "Self-attention relates different positions of a single sequence in order to",
        "compute a representation of the sequence.",
        "Multi-head attention allows the model to jointly attend to information from",
        "different representation subspaces at different positions.",
    ]),
]
DOC_PARAS = [
    "Chapter 1 - Defining and Collecting Data",
    "Most analysts focus on the cost of tuition as the way to measure the cost of a college education.",
    "But incidentals, such as textbook costs, are rarely considered. A researcher at University of Macau",
    "wishes to estimate the textbook costs of first-year students at UM during the academic year 2026/2027.",
    "To do so, he monitored the textbook cost of 250 first-year students and found that their average",
    "textbook cost was $600 per semester.",
    "6. Identify the population of interest to the researcher.",
    "a) All UM students during the academic year 2026/2027.",
    "b) All college students in Macao during the academic year 2026/2027.",
    "c) All first-year UM students during the academic year 2026/2027.",
    "d) The 250 students who were monitored.",
]


def make_pptx(path: Path) -> None:
    import win32com.client

    app = win32com.client.DispatchEx("PowerPoint.Application")
    pres = None
    try:
        pres = app.Presentations.Add()
        for idx, (title, bullets) in enumerate(SLIDES, start=1):
            slide = pres.Slides.Add(idx, 2)  # ppLayoutText
            slide.Shapes.Title.TextFrame.TextRange.Text = title
            body = slide.Shapes.Placeholders(2).TextFrame.TextRange
            body.Text = bullets[0]
            for line in bullets[1:]:
                body.InsertAfter("\r" + line)
        pres.SaveAs(str(path))
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


def make_docx(path: Path) -> None:
    import win32com.client

    app = win32com.client.DispatchEx("Word.Application")
    doc = None
    try:
        app.Visible = False
        doc = app.Documents.Add()
        for i, para in enumerate(DOC_PARAS):
            if i == 0:
                rng = doc.Content
                rng.Text = para
            else:
                doc.Content.InsertParagraphAfter()
                doc.Paragraphs(doc.Paragraphs.Count).Range.Text = para
        doc.SaveAs2(str(path), FileFormat=16)  # wdFormatDocumentDefault (.docx)
    finally:
        if doc is not None:
            try:
                doc.Close(0)
            except Exception:  # noqa: BLE001
                pass
        try:
            app.Quit()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from webapp.office_convert import convert_to_pdf, office_available

    print("=== 环境能力 ===")
    print(" ", office_available())

    pptx = OUT / "demo-slides.pptx"
    docx = OUT / "demo-handout.docx"
    print("\n=== 生成测试文件 ===")
    if not pptx.exists():
        make_pptx(pptx)
    if not docx.exists():
        make_docx(docx)
    for p in (pptx, docx):
        print(f"  {p.name}: {p.stat().st_size / 1024:.0f} KB")

    print("\n=== 转换测试 ===")
    for src in (pptx, docx):
        for mode in ("native", "printer"):
            dst = ROOT / "jobs" / f"_conv_{src.stem}_{mode}.pdf"
            dst.unlink(missing_ok=True)
            try:
                convert_to_pdf(src, dst, mode=mode, timeout=120,
                               on_log=lambda m: print(f"    [{src.suffix}{mode}] {m}"))
                import fitz

                with fitz.open(dst) as d:
                    txt = d.load_page(0).get_text()[:70].replace("\n", " ")
                    print(f"  ✓ {src.name} → {mode}: {d.page_count} 页 | {txt}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ {src.name} → {mode}: {type(exc).__name__}: {str(exc)[:150]}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        import multiprocessing as mp

        mp.freeze_support()
    sys.exit(main())
