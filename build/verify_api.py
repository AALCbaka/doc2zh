"""验证打包版接口：历史恢复 / 下载 / 图片预览。用法：python build/verify_api.py [PORT]"""
import json
import sys
import urllib.error
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8871"
B = f"http://127.0.0.1:{PORT}"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def get(path: str):
    try:
        with urllib.request.urlopen(B + path, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


status, body = get("/api/jobs?limit=10")
jobs = json.loads(body)["jobs"]
print(f"=== 历史任务 === HTTP {status}，共 {len(jobs)} 条")
for j in jobs:
    res = j.get("result") or {}
    print(f"  {j['id']}  {j['filename']}  状态={j['status']}  dual={'有' if res.get('dual_pdf') else '无'}")
if not jobs:
    sys.exit("没有可测任务")

jid = jobs[0]["id"]
print(f"\n=== 下载接口（任务 {jid}）===")
for kind in ("dual", "mono", "glossary"):
    s, b = get(f"/api/jobs/{jid}/file/{kind}?download=1")
    head = b[:5]
    kindname = "PDF" if head.startswith(b"%PDF") else ("CSV" if kind == "glossary" else "?")
    print(f"  {kind:9s} HTTP {s}  {len(b):>10,} bytes  格式={kindname}")

print("\n=== 图片预览接口 ===")
for kind in ("dual", "source", "mono"):
    s, b = get(f"/api/jobs/{jid}/pages/{kind}")
    pages = json.loads(b).get("pages") if s == 200 else "?"
    s2, img = get(f"/api/jobs/{jid}/page/{kind}/1?dpi=90")
    ok = img[:8] == PNG_MAGIC
    print(f"  {kind:9s} 共{pages}页  第1页 HTTP {s2}  {len(img):>9,} bytes  PNG签名={ok}  前8字节={img[:8]!r}")
    if not ok:
        print(f"      !! 返回内容不是 PNG，前 200 字节：{img[:200]!r}")

print("\n=== 末页也能渲染（边界检查）===")
s, b = get(f"/api/jobs/{jid}/pages/dual")
total = json.loads(b)["pages"]
s2, img = get(f"/api/jobs/{jid}/page/dual/{total}?dpi=80")
print(f"  第 {total} 页 HTTP {s2} {len(img):,} bytes PNG={img[:8] == PNG_MAGIC}")
s3, _ = get(f"/api/jobs/{jid}/page/dual/{total + 5}?dpi=80")
print(f"  超出范围的页 → HTTP {s3}（应为 404）")
