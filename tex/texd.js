// texd: TeX -> glyph outlines for chalk's scene boards (scene.py), through MathJax's SVG output.
// One JSON request per stdin line: {"tex": "...", "display": true}. One JSON reply per line:
// {"w", "h", "y0"} (viewBox: x 0..w, y y0..y0+h, baseline at y=0, y down, 1000 units = 1 em) and
// "items": [{"d": path data, "m": [a,b,c,d,e,f], "color": "#rrggbb"|null} | {"rect": [x,y,w,h], "m", "color"}
//            | {"text": "θ", "size", "italic", "bold", "m", "color"}]
// with every transform already composed, so the caller only applies m. Errors: {"error": "..."}.
// "text" items are characters MathJax's TeX fonts don't have (θ inside \text{}, CJK, emoji): MathJax leaves them
// as SVG <text> for a browser to draw, so the caller sets them in a real font at the baseline m puts at (0, 0),
// in a slot "w" wide (MathJax's own guess at their width, which it laid the rest out around).
const {mathjax} = require('mathjax-full/js/mathjax.js');
const {TeX} = require('mathjax-full/js/input/tex.js');
const {SVG} = require('mathjax-full/js/output/svg.js');
const {liteAdaptor} = require('mathjax-full/js/adaptors/liteAdaptor.js');
const {RegisterHTMLHandler} = require('mathjax-full/js/handlers/html.js');
const {AllPackages} = require('mathjax-full/js/input/tex/AllPackages.js');

const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);
const doc = mathjax.document('', {
  InputJax: new TeX({packages: AllPackages.filter(p => p !== 'bussproofs' && p !== 'noundefined')}),
  OutputJax: new SVG({fontCache: 'none'}),
});

const mul = (p, q) => [p[0] * q[0] + p[2] * q[1], p[1] * q[0] + p[3] * q[1], p[0] * q[2] + p[2] * q[3],
  p[1] * q[2] + p[3] * q[3], p[0] * q[4] + p[2] * q[5] + p[4], p[1] * q[4] + p[3] * q[5] + p[5]];

function parseTransform(s) {
  let m = [1, 0, 0, 1, 0, 0];
  for (const [, fn, args] of (s || '').matchAll(/(\w+)\(([^)]*)\)/g)) {
    const v = args.split(/[\s,]+/).filter(Boolean).map(Number);
    if (fn === 'translate') m = mul(m, [1, 0, 0, 1, v[0] || 0, v[1] || 0]);
    else if (fn === 'scale') m = mul(m, [v[0], 0, 0, v.length > 1 ? v[1] : v[0], 0, 0]);
    else if (fn === 'matrix') m = mul(m, v);
  }
  return m;
}

function walk(node, m, color, out) {
  if (!node || node.kind === '#text' || node.kind === '#comment') return;
  const attr = n => adaptor.getAttribute(node, n);
  const t = attr('transform');
  if (t) m = mul(m, parseTransform(t));
  const fill = attr('fill');
  if (fill && fill !== 'currentColor' && fill !== 'none') color = fill;
  if (node.kind === 'path' && attr('d')) out.push({d: attr('d'), m, color});
  else if (node.kind === 'rect') {
    out.push({rect: ['x', 'y', 'width', 'height'].map(k => Number(attr(k) || 0)), m, color});
  } else if (node.kind === 'text') {
    const text = adaptor.textContent(node);
    if (text.trim()) {
      const v = attr('data-variant') || '';
      out.push({text, size: parseFloat(attr('font-size')) || 1000, w: adaptor.nodeSize(node)[0] * 1000, m, color,
        italic: v.includes('italic') || attr('font-style') === 'italic',
        bold: v.includes('bold') || attr('font-weight') === 'bold'});
    }
    return;
  }
  for (const c of adaptor.childNodes(node)) walk(c, m, color, out);
}

function convert(req) {
  const node = doc.convert(String(req.tex || ''), {display: req.display !== false});
  const svg = adaptor.firstChild(node);
  const err = adaptor.textContent(svg).trim();
  const vb = (adaptor.getAttribute(svg, 'viewBox') || '0 0 0 0').split(/\s+/).map(Number);
  const items = [];
  walk(svg, [1, 0, 0, 1, 0, 0], null, items);
  if (!items.length && err) return {error: err};
  const merror = adaptor.outerHTML(svg).includes('data-mml-node="merror"');
  return {w: vb[2], h: vb[3], y0: vb[1], items, error: merror ? 'tex error' : undefined};
}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
  buf += chunk;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, i);
    buf = buf.slice(i + 1);
    if (!line.trim()) continue;
    let reply;
    try { reply = convert(JSON.parse(line)); } catch (e) { reply = {error: String(e && e.message || e)}; }
    process.stdout.write(JSON.stringify(reply) + '\n');
  }
});
process.stdin.on('end', () => process.exit(0));
