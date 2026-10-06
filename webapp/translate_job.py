"""单篇 PDF 翻译入口（子进程执行）。

设计要点：
- 由 Web 服务以子进程方式调用，实现"任务隔离 + 可取消 + 可并发"。
- stdout 只输出机器可读的 JSON 事件（每行一条），日志走 stderr。
- 通过 pdf2zh-next 官方高层 API 驱动 BabelDOC，输出保留排版的**双语对照 PDF**。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.list_split import wrap_translator  # noqa: E402
from webapp.line_structure import install_prompt_patch  # noqa: E402

LIST_SPLIT_ENABLED = False  # 见下方说明：当前对最终版面无效，默认关闭

# 说明：这两层补丁（翻译器代理 + 提示词行结构）已实现并通过单测，
# 但实测发现 BabelDOC 的重排会按「段落」粒度重新折行，模型输出多少换行都会被抹平，
# 因此对最终 PDF 没有可见效果。保留代码是为了下一步在「中间表示层拆段」时复用；
# 默认关闭以免白白增加 API 调用。恢复方式：环境变量 PDF_TRANSLATOR_LIST_SPLIT=1。
if os.environ.get("PDF_TRANSLATOR_LIST_SPLIT", "") not in ("", "0", "false"):
    LIST_SPLIT_ENABLED = True

# pdf2zh-next 原始的子进程入口（由 install_translator_wrapper 填充）
_ORIGINAL_WRAPPER = None


def _set_list_split(enabled: bool) -> None:
    global LIST_SPLIT_ENABLED
    LIST_SPLIT_ENABLED = enabled


# ---------------------------------------------------------------- 事件输出
def emit(event: dict) -> None:
    """向父进程输出一条 JSON 事件（单行）。"""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


_STAGE_LABELS = {
    "Parse PDF": "解析 PDF 结构",
    "Parse PDF and Create IR": "解析 PDF 结构",
    "Parse PDF and Create Intermediate Representation": "解析 PDF 结构",
    "DetectScannedFile": "检测扫描件",
    "Detect Scanned PDF": "检测扫描件",
    "Parse Page Layout": "版面分析（模型推理）",
    "Layout Analysis": "版面分析（模型推理）",
    "Parse Paragraphs": "段落与版式识别",
    "Find Paragraphs": "段落识别",
    "Parse Formulas and Styles": "识别公式与字体样式",
    "Automatic Term Extraction": "术语自动提取",
    "Translate": "AI 翻译中",
    "Translate Paragraphs": "AI 翻译段落",
    "Typesetting": "重排版",
    "Typesetting Paragraphs": "重排段落",
    "Generate drawing instructions": "生成绘图指令",
    "Add Fonts": "嵌入中文字体",
    "Subset font": "字体子集化",
    "Generate PDF": "生成对照 PDF",
    "Save PDF": "写入文件",
    "Term Extraction": "术语提取",
}


def label_for(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage)


# ---------------------------------------------------------------- 清单行拆分
# 背景（实测得到的结论）：
#   BabelDOC 会把「题干 + a) b) c) d) 选项」识别成一个段落，而送进翻译模型的
#   段落文本是**不带换行符**的（整段字符按间距拼成一个字符串），所以模型无从保留
#   行结构，译出来必然并成一段 —— 表现为"该换行的地方没换行"。
#   上游的 split_short_lines 是按「行宽 < 中位数 × 系数」判定，对等宽选项不生效。
# 对策：
#   在翻译前做一次确定性拆分：把「清单式」段落按行切开，每行作为独立段落送翻译，
#   这样回填时每行各自成段，行结构得以保留。普通正文段落不受影响。
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:"
    r"[\(\[]?[a-zA-Z][\)\].、]|"          # a) b. c、  (a)
    r"[\(\[]?\d{1,2}[\)\].、]|"           # 1) 2. 3、  (1)
    r"[ivxlcdm]{1,4}[\)\.]|"              # 罗马数字 i) ii.
    r"[•·▪◦‣∙*\-–—]\s|"                   # 项目符号
    r"\d+(?:\.\d+)+\s|"                   # 1.12 之类编号
    r"Pg\.\s*\d+|Suggested\s"
    r")",
    re.IGNORECASE,
)


def _is_list_like_line(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 160:
        return False
    return bool(_LIST_ITEM_RE.match(t))


def split_list_paragraph(block: dict) -> list[dict]:
    """把一个段落块按行拆开（仅当它是清单式内容时）。返回 1 个或多个块。"""
    text = block.get("text") or ""
    if "\n" not in text:
        return [block]
    raw_lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(raw_lines) < 2:
        return [block]

    items = [ln for ln in raw_lines if _is_list_like_line(ln)]
    # 至少要有 2 个清单行、且清单行占多数，才认为是"选项/条目堆叠"
    if len(items) < 2 or len(items) < len(raw_lines) * 0.6:
        return [block]

    out: list[dict] = []
    text_buffer: list[str] = []

    def flush_text() -> None:
        if text_buffer:
            merged = " ".join(text_buffer).strip()
            if merged:
                out.append({**block, "text": merged})
            text_buffer.clear()

    for ln in raw_lines:
        if _is_list_like_line(ln):
            flush_text()
            out.append({**block, "text": ln})
        else:
            text_buffer.append(ln)
    flush_text()
    return out or [block]


def apply_list_split(pages) -> int:
    """对整个文档应用清单行拆分，返回新增的块数。"""
    added = 0
    for page in pages:
        new_blocks = []
        for block in page.blocks:
            pieces = split_list_paragraph(block)
            added += len(pieces) - 1
            new_blocks.extend(pieces)
        page.blocks = new_blocks
    return added


# ---------------------------------------------------------------- 主流程
def list_split_wrapper(settings, file, pipe_progress_send, pipe_cancel_message_recv, logger_queue):
    """替代 `pdf2zh_next.high_level._translate_wrapper` 的子进程入口。

    必须是**模块级函数**：multiprocessing spawn 要把 target pickle 后传给子进程，
    闭包/局部函数无法被 pickle（会报 Can't get local object）。踩过这个坑。

    另一个坑：原实现必须放在**模块级变量**里，不能挂在函数属性上——
    pickle 只保存"模块名 + 函数名"，子进程重新 import 本模块时函数属性不会复原，
    但模块级变量会在 import 时重新赋值，所以这样才拿得到。
    """
    _patch_factory()
    _patch_babeldoc_config()
    if _ORIGINAL_WRAPPER is None:
        try:
            pipe_progress_send.send({"type": "error", "error": "无法取得 pdf2zh-next 原始子进程入口"})
        except Exception:  # noqa: BLE001
            pass
        return
    return _ORIGINAL_WRAPPER(settings, file, pipe_progress_send, pipe_cancel_message_recv, logger_queue)


def install_translator_wrapper() -> None:
    """让「清单行拆分」在**真正翻译的那个子进程**里生效。

    踩过的坑（重要）：
      pdf2zh-next 的 `do_translate_async_stream` 把翻译丢进 multiprocessing spawn 子进程，
      而子进程是全新解释器，会重新 import `pdf2zh_next.high_level`，
      所以「在父进程里给 `_create_translator_instance` 打补丁」是无效的——
      子进程里的引用会重新绑定回原版函数；`register_after_fork` 对 spawn 同样不可靠。

    可行做法：
      把 `pdf2zh_next.high_level._translate_wrapper` 换成上面这个**模块级**包装函数，
      同时把原实现存进本模块的 `_ORIGINAL_WRAPPER`。包装体在子进程里执行时
      （unpickle 触发 import 本模块），即可拿到原实现，并在调用它之前
      把该子进程内即将新建的翻译器换成拆分代理。
    """
    global _ORIGINAL_WRAPPER
    if not LIST_SPLIT_ENABLED:
        # 功能关闭时**不能**替换 pdf2zh-next 的子进程入口：
        # 子进程是全新解释器，拿不到本模块里的 _ORIGINAL_WRAPPER，会直接报错。
        return
    try:
        from pdf2zh_next import high_level as pdf2zh_high_level
    except Exception:  # noqa: BLE001
        return

    current = getattr(pdf2zh_high_level, "_translate_wrapper", None)
    if current is None or current is list_split_wrapper:
        return

    _ORIGINAL_WRAPPER = current
    pdf2zh_high_level._translate_wrapper = list_split_wrapper
    _patch_factory()
    _patch_babeldoc_config()
    install_line_structure()


def _patch_babeldoc_config() -> None:
    """兜底：直接替换 TranslationConfig 里已建好的翻译器（覆盖不走工厂的路径）。"""
    try:
        from babeldoc.format.pdf.translation_config import TranslationConfig
    except Exception:  # noqa: BLE001
        return

    init = getattr(TranslationConfig, "__init__", None)
    if init is None or getattr(init, "_list_split_patched", False):
        return

    def patched_init(self, *args, **kwargs):
        init(self, *args, **kwargs)
        try:
            current = getattr(self, "translator", None)
            if current is not None and LIST_SPLIT_ENABLED:
                self.translator = wrap_translator(current, enabled=True)
            term = getattr(self, "term_extraction_translator", None)
            if term is not None and term is not current:
                self.term_extraction_translator = term
        except Exception:  # noqa: BLE001
            pass

    patched_init._list_split_patched = True
    TranslationConfig.__init__ = patched_init


def _patch_factory() -> None:
    """把翻译器工厂的返回值套上拆分代理（幂等）。"""
    try:
        from pdf2zh_next.translator import utils as translator_utils
    except Exception:  # noqa: BLE001
        return

    original = getattr(translator_utils, "_create_translator_instance", None)
    if original is None or getattr(original, "_list_split_patched", False):
        return

    def patched(*args, **kwargs):
        translator, qps, workers = original(*args, **kwargs)
        return wrap_translator(translator, enabled=LIST_SPLIT_ENABLED), qps, workers

    patched._list_split_patched = True
    patched._original = original
    translator_utils._create_translator_instance = patched


def install_line_structure() -> None:
    """启用「保留清单行结构」的提示词补丁（见 webapp/line_structure.py）。

    实测该补丁对最终版面无效（重排按段落粒度折行），默认关闭，代码保留备查。
    """
    if not LIST_SPLIT_ENABLED:
        return
    ok = install_prompt_patch()
    print(f"[行结构] 提示词补丁{'已启用' if ok else '未启用（找不到目标类）'}", file=sys.stderr)


def install_ir_split() -> None:
    """启用「中间表示层拆分清单段落」——真正能修好换行的方案。

    两层配合：
      1) 本进程内直接装补丁（debug 模式 / 同进程执行时生效）；
      2) 设置 PDF_TRANSLATOR_IR_SPLIT=1 并把 webapp 目录加入 PYTHONPATH，
         让 pdf2zh-next 的 **spawn 子进程**在解释器启动时通过 sitecustomize.py
         自动装上同一个补丁（子进程必然继承环境变量，这是唯一可靠的传递方式）。
    """
    try:
        from webapp import ir_split

        ok = ir_split.install()
        # 让子进程也能装上：环境变量 + PYTHONPATH
        os.environ["PDF_TRANSLATOR_IR_SPLIT"] = "1"
        webapp_dir = str(WEBAPP_DIR)
        existing = os.environ.get("PYTHONPATH", "")
        if webapp_dir not in existing.split(os.pathsep):
            os.environ["PYTHONPATH"] = f"{webapp_dir}{os.pathsep}{existing}" if existing else webapp_dir
        print(f"[段落拆分] {'已启用' if ok else '未启用'}（版面解析阶段按行拆分清单段落）", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[段落拆分] 启用失败：{exc}", file=sys.stderr)


async def run(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    output_dir = Path(args.output).resolve()
    input_file = Path(args.input).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)

    # ---- PPT / Word 等 Office 文档：先转成 PDF，再走正常翻译流程 ----
    if input_file.suffix.lower() != ".pdf":
        from webapp.office_convert import OFFICE_EXTS, convert_to_pdf, is_office_file

        if not is_office_file(input_file):
            emit({"type": "error",
                  "message": f"不支持的文件类型：{input_file.suffix}（支持 PDF 与 "
                             f"{', '.join(sorted(OFFICE_EXTS))}）"})
            return 6

        def _conv_progress(done: bool) -> None:
            emit({"type": "progress", "stage": "Convert Office to PDF",
                  "stage_label": "PPT/Word 转 PDF",
                  "stage_current": 1 if done else 0, "stage_total": 1,
                  "overall": 8.0 if done else 2.0, "elapsed": 0.0})

        _conv_progress(False)
        converted = workdir / (input_file.stem + ".converted.pdf")
        mode = getattr(args, "convert_mode", "auto") or "auto"

        def _conv_log(msg: str) -> None:
            emit({"type": "log", "message": f"[转换] {msg}"})

        try:
            await asyncio.to_thread(
                lambda: convert_to_pdf(input_file, converted, mode=mode, on_log=_conv_log)
            )
        except Exception as exc:  # noqa: BLE001
            emit({"type": "error",
                  "message": f"Office 转 PDF 失败：{exc}。请确认本机已安装 Microsoft Office"
                             f"（若已安装，可能是该文件受保护或格式不受支持）。"})
            return 7
        _conv_progress(True)
        input_file = converted
        emit({"type": "log", "message": f"[转换] 已转换为 PDF：{converted.name}"})

        if getattr(args, "convert_only", False):
            # 只做转换（用于验证 Office→PDF 链路，不需要 API Key）
            emit({"type": "convert_only_done", "pdf": str(converted),
                  "size": converted.stat().st_size})
            emit({"type": "finish", "elapsed": 0.0, "dual_pdf": None, "mono_pdf": None,
                  "glossary_csv": None, "converted_pdf": str(converted)})
            return 0
    elif getattr(args, "convert_only", False):
        emit({"type": "convert_only_done", "pdf": str(input_file), "size": input_file.stat().st_size})
        emit({"type": "finish", "elapsed": 0.0, "dual_pdf": None, "mono_pdf": None,
              "glossary_csv": None, "converted_pdf": str(input_file)})
        return 0

    from pdf2zh_next.config.model import SettingsModel
    from pdf2zh_next.config.model import WatermarkOutputMode
    from pdf2zh_next.config.translate_engine_model import DeepSeekSettings
    from pdf2zh_next.config.translate_engine_model import OpenAICompatibleSettings
    from pdf2zh_next.high_level import do_translate_async_stream

    engine = args.engine
    if engine == "deepseek":
        engine_settings = DeepSeekSettings(
            deepseek_model=args.model,
            deepseek_api_key=args.api_key,
            deepseek_enable_json_mode=True,
            deepseek_thinking_mode=None,
        )
    else:
        engine_settings = OpenAICompatibleSettings(
            openai_compatible_model=args.model,
            openai_compatible_api_key=args.api_key,
            openai_compatible_base_url=args.base_url,
        )

    translation_kwargs = {
        "lang_in": args.lang_in,
        "lang_out": args.lang_out,
        "output": str(output_dir),
        "qps": args.qps,
        "min_text_length": 5,
    }
    if args.pages:
        translation_kwargs["min_text_length"] = 5

    # 术语表：上游接受「逗号分隔的 CSV 路径串」，CSV 必须含 source,target 表头
    glossary_arg = getattr(args, "glossaries", "") or ""
    if glossary_arg.strip():
        paths = [p.strip() for p in glossary_arg.split(",") if p.strip()]
        existing = [p for p in paths if Path(p).exists()]
        missing = [p for p in paths if p not in existing]
        if missing:
            print(f"[术语表] 以下文件不存在，已忽略：{missing}", file=sys.stderr)
        if existing:
            translation_kwargs["glossaries"] = ",".join(existing)
            # 关键：上游默认会「自动提取术语」（把它自己提取的术语表喂给模型）。
            # 实测（见 docs/术语表排查记录.md）：一旦同时存在用户术语表，
            # 自动提取的那份会与用户的强制译名冲突，导致用户术语表看起来"不生效"。
            # 因此只要用户显式给了术语表，就关闭自动提取，让用户的术语说了算。
            translation_kwargs["no_auto_extract_glossary"] = not bool(
                getattr(args, "keep_auto_glossary", False)
            )
            if getattr(args, "keep_auto_glossary", False):
                print("[术语表] 已加载用户术语集，且保留上游自动术语提取（可能冲突）", file=sys.stderr)
            else:
                print(f"[术语表] 已加载 {len(existing)} 个术语集（并关闭上游自动术语提取以避免冲突）",
                      file=sys.stderr)

    pdf_kwargs = {
        "pages": args.pages or None,
        "no_dual": False,
        "no_mono": False,              # 同时产出"仅译文"版
        "watermark_output_mode": WatermarkOutputMode.NoWatermark,
        "use_alternating_pages_dual": bool(args.alternating),
        "skip_scanned_detection": bool(args.skip_scanned_detection),
        "auto_enable_ocr_workaround": True,
        "translate_table_text": bool(args.translate_tables),
        # 短行独立成段：题干 + a) b) c) d) 选项这种版式，不开会把选项并成一段
        "split_short_lines": bool(args.split_short_lines),
        "short_line_split_factor": float(args.short_line_split_factor),
    }

    settings = SettingsModel(
        report_interval=0.5,
        # 注意：这里保持 debug=False，让 pdf2zh-next 在独立子进程里翻译。
        # 排查「换行」问题时曾临时开启 debug=True（那样翻译在主进程内跑，便于打补丁），
        # 但它会带来两个副作用，故不作为默认：
        #   1) 输出 PDF 会带 debug 标注（fallback_line / paragraph[..] 等）；
        #   2) 取消任务失效（没有子进程可终止）。
        # 详见 docs/换行问题排查记录.md
        basic={"input_files": [], "debug": False, "gui": False, "warmup": False},
        translation=translation_kwargs,
        pdf=pdf_kwargs,
        translate_engine_settings=engine_settings,
    )
    try:
        settings.validate_settings()
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "message": f"配置校验失败：{exc}"})
        return 2

    started = time.time()
    last_emit = 0.0
    result = None
    try:
        async for event in do_translate_async_stream(settings, str(input_file)):
            etype = event.get("type")
            if etype == "progress_update":
                now = time.time()
                if now - last_emit < 0.4:
                    continue
                last_emit = now
                emit(
                    {
                        "type": "progress",
                        "stage": event.get("stage", ""),
                        "stage_label": label_for(event.get("stage", "")),
                        "stage_current": event.get("stage_current", 0),
                        "stage_total": event.get("stage_total", 0),
                        "overall": round(float(event.get("overall_progress", 0.0)), 2),
                        "elapsed": round(now - started, 1),
                    }
                )
            elif etype == "progress_end":
                emit(
                    {
                        "type": "stage_done",
                        "stage": event.get("stage", ""),
                        "stage_label": label_for(event.get("stage", "")),
                        "overall": round(float(event.get("overall_progress", 0.0)), 2),
                        "elapsed": round(time.time() - started, 1),
                    }
                )
            elif etype == "finish":
                result = event.get("translate_result")
                break
            elif etype == "error":
                emit({"type": "error", "message": str(event.get("error", "未知错误"))})
                return 3
    except asyncio.CancelledError:
        emit({"type": "cancelled"})
        return 130
    except Exception as exc:  # noqa: BLE001
        emit(
            {
                "type": "error",
                "message": str(exc),
                "traceback": traceback.format_exc()[-4000:],
            }
        )
        return 4

    if result is None:
        emit({"type": "error", "message": "翻译过程未返回结果"})
        return 5

    def as_path(value):
        if value is None:
            return None
        p = Path(str(value))
        return str(p) if p.exists() else None

    payload = {
        "type": "finish",
        "elapsed": round(time.time() - started, 1),
        "dual_pdf": as_path(getattr(result, "dual_pdf_path", None)),
        "mono_pdf": as_path(getattr(result, "mono_pdf_path", None)),
        "glossary_csv": as_path(getattr(result, "auto_extracted_glossary_path", None)),
        "char_count": getattr(result, "total_valid_character_count", None),
        "token_count": getattr(result, "total_valid_text_token_count", None),
        "peak_memory_mb": (
            round(getattr(result, "peak_memory_usage", 0) / 1024 / 1024, 1)
            if getattr(result, "peak_memory_usage", None)
            else None
        ),    }
    emit(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="翻译单个 PDF（BabelDOC 引擎，输出双语对照）")
    p.add_argument("--input", required=True, help="输入 PDF 绝对路径")
    p.add_argument("--output", required=True, help="输出目录")
    p.add_argument("--workdir", required=True, help="工作目录（放缓存/中间产物）")
    p.add_argument("--lang-in", default="en")
    p.add_argument("--lang-out", default="zh-CN")
    p.add_argument("--engine", default="deepseek", choices=["deepseek", "openai-compatible"])
    p.add_argument("--model", default="deepseek-chat")
    p.add_argument("--api-key", default="")
    p.add_argument("--base-url", default="https://api.deepseek.com/v1")
    p.add_argument("--pages", default="", help="页码范围，如 1-5,8")
    p.add_argument("--qps", type=int, default=4, help="每秒请求数上限")
    p.add_argument("--alternating", action="store_true", help="对照版按整页交替而不是并排")
    p.add_argument("--skip-scanned-detection", action="store_true")
    p.add_argument("--translate-tables", action="store_true", help="翻译表格文字（实验性）")
    p.add_argument(
        "--split-short-lines",
        action="store_true",
        help="短行独立成段：题干与 a)/b)/c) 选项、列表条目分行时避免被并成一段",
    )
    p.add_argument("--short-line-split-factor", type=float, default=0.8,
                   help="短行判定阈值（上游参数，实测对等宽选项不生效，保留以兼容）")
    p.add_argument("--no-list-split", action="store_true",
                   help="关闭「清单行拆分」（题干与 a)b)c) 选项不再逐行翻译）")
    p.add_argument("--no-ir-split", action="store_true",
                   help="关闭「清单段落按行拆分」（关闭后选项会被并成一段，一般不用关）")
    p.add_argument("--glossaries", default="",
                   help="术语表 CSV 路径，多个用逗号分隔（CSV 需含 source,target 表头）")
    p.add_argument("--keep-auto-glossary", action="store_true",
                   help="即使提供了术语表，也保留上游的「自动术语提取」（默认关闭以避免冲突）")
    p.add_argument("--convert-mode", default="auto",
                   choices=["auto", "native", "printer", "libreoffice"],
                   help="Office(PPT/Word) 转 PDF 的方式：native=Office 原生导出（默认，可靠）；"
                        "printer=Microsoft Print to PDF 虚拟打印机；auto=原生优先失败再退打印机")
    p.add_argument("--convert-only", action="store_true",
                   help="只做 Office→PDF 转换，不翻译（验证转换链路用，不需要 API Key）")
    return p


if __name__ == "__main__":
    if sys.platform == "win32":
        import multiprocessing as mp

        mp.freeze_support()
        mp.set_start_method("spawn", force=True)
    parsed = build_parser().parse_args()
    if getattr(parsed, "no_list_split", False):
        _set_list_split(False)
        print("[清单拆分] 已关闭", file=sys.stderr)
    else:
        install_translator_wrapper()
    # 真正修换行的方案：在版面解析阶段把清单段落按行拆开
    if not getattr(parsed, "no_ir_split", False):
        install_ir_split()
    try:
        code = asyncio.run(run(parsed))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
