# 第三方组件与资源许可声明

本程序（PDF 中英对照翻译）以 **AGPL-3.0** 发布，因为它链接并调用了同样以 AGPL-3.0 授权的
BabelDOC 与 PDFMathTranslate-next。完整许可证见 [LICENSE](LICENSE)。

---

## 1. 核心上游项目（AGPL-3.0，本程序的衍生来源）

| 项目 | 版本 | 许可证 | 说明 |
|---|---|---|---|
| [BabelDOC](https://github.com/funstory-ai/BabelDOC) | 0.6.2 | AGPL-3.0 | 版面分析、中间表示、译文回填与 PDF 重建。Copyright (C) 2024 funstory.ai limited |
| [PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next) | 2.9.0 | AGPL-3.0 | 提供本程序使用的公开 Python 入口 `do_translate_async_stream` 与翻译引擎适配层 |

> BabelDOC 官方声明其自身的 API 属于内部接口，推荐经由 PDFMathTranslate-next 调用。
> 本程序的翻译主链路（`webapp/translate_job.py`）**正是通过 pdf2zh-next 的公开入口调用**，
> 未直接调用 BabelDOC 内部 API。

**AGPL-3.0 第 5 条要求的修改声明**：本程序为上述项目的新增衍生作品，
对其**未做任何修改**，仅以库的形式导入调用；新增的代码为本仓库中的
`webapp/`、`launcher.py`、`translate.py` 等文件，均以 AGPL-3.0 授权。

---

## 2. 分发的二进制中包含的第三方库

下表由 `build/gen_third_party_notices.py` 从**已安装包的元数据**中自动生成，
并非人工填写；标「未标注」者请以该包自带 LICENSE 文件为准。

<!-- BEGIN AUTO-GENERATED PACKAGE LIST -->

| 组件 | 版本 | 许可证 |
|---|---|---|
| babeldoc | 0.6.2 | AGPL-3.0 |
| pdf2zh-next | 2.9.0 | AGPL-3.0 |
| pymupdf | 1.25.2 | AGPL-3.0 |
| onnxruntime | 1.30.0 | MIT License |
| onnx | 1.23.0 | Apache-2.0 |
| opencv-python-headless | 5.0.0.93 | Apache Software License |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| scipy | 1.18.1 | BSD-3-Clause |
| scikit-image | 0.26.0 | BSD-3-Clause |
| scikit-learn | 1.9.1 | BSD-3-Clause |
| fastapi | 0.141.1 | MIT |
| uvicorn | 0.53.0 | BSD-3-Clause |
| starlette | 0.52.1 | BSD-3-Clause |
| pydantic | 2.11.10 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| openai | 3.16.2 | Apache-2.0 |
| httpx | 0.28.1 | BSD License |
| tiktoken | 0.14.0 | MIT License |
| peewee | 4.5.1 | MIT |
| tenacity | 9.1.4 | Apache Software License |
| rtree | 1.4.1 | MIT |
| hyperscan | 0.8.2 | MIT License |
| uharfbuzz | 0.56.1 | Apache License 2.0 |
| freetype-py | 2.5.1 | BSD License |
| pyzstd | 0.19.1 | BSD-3-Clause |
| msgpack | 1.2.2 | Apache-2.0 |
| orjson | 3.12.0 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| Levenshtein | 0.27.5 | GPL-2.0-or-later |
| huggingface-hub | 1.32.0 | Apache Software License |
| xsdata | 26.2 | MIT |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| rich | 15.0.0 | MIT License |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| toml | 0.10.2 | MIT License |
| configargparse | 1.7.7 | MIT License |
| bitstring | 4.4.0 | MIT License |
| psutil | 7.2.2 | BSD-3-Clause |
| chardet | 7.6.0 | 0BSD |
| charset-normalizer | 3.5.1 | MIT |
| python-multipart | 0.0.32 | Apache-2.0 |
| sse-starlette | 3.4.11 | BSD-3-Clause |
| pillow | 11.3.0 | MIT-CMU |
| joblib | 1.6.0 | BSD-3-Clause |
| lxml | 6.1.3 | BSD-3-Clause |

<!-- END AUTO-GENERATED PACKAGE LIST -->

### 需要额外说明的组件

- **PyMuPDF（AGPL-3.0）**：PDF 解析与渲染。其 AGPL 授权是本程序必须整体采用 AGPL-3.0 的原因之一。
  如需闭源商用，须自行向 Artifex 获取商业授权。
- **Levenshtein（GPL-2.0-or-later）**：上游依赖。GPL-2.0-or-later 允许按更高版本（含 AGPL-3.0）
  再许可，与本程序整体 AGPL-3.0 兼容。
- **hyperscan（MIT）**：Windows 上为预编译 wheel，其内置的 Intel Hyperscan 本体为 BSD-3-Clause。

---

## 3. 模型与字体资源（首次运行下载 / 离线包内置）

程序运行需要约 336MB 的版面模型与多语言字体，来自以下上游仓库。
这些资源**由程序在首次运行时下载**，或在离线发行包内以官方 `offline_assets_*.zip` 形式附带。

| 资源 | 来源 | 规模 | 许可 |
|---|---|---|---|
| DocLayout-YOLO DocStructBench ONNX 版面模型 | [HuggingFace: wybxc/DocLayout-YOLO-DocStructBench-onnx](https://huggingface.co/wybxc/DocLayout-YOLO-DocStructBench-onnx)（上游 [opendatalab/DocLayout-YOLO](https://github.com/opendatalab/DocLayout-YOLO)） | 1 个 / 约 72MB | 上游仓库标注 Apache-2.0 |
| 多语言字体（Source Han Sans/Serif CJK、GoNotoKurrent、LXGW WenKai、KleeOne、MaruBuri、Noto 等） | [HuggingFace: awwaawwa/BabelDOC-Assets](https://huggingface.co/datasets/awwaawwa/BabelDOC-Assets) | 34 个 | 各字体自身的开源许可（多为 OFL / Apache-2.0）；**具体许可见 BabelDOC-Assets 仓库说明** |
| 各语言 CMap 映射表 | [HuggingFace: awwaawwa/BabelDOC-Assets](https://huggingface.co/datasets/awwaawwa/BabelDOC-Assets) | 146 个 / 约 9MB | 随 PDF 规范/Adobe 发布 |
| tiktoken 词表 | OpenAI tiktoken | 1 个 | MIT |

> 说明：模型与字体资源的许可以**上游仓库声明为准**。若你要对外分发内置这些资源的安装包，
> 建议在发布前逐一核对字体许可（尤其 OFL 字体要求保留其版权声明，不得单独售卖字体本身）。

---

## 4. 本程序不包含的内容

- **不包含** DeepSeek API Key，也不代理任何翻译请求：Key 由使用者自行提供。
- **不包含** 任何训练数据或用户文档。

---

## 5. 输出文档的归属

翻译生成的 PDF 属于使用者的作品，AGPL-3.0 不主张其权利（第 2 条：程序输出不因运行而自动被覆盖，
除非输出本身构成衍生作品）。程序会在输出 PDF 中写入生成者标记
（`producer` 字段标注由 AI 翻译生成），属上游库的既有行为，用于提示读者内容为机器翻译。

---

## 6. 重新生成本文件

```powershell
.venv\Scripts\python.exe build\gen_third_party_notices.py
```

该脚本读取当前虚拟环境中各包的元数据（`License-Expression` / `License` / trove 分类器），
在下方两个标记之间重新生成表格：

```
<!-- BEGIN AUTO-GENERATED PACKAGE LIST -->
<!-- END AUTO-GENERATED PACKAGE LIST -->
```
