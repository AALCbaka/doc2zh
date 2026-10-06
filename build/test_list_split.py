"""最小单测：清单拆分规则 + 补丁是否真的挂上翻译器。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.list_split import split_listish

SAMPLES = [
    ("6. Identify the population of interest to the researcher. a) All UM students during the academic year 2026/2027. "
     "b) All college students in Macao during the academic year 2026/2027. "
     "c) All first-year UM students during the academic year 2026/2027. d) The 250 students who were monitored.", "应拆分"),
    ("7. Identify the variable of interest to the researcher. a) The textbook cost of first-year UM students. "
     "b) The year in school of UM students. c) The age of UM students. "
     "d) The cost of incidental expenses of UM students.", "应拆分"),
    ("Most analysts focus on the cost of tuition as the way to measure the cost of a college education. "
     "But incidentals, such as textbook costs, are rarely considered.", "不应拆分（普通正文）"),
    ("A researcher wishes to estimate the textbook costs of first-year students at UM during the academic year 2026/2027.", "不应拆分"),
    ("i) first item ii) second item iii) third item", "应拆分"),
    ("The study of a) statistics b) economics is interesting because both fields analyze data.", "边界：句中标记，理想不拆"),
]

print("=== 规则单测 split_listish ===")
ok = 0
for text, expect in SAMPLES:
    out = split_listish(text)
    got = "拆分" if out else "不拆"
    marks = f"{len(out)} 段" if out else ""
    flag = "✓" if (("应拆分" in expect and out) or ("不应拆分" in expect and not out) or "边界" in expect) else "✗"
    if flag == "✓":
        ok += 1
    print(f"  {flag} {got} {marks:8s} | {expect}")
    if out:
        for p in out[:6]:
            print(f"        - {p[:78]}")
print(f"通过 {ok}/{len(SAMPLES)}（边界样例不参与判定）")

print("\n=== 补丁是否挂上翻译器 ===")
from webapp import translate_job as tj

tj.install_translator_wrapper()
from pdf2zh_next.translator import utils as tu

print("  工厂已打补丁:", getattr(tu._create_translator_instance, "_list_split_patched", False))

from babeldoc.format.pdf.translation_config import TranslationConfig

print("  TranslationConfig.__init__ 已打补丁:", getattr(TranslationConfig.__init__, "_list_split_patched", False))

from pdf2zh_next import high_level as hl

print("  子进程入口已替换:", hl._translate_wrapper is tj.list_split_wrapper)
print("  原实现已保存:", tj._ORIGINAL_WRAPPER is not None)

# 构造一个假翻译器，验证代理真的会拆分
class FakeTranslator:
    name = "fake"

    def __init__(self):
        self.calls = []

    def do_translate(self, text, rate_limit_params=None):
        self.calls.append(text)
        return f"[译]{text[:12]}"

    def llm_translate(self, text, ignore_cache=False, rate_limit_params=None):
        self.calls.append(text)
        return f"[批量译]{text[:12]}"


fake = FakeTranslator()
wrapped = tj.wrap_translator(fake)
text = SAMPLES[0][0]
out = wrapped.llm_translate(text)
print("\n=== 代理行为验证 ===")
print(f"  原始调用数 {len(fake.calls)}（拆成 N 段就该是 N 次）")
print(f"  返回是否含换行: {chr(10) in out}")
print(f"  返回首 60 字: {out[:60]!r}")
