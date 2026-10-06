"""对打包后的 exe 做端到端验证：上传 → SSE 进度 → 下载。

用法：
    .venv\\Scripts\\python.exe build\\verify_exe.py <API_KEY> [PORT] [PAGES]
"""
import json
import sys
import time
from pathlib import Path

import requests

KEY = sys.argv[1] if len(sys.argv) > 1 else ""
PORT = sys.argv[2] if len(sys.argv) > 2 else "8850"
PAGES = sys.argv[3] if len(sys.argv) > 3 else "1"
BASE = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "attention-is-all-you-need.pdf"


def main() -> int:
    health = requests.get(f"{BASE}/api/health", timeout=30).json()
    print("health:", json.dumps({k: health[k] for k in ("assets_ready", "assets_mb", "license", "python")}, ensure_ascii=False))
    if not health.get("assets_ready"):
        print("!! 资源未就绪，终止")
        return 1

    with SAMPLE.open("rb") as fh:
        r = requests.post(
            f"{BASE}/api/translate",
            files={"file": (SAMPLE.name, fh, "application/pdf")},
            data={"api_key": KEY, "model": "deepseek-chat", "lang_out": "zh-CN",
                  "pages": PAGES, "qps": "4", "skip_scanned_detection": "true"},
            timeout=300,
        )
    print("upload:", r.status_code, r.text[:200])
    r.raise_for_status()
    job_id = r.json()["job_id"]

    t0 = time.time()
    last = ""
    with requests.get(f"{BASE}/api/jobs/{job_id}/events", stream=True, timeout=1800) as resp:
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data: "):
                continue
            payload = json.loads(raw[6:])
            job, event = payload["job"], payload["event"]
            line = f"[{time.time()-t0:6.1f}s] {job['status']:8s} {job['progress']:5.1f}% {job['stage']}"
            if line != last:
                print(line, flush=True)
                last = line
            if event["type"] in ("done", "error", "closed", "cancelled"):
                print("terminal:", json.dumps(event, ensure_ascii=False)[:300])
                break

    detail = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=30).json()
    print("final:", detail["status"], "elapsed:", detail["elapsed"])
    if detail["status"] != "done":
        print("!! 翻译未成功")
        return 1

    for kind in ("dual", "mono", "glossary"):
        rr = requests.get(f"{BASE}/api/jobs/{job_id}/file/{kind}", timeout=180)
        print(f"download {kind}: HTTP {rr.status_code}, {len(rr.content):,} bytes")
        if kind == "dual":
            out = ROOT / "output" / "exe-verify-dual.pdf"
            out.write_bytes(rr.content)
            print("   ->", out)
    print("✓ exe 端到端验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
