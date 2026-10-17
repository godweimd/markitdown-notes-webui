# MarkItDown Notes WebUI

面向学习资料和手写笔记整理的本地文档转换工具：在浏览器中上传文件，转换为 Markdown，并将结果与图片资源导入 Obsidian。

本项目基于微软开源项目 [MarkItDown v0.1.7](https://github.com/microsoft/markitdown/tree/v0.1.7) 二次开发。核心文档解析与多格式转换来自上游；本仓库主要扩展本地 Web 界面、OCR 接入、手写公式转写和笔记导出流程。

## 功能

- **浏览器操作**：拖拽上传、Markdown 与数学公式预览、复制和下载结果。
- **批量转换**：多个文件合并为一份 Markdown，或分别保存，并查看逐文件处理结果。
- **扫描件识别**：接入 RapidOCR 本地识别、腾讯云 OCR 和智谱视觉模型，提供 `auto`、`local`、`cloud`、`vl` 四种模式。
- **手写公式转写**：通过视觉模型输出 LaTeX，处理图片方向和模型输出长度限制。
- **笔记资源管理**：导出 Markdown 与 `assets/` 图片，支持 Obsidian 图片引用和 ZIP 打包。

## 处理流程

```text
文件上传 → MarkItDown 转换 → 文字提取不足时进入 OCR
                              ├─ local：RapidOCR
                              ├─ cloud：腾讯云 OCR
                              ├─ auto：腾讯云优先，失败转本地
                              └─ vl：智谱视觉模型，输出 Markdown / LaTeX
         → 浏览器预览 → Markdown / 图片资源 / ZIP 导出
```

## 快速开始

需要 Python 3.10 或更高版本。以下命令在项目根目录执行。

```powershell
git clone https://github.com/godweimd/markitdown-notes-webui.git
cd markitdown-notes-webui
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "packages/markitdown[pdf,docx,pptx,xlsx]"
.\.venv\Scripts\python.exe webui/server.py
```

浏览器会打开 `http://127.0.0.1:8765`；如果端口已占用，以启动日志显示的地址为准。完成环境安装后，Windows 下也可双击 `start_webui.bat`。

Linux / macOS 可用 `python3 -m venv .venv` 创建环境，并将后续命令中的 Python 路径替换为 `.venv/bin/python`。

### 本地 OCR（可选）

```powershell
.\.venv\Scripts\python.exe -m pip install rapidocr-onnxruntime pymupdf numpy pillow
```

在界面中选择 `local`。该识别路径不需要云端密钥，文档内容无需发送至识别服务。

### 云端 OCR / 视觉模型（可选）

安装图片与 PDF 处理依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install pymupdf pillow
```

| 模式 | 配置方式 |
| --- | --- |
| `cloud` | 复制 `webui/tencent_config.example.json` 为 `webui/tencent_config.json`，填入自己的腾讯云密钥 |
| `vl` | 复制 `webui/zhipu_config.example.json` 为 `webui/zhipu_config.json`，填入自己的智谱 API Key |
| `auto` | 腾讯云可用时优先调用，失败后尝试本地 RapidOCR |

云端模式会向相应服务商发送图片或页面内容，可能产生 API 费用；模型可用性和计费以服务商为准。真实配置文件已被 `.gitignore` 排除，仓库仅包含空密钥模板。

### 导出到 Obsidian

```powershell
.\.venv\Scripts\python.exe webui/server.py --output "D:/你的知识库/导入资料"
```

默认输出目录为项目根目录下的 `output/`。建议使用单独的导入目录；重复使用同一输出名称会覆盖对应结果和导出的图片。

## 二次开发范围

| 内容 | 来源 / 作用 |
| --- | --- |
| `packages/markitdown*` | 上游转换引擎、MCP、OCR 插件和示例插件；保留原作者信息 |
| `webui/server.py` | Python 标准库 HTTP 服务，上传解析、转换入口、批量处理与文件导出 |
| `webui/index.html` | 浏览器界面、识别选项、Markdown 与公式预览 |
| `webui/ocr_fallback.py` | OCR 模式调度、PDF 页面渲染、文字与插图排版 |
| `webui/tencent_ocr.py` | 腾讯云请求签名、图片预处理与识别结果转换 |
| `webui/zhipu_vl.py` | 视觉模型接入、方向处理、公式转写和输出截断续写 |
| `start_webui.bat` | Windows 启动入口 |

更多界面说明见 [webui/README.md](webui/README.md)，上游说明保留在 [README.upstream.md](README.upstream.md)。

## 验证情况与限制

- 已完成离线检查：中文文本、HTML、DOCX、PDF 转换，以及两文件批量转换与 Markdown 落盘。
- 云端 OCR、视觉模型、本地 OCR 的识别质量尚未完成本次发布的系统评测，不提供准确率或性能提升承诺。
- OCR 补充由提取文本长度阈值触发，可能遗漏文字层较多但仍含扫描内容的混合 PDF；当前逐页识别上限为 60 页。
- 公式转写依赖模型输出，需要人工核对；达到续写上限时仍可能不完整。
- 这是本地运行的工具，默认只监听 `127.0.0.1`；当前没有用户鉴权，不应直接暴露到公网。
- 公式预览依赖在线加载 MathJax；该页面资源与本地 OCR 识别本身是不同的联网环节。

## 许可与致谢

沿用 [MIT License](LICENSE)，保留 Microsoft Corporation 及上游贡献者的版权声明。感谢 [microsoft/markitdown](https://github.com/microsoft/markitdown)；本项目与微软官方无隶属关系。
