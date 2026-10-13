"use strict";

// Marked supplies tokens only. Model text never enters an HTML parsing sink.
window.JarvisMarkdown = (() => {
  const previous = new WeakMap();
  const pending = new WeakMap();
  let mathBudget;
  const entities = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: "\u00a0" };
  function decode(text) {
    return text.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (raw, name) => {
      if (name[0] !== "#") return entities[name.toLowerCase()] ?? raw;
      const value = name[1].toLowerCase() === "x" ? parseInt(name.slice(2), 16) : Number(name.slice(1));
      return value > 0 && value <= 0x10ffff && !(value >= 0xd800 && value <= 0xdfff) ? String.fromCodePoint(value) : "\ufffd";
    });
  }
  function text(parent, value) { parent.append(document.createTextNode(value)); }
  function element(parent, tag) {
    const child = document.createElement(tag);
    parent.append(child);
    return child;
  }
  function inline(parent, tokens, depth) {
    if (depth > 32) throw new Error("Markdown nesting limit");
    for (const token of tokens || []) {
      switch (token.type) {
        case "math": window.JarvisMath.append(parent, token, mathBudget); break;
        case "strong": case "em": case "del":
          inline(element(parent, token.type), token.tokens, depth + 1); break;
        case "codespan":
          element(parent, "code").textContent = token.text; break;
        case "br": element(parent, "br"); break;
        case "checkbox": text(parent, token.checked ? "☑ " : "☐ "); break;
        case "link": {
          // Destinations stay inert: model links cannot navigate the native host,
          // invoke a local endpoint, or transmit data by loading a resource.
          const label = element(parent, "span");
          label.className = "md-link";
          inline(label, token.tokens, depth + 1);
          text(label, ` (${decode(token.href)})`);
          break;
        }
        case "image": case "html":
          text(parent, token.raw); break;
        case "text":
          if (token.tokens) inline(parent, token.tokens, depth + 1);
          else text(parent, decode(token.text));
          break;
        case "escape": text(parent, token.text); break;
        default: text(parent, token.raw || "");
      }
    }
  }
  function blocks(parent, tokens, depth = 0, tight = false) {
    if (depth > 32) throw new Error("Markdown nesting limit");
    for (const token of tokens) {
      switch (token.type) {
        case "math": window.JarvisMath.append(parent, token, mathBudget); break;
        case "space": break;
        case "checkbox": text(parent, token.checked ? "☑ " : "☐ "); break;
        case "heading": inline(element(parent, `h${token.depth}`), token.tokens, depth + 1); break;
        case "paragraph": case "text":
          inline(tight ? parent : element(parent, "p"), token.tokens || [{ type: "text", text: token.text }], depth + 1); break;
        case "code": element(element(parent, "pre"), "code").textContent = token.text; break;
        case "blockquote": blocks(element(parent, "blockquote"), token.tokens, depth + 1); break;
        case "hr": element(parent, "hr"); break;
        case "list": {
          const list = element(parent, token.ordered ? "ol" : "ul");
          if (token.ordered && Number.isSafeInteger(token.start)) list.start = token.start;
          for (const item of token.items) {
            const li = element(list, "li");
            blocks(li, item.tokens, depth + 1, !token.loose);
          }
          break;
        }
        case "table": {
          const wrapper = element(parent, "div");
          wrapper.className = "md-table";
          wrapper.tabIndex = 0;
          wrapper.setAttribute("role", "region");
          wrapper.setAttribute("aria-label", "Таблица");
          const table = element(wrapper, "table");
          const row = element(element(table, "thead"), "tr");
          function cell(parentRow, tag, data, index) {
            const node = element(parentRow, tag);
            if (["left", "center", "right"].includes(token.align[index])) node.className = `md-align-${token.align[index]}`;
            if (tag === "th") node.setAttribute("scope", "col");
            inline(node, data.tokens, depth + 1);
          }
          token.header.forEach((data, i) => cell(row, "th", data, i));
          const body = element(table, "tbody");
          for (const data of token.rows) {
            const tr = element(body, "tr");
            data.forEach((value, i) => cell(tr, "td", value, i));
          }
          break;
        }
        default: text(element(parent, "p"), token.raw || "");
      }
    }
  }
  function render(container, source, { streaming = false } = {}) {
    const queued = pending.get(container);
    if (streaming && window.setTimeout && /\\[([]|\$/.test(source)) {
      if (queued) { queued.source = source; return; }
      const update = { source };
      update.timer = window.setTimeout(() => { pending.delete(container); render(container, update.source); }, 120);
      pending.set(container, update);
      return;
    }
    if (queued) { window.clearTimeout(queued.timer); pending.delete(container); }
    if (previous.get(container) === source) return;
    mathBudget = { count: 0, characters: 0 };
    const fragment = document.createDocumentFragment();
    try {
      blocks(fragment, window.JarvisMath ? window.JarvisMath.lexer.lexer(source) : window.marked.lexer(source, { gfm: true }));
      container.replaceChildren(fragment);
    } catch (_) {
      // Interrupted syntax, excessive nesting or parser failures retain the text.
      container.textContent = source;
    }
    previous.set(container, source);
  }
  return { render };
})();
