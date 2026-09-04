# BridgeDeck compatibility

Status: local adapter candidate. Synthetic loopback protocol tests are implemented; no real upstream inference or account authentication is claimed by this document.

## Choose the correct execution surface

| Surface | Who performs analysis | Workbench credential requirement |
| --- | --- | --- |
| Current Codex task | The task reads the tool's evidence request and submits structured analysis | No additional provider API key; use `codex prepare/apply` |
| Official OpenAI / MiniMax adapter | The workbench explicitly calls the selected API | An eligible endpoint-bound key |
| BridgeDeck adapter | An explicitly configured local BridgeDeck service forwards the request | No key is copied or forwarded by the workbench; BridgeDeck owns its authentication |

All three return to the existing review, Finalize, readiness and explicit-export flow. BridgeDeck is not a prerequisite for using the current Codex task.

## Protocol contract

The adapter calls the selected account-scoped `/v1/responses` route. It sends one fully decoded, bounded PNG/JPEG frame and the existing shot-observation prompt. It does not send the whole video, audio track, unrelated project files, environment credentials, cookies or another application's OAuth store.

The request uses `input_image` for the base64 image and `text.format` with `type: json_schema` and `strict: true`. These are the documented Responses image and structured-output formats. [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision#analyze-images), [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs#structured-outputs-vs-json-mode).

Local source inspection on 2026-09-01 found two relevant BridgeDeck behaviors:

- `chat_completions_to_responses()` retained image content but omitted the caller's `response_format`. This adapter therefore uses Responses directly.
- `normalize_request_body()` retained `text.format`, but removed `max_output_tokens` and forced upstream streaming. The bridge's non-streaming response surface is used to return the completed Responses object.

These are version-specific observations, not a promise about every BridgeDeck build. The adapter rejects a response unless it reports a completed result from the exact requested model, contains one completed assistant message, and provides valid structured observation JSON. Refusals, incomplete results, unexpected output types, duplicate JSON keys, non-finite values, malformed schemas and model mismatches do not become successful annotations.

## Explicit local configuration

Only numeric loopback HTTP endpoints with an explicit port and account route are accepted:

```text
http://127.0.0.1:8876/accounts/YOUR_ACCOUNT_ID/v1
http://[::1]:8876/accounts/YOUR_ACCOUNT_ID/v1
```

The unscoped `/v1` pool, remote hosts, DNS names such as `localhost`, URL credentials, queries and fragments are rejected. The workbench does not select an account for you or read account stores. In the inspected BridgeDeck implementation, an explicit account route selects that single account; future bridge routing behavior remains the bridge operator's responsibility.

For a one-run invocation after choosing the intended account and model in BridgeDeck:

```bash
analyze-video --workspace ./analysis-projects vision my-project \
  --provider bridgedeck \
  --base-url http://127.0.0.1:8876/accounts/YOUR_ACCOUNT_ID/v1 \
  --model YOUR_MODEL_ID \
  --limit 1
```

Replace the placeholders yourself; they are not account or model discovery commands. This invocation sends the selected frame through the chosen bridge. Do not run it on private media until that transfer and destination are acceptable. It does not persist the one-run route, mark human review, Finalize, or generate customer documents.

For an explicitly saved workbench configuration, the existing runtime-settings API accepts `vision_provider: bridgedeck`, `bridgedeck_base_url`, and `bridgedeck_model`. Selecting BridgeDeck without both a valid route and explicit model is rejected. Existing OpenAI/MiniMax configurations remain compatible. Local runtime configuration is private and should not be included in source control or screenshots.

## Security and resource boundaries

- No ambient `OPENAI_API_KEY` or `MINIMAX_API_KEY` is forwarded, even when set.
- Environment HTTP proxies are disabled for this numeric-loopback request.
- Redirects are rejected, including redirects to another local endpoint.
- Images use the same 20 MiB / decoded-raster checks as other visual evidence.
- Responses are bounded to 2 MiB with a 120-second socket timeout. This is not a monetary cap or a guarantee of an end-to-end upstream execution deadline.
- The inspected bridge removes upstream output-token limits. The receipt explicitly records `upstream_token_limit_enforced: false`; the workbench does not conceal that limitation or claim that `max_output_tokens` was enforced.
- The returned model field must match the requested identifier. This verifies response-field consistency, not a cryptographically attested upstream model identity.
- The existing receipt binds frame, media, shot state, provider source and the BridgeDeck transport contract. Account IDs and raw provider responses are not copied into it.

## Diagnostic interpretation

| Diagnostic | Meaning |
| --- | --- |
| Route/model not configured | The workbench lacks an explicit account-scoped bridge target; no model request was made |
| Loopback request could not complete | The local service was unreachable, timed out, or failed at transport level |
| HTTP 401/403/429/5xx | The selected bridge returned that status; investigate its authentication/quota/service state without copying credentials into this tool |
| Different or unreported model | The bridge response did not match the explicit model contract; no silent substitution is accepted |
| Incomplete/refusal/strict JSON failure | The response cannot satisfy the observation contract; affected shots remain unmodified |
| `health` is OK | The service is listening; this alone proves neither authenticated model access nor image/schema compatibility |

The repository tests use a disposable synthetic HTTP service and fake account/model identifiers. A real BridgeDeck inference remains a separate, explicitly authorized operator check. If that route is unavailable, the current Codex task can still use the native analysis contract without changing the tool's workflow.
