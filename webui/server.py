# -*- coding: utf-8 -*-
"""MarkItDown 本地 Web 界面 —— 后端服务。

只依赖 Python 标准库，直接复用项目 .venv 里已安装的 markitdown，
因此不需要额外安装任何第三方包。

用法::

    python webui/server.py                 # 默认 http://127.0.0.1:8765
    python webui/server.py --port 9000
    python webui/server.py --no-browser    # 不自动打开浏览器
    python webui/server.py --output "/path/to/obsidian/vault"   # 改输出目录
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
import zipfile
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 让脚本无论从哪里启动，都能导入到本项目的 markitdown 源码
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
_SRC = PROJECT_ROOT / "packages" / "markitdown" / "src"
for _p in (str(_SRC), str(BASE_DIR)):
    if Path(_p).is_dir() and _p not in sys.path:
        sys.path.insert(0, _p)

from markitdown import MarkItDown, StreamInfo  # noqa: E402
from markitdown.__about__ import __version__ as MARKITDOWN_VERSION  # noqa: E402

import ocr_fallback  # noqa: E402  （同目录模块）

INDEX_FILE = BASE_DIR / "index.html"
# 转换结果落盘的位置：每份文件一个子目录，里面是 同名.md + assets/ 图片。
# 默认落在项目根目录下的 output/（已被 .gitignore 忽略，不污染仓库）。
# 若要写进自己的 Obsidian 仓库，任选其一：
#   1) 启动时 --output "D:\\...\\你的知识库"
#   2) 设置环境变量 MARKITDOWN_OUTPUT_DIR
DEFAULT_OUTPUT_DIR = Path(
    os.environ.get("MARKITDOWN_OUTPUT_DIR")
    or (PROJECT_ROOT / "output")
)
OUTPUT_DIR = DEFAULT_OUTPUT_DIR

# 一个 MarkItDown 实例初始化要加载 magika 模型（约 1-2 秒），
# 所以按 “是否启用插件” 缓存复用。
_ENGINES: Dict[bool, MarkItDown] = {}
_ENGINE_LOCK = threading.Lock()


def get_engine(use_plugins: bool) -> MarkItDown:
    """获取（或惰性创建）一个 MarkItDown 实例。"""
    with _ENGINE_LOCK:
        engine = _ENGINES.get(use_plugins)
        if engine is None:
            engine = MarkItDown(enable_plugins=use_plugins)
            _ENGINES[use_plugins] = engine
        return engine


# --------------------------------------------------------------------------
# 转换结果缓存：导出的图片要能被浏览器取到、被下载打包
# --------------------------------------------------------------------------
_CONV_LOCK = threading.Lock()
_CONVERSIONS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
MAX_CONVERSIONS = 8


def _safe_dirname(name: str) -> str:
    """把任意名字清理成合法的目录名（Windows 禁止 <>:"/\\|?* 等字符）。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    cleaned = cleaned.strip(" .")
    return cleaned[:80] or "output"


def _looks_binary(data: bytes) -> bool:
    """粗判是否二进制文件：前 8KB 里出现 NUL 字节基本可断定不是纯文本。"""
    return b"\x00" in data[:8192]


def _guess_text_charset(data: bytes) -> Optional[str]:
    """给文本文件猜编码，**UTF-8 优先**，猜不出再交给 charset_normalizer。

    为什么需要它：charset_normalizer 对「中文字符占比低」的短文本容易误判成
    cp855 / cp1252 等单字节编码 —— 实测一份 UTF-8 的 JSON 被猜成 cp855，
    "测试配置" 变成 "ТхІУ»ЋжЁЇуй«"。UTF-8 是当今文本文件的绝对主流，
    能干净解码就应该采用；只有解不动（如 GBK 老文件）才回退到自动探测。
    """
    if not data:
        return None
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None and best.encoding:
            return best.encoding
    except Exception:  # noqa: BLE001
        pass
    return None


def _sniff_stream_info(data: bytes) -> Tuple[Optional[str], Optional[str]]:
    """通过文件头魔数/内容特征，为「无扩展名」文件补全 mimetype 和 extension。

    爬虫、截图工具、某些下载器保存的文件经常不带扩展名（例如直接以文章标题命名），
    但 markitdown / OCR fallback 都依赖扩展名或 mimetype 做路由。这里只嗅探最稳的
    几种魔数和文本特征，避免误伤。
    """
    if not data:
        return None, None

    head = data[:16]

    # 图片
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", ".gif"
    if head[:2] == b"BM":
        return "image/bmp", ".bmp"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff", ".tiff"

    # PDF
    if head[:4] == b"%PDF":
        return "application/pdf", ".pdf"

    # ZIP 族（docx/xlsx/pptx/epub/zip 都走 zip converter 即可）
    if head[:4] == b"PK\x03\x04":
        return "application/zip", ".zip"

    # 文本类特征（已经排除二进制，因为调用方会先用 _looks_binary 过滤）
    text_head = data[:512].decode("utf-8", "ignore").lstrip().lower()
    if text_head.startswith(("<!doctype html", "<html", "<?xml")):
        return "text/html", ".html"
    if text_head.startswith("{"):
        return "application/json", ".json"
    # CSV：至少两行，且都有逗号（保守）
    lines = data[:2048].decode("utf-8", "ignore").splitlines()
    if len(lines) >= 2 and all("," in line for line in lines[: min(5, len(lines))]):
        return "text/csv", ".csv"

    # 兜底当纯文本
    return "text/plain", ".txt"


