"""PDF 英译中 · 左右对照翻译服务

- 上传 PDF → 调用 DeepSeek → BabelDOC 保留排版生成双语对照 PDF
- SSE 实时推送进度；支持取消、历史任务、原文件下载
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .jobs import JOBS_DIR, PROJECT_DIR, manager

WEBAPP_DIR = PROJECT_DIR / "webapp"
STATIC_DIR = WEBAPP_DIR / "static"
UPLOAD_TMP = JOBS_DIR / "_incoming"
UPLOAD_TMP.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
MAX_BATCH_FILES = int(os.environ.get("MAX_BATCH_FILES", "20"))
# 并发上限：0 = 按可用内存自动（见 JobManager._auto_concurrency）
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "0"))
DEFAULT_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
SOURCE_URL = os.environ.get("SOURCE_URL", "https://github.com/AALCbaka/doc2zh")
LICENSE_NAME = "AGPL-3.0"

app = FastAPI(title="PDF 中英对照翻译", version="1.0.0")

# 资源准备进度（供前端显示）
_ASSET_STATE: dict = {"running": False, "done": False, "message": "", "log": [], "started": 0.0}


def _assets_mb() -> float:
    d = Path.home() / ".cache" / "babeldoc"
    if not d.exists():
        return 0.0
    return round(sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1024 / 1024, 1)


async def _run_asset_warmup() -> None:
    from .jobs import job_command

    _ASSET_STATE.update(running=True, done=False, message="正在下载模型与字体…", log=[], started=time.time())
    try:
        proc = await asyncio.create_subprocess_exec(
            *job_command(["--warmup"]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(PROJECT_DIR),
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                _ASSET_STATE["log"] = (_ASSET_STATE["log"] + [text[-300:]])[-40:]
        code = await proc.wait()
        ok = code == 0 and _assets_mb() > 200
        _ASSET_STATE.update(
            running=False,
            done=ok,
            message="资源已就绪" if ok else f"下载未完成（退出码 {code}），可重试；已下载部分会自动复用",
        )
    except Exception as exc:  # noqa: BLE001
        _ASSET_STATE.update(running=False, done=False, message=f"下载失败：{exc}")


# ------------------------------------------------------------------ 基础信息
@app.get("/api/health")
async def health() -> dict:
    from .jobs import python_executable

    venv_ok = Path(python_executable()).exists()
    assets_mb = _assets_mb()
    return {
        "ok": True,
        "engine": "BabelDOC + DeepSeek",
        "python": python_executable(),
        "venv_ok": venv_ok,
        "assets_mb": assets_mb,
        "assets_ready": assets_mb > 200,
        "assets_state": _ASSET_STATE,
        "server_has_key": bool(DEFAULT_API_KEY),
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_batch_files": MAX_BATCH_FILES,
        "max_concurrent": manager.max_concurrent,
        "license": LICENSE_NAME,
        "source_url": SOURCE_URL,
    }


@app.post("/api/assets/download")
async def assets_download() -> dict:
    if _ASSET_STATE.get("running"):
        return {"started": False, "message": "已在进行中"}
    asyncio.create_task(_run_asset_warmup())
    return {"started": True, "message": "已开始下载模型资源"}


@app.get("/api/assets/state")
async def assets_state() -> dict:
    return {**_ASSET_STATE, "assets_mb": _assets_mb()}


# ------------------------------------------------------------------ 术语表
@app.get("/api/glossaries")
async def glossary_list() -> dict:
    """术语集列表。附带数据目录，方便用户确认自己的术语表存在哪。"""
    from .glossary import GLOSSARY_DIR
    from .glossary import store as glossary_store

    return {"sets": glossary_store.list_sets(), "dir": str(GLOSSARY_DIR)}


@app.post("/api/glossaries/import")
async def glossary_import(
    file: UploadFile | None = File(None),
    name: str = Form(""),
    text: str = Form(""),
) -> JSONResponse:
    """导入术语表：上传文件（CSV/TSV/TXT，自动识别编码与分隔符）或直接粘贴文本。"""
    from .glossary import parse_terms
    from .glossary import store as glossary_store

    if file is not None and file.filename:
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="文件是空的")
        try:
            meta = glossary_store.import_file(file.filename, raw, name=name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"set": meta})

    if text.strip():
        entries, warnings = parse_terms(text)
        if not entries:
            raise HTTPException(status_code=400, detail="没解析出术语：" + ("；".join(warnings) or "格式无法识别"))
        meta = glossary_store.create(name or "粘贴导入", entries, description="粘贴导入", source="manual")
        meta["warnings"] = warnings
        return JSONResponse({"set": meta})

    raise HTTPException(status_code=400, detail="请上传文件或粘贴内容")


@app.get("/api/glossaries/{set_id}")
async def glossary_detail(set_id: str, limit: int = 2000) -> dict:
    from .glossary import store as glossary_store

    meta = glossary_store.get(set_id)
    if not meta:
        raise HTTPException(status_code=404, detail="术语集不存在")
    return {"set": meta, "terms": glossary_store.read_terms(set_id, limit=limit)}


@app.put("/api/glossaries/{set_id}")
async def glossary_update(set_id: str, payload: dict) -> dict:
    """改名称/说明，或整体替换词条（terms = [{source,target}]）。"""
    from .glossary import store as glossary_store

    entries = payload.get("terms")
    if entries is not None and not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="terms 必须是数组")
    meta = glossary_store.update(
        set_id,
        name=payload.get("name"),
        description=payload.get("description"),
        entries=entries,
    )
    if not meta:
        raise HTTPException(status_code=404, detail="术语集不存在")
    return {"set": meta}


@app.delete("/api/glossaries/{set_id}")
async def glossary_delete(set_id: str) -> dict:
    from .glossary import store as glossary_store

    return {"deleted": glossary_store.delete(set_id)}


@app.post("/api/glossaries/merge")
async def glossary_merge(payload: dict) -> dict:
    from .glossary import store as glossary_store

    ids = payload.get("ids") or []
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="至少选择两个术语集")
    name = (payload.get("name") or "合并术语集").strip()
    return {"set": glossary_store.merge(list(ids), name=name)}


@app.get("/api/glossaries/{set_id}/export")
async def glossary_export(set_id: str):
    from .glossary import store as glossary_store

    meta = glossary_store.get(set_id)
    path = glossary_store.csv_path(set_id)
    if not meta or not path:
        raise HTTPException(status_code=404, detail="术语集不存在")
    quoted = urllib.parse.quote(f"{meta['name']}.csv")
    return FileResponse(
        path, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )



# ------------------------------------------------------------------ 上传+翻译
ACCEPTED_EXTS = {".pdf", ".ppt", ".pptx", ".pps", ".ppsx", ".pot", ".potx", ".pptm",
                 ".doc", ".docx", ".docm", ".rtf"}


async def _store_upload(file: UploadFile, limit: int) -> tuple[Path, str, int]:
    """把上传文件落到临时区，返回 (路径, 文件名, 字节数)。

    接受 PDF 与 Office 文档（PPT/Word）；Office 会在翻译前自动转成 PDF。
    """
    name = file.filename or "document.pdf"
    suffix = Path(name).suffix.lower()
    if suffix not in ACCEPTED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {suffix or '(无扩展名)'}：只接受 PDF 与 PPT/Word 文档",
        )
    incoming = UPLOAD_TMP / f"{os.urandom(8).hex()}{suffix}"
    size = 0
    with incoming.open("wb") as fh:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                fh.close()
                incoming.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"{name} 超过 {MAX_UPLOAD_MB}MB 上限")
            fh.write(chunk)
    if size == 0:
        incoming.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"{name} 是空文件")
    return incoming, name, size


def _build_params(
    *, api_key: str, model: str, lang_in: str, lang_out: str, pages: str, qps: int,
    alternating: bool, skip_scanned_detection: bool, translate_tables: bool,
    split_short_lines: bool, short_line_split_factor: float, engine: str, base_url: str,
    size_mb: float, glossary_ids: str = "", keep_auto_glossary: bool = False,
    convert_mode: str = "auto",
) -> dict:
    key = (api_key or DEFAULT_API_KEY).strip()
    if not key:
        raise HTTPException(status_code=400, detail="请填写 DeepSeek API Key（或在服务端配置 DEEPSEEK_API_KEY 环境变量）")
    ids = [i.strip() for i in (glossary_ids or "").split(",") if i.strip()]
    return {
        "api_key": key,
        "model": model or "deepseek-chat",
        "lang_in": lang_in or "en",
        "lang_out": lang_out or "zh-CN",
        "pages": (pages or "").strip(),
        "qps": max(1, min(int(qps or 4), 16)),
        "alternating": bool(alternating),
        "skip_scanned_detection": bool(skip_scanned_detection),
        "translate_tables": bool(translate_tables),
        "split_short_lines": bool(split_short_lines),
        "short_line_split_factor": max(0.1, min(float(short_line_split_factor or 0.8), 1.0)),
        "glossaries": ids,
        "keep_auto_glossary": bool(keep_auto_glossary),
        "convert_mode": convert_mode if convert_mode in ("auto", "native", "printer", "libreoffice") else "auto",
        "engine": engine if engine in ("deepseek", "openai-compatible") else "deepseek",
        "base_url": base_url or "https://api.deepseek.com/v1",
        "size_mb": round(size_mb, 2),
    }


@app.post("/api/translate/batch")
async def create_translation_batch(
    files: list[UploadFile] = File(...),
    api_key: str = Form(""),
    model: str = Form("deepseek-chat"),
    lang_in: str = Form("en"),
    lang_out: str = Form("zh-CN"),
    pages: str = Form(""),
    qps: int = Form(4),
    alternating: bool = Form(False),
    skip_scanned_detection: bool = Form(False),
    translate_tables: bool = Form(False),
    split_short_lines: bool = Form(True),
    short_line_split_factor: float = Form(0.8),
    glossary_ids: str = Form(""),
    convert_mode: str = Form("auto"),
    engine: str = Form("deepseek"),
    base_url: str = Form("https://api.deepseek.com/v1"),
) -> JSONResponse:
    """批量上传：一次收多个 PDF，按并发上限排队翻译。"""
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件")
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(status_code=400, detail=f"单批最多 {MAX_BATCH_FILES} 个文件，请分批上传")

    limit = MAX_UPLOAD_MB * 1024 * 1024
    created: list[dict] = []
    failed: list[dict] = []
    for f in files:
        try:
            incoming, name, size = await _store_upload(f, limit)
            params = _build_params(
                api_key=api_key, model=model, lang_in=lang_in, lang_out=lang_out, pages=pages,
                qps=qps, alternating=alternating, skip_scanned_detection=skip_scanned_detection,
                translate_tables=translate_tables, split_short_lines=split_short_lines,
                short_line_split_factor=short_line_split_factor, engine=engine, base_url=base_url,
                size_mb=size / 1024 / 1024, glossary_ids=glossary_ids, convert_mode=convert_mode,
            )
            job = manager.create(upload_path=incoming, original_name=name, params=params)
            asyncio.create_task(manager.start(job))
            created.append({"job_id": job.id, "filename": job.filename, "size_mb": params["size_mb"]})
        except HTTPException as exc:
            failed.append({"filename": f.filename, "reason": exc.detail})
        except Exception as exc:  # noqa: BLE001
            failed.append({"filename": f.filename, "reason": str(exc)})

    manager.refresh_queue_positions()
    return JSONResponse({
        "jobs": created,
        "failed": failed,
        "count": len(created),
        "max_concurrent": manager.max_concurrent,
    })


@app.post("/api/translate")
async def create_translation(
    file: UploadFile = File(...),
    api_key: str = Form(""),
    model: str = Form("deepseek-chat"),
    lang_in: str = Form("en"),
    lang_out: str = Form("zh-CN"),
    pages: str = Form(""),
    qps: int = Form(4),
    alternating: bool = Form(False),
    skip_scanned_detection: bool = Form(False),
    translate_tables: bool = Form(False),
    split_short_lines: bool = Form(True),
    short_line_split_factor: float = Form(0.8),
    glossary_ids: str = Form(""),
    convert_mode: str = Form("auto"),
    engine: str = Form("deepseek"),
    base_url: str = Form("https://api.deepseek.com/v1"),
) -> JSONResponse:
    limit = MAX_UPLOAD_MB * 1024 * 1024
    incoming, name, size = await _store_upload(file, limit)
    params = _build_params(
        api_key=api_key, model=model, lang_in=lang_in, lang_out=lang_out, pages=pages,
        qps=qps, alternating=alternating, skip_scanned_detection=skip_scanned_detection,
        translate_tables=translate_tables, split_short_lines=split_short_lines,
        short_line_split_factor=short_line_split_factor, engine=engine, base_url=base_url,
        size_mb=size / 1024 / 1024, glossary_ids=glossary_ids, convert_mode=convert_mode,
    )
    job = manager.create(upload_path=incoming, original_name=name, params=params)
    asyncio.create_task(manager.start(job))
    return JSONResponse({"job_id": job.id, "filename": job.filename, "size_mb": params["size_mb"]})


# ------------------------------------------------------------------ 进度流
@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def stream():
        sent = 0
        idle = 0.0
        while True:
            if await request.is_disconnected():
                break
            events = job.events
            while sent < len(events):
                payload = {"job": job.snapshot(), "event": events[sent]}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                sent += 1
                idle = 0.0
            if job.status in ("done", "error", "cancelled") and sent >= len(job.events):
                yield f"data: {json.dumps({'job': job.snapshot(), 'event': {'type': 'closed'}}, ensure_ascii=False)}\n\n"
                break
            await asyncio.sleep(0.35)
            idle += 0.35
            if idle > 15:
                yield ": keep-alive\n\n"
                idle = 0.0

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.snapshot()


@app.get("/api/jobs")
async def job_list(limit: int = 20) -> dict:
    return {"jobs": manager.recent(limit)}


@app.post("/api/jobs/{job_id}/cancel")
async def job_cancel(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    ok = await manager.cancel(job)
    return {"cancelled": ok, "status": job.status}


@app.post("/api/jobs/{job_id}/restart")
async def job_restart(job_id: str, payload: dict | None = None) -> dict:
    """用同一份输入文件重新翻译。

    典型场景：程序在任务进行中被关闭/重启，磁盘上的任务停在"运行中"但进程已消失，
    界面会一直显示"查看进度"却永不推进。这个接口用于一键重跑。
    需要重新提供 API Key（Key 不落盘）。
    """
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.dir is None or not (job.dir / "input").is_dir():
        raise HTTPException(status_code=400, detail="找不到该任务的输入文件")

    inputs = [f for f in (job.dir / "input").glob("*") if f.is_file()]
    if not inputs:
        raise HTTPException(status_code=400, detail="该任务的输入文件已被清理，请重新上传")

    key = ""
    if payload and isinstance(payload, dict):
        key = str(payload.get("api_key") or "").strip()
    key = key or DEFAULT_API_KEY
    if not key:
        raise HTTPException(status_code=400, detail="请先在左侧填写 DeepSeek API Key（Key 不落盘，重跑需重新提供）")

    params = dict(job.params or {})
    params.pop("restored", None)
    params["api_key"] = key
    params.setdefault("model", "deepseek-chat")
    params.setdefault("lang_in", "en")
    params.setdefault("lang_out", "zh-CN")
    params.setdefault("engine", "deepseek")
    params.setdefault("qps", 4)
    params.setdefault("size_mb", round(sum(f.stat().st_size for f in inputs) / 1024 / 1024, 2))

    # 清掉上次的半成品，避免新旧产物混淆
    out_dir = job.dir / "output"
    if out_dir.is_dir():
        for old in list(out_dir.glob("*.pdf")) + list(out_dir.glob("*.csv")):
            try:
                old.unlink()
            except OSError:
                pass

    job.params = params
    job.result = None
    job.error = None
    job.progress = 0.0
    job.status = "queued"
    job.stage = "已重新排队"
    job.push({"type": "restarted", "message": "已重新开始翻译"})
    asyncio.create_task(manager.start(job))
    return {"ok": True, "job_id": job.id, "status": job.status}


# ------------------------------------------------------------------ 强制下载
@app.get("/api/jobs/{job_id}/save/{kind}")
async def job_save(job_id: str, kind: str):
    """强制下载：带 Content-Disposition: attachment。

    与 /file/{kind} 的区别：那个用于页内预览（inline），这个专门给「下载」用。
    WebView2 自带下载管理，会把 attachment 响应存到默认下载目录，
    因此即使原生"另存为"桥接不可用，用户也能拿到文件。
    """
    if kind not in ("dual", "mono", "glossary", "source"):
        raise HTTPException(status_code=400, detail="未知的文件类型")
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    path = (job.dir / "input" / job.filename) if kind == "source" else manager.result_file(job_id, kind)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="文件尚未生成")

    path = Path(path)
    base = job.filename
    for ext in (".pdf", ".docx", ".pptx", ".ppt", ".doc", ".rtf"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    suffix = {"dual": "-中英对照", "mono": "-仅译文", "glossary": "-术语表"}.get(kind, "")
    quoted = urllib.parse.quote(f"{base}{suffix}{path.suffix}")
    media = "application/pdf" if path.suffix.lower() == ".pdf" else "text/csv"
    return FileResponse(
        path, media_type=media,
        headers={
            "Content-Disposition": f"attachment; filename=\"{quoted}\"; filename*=UTF-8''{quoted}",
            "Cache-Control": "no-store",
        },
    )


# ------------------------------------------------------------------ 下载/预览
def _download_headers(path: Path, suffix: str = "") -> dict:
    stem = path.stem
    if suffix:
        stem = f"{stem}{suffix}"
    filename = f"{stem}{path.suffix}"
    quoted = urllib.parse.quote(filename)
    return {"Content-Disposition": f"inline; filename=\"{quoted}\"; filename*=UTF-8''{quoted}"}


@app.get("/api/jobs/{job_id}/file/{kind}")
async def job_file(job_id: str, kind: str, download: int = 0):
    if kind not in ("dual", "mono", "glossary", "source"):
        raise HTTPException(status_code=400, detail="未知的文件类型")
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    if kind == "source":
        path = job.dir / "input" / job.filename if job.dir else None
    else:
        path = manager.result_file(job_id, kind)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="文件尚未生成")

    path = Path(path)
    media = "application/pdf" if path.suffix.lower() == ".pdf" else "text/csv"
    headers = {}
    if download:
        base = job.filename[:-4] if job.filename.lower().endswith(".pdf") else job.filename
        suffix = "-对照" if kind == "dual" else ("-译文" if kind == "mono" else "")
        quoted = urllib.parse.quote(f"{base}{suffix}{path.suffix}")
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quoted}"
    else:
        headers = _download_headers(path)
    return FileResponse(path, media_type=media, headers=headers)


@app.get("/api/jobs/{job_id}/pages/{kind}")
async def job_pages(job_id: str, kind: str) -> dict:
    """返回该文件的页数，供界面画预览缩略图。"""
    if kind not in ("dual", "mono", "source"):
        raise HTTPException(status_code=400, detail="未知的文件类型")
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    path = (job.dir / "input" / job.filename) if kind == "source" else manager.result_file(job_id, kind)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="文件尚未生成")
    from .pdf_preview import page_count

    try:
        total = page_count(str(path))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"无法读取 PDF：{exc}") from exc
    return {"job_id": job_id, "kind": kind, "pages": total}


@app.get("/api/jobs/{job_id}/page/{kind}/{page_no}")
async def job_page_image(job_id: str, kind: str, page_no: int, dpi: int = 110, source_total: int = 0):
    """把结果 PDF 的某页渲染成 PNG（窗口内预览不依赖浏览器 PDF 插件）。

    kind=dual 时支持按「原始 PDF 页码」取图：整页交替模式下输出页是 原文/译文 交替排列，
    用 source_total 传原始总页数，即可把源页号换算成输出页号。
    """
    if kind not in ("dual", "mono", "source"):
        raise HTTPException(status_code=400, detail="未知的文件类型")
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    path = (job.dir / "input" / job.filename) if kind == "source" else manager.result_file(job_id, kind)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="文件尚未生成")

    from .pdf_preview import map_source_page_to_output, render_page

    index = page_no - 1
    if kind == "dual" and source_total:
        index = map_source_page_to_output(str(path), page_no, source_total)
    try:
        png = render_page(str(path), index, dpi)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"渲染失败：{exc}") from exc
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=600"})


# ------------------------------------------------------------------ 前端
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>前端未构建</h1>", status_code=500)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
