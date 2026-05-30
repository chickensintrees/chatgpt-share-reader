#!/usr/bin/env python3
"""Decode a ChatGPT share page into clean Markdown.

ChatGPT share pages (``https://chatgpt.com/share/<id>``) do not ship the
conversation as ordinary embedded JSON. Instead the page is rendered by
React Router, which streams its hydration data as a *turbo-stream* payload
embedded in one or more ``streamController.enqueue("...")`` JavaScript calls.

This module turns that payload back into a normal Python object tree and then
into Markdown. Nothing here is specific to any particular conversation: it is a
generic decoder for the turbo-stream *positional-flatten* wire format.


The turbo-stream positional-flatten format
------------------------------------------

The decoded stream is newline-delimited. The interesting structure lives on the
first line, which is a single large JSON array we call ``flat``::

    [ <slot 0>, <slot 1>, <slot 2>, ... ]

Every value in the original object graph - every string, number, boolean,
object, and list - occupies its own slot in ``flat`` and is referenced *by its
integer index* rather than appearing inline. This is how the format
deduplicates and how it represents cycles/shared references compactly.

Decoding rules:

* **Integers are pointers.** A slot whose value is the integer ``i`` means
  "the real value lives at ``flat[i]``". Resolution is recursive.
* **Objects** are encoded as ``{"_<keyIndex>": <valueIndex>, ...}``. The map
  key is the literal string ``"_"`` followed by the index of the slot holding
  the *key string*; the map value is the index of the slot holding the value.
  (Occasionally a key is already a plain string and is used verbatim.)
* **Lists** are JSON arrays whose elements are themselves edges (indices or
  inline literals).
* **Negative integers are sentinels** - holes / ``undefined`` - and decode to
  ``None``.
* **Inline literals.** Strings, floats, ``true``/``false``/``null`` may appear
  directly in a slot and are taken at face value.

Later lines resolve *deferred promises*. They look like ``P<idx>:<json>`` and
mean "slot ``<idx>`` was a promise; here is its resolved JSON value". We splice
those resolved values in when an edge points at ``<idx>``. The ``P``-line JSON
is itself in the same positional-flatten encoding and is decoded the same way.

Robustness notes:

* The ``enqueue`` payload may be split across several calls; we concatenate
  every chunk before parsing.
* Promise lines may be missing or malformed; an unresolved promise simply
  decodes via the normal slot it points at (or to ``None``).
* Message content may be plain ``parts`` strings, multimodal parts (images,
  audio, file pointers), or already-rendered text; all are handled.


Public API
----------

``fetch(share_id_or_url)``
    Download the raw HTML of a share page.
``decode_stream(stream)``
    Decode a turbo-stream payload string into ``(root, Decoder)``.
``extract_stream(html)``
    Pull and concatenate the turbo-stream payload out of page HTML.
``find_conversation(root)``
    Locate the conversation payload inside the hydrated object tree.
``conversation_to_md(conv, share_id)``
    Render a conversation payload as Markdown.
``decode_html(html, share_id="")``
    Convenience: HTML in, Markdown out.

Command line
------------

::

    chatgpt_share_reader.py <share-id-or-url> [more ...] --out DIR
    chatgpt_share_reader.py --html page.html --out DIR
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.request

__all__ = [
    "fetch",
    "extract_stream",
    "decode_stream",
    "find_conversation",
    "conversation_to_md",
    "decode_html",
    "Decoder",
]

# A run-of-the-mill desktop browser UA. Share pages are public; this is only to
# avoid being treated as an unknown bot.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch(share_id_or_url, timeout=60):
    """Download the raw HTML for a ChatGPT share page.

    ``share_id_or_url`` may be a bare share id (``"abc123"``) or a full URL.
    Returns the page HTML as a ``str``.
    """
    if share_id_or_url.startswith("http://") or share_id_or_url.startswith("https://"):
        url = share_id_or_url
    else:
        url = f"https://chatgpt.com/share/{share_id_or_url}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "replace")


# ---------------------------------------------------------------------------
# Extracting the turbo-stream payload from page HTML
# ---------------------------------------------------------------------------
def _read_js_string(s, start):
    """Read a double-quoted JS string literal.

    ``start`` indexes the opening quote. Returns ``(raw_escaped_body, end)``
    where ``end`` is the index just past the closing quote and
    ``raw_escaped_body`` still contains backslash escapes (so it can be handed
    back to ``json.loads`` wrapped in quotes).
    """
    if start >= len(s) or s[start] != '"':
        raise ValueError("expected opening quote")
    i = start + 1
    out = []
    while i < len(s):
        c = s[i]
        if c == "\\":
            # Keep the escape pair intact; let json.loads interpret it.
            out.append(s[i : i + 2])
            i += 2
            continue
        if c == '"':
            return "".join(out), i + 1
        out.append(c)
        i += 1
    # Unterminated string: return what we have.
    return "".join(out), i


def extract_stream(html):
    """Extract and concatenate the turbo-stream payload from page HTML.

    Every ``streamController.enqueue("...")`` argument is JS-unescaped and
    joined in order. Returns the concatenated payload string (possibly empty if
    no enqueue calls are present).
    """
    chunks = []
    for m in re.finditer(r"streamController\.enqueue\(", html):
        j = m.end()
        # Skip whitespace between '(' and the string literal.
        while j < len(html) and html[j] in " \n\t\r":
            j += 1
        if j < len(html) and html[j] == '"':
            raw, _ = _read_js_string(html, j)
            try:
                chunks.append(json.loads('"' + raw + '"'))
            except json.JSONDecodeError:
                # Malformed chunk: skip it rather than aborting the whole parse.
                continue
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Decoding the positional-flatten payload
# ---------------------------------------------------------------------------
class Decoder:
    """Resolve a positional-flatten ``flat`` array into a real object tree.

    Indices are pointers into ``flat``; ``promises`` maps an index to an
    already-resolved value (spliced in for deferred promise slots). Results are
    memoised, and a cycle guard prevents infinite recursion on self-referential
    graphs.
    """

    def __init__(self, flat, promises=None):
        self.flat = flat
        self.promises = promises or {}
        self.memo = {}
        self.inprogress = set()

    def resolve_edge(self, e):
        """Resolve a single edge: an index pointer or an inline literal."""
        # bool is a subclass of int, so check it first.
        if isinstance(e, bool):
            return e
        if isinstance(e, int):
            if e < 0:
                return None  # sentinel: undefined / hole
            if e in self.promises:
                return self.promises[e]
            if 0 <= e < len(self.flat):
                return self.resolve_index(e)
            return None  # out-of-range pointer: treat as a hole
        return e  # inline literal (str / float / None)

    def resolve_index(self, i):
        """Resolve the value living at ``flat[i]`` (recursively, memoised)."""
        if i in self.memo:
            return self.memo[i]
        if i in self.inprogress:
            return None  # cycle guard
        self.inprogress.add(i)
        node = self.flat[i]

        if isinstance(node, bool):
            val = node
        elif isinstance(node, dict):
            val = {}
            self.memo[i] = val  # publish early so cycles resolve to this dict
            for k, v in node.items():
                if isinstance(k, str) and k.startswith("_"):
                    rest = k[1:]
                    key = self.resolve_edge(int(rest)) if rest.lstrip("-").isdigit() else k
                else:
                    key = k
                # Object keys must be hashable strings; coerce defensively.
                if not isinstance(key, str):
                    key = str(key)
                val[key] = self.resolve_edge(v)
        elif isinstance(node, list):
            val = []
            self.memo[i] = val
            for x in node:
                val.append(self.resolve_edge(x))
        else:
            val = node  # str / float / None

        self.memo[i] = val
        self.inprogress.discard(i)
        return val


def _parse_promise_lines(lines):
    """Decode ``P<idx>:<json>`` promise lines into ``{idx: value}``.

    Each promise value is itself a positional-flatten payload (its own ``flat``
    array), so we recursively decode it. Malformed or missing lines are skipped.
    """
    promises = {}
    for ln in lines:
        m = re.match(r"^P(\d+):(.*)$", ln)
        if not m:
            continue
        idx = int(m.group(1))
        body = m.group(2)
        try:
            sub_flat = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(sub_flat, list) and sub_flat:
            try:
                promises[idx] = Decoder(sub_flat).resolve_index(0)
            except Exception:
                promises[idx] = sub_flat
        else:
            promises[idx] = sub_flat
    return promises


def decode_stream(stream):
    """Decode a turbo-stream payload string.

    Returns ``(root, decoder)`` where ``root`` is the hydrated object tree
    (slot 0 fully resolved) and ``decoder`` is the :class:`Decoder` used (handy
    for resolving other slots). Raises ``ValueError`` if the payload has no
    parseable first line.
    """
    if not stream or not stream.strip():
        raise ValueError("empty turbo-stream payload")
    lines = stream.split("\n")
    try:
        flat = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"first turbo-stream line is not JSON: {exc}") from exc
    if not isinstance(flat, list):
        raise ValueError("first turbo-stream line is not a JSON array")
    promises = _parse_promise_lines(lines[1:])
    dec = Decoder(flat, promises)
    root = dec.resolve_index(0)
    return root, dec


# ---------------------------------------------------------------------------
# Locating the conversation inside the hydrated tree
# ---------------------------------------------------------------------------
def find_conversation(root):
    """Find the conversation payload inside a hydrated turbo-stream tree.

    Tries the known React Router loader path first, then falls back to a
    bounded depth-first search for any dict containing ``mapping`` or
    ``linear_conversation``. Returns the conversation dict, or ``None``.
    """
    # Known fast path: React Router loaderData for the share route.
    try:
        ld = root["loaderData"]
        for route_key, route in ld.items():
            if not isinstance(route, dict):
                continue
            server = route.get("serverResponse")
            if isinstance(server, dict):
                data = server.get("data")
                if isinstance(data, dict) and (
                    "mapping" in data or "linear_conversation" in data
                ):
                    return data
    except Exception:
        pass

    # Fallback: bounded DFS for a conversation-shaped dict.
    found = []
    seen_ids = set()

    def walk(n, depth=0):
        if depth > 60 or found:
            return
        if isinstance(n, dict):
            if id(n) in seen_ids:
                return
            seen_ids.add(id(n))
            if "mapping" in n or "linear_conversation" in n:
                found.append(n)
                return
            for v in n.values():
                walk(v, depth + 1)
        elif isinstance(n, list):
            for v in n:
                walk(v, depth + 1)

    walk(root)
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Rendering content parts to text
# ---------------------------------------------------------------------------
def _multimodal_part_to_text(part):
    """Render a non-string content part (image / audio / file pointer) safely."""
    # Some parts wrap their text in a "text" field.
    if isinstance(part.get("text"), str):
        return part["text"]
    ctype = part.get("content_type") or part.get("type")
    if ctype in ("image_asset_pointer", "image"):
        w = part.get("width")
        h = part.get("height")
        dims = f" {w}x{h}" if w and h else ""
        return f"[image{dims}]"
    if ctype in ("audio_asset_pointer", "audio_transcription"):
        # Audio transcriptions often carry the spoken text.
        if isinstance(part.get("text"), str):
            return part["text"]
        return "[audio]"
    if ctype:
        return f"[{ctype}]"
    return ""


def parts_to_text(content):
    """Flatten a message ``content`` object into a single text string.

    Handles ``parts`` lists (strings and multimodal dicts), a bare ``text``
    field, and ``code`` content (wrapping it in a fenced block). Unknown shapes
    degrade to an empty string rather than raising.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return str(content)

    ctype = content.get("content_type")

    # Code blocks: render as a fenced block with the language, if any.
    if ctype == "code" and isinstance(content.get("text"), str):
        lang = content.get("language") or ""
        return f"```{lang}\n{content['text']}\n```"

    parts = content.get("parts")
    if isinstance(parts, list):
        out = []
        for p in parts:
            if isinstance(p, str):
                if p:
                    out.append(p)
            elif isinstance(p, dict):
                txt = _multimodal_part_to_text(p)
                if txt:
                    out.append(txt)
        return "\n".join(out)

    if isinstance(content.get("text"), str):
        return content["text"]

    return ""


