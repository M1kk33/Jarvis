# Bundled Markdown lexer

Jarvis 0.4.4 ships Marked **18.0.14** as `src/jarvis/interfaces/static/marked.umd.js`, copied unchanged from the official npm package. It is loaded locally; the running application does not use a CDN or download a parser. Source: https://github.com/markedjs/marked. Package: https://registry.npmjs.org/marked/-/marked-18.0.14.tgz.

The package archive was checked against its registry SHA-512 integrity value before extracting the two explicitly named assets:

```text
archive: sha512-mBHK6FBHuBAlhgRe88w9F0O1AbwwXJUcQibUbC/QcdTbVGAD7aWza+xt3N6oT/jCZx3/OMeS+8rnuiHZcQ9s7A==
marked.umd.js SHA-256: 21568877a938d2c4e7d74e27f18e60da96bb73a68809610ca39216e1efebae62
marked.LICENSE.txt SHA-256: 8e3a3f82f59a60958f56ca08f445647c32a4733dc7ca6c2c46f6eb898471ab9c
```

The MIT license and copyright notices are preserved in `marked.LICENSE.txt` beside the bundle and included in the Python wheel/desktop distribution. This component record does not replace the outstanding distribution-wide license review.

`markdown.js` uses only `marked.lexer()` to obtain tokens. It does not use Marked's HTML output. It creates a fixed set of DOM elements with text nodes, ignores source-provided tag/attribute names, limits rendering nesting to 32 levels and falls back to the unchanged text if parsing/rendering fails. Raw HTML and Markdown image syntax are literal text. Links render their label and destination as text, with no anchor or click handler. Generated artifacts use the existing authenticated preview/download component, outside Markdown. User submissions, tool details, stored messages and voice synthesis keep their original text.
