"use strict";

// Parse math before Markdown escapes consume its delimiters. Code stays with
// Marked's code tokenizers. Render only detached MathML; no HTML string sinks.
window.JarvisMath = (() => {
  const cache = new Map();
  const maxLength = 2048;
  // MathML Core supports mathvariant="normal"; use Unicode mathematical
  // alphabets for other variants so Chromium preserves distinctions like ℝ/ℱ.
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρςστυφχψω∂∇ϵϑϰϕϱϖ";
  const variants = {
    "bold": "𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗𝚨𝚩𝚪𝚫𝚬𝚭𝚮𝚯𝚰𝚱𝚲𝚳𝚴𝚵𝚶𝚷𝚸𝚺𝚻𝚼𝚽𝚾𝚿𝛀𝛂𝛃𝛄𝛅𝛆𝛇𝛈𝛉𝛊𝛋𝛌𝛍𝛎𝛏𝛐𝛑𝛒𝛓𝛔𝛕𝛖𝛗𝛘𝛙𝛚𝛛𝛁𝛜𝛝𝛞𝛟𝛠𝛡",
    "italic": "𝐴𝐵𝐶𝐷𝐸𝐹𝐺𝐻𝐼𝐽𝐾𝐿𝑀𝑁𝑂𝑃𝑄𝑅𝑆𝑇𝑈𝑉𝑊𝑋𝑌𝑍𝑎𝑏𝑐𝑑𝑒𝑓𝑔ℎ𝑖𝑗𝑘𝑙𝑚𝑛𝑜𝑝𝑞𝑟𝑠𝑡𝑢𝑣𝑤𝑥𝑦𝑧0123456789𝛢𝛣𝛤𝛥𝛦𝛧𝛨𝛩𝛪𝛫𝛬𝛭𝛮𝛯𝛰𝛱𝛲𝛴𝛵𝛶𝛷𝛸𝛹𝛺𝛼𝛽𝛾𝛿𝜀𝜁𝜂𝜃𝜄𝜅𝜆𝜇𝜈𝜉𝜊𝜋𝜌𝜍𝜎𝜏𝜐𝜑𝜒𝜓𝜔𝜕𝛻𝜖𝜗𝜘𝜙𝜚𝜛",
    "bold-italic": "𝑨𝑩𝑪𝑫𝑬𝑭𝑮𝑯𝑰𝑱𝑲𝑳𝑴𝑵𝑶𝑷𝑸𝑹𝑺𝑻𝑼𝑽𝑾𝑿𝒀𝒁𝒂𝒃𝒄𝒅𝒆𝒇𝒈𝒉𝒊𝒋𝒌𝒍𝒎𝒏𝒐𝒑𝒒𝒓𝒔𝒕𝒖𝒗𝒘𝒙𝒚𝒛0123456789𝜜𝜝𝜞𝜟𝜠𝜡𝜢𝜣𝜤𝜥𝜦𝜧𝜨𝜩𝜪𝜫𝜬𝜮𝜯𝜰𝜱𝜲𝜳𝜴𝜶𝜷𝜸𝜹𝜺𝜻𝜼𝜽𝜾𝜿𝝀𝝁𝝂𝝃𝝄𝝅𝝆𝝇𝝈𝝉𝝊𝝋𝝌𝝍𝝎𝝏𝜵𝝐𝝑𝝒𝝓𝝔𝝕",
    "double-struck": "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ𝕒𝕓𝕔𝕕𝕖𝕗𝕘𝕙𝕚𝕛𝕜𝕝𝕞𝕟𝕠𝕡𝕢𝕣𝕤𝕥𝕦𝕧𝕨𝕩𝕪𝕫𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡ΑΒℾΔΕΖΗΘΙΚΛΜΝΞΟℿΡΣΤΥΦΧΨΩαβℽδεζηθικλμνξοℼρςστυφχψω∂∇ϵϑϰϕϱϖ",
    "script": "𝒜ℬ𝒞𝒟ℰℱ𝒢ℋℐ𝒥𝒦ℒℳ𝒩𝒪𝒫𝒬ℛ𝒮𝒯𝒰𝒱𝒲𝒳𝒴𝒵𝒶𝒷𝒸𝒹ℯ𝒻ℊ𝒽𝒾𝒿𝓀𝓁𝓂𝓃ℴ𝓅𝓆𝓇𝓈𝓉𝓊𝓋𝓌𝓍𝓎𝓏0123456789ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρςστυφχψω∂∇ϵϑϰϕϱϖ",
    "bold-script": "𝓐𝓑𝓒𝓓𝓔𝓕𝓖𝓗𝓘𝓙𝓚𝓛𝓜𝓝𝓞𝓟𝓠𝓡𝓢𝓣𝓤𝓥𝓦𝓧𝓨𝓩𝓪𝓫𝓬𝓭𝓮𝓯𝓰𝓱𝓲𝓳𝓴𝓵𝓶𝓷𝓸𝓹𝓺𝓻𝓼𝓽𝓾𝓿𝔀𝔁𝔂𝔃0123456789ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρςστυφχψω∂∇ϵϑϰϕϱϖ",
    "fraktur": "𝔄𝔅ℭ𝔇𝔈𝔉𝔊ℌℑ𝔍𝔎𝔏𝔐𝔑𝔒𝔓𝔔ℜ𝔖𝔗𝔘𝔙𝔚𝔛𝔜ℨ𝔞𝔟𝔠𝔡𝔢𝔣𝔤𝔥𝔦𝔧𝔨𝔩𝔪𝔫𝔬𝔭𝔮𝔯𝔰𝔱𝔲𝔳𝔴𝔵𝔶𝔷0123456789ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρςστυφχψω∂∇ϵϑϰϕϱϖ",
    "bold-fraktur": "𝕬𝕭𝕮𝕯𝕰𝕱𝕲𝕳𝕴𝕵𝕶𝕷𝕸𝕹𝕺𝕻𝕼𝕽𝕾𝕿𝖀𝖁𝖂𝖃𝖄𝖅𝖆𝖇𝖈𝖉𝖊𝖋𝖌𝖍𝖎𝖏𝖐𝖑𝖒𝖓𝖔𝖕𝖖𝖗𝖘𝖙𝖚𝖛𝖜𝖝𝖞𝖟0123456789ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρςστυφχψω∂∇ϵϑϰϕϱϖ",
    "sans-serif": "𝖠𝖡𝖢𝖣𝖤𝖥𝖦𝖧𝖨𝖩𝖪𝖫𝖬𝖭𝖮𝖯𝖰𝖱𝖲𝖳𝖴𝖵𝖶𝖷𝖸𝖹𝖺𝖻𝖼𝖽𝖾𝖿𝗀𝗁𝗂𝗃𝗄𝗅𝗆𝗇𝗈𝗉𝗊𝗋𝗌𝗍𝗎𝗏𝗐𝗑𝗒𝗓𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρςστυφχψω∂∇ϵϑϰϕϱϖ",
    "bold-sans-serif": "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵𝝖𝝗𝝘𝝙𝝚𝝛𝝜𝝝𝝞𝝟𝝠𝝡𝝢𝝣𝝤𝝥𝝦𝝨𝝩𝝪𝝫𝝬𝝭𝝮𝝰𝝱𝝲𝝳𝝴𝝵𝝶𝝷𝝸𝝹𝝺𝝻𝝼𝝽𝝾𝝿𝞀𝞁𝞂𝞃𝞄𝞅𝞆𝞇𝞈𝞉𝝯𝞊𝞋𝞌𝞍𝞎𝞏",
    "sans-serif-italic": "𝘈𝘉𝘊𝘋𝘌𝘍𝘎𝘏𝘐𝘑𝘒𝘓𝘔𝘕𝘖𝘗𝘘𝘙𝘚𝘛𝘜𝘝𝘞𝘟𝘠𝘡𝘢𝘣𝘤𝘥𝘦𝘧𝘨𝘩𝘪𝘫𝘬𝘭𝘮𝘯𝘰𝘱𝘲𝘳𝘴𝘵𝘶𝘷𝘸𝘹𝘺𝘻0123456789ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρςστυφχψω∂∇ϵϑϰϕϱϖ",
    "sans-serif-bold-italic": "𝘼𝘽𝘾𝘿𝙀𝙁𝙂𝙃𝙄𝙅𝙆𝙇𝙈𝙉𝙊𝙋𝙌𝙍𝙎𝙏𝙐𝙑𝙒𝙓𝙔𝙕𝙖𝙗𝙘𝙙𝙚𝙛𝙜𝙝𝙞𝙟𝙠𝙡𝙢𝙣𝙤𝙥𝙦𝙧𝙨𝙩𝙪𝙫𝙬𝙭𝙮𝙯0123456789𝞐𝞑𝞒𝞓𝞔𝞕𝞖𝞗𝞘𝞙𝞚𝞛𝞜𝞝𝞞𝞟𝞠𝞢𝞣𝞤𝞥𝞦𝞧𝞨𝞪𝞫𝞬𝞭𝞮𝞯𝞰𝞱𝞲𝞳𝞴𝞵𝞶𝞷𝞸𝞹𝞺𝞻𝞼𝞽𝞾𝞿𝟀𝟁𝟂𝟃𝞩𝟄𝟅𝟆𝟇𝟈𝟉",
    "monospace": "𝙰𝙱𝙲𝙳𝙴𝙵𝙶𝙷𝙸𝙹𝙺𝙻𝙼𝙽𝙾𝙿𝚀𝚁𝚂𝚃𝚄𝚅𝚆𝚇𝚈𝚉𝚊𝚋𝚌𝚍𝚎𝚏𝚐𝚑𝚒𝚓𝚔𝚕𝚖𝚗𝚘𝚙𝚚𝚛𝚜𝚝𝚞𝚟𝚠𝚡𝚢𝚣𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρςστυφχψω∂∇ϵϑϰϕϱϖ"
  };
  function normalizeAlphabet(node) {
    const variant = variants[node.getAttribute("mathvariant")];
    if (!variant || !["mi", "mn", "mtext"].includes(node.localName)) return;
    const letters = [...variant];
    const original = node.textContent;
    node.textContent = [...original].map(ch => {
      const index = alphabet.indexOf(ch);
      return index < 0 ? ch : letters[index];
    }).join("");
    if (node.textContent !== original) node.setAttribute("mathvariant", "normal");
  }

  const mathTags = new Set("math semantics annotation mrow mi mn mo mtext mspace mfrac msqrt mroot msup msub msubsup munder mover munderover mtable mtr mtd mstyle mpadded menclose mphantom mmultiscripts mprescripts none".split(" "));
  function escaped(source, index) {
    let count = 0;
    while (index > 0 && source[--index] === "\\") count++;
    return count % 2 === 1;
  }
  function token(source, block = false) {
    const open = ["\\[", "$$", "\\(", "$"].find(value => source.startsWith(value));
    if (!open || (block && open !== "\\[" && open !== "$$")) return;
    const close = ({ "\\[": "\\]", "$$": "$$", "\\(": "\\)", "$": "$" })[open];
    const display = open === "\\[" || open === "$$";
    if (open === "$" && (!source[1] || /\s|\$/.test(source[1]))) return;
    let braces = 0;
    for (let index = open.length; index < source.length; index++) {
      if (open === "$" && source[index] === "\n") return;
      if (!escaped(source, index)) {
        if (source.startsWith(close, index) && braces === 0) {
          if (open === "$" && (/\s/.test(source[index - 1]) || /\d/.test(source[index + 1] || ""))) continue;
          return { type: "math", raw: source.slice(0, index + close.length), text: source.slice(open.length, index), display };
        }
        if (source[index] === "{") braces++;
        if (source[index] === "}") braces = Math.max(0, braces - 1);
      }
    }
    // A plain dollar amount is not an unfinished formula.
    if (open === "$") return;
    return { type: "math", raw: source, text: source.slice(open.length), display, pending: true };
  }
  const lexer = new window.marked.Marked({ gfm: true });
  lexer.use({ extensions: [
    { name: "math", level: "block", start: source => source.search(/(?:^|\n)(?:\\\[|\$\$)/), tokenizer: source => token(source, true) },
    { name: "math", level: "inline", start: source => source.search(/\\[([]|\$/), tokenizer: source => token(source) },
  ] });
  function append(parent, item, budget) {
    const raw = () => parent.append(document.createTextNode(item.raw));
    if (item.pending || !window.katex || item.text.length > maxLength || ++budget.count > 64 || (budget.characters += item.text.length) > 16384) return raw();
    // Bound source nesting before entering the synchronous math parser.
    let depth = 0;
    for (let index = 0; index < item.text.length; index++) {
      if (escaped(item.text, index)) continue;
      if (item.text[index] === "{" && ++depth > 32) return raw();
      if (item.text[index] === "}") depth--;
    }
    const key = `${item.display ? "block" : "inline"}:${item.text}`;
    let node = cache.get(key);
    if (node === undefined) {
      try {
        node = document.createElement("span");
        node.className = item.display ? "math-display" : "math-inline";
        window.katex.render(item.text, node, { output: "mathml", displayMode: item.display, trust: false, strict: "error", throwOnError: true, maxExpand: 100, maxSize: 10, macros: {} });
        const descendants = [...node.querySelectorAll("*")];
        if (descendants.length > 512) throw Error("Math size limit");
        for (const child of descendants) {
          normalizeAlphabet(child);
          if (!(child.namespaceURI === "http://www.w3.org/1998/Math/MathML" && mathTags.has(child.localName)) && !(child.namespaceURI === "http://www.w3.org/1999/xhtml" && child.localName === "span")) throw Error("Unexpected math node");
          for (const attribute of child.attributes) {
            if (/^(?:on|href$|src$|style$)|:href$/i.test(attribute.name)) throw Error("Active math attribute");
          }
        }
      } catch (_) { node = null; }
      cache.set(key, node);
      if (cache.size > 64) cache.delete(cache.keys().next().value);
    } else {
      cache.delete(key); cache.set(key, node);
    }
    if (node) parent.append(node.cloneNode(true));
    else raw();
  }
  return { lexer, append };
})();
