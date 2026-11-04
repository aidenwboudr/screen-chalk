// Render every bench page at 1920x1280 logical, scale 1.5 (a common laptop panel)
// and record the ground-truth boxes of its [data-gt] elements.
//   node render.js [outdir]   -> outdir/<page>.hires.png + outdir/gt.json
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const PAGES = path.join(__dirname, "pages");
const OUT = process.argv[2] || path.join(__dirname, "out", "render");
// Playwright's own Chromium (npx playwright install chromium) unless CHALK_CHROME names another browser
const CHROME = process.env.CHALK_CHROME || undefined;

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: CHROME });
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1280 }, deviceScaleFactor: 1.5 });
  const gt = {};
  const files = [];
  for (const dir of [PAGES, PAGES + "-hard", path.join(__dirname, "visual", "heldout")]) {
    if (fs.existsSync(dir)) files.push(...fs.readdirSync(dir).filter((f) => f.endsWith(".html")).sort().map((f) => [dir, f]));
  }
  for (const [dir, file] of files) {
    const page = await ctx.newPage();
    await page.goto("file://" + path.join(dir, file));
    await page.waitForTimeout(150);
    await page.screenshot({ path: path.join(OUT, file.replace(".html", ".hires.png")) });
    gt[file] = await page.evaluate(() => {
      const out = {};
      for (const el of document.querySelectorAll("[data-gt]")) {
        // a sentence that wraps is several rects; keep them all, scoring uses their union
        const rects = [...el.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
        const r = el.getBoundingClientRect();
        out[el.dataset.gt] = {
          box: [r.x, r.y, r.width, r.height].map((v) => Math.round(v * 10) / 10),
          rects: rects.map((q) => [q.x, q.y, q.width, q.height].map((v) => Math.round(v * 10) / 10)),
        };
      }
      return out;
    });
    await page.close();
  }
  fs.writeFileSync(path.join(OUT, "gt.json"), JSON.stringify(gt, null, 1));
  await browser.close();
  console.log(`rendered ${Object.keys(gt).length} pages to ${OUT}`);
})();
