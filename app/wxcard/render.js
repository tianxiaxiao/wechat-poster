/* ============================================================
 * 微信公众号「卡片模式」HTML 生成器
 * 逐函数移植自 D:\多平台图文生产系统\md2card-local\js\app.js 的
 * 「复制到公众号」管线（对标官方 processClipboardContent + juice.inlineContent）：
 *   markdown → 卡片结构(card > card-content > card-content-inner)
 *   → 按选择器匹配把 baseCss+主题CSS 逐条内联进 style
 *   → 伪元素实体化 → var()/calc() 解析 → 微信兼容化(div→section 等)
 *   → 背景拆 longhand + 渐变纯色兜底 → 收尾规范化
 * 主题数据来自 md2card 的 5 套「公众号专用样式」(data.json)。
 *
 * 用法：node render.js <input.md> <output.html> [themeId]
 *   themeId ∈ bytedance(字节范) | alibaba(阿里橙) | notebook(笔记本)
 *           | business(商务简报) | japanese-magazine(日本杂志)，默认 alibaba
 * ============================================================ */
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");
const marked = require("./marked.min.js");

const dom = new JSDOM("<!doctype html><html><body></body></html>");
const document = dom.window.document;
// hljs 的 DOM 模式需要全局 window/document（Node 22 的 navigator 是只读 getter，需兼容处理）
global.window = dom.window;
global.document = dom.window.document;
try { global.navigator = dom.window.navigator; } catch (e) { /* Node 自带 navigator，忽略 */ }
const katex = require("./katex.min.js");
let hljs = null;
try { hljs = require("./hljs.min.js"); } catch (e) { hljs = null; }

/* ---------------- 数据 ---------------- */
const D = JSON.parse(fs.readFileSync(path.join(__dirname, "data.json"), "utf8"));
const BASE_CSS = D.baseCss || "";
const MP_THEMES = D.mpThemes || [];
function mpThemeById(id) { return MP_THEMES.find(t => t.id === id) || null; }

/* 公众号模式下的排版参数（与编辑器默认值一致：字号标准档、行高 175%、左对齐） */
const SPACING = 4;               // SPACING_LEVELS[1]（默认 fontLevel=1）
const LINE_HEIGHT = 1.75;        // state.lineHeight 175 / 100
const LETTER_SPACING = 0;
const ALIGN = "left";

