"""前端静态检查：HTML 元素 id 与 JS 引用是否一致（避免运行期 $(id) 拿到 null）。"""
import re
import sys
from pathlib import Path

html_path = Path(__file__).resolve().parent.parent / "webapp" / "static" / "index.html"
html = html_path.read_text(encoding="utf-8")

ids = set(re.findall(r'id="([^"]+)"', html))
# $(...) 调用；同时抓 querySelector 里的 #id
dollar_refs = set(re.findall(r"\$\(\s*'([^']+)'\s*\)", html)) | set(re.findall(r'\$\(\s*"([^"]+)"\s*\)', html))
hash_refs = set(re.findall(r"querySelector(?:All)?\(\s*'#([A-Za-z0-9_-]+)", html))
refs = {r for r in (dollar_refs | hash_refs) if re.fullmatch(r"[A-Za-z0-9_-]+", r)}

missing = sorted(r for r in refs if r not in ids)
unused = sorted(i for i in ids if i not in refs)

print(f"HTML 元素 id：{len(ids)} 个")
print(f"JS 引用到的 id：{len(refs)} 个")
print(f"引用了但 HTML 不存在（会报 null 错误）：{missing if missing else '无 —— 全部匹配 ✅'}")
print(f"定义了但 JS 未直接引用：{len(unused)} 个（多为 CSS/动态拼接使用）")
if unused:
    print("   " + ", ".join(unused[:18]))

# 基本结构检查
for tag in ("<html", "</html>", "<script", "</script>", "<style", "</style>"):
    print(f"  {tag:10s} 出现 {html.count(tag)} 次")

ok = not missing and html.count("<script") == html.count("</script>") and html.count("<style") == html.count("</style>")
print("\n结论:", "通过" if ok else "有问题")
sys.exit(0 if ok else 1)
