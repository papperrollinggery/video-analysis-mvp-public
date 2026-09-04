import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  classifyRendererError,
  diagnosticStageAfterClose,
} = require("../../src/video_analysis_mvp/templates/client/render_pdf.cjs");

assert.equal(classifyRendererError("browserType.launch: spawn ENAMETOOLONG"), "path-too-long");
assert.equal(classifyRendererError("browserType.launch: ENOSPC"), "no-space");
assert.equal(classifyRendererError("browserType.launch: EACCES"), "permission-denied");
assert.equal(classifyRendererError("browserType.launch: Failed to launch"), "browser-launch-failed");
assert.equal(classifyRendererError("unknown"), "renderer-error");

assert.equal(diagnosticStageAfterClose("render-pdf", false), "render-pdf");
assert.equal(diagnosticStageAfterClose("render-pdf", true), "close-browser");

process.stdout.write(JSON.stringify({ status: "PASS", cases: 7 }));
