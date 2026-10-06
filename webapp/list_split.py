"""清单行拆分：解决「该换行的地方没换行」。

问题（实测结论）
----------------
BabelDOC 的版面解析会把「题干 + a) b) c) d) 选项」这类内容识别成**一个段落**，
而交给翻译模型的段落文本是**不带换行符**的（整段按字符间距拼成一个字符串，只插空格）。
模型无从得知这里原本分行，译出来自然是并成一段——就是用户看到的"该换行没换行"。

试过但无效
----------
上游 `split_short_lines` / `short_line_split_factor`（0.2 / 0.5 / 0.8 全试过）：
它按「前一行宽度 < 页内行宽中位数 × 系数」判定换段，对**等宽的多选项**不会触发。

本模块的做法
------------
在翻译粒度上动手：包一层翻译器代理，遇到「清单式段落」就按选项标记切成多行，
逐行调用翻译，再把结果用换行拼回去。
  * 只对「含有 ≥2 个行首标记、且标记之间的片段都较短」的段落生效，普通正文不受影响；
  * 不改上游代码，只包装调用链；
  * 拆分结构完全由文本决定，可预测、可关闭（--no-list-split）。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

# 诊断埋点：设置 PDF_TRANSLATOR_TRACE=路径 可把拆分与调用情况写入文件
_TRACE_PATH = os.environ.get("PDF_TRANSLATOR_TRACE", "")
_DEBUG = os.environ.get("PDF_TRANSLATOR_DEBUG", "") not in ("", "0", "false")


def _trace(msg: str) -> None:
    if not _TRACE_PATH:
        return
    try:
        with Path(_TRACE_PATH).open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _dbg(msg: str, payload: str | None = None) -> None:
    if not _DEBUG:
        return
    import sys as _sys

    print(f"[list_split] {msg}", file=_sys.stderr, flush=True)
    if payload is not None and _TRACE_PATH:
        try:
            with Path(_TRACE_PATH + ".prompt").open("a", encoding="utf-8") as fh:
                fh.write("\n" + "=" * 80 + f"\n{msg}\n" + "-" * 80 + f"\n{payload}\n")
        except OSError:
            pass

# 行首标记：a) b. c、 (a) / 1) 2. 3、 (1) / i) ii. / 项目符号 / 编号小节
MARKER_RE = re.compile(
    r"(?:^|(?<=\s))("
    r"[\(\[]?[a-hA-H][\)\].、]|"
    r"[\(\[]?\d{1,2}[\)\].、]|"
    r"[\(\[]?[ivxIVX]{1,4}[\)\.]|"
    r"[•·▪◦‣∙*]|"
    r"[-–—](?=\s)|"
    r"\d+\.\d+(?=\s)"
    r")(?=\s|$|\S)"
)

# 单个片段的最大长度：超过就说明是正常句子里的 "a)"，不是选项
MAX_ITEM_CHARS = 88
MIN_ITEMS = 2


def split_listish(text: str) -> list[str] | None:
    """如果是清单式段落，返回按行切好的列表；否则返回 None。"""
    if not text or len(text) < 12 or len(text) > 4000:
        return None

    matches = list(MARKER_RE.finditer(text))
    if len(matches) < MIN_ITEMS:
        return None

    # 以标记位置切段（第一段是标记之前的题干）
    pieces: list[str] = []
    prefix = text[: matches[0].start()].strip()
    if prefix:
        pieces.append(prefix)
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg = text[m.start(): end].strip()
        if seg:
            pieces.append(seg)
    if len(pieces) < MIN_ITEMS + 1:  # 题干 + 至少两个选项
        return None

    # 校验：除题干外，每个片段都要"像一条"（不太长、不是完整长句）
    items = pieces[1:]
    if any(len(p) > MAX_ITEM_CHARS for p in items):
        return None
    if sum(1 for p in items if len(p) <= MAX_ITEM_CHARS) < len(items) * 0.8:
        return None
    # 题干也不该太长（太长说明整段是正文，标记只是巧合）
    if pieces[0] and len(pieces[0]) > 260:
        return None
    return pieces


class ListSplittingTranslator:
    """代理翻译器：清单段落逐行翻译，其余原样委托。"""

    def __init__(self, inner, enabled: bool = True, on_split=None):
        self._inner = inner
        self._enabled = enabled
        self._on_split = on_split
        self.split_count = 0
        _dbg(f"代理已创建，内层={type(inner).__name__}")

    # ---- 属性透传：让上游把它当成原来的翻译器 ----
    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __str__(self):
        return str(self._inner)

    # ---- 单条翻译 ----
    def do_translate(self, text, rate_limit_params=None):
        pieces = self._maybe_split(text)
        if not pieces:
            return self._inner.do_translate(text, rate_limit_params)
        outs = [self._inner.do_translate(p, rate_limit_params) or "" for p in pieces]
        return "\n".join(o.strip() for o in outs if o is not None)

    def do_llm_translate(self, text, rate_limit_params=None):
        pieces = self._maybe_split(text)
        if not pieces:
            return self._inner.do_llm_translate(text, rate_limit_params)
        outs = [self._inner.do_llm_translate(p, rate_limit_params) or "" for p in pieces]
        return "\n".join(o.strip() for o in outs if o is not None)

    # ---- 批量翻译 ----
    def llm_translate(self, text, ignore_cache=False, rate_limit_params=None):
        target = getattr(self._inner, "llm_translate", None)
        pieces = self._maybe_split(text)
        if not pieces or target is None:
            if target is not None:
                return target(text, ignore_cache=ignore_cache, rate_limit_params=rate_limit_params)
            return self.do_llm_translate(text, rate_limit_params)
        _trace(f"llm_translate 拆分: {len(text)} 字 -> {len(pieces)} 段")
        outs = [
            target(p, ignore_cache=ignore_cache, rate_limit_params=rate_limit_params)
            for p in pieces
        ]
        _trace(f"llm_translate 完成: {len(pieces)} 段")
        return "\n".join((o or "").strip() for o in outs)

    # ---- 内部 ----
    def _maybe_split(self, text) -> list[str] | None:
        if not self._enabled or not isinstance(text, str):
            return None
        pieces = split_listish(text)
        if pieces:
            self.split_count += 1
            _dbg(f"命中拆分 #{self.split_count}: {len(pieces)} 段 | {text[:50]!r}")
            _trace(f"拆分命中 #{self.split_count}: {len(pieces)} 段 | 首段={pieces[0][:40]!r}")
            if self._on_split:
                try:
                    self._on_split(pieces)
                except Exception:  # noqa: BLE001
                    pass
        else:
            _dbg(f"未命中: 长度={len(text)} 首 40 字={text[:40]!r}", payload=text if len(text) < 8000 else text[:8000])
        return pieces


def wrap_translator(translator, enabled: bool = True, on_split=None):
    """给翻译器套上清单拆分代理（幂等）。"""
    if not enabled or isinstance(translator, ListSplittingTranslator):
        return translator
    return ListSplittingTranslator(translator, enabled=enabled, on_split=on_split)
