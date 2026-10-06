"""批量翻译端到端验证：一次提交多个 PDF → 观察排队/并发 → 校验产出。

用法：python build/verify_batch.py <API_KEY> [PORT]
"""
import json
import shutil
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
KEY = sys.argv[1]
PORT = sys.argv[2] if len(sys.argv) > 2 else "8875"
BASE = f"http://127.0.0.1:{PORT}"

# 造 3 个不同的输入文件（同一份 PDF 复制改名，验证批量链路的并发与隔离）
SRC = ROOT / "samples" / "attention-is-all-you-need.pdf"
TMP = ROOT / "jobs" / "_batch_in"
TMP.mkdir(parents=True, exist_ok=True)
names = ["batch-A.pdf", "batch-B.pdf", "batch-C.pdf"]
for n in names:
    shutil.copy2(SRC, TMP / n)


def main() -> int:
    health = requests.get(f"{BASE}/api/health", timeout=20).json()
    print("health:", json.dumps({k: health[k] for k in
          ("assets_ready", "max_batch_files", "max_concurrent")}, ensure_ascii=False))

    files = [("files", (n, (TMP / n).open("rb"), "application/pdf")) for n in names]
    t0 = time.time()
    r = requests.post(f"{BASE}/api/translate/batch", files=files,
                      data={"api_key": KEY, "lang_out": "zh-CN", "pages": "1",
                            "qps": "4", "skip_scanned_detection": "true"}, timeout=600)
    print(f"批量提交: HTTP {r.status_code} {r.text[:260]}")
    r.raise_for_status()
    payload = r.json()
    jobs = payload["jobs"]
    print(f"创建 {payload['count']} 个任务，并发上限 {payload['max_concurrent']}，拒绝 {len(payload['failed'])} 个")

    # 轮询直到全部结束，记录并发峰值与排队位置变化
    peak_running = 0
    seen_queue_pos = {}
    deadline = time.time() + 1800
    while time.time() < deadline:
        details = []
        for j in jobs:
            try:
                details.append(requests.get(f"{BASE}/api/jobs/{j['job_id']}", timeout=20).json())
            except Exception:  # noqa: BLE001
                pass
        running = sum(1 for d in details if d["status"] == "running")
        done = sum(1 for d in details if d["status"] == "done")
        failed = sum(1 for d in details if d["status"] == "error")
        peak_running = max(peak_running, running)
        for d in details:
            if d["status"] == "queued" and d.get("queue_position"):
                seen_queue_pos[d["filename"]] = d["queue_position"]
        print(f"[{time.time()-t0:6.1f}s] 运行中 {running} / 完成 {done} / 失败 {failed}", flush=True)
        if done + failed == len(jobs):
            break
        time.sleep(4)

    print(f"\n并发峰值: {peak_running}（上限 {payload['max_concurrent']}）")
    print("排队位置观测:", seen_queue_pos or "（都是一提交就开始，未出现排队）")

    ok = 0
    for j in jobs:
        d = requests.get(f"{BASE}/api/jobs/{j['job_id']}", timeout=30).json()
        res = d.get("result") or {}
        status = d["status"]
        size = "—"
        if status == "done":
            rr = requests.get(f"{BASE}/api/jobs/{j['job_id']}/file/dual", timeout=180)
            size = f"{len(rr.content):,} bytes"
            ok += 1
        print(f"  {j['filename']:14s} {status:8s} 耗时 {d.get('elapsed')}s  对照版 {size}")
    print(f"\n成功 {ok}/{len(jobs)}")
    return 0 if ok == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
