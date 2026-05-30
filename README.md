# chatgpt-share-reader

Decode a ChatGPT share page (`https://chatgpt.com/share/<share-id>`) into clean
Markdown.

ChatGPT share pages do not embed the conversation as ordinary JSON. The page is
rendered by React Router, which streams its hydration data as a *turbo-stream*
payload tucked inside one or more `streamController.enqueue("...")` JavaScript
calls. This tool turns that payload back into a normal object tree and then into
a readable Markdown transcript.

Pure Python standard library. No third-party dependencies. No pip install of
runtime packages required.

## Install

```sh
git clone https://github.com/chickensintrees/chatgpt-share-reader.git
cd chatgpt-share-reader
```

That is the whole install. The tool runs on the Python standard library
(`urllib`, `json`, `re`, `argparse`), so there is nothing to `pip install`.

Optionally, install it as a console script:

```sh
pip install .
```

This exposes a `chatgpt-share-reader` command. It is convenience only; the
script also runs directly with `python3 chatgpt_share_reader.py`.

Requires Python 3.8 or newer.

## Usage

There are two modes.

### Fetch mode — download and decode a live share page

```sh
python3 chatgpt_share_reader.py <share-id> --out OUTDIR
```

You can pass a bare share id or a full URL. Multiple ids are allowed:

```sh
python3 chatgpt_share_reader.py <share-id> --out ./threads
python3 chatgpt_share_reader.py https://chatgpt.com/share/<share-id> --out ./threads
python3 chatgpt_share_reader.py <share-id-a> <share-id-b> --out ./threads
```

Each thread is written to `OUTDIR/thread-<share-id>.md`.

### Local mode — decode an already-saved HTML page

If you have saved the share page HTML to disk (for example via your browser's
"Save Page As"), decode it offline with `--html`:

```sh
python3 chatgpt_share_reader.py --html saved.html --out OUTDIR
```

This makes no network requests.

### As a library

```python
import chatgpt_share_reader as csr

html = csr.fetch("<share-id>")          # or read your own saved HTML
markdown = csr.decode_html(html, "<share-id>")
print(markdown)
```

## How it works

The decoded stream is newline-delimited. The interesting structure lives on the
first line: one large JSON array called `flat`.

```
[ <slot 0>, <slot 1>, <slot 2>, ... ]
```

Every value in the original object graph — every string, number, boolean,
object, and list — occupies its own slot in `flat` and is referenced *by its
integer index* rather than appearing inline. This is how the format deduplicates
repeated values and represents shared references and cycles compactly. We call
this the **turbo-stream positional-flatten** format.

Decoding rules:

- **Integers are pointers.** A slot whose value is the integer `i` means "the
  real value lives at `flat[i]`". Resolution is recursive and memoised.
- **Objects** are encoded as `{"_<keyIndex>": <valueIndex>, ...}`. The map key
  is the literal string `"_"` followed by the index of the slot holding the
  *key string*; the map value is the index of the slot holding the value.
- **Lists** are JSON arrays whose elements are themselves edges (indices or
  inline literals).
- **Negative integers are sentinels** (holes / `undefined`) and decode to
  `None`.
- **Inline literals.** Strings, floats, and `true`/`false`/`null` may appear
  directly in a slot and are taken at face value.

Later lines resolve *deferred promises*. They look like `P<idx>:<json>` and mean
"slot `<idx>` was a promise; here is its resolved value". The promise body is
itself in the same positional-flatten encoding, so it is decoded the same way
and spliced in wherever an edge points at `<idx>`.

Once the object tree is hydrated, the tool locates the conversation payload
(the dict carrying `mapping` or `linear_conversation`), walks it in display
order, flattens each message's content parts to text (including image and audio
pointers and fenced code blocks), and emits Markdown with `## USER` /
`## ASSISTANT` headings.

The `enqueue` payload is often split across several calls; all chunks are
JS-unescaped and concatenated before parsing. Malformed chunks and promise lines
are skipped rather than aborting the whole parse.

## Limitations

- This is a best-effort decoder for an **undocumented, internal wire format**.
  OpenAI can change it at any time, which may break decoding without notice.
- It works only on **public share pages** (`/share/<id>`). It cannot read
  private conversations, accounts, or anything requiring authentication.
- Rendering targets text. Images, audio, and file attachments are represented by
  placeholders such as `[image 1024x768]`, not by the binary assets themselves.
- `system` messages and empty messages are intentionally omitted from the output.

## Tests

Offline tests build a synthetic turbo-stream fixture (no real share page) and
round-trip it through the decoder:

```sh
python3 tests/test_decoder.py
# or, if you have pytest installed:
pytest
```

## Contributing

Issues and pull requests are welcome. If the share-page format changes and
decoding breaks, a saved (sanitised) HTML sample attached to an issue is the
most useful thing you can provide. Please keep the project dependency-free
(standard library only) and add a test for any format change you handle.

## License

MIT. See [LICENSE](LICENSE).
