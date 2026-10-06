"""术语表管理：让「跨文档译名统一」变成可管理的资产。

上游能力与约束（读源码确认）
----------------------------
* 上游 `TranslationSettings.glossaries` 是**逗号分隔的文件路径字符串**，不是结构体；
* CSV 必须含 `source,target` 表头（用 csv.DictReader 按列名取），可选 `tgt_lng`；
* 匹配规则：大小写不敏感、空白归一、**长词优先**，用 hyperscan 扫描；
* 术语表名 = CSV 文件名（stem）。

所以本模块做三件事：
1. 把用户给的任意格式（CSV/TSV/制表符文本/带 BOM/GBK）**归一化**成上游要的格式；
2. 以「术语集」为单位管理（创建/重命名/删除/查看/编辑/导出），落盘到 glossaries/；
3. 翻译时把选中的术语集合并成上游需要的路径串。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _app_dir() -> Path:
    """程序所在目录：打包后 = exe 所在目录；源码 = 仓库根。

    注意不能用 PROJECT_DIR 代替：打包后 __file__ 在只读的 _internal 里，
    把数据写到那儿会导致「导入的术语集重启后就找不到了」。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_DIR


def _resolve_glossary_dir() -> Path:
    """确定术语集存放目录（必须可写）。

    优先级：
      1. 环境变量 GLOSSARY_DIR
      2. 打包运行 → **exe 同级的 glossaries/**（便携、用户看得见、可写）
         若同级不可写（例如装在 Program Files），退回用户目录
      3. 源码运行 → 仓库下的 glossaries/
    """
    env = os.environ.get("GLOSSARY_DIR", "").strip()
    if env:
        return Path(env).expanduser()

    base = _app_dir()
    target = base / "glossaries"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return target
    except OSError:
        fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
        fallback = fallback / "PDFBilingualTranslator" / "glossaries"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


GLOSSARY_DIR = _resolve_glossary_dir()
INDEX_FILE = GLOSSARY_DIR / "index.json"

REQUIRED_COLUMNS = ("source", "target")
OPTIONAL_COLUMNS = ("tgt_lng",)
MAX_TERMS = 20000


