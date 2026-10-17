# MarkItDown OCR 插件

用于 MarkItDown 的 LLM Vision 插件，可从 PDF、DOCX、PPTX 和 XLSX 文件中嵌入的图片里提取文本。

使用 MarkItDown 已有的 `llm_client` / `llm_model` 模式（与图片描述功能相同）——无需额外引入 ML 库或二进制依赖。

## 功能特性

- **增强 PDF 转换器**：从 PDF 内嵌图片中提取文本，并对扫描版 PDF 提供整页 OCR 回退
- **增强 DOCX 转换器**：对 Word 文档中的图片进行 OCR
- **增强 PPTX 转换器**：对 PowerPoint 演示文稿中的图片进行 OCR
- **增强 XLSX 转换器**：对 Excel 表格中的图片进行 OCR
- **上下文保留**：插入提取文本时保持文档结构和阅读顺序

## 安装

```bash
pip install markitdown-ocr
```

插件使用你已有的任意 OpenAI 兼容客户端。如果还没有，请先安装：

```bash
pip install openai
```

## 使用方法

### 命令行

```bash
markitdown document.pdf --use-plugins --llm-client openai --llm-model gpt-4o
```

### Python API

与图片描述用法相同，向 `MarkItDown()` 传入 `llm_client` 和 `llm_model`：

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

如果未提供 `llm_client`，插件仍会加载，但 OCR 会被静默跳过——回退到标准内置转换器。

### 自定义提示词

针对特定文档类型，可覆盖默认提取提示词：

```python
md = MarkItDown(
    enable_plugins=True,
    llm_client=OpenAI(),
    llm_model="gpt-4o",
    llm_prompt="Extract all text from this image, preserving table structure.",
)
```

### 任意 OpenAI 兼容客户端

可与任何遵循 OpenAI API 的客户端配合使用：

```python
from openai import AzureOpenAI

md = MarkItDown(
    enable_plugins=True,
    llm_client=AzureOpenAI(
        api_key="...",
        azure_endpoint="https://your-resource.openai.azure.com/",
        api_version="2024-02-01",
    ),
    llm_model="gpt-4o",
)
```

## 工作原理

当调用 `MarkItDown(enable_plugins=True, llm_client=..., llm_model=...)` 时：

1. MarkItDown 通过 `markitdown.plugin` 入口点组发现插件
2. 调用 `register_converters()`，并转发所有 kwargs（包括 `llm_client` 和 `llm_model`）
3. 插件根据这些参数创建 `LLMVisionOCRService`
4. 四个 OCR 增强转换器以 **优先级 -1.0** 注册——优先于优先级 0.0 的内置转换器

文件转换时：

1. OCR 转换器接受该文件
2. 从文档中提取嵌入图片
3. 每张图片连同提取提示词发送给 LLM
4. 返回的文本以内联方式插入，保留文档结构
5. 若 LLM 调用失败，转换会继续进行，只是跳过该图片的文本

## 支持的文件格式

### PDF

- 嵌入图片按位置提取（通过 `page.images` / 页面 XObject），并以内联 OCR 方式与周围文本按垂直阅读顺序交错排列。
- **扫描版 PDF**（页面无可提取文本）会自动检测：每页以 300 DPI 渲染并作为整页图片发送给 LLM。
- **格式损坏的 PDF**（pdfplumber/pdfminer 无法打开，例如 EOF 截断）会使用 PyMuPDF 页面渲染重试，因此仍能恢复内容。

### DOCX

- 通过文档部件关系（`doc.part.rels`）提取图片。
- OCR 在 DOCX→HTML→Markdown 流水线执行前运行：向 HTML 注入占位符 token，避免 markdown 转换器转义 OCR 标记，转换完成后再将占位符替换为格式化的 `*[Image OCR]...[End OCR]*` 块。
- 文档流（标题、段落、表格）在 OCR 块周围完整保留。

### PPTX

- 支持图片形状、带图片的占位符形状，以及组合内的图片。
- 每页幻灯片中的形状按从上到下、从左到右的阅读顺序处理。
- 若配置了 `llm_client`，LLM 会先尝试生成描述；未返回描述时再回退到 OCR。

### XLSX

- 从工作表（`sheet._images`）中提取嵌入图片。
- 根据图片锚点坐标（列/行 → Excel 字母表示法）计算单元格位置。
- 图片列在该工作表数据表之后的 `### Images in this sheet:` 小节中——不会插入到表格行内。

### 输出格式

每个提取的 OCR 块都会被包装为：

```text
*[Image OCR]
<extracted text>
[End OCR]*
```

## 故障排查

### 输出中缺少 OCR 文本

最常见原因是缺少 `llm_client` 或 `llm_model`。请检查：

```python
from openai import OpenAI
from markitdown import MarkItDown

md = MarkItDown(
    enable_plugins=True,
    llm_client=OpenAI(),   # 必需
    llm_model="gpt-4o",    # 必需
)
```

### 插件未加载

确认插件已安装且可被识别：

```bash
markitdown --list-plugins   # 应显示：ocr
```

### API 错误

插件会将 LLM API 错误作为警告输出并继续转换。请检查 API Key、配额，以及所选模型是否支持视觉输入。

## 开发

### 运行测试

```bash
cd packages/markitdown-ocr
pytest tests/ -v
```

### 从源码构建

```bash
git clone https://github.com/microsoft/markitdown.git
cd markitdown/packages/markitdown-ocr
pip install -e .
```

## 贡献

欢迎贡献！指南请参阅 [MarkItDown 仓库](https://github.com/microsoft/markitdown)。

## 许可证

MIT — 详见 [LICENSE](LICENSE)。

## 更新日志

### 0.1.0（首次发布）

- 支持 PDF、DOCX、PPTX、XLSX 的 LLM Vision OCR
- 扫描版 PDF 整页 OCR 回退
- 上下文感知的内联文本插入
- 基于优先级的转换器替换（无需修改代码）
