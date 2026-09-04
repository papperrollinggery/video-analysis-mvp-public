"use strict";

const fs = require("node:fs");

let stage = "startup";

function classifyRendererError(message) {
  const value = String(message || "");
  if (/File name too long|ENAMETOOLONG/i.test(value)) return "path-too-long";
  if (/No space left|ENOSPC/i.test(value)) return "no-space";
  if (/Permission denied|EACCES|EPERM/i.test(value)) return "permission-denied";
  if (/Executable doesn't exist/i.test(value)) return "browser-executable-missing";
  if (/Failed to launch|browserType\.launch/i.test(value)) return "browser-launch-failed";
  if (/Target page, context or browser has been closed|TargetClosedError/i.test(value)) return "target-closed";
  if (/EmbeddedFontLoadError/i.test(value)) return "embedded-font-load-failed";
  return "renderer-error";
}

function diagnosticStageAfterClose(previousStage, closeFailed) {
  return closeFailed ? "close-browser" : previousStage;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function main() {
  stage = "read-input";
  const [htmlPath, pdfPath, configPath] = process.argv.slice(2);
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const html = fs.readFileSync(htmlPath, "utf8");
  stage = "launch-browser";
  const { chromium } = require("playwright");
  const browser = await chromium.launch({
    headless: true,
    executablePath: config.browserExecutable,
  });
  const watchdog = setTimeout(async () => {
    try { await browser.close(); } finally { process.exit(124); }
  }, config.watchdogMs || 170000);
  try {
    stage = "open-page";
    const page = await browser.newPage();
    stage = "install-network-guard";
    await page.route("**/*", async (route) => {
      const url = route.request().url();
      if (url === "about:blank" || url.startsWith("data:")) await route.continue();
      else await route.abort("blockedbyclient");
    });
    stage = "set-content";
    await page.setContent(html, { waitUntil: "load" });
    stage = "load-font";
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
    stage = "render-pdf";
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
    stage = "emit-receipt";
    process.stdout.write(
      JSON.stringify({ browserVersion: browser.version(), renderer: "playwright-node", fontLoaded }),
    );
  } finally {
    clearTimeout(watchdog);
    const previousStage = stage;
    try {
      await browser.close();
    } catch (error) {
      stage = diagnosticStageAfterClose(previousStage, true);
      throw error;
    }
    stage = diagnosticStageAfterClose(previousStage, false);
  }
}

if (require.main === module) {
  main().catch((error) => {
    const name = String(error && error.name ? error.name : "RenderError")
      .replace(/[^A-Za-z0-9_.-]/g, "-")
      .slice(0, 64);
    const code = classifyRendererError(error && error.message ? error.message : "");
    process.stderr.write(`RendererDiagnostic:${stage}:${code}:${name}`);
    process.exit(1);
  });
}

module.exports = { classifyRendererError, diagnosticStageAfterClose };
