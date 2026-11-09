#!/usr/bin/env python3
"""Offline tests for chatgpt_share_reader.

These tests use a hand-built SYNTHETIC turbo-stream payload encoding a fake
two-message conversation with invented harmless content. No real ChatGPT share
page is used as a fixture.

The fixture is built programmatically by a tiny encoder (``_flatten``) that
turns a normal Python object tree into the positional-flatten wire format the
decoder expects. This keeps the fixture honest: if our understanding of the
format were wrong, the round-trip would fail.
"""

import json
import os
import sys

# Make the module importable whether run via pytest or directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chatgpt_share_reader as csr  # noqa: E402


# ---------------------------------------------------------------------------
# A minimal encoder that produces the positional-flatten format the decoder
# consumes: every value gets its own slot; objects are {"_<keyIdx>": valIdx}.
# ---------------------------------------------------------------------------
def _flatten(obj):
    """Encode ``obj`` into a positional-flatten ``flat`` list (slot 0 = root)."""
    flat = []

    def emit(value):
        idx = len(flat)
        flat.append(None)  # reserve the slot
        if isinstance(value, dict):
            shell = {}
            for k, v in value.items():
                key_idx = emit(k)
                val_idx = emit(v)
                shell[f"_{key_idx}"] = val_idx
            flat[idx] = shell
        elif isinstance(value, list):
            shell = [emit(v) for v in value]
            flat[idx] = shell
        else:
            # str / int / float / bool / None as inline literals.
            flat[idx] = value
        return idx

    emit(obj)
    return flat


def _build_synthetic_html():
    """Return HTML embedding a synthetic 2-message conversation turbo-stream."""
    conv = {
        "title": "Greeting test",
        "linear_conversation": [
            {
                "message": {
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["hello"]},
                }
            },
            {
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["hi there"]},
                }
            },
        ],
    }
    root = {
        "loaderData": {
            "routes/share.$shareId.($action)": {
                "serverResponse": {"data": conv}
            }
        }
    }
    flat = _flatten(root)
    first_line = json.dumps(flat)
    # JS-escape the payload as it would appear inside enqueue("...").
    js_string = json.dumps(first_line)  # produces a quoted, escaped JS literal
    return (
        "<!doctype html><html><body><script>"
        "window.__reactRouterContext={};"
        f"streamController.enqueue({js_string});"
        "</script></body></html>"
    )


def test_decode_two_turns():
    html = _build_synthetic_html()
    md = csr.decode_html(html, "synthetic")

    assert "Greeting test" in md, md
    assert "## USER" in md, md
    assert "## ASSISTANT" in md, md
    assert "hello" in md, md
    assert "hi there" in md, md
    # Two visible turns rendered as headings.
    assert md.count("## ") == 2, md
    # USER must precede ASSISTANT.
    assert md.index("## USER") < md.index("## ASSISTANT"), md


def test_split_enqueue_chunks():
    """The payload may arrive split across multiple enqueue() calls."""
    html = _build_synthetic_html()
    # Find the single enqueue string and split it down the middle into two calls.
    stream = csr.extract_stream(html)
    half = len(stream) // 2
    a = json.dumps(stream[:half])
    b = json.dumps(stream[half:])
    split_html = (
        "<script>"
        f"streamController.enqueue({a});"
        f"streamController.enqueue({b});"
        "</script>"
    )
    md = csr.decode_html(split_html, "synthetic")
    assert "hello" in md and "hi there" in md, md


def test_multimodal_and_code_parts():
    """Image pointers and code blocks render without raising."""
    img = csr.parts_to_text(
        {
            "content_type": "multimodal_text",
            "parts": [
                "look at this",
                {"content_type": "image_asset_pointer", "width": 4, "height": 2},
            ],
        }
    )
    assert "look at this" in img and "[image 4x2]" in img, img

    code = csr.parts_to_text(
        {"content_type": "code", "language": "python", "text": "print('x')"}
    )
    assert code.startswith("```python") and "print('x')" in code, code


def test_missing_promise_line_is_tolerated():
    """A malformed/missing promise line must not break decoding."""
    html = _build_synthetic_html()
    stream = csr.extract_stream(html)
    # Append a junk promise line that should be ignored.
    enq = json.dumps(stream + "\nP999:{not json}")
    junk_html = f"<script>streamController.enqueue({enq});</script>"
    md = csr.decode_html(junk_html, "synthetic")
    assert "hello" in md and "hi there" in md, md


def test_share_id_strips_query_and_fragment():
    """Links copied from a browser often carry ?query or #fragment suffixes."""
    sid = "6af12640-7654-83ea-bfbb-1c1414b62d2b"
    cases = [
        sid,
        f"{sid}?ogimg=plain",
        f"https://chatgpt.com/share/{sid}",
        f"https://chatgpt.com/share/{sid}/",
        f"https://chatgpt.com/share/{sid}?ogimg=plain",
        f"https://chatgpt.com/share/{sid}#top",
        f"https://chatgpt.com/share/{sid}/?a=1&b=2#frag",
    ]
    for c in cases:
        assert csr._share_id_from(c) == sid, (c, csr._share_id_from(c))
    assert csr._share_id_from("https://chatgpt.com/share/") == "share"
    assert csr._share_id_from("") == "thread"


if __name__ == "__main__":
    test_decode_two_turns()
    test_split_enqueue_chunks()
    test_multimodal_and_code_parts()
    test_missing_promise_line_is_tolerated()
    test_share_id_strips_query_and_fragment()
    print("OK: all tests passed")
