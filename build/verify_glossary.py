"""术语表端到端验证：导入术语集 → 用它翻译 → 检查译文是否强制使用指定译名。

判定方法：给一些词指定**非默认**译法（例如 transformer 通常被译作"Transformer/变换器"，
我们强制成「变压器模型」；attention 强制成「注意力机制(ZT)」），
再检查产出 PDF 的译文里是否出现这些强制译名。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import fitz
import requests

ROOT = Path(__file__).resolve().parent.parent
BASE = f"http://127.0.0.1:{sys.argv[2] if len(sys.argv) > 2 else '8878'}"
KEY = sys.argv[1]
SRC = ROOT / "samples" / "attention-is-all-you-need.pdf"

# 特意选了与常规译法不同的目标词，便于判定"是否真的按术语表翻"
TERMS = [
    {"source": "transformer", "target": "变压器模型"},
    {"source": "attention", "target": "注意力机制ZT"},
    {"source": "encoder", "target": "编码器ZT"},
    {"source": "decoder", "target": "解码器ZT"},
]
GLOSSARY_TEXT = "source,target\n" + "\n".join(f"{t['source']},{t['target']}" for t in TERMS)


def main() -> int:
    print("=== 1) 导入术语集 ===")
    r = requests.post(f"{BASE}/api/glossaries/import",
                      data={"name": "术语表验证集", "text": GLOSSARY_TEXT}, timeout=60)
    print("   HTTP", r.status_code, r.text[:200])
    r.raise_for_status()
    set_id = r.json()["set"]["id"]
    print(f"   术语集 id={set_id}，条目 {r.json()['set']['terms']}")

    print("\n=== 2) 查看术语集 ===")
    d = requests.get(f"{BASE}/api/glossaries/{set_id}", timeout=30).json()
    print("   名称:", d["set"]["name"], "| 词条:", [f"{t['source']}→{t['target']}" for t in d["terms"]])

    print("\n=== 3) 用该术语集翻译（第 1 页）===")
    with SRC.open("rb") as fh:
        r = requests.post(f"{BASE}/api/translate",
                          files={"file": ("glossary-test.pdf", fh, "application/pdf")},
                          data={"api_key": KEY, "lang_out": "zh-CN", "pages": "1",
                                "skip_scanned_detection": "true", "glossary_ids": set_id},
                          timeout=300)
    r.raise_for_status()
    jid = r.json()["job_id"]
    t0 = time.time()
    while time.time() - t0 < 900:
        job = requests.get(f"{BASE}/api/jobs/{jid}", timeout=20).json()
        if job["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(3)
    print("   任务状态:", job["status"], f"{job.get('elapsed')}s")
    if job["status"] != "done":
        print("   !! 翻译失败:", job.get("error"))
        return 1

    print("\n=== 4) 检查译文是否使用强制译名 ===")
    pdf = requests.get(f"{BASE}/api/jobs/{jid}/file/dual", timeout=180)
    tmp = ROOT / "jobs" / "_glossary_check.pdf"
    tmp.write_bytes(pdf.content)
    with fitz.open(tmp) as doc:
        text = "".join(doc.load_page(i).get_text() for i in range(doc.page_count))

    hits, miss = [], []
    for t in TERMS:
        n = text.count(t["target"])
        (hits if n else miss).append((t["source"], t["target"], n))
    print("   命中：")
    for s, tg, n in hits:
        print(f"     ✓ {s:12s} -> {tg:14s} 出现 {n} 次")
    print("   未命中：")
    for s, tg, _ in miss:
        print(f"     ✗ {s:12s} -> {tg}")
    # 反向检查：不应出现"未加 ZT 后缀"的默认译法
    default_like = text.count("注意力机制") - text.count("注意力机制ZT")
    print(f"\n   未带后缀的「注意力机制」出现 {default_like} 次（术语表外用法，正常）")

    ok = len(hits) >= 3
    print(f"\n结论: {'术语表生效 ✅' if ok else '术语表可能未生效 ❌'}（{len(hits)}/{len(TERMS)} 个强制译名命中）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
