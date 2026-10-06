"""保留清单行结构：让译文里「a) b) c) d)」各自成行。

问题（逐步实测确认）
--------------------
1. 版面解析把「题干 + a) b) c) d) 选项」识别成**一个段落**；
2. 该段落交给模型时，内部的换行早就没了（整段按字符间距拼成一个字符串）；
3. 上游提示词里有一条硬规则：“Do NOT merge paragraphs, **split paragraphs**, …”
   —— 模型即使猜到原本分行，也被这条规则禁止拆分；
4. 结果就是用户看到的：该换行的地方没换行。

试过但无效
----------
上游 `split_short_lines` / `short_line_split_factor`（0.2 / 0.5 / 0.8 都试过）：
按「行宽 < 页内行宽中位数 × 系数」判定换段，对**等宽的多选项**不触发。

本模块的做法（配合 pdf2zh-next 的 debug 模式在**主进程**内翻译）
--------------------------------------------------------------
两件事一起做，缺一不可：
  A. 在提示词里补一条规则：段落内部若有选项/条目，输出的换行数要与输入一致
     （并说明这是规则 2 的例外，否则会和“不许拆段”冲突）
  B. 在每个段落的输入文本里，把选项标记处替换成真正的换行符，
     让模型“看得见”行边界，能按行一一对应地译。

只对符合清单特征的段落（≥2 个行首标记、条目都不长）生效，普通正文段落原样不动。
"""
from __future__ import annotations

import json
import re

from .list_split import split_listish

_INSTRUCTION = (
    "\n## Line Structure Exception (IMPORTANT)\n"
    "Rule 2 forbids splitting paragraphs — **this is the only exception**:\n"
    "If a paragraph's \"input\" text contains internal line breaks (\\n), it is a list / "
    "multiple-choice block (e.g. a question followed by a) b) c) d) options, or numbered items).\n"
    "- Translate each line separately.\n"
    "- Your \"output\" MUST keep **exactly the same number of lines** as that \"input\", "
    "in the same order, one translated line per source line.\n"
    "- Never merge those lines back into one line, and never invent extra lines.\n"
    "- Keep the option markers themselves (a) b) c) d), 1. 2., bullets) at the start of each line.\n"
)

_APPLIED = False


def _inject_line_breaks(text: str) -> str | None:
    """把清单段落的文本按条目插入换行；不是清单则返回 None。"""
    pieces = split_listish(text)
    if not pieces:
        return None
    return "\n".join(pieces)


def _rewrite_json_inputs(json_input_str: str) -> tuple[str, int]:
    """解析提示词里的段落 JSON，对清单段落插入换行。返回 (新 JSON, 改写段数)。"""
    try:
        data = json.loads(json_input_str)
    except (ValueError, TypeError):
        return json_input_str, 0
    if not isinstance(data, list):
        return json_input_str, 0

    changed = 0
    for obj in data:
        if not isinstance(obj, dict):
            continue
        raw = obj.get("input")
        if not isinstance(raw, str):
            continue
        rebuilt = _inject_line_breaks(raw)
        if rebuilt and rebuilt != raw:
            obj["input"] = rebuilt
            changed += 1
    if not changed:
        return json_input_str, 0
    return json.dumps(data, ensure_ascii=False, indent=2), changed


def install_prompt_patch() -> bool:
    """包装 `_build_llm_prompt`：补规则 + 给清单段落插入换行。"""
    global _APPLIED
    if _APPLIED:
        return True
    try:
        from babeldoc.format.pdf.document_il.midend import il_translator_llm_only as mod
    except Exception:  # noqa: BLE001
        return False

    target_cls = getattr(mod, "ILTranslatorLLMOnly", None)
    if target_cls is None:
        return False
    original = getattr(target_cls, "_build_llm_prompt", None)
    if original is None or getattr(original, "_list_line_patched", False):
        _APPLIED = True
        return True

    def patched(self, json_input_str, title_paragraph, local_title_paragraph, batch_text_for_glossary_matching):
        new_json, changed = _rewrite_json_inputs(json_input_str)
        if changed:
            batch_text = "\n".join(
                item.get("input", "")
                for item in (json.loads(new_json) if isinstance(new_json, str) else [])
                if isinstance(item, dict)
            ) or batch_text_for_glossary_matching
        else:
            batch_text = batch_text_for_glossary_matching
        prompt = original(self, new_json, title_paragraph, local_title_paragraph, batch_text)
        if changed and "Line Structure Exception" not in prompt:
            # 插在第一个二级标题之前，位置显眼且不破坏模板其它部分
            marker = "\n## "
            idx = prompt.find(marker)
            prompt = prompt[:idx] + _INSTRUCTION + prompt[idx:] if idx > 0 else prompt + _INSTRUCTION
        return prompt

    patched._list_line_patched = True
    patched._original = original
    target_cls._build_llm_prompt = patched
    _APPLIED = True
    return True
