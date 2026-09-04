import { readFileSync } from "node:fs";

const status = Number.parseInt(process.argv[2] ?? "", 10);
if (!Number.isInteger(status) || status < 0) {
  console.error("usage: node scripts/classify-npm-audit.mjs EXIT_STATUS < npm-audit-output");
  process.exit(2);
}

const raw = readFileSync(0, "utf8").trim();
let report;
try {
  report = JSON.parse(raw);
} catch {
  report = null;
}

const vulnerabilities = report?.metadata?.vulnerabilities;
const validCount = (value) => Number.isSafeInteger(value) && value >= 0;
if (
  vulnerabilities &&
  (!validCount(vulnerabilities.high) || !validCount(vulnerabilities.critical))
) {
  console.error("npm audit returned invalid vulnerability counts; refusing a clean verdict");
  process.exit(1);
}
const high = Number(vulnerabilities?.high ?? 0);
const critical = Number(vulnerabilities?.critical ?? 0);
if (high > 0 || critical > 0) {
  console.error(`npm audit found high or critical vulnerabilities (high=${high}, critical=${critical})`);
  process.exit(1);
}

if (status === 0 && vulnerabilities && Number.isFinite(high) && Number.isFinite(critical)) {
  console.log("npm audit completed without high or critical vulnerabilities");
  process.exit(0);
}

const endpointFailure = /(audit endpoint|registry\.npmjs\.org\/-\/npm\/v1\/security\/advisories\/bulk)/i.test(raw);
const transientFailure = /(network timeout|503 service unavailable|econnreset|etimedout|eai_again|enoaudit)/i.test(raw);
if (status !== 0 && endpointFailure && transientFailure) {
  console.log("::warning::npm audit unavailable because the registry advisory endpoint failed; no vulnerability verdict was produced");
  process.exit(0);
}

console.error(`unclassified npm audit failure (exit=${status}); refusing to treat it as a clean audit`);
process.exit(1);
