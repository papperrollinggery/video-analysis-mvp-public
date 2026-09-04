# T07 optional advanced-audio adapter review

Date: 2026-09-01. Status: implementation and native cold review complete.
The user cancelled DeepSeek Harness as a project dependency; no DeepSeek result
is required for T07 and no further call or recharge is expected.

## Delivered

- One generic, explicitly configured local executable adapter over the existing
  `audio-timeline/v1`; no second audio schema, in-process model dependency or
  automatic provider/model download was added.
- Deterministic `audio-adapter-request/v1` binds the current generation, WAV
  digest and complete baseline. Request and response are both capped at 16 MiB.
- Provider output must preserve all baseline source/event values, add only
  `source_type=adapter` evidence, keep speaker IDs anonymous, and contain no
  human-review assertion. The complete timeline is validated before the
  existing staged audio-intelligence transaction commits it.
- Disabled, missing/unsafe executable, launch race, timeout, crash, combined
  stdout/stderr overflow and invalid response all return a bounded fallback
  receipt without changing baseline bytes.
- The subprocess uses an argument vector rather than a shell, a temporary
  HOME/TMPDIR and a minimal environment without ambient provider tokens.
- The current Codex task uses the same prepare/apply functions. Direct apply is
  reserved for `codex-current-task`; source engine/model remain
  `host-managed-unverified`, engine version/device remain absent, and the run
  receipt records `provider_called=false` and `model_identity_verified=false`.
- Runtime configuration accepts only a bounded absolute executable path and a
  1–600 second timeout. Doctor and settings expose capability state without
  disclosing the private executable path.

## Native review disposition

The independent reviewer reproduced four issues before approval:

1. an external executable named `codex-current-task` could hide a real provider
   call;
2. current-task submissions could persist verified-looking adapter/model
   metadata;
3. direct apply bypassed the subprocess response-size cap, while the former
   64/16 MiB request/response mismatch created an impossible protocol range;
4. an executable disappearing between validation and launch raised a raw error.

All four now have negative regressions and are closed. Final current-file review
found no remaining HIGH/MEDIUM and no new negative optimization. The accepted
LOW residual is that the adapter reuses `vision._communicate_bounded` and maps
its legacy error text. Timeout, output-limit and crash behavior are covered;
moving that helper into a shared typed subprocess module belongs to T18 rather
than expanding T07.

## Verification

- Adapter file: 11/11 tests passed.
- Related audio/provider/config/Codex/API boundary: 118/118 tests passed.
- Targeted Ruff and Python compileall: passed.
- Core full suite: 439 passed, 20 skipped, 358 subtests passed; one known stale
  UI acceptance receipt fails (`113 != 185`) and remains intentionally deferred
  to the final candidate freeze.

## Explicit residuals

- No real pyannote, separator, cloud provider or Codex audio perception was run;
  semantic accuracy and model licensing remain unverified.
- The adapter is a process boundary, not an OS network/filesystem sandbox. Only
  trusted local executables are supported; hostile same-user executable swaps
  and cross-platform resource enforcement remain T18/T19 gates.
- No dependency was silently added to the MIT core. Provider-specific packages
  must be installed explicitly by their adapter; their extras are defined only
  when an actual maintained/licensed implementation is selected.
- Historical DeepSeek quota failures remain in the task event log as factual
  audit history, but they are not a current requirement or blocker.
