"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const marked = require("../src/jarvis/interfaces/static/marked.umd.js");
const source = fs.readFileSync(path.join(__dirname, "../src/jarvis/interfaces/static/markdown.js"), "utf8");

function fixture() {
  class Node {
    constructor(tag) { this.tag = tag; this.children = []; this._text = ""; this.attributes = {}; }
    get textContent() { return this._text + this.children.map(node => node.textContent).join(""); }
    set textContent(value) { this._text = value; this.children = []; }
    append(...nodes) { for (const node of nodes) this.children.push(...(node.tag === "fragment" ? node.children : [node])); }
    replaceChildren(...nodes) { this.replacements = (this.replacements || 0) + 1; this.textContent = ""; this.append(...nodes); }
    setAttribute(key, value) { this.attributes[key] = value; }
    set innerHTML(_) { throw Error("HTML sink forbidden"); }
  }
  const window = { marked };
  const document = {
    createElement(tag) { assert.ok(!["img", "a", "script", "iframe", "style", "input"].includes(tag)); return new Node(tag); },
    createDocumentFragment() { return new Node("fragment"); },
    createTextNode(text) { const node = new Node("text"); node.textContent = text; return node; },
  };
  vm.runInNewContext(source, { window, document });
  const root = new Node("root");
  const nodes = (node = root) => [node, ...node.children.flatMap(nodes)];
  return { root, nodes, window, render: text => window.JarvisMarkdown.render(root, text) };
}

test("headings, nested emphasis, numbered/nested lists and quotes render as semantic DOM", () => {
  const f = fixture();
  f.render("# Заголовок\n\n**Жирный и *курсив***, ~~убрано~~.\n\n3. Первый\n4. Второй\n   - Вложенный\n\n> Цитата\n\n---");
  const tags = f.nodes().map(node => node.tag);
  for (const tag of ["h1", "strong", "em", "del", "ol", "li", "ul", "blockquote", "hr"]) assert.ok(tags.includes(tag), tag);
  assert.equal(f.nodes().find(node => node.tag === "ol").start, 3);
  assert.equal(f.nodes().find(node => node.tag === "strong").textContent, "Жирный и курсив");
  assert.equal(f.nodes().find(node => node.tag === "blockquote").textContent, "Цитата");
});

test("fenced and inline code preserve literal markup and streaming unclosed fences", () => {
  const f = fixture();
  const code = '<img src="https://example.invalid/x" onerror="alert(1)">\n**literal**';
  f.render(`Код: \`**inline**\`\n\n\`\`\`html\n${code}`);
  assert.equal(f.nodes().filter(node => node.tag === "code")[0].textContent, "**inline**");
  assert.equal(f.nodes().find(node => node.tag === "pre").textContent, code);
  f.render(`Код: \`**inline**\`\n\n\`\`\`html\n${code}\n\`\`\``);
  assert.equal(f.nodes().find(node => node.tag === "pre").textContent, code);
});

test("model HTML, links, images and encoded destinations never create active elements", () => {
  const f = fixture();
  f.render('<script>alert(1)</script>\n\n<img src="/api/anything" onerror="alert(2)">\n\n[Click](javascript:alert%281%29) [Local](/api/anything)\n\n![external](https://example.invalid/private) ![local](/api/anything)\n\n&lt;iframe&gt; &#60;svg&#62; &amp;');
  assert.ok(f.root.textContent.includes('<script>alert(1)</script>'));
  assert.ok(f.root.textContent.includes('<img src="/api/anything"'));
  assert.ok(f.root.textContent.includes("javascript:alert%281%29"));
  assert.ok(f.root.textContent.includes("![external](https://example.invalid/private)"));
  assert.ok(f.root.textContent.includes("<iframe> <svg> &"));
  assert.ok(!f.nodes().some(node => node.attributes.src || node.attributes.href));
});

test("GFM tables and task lists do not introduce interactive controls", () => {
  const f = fixture();
  f.render("| Имя | Число |\n| :--- | ---: |\n| **Тест** | 42 |\n\n- [x] Готово\n- [ ] Позже");
  const table = f.nodes().find(node => node.tag === "table");
  assert.ok(table);
  assert.equal(f.nodes().filter(node => node.tag === "th")[0].attributes.scope, "col");
  assert.equal(f.nodes().filter(node => node.tag === "td")[1].className, "md-align-right");
  assert.ok(f.root.textContent.includes("☑ Готово"));
  assert.ok(f.root.textContent.includes("☐ Позже"));
});

test("unfinished emphasis can become formatted text without retaining stale DOM", () => {
  const f = fixture();
  f.render("**Начало");
  assert.equal(f.root.textContent, "**Начало");
  assert.equal(f.nodes().some(node => node.tag === "strong"), false);
  f.render("**Начало** продолжение");
  assert.equal(f.root.textContent, "Начало продолжение");
  assert.equal(f.nodes().filter(node => node.tag === "strong").length, 1);
  const replacements = f.root.replacements;
  f.render("**Начало** продолжение");
  assert.equal(f.root.replacements, replacements);
});

test("excessive nesting and parser failure retain the original reply as plain text", () => {
  const f = fixture();
  const deep = "> ".repeat(40) + "Текст";
  f.render(deep);
  assert.equal(f.root.textContent, deep);
  f.window.marked = { lexer() { throw Error("Injected lexer failure"); } };
  f.render("**Сохранённый ответ**");
  assert.equal(f.root.textContent, "**Сохранённый ответ**");
});