def _base_name(filename: Optional[str], url: Optional[str]) -> str:
    """决定输出目录 / 文件的名字。"""
    if filename:
        stem = Path(filename).stem
    elif url:
        stem = Path(urllib.parse.urlparse(url).path).stem
    else:
        stem = ""
    return _safe_dirname(stem)


def _write_output(
    base_name: str, markdown: str, assets: List[Dict[str, Any]]
) -> Optional[str]:
    """把结果写到 output/<名字>/ 下，返回该目录的绝对路径。

    结构::

        output/键盘盲打/
        ├── 键盘盲打.md
        └── assets/page1_img1.png

    注意：只有在真正需要落地图片/资源时才会创建 ``assets`` 目录，
    避免纯文字转换留下一个空 ``assets`` 文件夹。
    """
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        target = (OUTPUT_DIR / base_name).resolve()
        if target.parent != OUTPUT_DIR.resolve():
            return None  # 防御性检查：必须直接落在 output/ 下

        # 确保目标子目录存在，再写 markdown
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{base_name}.md").write_text(markdown, encoding="utf-8")

        asset_dir = target / ocr_fallback.ASSET_DIR
        if assets:
            asset_dir.mkdir(parents=True, exist_ok=True)
            # 清掉上一次同名转换留下的图片，避免新旧混在一起
            for old in asset_dir.iterdir():
                if old.is_file():
                    old.unlink()
            for asset in assets:
                (asset_dir / asset["name"]).write_bytes(asset["data"])
        else:
            # 本次没有资源：如果上一次残留了一个空 assets 目录，顺手删掉，
            # 保持输出目录整洁；若目录非空（用户放了自己的文件）则保留不误伤。
            if asset_dir.exists() and asset_dir.is_dir():
                try:
                    asset_dir.rmdir()
                except OSError:
                    pass

        return str(target)
    except OSError:
        return None


def _embed_assets_in_markdown(
    markdown: str, assets: List[Dict[str, Any]]
) -> str:
    """把 Markdown 中引用的外部图片转成 base64 data URI 内嵌。

    支持两种引用写法：
    * Obsidian wikilink：``![[图片名]]``
    * 标准 Markdown：``![说明](assets/图片名)``

    转换后图片不再依赖外部 ``assets`` 目录，单个 ``.md`` 文件即可带走。
    """
    for asset in assets:
        name = asset["name"]
        mime = asset["mime"] or "application/octet-stream"
        data_uri = f"data:{mime};base64,{base64.b64encode(asset['data']).decode('ascii')}"

        # Obsidian wikilink：![[name]]
        wiki_pattern = re.compile(rf"!\[\[{re.escape(name)}\]\]")
        if wiki_pattern.search(markdown):
            markdown = wiki_pattern.sub(f"![{name}]({data_uri})", markdown)
            continue

        # 标准 Markdown：![alt](assets/name)
        md_pattern = re.compile(
            rf"!\[(.*?)\]\({re.escape(ocr_fallback.ASSET_DIR)}/{re.escape(name)}\)"
        )
        if md_pattern.search(markdown):
            markdown = md_pattern.sub(rf"![\1]({data_uri})", markdown)

    return markdown


def _store_conversion(
    base_name: str,
    markdown: str,
    assets: List[Dict[str, Any]],
    output_dir: Optional[str] = None,
) -> str:
    """把结果存进内存（图片要能被浏览器取到），返回可引用的 id。"""
    cid = uuid.uuid4().hex[:12]
    with _CONV_LOCK:
        _CONVERSIONS[cid] = {
            "name": base_name,
            "markdown": markdown,
            "assets": {a["name"]: a for a in assets},
            "output_dir": output_dir,
            "time": time.time(),
        }
        while len(_CONVERSIONS) > MAX_CONVERSIONS:
            _CONVERSIONS.popitem(last=False)
    return cid


def _get_conversion(cid: str) -> Optional[Dict[str, Any]]:
    with _CONV_LOCK:
        return _CONVERSIONS.get(cid)


