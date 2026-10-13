"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const marked = require("../src/jarvis/interfaces/static/marked.umd.js");
const staticDir = path.join(__dirname, "../src/jarvis/interfaces/static");

function fixture() {
  class Node {
    constructor(name, namespaceURI = "http://www.w3.org/1999/xhtml") {
      this.localName = name; this.namespaceURI = namespaceURI; this.children = []; this.attributes = []; this.style = {}; this._text = "";
    }
    get textContent() { return this._text + this.children.map(node => node.textContent).join(""); }
    set textContent(value) { this._text = value; this.children = []; }
    append(...nodes) { for (const node of nodes) this.children.push(...(node.localName === "fragment" ? node.children : [node])); }
    appendChild(node) { this.append(node); return node; }
    replaceChildren(...nodes) { this.textContent = ""; this.append(...nodes); }
    setAttribute(name, value) { this.attributes = this.attributes.filter(a => a.name !== name); this.attributes.push({ name, value }); }
    getAttribute(name) { return this.attributes.find(a => a.name === name)?.value ?? null; }
    querySelectorAll() { return this.children.flatMap(node => node.localName === "text" ? [] : [node, ...node.querySelectorAll()]); }
    cloneNode(deep) { const node = new Node(this.localName, this.namespaceURI); node._text = this._text; node.className = this.className; node.attributes = this.attributes.map(a => ({ ...a })); if (deep) node.children = this.children.map(n => n.cloneNode(true)); return node; }
    set innerHTML(_) { throw Error("HTML sink forbidden"); }
  }
  const document = {
    compatMode: "CSS1Compat",
    createElement: tag => new Node(tag),
    createElementNS: (namespace, tag) => new Node(tag, namespace),
    createDocumentFragment: () => new Node("fragment"),
    createTextNode: text => { const node = new Node("text"); node._text = text; return node; },
  };
  const window = { marked };
  const context = vm.createContext({ window, document, console });
  vm.runInContext(fs.readFileSync(path.join(staticDir, "katex.min.js"), "utf8"), context);
  window.katex = context.katex;
  for (const name of ["math.js", "markdown.js"]) vm.runInContext(fs.readFileSync(path.join(staticDir, name), "utf8"), context);
  const root = new Node("root");
  const nodes = () => [root, ...root.querySelectorAll()];
  return { root, window, nodes, render: (source, options) => window.JarvisMarkdown.render(root, source, options) };
}

test("real KaTeX renders inline/display TeX before Markdown, including lists and tables", () => {
  const f = fixture();
  f.render(String.raw`**Формула:** \((\Omega,\mathcal{F})\), $x_i^2$.

\[\frac{1}{2}+\sqrt{x}\]

1. Вероятность \(P(X=1)\).

| Величина | Значение |
| --- | --- |
| $p$ | $\mathbb{R}$ |`);
  const math = f.nodes().filter(n => n.localName === "math");
  assert.equal(math.length, 6);
  assert.ok(f.nodes().some(n => n.localName === "mi" && n.textContent === "ℝ"));
  assert.ok(f.nodes().some(n => n.localName === "mi" && n.textContent === "ℱ"));
  assert.ok(f.nodes().some(n => n.localName === "mfrac"));
  assert.ok(f.nodes().some(n => n.localName === "msqrt"));
  assert.ok(f.nodes().some(n => n.localName === "strong"));
  assert.ok(f.nodes().some(n => n.localName === "msubsup"));
});

test("code, escaped dollars and ordinary currency remain literal", () => {
  const f = fixture();
  const source = String.raw`Цена $10 и $20; \$x. Код: ` + "`\\(x^2\\)`\n\n```latex\n\\[x^2\\]\n```";
  f.render(source);
  assert.equal(f.nodes().filter(n => n.localName === "math").length, 0);
  assert.match(f.root.textContent, /Цена \$10 и \$20/);
  assert.ok(f.nodes().find(n => n.localName === "code").textContent.includes("\\(x^2\\)"));
});

test("unfinished and invalid math retain exact text and can become a valid formula", () => {
  const f = fixture();
  f.render(String.raw`Начало \(\frac{1}{`);
  assert.equal(f.root.textContent, String.raw`Начало \(\frac{1}{`);
  f.render(String.raw`Начало \(\frac{1}{2}\)`);
  assert.equal(f.nodes().filter(n => n.localName === "math").length, 1);
  f.render(String.raw`\(\unknownCommand{x}\)`);
  assert.equal(f.root.textContent, String.raw`\(\unknownCommand{x}\)`);
});

test("untrusted formulas cannot create navigation, resources or HTML sinks", () => {
  const f = fixture();
  for (const formula of [String.raw`\href{javascript:alert(1)}{click}`, String.raw`\includegraphics{https://example.invalid/p}`, String.raw`\htmlClass{injected}{x}`, String.raw`\text{<img src=x onerror=bad()>}`]) {
    f.render(`\\(${formula}\\)`);
    for (const node of f.nodes()) {
      assert.ok(!["a", "img", "script", "iframe", "style"].includes(node.localName));
      assert.ok(!node.attributes.some(a => /^(?:on|src$|href$)/.test(a.name)));
    }
  }
});

test("macro loops, excessive sizes/counts and nesting fall back within limits; macros are isolated", () => {
  const f = fixture();
  const recursive = String.raw`\(\def\x{\x}\x\)`;
  f.render(recursive); assert.equal(f.root.textContent, recursive);
  const nested = "\\(" + "{".repeat(40) + "x" + "}".repeat(40) + "\\)";
  f.render(nested); assert.equal(f.root.textContent, nested);
  f.render("\\(" + "x".repeat(2050) + "\\)"); assert.equal(f.nodes().filter(n => n.localName === "math").length, 0);
  f.render(Array.from({ length: 80 }, (_, i) => `$x_{${i}}$`).join(" "));
  assert.equal(f.nodes().filter(n => n.localName === "math").length, 64);
  f.render(String.raw`\(\gdef\other{x}\other\) \(\other\)`);
  assert.ok(f.root.textContent.endsWith(String.raw`\(\other\)`));
});

test("closed formulas are cached across updates and streaming timers cannot overwrite final text", () => {
  const f = fixture();
  const original = f.window.katex.render; let calls = 0;
  f.window.katex.render = (...args) => { calls++; return original(...args); };
  f.render(String.raw`\(x\) first`); f.render(String.raw`\(x\) second`);
  assert.equal(calls, 1);
  const timers = new Map(); let id = 0;
  f.window.setTimeout = (fn, ms) => { assert.equal(ms, 120); timers.set(++id, fn); return id; };
  f.window.clearTimeout = key => timers.delete(key);
  f.render(String.raw`\(x\) draft`, { streaming: true });
  f.render(String.raw`\(x\) newer draft`, { streaming: true });
  assert.equal(timers.size, 1);
  f.render(String.raw`\(x\) final`);
  assert.equal(timers.size, 0);
  assert.ok(f.root.textContent.endsWith(" final"));
});


test("native MathML preserves Greek bold letters and legacy Unicode alphabet holes", () => {
  const f = fixture();
  f.render(String.raw`\(\mathit{h}+\mathfrak{CHIRZ}+\mathbf{\Omega}\)`);
  const letters = f.nodes().filter(n => n.localName === "mi").map(n => n.textContent).join("");
  for (const value of ["h", "ℭ", "ℌ", "ℑ", "ℜ", "ℨ", "𝛀"]) assert.ok(letters.includes(value), value);
});
