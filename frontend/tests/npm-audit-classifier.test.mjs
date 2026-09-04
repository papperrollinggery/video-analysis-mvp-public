import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const classifier = path.resolve(frontend, "../scripts/classify-npm-audit.mjs");

function classify(status, payload) {
  return spawnSync(process.execPath, [classifier, String(status)], {
    cwd: frontend,
    encoding: "utf8",
    input: payload,
  });
}

const clean = classify(0, JSON.stringify({
  metadata: { vulnerabilities: { info: 0, low: 0, moderate: 0, high: 0, critical: 0 } },
}));
assert.equal(clean.status, 0, clean.stderr);

const vulnerable = classify(1, JSON.stringify({
  metadata: { vulnerabilities: { info: 0, low: 0, moderate: 0, high: 1, critical: 0 } },
}));
assert.equal(vulnerable.status, 1);
assert.match(vulnerable.stderr, /high or critical vulnerabilities/i);

const critical = classify(1, JSON.stringify({
  metadata: { vulnerabilities: { info: 0, low: 0, moderate: 0, high: 0, critical: 1 } },
}));
assert.equal(critical.status, 1);
assert.match(critical.stderr, /high or critical vulnerabilities/i);

const malformedCounts = classify(0, JSON.stringify({
  metadata: { vulnerabilities: { info: 0, low: 0, moderate: 0, high: -1, critical: "none" } },
}));
assert.equal(malformedCounts.status, 1);
assert.match(malformedCounts.stderr, /invalid vulnerability counts/i);

const unavailable = classify(
  1,
  "npm warn audit 503 Service Unavailable\n" +
    "npm error audit endpoint returned an error\n",
);
assert.equal(unavailable.status, 0, unavailable.stderr);
assert.match(unavailable.stdout, /::warning::npm audit unavailable/);

const endpointTimeoutWithoutBoilerplate = classify(
  1,
  "npm warn audit network timeout at: " +
    "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk\n",
);
assert.equal(endpointTimeoutWithoutBoilerplate.status, 0, endpointTimeoutWithoutBoilerplate.stderr);
assert.match(endpointTimeoutWithoutBoilerplate.stdout, /::warning::npm audit unavailable/);

const unknown = classify(1, "unexpected audit process failure\n");
assert.equal(unknown.status, 1);
assert.match(unknown.stderr, /unclassified npm audit failure/i);

console.log("npm audit classifier contract passed");
