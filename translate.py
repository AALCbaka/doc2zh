"""PDF 英译中翻译（命令行版）

用法：
    .venv\\Scripts\\python.exe translate.py 输入.pdf --api-key sk-xxx
    .venv\\Scripts\\python.exe translate.py 输入.pdf --api-key sk-xxx --pages 1-5

输出：在 --out 目录（默认 ./output）生成
    <文件名>.zh-CN.dual.pdf   中英对照版（同页左右并排）
    <文件名>.zh-CN.mono.pdf   仅译文版（加 --mono）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="PDF 英译中（DeepSeek + BabelDOC，输出中英对照）")
    parser.add_argument("input", help="输入 PDF 路径")
    parser.add_argument("--api-key", default="", help="DeepSeek API Key（或设环境变量 DEEPSEEK_API_KEY）")
    parser.add_argument("--model", default="deepseek-chat", help="deepseek-chat / deepseek-reasoner")
    parser.add_argument("--out", default=str(PROJECT / "output"), help="输出目录")
    parser.add_argument("--workdir", default=str(PROJECT / "jobs" / "_cli_work"), help="工作目录")
    parser.add_argument("--lang-in", default="en")
    parser.add_argument("--lang-out", default="zh-CN")
    parser.add_argument("--pages", default="", help="页码范围，如 1-5,8；留空=全部")
    parser.add_argument("--qps", type=int, default=4, help="每秒请求上限")
    parser.add_argument("--mono", action="store_true", help="额外输出仅译文版")
    parser.add_argument("--alternating", action="store_true", help="对照版按整页交替")
    parser.add_argument("--translate-tables", action="store_true", help="翻译表格文字（实验性）")
    args = parser.parse_args()

    import os

    api_key = (args.api_key or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
    if not api_key:
        print("错误：缺少 API Key。用 --api-key 传入，或设置环境变量 DEEPSEEK_API_KEY。", file=sys.stderr)
        return 2

    pdf = Path(args.input).resolve()
    if not pdf.exists():
        print(f"错误：找不到文件 {pdf}", file=sys.stderr)
        return 2

    cmd = [
        sys.executable,
        str(PROJECT / "webapp" / "translate_job.py"),
        "--input", str(pdf),
        "--output", str(Path(args.out).resolve()),
        "--workdir", str(Path(args.workdir).resolve()),
        "--lang-in", args.lang_in,
        "--lang-out", args.lang_out,
        "--engine", "deepseek",
        "--model", args.model,
        "--api-key", api_key,
        "--qps", str(args.qps),
    ]
    if args.pages:
        cmd += ["--pages", args.pages]
    if args.alternating:
        cmd.append("--alternating")
    if args.translate_tables:
        cmd.append("--translate-tables")

    return asyncio.run(_run(cmd, args))


async def _run(cmd: list[str], args) -> int:
    mono_requested = args.mono
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    async def pump_stderr():
        assert proc.stderr
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            low = text.lower()
            if any(k in low for k in ("error", "failed", "traceback", "exception")):
                print("  [日志] " + text[:200], file=sys.stderr)

    stderr_task = asyncio.create_task(pump_stderr())
    last_stage = None
    result = None
    assert proc.stdout
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        text = raw.decode("utf-8", "replace").strip()
        if not text.startswith("{"):
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "progress":
            stage = event.get("stage_label") or event.get("stage")
            overall = event.get("overall", 0)
            if stage != last_stage:
                last_stage = stage
                print(f"\n▶ {stage}", flush=True)
            bar_len = int(overall / 100 * 30)
            print(f"\r  [{'█' * bar_len}{'·' * (30 - bar_len)}] {overall:5.1f}%  已用 {event.get('elapsed', 0)}s", end="", flush=True)
        elif etype == "error":
            print(f"\n✗ 失败：{event.get('message')}", file=sys.stderr)
        elif etype == "finish":
            result = event
    code = await proc.wait()
    await stderr_task
    print()

    if code != 0 or not result:
        print(f"✗ 翻译未完成（退出码 {code}）", file=sys.stderr)
        return code or 1

    dual = result.get("dual_pdf")
    print("✓ 完成，用时 %.1fs" % (result.get("elapsed") or 0))
    if result.get("char_count"):
        print("  翻译字符数：" + f"{result['char_count']:,}")
    if result.get("token_count"):
        print("  token 估算：" + f"{result['token_count']:,}")
    if dual:
        print("  对照版：" + dual)
        if mono_requested:
            print("  （BabelDOC 同时在输出目录生成了仅译文版，文件名含 .mono.pdf）")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        import multiprocessing as mp

        mp.freeze_support()
    sys.exit(main())
