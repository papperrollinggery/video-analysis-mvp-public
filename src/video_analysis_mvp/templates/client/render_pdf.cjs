"use strict";

const fs = require("node:fs");
const { chromium } = require("playwright");

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function main() {
  const [htmlPath, pdfPath, configPath] = process.argv.slice(2);
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const html = fs.readFileSync(htmlPath, "utf8");
  const browser = await chromium.launch({
    headless: true,
    executablePath: config.browserExecutable,
  });
  const watchdog = setTimeout(async () => {
    try { await browser.close(); } finally { process.exit(124); }
  }, config.watchdogMs || 170000);
  try {
    const page = await browser.newPage();
    await page.route("**/*", async (route) => {
      const url = route.request().url();
      if (url === "about:blank" || url.startsWith("data:")) await route.continue();
      else await route.abort("blockedbyclient");
    });
    await page.setContent(html, { waitUntil: "load" });
    await page.evaluate(async () => {
      await document.fonts.ready;
      await document.fonts.load("12px 'VEW Embedded CJK'", "中文");
    });
    const fontLoaded = await page.evaluate(() =>
      Array.from(document.fonts).some(
        (face) => face.family.replaceAll('"', "") === "VEW Embedded CJK" && face.status === "loaded",
      ),
    );
    if (config.requireEmbeddedFont && !fontLoaded) {
      throw new Error("EmbeddedFontLoadError");
    }
    await page.emulateMedia({ media: "print" });
    await page.pdf({
      path: pdfPath,
      format: "A4",
      landscape: true,
      preferCSSPageSize: true,
      printBackground: true,
      tagged: true,
      outline: true,
      displayHeaderFooter: true,
      headerTemplate: "<span></span>",
      footerTemplate:
        '<div style="font:12px Arial;color:#667085;width:100%;padding:0 12mm;display:flex;justify-content:space-between"><span>' +
        escapeHtml(config.shortTitle) +
        '</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>',
      margin: { top: "8mm", right: "0", bottom: "12mm", left: "0" },
    });
    process.stdout.write(
      JSON.stringify({ browserVersion: browser.version(), renderer: "playwright-node", fontLoaded }),
    );
  } finally {
    clearTimeout(watchdog);
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(String(error && error.name ? error.name : "RenderError"));
  process.exit(1);
});
