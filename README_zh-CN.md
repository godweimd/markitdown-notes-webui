# MarkItDown

[![PyPI](https://img.shields.io/pypi/v/markitdown.svg)](https://pypi.org/project/markitdown/)
![PyPI - Downloads](https://img.shields.io/pypi/dd/markitdown)
[![Built by AutoGen Team](https://img.shields.io/badge/Built%20by-AutoGen%20Team-blue)](https://github.com/microsoft/autogen)

> [!IMPORTANT]
> MarkItDown 会以当前进程的权限执行 I/O 操作。与 `open()` 或 `requests.get()` 类似，它会访问当前进程本身有权访问的资源。在不可信环境中使用时，请对输入进行安全处理，并仅调用满足你使用场景的最窄 `convert_*` 函数（例如 `convert_stream()` 或 `convert_local()`）。更多信息请参阅文档中的[安全注意事项](#安全注意事项)部分。

MarkItDown 是一个轻量级 Python 工具，用于将各种文件转换为 Markdown，以便与大语言模型（LLM）及相关文本分析流水线配合使用。从用途上看，它最接近于 [textract](https://github.com/deanmalmgren/textract)，但更侧重于将文档的重要结构和内容保留为 Markdown 格式（包括：标题、列表、表格、链接等）。虽然输出通常具有较好的可读性且对人类友好，但它主要面向文本分析工具消费——可能并不是面向人类阅读的高保真文档转换的最佳选择。

MarkItDown 目前支持从以下格式转换：

- PDF
- PowerPoint
- Word
- Excel
- 图片（EXIF 元数据和 OCR）
- 音频（EXIF 元数据和语音转录）
- HTML
- 基于文本的格式（CSV、JSON、XML）
- ZIP 文件（遍历其中内容）
- YouTube URL
- EPub
- ……以及更多！

## 为什么选择 Markdown？

Markdown 非常接近纯文本，标记和格式开销极小，但仍能表达重要的文档结构。OpenAI 的 GPT-4o 等主流 LLM 原生就“会说” Markdown，且常常会在回复中自动使用 Markdown。这表明它们已在大量 Markdown 格式文本上训练过，并对此理解良好。作为额外好处，Markdown 约定在 token 使用上也极为高效。

## 前置要求

MarkItDown 需要 Python 3.10 或更高版本。建议使用虚拟环境，以避免依赖冲突。

使用标准 Python 安装时，可通过以下命令创建并激活虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate
```

如果使用 `uv`，可以这样创建虚拟环境：

```bash
uv venv --python=3.12 .venv
source .venv/bin/activate
# 注意：在此虚拟环境中安装包时，请使用 'uv pip install'，而不是直接使用 'pip install'
```

如果使用 Anaconda，可以这样创建虚拟环境：

```bash
conda create -n markitdown python=3.12
conda activate markitdown
```

## 安装

安装 MarkItDown 可使用 pip：`pip install 'markitdown[all]'`。也可以从源码安装：

```bash
git clone git@github.com:microsoft/markitdown.git
cd markitdown
pip install -e 'packages/markitdown[all]'
```

## 使用方法

### 命令行

```bash
markitdown path-to-file.pdf > document.md
```

或使用 `-o` 指定输出文件：

```bash
markitdown path-to-file.pdf -o document.md
```

你也可以通过管道传入内容：

```bash
cat path-to-file.pdf | markitdown
```

### 可选依赖

MarkItDown 为不同文件格式提供了可选依赖。前文我们使用 `[all]` 选项安装了全部可选依赖。但你也可以按需单独安装，以获得更精细的控制。例如：

```bash
pip install 'markitdown[pdf, docx, pptx]'
```

这只会安装 PDF、DOCX 和 PPTX 文件所需的依赖。

目前可用的可选依赖如下：

* `[all]` 安装所有可选依赖
* `[pptx]` 安装 PowerPoint 文件所需依赖
* `[docx]` 安装 Word 文件所需依赖
* `[xlsx]` 安装 Excel 文件所需依赖
* `[xls]` 安装旧版 Excel 文件所需依赖
* `[pdf]` 安装 PDF 文件所需依赖
* `[outlook]` 安装 Outlook 邮件所需依赖
* `[az-doc-intel]` 安装 Azure Document Intelligence 所需依赖
* `[az-content-understanding]` 安装 Azure Content Understanding 所需依赖
* `[audio-transcription]` 安装 wav 和 mp3 文件音频转录所需依赖
* `[youtube-transcription]` 安装 YouTube 视频字幕获取所需依赖

### 插件

MarkItDown 也支持第三方插件。插件默认禁用。要列出已安装的插件：

```bash
markitdown --list-plugins
```

要启用插件，请使用：

```bash
markitdown --use-plugins path-to-file.pdf
```

要查找可用插件，可在 GitHub 上搜索标签 `#markitdown-plugin`。若要开发插件，请参阅 `packages/markitdown-sample-plugin`。

#### markitdown-ocr 插件

`markitdown-ocr` 插件为 PDF、DOCX、PPTX 和 XLSX 转换器增加了 OCR 支持，可通过 LLM Vision 从嵌入图片中提取文本——使用与 MarkItDown 现有图片描述功能相同的 `llm_client` / `llm_model` 模式。无需额外引入新的 ML 库或二进制依赖。

**安装：**

```bash
pip install markitdown-ocr
pip install openai  # 或任意 OpenAI 兼容客户端
```

**用法：**

传入与图片描述相同的 `llm_client` 和 `llm_model`：

```python
from markitdown import MarkItDown
from openai import OpenAI

md = MarkItDown(
    enable_plugins=True,
    llm_client=OpenAI(),
    llm_model="gpt-4o",
)
result = md.convert("document_with_images.pdf")
print(result.text_content)
```

如果未提供 `llm_client`，插件仍会加载，但 OCR 会被静默跳过，转而使用标准内置转换器。

详细文档请参阅 [`packages/markitdown-ocr/README.md`](packages/markitdown-ocr/README.md)。

### Azure Content Understanding

[Azure Content Understanding](https://learn.microsoft.com/azure/ai-services/content-understanding/) 提供更高质量的转换能力，支持结构化字段提取（YAML front matter）、多模态支持（文档、图片、音频、视频）以及可配置的分析器。

安装：`pip install 'markitdown[az-content-understanding]'`

#### 何时使用 Content Understanding

当你需要超出内置转换器或 Document Intelligence 转换器能力的功能时，Content Understanding 是理想选择：

- **音频和视频文件** — CU 是视频的唯一选项，也是音频更高质量的云端选项。内置转换器不支持视频，且仅提供基础音频转录。
- **结构化字段提取** — [预构建](https://learn.microsoft.com/azure/ai-services/content-understanding/concepts/prebuilt-analyzers)或[自定义构建](https://learn.microsoft.com/azure/ai-services/content-understanding/how-to/customize-analyzer-content-understanding-studio?tabs=portal)的分析器可提取领域特定字段（发票金额、收据日期、合同条款等），并序列化为 YAML front matter。内置转换器和 Doc Intel 集成都不会暴露这些字段。
- **更高质量的文档提取** — 针对扫描 PDF、复杂表格和多页文档，提供基于云端的布局分析和 OCR。
- **单一 API 覆盖所有模态** — 一个 `cu_endpoint` 即可处理文档、图片、音频和视频，并自动路由到合适的分析器。

| 能力 | 内置转换器 | Azure Document Intelligence | Azure Content Understanding |
|------|-----------|----------------------------|----------------------------|
| 文档转换 | 离线、按格式提取 | 云端布局提取 | 云端多模态提取 |
| 结构化字段 | 不可用 | 此集成不暴露 | 来自分析器字段的 YAML front matter |
| 自定义分析器 | 不可用 | 此集成不可配置 | 支持 `cu_analyzer_id` |
| 音频和视频 | 基础音频，不支持视频 | 不支持 | 音频和视频分析器 |
| 成本 | 仅本地计算 | 可计费的 Azure API 调用 | 可计费的 Azure API 调用 |

**命令行：**

```bash
markitdown path-to-file.pdf --use-cu --cu-endpoint "<content_understanding_endpoint>"
```

**Python API：**

```python
from markitdown import MarkItDown

# 零配置 — 按文件类型自动选择分析器
md = MarkItDown(cu_endpoint="<content_understanding_endpoint>")
result = md.convert("report.pdf")   # 文档 → prebuilt-documentSearch
result = md.convert("meeting.mp4")  # 视频 → prebuilt-videoSearch
result = md.convert("call.wav")     # 音频 → prebuilt-audioSearch
print(result.markdown)
```

**使用自定义分析器**（用于领域特定字段提取）：

```python
md = MarkItDown(
    cu_endpoint="<content_understanding_endpoint>",
    cu_analyzer_id="my-invoice-analyzer",
)
result = md.convert("invoice.pdf")
print(result.markdown)
# 输出包含带提取字段的 YAML front matter：
# ---
# contentType: document
# fields:
#   VendorName: CONTOSO LTD.
#   InvoiceDate: '2019-11-15'
# ---
# <!-- page 1 -->
# ...
```

当设置了 `cu_analyzer_id` 时，转换器会根据分析器的模态自动将其限定到兼容的文件类型。不兼容的类型（例如用文档分析器处理音频文件）会自动路由到默认预构建分析器。

**成本说明：** 每次对 CU 路由格式的 `convert()` 调用都是一次可计费的 Azure API 调用。可使用 `cu_file_types` 限制哪些格式走 CU：

```python
from markitdown.converters import ContentUnderstandingFileType

md = MarkItDown(
    cu_endpoint="<content_understanding_endpoint>",
    cu_file_types=[ContentUnderstandingFileType.PDF],  # 仅 PDF 使用 CU
)
```

更多关于 Azure Content Understanding 的信息请参阅[此处](https://learn.microsoft.com/azure/ai-services/content-understanding/)。

### Azure Document Intelligence

要使用 Microsoft Document Intelligence 进行转换：

```bash
markitdown path-to-file.pdf -o document.md -d -e "<document_intelligence_endpoint>"
```

关于如何设置 Azure Document Intelligence 资源的更多信息请参阅[此处](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/how-to-guides/create-document-intelligence-resource?view=doc-intel-4.0.0)

### Python API

Python 中的基础用法：

```python
from markitdown import MarkItDown

md = MarkItDown(enable_plugins=False) # 设为 True 以启用插件
result = md.convert("test.xlsx")
print(result.text_content)
```

Python 中使用 Document Intelligence 转换：

```python
from markitdown import MarkItDown

md = MarkItDown(docintel_endpoint="<document_intelligence_endpoint>")
result = md.convert("test.pdf")
print(result.text_content)
```

要使用大语言模型生成图片描述（目前仅适用于 pptx 和图片文件），请提供 `llm_client` 和 `llm_model`：

```python
from markitdown import MarkItDown
from openai import OpenAI

client = OpenAI()
md = MarkItDown(llm_client=client, llm_model="gpt-4o", llm_prompt="optional custom prompt")
result = md.convert("example.jpg")
print(result.text_content)
```

### Docker

```sh
docker build -t markitdown:latest .
docker run --rm -i markitdown:latest < ~/your-file.pdf > output.md
```

## 贡献

本项目欢迎贡献和建议。大多数贡献需要你同意贡献者许可协议（CLA），声明你有权并确实授予我们使用你贡献内容的权利。详情请访问 https://cla.opensource.microsoft.com。

当你提交 Pull Request 时，CLA 机器人会自动判断你是否需要提供 CLA，并相应地标注 PR（例如状态检查、评论）。只需按照机器人提供的说明操作即可。在我们使用 CLA 的所有仓库中，你只需要完成一次。

本项目已采用 [Microsoft 开源行为准则](https://opensource.microsoft.com/codeofconduct/)。更多信息请参阅[行为准则 FAQ](https://opensource.microsoft.com/codeofconduct/faq/)，或通过 [opencode@microsoft.com](mailto:opencode@microsoft.com) 联系我们提出其他问题或意见。

### 如何贡献

你可以通过查看 issue 或帮助 review PR 来参与贡献。任何 issue 或 PR 都欢迎参与，我们也标记了一些“open for contribution”和“open for reviewing”的条目，以便促进社区贡献。这些只是建议，欢迎你以任何方式参与。

<div align="center">

|            | 全部                                                          | 特别需要社区帮助                                                                                                      |
| ---------- | ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| **Issues** | [All Issues](https://github.com/microsoft/markitdown/issues) | [Issues open for contribution](https://github.com/microsoft/markitdown/issues?q=is%3Aissue+is%3Aopen+label%3A%22open+for+contribution%22) |
| **PRs**    | [All PRs](https://github.com/microsoft/markitdown/pulls)     | [PRs open for reviewing](https://github.com/microsoft/markitdown/pulls?q=is%3Apr+is%3Aopen+label%3A%22open+for+reviewing%22)              |

</div>

### 运行测试和检查

- 进入 MarkItDown 包目录：

  ```sh
  cd packages/markitdown
  ```

- 在环境中安装 `hatch` 并运行测试：

  ```sh
  pip install hatch  # 其他安装 hatch 的方式：https://hatch.pypa.io/dev/install/
  hatch shell
  hatch test
  ```

  （替代方案）使用已安装全部依赖的 Devcontainer：

  ```sh
  # 在 Devcontainer 中重新打开项目并运行：
  hatch test
  ```

- 提交 PR 前运行 pre-commit 检查：`pre-commit run --all-files`

### 安全注意事项

MarkItDown 会以当前进程的权限执行 I/O 操作。与 `open()` 或 `requests.get()` 类似，它会访问当前进程本身有权访问的资源。

**清理你的输入：** 不要将不可信输入直接传给 MarkItDown。如果输入的任何部分可能由不可信用户或系统控制（例如在托管或服务端应用中），在调用 MarkItDown 之前必须对其进行验证和限制。根据你的环境，这可能包括限制文件路径、限制 URI 方案和网络目标，以及阻止访问私有、回环、链路本地或元数据服务地址。

**只调用你需要的转换方法：** 优先使用符合你使用场景的最窄转换 API。MarkItDown 的 `convert()` 方法有意设计得较为宽松，可处理本地文件、远程 URI 和字节流。如果你的应用只需要读取本地文件，请改用 `convert_local()`。如果你需要对 URI 获取有更多控制，请自行调用 `requests.get()`，并将响应对象传给 `convert_response()`。若要最大控制权，请打开你要转换的输入流，并调用 `convert_stream()`。

### 贡献第三方插件

你也可以通过创建和分享第三方插件来参与贡献。详情请参阅 `packages/markitdown-sample-plugin`。

## 商标

本项目可能包含项目、产品或服务的商标或标识。Microsoft 商标或标识的授权使用须遵守并遵循 [Microsoft 商标与品牌指南](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general)。在本项目的修改版本中使用 Microsoft 商标或标识时，不得引起混淆或暗示 Microsoft 赞助。任何第三方商标或标识的使用均受相应第三方政策约束。
