"""sitecustomize：启动时自动安装「清单段落拆分」补丁。

为什么需要它
------------
pdf2zh-next 把版面解析与翻译跑在 multiprocessing **spawn 子进程**里，子进程是全新解释器：
  * 审计钩子不会继承 → 试过，无效；
  * 替换子进程入口 target 并不可靠 → 子进程重新 import 会重置 __main__ 的模块级状态，试过，无效；
  * `register_after_fork` 对 spawn 同样不适用。
但有一个东西是必然继承的：**环境变量**。Python 解释器启动时会自动 import `sitecustomize`，
所以只要把本目录放进 PYTHONPATH 并设置 SWITCH，子进程一启动就会执行这里的补丁，
从而在版面解析之前完成 `ParagraphFinder.process_page` 的替换。

仅当环境变量 `PDF_TRANSLATOR_IR_SPLIT` 明确为真时才动作，因此对其它 Python 进程零影响。
"""
from __future__ import annotations

import os

if os.environ.get("PDF_TRANSLATOR_IR_SPLIT", "") not in ("", "0", "false", "False"):
    try:
        import glob
        import sys

        # 本项目根目录：本文件位于 <root>/webapp/sitecustomize.py
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # 冻结环境（PyInstaller）下按包名导入即可，不需要改 sys.path
        if _root not in sys.path:
            sys.path.append(_root)

        # 让 `import webapp` 生效：把 root 的父目录也放进去
        _parent = os.path.dirname(_root)
        if _parent not in sys.path:
            sys.path.append(_parent)

        try:
            from webapp import ir_split

            ir_split.install()
        except Exception:
            # 退路：直接按文件路径加载，避开包导入问题
            import importlib.util

            _target = os.path.join(_root, "webapp", "ir_split.py")
            if os.path.exists(_target):
                _spec = importlib.util.spec_from_file_location("_ir_split_boot", _target)
                if _spec and _spec.loader:
                    _mod = importlib.util.module_from_spec(_spec)
                    _spec.loader.exec_module(_mod)
                    _mod.install()
    except Exception:  # noqa: BLE001
        pass
