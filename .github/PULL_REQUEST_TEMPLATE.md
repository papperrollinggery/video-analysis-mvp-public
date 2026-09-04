## Outcome

What user-visible or contributor-visible result does this change deliver?

## Scope

- Included:
- Deliberately excluded:

## Evidence

List commands and their actual results. Remove credentials, private URLs, media, and sensitive paths.

- [ ] `python3 -m compileall -q src`
- [ ] `sh scripts/smoke-test.sh`
- [ ] `sh scripts/api-smoke-test.sh`
- [ ] `npm --prefix frontend run test:integration`
- [ ] `npm --prefix frontend run build`
- [ ] Desktop and mobile screenshots attached for UI changes
- [ ] Not applicable checks are explained below

## Data and trust boundaries

- [ ] No secret, password, private media, private URL, or generated workspace is included
- [ ] External network/provider behavior is disclosed
- [ ] Measured, estimated, provider-annotated, and human-reviewed data remain distinguishable
- [ ] Schema or artifact changes include compatibility/migration notes
- [ ] Documentation matches the implemented commands and limits

## Residual risk

What remains unverified or intentionally deferred?
