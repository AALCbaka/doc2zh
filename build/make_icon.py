"""生成一个 .ico 作为桌面快捷方式图标（从打包 exe 里抽出来，避免依赖外部素材）。

用法：python build/make_icon.py
产物：dist/PDF中英对照翻译/app.ico
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "PDF中英对照翻译" / "PDF中英对照翻译.exe"
OUT = ROOT / "dist" / "PDF中英对照翻译" / "app.ico"


def extract_group_icon(exe: Path) -> bytes | None:
    """从 PE 资源里找 RT_GROUP_ICON，拼出一个独立 .ico。

    简化做法：优先直接读 PE 中的图标资源；失败则返回 None（调用方回退到 exe 图标）。
    """
    try:
        import win32api  # noqa: F401
        import win32con
        import win32gui
    except Exception:  # noqa: BLE001
        return None
    return None  # Windows API 抽取较繁琐，本脚本用更稳的方式（见 main）


def build_from_png() -> bytes | None:
    """用 PyMuPDF 没有画图标的能力，这里改为程序化生成一个简洁的 PDF 图标。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return None

    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方底 + 渐变感（用两段纯色近似）
    radius = 52
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=(37, 99, 235, 255))
    d.rounded_rectangle([0, size // 2, size - 1, size - 1], radius=radius, fill=(124, 58, 237, 255))
    d.rounded_rectangle([6, 6, size - 7, size - 7], radius=radius - 6, fill=(37, 99, 235, 255))

    # 白色文档形状
    left, top, right, bottom = 66, 48, size - 66, size - 48
    fold = 42
    d.polygon(
        [(left, top), (right - fold, top), (right, top + fold), (right, bottom), (left, bottom)],
        fill=(255, 255, 255, 255),
    )
    d.polygon([(right - fold, top), (right, top + fold), (right - fold, top + fold)], fill=(219, 234, 254, 255))

    # 文字行
    for i, w in enumerate((0.62, 0.78, 0.70, 0.5)):
        y = top + 66 + i * 26
        d.rounded_rectangle([left + 26, y, left + 26 + int((right - left - 52) * w), y + 11],
                            radius=5, fill=(148, 163, 184, 255))
    # 双语对照的两条竖条（呼应"对照"）
    d.rounded_rectangle([left + 14, top + 60, left + 20, bottom - 22], radius=3, fill=(37, 99, 235, 255))
    d.rounded_rectangle([right - 20, top + 60, right - 14, bottom - 22], radius=3, fill=(16, 185, 129, 255))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    return OUT.read_bytes()


def main() -> int:
    if not EXE.exists():
        print(f"未找到 exe：{EXE}")
        return 1
    data = build_from_png()
    if data:
        print(f"已生成图标：{OUT}（{len(data) / 1024:.1f} KB）")
        return 0
    print("Pillow 不可用，快捷方式将直接使用 exe 自带图标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
