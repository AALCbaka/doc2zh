"""翻译任务管理：子进程调度 + 事件流 + 结果登记。

每个任务独立目录、独立子进程：
  一次上传 → 一次翻译 → 可随时取消 → 产出双语文档
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
WEBAPP_DIR = PROJECT_DIR / "webapp"


def _resolve_jobs_dir() -> Path:
    """任务数据目录（必须可写、且跨版本稳定）。

    踩过的坑：打包后 `__file__` 落在只读的 `_internal` 里，
    若把 jobs 目录挂在 PROJECT_DIR 下，会导到两个后果：
      1) 任务历史每次重装/重建都"丢失"（其实被写进了 _internal 或根本读不到）；
      2) 用户以为文件没了。
    因此与术语表同一策略：打包运行用 **exe 同级的 jobs/**（便携、看得见），
    源码运行用仓库下的 jobs/。可用环境变量 JOBS_DIR 覆盖。
    """
    env = os.environ.get("JOBS_DIR", "").strip()
    if env:
        target = Path(env).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        return target

    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT_DIR
    target = base / "jobs"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return target
    except OSError:
        fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
        fallback = fallback / "PDFBilingualTranslator" / "jobs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


JOBS_DIR = _resolve_jobs_dir()
VENV_PYTHON = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
FALLBACK_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"


@dataclass
class Job:
    id: str
    filename: str
    status: str = "queued"  # queued | running | done | error | cancelled
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: float = 0.0
    stage: str = "排队中"
    events: list[dict] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    params: dict = field(default_factory=dict)
    process: asyncio.subprocess.Process | None = None
    dir: Path | None = None
    elapsed: float = 0.0
    queue_position: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, event: dict) -> None:
        with self._lock:
            self.events.append(event)
            self.updated_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            events = list(self.events)
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "elapsed": self.elapsed,
            "result": self.result,
            "error": self.error,
            "params": self.params,
            "events": events[-40:],
            "queue_position": self.queue_position,
        }


def python_executable() -> str:
    if getattr(sys, "frozen", False):
        # 打包后：子进程重新拉起本 exe，用 --run-job 分发到翻译逻辑
        return sys.executable
    if VENV_PYTHON.exists():
        return str(VENV_PYTHON)
    if FALLBACK_PYTHON.exists():
        return str(FALLBACK_PYTHON)
    return sys.executable


def job_command(args: list[str]) -> list[str]:
    """组装「执行单个翻译任务」的子进程命令行（源码模式 / 打包模式不同）。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-job", *args]
    return [python_executable(), str(WEBAPP_DIR / "translate_job.py"), *args]


class JobManager:
    def __init__(self, max_concurrent: int = 0) -> None:
        self.jobs: dict[str, Job] = {}
        self._order: deque[str] = deque(maxlen=200)
        # 并发上限：每个翻译子进程约 1GB 内存（要加载版面模型），按可用内存保守取值
        self.max_concurrent = max_concurrent or self._auto_concurrency()
        self._sem: asyncio.Semaphore | None = None
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self.rescan()

    @staticmethod
    def _auto_concurrency() -> int:
        """按可用内存自动决定并发数（>8GB 跑 3 个，>4GB 跑 2 个，否则 1 个）。"""
        try:
            import psutil

            avail_gb = psutil.virtual_memory().available / 1024**3
            if avail_gb > 8:
                return 3
            if avail_gb > 4:
                return 2
            return 1
        except Exception:  # noqa: BLE001
            return 1

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(max(1, self.max_concurrent))
        return self._sem

    def queue_position(self, job_id: str) -> int:
        """排队位置：1 = 下一个就跑，0 = 已在运行或已结束。"""
        pending = [i for i in self._order if i in self.jobs and self.jobs[i].status == "queued"]
        if job_id in pending:
            return pending.index(job_id) + 1
        return 0

    def refresh_queue_positions(self) -> None:
        """把排队位置写进各任务，供界面显示"前面还有 N 个"。"""
        pending = [i for i in self._order if i in self.jobs and self.jobs[i].status == "queued"]
        for idx, jid in enumerate(pending, start=1):
            job = self.jobs[jid]
            job.queue_position = idx
            job.stage = f"排队中（第 {idx} 位 / 并发上限 {self.max_concurrent}）"
        for jid, job in self.jobs.items():
            if job.status != "queued":
                job.queue_position = 0

    # ------------------------------------------------------------ 从磁盘重建
    def rescan(self) -> int:
        """扫描 jobs/ 目录，把历史任务重新登记进来。

        为什么必须有这个：任务原先只存在内存里，程序一重启（或换了进程），
        用户之前翻译好的文件虽然还在磁盘上，界面上却找不到、下载会 404。
        """
        restored = 0
        skip = {"_incoming", "_cli_work", "_t"}
        try:
            candidates = [d for d in JOBS_DIR.iterdir() if d.is_dir() and d.name not in skip]
        except OSError:
            return 0

        entries = []
        for job_dir in candidates:
            job_id = job_dir.name
            if job_id in self.jobs:
                continue
            input_dir = job_dir / "input"
            output_dir = job_dir / "output"
            inputs = [f for f in input_dir.glob("*") if f.is_file()] if input_dir.is_dir() else []
            if not inputs:
                continue
            input_file = max(inputs, key=lambda p: p.stat().st_mtime)

            def newest(pattern: str):
                if not output_dir.is_dir():
                    return None
                hits = [p for p in output_dir.glob(pattern) if p.is_file()]
                return max(hits, key=lambda p: p.stat().st_mtime) if hits else None

            dual = newest("*dual*.pdf")
            mono = newest("*mono*.pdf")
            glossary = newest("*.csv")

            job = Job(
                id=job_id,
                filename=input_file.name,
                params={"restored": True},
                dir=job_dir,
            )
            job.created_at = input_file.stat().st_mtime
            job.updated_at = job.created_at
            if dual:
                job.result = {
                    "dual_pdf": str(dual),
                    "mono_pdf": str(mono) if mono else None,
                    "glossary_csv": str(glossary) if glossary else None,
                    "elapsed": None,
                }
                job.status = "done"
                job.stage = "历史任务（文件已在磁盘）"
                job.progress = 100.0
                job.elapsed = round(max(0.0, dual.stat().st_mtime - job.created_at), 1)
            else:
                job.status = "error"
                job.error = "历史任务：未找到输出文件"
                job.stage = "无产物"
            job.push({"type": "restored", "message": "从磁盘恢复的历史任务"})
            self.jobs[job_id] = job
            entries.append((job.created_at, job_id))
            restored += 1

        for _, job_id in sorted(entries):
            self._order.append(job_id)
        return restored

    # ------------------------------------------------------------ 创建任务
    def create(self, *, upload_path: Path, original_name: str, params: dict) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job_dir = JOBS_DIR / job_id
        (job_dir / "input").mkdir(parents=True, exist_ok=True)
        (job_dir / "output").mkdir(parents=True, exist_ok=True)
        (job_dir / "work").mkdir(parents=True, exist_ok=True)

        safe_name = sanitize_name(original_name)
        input_path = job_dir / "input" / safe_name
        shutil.move(str(upload_path), input_path)

        job = Job(id=job_id, filename=safe_name, params=params, dir=job_dir)
        job.push({"type": "queued", "message": "任务已创建，等待开始"})
        self.jobs[job_id] = job
        self._order.append(job_id)
        return job

    def get(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            # 可能是本次进程启动之后才出现的目录（例如命令行模式产生的），补扫一次
            if (JOBS_DIR / job_id).is_dir():
                self.rescan()
                job = self.jobs.get(job_id)
        return job

    def recent(self, limit: int = 20) -> list[dict]:
        self.refresh_queue_positions()
        ids = [i for i in reversed(self._order) if i in self.jobs]
        return [self.jobs[i].snapshot() for i in ids[:limit]]

    # ------------------------------------------------------------ 执行任务
    async def start(self, job: Job) -> None:
        """排队 → 拿到并发名额后启动子进程，直到结束。

        批量翻译的并发控制就在这里：翻译子进程每个约 1GB 内存，
        因此按可用内存设定上限，超出的任务保持 queued 状态排队等待。
        """
        job.status = "queued"
        job.stage = f"排队中（并发上限 {self.max_concurrent}）"
        sem = self._semaphore()
        async with sem:
            job.status = "running"
            job.stage = "启动引擎"
            job.queue_position = 0
            self.refresh_queue_positions()
            await self._run_process(job)
        self.refresh_queue_positions()

    async def _run_process(self, job: Job) -> None:
        assert job.dir is not None
        params = job.params
        job_args = [
            "--input", str(job.dir / "input" / job.filename),
            "--output", str(job.dir / "output"),
            "--workdir", str(job.dir / "work"),
            "--lang-in", params.get("lang_in", "en"),
            "--lang-out", params.get("lang_out", "zh-CN"),
            "--engine", params.get("engine", "deepseek"),
            "--model", params.get("model", "deepseek-chat"),
            "--api-key", params.get("api_key", ""),
            "--base-url", params.get("base_url", "https://api.deepseek.com/v1"),
            "--qps", str(params.get("qps", 4)),
        ]
        if params.get("pages"):
            job_args += ["--pages", str(params["pages"])]
        if params.get("alternating"):
            job_args.append("--alternating")
        if params.get("skip_scanned_detection"):
            job_args.append("--skip-scanned-detection")
        if params.get("translate_tables"):
            job_args.append("--translate-tables")
        if params.get("split_short_lines"):
            job_args.append("--split-short-lines")
            job_args += ["--short-line-split-factor", str(params.get("short_line_split_factor", 0.8))]
        if params.get("glossaries"):
            # 把选中的术语集 id 解析成上游要的「逗号分隔 CSV 路径」
            try:
                from .glossary import store as glossary_store

                paths = glossary_store.upstream_paths(list(params["glossaries"]))
                if paths:
                    job_args += ["--glossaries", paths]
                else:
                    job.push({"type": "log", "message": "选中的术语集没有可用词条，已跳过"})
            except Exception as exc:  # noqa: BLE001
                job.push({"type": "log", "message": f"术语表加载失败，已忽略：{exc}"})
        if params.get("keep_auto_glossary"):
            job_args.append("--keep-auto-glossary")
        if params.get("convert_mode"):
            job_args += ["--convert-mode", str(params["convert_mode"])]
        cmd = job_command(job_args)

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        job.push({"type": "started", "message": "翻译引擎已启动"})
        started = time.time()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_DIR),
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = f"无法启动翻译进程：{exc}"
            job.push({"type": "error", "message": job.error})
            return

        job.process = proc
        stderr_tail: deque[str] = deque(maxlen=60)

        async def pump_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if not text:
                    continue
                stderr_tail.append(text)
                job.push({"type": "log", "message": text[-400:]})

        stderr_task = asyncio.create_task(pump_stderr())

        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").strip()
            if not text or not text.startswith("{"):
                if text:
                    job.push({"type": "log", "message": text[-400:]})
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                job.push({"type": "log", "message": text[-400:]})
                continue
            self._apply(job, event, started)

        code = await proc.wait()
        await stderr_task
        job.elapsed = round(time.time() - started, 1)

        if job.status == "running":
            if code == 0 and job.result:
                job.status = "done"
                job.progress = 100.0
                job.stage = "完成"
                job.push({"type": "done", "message": "翻译完成", "elapsed": job.elapsed})
            elif code == 130:
                job.status = "cancelled"
                job.stage = "已取消"
            else:
                job.status = "error"
                job.error = job.error or (stderr_tail[-1] if stderr_tail else f"进程异常退出（code={code}）")
                job.push({"type": "error", "message": job.error})
        job.process = None

    def _apply(self, job: Job, event: dict, started: float) -> None:
        etype = event.get("type")
        if etype == "progress":
            job.progress = max(job.progress, float(event.get("overall", 0.0)))
            job.stage = event.get("stage_label") or event.get("stage") or job.stage
            event["elapsed"] = round(time.time() - started, 1)
            job.push(event)
        elif etype == "stage_done":
            job.progress = max(job.progress, float(event.get("overall", 0.0)))
            job.stage = event.get("stage_label") or job.stage
            event["elapsed"] = round(time.time() - started, 1)
            job.push(event)
        elif etype == "finish":
            peak = event.get("peak_memory_mb")
            job.result = {
                "dual_pdf": event.get("dual_pdf"),
                "mono_pdf": event.get("mono_pdf"),
                "glossary_csv": event.get("glossary_csv"),
                "char_count": event.get("char_count"),
                "token_count": event.get("token_count"),
                "peak_memory_mb": peak if peak else None,
                "elapsed": event.get("elapsed"),
            }
            job.elapsed = event.get("elapsed") or job.elapsed
            job.push({"type": "finish", "message": "已生成对照文档", **job.result})
        elif etype == "error":
            job.error = str(event.get("message", "未知错误"))[:2000]
            job.push({"type": "error", "message": job.error})
        elif etype == "cancelled":
            job.status = "cancelled"
        else:
            job.push(event)

    # ------------------------------------------------------------ 取消
    async def cancel(self, job: Job) -> bool:
        proc = job.process
        if proc is None or proc.returncode is not None:
            if job.status in ("queued", "running"):
                job.status = "cancelled"
                job.stage = "已取消"
                return True
            return False
        try:
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=6)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        job.status = "cancelled"
        job.stage = "已取消"
        job.push({"type": "cancelled", "message": "任务已取消"})
        return True

    # ------------------------------------------------------------ 结果文件
    def result_file(self, job_id: str, kind: str = "dual") -> Path | None:
        job = self.jobs.get(job_id)
        if not job or not job.dir:
            return None
        output = job.dir / "output"
        if kind == "dual" and job.result and job.result.get("dual_pdf"):
            p = Path(job.result["dual_pdf"])
            if p.exists():
                return p
        if kind == "mono" and job.result and job.result.get("mono_pdf"):
            p = Path(job.result["mono_pdf"])
            if p.exists():
                return p
        if kind == "glossary" and job.result and job.result.get("glossary_csv"):
            p = Path(job.result["glossary_csv"])
            if p.exists():
                return p
        if not output.exists():
            return None
        patterns = {
            "dual": "*dual*.pdf",
            "mono": "*mono*.pdf",
            "glossary": "*.csv",
            "source": "*",
        }
        for pattern in [patterns.get(kind, "*.pdf")] + ["*.pdf"]:
            hits = sorted(output.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
            hits = [h for h in hits if h.is_file()]
            if hits:
                return hits[0]
        return None


def sanitize_name(name: str) -> str:
    """清理文件名，**保留原扩展名**。

    踩过的坑：早期版本无条件补 .pdf，导致上传 `课程讲义.pptx` 变成 `课程讲义.pptx.pdf`，
    后续按扩展名判断文件类型就全错了。
    """
    raw = Path(str(name)).name
    suffix = Path(raw).suffix.lower()
    keep = []
    for ch in raw:
        if ch.isalnum() or ch in "._- ()[]（）【】":
            keep.append(ch)
        else:
            keep.append("_")
    cleaned = "".join(keep).strip() or "document"
    # 确保扩展名存在且合法（扩展名本身也走过清理，可能被替换成下划线）
    if suffix and not cleaned.lower().endswith(suffix):
        cleaned = cleaned[: -len(suffix)] + suffix if len(cleaned) > len(suffix) else cleaned + suffix
    if not Path(cleaned).suffix:
        cleaned += ".pdf"
    return cleaned[:120]


manager = JobManager(max_concurrent=int(os.environ.get("MAX_CONCURRENT", "0") or 0))
