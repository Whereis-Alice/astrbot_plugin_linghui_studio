"""Present plugin notices using QQ Official's native Markdown capability."""

import copy
import html
import re
from typing import Any

from astrbot.api.message_components import File, Image, Plain, Record, Video


QQ_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})
_MARKDOWN_META = re.compile(r"([\\`*_\[\]#|~])")
_LINE_MARKER = re.compile(r"(?m)^(\s*)([+\-=]|\d+[.)])(?=\s|$)")


def escape_markdown(value: Any) -> str:
    """Treat every supplied value as text, including links and HTML entities."""
    text = html.escape(str(value), quote=False)
    text = _MARKDOWN_META.sub(r"\\\1", text)
    return _LINE_MARKER.sub(r"\1\\\2", text)


def _detail_markdown(line: str) -> str:
    # The source text uses a bullet as text, rather than trusted Markdown.
    if line.startswith("• "):
        return f"- {_detail_markdown(line[2:])}"
    match = re.match(r"^([^:：\n]{1,16})[:：]\s*(.*)$", line)
    if match:
        return f"**{escape_markdown(match[1])}：** {escape_markdown(match[2])}"
    return escape_markdown(line)


def format_notice_markdown(text: Any) -> str:
    """Add a short title and readable details without interpreting user markup."""
    body = str(text).strip()
    if not body:
        return ""
    lines = body.splitlines()
    first = lines.pop(0).strip()
    fields = first.split(" | ")
    heading = fields.pop(0)
    requester = ""
    # Inspect only the heading: bracketed preset names are metadata, and batch
    # positions must remain in the title. IDs delimit bracketed nicknames.
    prefix_match = re.match(r"^(\[.*\([^()\[\]\n]+\)\]|\[[^\[\]\n]+\])\s+(\S.*)$", heading)
    if prefix_match:
        requester, heading = prefix_match.groups()
    title = heading.rstrip("：:")
    if len(title) > 120:
        fields.insert(0, title)
        title = "灵绘 Studio"
    result = [f"### {escape_markdown(title)}"]
    if requester:
        result.append(f"> {escape_markdown(requester)}")
    details = [part.strip() for part in fields if part.strip()]
    for line in lines:
        details.extend(part.strip() for part in line.split(" | ") if part.strip())
    if details:
        result.append("  \n".join(_detail_markdown(line) for line in details))
    return "\n\n".join(result)


def _event_platform_name(event: Any) -> str:
    try:
        return str(event.get_platform_name() or "")
    except (AttributeError, TypeError):
        return ""


def _copy_chain(chain: Any, nodes: list[Any], *, markdown: bool) -> Any:
    # A shallow chain copy preserves result metadata while never altering the
    # original image bytes, requester nodes, or plain-text fallback chain.
    prepared = copy.copy(chain)
    prepared.chain = nodes
    prepared.use_markdown(markdown)
    if markdown and callable(getattr(prepared, "use_t2i", None)):
        prepared.use_t2i(False)
    prepared._linghui_presentation_prepared = True
    return prepared


def prepare_outgoing_messages(
    chain: Any, *, event: Any = None, platform_name: str = ""
) -> list[Any]:
    """Return native Markdown notices only for supported official adapters.

    QQ group/C2C media payloads discard Markdown even when the chain requests it.
    Return media first and its notice second in that case. The caller must treat
    a failure sending the secondary notice as informational: the media may have
    already been delivered and must not be retried because its caption failed.

    AstrBot's official adapter handles explicit Markdown permission rejection
    by retrying as plain content. No plugin-level network retry is performed.
    Legacy SDKs lacking the public Markdown method retain the original chain.
    """
    name = platform_name or _event_platform_name(event)
    if (
        name not in QQ_OFFICIAL_PLATFORMS
        or not callable(getattr(chain, "use_markdown", None))
        or getattr(chain, "_linghui_presentation_prepared", False)
    ):
        return [chain]
    nodes = getattr(chain, "chain", None)
    if not isinstance(nodes, list):
        return [chain]
    text = "".join(str(node.text) for node in nodes if isinstance(node, Plain))
    if not text.strip():
        return [chain]
    markdown = format_notice_markdown(text)
    media_types = (Image, Record, Video, File)
    has_media = any(isinstance(node, media_types) for node in nodes)
    if has_media:
        media = _copy_chain(
            chain, [node for node in nodes if isinstance(node, media_types)],
            markdown=False,
        )
        # Requester mentions/reply references belong to the receipt. Keeping
        # them out of the media message avoids notifying the requester twice.
        notice_nodes = [
            node for node in nodes if not isinstance(node, (Plain,) + media_types)
        ]
        # QQ renders At as an inline tag; the heading needs its own line.
        notice_text = "\n" + markdown if notice_nodes else markdown
        notice = _copy_chain(chain, notice_nodes + [Plain(notice_text)], markdown=True)
        return [media, notice]
    # Keep At/Reply and any other non-text components intact. Replace all Plain
    # nodes with one formatted body, preserving its position in the chain.
    formatted_nodes = []
    inserted = False
    for node in nodes:
        if isinstance(node, Plain):
            if not inserted:
                notice_text = "\n" + markdown if formatted_nodes else markdown
                formatted_nodes.append(Plain(notice_text))
                inserted = True
        else:
            formatted_nodes.append(node)
    return [_copy_chain(chain, formatted_nodes, markdown=True)]
