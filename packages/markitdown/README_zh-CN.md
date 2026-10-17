# MarkItDown

> [!TIP]
> MarkItDown 是一个 Python 包和命令行工具，用于将各种文件转换为 Markdown（例如用于索引、文本分析等）。
>
> 更多信息及完整文档，请参阅 GitHub 上的项目 [README.md](https://github.com/microsoft/markitdown)。

> [!IMPORTANT]
> MarkItDown 会以当前进程的权限执行 I/O 操作。与 `open()` 或 `requests.get()` 类似，它会访问当前进程本身有权访问的资源。在不可信环境中使用时，请对输入进行安全处理，并仅调用满足你使用场景的最窄 `convert_*` 函数（例如 `convert_stream()` 或 `convert_local()`）。更多信息请参阅文档中的[安全注意事项](https://github.com/microsoft/markitdown#security-considerations)部分。

## 安装

从 PyPI 安装：

```bash
pip install 'markitdown[all]'
```

从源码安装：

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

### Python API

```python
from markitdown import MarkItDown

md = MarkItDown()
result = md.convert("test.xlsx")
print(result.text_content)
```

### 更多信息

更多信息及完整文档，请参阅 GitHub 上的项目 [README.md](https://github.com/microsoft/markitdown)。

## 商标

本项目可能包含项目、产品或服务的商标或标识。Microsoft 商标或标识的授权使用须遵守并遵循 [Microsoft 商标与品牌指南](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general)。在本项目的修改版本中使用 Microsoft 商标或标识时，不得引起混淆或暗示 Microsoft 赞助。任何第三方商标或标识的使用均受相应第三方政策约束。