def _build_zip(conv: Dict[str, Any]) -> bytes:
    """把 Markdown 和图片打包成 zip（解压后图片相对路径即可用）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{conv['name']}.md", conv["markdown"])
        for name, asset in conv["assets"].items():
            zf.writestr(f"{ocr_fallback.ASSET_DIR}/{name}", asset["data"])
    return buf.getvalue()


def _convert_single(
    data: bytes,
    filename: str,
    *,
    mimetype: Optional[str] = None,
    charset: Optional[str] = None,
    keep_data_uris: bool = False,
    use_plugins: bool = False,
    enable_ocr: bool = True,
    keep_images: bool = True,
    ocr_engine: str = "auto",
    action: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """把**单个文件**的字节转成 Markdown（markitdown 优先，扫描件再 OCR 兜底）。

    单文件转换（``/api/convert``）和批量转换（``/api/convert-batch``）共用这一套
    逻辑 —— 之前批量只走了 OCR，导致 HTML / docx / 带文字层的 PDF 这类文件
    被当成「OCR 候选之外的类型」直接跳过，产出空文件。
    """
    started = time.perf_counter()
    engine = get_engine(use_plugins)

    # 文件名无扩展名时，通过文件内容魔数/特征补一个「仅用于类型判断」的扩展名，
    # 避免 markitdown 和 OCR fallback 都因为认不出类型而把文件丢弃。
    judge_filename = filename
    sniff_mimetype: Optional[str] = None
    sniff_extension: Optional[str] = None
    if filename and not os.path.splitext(filename)[1] and data:
        sniff_mimetype, sniff_extension = _sniff_stream_info(data)
        if sniff_extension:
            judge_filename = filename + sniff_extension

    # 文本类文件显式带上编码：优先 UTF-8，绕开 charset_normalizer 对
    # 中文短文本（JSON/配置类）误判成 cp855 等单字节编码的坑。
    if charset is None and not _looks_binary(data):
        charset = _guess_text_charset(data)

    markitdown_error: Optional[str] = None
    result = None
    try:
        stream_info = StreamInfo(
            filename=judge_filename,
            extension=(os.path.splitext(judge_filename)[1] if judge_filename else None) or None,
            mimetype=mimetype or sniff_mimetype or None,
            charset=charset,
        )
        result = engine.convert_stream(
            io.BytesIO(data), stream_info=stream_info, keep_data_uris=keep_data_uris
        )
        markdown = result.text_content or ""
    except Exception as exc:  # noqa: BLE001
        # markitdown 内置转换器只认 .jpg/.jpeg/.png，
        # 遇到 webp / bmp / gif / tif 等图片会直接抛 UnsupportedFormatException。
        # 只要这个文件本来就在 OCR 候选范围内，就不算失败 —— 交给 OCR 兜底。
        can_fallback = enable_ocr and bool(judge_filename) and ocr_fallback.is_ocr_candidate(judge_filename)
        if not can_fallback:
            raise
        markitdown_error = f"{type(exc).__name__}: {exc}"
        result = None
        markdown = ""

    # ---- 扫描件兜底：标准转换器读不到文字层时，转用 OCR ----
    # 注意触发条件不是“结果为空”，而是“结果少得不正常”：
    # 有的扫描件 PDF 带一丁点文字层（实测一份 21 页手写笔记只提取出 14 个字符），
    # 若只在结果为空时才兜底，这种文件就会漏掉 OCR。
    MIN_USEFUL_CHARS = 80
    ocr_used = False
    ocr_tried = False
    assets: List[Dict[str, Any]] = []
    if (
        len(markdown.strip()) < MIN_USEFUL_CHARS
        and enable_ocr
        and bool(judge_filename)
        and ocr_fallback.is_ocr_candidate(judge_filename)
    ):
        ocr_tried = True
        ocr_text, assets = ocr_fallback.ocr_file(
            data, judge_filename, keep_images=keep_images, engine=ocr_engine, action=action, model=model
        )
        # OCR 结果明显更丰富时才替换，避免把正常的短文档越改越差
        if ocr_text and len(ocr_text.strip()) > len(markdown.strip()):
            markdown = ocr_text
            ocr_used = True

    # 用户选择「保留内嵌图片数据」时，把 OCR 导出的图片也内嵌到 Markdown 中，
    # 这样产出的 .md 文件可以单独带走，不需要 assets 目录。
    if keep_data_uris and assets:
        markdown = _embed_assets_in_markdown(markdown, assets)
        assets = []

    ocr_error = ocr_fallback.engine_info().get("cloud_error") if ocr_tried else None

    return {
        "markdown": markdown,
        "assets": assets,
        "title": getattr(result, "title", None),
        "ocr": ocr_used,
        "ocr_tried": ocr_tried,
        "ocr_engine": ocr_fallback.last_engine() if ocr_tried else None,
        "ocr_mode": ocr_fallback.normalize_mode(ocr_engine),
        "ocr_error": ocr_error,
        "markitdown_error": markitdown_error,
        "elapsed": round(time.perf_counter() - started, 3),
    }


def do_convert(
    *,
    data: Optional[bytes] = None,
    filename: Optional[str] = None,
    url: Optional[str] = None,
    extension: Optional[str] = None,
    mimetype: Optional[str] = None,
    charset: Optional[str] = None,
    keep_data_uris: bool = False,
    use_plugins: bool = False,
    enable_ocr: bool = True,
    keep_images: bool = True,
    ocr_engine: str = "auto",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """执行一次转换，返回统一结构的 dict。"""
    started = time.perf_counter()
    engine = get_engine(use_plugins)

    markitdown_error: Optional[str] = None
    try:
        try:
            if url:
                result = engine.convert_uri(url, keep_data_uris=keep_data_uris)
                source = url
            else:
                if data is None:
                    raise ValueError("没有收到任何内容，请选择文件或输入网址。")

                # 文本类文件显式带上编码（UTF-8 优先），避免 charset_normalizer
                # 把中文短文本误判成 cp855 等单字节编码
                if charset is None and not _looks_binary(data):
                    charset = _guess_text_charset(data)

                stream_info = StreamInfo(
                    filename=filename,
                    extension=extension or (os.path.splitext(filename)[1] if filename else None) or None,
                    mimetype=mimetype or None,
                    charset=charset or None,
                )
                result = engine.convert_stream(
                    io.BytesIO(data),
                    stream_info=stream_info,
                    keep_data_uris=keep_data_uris,
                )
                source = filename or "(stdin)"

            markdown = result.text_content or ""
        except Exception as exc:  # noqa: BLE001
            # markitdown 内置转换器只认 .jpg/.jpeg/.png，
            # 遇到 webp / bmp / gif / tif 等图片会直接抛 UnsupportedFormatException。
            # 只要这个文件本来就在 OCR 候选范围内，就不算失败 —— 交给下面的 OCR 兜底，
            # 否则这些格式的图片根本走不到 OCR，会直接报“格式不支持”。
            can_fallback = (
                enable_ocr
                and data is not None
                and bool(filename)
                and ocr_fallback.is_ocr_candidate(filename)
            )
            if not can_fallback:
                raise
            markitdown_error = f"{type(exc).__name__}: {exc}"
            result = None
            source = filename or "(stdin)"
            markdown = ""

        # ---- 扫描件兜底：标准转换器读不到文字层时，转用本地 OCR ----
        # keep_images=True 时走“混合模式”：文档里的插图原样导出成图片，
        # 只对插图之外的区域（例如手写笔迹）做 OCR。
        #
        # 注意触发条件不是“结果为空”，而是“结果少得不正常”：
        # 有的扫描件 PDF 带一丁点文字层（比如只有页眉/文件名水印，
        # 实测一份 21 页的手写笔记只提取出 "python语法+p18" 14 个字符），
        # 若只在结果为空时才兜底，这种文件就会漏掉 OCR，输出一句废话。
        MIN_USEFUL_CHARS = 80
        ocr_used = False
        ocr_tried = False
        assets: List[Dict[str, Any]] = []
        if (
            len(markdown.strip()) < MIN_USEFUL_CHARS
            and enable_ocr
            and data is not None
            and filename
            and ocr_fallback.is_ocr_candidate(filename)
        ):
            ocr_tried = True
            ocr_text, assets = ocr_fallback.ocr_file(
                data, filename, keep_images=keep_images, engine=ocr_engine, model=model
            )
            # OCR 结果明显更丰富时才替换，避免把正常的短文档越改越差
            if ocr_text and len(ocr_text.strip()) > len(markdown.strip()):
                markdown = ocr_text
                ocr_used = True

        # 用户选择「保留内嵌图片数据」时，把 OCR 导出的图片也内嵌到 Markdown 中，
        # 这样产出的 .md 文件可以单独带走，不需要 assets 目录。
        if keep_data_uris and assets:
            markdown = _embed_assets_in_markdown(markdown, assets)
            assets = []

        title = getattr(result, "title", None)
        stripped = markdown.strip()

        # 本次实际生效的 OCR 引擎（云端失败回落本地时会变成 "local"）
        ocr_engine_used = ocr_fallback.last_engine() if ocr_tried else None
        ocr_error = ocr_fallback.engine_info().get("cloud_error") if ocr_tried else None

        # ---- 落盘：转换结果自动写到 output/<名字>/ ----
        base_name = _base_name(filename, url)
        output_dir = (
            _write_output(base_name, markdown, assets) if (stripped or assets) else None
        )
        cid = _store_conversion(base_name, markdown, assets, output_dir)

        return {
            "ok": True,
            "markdown": markdown,
            "title": title,
            "source": source,
            "empty": len(stripped) == 0,
            "ocr": ocr_used,
            "ocr_tried": ocr_tried,
            "ocr_engine": ocr_engine_used,
            "ocr_mode": ocr_fallback.normalize_mode(ocr_engine),
            "ocr_error": ocr_error,
            "markitdown_error": markitdown_error,
            "assets": [
                {
                    "name": a["name"],
                    "mime": a["mime"],
                    "size": a["size"],
                    "url": f"/api/asset/{cid}/{urllib.parse.quote(a['name'])}",
                }
                for a in assets
            ],
            "cid": cid,
            "output_dir": output_dir,
            "download_url": f"/api/package/{cid}" if assets else None,
            "chars": len(markdown),
            "lines": markdown.count("\n") + (1 if markdown else 0),
            "elapsed": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "detail": traceback.format_exc(),
            "elapsed": round(time.perf_counter() - started, 3),
        }


def _parse_multipart(
    body: bytes, content_type: str
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """解析 multipart/form-data，返回 (文件列表, 普通字段 dict)。

    只依赖标准库，并且**手动解析头部**（不用 email 库），避免中文文件名
    被 email 模块按 latin-1 解码导致乱码。每个文件项含 ``name`` /
    ``filename`` / ``data``；普通字段放进 fields。
    """
    fields: Dict[str, Any] = {}
    files: List[Dict[str, Any]] = []

    if "boundary=" not in content_type:
        raise ValueError("不是 multipart/form-data")
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"')
    if not boundary:
        raise ValueError("缺少 boundary")

    delimiter = b"--" + boundary.encode("utf-8")
    parts = body.split(delimiter)
    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        # 找到头部与正文的分隔（空行，兼容 CRLF 与 LF）
        sep = part.find(b"\r\n\r\n")
        if sep == -1:
            sep = part.find(b"\n\n")
        if sep == -1:
            continue
        head_bytes = part[:sep]
        # 正文：剥掉结尾可能的 CRLF / 结束符 "--"
        data = part[sep + 4 :] if sep == part.find(b"\r\n\r\n") else part[sep + 2 :]
        data = data.rstrip(b"\r\n")

        head_text = head_bytes.decode("utf-8", "replace")
        if "content-disposition" not in head_text.lower():
            continue

        disp = _extract_disposition(head_bytes)
        name = disp.get("name")
        filename = disp.get("filename")
        # 每个 part 自带的 Content-Type（浏览器上传时会带，如 application/json）
        part_mimetype = None
        for line in head_text.splitlines():
            if line.strip().lower().startswith("content-type"):
                part_mimetype = line.split(":", 1)[1].strip().split(";")[0].strip().lower()
                break

        if filename:
            files.append(
                {"name": name, "filename": filename, "data": data, "mimetype": part_mimetype}
            )
        elif name is not None:
            fields[name] = data.decode("utf-8", "replace")
    return files, fields


def _extract_disposition(head_bytes: bytes) -> Dict[str, str]:
    """从一段 multipart 头部字节里提取 Content-Disposition 的 name/filename。

    手动按字节解析，filename 按 UTF-8 解码，避免 email 库的 latin-1 乱码问题。
    同时兼容 RFC 2047 的 ``filename*=UTF-8''...`` 与 RFC 5987 ``filename*=utf-8''...``。
    """
    result: Dict[str, str] = {}
    # 找到 Content-Disposition 行（可能被折行，这里按简单情况处理）
    text = head_bytes.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.strip().lower().startswith("content-disposition"):
            # 收集 value
            value = line.split(":", 1)[1] if ":" in line else line
            # 处理 filename*=UTF-8''... （RFC 2231/5987）
            m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;\r\n]+)", value)
            if m:
                from urllib.parse import unquote

                result["filename"] = unquote(m.group(1).strip().strip('"'))
                # 也可能同时有普通 filename，继续解析 name
            for token in value.split(";"):
                token = token.strip()
                if not token or "=" not in token:
                    continue
                k, v = token.split("=", 1)
                k = k.strip().lower()
                v = v.strip().strip('"')
                if k == "name":
                    result["name"] = v
                elif k == "filename" and "filename" not in result:
                    result["filename"] = v
            break
    return result


def _parse_disposition(value: str) -> List[Tuple[str, str]]:
    """解析 Content-Disposition 里的 key=value 对（含带引号的值）。"""
    result: List[Tuple[str, str]] = []
    for token in value.split(";"):
        token = token.strip()
        if "=" in token:
            k, v = token.split("=", 1)
            result.append((k.strip().lower(), v.strip().strip('"')))
    return result


def _write_assets(target_dir: Path, assets: List[Dict[str, Any]]) -> None:
    """把插图资源写进 ``<target_dir>/assets/``（已存在则跳过，不覆盖）。"""
    if not assets:
        return
    try:
        asset_dir = target_dir / ocr_fallback.ASSET_DIR
        asset_dir.mkdir(parents=True, exist_ok=True)
        for asset in assets:
            dest = asset_dir / asset["name"]
            if not dest.exists():  # 不同文件重名时保留先写入的那份
                dest.write_bytes(asset["data"])
    except OSError:
        pass


def do_convert_batch(
    files: List[Dict[str, Any]],
    *,
    enable_ocr: bool = True,
    keep_data_uris: bool = False,
    use_plugins: bool = False,
    keep_images: bool = False,
    ocr_engine: str = "auto",
    action: Optional[str] = None,
    title: Optional[str] = None,
    model: Optional[str] = None,
    separate: bool = False,
) -> Dict[str, Any]:
    """批量转换多个文件（HTML / docx / PDF / 图片都走同一条转换链）。

    ``separate=False``（默认）：把所有文件合并成一个 Markdown，写入
    ``output/<主名>/<主名>.md``。
    ``separate=True``：每个文件各自转换、各自保存成独立的
    ``output/<主名>/<各文件名>.md``，互不影响。

    注意：这里**不是**「只做 OCR」—— 每个文件都先按普通文件转换（markitdown），
    读不到文字层时才回落 OCR。之前的版本只调 ``ocr_file``，导致 HTML 这类
    非 OCR 候选类型被直接跳过，产出空文件。

    Args:
        files: ``[{"filename": str, "data": bytes}, ...]``，按文件名排序后处理。
        action: 覆盖腾讯云接口名（手写笔记可传 ``GeneralHandwritingOCR``）。
        title: 输出目录/文件主名（默认取第一个文件的 stem）。
        model: 仅对 vl 引擎生效，覆盖智谱模型（如 ``glm-4.5v``）。
        separate: True 时每个文件分别保存，而不是合并成一个。

    Returns:
        统一结构的 dict，含 ``markdown`` / ``output_dir`` / ``per_file`` 明细。
    """
    started = time.perf_counter()

    # 按文件名排序（IMG20261011... 这种带时间戳的名字天然就是时间序）
    ordered = sorted(files, key=lambda f: f.get("filename") or "")

    if title:
        base_name = _safe_dirname(title)
    elif ordered:
        base_name = _safe_dirname(Path(ordered[0]["filename"]).stem)
    else:
        base_name = "批量转换"

    parts: List[str] = []
    per_file: List[Dict[str, Any]] = []
    output_files: List[str] = []
    all_assets: List[Dict[str, Any]] = []
    ok_count = 0
    empty_count = 0
    fail_count = 0
    used_engine: Optional[str] = None

    # 分别保存模式：所有文件落到同一个 output/<主名>/ 目录，文件名 = 各文件的 stem
    separate_dir: Optional[Path] = None
    if separate:
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            separate_dir = (OUTPUT_DIR / base_name).resolve()
            if separate_dir.parent != OUTPUT_DIR.resolve():
                separate_dir = None  # 防御：必须直接落在 output/ 下
            else:
                separate_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            separate_dir = None

    for idx, item in enumerate(ordered, 1):
        filename = item.get("filename") or f"图片{idx}"
        data = item.get("data") or b""

        # 与单文件转换完全同一条链：markitdown 优先，扫描件再 OCR 兜底
        try:
            info = _convert_single(
                data,
                filename,
                mimetype=item.get("mimetype"),
                keep_data_uris=keep_data_uris,
                use_plugins=use_plugins,
                enable_ocr=enable_ocr,
                keep_images=keep_images,
                ocr_engine=ocr_engine,
                action=action,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001
            fail_count += 1
            per_file.append(
                {
                    "index": idx,
                    "filename": filename,
                    "engine": None,
                    "chars": 0,
                    "empty": True,
                    "preview": "",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if separate:
                stem = _safe_dirname(Path(filename).stem) or f"图片{idx}"
                if separate_dir is not None:
                    try:
                        (separate_dir / f"{stem}.md").write_text(
                            f"> （转换失败：{type(exc).__name__}: {exc}）", encoding="utf-8"
                        )
                        output_files.append(str(separate_dir / f"{stem}.md"))
                    except OSError:
                        pass
            else:
                parts.append(f"## {Path(filename).stem}\n\n> （转换失败：{type(exc).__name__}: {exc}）")
            continue

        text = (info["markdown"] or "").strip()
        eng = info.get("ocr_engine") or (
            ocr_fallback.last_engine() if info.get("ocr_tried") else None
        )
        if eng:
            used_engine = eng

        if text:
            ok_count += 1
        else:
            empty_count += 1

        per_file.append(
            {
                "index": idx,
                "filename": filename,
                "engine": eng,
                "chars": len(text),
                "empty": not text,
                "preview": text[:120],
                "error": info.get("ocr_error") or info.get("markitdown_error"),
            }
        )

        # 插图资源：分别保存时所有文件共用 output/<主名>/assets/（文件名带文档前缀，不冲突）
        if info.get("assets"):
            all_assets.extend(info["assets"])
            if separate and separate_dir is not None:
                _write_assets(separate_dir, info["assets"])

        if separate:
            stem = _safe_dirname(Path(filename).stem) or f"图片{idx}"
            body = text if text else "> （没有提取到文字）"
            if separate_dir is not None:
                try:
                    (separate_dir / f"{stem}.md").write_text(body, encoding="utf-8")
                    output_files.append(str(separate_dir / f"{stem}.md"))
                except OSError:
                    pass  # 写失败不影响其它文件
        else:
            # 即使某个文件没内容，也保留一个占位标题，保证顺序不丢
            heading = f"## {Path(filename).stem}"
            parts.append(f"{heading}\n\n{text}" if text else f"{heading}\n\n> （没有提取到文字）")

    if separate:
        # 汇总文本仅用于前端预览，不落盘；真实结果是 separate_dir 下的各 .md
        summary = "\n\n".join(
            f"## {f['filename']}\n\n" + (f["preview"] if f["preview"] else "> （没有提取到文字）")
            for f in per_file
        )
        markdown = f"# {base_name}（分别保存 {len(output_files)} 个文件）\n\n" + summary
        output_dir = str(separate_dir) if separate_dir is not None else None
        cid = _store_conversion(base_name, markdown, all_assets, output_dir)
    else:
        markdown = f"# {base_name}\n\n" + "\n\n".join(parts)
        output_dir = _write_output(base_name, markdown, all_assets)
        cid = _store_conversion(base_name, markdown, all_assets, output_dir)

    return {
        "ok": True,
        "markdown": markdown,
        "empty": ok_count == 0,
        "ocr": ok_count > 0,
        "ocr_engine": used_engine,
        "ocr_mode": ocr_fallback.normalize_mode(ocr_engine),
        "total": len(ordered),
        "ok_count": ok_count,
        "empty_count": empty_count,
        "fail_count": fail_count,
        "per_file": per_file,
        "separate": separate,
        "output_files": output_files,
        "ocr_error": ocr_fallback.engine_info().get("cloud_error"),
        "cid": cid,
        "output_dir": output_dir,
        "download_url": f"/api/package/{cid}" if cid else None,
        "chars": len(markdown),
        "lines": markdown.count("\n") + 1,
        "elapsed": round(time.perf_counter() - started, 3),
    }


def _open_in_file_manager(path: str) -> None:
    """在系统文件管理器里打开该目录（Windows 用 os.startfile）。"""
    if hasattr(os, "startfile"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class Handler(BaseHTTPRequestHandler):
    server_version = "MarkItDownWebUI/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础工具 ----------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_raw(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # 精简日志
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------------- GET ----------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            if not INDEX_FILE.is_file():
                self._send(500, b"index.html not found", "text/plain; charset=utf-8")
                return
            self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
            return

        # 转换时导出的插图（供预览直接引用）
        if path.startswith("/api/asset/"):
            rest = path[len("/api/asset/") :]
            cid, _, raw_name = rest.partition("/")
            conv = _get_conversion(cid)
            name = urllib.parse.unquote(raw_name)
            if not conv or name not in conv["assets"]:
                self._send(404, b"Not Found", "text/plain; charset=utf-8")
                return
            asset = conv["assets"][name]
            self._send_raw(200, asset["data"], asset["mime"], cache="private, max-age=600")
            return

        # 在资源管理器里打开本次转换的输出目录
        if path.startswith("/api/reveal/"):
            conv = _get_conversion(path[len("/api/reveal/") :])
            target = (conv or {}).get("output_dir")
            real = Path(target).resolve() if target else None
            # 只允许打开本服务自己写在 output/ 下的目录
            if not real or not real.is_dir() or real.parent != OUTPUT_DIR.resolve():
                self._send_json({"ok": False, "error": "输出目录不存在"}, 404)
                return
            try:
                _open_in_file_manager(str(real))
                self._send_json({"ok": True, "dir": str(real)})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        # 打包下载：Markdown + assets/ 目录
        if path.startswith("/api/package/"):
            conv = _get_conversion(path[len("/api/package/") :])
            if not conv:
                self._send(404, b"Not Found", "text/plain; charset=utf-8")
                return
            blob = _build_zip(conv)
            filename = urllib.parse.quote(f"{conv['name']}.zip")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header(
                "Content-Disposition", f"attachment; filename*=UTF-8''{filename}"
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(blob)
            return

        if path == "/api/info":
            self._send_json(
                {
                    "ok": True,
                    "markitdown": MARKITDOWN_VERSION,
                    "python": sys.version.split()[0],
                    "ocr": ocr_fallback.ocr_installed(),
                    "ocr_engine": ocr_fallback.active_engine(),
                    "ocr_status": ocr_fallback.ocr_status(),
                    "ocr_info": ocr_fallback.engine_info(),
                    "cwd": os.getcwd(),
                }
            )
            return

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        self._send(404, b"Not Found", "text/plain; charset=utf-8")

    # ---------------- HEAD ----------------
    def do_HEAD(self) -> None:  # noqa: N802
        """支持 HEAD（只回响应头，不回正文）。

        浏览器、预览面板、各类探活脚本都会先发 ``HEAD`` 做健康检查。
        BaseHTTPRequestHandler 按 ``do_<方法名>`` 派发，没有 do_HEAD 就会
        自动回 501 Unsupported method —— 探活方会据此判定「服务不可用」并
        断开/关闭页面（表现为页面用着用着自己关掉）。直接复用 do_GET 即可，
        因为 _send / _send_raw 已经对 HEAD 做了「不写正文」的兜底。
        """
        self.do_GET()

    # ---------------- POST ----------------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/convert-batch":
            self._handle_batch()
            return
        if parsed.path != "/api/convert":
            self._send_json({"ok": False, "error": "Not Found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""

        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

        try:
            if ctype == "application/json":
                payload = json.loads(raw.decode("utf-8") or "{}")
                result = do_convert(
                    url=(payload.get("url") or "").strip() or None,
                    keep_data_uris=bool(payload.get("keep_data_uris")),
                    use_plugins=bool(payload.get("use_plugins")),
                    enable_ocr=bool(payload.get("enable_ocr", True)),
                    keep_images=bool(payload.get("keep_images", True)),
                    ocr_engine=payload.get("ocr_engine") or "auto",
                    model=payload.get("model") or None,
                    extension=payload.get("extension") or None,
                    mimetype=payload.get("mimetype") or None,
                    charset=payload.get("charset") or None,
                )
            else:
                # 原始二进制：文件名等元信息放在自定义请求头里
                options_header = self.headers.get("X-Options") or "{}"
                try:
                    options = json.loads(options_header)
                except json.JSONDecodeError:
                    options = {}

                filename = None
                raw_name = self.headers.get("X-Filename")
                if raw_name:
                    filename = urllib.parse.unquote(raw_name)

                result = do_convert(
                    data=raw,
                    filename=filename,
                    extension=options.get("extension") or None,
                    keep_data_uris=bool(options.get("keep_data_uris")),
                    use_plugins=bool(options.get("use_plugins")),
                    enable_ocr=bool(options.get("enable_ocr", True)),
                    keep_images=bool(options.get("keep_images", True)),
                    ocr_engine=options.get("ocr_engine") or "auto",
                    model=options.get("model") or None,
                )
        except Exception as exc:  # noqa: BLE001
            result = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "detail": traceback.format_exc(),
            }

        self._send_json(result)

    def _handle_batch(self) -> None:  # noqa: N802
        """处理 /api/convert-batch：multipart/form-data 多文件批量 OCR。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type") or ""

        try:
            files, fields = _parse_multipart(raw, content_type)
        except Exception as exc:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": f"解析上传失败：{type(exc).__name__}: {exc}"}
            )
            return

        if not files:
            self._send_json({"ok": False, "error": "没有收到任何文件"})
            return

        action = fields.get("action") or None
        ocr_engine = fields.get("ocr_engine") or "auto"
        # 「智谱 GLM」模式自己决定识别方式，不套用腾讯云手写体接口名
        if not action and ocr_engine != "vl":
            action = "GeneralHandwritingOCR"
        result = do_convert_batch(
            files,
            enable_ocr=fields.get("enable_ocr", "true").lower() != "false",
            keep_data_uris=fields.get("keep_data_uris", "false").lower() == "true",
            use_plugins=fields.get("use_plugins", "false").lower() == "true",
            keep_images=fields.get("keep_images", "false").lower() == "true",
            ocr_engine=ocr_engine,
            action=action,
            title=fields.get("title") or None,
            model=fields.get("model") or None,
            separate=fields.get("separate", "false").lower() == "true",
        )
        self._send_json(result)