# ----------------------------------------------------------------- 解析/归一化
def sniff_text(raw: bytes) -> str:
    """猜测编码并解码（带 BOM 的 UTF-8、GBK、Latin-1 都能吃）。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc == "latin-1" or "\ufffd" not in text:
                return text
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_terms(text: str) -> tuple[list[dict], list[str]]:
    """把用户内容解析成 [{source,target,tgt_lng?}]。返回 (条目, 警告)。

    支持：
      * 标准 CSV（表头 source,target[,tgt_lng]，列序不限，多余列忽略）
      * TSV（表头同上）
      * 无表头的两列/三列文本（逗号或制表符分隔）
      * 每行「原词 => 译词」或「原词 = 译词」或「原词<TAB>译词」
    """
    warnings: list[str] = []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return [], ["内容为空"]

    header = [c.strip().lower().lstrip("\ufeff") for c in _split_row(lines[0])]
    has_header = all(col in header for col in REQUIRED_COLUMNS)
    rows: list[list[str]] = []
    col_map: dict[str, int] = {}

    if has_header:
        col_map = {name: idx for idx, name in enumerate(header)}
        rows = [_split_row(ln) for ln in lines[1:]]
    else:
        # 无表头：识别分隔符，按两列/三列解析
        first = lines[0]
        if "=>" in first:
            pairs = [_split_arrow(ln) for ln in lines]
        else:
            pairs = [_split_row(ln) for ln in lines]
        col_map = {"source": 0, "target": 1}
        rows = pairs
        warnings.append("未检测到 source,target 表头，已按「第1列=原词，第2列=译词」解析")

    entries: list[dict] = []
    skipped = 0
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        try:
            src = row[col_map["source"]].strip()
            tgt = row[col_map["target"]].strip()
        except (IndexError, KeyError):
            skipped += 1
            continue
        if not src or not tgt:
            skipped += 1
            continue
        item = {"source": src, "target": tgt}
        if "tgt_lng" in col_map:
            try:
                lang = row[col_map["tgt_lng"]].strip()
                if lang:
                    item["tgt_lng"] = lang
            except IndexError:
                pass
        entries.append(item)
        if len(entries) >= MAX_TERMS:
            warnings.append(f"超过 {MAX_TERMS} 条，已截断")
            break
    if skipped:
        warnings.append(f"跳过 {skipped} 行空/残缺数据")
    return dedupe(entries), warnings


def _split_row(line: str) -> list[str]:
    """按逗号或制表符切列（引号包裹的逗号交给 csv 处理）。"""
    if "\t" in line and line.count("\t") >= line.count(","):
        return [c.strip().strip('"') for c in line.split("\t")]
    try:
        return next(csv.reader(io.StringIO(line)))
    except Exception:  # noqa: BLE001
        return [c.strip() for c in line.split(",")]


def _split_arrow(line: str) -> list[str]:
    for sep in ("=>", "＝", "=", "->", "→"):
        if sep in line:
            a, _, b = line.partition(sep)
            return [a.strip(), b.strip()]
    return [line.strip(), ""]


def dedupe(entries: list[dict]) -> list[dict]:
    """按归一化后的 source 去重（与上游 normalize_source 行为一致：小写+空白归一）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for e in entries:
        key = re.sub(r"\s+", " ", e["source"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def to_upstream_csv(entries: list[dict]) -> str:
    """产出上游 Glossary.from_csv 能吃的 CSV 文本。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["source", "target", "tgt_lng"])
    for e in entries:
        writer.writerow([e.get("source", ""), e.get("target", ""), e.get("tgt_lng", "") or ""])
    return buf.getvalue()


# ----------------------------------------------------------------- 存储
class GlossaryStore:
    def __init__(self) -> None:
        GLOSSARY_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_index()

    # ---- 索引 ----
    def _ensure_index(self) -> None:
        if not INDEX_FILE.exists():
            INDEX_FILE.write_text(json.dumps({"sets": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_index(self) -> dict:
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"sets": []}

    def _save_index(self, data: dict) -> None:
        INDEX_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 查询 ----
    def list_sets(self) -> list[dict]:
        data = self._load_index()
        out = []
        for meta in data.get("sets", []):
            csv_path = GLOSSARY_DIR / f"{meta['id']}.csv"
            meta = dict(meta)
            meta["exists"] = csv_path.exists()
            meta["terms"] = self._count(csv_path) if csv_path.exists() else 0
            out.append(meta)
        out.sort(key=lambda m: m.get("created_at", 0), reverse=True)
        return out

    @staticmethod
    def _count(csv_path: Path) -> int:
        try:
            with csv_path.open(encoding="utf-8", newline="") as fh:
                return max(0, sum(1 for _ in fh) - 1)
        except OSError:
            return 0

    def get(self, set_id: str) -> dict | None:
        for meta in self._load_index().get("sets", []):
            if meta["id"] == set_id:
                return meta
        return None

    def csv_path(self, set_id: str) -> Path | None:
        p = GLOSSARY_DIR / f"{set_id}.csv"
        return p if p.exists() else None

    def read_terms(self, set_id: str, limit: int = 2000) -> list[dict]:
        path = self.csv_path(set_id)
        if not path:
            return []
        entries, _ = parse_terms(sniff_text(path.read_bytes()))
        return entries[:limit]

    # ---- 写入 ----
    def create(self, name: str, entries: list[dict], *, description: str = "",
               source: str = "manual", filename: str = "", lang_out: str = "zh-CN") -> dict:
        set_id = uuid.uuid4().hex[:10]
        entries = dedupe(entries)
        csv_text = to_upstream_csv(entries)
        (GLOSSARY_DIR / f"{set_id}.csv").write_text(csv_text, encoding="utf-8")
        meta = {
            "id": set_id,
            "name": (name or filename or "未命名术语集").strip()[:60],
            "description": description.strip()[:200],
            "source": source,               # manual | upload | extracted
            "origin_filename": filename,
            "lang_out": lang_out,
            "created_at": time.time(),
            "updated_at": time.time(),
            "terms": len(entries),
        }
        data = self._load_index()
        data.setdefault("sets", []).append(meta)
        self._save_index(data)
        return meta

    def update(self, set_id: str, *, name: str | None = None, description: str | None = None,
               entries: list[dict] | None = None) -> dict | None:
        data = self._load_index()
        for meta in data.get("sets", []):
            if meta["id"] != set_id:
                continue
            if name is not None:
                meta["name"] = name.strip()[:60] or meta["name"]
            if description is not None:
                meta["description"] = description.strip()[:200]
            if entries is not None:
                entries = dedupe(entries)
                (GLOSSARY_DIR / f"{set_id}.csv").write_text(to_upstream_csv(entries), encoding="utf-8")
                meta["terms"] = len(entries)
            meta["updated_at"] = time.time()
            self._save_index(data)
            return meta
        return None

    def delete(self, set_id: str) -> bool:
        data = self._load_index()
        before = len(data.get("sets", []))
        data["sets"] = [m for m in data.get("sets", []) if m["id"] != set_id]
        if len(data["sets"]) == before:
            return False
        self._save_index(data)
        path = GLOSSARY_DIR / f"{set_id}.csv"
        if path.exists():
            path.unlink()
        return True

    def import_file(self, filename: str, raw: bytes, *, name: str = "") -> dict:
        """导入用户上传的文件（CSV/TSV/TXT），自动归一化。"""
        text = sniff_text(raw)
        entries, warnings = parse_terms(text)
        if not entries:
            raise ValueError("没能从文件里解析出术语：" + ("；".join(warnings) or "格式无法识别"))
        meta = self.create(
            name or Path(filename).stem,
            entries,
            description=f"导入自 {filename}",
            source="upload",
            filename=filename,
        )
        meta["warnings"] = warnings
        return meta

    def merge(self, set_ids: list[str], *, name: str) -> dict:
        """把多个术语集合并成一个新的（后面的不覆盖前面的同名条目）。"""
        entries: list[dict] = []
        for sid in set_ids:
            entries.extend(self.read_terms(sid, limit=MAX_TERMS))
        return self.create(name, entries, description=f"合并自 {len(set_ids)} 个术语集", source="manual")

    def upstream_paths(self, set_ids: list[str]) -> str:
        """翻译时要传给上游的逗号分隔路径串。"""
        paths = []
        for sid in set_ids:
            p = self.csv_path(sid)
            if p:
                paths.append(str(p))
        return ",".join(paths)


store = GlossaryStore()
