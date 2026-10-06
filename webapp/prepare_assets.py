"""下载并校验版面模型与多语言字体资源（约 340MB，仅首次）。

走 pdf2zh-next 官方入口 `--warmup`，而不是直接调用 babeldoc 的内部 API
（BabelDOC README 明确声明其 API 属于内部接口，推荐经由 pdf2zh-next 调用）。

注意：BabelDOC 的缓存目录在包内 const.py 中**硬编码**为 `~/.cache/babeldoc`，
没有提供环境变量开关，因此这里只负责触发下载与完整性校验。
下载源会在 huggingface / hf-mirror / modelscope 之间自动测速选择。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "babeldoc"
PROJECT_DIR = Path(__file__).resolve().parent.parent
READY_THRESHOLD_MB = 200


def cache_stats() -> tuple[int, float]:
    if not CACHE_DIR.exists():
        return 0, 0.0
    files = [f for f in CACHE_DIR.rglob("*") if f.is_file()]
    return len(files), round(sum(f.stat().st_size for f in files) / 1024 / 1024, 1)


def warmup_command() -> list[str]:
    """官方 warmup 入口（源码模式与打包模式都能用）。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--warmup"]
    return [sys.executable, "-m", "pdf2zh_next.main", "--warmup"]


def _find_local_bundle() -> Path | None:
    """在程序目录附近找 offline_assets_*.zip（轻量版把它单独分发，用户下载后放旁边即可）。"""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT_DIR
    for d in (base, base / "assets", base.parent, PROJECT_DIR):
        try:
            if d.is_dir():
                hits = sorted(d.glob("offline_assets_*.zip"))
                if hits:
                    return hits[0]
        except OSError:
            continue
    return None


def main() -> int:
    print(f"[assets] 缓存目录: {CACHE_DIR}", flush=True)
    count, mb = cache_stats()
    if count and mb > READY_THRESHOLD_MB:
        print(f"[assets] 已就绪：{count} 个文件 / {mb} MB，跳过下载", flush=True)
        return 0

    started = time.time()

    # 优先用「旁边放着的离线资源包」——轻量版发行时的形态：
    # 程序目录下有 offline_assets_*.zip 就直接恢复，不用联网下载。
    bundle = _find_local_bundle()
    if bundle is not None:
        print(f"[assets] 发现随包资源：{bundle.name}", flush=True)
        print("[assets] 正在恢复版面模型与字体（约 340MB，首次需要 1-3 分钟）…", flush=True)
        try:
            from babeldoc.assets import assets

            assets.restore_offline_assets_package(bundle)
        except Exception as exc:  # noqa: BLE001
            print(f"[assets] 离线恢复失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            print("[assets] 改为联网下载。", file=sys.stderr, flush=True)
        else:
            count, mb = cache_stats()
            if mb > READY_THRESHOLD_MB:
                print(f"[assets] 完成（离线恢复）：{count} 个文件 / {mb} MB，用时 {time.time() - started:.0f}s", flush=True)
                return 0

    print("[assets] 开始下载模型与字体（约 340MB，首次较慢，可中断后重跑续传）…", flush=True)

    if getattr(sys, "frozen", False):
        # 打包模式：当前进程本身就是分发入口，直接 import 走内部实现，
        # 避免再次启动 exe 造成递归。
        from babeldoc.assets import assets

        try:
            assets.warmup()
        except Exception as exc:  # noqa: BLE001
            print(f"[assets] 下载失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 1
    else:
        proc = subprocess.run(warmup_command(), check=False)
        if proc.returncode != 0:
            print("[assets] 下载未完成，请重跑本脚本（已下载部分会自动校验复用）。", file=sys.stderr, flush=True)
            return proc.returncode

    count, mb = cache_stats()
    if mb <= READY_THRESHOLD_MB:
        print(f"[assets] 资源似乎不完整（{count} 个文件 / {mb} MB），请重跑。", file=sys.stderr, flush=True)
        return 1
    print(f"[assets] 完成：{count} 个文件 / {mb} MB，用时 {time.time() - started:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
