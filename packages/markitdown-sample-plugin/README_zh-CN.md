# MarkItDown 示例插件

[![PyPI](https://img.shields.io/pypi/v/markitdown-sample-plugin.svg)](https://pypi.org/project/markitdown-sample-plugin/)
![PyPI - Downloads](https://img.shields.io/pypi/dd/markitdown-sample-plugin)
[![Built by AutoGen Team](https://img.shields.io/badge/Built%20by-AutoGen%20Team-blue)](https://github.com/microsoft/autogen)


本项目演示如何为 MarkItDown 创建一个示例插件。最重要的部分如下：

首先，实现你的自定义 `DocumentConverter`：

```python
from typing import BinaryIO, Any
from markitdown import MarkItDown, DocumentConverter, DocumentConverterResult, StreamInfo, PRIORITY_SPECIFIC_FILE_FORMAT

class RtfConverter(DocumentConverter):

    def __init__(
        self, priority: float = PRIORITY_SPECIFIC_FILE_FORMAT
    ):
        super().__init__(priority=priority)

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> bool:

        # 实现判断文件流是否为 RTF 文件的逻辑
        # ...
        raise NotImplementedError()


    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,
    ) -> DocumentConverterResult:

        # 实现将文件流转换为 Markdown 的逻辑
        # ...
        raise NotImplementedError()
```

接下来，确保你的包实现并导出以下内容：

```python
# 本插件使用的插件接口版本。
# 目前唯一支持的版本是 1。
__plugin_interface_version__ = 1

# 插件的主入口点。每次创建 MarkItDown 实例时都会调用。
def register_converters(markitdown: MarkItDown, **kwargs):
    """
    在 MarkItDown 实例构造期间调用，用于注册插件提供的转换器。
    """

    # 创建并挂载一个 RtfConverter 实例
    markitdown.register_converter(RtfConverter())
```

最后，在 `pyproject.toml` 文件中创建入口点：

```toml
[project.entry-points."markitdown.plugin"]
sample_plugin = "markitdown_sample_plugin"
```

这里的 `sample_plugin` 可以是任意键，但最好使用插件名称。对应的值是实现该插件的包的全限定名。

## 安装

要在 MarkItDown 中使用插件，必须先安装它。从当前目录安装插件：

```bash
pip install -e .
```

插件包安装完成后，运行以下命令验证 MarkItDown 是否能识别该插件：

```bash
markitdown --list-plugins
```

要在转换时使用插件，请使用 `--use-plugins` 标志。例如，转换 RTF 文件：

```bash
markitdown --use-plugins path-to-file.rtf
```

在 Python 中，可以这样启用插件：

```python
from markitdown import MarkItDown

md = MarkItDown(enable_plugins=True)
result = md.convert("path-to-file.rtf")
print(result.text_content)
```

## 商标

本项目可能包含项目、产品或服务的商标或标识。Microsoft 商标或标识的授权使用须遵守并遵循 [Microsoft 商标与品牌指南](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general)。在本项目的修改版本中使用 Microsoft 商标或标识时，不得引起混淆或暗示 Microsoft 赞助。任何第三方商标或标识的使用均受相应第三方政策约束。
