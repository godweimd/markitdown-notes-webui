# MarkItDown-MCP

> [!IMPORTANT]
> MarkItDown-MCP 包适用于**本地使用**，配合本地可信 Agent。特别是，当使用 Streamable HTTP 或 SSE 运行 MCP 服务器时，它默认绑定到 `localhost`，不会暴露给网络上的其他机器或互联网。在此配置下，它可作为 STDIO 传输的直接替代方案，在某些场景下可能更方便。除非你理解这样做的[安全影响](#安全注意事项)，否则**不要**将服务器绑定到其他网络接口。


[![PyPI](https://img.shields.io/pypi/v/markitdown-mcp.svg)](https://pypi.org/project/markitdown-mcp/)
![PyPI - Downloads](https://img.shields.io/pypi/dd/markitdown-mcp)
[![Built by AutoGen Team](https://img.shields.io/badge/Built%20by-AutoGen%20Team-blue)](https://github.com/microsoft/autogen)

`markitdown-mcp` 包提供了一个轻量级的 STDIO、Streamable HTTP 和 SSE MCP 服务器，用于调用 MarkItDown。

它暴露一个工具：`convert_to_markdown(uri)`，其中 `uri` 可以是任意 `http:`、`https:`、`file:` 或 `data:` URI。

## 安装

使用 pip 安装：

```bash
pip install markitdown-mcp
```

## 使用方法

使用 STDIO（默认）运行 MCP 服务器：

```bash
markitdown-mcp
```

使用 Streamable HTTP 和 SSE 运行 MCP 服务器：

```bash
markitdown-mcp --http --host 127.0.0.1 --port 3001
```

## 在 Docker 中运行

要在 Docker 中运行 `markitdown-mcp`，使用提供的 Dockerfile 构建镜像：
```bash
docker build -t markitdown-mcp:latest .
```

然后运行：
```bash
docker run -it --rm markitdown-mcp:latest
```
这对于远程 URI 已足够。若要访问本地文件，需要将本地目录挂载到容器中。例如，若要访问 `/home/user/data` 中的文件，可以运行：

```bash
docker run -it --rm -v /home/user/data:/workdir markitdown-mcp:latest
```

挂载后，`data` 目录下的所有文件在容器中可通过 `/workdir` 访问。例如，若 `/home/user/data` 中有 `example.txt`，在容器中可通过 `/workdir/example.txt` 访问。

## 从 Claude Desktop 访问

建议在 Claude Desktop 中使用 Docker 镜像运行 MCP 服务器。

按照[这些说明](https://modelcontextprotocol.io/quickstart/user#for-claude-desktop-users)访问 Claude 的 `claude_desktop_config.json` 文件。

编辑该文件，加入以下 JSON 配置：

```json
{
  "mcpServers": {
    "markitdown": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "markitdown-mcp:latest"
      ]
    }
  }
}
```

若要挂载目录，请相应调整：

```json
{
  "mcpServers": {
    "markitdown": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-v",
        "/home/user/data:/workdir",
        "markitdown-mcp:latest"
      ]
    }
  }
}
```

## 调试

可使用 `MCP Inspector` 工具调试 MCP 服务器。

```bash
npx @modelcontextprotocol/inspector
```

然后通过指定的主机和端口连接 Inspector（例如 `http://localhost:5173/`）。

如果使用 STDIO：
* 选择 `STDIO` 作为传输类型，
* 输入 `markitdown-mcp` 作为命令，并
* 点击 `Connect`

如果使用 Streamable HTTP：
* 选择 `Streamable HTTP` 作为传输类型，
* 输入 `http://127.0.0.1:3001/mcp` 作为 URL，并
* 点击 `Connect`

如果使用 SSE：
* 选择 `SSE` 作为传输类型，
* 输入 `http://127.0.0.1:3001/sse` 作为 URL，并
* 点击 `Connect`

最后：
* 点击 `Tools` 标签页，
* 点击 `List Tools`，
* 点击 `convert_to_markdown`，并
* 对任意有效 URI 运行该工具。

## 安全注意事项

该服务器不支持身份认证，并以运行它的用户权限执行。因此，在 SSE 或 Streamable HTTP 模式下，服务器默认绑定到 `localhost`。即便如此，仍需注意：同一本地机器上的任何进程或用户都可能访问该服务器，且 `convert_to_markdown` 工具可用于读取服务器用户有权访问的任何文件，或从网络获取任何数据。如果你需要更高安全性，请考虑在沙箱环境（如虚拟机或容器）中运行服务器，并确保用户权限配置正确，以限制对敏感文件和网络段的访问。最重要的是，除非你理解这样做的安全影响，否则**不要**将服务器绑定到其他接口（非 localhost）。

## 商标

本项目可能包含项目、产品或服务的商标或标识。Microsoft 商标或标识的授权使用须遵守并遵循 [Microsoft 商标与品牌指南](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general)。在本项目的修改版本中使用 Microsoft 商标或标识时，不得引起混淆或暗示 Microsoft 赞助。任何第三方商标或标识的使用均受相应第三方政策约束。
