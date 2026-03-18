#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import subprocess
import sys
import tempfile
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "wechat.local.json"
DEFAULT_TIMEOUT = 30
VOID_TAGS = {"br", "hr", "img"}


def resolve_repo_path(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def resolve_existing_dir(path_value: str | None, default_relative: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if path_value:
        resolved = resolve_repo_path(path_value)
        if resolved is not None:
            candidates.append(resolved)
    if default_relative:
        candidates.append(ROOT / default_relative)
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


class SyncError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync a published article to the WeChat draft box.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="Published article URL. Optional when --slug is provided.",
    )
    parser.add_argument(
        "--slug",
        help="Article slug, resolved as <site_url>/blog/<slug>/.",
    )
    parser.add_argument(
        "--thumb",
        help="Cover image path or URL. Required when no usable article image is found.",
    )
    parser.add_argument(
        "--author",
        help="Override article author shown in WeChat.",
    )
    parser.add_argument(
        "--config",
        help="Path to config JSON. Defaults to config/wechat.local.json.",
    )
    parser.add_argument(
        "--update-media-id",
        help="Update an existing draft instead of creating a new one.",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=0,
        help="Article index in a multi-article draft. Defaults to 0.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="After creating or updating a draft, submit it for publish.",
    )
    parser.add_argument(
        "--publish-existing-media-id",
        help="Publish an existing draft media_id directly without rebuilding article content.",
    )
    parser.add_argument(
        "--mode",
        choices=("draft", "sendall"),
        default="draft",
        help="draft: only create draft; sendall: create draft and send to all or a tag.",
    )
    parser.add_argument(
        "--tag-id",
        type=int,
        help="Optional fan tag id for sendall mode. Defaults to all followers.",
    )
    parser.add_argument(
        "--send-ignore-reprint",
        type=int,
        choices=(0, 1),
        default=0,
        help="sendall mode only. Whether to continue when originality check flags reprint.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build payload only and skip WeChat API calls.",
    )
    parser.add_argument(
        "--output",
        help="Write the generated payload JSON to this path.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.index < 0:
        raise SyncError("--index must be >= 0.")
    if args.mode == "sendall" and args.publish:
        raise SyncError("--publish and --mode sendall cannot be used together.")
    if args.publish_existing_media_id:
        if args.mode != "draft":
            raise SyncError("--publish-existing-media-id cannot be combined with --mode sendall.")
        if args.update_media_id:
            raise SyncError("--publish-existing-media-id cannot be combined with --update-media-id.")
        if args.url or args.slug:
            raise SyncError("--publish-existing-media-id does not need a URL or --slug.")
        if args.thumb or args.author:
            raise SyncError("--publish-existing-media-id cannot be combined with content override flags.")
    elif not (args.url or args.slug):
        raise SyncError("Provide either an article URL or --slug.")


def load_config(config_path_arg: str | None) -> dict:
    config_path = Path(config_path_arg) if config_path_arg else CONFIG_PATH
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    if not config_path.exists():
        raise SyncError(
            f"Missing config: {config_path}. Copy config/wechat.example.json to wechat.local.json first."
        )
    with config_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if "wechat" not in data:
        raise SyncError("Config file must contain a top-level 'wechat' object.")
    return data["wechat"]


def find_article_source_by_slug(slug: str, content_root: Path | None) -> Path | None:
    if content_root is None or not content_root.exists():
        return None
    for path in content_root.rglob("*"):
        if path.suffix not in {".md", ".mdx"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(rf'(?m)^slug:\s*"{re.escape(slug)}"\s*$', text):
            return path
    return None


def upsert_frontmatter_field(path: Path, key: str, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise SyncError(f"File does not start with YAML frontmatter: {path}")
    closing = text.find("\n---\n", 4)
    if closing == -1:
        raise SyncError(f"Could not find closing YAML frontmatter marker in {path}")
    frontmatter = text[4:closing]
    body = text[closing + 5 :]
    field_pattern = re.compile(rf"(?m)^{re.escape(key)}:\s*.*$")
    new_line = f'{key}: "{value}"'
    if field_pattern.search(frontmatter):
        frontmatter = field_pattern.sub(new_line, frontmatter, count=1)
    else:
        frontmatter = f"{frontmatter}\n{new_line}"
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


def get_frontmatter_field(path: Path, key: str) -> str | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    closing = text.find("\n---\n", 4)
    if closing == -1:
        return None
    frontmatter = text[4:closing]
    match = re.search(rf'(?m)^{re.escape(key)}:\s*"(.*)"\s*$', frontmatter)
    if match:
        return match.group(1)
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", frontmatter)
    if match:
        return match.group(1).strip()
    return None


def resolve_article_url(args: argparse.Namespace, config: dict) -> str:
    if args.url:
        if args.url.startswith(("http://", "https://")):
            return args.url
        return urljoin(config["site_url"].rstrip("/") + "/", args.url.lstrip("/"))
    if args.slug:
        slug = args.slug.strip("/")
        return urljoin(config["site_url"].rstrip("/") + "/", f"blog/{slug}/")
    raise SyncError("Provide either an article URL or --slug.")


def request_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "astro-to-wechat/1.0"})
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise SyncError(f"Request failed for {url}: HTTP {exc.code}") from exc
    except URLError as exc:
        raise SyncError(f"Request failed for {url}: {exc.reason}") from exc


def request_bytes(url: str) -> tuple[bytes, str | None]:
    req = Request(url, headers={"User-Agent": "astro-to-wechat/1.0"})
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return resp.read(), resp.headers.get_content_type()
    except HTTPError as exc:
        raise SyncError(f"Request failed for {url}: HTTP {exc.code}") from exc
    except URLError as exc:
        raise SyncError(f"Request failed for {url}: {exc.reason}") from exc


def request_json(url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    body = None
    request_headers = {"User-Agent": "astro-to-wechat/1.0"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    req = Request(url, data=body, headers=request_headers)
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            data = json.loads(resp.read().decode(charset))
    except HTTPError as exc:
        raise SyncError(f"WeChat request failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise SyncError(f"WeChat request failed: {exc.reason}") from exc
    if data.get("errcode") not in (None, 0):
        raise SyncError(f"WeChat API error {data['errcode']}: {data.get('errmsg', 'unknown error')}")
    return data


def encode_multipart(field_name: str, filename: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = f"----AstroToWeChatBoundary{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8"),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def post_multipart(url: str, field_name: str, filename: str, data: bytes, content_type: str) -> dict:
    body, header = encode_multipart(field_name, filename, data, content_type)
    req = Request(
        url,
        data=body,
        headers={
            "User-Agent": "astro-to-wechat/1.0",
            "Content-Type": header,
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            result = json.loads(resp.read().decode(charset))
    except HTTPError as exc:
        raise SyncError(f"WeChat upload failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise SyncError(f"WeChat upload failed: {exc.reason}") from exc
    if result.get("errcode") not in (None, 0):
        raise SyncError(f"WeChat API error {result['errcode']}: {result.get('errmsg', 'unknown error')}")
    return result


class HeadMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        attr_map = {key: value or "" for key, value in attrs}
        content = attr_map.get("content", "")
        for key in ("property", "name"):
            name = attr_map.get(key, "").strip().lower()
            if name and content:
                self.meta[name] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


class ArticleHtmlExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._in_prose = False
        self._stack: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        class_names = set(attr_map.get("class", "").split())
        if not self._in_prose and tag == "div" and "prose" in class_names:
            self._in_prose = True
            return
        if not self._in_prose:
            return
        if self._skip_depth > 0:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return
        if tag == "div" and "title" in class_names and not self._stack:
            self._skip_depth = 1
            return
        self.parts.append(render_start_tag(tag, attrs))
        if tag not in VOID_TAGS:
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        class_names = set(attr_map.get("class", "").split())
        if not self._in_prose:
            return
        if self._skip_depth > 0:
            return
        if tag == "div" and "title" in class_names and not self._stack:
            return
        self.parts.append(render_start_tag(tag, attrs, self_closing=True))

    def handle_endtag(self, tag: str) -> None:
        if not self._in_prose:
            return
        if self._skip_depth > 0:
            if tag not in VOID_TAGS:
                self._skip_depth -= 1
            return
        if not self._stack:
            self._in_prose = False
            return
        self._stack.pop()
        if tag not in VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._in_prose and self._skip_depth == 0:
            self.parts.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        if self._in_prose and self._skip_depth == 0:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._in_prose and self._skip_depth == 0:
            self.parts.append(f"&#{name};")


class ImageCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        src = dict(attrs).get("src")
        if src:
            self.sources.append(src)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


class FirstParagraphParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_p = False
        self._done = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        if tag == "p":
            self._in_p = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._in_p:
            self._in_p = False
            self._done = True

    def handle_data(self, data: str) -> None:
        if self._in_p and not self._done:
            self.parts.append(data)


class WeChatHtmlRewriter(HTMLParser):
    allowed_tags = {
        "a",
        "blockquote",
        "br",
        "code",
        "em",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }

    def __init__(self, base_url: str, rewrite_image: Callable[[str], str]) -> None:
        super().__init__(convert_charrefs=False)
        self.base_url = base_url
        self.rewrite_image = rewrite_image
        self.parts: list[str] = []
        self.tag_stack: list[str] = []
        self.link_ref_stack: list[int | None] = []
        self.external_link_refs: dict[str, int] = {}
        self.external_link_order: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.allowed_tags:
            return
        if tag == "a":
            self.link_ref_stack.append(self._register_link_ref(attrs))
        attr_text = self._render_attrs(tag, attrs)
        self.parts.append(f"<{tag}{attr_text}>")
        if tag not in VOID_TAGS:
            self.tag_stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.allowed_tags:
            return
        attr_text = self._render_attrs(tag, attrs)
        self.parts.append(f"<{tag}{attr_text} />")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.allowed_tags and tag not in VOID_TAGS:
            if tag in self.tag_stack:
                self.tag_stack.pop(len(self.tag_stack) - 1 - self.tag_stack[::-1].index(tag))
            self.parts.append(f"</{tag}>")
            if tag == "a" and self.link_ref_stack:
                ref_index = self.link_ref_stack.pop()
                if ref_index is not None:
                    self.parts.append(
                        f'<code style="font-family: Menlo, Consolas, monospace; font-size: 0.78em; '
                        f'background: transparent; color: #576b95; padding: 0; border: none;">[{ref_index}]</code>'
                    )

    def handle_data(self, data: str) -> None:
        # Ignore formatting whitespace between block tags; otherwise WeChat
        # renders it as visible blank lines in lists and other structures.
        if not data.strip():
            if any(tag in {"pre", "code"} for tag in self.tag_stack):
                self.parts.append(data)
            return
        self.parts.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def _render_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        attr_map = {key: value or "" for key, value in attrs}
        cleaned: list[tuple[str, str]] = []
        if tag == "a" and attr_map.get("href"):
            cleaned.append(("href", absolutize_url(attr_map["href"], self.base_url)))
            cleaned.append(
                (
                    "style",
                    "color: #576b95; text-decoration: none; border-bottom: 1px solid #c7d2fe; "
                    "word-break: break-all;",
                )
            )
        elif tag == "img" and attr_map.get("src"):
            cleaned.append(("src", self.rewrite_image(attr_map["src"])))
            alt = attr_map.get("alt", "").strip()
            if alt:
                cleaned.append(("alt", alt))
            cleaned.append(
                (
                    "style",
                    "display: block; max-width: 100%; height: auto; margin: 1.1em auto 0.35em; "
                    "border-radius: 6px;",
                )
            )
        elif tag == "p":
            cleaned.append(
                (
                    "style",
                    "margin: 1em 0; line-height: 1.85; color: #1f2937; text-align: justify; "
                    "word-break: break-word;",
                )
            )
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading_styles = {
                "h1": "margin: 1.6em 0 0.75em; font-size: 1.5em; font-weight: 700; line-height: 1.45; color: #111827;",
                "h2": "margin: 1.5em 0 0.7em; padding-left: 0.55em; border-left: 4px solid #111827; font-size: 1.28em; font-weight: 700; line-height: 1.45; color: #111827;",
                "h3": "margin: 1.35em 0 0.6em; font-size: 1.14em; font-weight: 700; line-height: 1.5; color: #111827;",
                "h4": "margin: 1.2em 0 0.55em; font-size: 1.05em; font-weight: 700; line-height: 1.55; color: #111827;",
                "h5": "margin: 1.1em 0 0.5em; font-size: 1em; font-weight: 700; line-height: 1.55; color: #111827;",
                "h6": "margin: 1.05em 0 0.45em; font-size: 0.95em; font-weight: 700; line-height: 1.55; color: #374151;",
            }
            cleaned.append(("style", heading_styles[tag]))
        elif tag == "pre":
            cleaned.append(
                (
                    "style",
                    "margin: 1.15em 0; padding: 14px 16px; overflow-x: auto; "
                    "background: #f6f8fa; color: #1f2937; border: 1px solid #e5e7eb; border-radius: 10px; "
                    "font-size: 13px; line-height: 1.75; white-space: pre-wrap; word-break: break-word; "
                    "font-family: Menlo, Consolas, monospace;",
                )
            )
        elif tag == "code":
            if "pre" in self.tag_stack:
                cleaned.append(
                    (
                        "style",
                        "font-family: Menlo, Consolas, monospace; background: transparent; color: inherit; "
                        "padding: 0; border: none; border-radius: 0; font-size: inherit;",
                    )
                )
            else:
                cleaned.append(
                    (
                        "style",
                        "display: inline-block; vertical-align: baseline; line-height: 1.35; "
                        "font-family: Menlo, Consolas, monospace; font-size: 0.9em; font-weight: 500; "
                        "background: #f6f8fa; color: #334155; padding: 0.04em 0.32em; "
                        "border: 1px solid #e5e7eb; border-radius: 4px;",
                    )
                )
        elif tag == "blockquote":
            cleaned.append(
                (
                    "style",
                    "margin: 1.15em 0; padding: 0.85em 1em; color: #4b5563; "
                    "background: #f9fafb; border-left: 3px solid #d1d5db; border-radius: 0 6px 6px 0;",
                )
            )
        elif tag in {"ul", "ol"}:
            cleaned.append(
                (
                    "style",
                    "margin: 0.9em 0 0.9em 1.35em; padding: 0; color: #1f2937; "
                    "line-height: 1.85;",
                )
            )
        elif tag == "li":
            cleaned.append(("style", "margin: 0.42em 0; line-height: 1.85;"))
        elif tag == "strong":
            cleaned.append(("style", "font-weight: 700; color: #111827;"))
        elif tag == "em":
            cleaned.append(("style", "font-style: italic; color: #374151;"))
        elif tag == "figcaption":
            cleaned.append(
                (
                    "style",
                    "margin-top: 0.35em; color: #6b7280; text-align: center; font-size: 0.9em; line-height: 1.7;",
                )
            )
        elif tag == "figure":
            cleaned.append(("style", "margin: 1.2em 0;"))
        elif tag == "hr":
            cleaned.append(("style", "margin: 1.8em auto; border: none; border-top: 1px solid #e5e7eb; width: 42%;"))
        elif tag == "table":
            cleaned.append(
                (
                    "style",
                    "width: 100%; margin: 1.15em 0; border-collapse: collapse; font-size: 0.92em; "
                    "color: #1f2937; table-layout: fixed; word-break: break-word;",
                )
            )
        elif tag == "th":
            cleaned.append(("style", "border: 1px solid #d1d5db; padding: 8px 10px; text-align: left; background: #f9fafb; font-weight: 700;"))
        elif tag == "td":
            cleaned.append(("style", "border: 1px solid #e5e7eb; padding: 8px 10px; text-align: left;"))
        return "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in cleaned)

    def _register_link_ref(self, attrs: list[tuple[str, str | None]]) -> int | None:
        attr_map = {key: value or "" for key, value in attrs}
        href = attr_map.get("href", "").strip()
        if not href:
            return None
        absolute_href = absolutize_url(href, self.base_url)
        parsed = urlparse(absolute_href)
        if parsed.scheme not in {"http", "https"}:
            return None
        host = parsed.netloc.lower()
        if host.endswith("weixin.qq.com"):
            return None
        if absolute_href not in self.external_link_refs:
            self.external_link_order.append(absolute_href)
            self.external_link_refs[absolute_href] = len(self.external_link_order)
        return self.external_link_refs[absolute_href]

    def render_output(self) -> str:
        output = "".join(self.parts).strip()
        if not self.external_link_order:
            return output
        references = [
            '<hr style="margin: 1.8em auto; border: none; border-top: 1px solid #e5e7eb; width: 42%;" />',
            '<h3 style="margin: 1.35em 0 0.6em; font-size: 1.14em; font-weight: 700; line-height: 1.5; color: #111827;">参考链接</h3>',
            '<ol style="margin: 0.9em 0 0.9em 1.35em; padding: 0; color: #1f2937; line-height: 1.85;">',
        ]
        for href in self.external_link_order:
            references.append(
                '<li style="margin: 0.42em 0; line-height: 1.85;">'
                f'<a href="{html.escape(href, quote=True)}" '
                'style="color: #576b95; text-decoration: none; border-bottom: 1px solid #c7d2fe; word-break: break-all;">'
                f"{html.escape(href)}"
                "</a></li>"
            )
        references.append("</ol>")
        return output + "".join(references)


def render_start_tag(tag: str, attrs: list[tuple[str, str | None]], self_closing: bool = False) -> str:
    attr_text = "".join(
        f' {key}="{html.escape(value or "", quote=True)}"' for key, value in attrs
    )
    suffix = " />" if self_closing or tag in VOID_TAGS else ">"
    return f"<{tag}{attr_text}{suffix}"


def absolutize_url(value: str, base_url: str) -> str:
    return urljoin(base_url, value)


def parse_head_meta(html_text: str) -> dict:
    parser = HeadMetaParser()
    parser.feed(html_text)
    title = parser.meta.get("og:title") or parser.title.strip()
    description = parser.meta.get("description") or parser.meta.get("og:description", "")
    hero_image = parser.meta.get("og:image", "")
    return {
        "title": html.unescape(title).strip(),
        "description": html.unescape(description).strip(),
        "hero_image": html.unescape(hero_image).strip(),
    }


def extract_article_html(html_text: str) -> str:
    parser = ArticleHtmlExtractor()
    parser.feed(html_text)
    body = "".join(parser.parts).strip()
    if not body:
        raise SyncError("Could not locate article body in page HTML.")
    return body


def collect_image_sources(body_html: str) -> list[str]:
    parser = ImageCollector()
    parser.feed(body_html)
    return parser.sources


def infer_digest(meta_description: str, body_html: str) -> str:
    if meta_description:
        return meta_description.strip()
    parser = FirstParagraphParser()
    parser.feed(body_html)
    text = " ".join(part.strip() for part in parser.parts if part.strip())
    return text[:120].strip()


def read_local_asset(path_str: str, public_root: Path | None) -> tuple[bytes, str | None]:
    raw_path = Path(path_str).expanduser()
    candidates = [raw_path]
    resolved = resolve_repo_path(path_str)
    if resolved is not None:
        candidates.append(resolved)
    if public_root is not None:
        candidates.append(public_root / path_str.lstrip("/"))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.read_bytes(), mimetypes.guess_type(candidate.name)[0]
    raise SyncError(f"Local file not found: {path_str}")


def load_asset_bytes(source: str, base_url: str, public_root: Path | None) -> tuple[bytes, str | None, str]:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        data, content_type = request_bytes(source)
        return data, content_type, source
    if source.startswith("/"):
        local_candidate = public_root / source.lstrip("/") if public_root is not None else None
        if local_candidate is not None and local_candidate.exists():
            data = local_candidate.read_bytes()
            return data, mimetypes.guess_type(local_candidate.name)[0], local_candidate.name
        absolute_url = absolutize_url(source, base_url)
        data, content_type = request_bytes(absolute_url)
        return data, content_type, absolute_url
    data, content_type = read_local_asset(source, public_root)
    return data, content_type, source


def convert_svg_to_png(data: bytes, source_name: str, node_modules_root: Path | None) -> tuple[bytes, str, str]:
    with tempfile.TemporaryDirectory(prefix="wechat-thumb-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        svg_path = tmp_path / "source.svg"
        png_path = tmp_path / "converted.png"
        svg_path.write_bytes(data)
        script = (
            "import sharp from 'sharp';"
            "const [inputPath, outputPath] = process.argv.slice(1);"
            "await sharp(inputPath).png().toFile(outputPath);"
        )
        try:
            subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    script,
                    str(svg_path),
                    str(png_path),
                ],
                cwd=str(node_modules_root or ROOT),
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise SyncError("Node.js is required to convert SVG cover images to PNG. Install Node.js and run `npm install` in this repository.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() or exc.stdout.strip() or "unknown error"
            raise SyncError(
                f"Failed to convert SVG cover image to PNG with sharp: {source_name}: {stderr}. "
                "Make sure the `sharp` package is installed in this repository."
            ) from exc
        return png_path.read_bytes(), "image/png", f"{source_name}.png"


def ensure_supported_image(content_type: str | None, source_name: str) -> tuple[str, str]:
    guessed = content_type or mimetypes.guess_type(source_name)[0] or "application/octet-stream"
    allowed = {
        "image/bmp": ".bmp",
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/png": ".png",
    }
    if guessed not in allowed:
        raise SyncError(
            f"Unsupported image format for WeChat: {source_name} ({guessed}). Use bmp/png/jpeg/jpg/gif."
        )
    return guessed, allowed[guessed]


def load_wechat_upload_asset(
    source: str,
    base_url: str,
    *,
    public_root: Path | None,
    node_modules_root: Path | None,
    allow_svg_conversion: bool,
) -> tuple[bytes, str, str]:
    data, content_type, source_name = load_asset_bytes(source, base_url, public_root)
    guessed = content_type or mimetypes.guess_type(source_name)[0] or "application/octet-stream"
    if guessed == "image/svg+xml":
        if allow_svg_conversion:
            return convert_svg_to_png(data, source_name, node_modules_root)
        raise SyncError(
            f"Unsupported image format for WeChat: {source_name} ({guessed})."
        )
    mime_type, ext = ensure_supported_image(guessed, source_name)
    return data, mime_type, f"{uuid.uuid4().hex}{ext}"


def get_access_token(config: dict) -> str:
    url = (
        "https://api.weixin.qq.com/cgi-bin/token?"
        + urlencode(
            {
                "grant_type": "client_credential",
                "appid": config["app_id"],
                "secret": config["app_secret"],
            }
        )
    )
    data = request_json(url)
    token = data.get("access_token")
    if not token:
        raise SyncError("WeChat did not return access_token.")
    return token


def upload_inline_image(
    access_token: str,
    source: str,
    base_url: str,
    public_root: Path | None,
    node_modules_root: Path | None,
) -> str:
    data, mime_type, generated_name = load_wechat_upload_asset(
        source,
        base_url,
        public_root=public_root,
        node_modules_root=node_modules_root,
        allow_svg_conversion=True,
    )
    ext = Path(generated_name).suffix or ".png"
    filename = f"inline-{uuid.uuid4().hex}{ext}"
    url = f"https://api.weixin.qq.com/cgi-bin/media/uploadimg?access_token={access_token}"
    result = post_multipart(url, "media", filename, data, mime_type)
    image_url = result.get("url")
    if not image_url:
        raise SyncError("WeChat uploadimg returned no URL.")
    return image_url


def upload_thumb_image(
    access_token: str,
    source: str,
    base_url: str,
    public_root: Path | None,
    node_modules_root: Path | None,
) -> str:
    data, mime_type, generated_name = load_wechat_upload_asset(
        source,
        base_url,
        public_root=public_root,
        node_modules_root=node_modules_root,
        allow_svg_conversion=True,
    )
    ext = Path(generated_name).suffix or ".png"
    filename = f"thumb-{uuid.uuid4().hex}{ext}"
    url = f"https://api.weixin.qq.com/cgi-bin/material/add_material?access_token={access_token}&type=image"
    result = post_multipart(url, "media", filename, data, mime_type)
    media_id = result.get("media_id")
    if not media_id:
        raise SyncError("WeChat add_material returned no media_id.")
    return media_id


def get_draft_article(access_token: str, media_id: str, index: int) -> dict:
    try:
        result = request_json(
            f"https://api.weixin.qq.com/cgi-bin/draft/get?access_token={access_token}",
            payload={"media_id": media_id},
        )
    except SyncError as exc:
        raise SyncError(
            "Could not read existing WeChat draft for update. "
            f"media_id={media_id}. This usually means the draft does not belong to the current "
            "公众号/app_id, has already become unusable, or the media_id is wrong."
        ) from exc
    news_items = result.get("news_item") or []
    if index >= len(news_items):
        raise SyncError(
            f"Draft {media_id} has only {len(news_items)} article(s), so --index {index} is out of range."
        )
    article = news_items[index]
    if not isinstance(article, dict):
        raise SyncError(f"Unexpected draft/get response for media_id={media_id}.")
    return article


def choose_thumb_source(
    cli_thumb: str | None,
    config: dict,
    head_meta: dict,
    body_images: list[str],
    public_root: Path | None,
    node_modules_root: Path | None,
) -> str:
    candidates = [
        cli_thumb,
        config.get("thumb_image_path"),
        head_meta.get("hero_image"),
        *body_images,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            load_wechat_upload_asset(
                candidate,
                config["site_url"],
                public_root=public_root,
                node_modules_root=node_modules_root,
                allow_svg_conversion=True,
            )
            return candidate
        except SyncError:
            continue
    raise SyncError(
        "No usable cover image found. Provide --thumb with a bmp/png/jpeg/jpg/gif image."
    )


def rewrite_body_html(
    body_html: str,
    article_url: str,
    image_rewriter: Callable[[str], str],
) -> str:
    parser = WeChatHtmlRewriter(article_url, lambda src: image_rewriter(absolutize_url(src, article_url)))
    parser.feed(body_html)
    return parser.render_output()


def build_draft_payload(
    *,
    title: str,
    author: str,
    digest: str,
    content: str,
    article_url: str,
    thumb_media_id: str,
    config: dict,
) -> dict:
    article = {
        "title": title,
        "author": author,
        "digest": digest,
        "content": content,
        "content_source_url": article_url,
        "need_open_comment": int(config.get("open_comment", 0)),
        "only_fans_can_comment": int(config.get("fans_can_comment", 0)),
    }
    if thumb_media_id:
        article["thumb_media_id"] = thumb_media_id
    return {"articles": [article]}


def build_sendall_payload(
    *,
    media_id: str,
    tag_id: int | None,
    send_ignore_reprint: int,
) -> dict:
    filter_payload: dict[str, object]
    if tag_id is None:
        filter_payload = {"is_to_all": True}
    else:
        filter_payload = {"is_to_all": False, "tag_id": tag_id}
    return {
        "filter": filter_payload,
        "mpnews": {"media_id": media_id},
        "msgtype": "mpnews",
        "send_ignore_reprint": send_ignore_reprint,
    }


def build_update_draft_payload(
    *,
    media_id: str,
    index: int,
    article: dict,
) -> dict:
    return {
        "media_id": media_id,
        "index": index,
        "articles": article,
    }


def build_publish_payload(media_id: str) -> dict:
    return {"media_id": media_id}


def maybe_write_output(path_str: str | None, payload: dict) -> None:
    if not path_str:
        return
    path = Path(path_str)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    validate_args(args)
    config = load_config(args.config)
    content_root = resolve_existing_dir(config.get("content_root"), "src/content/blog")
    public_root = resolve_existing_dir(config.get("public_root"), "public")
    node_modules_root = resolve_existing_dir(config.get("node_modules_root")) or ROOT

    if args.publish_existing_media_id:
        publish_payload = build_publish_payload(args.publish_existing_media_id)
        if args.dry_run:
            output = {
                "mode": "publish-existing",
                "publish_payload": publish_payload,
            }
            maybe_write_output(args.output, output)
            print(f"dry-run ok: publish existing {args.publish_existing_media_id}")
            if args.output:
                print(f"wrote preview payload to {args.output}")
            return
        access_token = get_access_token(config)
        publish_result = request_json(
            f"https://api.weixin.qq.com/cgi-bin/freepublish/submit?access_token={access_token}",
            payload=publish_payload,
        )
        publish_id = publish_result.get("publish_id", "")
        print(f"publish submitted for existing draft: {args.publish_existing_media_id}")
        if publish_id:
            print(f"publish_id: {publish_id}")
        if args.output:
            maybe_write_output(args.output, publish_result)
            print(f"wrote response to {args.output}")
        return

    article_url = resolve_article_url(args, config)
    page_html = request_text(article_url)

    head_meta = parse_head_meta(page_html)
    body_html = extract_article_html(page_html)
    body_images = collect_image_sources(body_html)

    title = head_meta["title"]
    if not title:
        raise SyncError("Could not determine article title from page metadata.")
    digest = infer_digest(head_meta["description"], body_html)
    author = args.author or config.get("author", "")
    article_source_path = find_article_source_by_slug(args.slug.strip("/"), content_root) if args.slug else None
    effective_update_media_id = args.update_media_id
    if not effective_update_media_id and article_source_path is not None:
        effective_update_media_id = get_frontmatter_field(article_source_path, "wechatDraftMediaId")

    thumb_source = choose_thumb_source(args.thumb, config, head_meta, body_images, public_root, node_modules_root)

    if args.dry_run:
        content = rewrite_body_html(body_html, article_url, lambda src: src)
        dry_run_thumb_media_id = (
            "REUSE_EXISTING_THUMB_MEDIA_ID" if effective_update_media_id else "DRY_RUN_THUMB_MEDIA_ID"
        )
        draft_payload = build_draft_payload(
            title=title,
            author=author,
            digest=digest,
            content=content,
            article_url=article_url,
            thumb_media_id=dry_run_thumb_media_id,
            config=config,
        )
        output: dict[str, object] = {
            "mode": args.mode,
            "article_url": article_url,
            "thumb_source": thumb_source,
            "draft_payload": draft_payload,
        }
        if effective_update_media_id:
            output["draft_update_payload"] = build_update_draft_payload(
                media_id=effective_update_media_id,
                index=args.index,
                article=draft_payload["articles"][0],
            )
        if args.publish:
            output["publish_payload"] = build_publish_payload(
                effective_update_media_id or "DRY_RUN_DRAFT_MEDIA_ID"
            )
        if args.mode == "sendall":
            output["sendall_payload"] = build_sendall_payload(
                media_id="DRY_RUN_DRAFT_MEDIA_ID",
                tag_id=args.tag_id,
                send_ignore_reprint=args.send_ignore_reprint,
            )
        maybe_write_output(args.output, output)
        print(f"dry-run ok: {title}")
        print(f"mode: {args.mode}")
        print(f"thumb source: {thumb_source}")
        if args.output:
            print(f"wrote preview payload to {args.output}")
        return

    access_token = get_access_token(config)
    uploaded_images: dict[str, str] = {}
    skipped_images: list[str] = []
    existing_draft_article: dict | None = None

    if effective_update_media_id:
        existing_draft_article = get_draft_article(access_token, effective_update_media_id, args.index)

    def upload_once(source: str) -> str:
        if source not in uploaded_images:
            try:
                uploaded_images[source] = upload_inline_image(access_token, source, article_url, public_root, node_modules_root)
            except SyncError:
                uploaded_images[source] = source
                skipped_images.append(source)
        return uploaded_images[source]

    content = rewrite_body_html(body_html, article_url, upload_once)
    thumb_media_id = ""
    if effective_update_media_id:
        thumb_media_id = existing_draft_article.get("thumb_media_id", "") if existing_draft_article else ""
        if args.thumb:
            thumb_media_id = upload_thumb_image(access_token, thumb_source, article_url, public_root, node_modules_root)
    else:
        thumb_media_id = upload_thumb_image(access_token, thumb_source, article_url, public_root, node_modules_root)
    draft_payload = build_draft_payload(
        title=title,
        author=author,
        digest=digest,
        content=content,
        article_url=article_url,
        thumb_media_id=thumb_media_id,
        config=config,
    )
    maybe_write_output(args.output, draft_payload)

    if effective_update_media_id:
        update_payload = build_update_draft_payload(
            media_id=effective_update_media_id,
            index=args.index,
            article=draft_payload["articles"][0],
        )
        result = request_json(
            f"https://api.weixin.qq.com/cgi-bin/draft/update?access_token={access_token}",
            payload=update_payload,
        )
        media_id = effective_update_media_id
        print(f"draft updated: {title}")
        print(f"draft index: {args.index}")
        if article_source_path is not None:
            upsert_frontmatter_field(article_source_path, "wechatDraftMediaId", media_id)
            print(f"saved wechatDraftMediaId to {article_source_path}")
    else:
        result = request_json(
            f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={access_token}",
            payload=draft_payload,
        )
        media_id = result.get("media_id", "")
        print(f"draft created: {title}")
        if media_id and article_source_path is not None:
            upsert_frontmatter_field(article_source_path, "wechatDraftMediaId", media_id)
            print(f"saved wechatDraftMediaId to {article_source_path}")
    print(f"mode: {args.mode}")
    print(f"article url: {article_url}")
    print(f"thumb source: {thumb_source}")
    if effective_update_media_id and not args.update_media_id:
        print(f"resolved update media_id from frontmatter: {effective_update_media_id}")
    if media_id:
        print(f"draft media_id: {media_id}")
    if args.publish:
        if not media_id:
            raise SyncError("No draft media_id available for publish.")
        publish_result = request_json(
            f"https://api.weixin.qq.com/cgi-bin/freepublish/submit?access_token={access_token}",
            payload=build_publish_payload(media_id),
        )
        publish_id = publish_result.get("publish_id")
        print("publish submitted")
        if publish_id:
            print(f"publish_id: {publish_id}")
            if article_source_path is not None:
                upsert_frontmatter_field(article_source_path, "wechatPublishId", str(publish_id))
                print(f"saved wechatPublishId to {article_source_path}")
    if args.mode == "sendall":
        if not media_id:
            raise SyncError("WeChat draft/add returned no media_id for sendall mode.")
        sendall_payload = build_sendall_payload(
            media_id=media_id,
            tag_id=args.tag_id,
            send_ignore_reprint=args.send_ignore_reprint,
        )
        send_result = request_json(
            f"https://api.weixin.qq.com/cgi-bin/message/mass/sendall?access_token={access_token}",
            payload=sendall_payload,
        )
        msg_id = send_result.get("msg_id")
        msg_data_id = send_result.get("msg_data_id")
        print("sendall submitted")
        if args.tag_id is None:
            print("send target: all followers")
        else:
            print(f"send target tag_id: {args.tag_id}")
        if msg_id:
            print(f"send msg_id: {msg_id}")
        if msg_data_id:
            print(f"send msg_data_id: {msg_data_id}")
    print(f"uploaded inline images: {len(uploaded_images)}")
    if skipped_images:
        print(f"skipped unsupported images: {len(skipped_images)}")
        for source in skipped_images:
            print(f" - {source}")
    if args.output:
        print(f"wrote payload to {args.output}")


if __name__ == "__main__":
    try:
        main()
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
