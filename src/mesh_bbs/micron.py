"""A bounded, display-only subset of NomadNet Micron for public post bodies.

The explicit marker travels inside the signed body, so old peers can store and
forward posts unchanged. No user markup is passed straight to HTML or NomadNet.
Syntax reference: markqvist/NomadNet nomadnet/ui/textui/MicronParser.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from html import escape
from urllib.parse import urlsplit

MARKER = "#!micron\n"
_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]")
_COLOR = re.compile(r"`([FB])(T[0-9a-fA-F]{6}|[0-9a-fA-F]{3})")


def clean(value: str) -> str:
    return _INVALID.sub("\ufffd", value.replace("\r\n", "\n").replace("\r", "\n"))


def literal(value: str) -> str:
    # An exact `= line toggles literal mode even within a literal block.
    return "`=\n" + "\n".join(" " + line for line in clean(value).split("\n")) + "\n`=\n"


@dataclass(frozen=True)
class Style:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    foreground: str = ""
    background: str = ""


@dataclass(frozen=True)
class Span:
    text: str
    style: Style = Style()
    link: str = ""


@dataclass(frozen=True)
class Line:
    spans: tuple[Span, ...]
    heading: int = 0
    align: str = "left"
    divider: bool = False


def safe_link(url: str) -> bool:
    if len(url) > 2048 or any(ord(c) <= 32 or c in "`[]|\\<>\"'" for c in url):
        return False
    if re.fullmatch(r"[0-9a-f]{32}:/page/[A-Za-z0-9_./-]+", url):
        return ".." not in url.split("/")
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _flush(text: list[str], spans: list[Span], style: Style) -> None:
    if text:
        spans.append(Span("".join(text), style))
        text.clear()


def parse(value: str) -> list[Line]:
    """Parse formatting without forms, submissions, partials, or page directives."""
    value = clean(value)
    if not value.startswith(MARKER):
        return [Line((Span(line),)) for line in value.split("\n")]
    source = value[len(MARKER) :]
    style, align, verbatim = Style(), "left", False
    lines: list[Line] = []
    span_count = 0
    for raw in source.split("\n"):
        if raw == "`=":
            verbatim = not verbatim
            continue
        if verbatim:
            lines.append(Line((Span("`=" if raw == "\\`=" else raw),)))
            continue
        if raw.startswith("#") and not raw.startswith("#!"):
            continue
        heading = 0
        if raw.startswith("\\"):
            if len(raw) < 2 or raw[1] not in "`\\":
                raw = raw[1:]
        elif raw.startswith("<"):
            raw = raw.lstrip("<")
        elif raw.startswith(">"):
            heading = min(3, len(raw) - len(raw.lstrip(">")))
            raw = raw.lstrip(">")
        elif raw.startswith("-"):
            lines.append(Line((), divider=True))
            continue
        # Active constructs are displayed verbatim, including their delimiters.
        if raw.startswith(("#!", "`{", "`t")):
            lines.append(Line((Span(raw),)))
            continue
        spans: list[Span] = []
        text: list[str] = []

        i = 0
        while i < len(raw):
            if raw[i] == "\\" and i + 1 < len(raw) and raw[i + 1] in "\\`":
                text.append(raw[i + 1])
                i += 2
                continue
            if raw[i] != "`" or i + 1 == len(raw):
                text.append(raw[i])
                i += 1
                continue
            command = raw[i + 1]
            color = _COLOR.match(raw, i)
            if color:
                _flush(text, spans, style)
                code = color[2].removeprefix("T").lower()
                style = (
                    replace(style, foreground=code)
                    if color[1] == "F"
                    else replace(style, background=code)
                )
                i = color.end()
                continue
            if command in "!*_fbcarl`":
                _flush(text, spans, style)
                if command == "!":
                    style = replace(style, bold=not style.bold)
                elif command == "*":
                    style = replace(style, italic=not style.italic)
                elif command == "_":
                    style = replace(style, underline=not style.underline)
                elif command in "fb":
                    style = (
                        replace(style, foreground="")
                        if command == "f"
                        else replace(style, background="")
                    )
                elif command == "`":
                    style, align = Style(), "left"
                else:
                    align = {"c": "center", "r": "right"}.get(command, "left")
                i += 2
                continue
            if command in "[<":
                closing = "]" if command == "[" else ">"
                end = raw.find(closing, i + 2)
                if end != -1:
                    whole = raw[i : end + 1]
                    parts = raw[i + 2 : end].split("`")
                    url = parts[-1]
                    label = parts[0] if len(parts) == 2 else url
                    if command == "[" and len(parts) in {1, 2} and safe_link(url):
                        _flush(text, spans, style)
                        spans.append(Span(label, style, url))
                    else:
                        text.append(whole)
                    i = end + 1
                    continue
            text.append("`")
            i += 1
        _flush(text, spans, style)
        span_count += len(spans)
        if span_count > 4096:
            return [Line((Span(line),)) for line in source.split("\n")]
        lines.append(Line(tuple(spans), heading, align))
    return lines


def plain(value: str) -> str:
    if not clean(value).startswith(MARKER):
        return clean(value)
    return "\n".join(
        "---"
        if line.divider
        else "".join(
            span.text + (f" ({span.link})" if span.link and span.link != span.text else "")
            for span in line.spans
        )
        for line in parse(value)
    )


def html(value: str) -> str:
    if not clean(value).startswith(MARKER):
        return (
            "<p>" + escape(clean(value)).replace("\n\n", "</p><p>").replace("\n", "<br>\n") + "</p>"
        )
    output = []
    for line in parse(value):
        if line.divider:
            output.append("<hr>")
            continue
        tag = f"h{line.heading + 2}" if line.heading else "div"
        content = []
        for span in line.spans:
            text = escape(span.text)
            if span.link.startswith(("http://", "https://")):
                text = (
                    f'<a href="{escape(span.link, quote=True)}" '
                    f'rel="nofollow noreferrer">{text}</a>'
                )
            elif span.link and span.link != span.text:
                text += f" ({escape(span.link)})"
            for enabled, name in (
                (span.style.bold, "strong"),
                (span.style.italic, "em"),
                (span.style.underline, "u"),
            ):
                if enabled:
                    text = f"<{name}>{text}</{name}>"
            colors = []
            for color, prefix in ((span.style.foreground, "f"), (span.style.background, "b")):
                if color:
                    colors.append(f"micron-{prefix}-{color}")
            if colors:
                text = f'<span class="{" ".join(colors)}">{text}</span>'
            content.append(text)
        output.append(
            f'<{tag} class="micron-line micron-{line.align}">{"".join(content) or "<br>"}</{tag}>'
        )
    return "".join(output)


def stylesheet(rendered: str) -> str:
    """Only parsed color classes become CSS; arbitrary post CSS is never accepted."""
    colors = sorted(set(re.findall(r"micron-([fb])-([a-f0-9]{6}|[a-f0-9]{3})\b", rendered)))
    return "".join(
        f".micron-{kind}-{color}{{{'color' if kind == 'f' else 'background-color'}:#{color}}}"
        for kind, color in colors
    )


def nomad(value: str) -> str:
    if not clean(value).startswith(MARKER):
        return literal(value)
    output = ["<``\n"]
    for line in parse(value):
        if line.divider:
            output.append("-\n")
            continue
        prefix = ">" * line.heading
        alignment = {"left": "`l", "center": "`c", "right": "`r"}[line.align]
        parts = [prefix + ("" if line.heading else "``") + alignment]
        for index, span in enumerate(line.spans):
            style = span.style
            # Preserve NomadNet's theme-dependent heading palette on the first span.
            if index or not line.heading:
                parts.append("``" + alignment)
            for enabled, command in (
                (style.bold, "!"),
                (style.italic, "*"),
                (style.underline, "_"),
            ):
                if enabled:
                    parts.append("`" + command)
            for color, command in ((style.foreground, "F"), (style.background, "B")):
                if color:
                    parts.append("`" + command + ("T" if len(color) == 6 else "") + color)
            if span.link and not any(c in span.text for c in "`[]\\"):
                parts.append(f"`[{span.text}`{span.link}]")
            else:
                text = span.text + (f" ({span.link})" if span.link else "")
                parts.append(text.replace("\\", "\\\\").replace("`", "\\`"))
        output.append("".join(parts) + "\n")
    output.append("<``\n")
    result = "".join(output)
    return result if len(result.encode()) <= 180 * 1024 else literal(plain(value))
