import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { dirname, extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const distRoot = join(frontendRoot, "dist");
const diagnosticRoot = join(frontendRoot, "..", "test-results");
const state = {
  mode: "empty",
  currentAvailable: false,
  generation: false,
  cancelled: false,
  generationPosts: 0,
  cancelPosts: 0,
  savePosts: 0,
  deletePosts: 0,
  stateReads: 0,
  stateActive: 0,
  stateMax: 0
};

const receipt = {
  schema_id: "client-export-package/v1",
  state: "current",
  export_id: "a".repeat(64),
  receipt_digest: "b".repeat(64),
  idempotency_key: "e2e",
  request_digest: "c".repeat(64),
  dataset_digest: "d".repeat(64),
  source_generation_id: "generation-1",
  formats: ["xlsx", "pdf"],
  settings: {},
  outputs: {},
  created_at_utc: "2026-09-01T06:00:00+00:00"
};

function exportCenter() {
  return {
    schema_id: "client-export-center/v1",
    state: { schema_id: "client-export-state/v1", status: "current", request_digest: "c".repeat(64) },
    current: !state.currentAvailable ? null : {
      lifecycle_state: state.mode === "stale" ? "stale" : "current",
      receipt,
      downloads: {
        xlsx: "/files/demo/reports/client/current/client_breakdown.xlsx",
        pdf: "/files/demo/reports/client/current/client_breakdown.pdf"
      }
    },
    saved: !state.currentAvailable ? [] : [{
      version_id: "client-v1",
      export_id: "a".repeat(64),
      formats: ["xlsx"],
      created_at_utc: "2026-09-01T06:00:00+00:00",
      size_bytes: 2048,
      downloads: { xlsx: "/files/demo/reports/client/saved/client-v1/client_breakdown.xlsx" }
    }]
  };
}

function deliverables() {
  const stale = state.mode === "stale";
  return {
    project_id: "demo",
    readiness: {
      status: stale ? "blocked" : "ready",
      professional_export_allowed: !stale,
      reasons: stale ? ["Project changed after export"] : []
    },
    export: { blocked_reasons: stale ? ["Project changed after export"] : [] },
    artifacts: []
  };
}

function json(response, status, value) {
  const payload = Buffer.from(JSON.stringify(value));
  response.writeHead(status, { "Content-Type": "application/json", "Content-Length": payload.length });
  response.end(payload);
}

async function body(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 1024 * 1024) throw new Error("mock request exceeded 1 MiB");
    chunks.push(chunk);
  }
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
}

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    const { pathname } = url;
    if (pathname === "/api/session") return json(response, 200, { csrf_token: "e2e-csrf-token-1234567890" });
    if (pathname === "/api/projects/demo/deliverables") return json(response, 200, deliverables());
    if (pathname === "/api/projects/demo/exports" && request.method === "GET") return json(response, 200, exportCenter());
    if (pathname === "/api/projects/demo/exports/state") {
      state.stateReads += 1;
      state.stateActive += 1;
      state.stateMax = Math.max(state.stateMax, state.stateActive);
      await new Promise((resolve) => setTimeout(resolve, 180));
      state.stateActive -= 1;
      const status = state.generation && !state.cancelled ? "rendering" : state.cancelled ? "cancelled" : "absent";
      return json(response, 200, { schema_id: "client-export-state/v1", status, request_digest: "c".repeat(64) });
    }
    if (pathname === "/api/projects/demo/exports" && request.method === "POST") {
      await body(request);
      state.generationPosts += 1;
      state.generation = true;
      state.cancelled = false;
      if (state.mode === "generate-success") {
        await new Promise((resolve) => setTimeout(resolve, 350));
        state.generation = false;
        state.currentAvailable = true;
        state.mode = "ready";
        return json(response, 200, receipt);
      }
      await new Promise((resolve) => setTimeout(resolve, 2600));
      state.generation = false;
      return json(response, 422, { error: { message: "Client export was cancelled" } });
    }
    if (pathname === "/api/projects/demo/exports/cancel" && request.method === "POST") {
      await body(request);
      state.cancelPosts += 1;
      state.cancelled = true;
      return json(response, 200, { status: "cancel_requested", request_digest: "c".repeat(64) });
    }
    if (pathname === "/api/projects/demo/exports/save" && request.method === "POST") {
      await body(request);
      state.savePosts += 1;
      return json(response, 200, receipt);
    }
    if (pathname === "/api/projects/demo/exports/saved/client-v1" && request.method === "DELETE") {
      state.deletePosts += 1;
      return json(response, 200, { status: "deleted", version_id: "client-v1" });
    }
    if (pathname === "/api/test/mode" && request.method === "POST") {
      state.mode = (await body(request)).mode;
      state.currentAvailable = ["ready", "stale"].includes(state.mode);
      state.cancelled = false;
      return json(response, 200, { mode: state.mode });
    }

    const relative = pathname.startsWith("/assets/") ? pathname.slice(1) : "index.html";
    const candidate = normalize(join(distRoot, relative));
    if (!candidate.startsWith(`${normalize(distRoot)}/`) && candidate !== join(distRoot, "index.html")) {
      return json(response, 404, { error: { message: "unsafe static path" } });
    }
    const payload = await readFile(candidate);
    const contentType = { ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" }[extname(candidate)] ?? "text/html";
    response.writeHead(200, { "Content-Type": contentType, "Content-Length": payload.length });
    response.end(payload);
  } catch (error) {
    json(response, 500, { error: { message: error instanceof Error ? error.message : "mock failure" } });
  }
});

