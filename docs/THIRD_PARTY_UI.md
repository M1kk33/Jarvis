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


## Local mathematical rendering (0.5.10)

KaTeX **0.19.0** is bundled unchanged as `katex.min.js`, with its MIT license in `katex.LICENSE.txt`. The official npm archive was verified against its SHA-512 registry integrity before extracting only those two named files. No CDN, font download or runtime package installation is used.

```text
archive: sha512-v6Tznz3zJ7u3niRCoDTsumM2+HA2XXcCu+WAacCeHD2z3p9A9Ks987o5FzfTGBN0e8A0vjEgIDvLbICrXpdw/Q==
katex.min.js SHA-256: 103a53763cc033bba8d175bf3f0ba597c3505c9b6747dd3f2c7bc2a6bfcc8ae7
katex.LICENSE.txt SHA-256: 766ccc1f306c885aa45542a9846bbd0a505b27a0374f146778171c2254ce18e3
```

A dedicated Marked instance recognizes `\(...\)`, `\[...\]`, `$...$` and `$$...$$` before Markdown escaping. Code spans/fences and user messages remain literal. KaTeX's DOM API produces detached MathML with `trust:false`, isolated macros, strict parsing, `maxExpand:100` and `maxSize:10`. No HTML string parsing sink is used. Generated elements/namespaces/attributes are checked before insertion. Invalid, incomplete, unsupported or over-limit expressions retain their original text.

The renderer additionally limits TeX to 2,048 characters per expression, 32 brace nesting levels, 64 formulas/16,384 TeX characters per message and 512 generated descendants per formula. A 64-entry cache reuses detached formula nodes. Streaming mathematical drafts are coalesced at 120 milliseconds; a final/direct render cancels an older queued draft. Stored replies, copied source and spoken text are unchanged.

MathML uses local Windows mathematical fonts. Non-normal Latin/Greek mathematical alphabets are mapped to their Unicode equivalents because Chromium's MathML Core does not implement the older `mathvariant` styles; this preserves distinctions such as ℝ, ℱ and bold Greek symbols. Sources: [KaTeX API](https://katex.org/docs/api), [options](https://katex.org/docs/options), [security](https://katex.org/docs/security), [MathML Core](https://www.w3.org/TR/mathml-core/).