# ---------------------------------------------------------------------------
# Rendering the conversation to Markdown
# ---------------------------------------------------------------------------
_ROLE_LABELS = {
    "user": "## USER",
    "assistant": "## ASSISTANT",
    "tool": "## TOOL",
    "system": "## SYSTEM",
}


def _ordered_nodes(conv):
    """Return conversation nodes in display order.

    Prefers ``linear_conversation``; otherwise walks the ``mapping`` tree from
    its root(s) following ``children`` links.
    """
    nodes = conv.get("linear_conversation")
    if isinstance(nodes, list) and nodes:
        return nodes

    mapping = conv.get("mapping") or {}
    if not isinstance(mapping, dict):
        return []

    roots = [
        k
        for k, v in mapping.items()
        if isinstance(v, dict) and not v.get("parent")
    ]
    if not roots:
        roots = list(mapping.keys())[:1]

    order = []
    seen = set()

    def walk(nid):
        if nid in seen or nid not in mapping:
            return
        seen.add(nid)
        node = mapping[nid]
        order.append(node)
        children = node.get("children") if isinstance(node, dict) else None
        for c in children or []:
            walk(c)

    for r in roots:
        walk(r)
    return order


def conversation_to_md(conv, share_id=""):
    """Render a conversation payload as Markdown.

    ``share_id`` is only used for the source attribution line. ``system`` and
    empty messages are skipped. Returns a Markdown string.
    """
    if not isinstance(conv, dict):
        return ""
    title = conv.get("title") or "Untitled"
    md = [f"# ChatGPT thread: {title}", ""]
    if share_id:
        md.append(f"_Source: chatgpt.com/share/{share_id}_")
        md.append("")

    for node in _ordered_nodes(conv):
        msg = node.get("message") if isinstance(node, dict) else None
        if not isinstance(msg, dict):
            continue
        author = (msg.get("author") or {}).get("role", "?")
        if author == "system":
            continue
        text = parts_to_text(msg.get("content"))
        if not text or not text.strip():
            continue
        label = _ROLE_LABELS.get(author, f"## {str(author).upper()}")
        md.append(label)
        md.append("")
        md.append(text.strip())
        md.append("")

    return "\n".join(md).rstrip() + "\n"