def find_free_port(host: str, preferred: int) -> int:
    # 注意：这里刻意不设置 SO_REUSEADDR。
    # 在 Windows 上 SO_REUSEADDR 允许两个进程绑定同一端口，
    # 会让探测误判“端口可用”，从而出现新旧实例同时监听的情况。
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    # 同上：禁用地址复用，避免端口被重复绑定后请求被旧进程抢走
    allow_reuse_address = False


def main() -> None:
    parser = argparse.ArgumentParser(description="MarkItDown 本地 Web 界面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_DIR),
        help="转换结果的输出目录（默认项目根目录 output/，可用环境变量 MARKITDOWN_OUTPUT_DIR 覆盖）",
    )
    args = parser.parse_args()

    global OUTPUT_DIR
    OUTPUT_DIR = Path(args.output).expanduser().resolve()

    port = find_free_port(args.host, args.port)
    url = f"http://{args.host}:{port}/"

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"警告：无法创建输出目录 {OUTPUT_DIR}（{exc}），转换结果将不会落盘。")

    httpd = WebServer((args.host, port), Handler)

    print("=" * 58)
    print("  MarkItDown 本地转换界面已启动")
    print(f"  markitdown 版本 : {MARKITDOWN_VERSION}")
    print(f"  Python          : {sys.version.split()[0]}")
    print(f"  访问地址        : {url}")
    print(f"  输出目录        : {OUTPUT_DIR}")
    print("  停止服务        : 在此窗口按 Ctrl+C")
    print("=" * 58)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
