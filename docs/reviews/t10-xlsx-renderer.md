# T10 professional XLSX renderer review

Date: 2026-09-01. Scope: the generation-only XLSX adapter for the fixed
`client-storyboard` template. This task does not publish the current client
package, create saved versions, update the artifact registry, render PDF or add
an automatic analysis/Finalize side effect.

## Delivered

- `src/video_analysis_mvp/export_xlsx.py` consumes only a validated
  `client-export-dataset/v1` and the T09 preflight result. It writes one new,
  caller-supplied `.xlsx` staging path and refuses to replace an existing file.
- Five visible sheets in the fixed manifest order: project overview,
  shot-by-shot storyboard, VO/on-screen text, music/SFX/rhythm, and
  evidence/limitations.
- Professional table hierarchy, freeze panes, filters, A4 landscape print
  areas, repeated table headers, embedded evidence frames, explicit missing
  frames, formula-neutralized text and client-safe evidence references.
- Long text continues into linked rows on every sheet. Excel cell and row-height
  limits are respected; overview pages are never force-shrunk vertically.
- Exact frame/logo bytes are re-read through the project-confined regular-file
  boundary and compared with their bound digest, media type and dimensions
  before embedding.
- The generated OOXML package is read back and checked for ZIP integrity, exact
  sheet order, worksheet formulas, macros, external links and media count before
  a receipt is returned.
- The receipt binds dataset, preflight, template, settings, renderer version,
  UTC generation time, output bytes, sheet/row/image counts, font status and
  energy-visualization method.
- Audio energy uses a numeric-time ScatterChart when all measured points fit the
  240-point visual limit. Above the limit, the renderer keeps the complete event
  table, omits the chart and adds a client-visible explanation instead of
  silently dropping an extreme value.
- `openpyxl>=3.1.5,<4` is an optional `export` dependency; the core package keeps
  lazy import and returns an actionable missing-extra error.

## Native review and negative-optimization decisions

The first native cold review reproduced four HIGH defects: header/footer
control codes could expose an absolute workbook path, legal long scene text
bypassed continuation, audio continuation rows broke chart references, and
long overview content was force-shrunk below the typography floor. It also
found a WebP schema/renderer mismatch and a font-declaration compatibility
risk. All were fixed and regression-pinned.

The second review rejected two tempting negative optimizations. The storyboard
was not kept at two print pages wide because merged headings became incomplete
on the second horizontal page. A detected PingFang font is not blindly applied
to every cell because LibreOffice rendered Latin glyphs incorrectly in that
mode. XLSX instead declares the fixed template font, records whether it was
verified for the target renderer and shows a substitution warning when it was
not.

The final native verdict found no remaining HIGH/MEDIUM issue. Conservative
choices retained: no template DSL/factory, pandas layer, cache, database, hidden
tracking sheet, automatic export, remote image/font download or chart sampling
algorithm. A 241-point timeline deliberately loses the optional chart, not the
underlying event data; a future scale task may add a separately verified
downsampler if users need it.

## Visual and structural verification

- Dedicated openpyxl 3.1.5 runtime: **15/15 T10 tests passed**.
- T10 + T09 + T08 + report boundary: **46/46 tests passed**.
- Host Ruff and compileall: PASS.
- Artifact-tool 2.8.6 imported the generated workbook, found five sheets and
  zero formula-error strings, and rendered all five sheet surfaces.
- LibreOffice 26.2.3.2 opened the same workbook and produced a seven-page A4
  landscape preview. The evidence image, overview continuation, table-header
  continuation, VO table, numeric-time energy chart and evidence continuation
  were visually inspected; no clipped cell content or missing glyph block was
  observed. The temporary PDF is validation evidence only, not the T11 PDF
  product.
- Full core environment: **421 passed, 15 skipped, 345 subtests passed; 1
  failure**. The only failure is the intentionally stale UI acceptance receipt
  (`113 != 179` files). T10's 15 export-extra tests are skipped there
  because the optional export dependency is deliberately absent and pass in the
  dedicated bundled runtime.

The generated workbook, LibreOffice PDF and previews live only in a bounded
temporary directory during this review and are removed after the audit. No
project `reports/` XLSX/PDF and no saved-version history were created.

## Independent DeepSeek audit

DeepSeek Harness session `session-b7a01452-779b-48fb-a685-e10e53b965e1`
completed three read-only turns with `deepseek-v4-pro`, high effort, `code` +
`proposal-only`. Initial RPC `a8241370-127c-4f46-b14a-26168084e415` found two
HIGH and five MEDIUM issues. Final-delta RPC
`acca95ae-b093-4504-bb07-51d2524ae852` closed every HIGH and left one
guarantee gap; closure RPC `4ed3ed50-7d56-4276-922e-88e7c24a8bdd` verified the
Excel row-limit guard and returned no remaining HIGH/MEDIUM. Every turn ended
with `completionReason=completed`; final live state was false.

Adopted findings: continuation chunks now pass through formula neutralization;
XLSX-specific capacity is hash-bound at 3,200 shots / 8,000 events; Excel's
1,048,576-row limit is enforced at the shared data-row write path; chart source
rows cannot be altered by filters and are hidden only after `plotVisOnly=false`;
overview numbers are formatted; renderer exceptions share one public contract;
and client-export scope/WebP claims match the evidence layer.

Rejected or deferred to avoid negative optimization: no `write_only` rewrite,
xlsxwriter migration, pandas layer, cache, database, chart downsampler, formula
gate weakening or automatic export. DeepSeek proposed declaring a detected
PingFang font, but the observed LibreOffice 26.2.3.2 Latin-glyph corruption is
stronger evidence; the final workbook keeps the fixed template font, records
`declared_font_verified=false` and shows a localized substitution warning.
DeepSeek explicitly accepted that decision in its final-delta review.

Local audit record: `tmp/.t10-deepseek-audit-20260901`. `RESULT.md` and
`OPINION.md` preserve the initial findings; `FINAL_TURN_RECEIPT.json` records all
three RPCs, sequence boundaries and final verdict. The copied source trees are
compacted after final verification into `audit-snapshot.tar.gz` (SHA-256
`12baa2c8189460ff0eecd9e2ae5ae54d56980259c8a631385d0c55bff715c4ea`).

Overall product verification remains partial. T11 still owns professional PDF;
T12 owns locks, idempotency, staging, atomic current-package publication,
crash recovery and saved-version lifecycle. T19-T22 own large-scale/platform,
accessibility and final frozen-candidate acceptance.