def decode_html(html, share_id=""):
    """Convenience: decode page HTML straight to Markdown.

    Returns the Markdown string, or raises ``ValueError`` if no turbo-stream
    payload or conversation could be found.
    """
    stream = extract_stream(html)
    if not stream:
        raise ValueError("no turbo-stream payload found in HTML")
    root, _ = decode_stream(stream)
    conv = find_conversation(root)
    if conv is None:
        raise ValueError("no conversation payload found in decoded tree")
    return conversation_to_md(conv, share_id)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def _share_id_from(arg):
    """Derive a short id for output filenames from an id or URL."""
    return arg.rstrip("/").split("/")[-1] or "thread"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Decode chatgpt.com/share/<id> pages into Markdown."
    )
    ap.add_argument("ids", nargs="*", help="share id(s) or full url(s)")
    ap.add_argument(
        "--html",
        help="decode an already-saved HTML file instead of fetching",
    )
    ap.add_argument("--out", default=".", help="output directory")
    args = ap.parse_args(argv)

    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    jobs = []  # (share_id, html)
    if args.html:
        jobs.append(("local", pathlib.Path(args.html).read_text(encoding="utf-8")))
    for x in args.ids:
        jobs.append((_share_id_from(x), fetch(x)))

    if not jobs:
        ap.error("provide a share id/url or --html FILE")

    exit_code = 0
    for sid, html in jobs:
        try:
            md = decode_html(html, sid)
        except ValueError as exc:
            print(f"[{sid}] {exc}", file=sys.stderr)
            exit_code = 1
            continue
        outpath = outdir / f"thread-{sid}.md"
        outpath.write_text(md, encoding="utf-8")
        turns = md.count("\n## ")
        print(f"[{sid}] -> {outpath}  ({len(md)} chars, ~{turns} turns)")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
