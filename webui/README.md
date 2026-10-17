# MarkItDown Web 界面（webui）

给 [MarkItDown](https://github.com/microsoft/markitdown) 加的一层本地网页界面：拖拽文件即可转换成 Markdown，无需命令行。

## 相比原版 CLI 多了什么

- **网页拖拽界面**：浏览器里上传文件 → 实时预览 Markdown → 一键打包下载
- **批量处理**：一次拖多张图片 / 多个文件，合并输出
- **四档 OCR 引擎**（扫描件 / 图片没有文字层时自动识别）：
  | 模式 | 后端 | 说明 |
  |------|------|------|
  | `auto` | 云端优先，失败回落本地 | 默认 |
  | `local` | RapidOCR（离线） | 免费、隐私、无需联网 |
  | `cloud` | 腾讯云 OCR | 手写体 / 低质量扫描件精度更高 |
  | `vl` | 智谱 GLM-4V | **公式 → LaTeX**，手写数学笔记首选 |
- **数学公式转 LaTeX**：`vl` 模式把公式输出为 `$...$` / `$$...$$`，Obsidian 的 MathJax 可直接渲染
- **图片自动摆正**：识别并纠正倒置 / 侧躺的手机照片
- **超长页面自动续写**：输出撞到模型上限时会自动「接着写」，免费模型也能转写完长页面
- **落盘到 Obsidian**：转换结果按「文件名/同名.md + assets/」落盘，配合 wikilink 直接进你的知识库

## 快速开始

```bash
# 1. 安装 markitdown（及其可选依赖）
uv pip install -e "packages/markitdown[all]"

# 2. 启动界面（本地 OCR 可选安装）
python webui/server.py
# 或 Windows 下双击 start_webui.bat
```

浏览器会自动打开 `http://127.0.0.1:8765`。

### 可选：本地 OCR（`local` 模式）

```bash
uv pip install rapidocr-onnxruntime pymupdf numpy pillow
```

### 可选：云端 OCR（`cloud` / `vl` 模式）

| 引擎 | 配置 | 获取 Key |
|------|------|----------|
| 腾讯云 | 复制 `webui/tencent_config.example.json` → `tencent_config.json`，填 `secret_id` / `secret_key` | 腾讯云「文字识别」控制台 |
| 智谱 | 复制 `webui/zhipu_config.example.json` → `zhipu_config.json`，填 `api_key` | [open.bigmodel.cn](https://open.bigmodel.cn) |

> ⚠️ 两个 `*_config.json` 含真实密钥，**已被 `.gitignore` 排除，切勿提交**。只提交 `*.example.json` 模板。

## 命令行参数

```bash
python webui/server.py --port 9000          # 改端口
python webui/server.py --no-browser         # 不自动打开浏览器
python webui/server.py --output "D:/你的/知识库"   # 改输出目录（默认项目根 output/）
```

也可用环境变量 `MARKITDOWN_OUTPUT_DIR` 覆盖默认输出目录。

## 目录结构

```
webui/
├── server.py          # 标准库实现的 HTTP 服务（无框架依赖）
├── index.html         # 前端单页（含 MathJax 公式渲染）
├── ocr_fallback.py    # 四引擎 OCR 调度 + 版面重排
├── tencent_ocr.py     # 腾讯云 OCR（TC3-HMAC-SHA256 签名，纯标准库）
├── zhipu_vl.py        # 智谱 GLM-4V（公式转 LaTeX + 自动摆正）
└── *.example.json     # 配置文件模板（复制后改名填 Key）
```

## 注意事项

- 本界面是 markitdown 的**附加层**，核心转换逻辑仍来自原版 `packages/markitdown`。
- 输出目录默认为项目根 `output/`（已 gitignore），不会污染仓库。
- 转换内容通过 `--output` 或环境变量指向你的 Obsidian 仓库即可直接入库。