/* ---------------- 工具（移植自 app.js） ---------------- */
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function attrDecode(s) { return String(s).replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">"); }
function attrEncode(s) { return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;"); }

/* 微信不内联的声明（官方产物里没有这些；transform/box-shadow/position 官方保留，勿删） */
const WECHAT_STRIP_DECLS = [
  "filter", "backdrop-filter", "mix-blend-mode", "clip-path", "mask", "mask-image",
  "transition", "transition-property", "transition-duration", "transition-delay", "transition-timing-function",
  "animation", "animation-name", "animation-duration", "animation-delay", "animation-iteration-count",
  "animation-direction", "animation-fill-mode", "animation-timing-function",
  "will-change", "content", "quotes", "counter-reset", "counter-increment",
  "cursor", "pointer-events", "user-select", "resize", "appearance", "aspect-ratio",
  "outline", "outline-width", "outline-style", "outline-color", "outline-offset",
  "contain", "backface-visibility", "perspective"
];
const NOOP_BY_PROP = {
  "box-shadow": ["none"], "text-shadow": ["none"], "background-image": ["none"],
  "background-color": ["rgba(0, 0, 0, 0)", "transparent"], "float": ["none"], "clear": ["none"]
};
function isNoopDecl(p, val) {
  if (!val || val === "initial") return true;
  const list = NOOP_BY_PROP[p];
  return !!(list && list.includes(val));
}
/* url() 相对/本地路径微信抓不到；data:/http(s) 保留 */
function urlProblem(val) {
  const m = String(val).match(/url\((['"]?)([^'")]+)\1\)/gi);
  if (!m) return false;
  return m.some(u => {
    const inner = u.replace(/^url\((['"]?)/i, "").replace(/['"]?\)$/i, "").trim();
    return !/^(data:|https?:\/\/)/i.test(inner);
  });
}
/* 极简 CSS 声明解析：按顶层分号切分（括号内分号不算），保留 var()/calc() 原样 */
function parseCssDecls(body) {
  const out = [];
  let depth = 0, buf = "";
  const push = () => {
    const d = buf.trim(); buf = "";
    if (!d) return;
    const i = d.indexOf(":");
    if (i < 0) return;
    out.push([d.slice(0, i).trim(), d.slice(i + 1).trim()]);
  };
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (ch === "(") depth++;
    else if (ch === ")") depth = Math.max(0, depth - 1);
    if (ch === ";" && depth === 0) { push(); continue; }
    buf += ch;
  }
  push();
  return out;
}
function parseCssRules(text) {
  const rules = [];
  const clean = String(text).replace(/\/\*[\s\S]*?\*\//g, "");
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(clean))) {
    const sel = m[1].trim();
    if (!sel || sel.startsWith("@")) continue;
    const decls = parseCssDecls(m[2]);
    if (decls.length) rules.push({ sel, decls });
  }
  return rules;
}
/* 选择器权重（id, class/属性/伪类, 类型） */
function cssSpecificity(sel) {
  const s = sel.replace(/\\./g, " ");
  const ids = (s.match(/#[\w-]+/g) || []).length;
  const cls = (s.match(/\.[\w-]+|\[[^\]]*\]|:(?!:)[\w-]+(\([^)]*\))?/g) || []).length;
  const type = (s.match(/(^|[\s>+~])([a-zA-Z][\w-]*)/g) || []).length;
  return ids * 10000 + cls * 100 + type;
}
/* 全部规则（baseCss + 主题 css + 注入的排版覆盖），按权重排序；伪元素规则带 pseudo 标记 */
function cardRules(cssText) {
  const out = [];
  parseCssRules(cssText).forEach((r, idx) => {
    r.sel.split(",").forEach(part => {
      let sel = part.trim();
      if (!sel) return;
      let pseudo = "";
      const pm = sel.match(/::?(before|after)\b/i);
      if (pm) { pseudo = "::" + pm[1].toLowerCase(); sel = sel.replace(pm[0], "").trim(); }
      if (!sel) return;
      out.push({ sel, pseudo, decls: r.decls, spec: cssSpecificity(sel), order: idx });
    });
  });
  out.sort((a, b) => (a.spec - b.spec) || (a.order - b.order));
  return out;
}

/* ---------------- 微信背景兼容：渐变/图片 → longhand + 纯色兜底 ---------------- */
const BG_PROPS = ["background", "background-color", "background-image", "background-repeat", "background-size", "background-position"];
function splitTopLevel(str, sep) {
  const out = []; let depth = 0, cur = "";
  for (const ch of String(str)) {
    if (ch === "(") depth++;
    else if (ch === ")") depth = Math.max(0, depth - 1);
    if (ch === sep && depth === 0) { out.push(cur); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur);
  return out.map(s => s.trim()).filter(Boolean);
}
const NAMED_COLORS = { white: "#ffffff", black: "#000000", red: "#ff0000", blue: "#0000ff", green: "#008000", gray: "#808080", grey: "#808080", transparent: "transparent" };
function cssColorToHex(c) {
  const s = String(c == null ? "" : c).trim();
  const named = NAMED_COLORS[s.toLowerCase()];
  if (named) return named;
  const m3 = /^#([0-9a-f]{3})$/i.exec(s);
  if (m3) return "#" + m3[1].split("").map(x => x + x).join("").toLowerCase();
  if (/^#[0-9a-f]{6}$/i.test(s)) return s.toLowerCase();
  const m = /^rgba?\(([^)]+)\)$/i.exec(s);
  if (m) {
    const p = m[1].split(/[,/\s]+/).filter(Boolean);
    const r = parseFloat(p[0]), g = parseFloat(p[1]), b = parseFloat(p[2]);
    let a = p.length > 3 ? parseFloat(p[3]) : 1;
    if (!isFinite(r) || !isFinite(g) || !isFinite(b)) return s;
    if (!isFinite(a)) a = 1;
    if (a <= 0) return "transparent";
    const hx = n => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0");
    return "#" + hx(r * a + 255 * (1 - a)) + hx(g * a + 255 * (1 - a)) + hx(b * a + 255 * (1 - a));
  }
  return s;
}
function findGradient(val) {
  const m = /(?:repeating-)?(?:linear|radial|conic)-gradient\(/i.exec(val);
  if (!m) return "";
  let depth = 0, i = val.indexOf("(", m.index);
  for (; i < val.length; i++) {
    if (val[i] === "(") depth++;
    else if (val[i] === ")") { depth--; if (!depth) { i++; break; } }
  }
  return val.slice(m.index, i);
}
function gradientFallbackColor(grad) {
  const inner = grad.slice(grad.indexOf("(") + 1, grad.lastIndexOf(")"));
  const parts = splitTopLevel(inner, ",");
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (i === 0 && /^(to\b|[-+]?[\d.]+(?:deg|grad|rad|turn)\b|circle\b|ellipse\b|closest-|farthest-|at\b)/i.test(p)) continue;
    const cm = /(#[0-9a-f]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\)|[a-z]+)/i.exec(p);
    if (cm) {
      const c = cssColorToHex(cm[1]);
      if (/^#/.test(c)) return c;
    }
  }
  return "";
}
function wechatBgPairs(prop, val) {
  const v = String(val == null ? "" : val).trim();
  if (!v || /^(none|initial)$/i.test(v)) return [];
  if (prop === "background-color") return [["background-color", cssColorToHex(v)]];
  if (prop === "background-image") {
    if (urlProblem(v)) return [];
    const g = findGradient(v);
    if (!g) return [["background-image", v]];
    const out = [];
    const fb = gradientFallbackColor(g);
    if (fb && fb !== "transparent") out.push(["background-color", fb]);
    out.push(["background-image", v]);
    return out;
  }
  if (prop !== "background") return [[prop, v]];
  const grad = findGradient(v);
  if (grad) {
    const out = [];
    const fb = gradientFallbackColor(grad);
    if (fb && fb !== "transparent") out.push(["background-color", fb]);
    out.push(["background-image", grad]);
    if (/no-repeat/i.test(v)) out.push(["background-repeat", "no-repeat"]);
    return out;
  }
  const urls = v.match(/url\([^)]*\)/gi);
  if (urls && urls.length) {
    if (urlProblem(v)) return [];
    const out = [["background-image", urls.join(", ")]];
    if (/no-repeat/i.test(v)) out.push(["background-repeat", "no-repeat"]);
    if (/\bcover\b/i.test(v)) out.push(["background-size", "cover"]);
    else if (/\bcontain\b/i.test(v)) out.push(["background-size", "contain"]);
    if (/\bcenter\b/i.test(v)) out.push(["background-position", "center"]);
    return out;
  }
  return [["background-color", cssColorToHex(v)]];
}
function dedupeDecls(list) {
  const last = new Map();
  list.forEach((it, i) => last.set(it[0], i));
  return list.filter((it, i) => last.get(it[0]) === i);
}

/* ---------------- 按选择器匹配内联样式 ---------------- */
function inlineStylesForWechat(root, rules) {
  const clone = root.cloneNode(true);
  const srcAll = [root, ...root.querySelectorAll("*")];
  const dstAll = [clone, ...clone.querySelectorAll("*")];
  dstAll.forEach((el, i) => {
    const s = srcAll[i];
    const decls = new Map();
    if (s && s.nodeType === 1) {
      for (const r of rules) {
        if (r.pseudo) continue;
        let hit = false;
        try { hit = s.matches(r.sel); } catch (e) { hit = false; }
        if (!hit) continue;
        r.decls.forEach(([p, v]) => decls.set(p, v));
      }
    }
    parseCssDecls(el.getAttribute("style") || "").forEach(([p, v]) => decls.set(p, v));
    const list = [];
    decls.forEach((v, p) => {
      const isCustom = p.startsWith("--");
      if (!p) return;
      if (!isCustom && p.startsWith("-")) return;
      if (!isCustom && WECHAT_STRIP_DECLS.includes(p)) return;
      const val = String(v == null ? "" : v).trim().replace(/\s+/g, " ");
      if (isNoopDecl(p, val)) return;
      if (BG_PROPS.includes(p)) {
        wechatBgPairs(p, val).forEach(([pp, vv]) => { if (vv) list.push([pp, vv]); });
        return;
      }
      if (urlProblem(val)) return;
      list.push([p, val]);
    });
    const out = dedupeDecls(list).map(([p, v]) => `${p}:${v};`).join("");
    if (out) el.setAttribute("style", out); else el.removeAttribute("style");
    el.removeAttribute("class");
  });
  return clone;
}

/* ---------------- 伪元素实体化（juice inlinePseudoElements 的效果） ----------------
   content 只能离线解析字面量与 attr(x)（counter 无法离线求值，跳过） */
function pseudoResolveContent(c, el) {
  const t = String(c == null ? "" : c).trim();
  if (!t || t === "none" || t === "normal" || /^url\(/i.test(t)) return "";
  const am = /^attr\(([\w-]+)\)$/i.exec(t);
  if (am) return el.getAttribute(am[1]) || "";
  if (/counter/i.test(t)) return "";
  return t.replace(/^["']/, "").replace(/["']$/, "");
}
function materializePseudos(srcRoot, cloneRoot, rules) {
  const srcAll = [srcRoot, ...srcRoot.querySelectorAll("*")];
  const dstAll = [cloneRoot, ...cloneRoot.querySelectorAll("*")];
  const jobs = [];
  dstAll.forEach((dst, i) => {
    const src = srcAll[i];
    if (!dst || dst.nodeType !== 1 || !src || src.nodeType !== 1) return;
    ["::before", "::after"].forEach(pseudo => {
      const decls = new Map();
      for (const r of rules) {
        if (r.pseudo !== pseudo) continue;
        let hit = false;
        try { hit = src.matches(r.sel); } catch (e) { hit = false; }
        if (!hit) continue;
        r.decls.forEach(([p, v]) => decls.set(p, v));
      }
      if (!decls.size) return;
      const contentDecl = decls.get("content");
      const text = pseudoResolveContent(contentDecl, src);
      const list = [];
      decls.forEach((v, p) => {
        if (p === "content") return;
        if (!p || (p.startsWith("-") && !p.startsWith("--"))) return;
        if (WECHAT_STRIP_DECLS.includes(p)) return;
        const val = String(v == null ? "" : v).trim().replace(/\s+/g, " ");
        if (isNoopDecl(p, val)) return;
        if (BG_PROPS.includes(p)) {
          wechatBgPairs(p, val).forEach(([pp, vv]) => { if (vv) list.push([pp, vv]); });
          return;
        }
        if (urlProblem(val)) return;
        list.push([p, val]);
      });
      const style = dedupeDecls(list).map(([p, v]) => `${p}:${v};`).join("");
      if (!style && !text) return;
      jobs.push({ dst, pseudo, style, text });
    });
  });
  jobs.forEach(({ dst, pseudo, style, text }) => {
    const span = document.createElement("span");
    if (style) span.setAttribute("style", style);
    if (text) span.textContent = text;
    if (pseudo === "::before") dst.insertBefore(span, dst.firstChild); else dst.appendChild(span);
  });
}

/* ---------------- calc() 求值 与 var() 解析（官方 replaceCssVariables） ---------------- */
function calcEval(inner) {
  const cleaned = String(inner).replace(/\s+/g, "");
  if (!cleaned || cleaned.includes("%")) return null;
  if (cleaned.replace(/-?\d*\.?\d+(px|em|rem)?|[()+\-*/]/g, "")) return null;
  const toks = cleaned.match(/-?\d*\.?\d+(?:px|em|rem)?|[()+\-*/]/g);
  if (!toks) return null;
  let p = 0, ok = true, unit = "px";
  const peek = () => toks[p];
  const num = () => {
    const t = toks[p++];
    if (t === "(") { const v = expr(); if (toks[p] !== ")") ok = false; else p++; return v; }
    if (t === "-") return -num();
    const m = /^(-?\d*\.?\d+)(px|em|rem)?$/.exec(t || "");
    if (!m) { ok = false; return 0; }
    if (m[2]) unit = "px";
    let v = parseFloat(m[1]);
    if (m[2] === "em" || m[2] === "rem") v *= 16;
    return v;
  };
  const term = () => { let v = num(); while (peek() === "*" || peek() === "/") { const op = toks[p++]; const r = num(); v = op === "*" ? v * r : v / r; } return v; };
  const expr = () => { let v = term(); while (peek() === "+" || peek() === "-") { const op = toks[p++]; const rt = term(); v = op === "+" ? v + rt : v - rt; } return v; };
  const val = expr();
  if (!ok || p !== toks.length || !isFinite(val)) return null;
  return Math.round(val * 1000) / 1000 + unit;
}
function resolveVarsAndCalc(html) {
  const vars = {};
  String(html).replace(/style="([^"]*)"/g, (m, s) => {
    parseCssDecls(attrDecode(s)).forEach(([k, v]) => { if (k.startsWith("--")) vars[k] = v; });
    return "";
  });
  const lookup = (name, depth) => {
    if (depth > 6) return null;
    let v = vars[name];
    if (v == null) return null;
    if (v.includes("var(")) v = v.replace(/var\(\s*(--[\w-]+)\s*\)/g, (m, n) => lookup(n, depth + 1) || m);
    return v;
  };
  let out = String(html).replace(/var\(\s*(--[\w-]+)\s*\)/g, (m, n) => lookup(n, 0) || m);
  for (let pass = 0; pass < 5; pass++) {
    let changed = false;
    out = out.replace(/calc\(([^()]*)\)/g, (m, inner) => {
      const r = calcEval(inner);
      if (r == null) return m;
      changed = true; return r;
    });
    if (!changed) break;
  }
  out = out.replace(/style="([^"]*)"/g, (m, s) => {
    const kept = [];
    parseCssDecls(attrDecode(s)).forEach(([p, v]) => {
      if (!p || p.startsWith("--")) return;
      if (!v || v.includes("var(")) return;
      kept.push(`${p}:${v}`);
    });
    return kept.length ? `style="${attrEncode(kept.join(";") + ";")}"` : "";
  });
  return out;
}

/* ---------------- 结构与微信兼容化 ---------------- */
function divToSection(el) {
  if (!el || el.tagName !== "DIV") return el;
  const sec = document.createElement("section");
  for (const a of Array.from(el.attributes)) sec.setAttribute(a.name, a.value);
  while (el.firstChild) sec.appendChild(el.firstChild);
  return sec;
}
function wechatCompatHtml(node) {
  const wrap = document.createElement("div");
  wrap.appendChild(node);
  // <div>/<blockquote> 微信白名单没有 → 换成 <section>
  wrap.querySelectorAll("div, blockquote").forEach(el => {
    const sec = document.createElement("section");
    for (const a of Array.from(el.attributes)) sec.setAttribute(a.name, a.value);
    while (el.firstChild) sec.appendChild(el.firstChild);
    el.replaceWith(sec);
  });
  // <mark> → <span>
  wrap.querySelectorAll("mark").forEach(el => {
    const span = document.createElement("span");
    for (const a of Array.from(el.attributes)) span.setAttribute(a.name, a.value);
    while (el.firstChild) span.appendChild(el.firstChild);
    el.replaceWith(span);
  });
  // <del>/<s> → <span> + line-through
  wrap.querySelectorAll("del, s").forEach(el => {
    const span = document.createElement("span");
    for (const a of Array.from(el.attributes)) span.setAttribute(a.name, a.value);
    while (el.firstChild) span.appendChild(el.firstChild);
    const st = span.getAttribute("style") || "";
    if (!/text-decoration/i.test(st)) span.setAttribute("style", st + "text-decoration:line-through;");
    el.replaceWith(span);
  });
  // 嵌套列表外移
  wrap.querySelectorAll("li > ul, li > ol").forEach(sub => {
    sub.parentElement.insertAdjacentElement("afterend", sub);
  });
  // 去掉空的 <p>
  wrap.querySelectorAll("p").forEach(p => { if (!(p.innerHTML || "").trim()) p.remove(); });
  // <li> 内容包一层 <section>；实体化出来的绝对定位装饰留在 <li> 上
  wrap.querySelectorAll("li").forEach(li => {
    const markers = Array.from(li.children).filter(c =>
      c.tagName === "SPAN" && /position:\s*absolute/i.test(c.getAttribute("style") || ""));
    markers.forEach(m => m.remove());
    const sec = document.createElement("section");
    while (li.firstChild) sec.appendChild(li.firstChild);
    li.appendChild(sec);
    markers.forEach(m => li.insertBefore(m, sec));
  });
  // <hr>（分割线）→ 非自闭合的 <section>：微信服务端会把 <hr> 规范化成自闭合 <hr ... />，
  // 客户端（Windows 版）渲染时会把那个 "/" 当正文字符显示出来（2026-10-06 用户实测）。
  // 换成 <section> 承载同样的样式，既画出横线又不留下任何自闭合标签。
  // 注意：不能覆盖主题已有的 height/background（例如字节范用 height:1px+背景色画线），
  // 只补 font-size:0;line-height:0; 消灭空行高度。
  wrap.querySelectorAll("hr").forEach(hr => {
    const sec = document.createElement("section");
    for (const a of Array.from(hr.attributes)) sec.setAttribute(a.name, a.value);
    let st = (sec.getAttribute("style") || "").trim().replace(/\s+$/, "");
    const hasLine = /height\s*:|background(-color)?\s*:|border/i.test(st);
    const extra = (hasLine ? "" : "border-top:1px solid #eaecef;height:0;") + "font-size:0;line-height:0;";
    sec.setAttribute("style", st + (st && !st.endsWith(";") ? ";" : "") + extra);
    hr.replaceWith(sec);
  });
  // 图片：相对/本地路径 → 文字；data:/http(s) 保留
  wrap.querySelectorAll("img").forEach(img => {
    const src = img.getAttribute("src") || "";
    if (!/^(data:|https?:\/\/)/i.test(src)) {
      const alt = img.getAttribute("alt") || "";
      if (alt) img.replaceWith(document.createTextNode(alt));
      else img.remove();
    }
  });
  return wrap.innerHTML;
}
/* 收尾规范化：补渐变纯色兜底 / 去 !important / font-family 去引号 */
function normalizeWechatAttrs(html) {
  return String(html).replace(/style="([^"]*)"/g, (m, raw) => {
    const decls = parseCssDecls(attrDecode(raw)).map(([p, v]) => {
      let val = String(v == null ? "" : v).replace(/\s*!\s*important\s*$/i, "").trim();
      if (p === "font-family") val = val.replace(/["']/g, "");
      return [p, val];
    }).filter(([p, v]) => p && v);
    const hasColor = decls.some(([p]) => p === "background-color" || p === "background");
    if (!hasColor) {
      const bgDecl = decls.find(([p]) => p === "background-image" || p === "background");
      const g = bgDecl ? findGradient(bgDecl[1]) : "";
      const fb = g ? gradientFallbackColor(g) : "";
      if (fb && fb !== "transparent") {
        const at = decls.findIndex(([p]) => p === "background-image");
        decls.splice(at < 0 ? 0 : at, 0, ["background-color", fb]);
      }
    }
    return decls.length ? `style="${attrEncode(decls.map(([p, v]) => `${p}:${v}`).join(";") + ";")}"` : "";
  });
}
function emptySeparatorHtml() {
  return `<p style="font-size:0;line-height:0;margin:0;">&nbsp;</p>`;
}

/* ---------------- Markdown 渲染（逐字移植 md2card app.js） ---------------- */
marked.setOptions({ gfm: true, breaks: true });

const MATH = [];
function protectMath(src) {
  MATH.length = 0;
  return src
    .replace(/\$\$([\s\S]+?)\$\$/g, (m, tex) => { MATH.push({ tex, display: true }); return `\u0000MATH${MATH.length - 1}\u0000`; })
    .replace(/(?<!\\)\$([^\n$]+?)\$/g, (m, tex) => { MATH.push({ tex, display: false }); return `\u0000MATH${MATH.length - 1}\u0000`; });
}
function restoreMath(html) {
  return html.replace(/\u0000MATH(\d+)\u0000/g, (m, i) => {
    const item = MATH[+i];
    if (!item) return "";
    try {
      return katex.renderToString(item.tex, { displayMode: item.display, throwOnError: false });
    } catch (e) { }
    return `<code>${esc(item.tex)}</code>`;
  });
}
/* 官方图片尺寸语法：![描述](图片链接 | 宽度x高度) —— 先转成 HTML，marked 不认这种带空格的 URL */
function preprocessImages(src) {
  let inFence = false;
  return src.split("\n").map(line => {
    if (/^\s*(```|~~~)/.test(line)) { inFence = !inFence; return line; }
    if (inFence) return line;
    const whole = /^\s*!\[([^\]]*)\]\(\s*([^)|]+?)\s*\|\s*(\d+)\s*[x×*]\s*(\d+)\s*\)\s*$/.exec(line);
    if (whole) {
      const cap = whole[1] ? `\n\n<div class="img-alt">${esc(whole[1])}</div>` : "";
      return `<img src="${whole[2].trim()}" alt="${esc(whole[1])}" style="width:${whole[3]}px;height:${whole[4]}px">${cap}`;
    }
    return line.replace(/!\[([^\]]*)\]\(\s*([^)|]+?)\s*\|\s*(\d+)\s*[x×*]\s*(\d+)\s*\)/g,
      (m, alt, url, w, h) => `<img src="${url.trim()}" alt="${esc(alt)}" style="width:${w}px;height:${h}px">`);
  }).join("\n");
}

const mdRenderer = new marked.Renderer();
mdRenderer.image = function (href, title, text) {
  let w = "", h = "", alt = text || "";
  const size = /\|\s*(\d+)\s*(?:x|×|\*)\s*(\d+)\s*$/.exec(href || "");
  if (size) { w = size[1]; h = size[2]; href = href.replace(size[0], "").trim(); }
  const style = w ? ` style="width:${w}px${h ? `;height:${h}px` : ""}"` : "";
  let out = `<img src="${href}" alt="${esc(alt)}"${style}${title ? ` title="${esc(title)}"` : ""}>`;
  if (alt) out += `<div class="img-alt">${esc(alt)}</div>`;
  return out;
};
mdRenderer.listitem = function (text, task, checked) {
  const body = String(text || "").replace(/^\s*(<input[^>]*>)\s*/, "");
  if (task) {
    const cb = `<input type="checkbox" disabled${checked ? " checked" : ""}>`;
    return `<li class="task-list-item">${cb}${body}</li>\n`;
  }
  return `<li>${body}</li>\n`;
};
mdRenderer.list = function (body, ordered, start) {
  const tag = ordered ? "ol" : "ul";
  const s = ordered && start && start !== 1 ? ` start="${start}"` : "";
  const cls = /task-list-item/.test(String(body)) ? ' class="contains-task-list"' : "";
  let inner = body;
  if (ordered) {
    const div = document.createElement("div");
    div.innerHTML = `<ol>${body}</ol>`;
    const ol = div.firstChild;
    const startN = Number(start) || 1;
    Array.from(ol.children).forEach((li, i) => {
      if (li.tagName === "LI") li.setAttribute("data-index", String(startN + i));
    });
    inner = ol.innerHTML;
  }
  return `<${tag}${s}${cls}>\n${inner}</${tag}>\n`;
};
marked.use({ renderer: mdRenderer });
/* 官方扩展语法：^高亮^ → <mark>；++下划线++ → <u>；~波浪线~ → span.wavy-underline
   （注意：官方没有注册 ==xx== 扩展，== 会原样保留为文本，这里同样不注册） */
marked.use({
  extensions: [
    { // ^高亮^
      name: "hl", level: "inline",
      start: src => src.indexOf("^"),
      tokenizer(src) {
        const m = /^\^([^\^\n]+)\^/.exec(src);
        if (m) return { type: "hl", raw: m[0], tokens: this.lexer.inlineTokens(m[1]) };
      },
      renderer(t) { return `<mark>${this.parser.parseInline(t.tokens)}</mark>`; }
    },
    { // ++下划线++
      name: "ul", level: "inline",
      start: src => src.indexOf("++"),
      tokenizer(src) {
        const m = /^\+\+([^\+\n]+)\+\+/.exec(src);
        if (m) return { type: "ul", raw: m[0], tokens: this.lexer.inlineTokens(m[1]) };
      },
      renderer(t) { return `<u>${this.parser.parseInline(t.tokens)}</u>`; }
    },
    { // ~波浪线~（单个波浪，不冲突 ~~删除线~~）
      name: "wavy", level: "inline",
      start: src => src.indexOf("~"),
      tokenizer(src) {
        const m = /^~([^~\n]+)~/.exec(src);
        if (m) return { type: "wavy", raw: m[0], tokens: this.lexer.inlineTokens(m[1]) };
      },
      renderer(t) { return `<span class="wavy-underline">${this.parser.parseInline(t.tokens)}</span>`; }
    }
  ]
});

/* 代码块语法高亮（官方用 hljs.highlightElement，DOM 模式 → 保留 data-highlighted 属性） */
function highlightCode(root) {
  if (!hljs) return;
  root.querySelectorAll("pre code").forEach(block => {
    try { hljs.highlightElement(block); } catch (e) { }
  });
}

function renderMarkdown(src) {
  let html = marked.parse(preprocessImages(protectMath(src)));
  html = restoreMath(html);
  return html;
}

/* ---------------- 主流程：markdown → 微信可直接粘贴的 HTML ---------------- */
function buildWechatHtml(md, themeId) {
  const theme = mpThemeById(themeId) || mpThemeById("alibaba");
  // 注入的排版覆盖规则：与原系统 injectCardCss() 一致（默认参数下的取值）
  const cssText = BASE_CSS + "\n" + (theme.css || "") + "\n"
    + `.card-host .markdown-body.card{box-shadow:none;}\n`
    + `.card-host .card .card-content{line-height:${LINE_HEIGHT};letter-spacing:${LETTER_SPACING}px;text-align:${ALIGN};}\n`
    + `.card-host .card .watermark{display:inline-block;opacity:.72;margin-left:8px;font-size:calc(var(--spacing) * 3);}\n`
    + `.card-host .card .card-footer-inner{display:flex;align-items:center;justify-content:space-between;}\n`
    + `.card-host .card .card-footer .page{margin-left:auto;}\n`
    + `.card-host .card img{max-width:100%;}\n`
    + `.card-host .card .katex{font-size:1.05em;}\n`;
  const rules = cardRules(cssText);

  const innerHtml = renderMarkdown(md);
  const el = document.createElement("div");
  el.className = "markdown-body card card-" + theme.className + " wechat";
  el.style.setProperty("--spacing", SPACING + "px");
  el.style.setProperty("--card-height", "auto");
  el.innerHTML = `<section class="card-content"><section class="card-content-inner">${innerHtml}</section></section>`;
  highlightCode(el);   // 官方在 buildCardEl 里就做代码高亮（内联样式之前）

  // 关键：必须包在与原系统一致的 .card-host 容器里再做样式内联 ——
  // 注入的排版规则选择器是 `.card-host .card .card-content{...}` / `.card-host .markdown-body.card{...}`，
  // 少了这层容器这些规则匹配不到，行高/字距/对齐等排版覆盖就会丢失（曾与原产物不一致）。
  const holder = document.createElement("div");
  holder.className = "card-host";
  holder.appendChild(el);
  document.body.appendChild(holder);
  let out;
  try {
    const clone = inlineStylesForWechat(el, rules);
    const card = divToSection(clone);
    materializePseudos(el, card, rules);
    out = resolveVarsAndCalc(wechatCompatHtml(card));
  } finally {
    holder.remove();
  }
  const body = normalizeWechatAttrs(`<section class="js-preview">${out}</section>`);
  return body;
}

/* ---------------- CLI ---------------- */
function main() {
  const [, , inFile, outFile, themeId] = process.argv;
  if (!inFile || !outFile) {
    console.error("用法: node render.js <input.md> <output.html> [themeId]");
    process.exit(2);
  }
  const md = fs.readFileSync(inFile, "utf8");
  const html = buildWechatHtml(md, themeId || "alibaba");
  fs.writeFileSync(outFile, html, "utf8");
  console.log(JSON.stringify({ ok: true, theme: themeId || "alibaba", out: outFile, bytes: html.length }));
}
if (require.main === module) main();
module.exports = { buildWechatHtml };