await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
if (!address || typeof address === "string") throw new Error("mock server did not bind a TCP port");
const origin = `http://127.0.0.1:${address.port}`;
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const consoleProblems = [];
page.on("console", (message) => {
  if (["warning", "error"].includes(message.type())) consoleProblems.push(message.text());
});
page.on("pageerror", (error) => consoleProblems.push(String(error)));

try {
  await page.goto(`${origin}/projects/demo/deliverables`);
  await page.getByRole("heading", { name: "Export center" }).waitFor();
  assert.equal(state.generationPosts, 0, "opening must never generate files");
  const generate = page.getByRole("button", { name: "Generate both" });
  await generate.click({ trial: true });
  assert.equal(await generate.isEnabled(), true);
  await generate.evaluate((button) => { button.click(); button.click(); });
  await page.getByRole("button", { name: "Cancel before publish" }).waitFor({ timeout: 5000 });
  await page.getByRole("button", { name: "Cancel before publish" }).click();
  await page.getByText("Cancellation requested before publication.").waitFor();
  await page.getByText("Client export was cancelled").waitFor({ timeout: 5000 });
  const expectedCancellationConsole = [...consoleProblems];
  assert.equal(expectedCancellationConsole.length, 1);
  assert.ok(expectedCancellationConsole[0].includes("422"));
  consoleProblems.length = 0;
  assert.equal(state.generationPosts, 1);
  assert.equal(state.cancelPosts, 1);
  assert.equal(state.stateMax, 1, "progress reads must be serial");

  await page.evaluate(() => fetch("/api/test/mode", { method: "POST", body: JSON.stringify({ mode: "generate-success" }) }));
  await page.reload();
  await page.getByText("Not generated").waitFor();
  await page.getByRole("button", { name: "Generate both" }).click();
  await page.getByText("Current package generated · XLSX + PDF.").waitFor({ timeout: 5000 });
  await page.getByRole("link", { name: "Download XLSX" }).waitFor();
  assert.equal(state.generationPosts, 2);

  await page.evaluate(() => fetch("/api/test/mode", { method: "POST", body: JSON.stringify({ mode: "stale" }) }));
  await page.reload();
  await page.getByText("Stale—inspect only; do not present as current").waitFor();
  assert.equal(await page.getByRole("link", { name: "Download historical XLSX" }).count(), 1);
  assert.equal(await page.getByRole("link", { name: "Download saved XLSX from client-v1" }).count(), 1);

  await page.evaluate(() => fetch("/api/test/mode", { method: "POST", body: JSON.stringify({ mode: "ready" }) }));
  await page.reload();
  await page.getByLabel("Version ID").fill("client-v2");
  await page.getByRole("button", { name: "Save version" }).click();
  await page.getByText("Saved immutable version client-v2.").waitFor();
  await page.getByRole("button", { name: "Delete saved version client-v1" }).click();
  await page.getByRole("button", { name: "Confirm" }).click();
  await page.getByText("Deleted saved version client-v1.").waitFor();
  assert.equal(state.savePosts, 1);
  assert.equal(state.deletePosts, 1);

  const responsive = [];
  for (const [width, height] of [[1440, 900], [900, 1000], [390, 844]]) {
    await page.setViewportSize({ width, height });
    const observed = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      smallTargets: [...document.querySelectorAll(".export-center button,.export-center a")]
        .filter((element) => {
          const rectangle = element.getBoundingClientRect();
          return rectangle.width > 0 && rectangle.height > 0 && rectangle.height < 44;
        }).length
    }));
    assert.ok(observed.scrollWidth <= observed.clientWidth, `${width}px overflow`);
    assert.equal(observed.smallTargets, 0, `${width}px touch targets`);
    responsive.push({ width, height, ...observed });
  }
  await page.setViewportSize({ width: 900, height: 1000 });
  await page.locator('.export-format-switch input[value="xlsx"]').focus();
  await page.keyboard.press("Space");
  const focus = await page.evaluate(() => {
    const input = document.activeElement;
    if (!(input instanceof HTMLInputElement)) return null;
    const style = getComputedStyle(input.closest("label"));
    return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth };
  });
  assert.ok(focus && focus.outlineStyle !== "none" && focus.outlineWidth !== "0px");
  assert.deepEqual(consoleProblems, []);
  console.log(JSON.stringify({
    status: "PASS",
    requests: { generate: state.generationPosts, cancel: state.cancelPosts, save: state.savePosts, delete: state.deletePosts },
    stateMaxConcurrent: state.stateMax,
    responsive,
    focus,
    expectedCancellationConsoleCount: expectedCancellationConsole.length,
    unexpectedConsoleProblems: consoleProblems
  }));
} catch (error) {
  await mkdir(diagnosticRoot, { recursive: true });
  await page.screenshot({ path: join(diagnosticRoot, "export-center-e2e-failure.png"), fullPage: true });
  throw error;
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
