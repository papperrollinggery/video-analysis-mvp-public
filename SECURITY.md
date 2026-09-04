# Security Policy

## Supported versions

This project is pre-1.0. Security fixes are applied to the latest revision only; no long-term support window has been declared.

## Reporting a vulnerability

Do not open a public issue containing exploit details, private media, passwords, API keys, or local filesystem contents.

Private vulnerability reporting is enabled for the canonical repository:
[Security → Report a vulnerability](https://github.com/papperrollinggery/video-analysis-mvp-public/security/advisories/new).
Use that form for exploit details. If the form is unavailable, open only a
minimal public issue asking for a private contact method; do not include the
vulnerability, secrets, private media, or local filesystem contents.

Include, when safe:

- affected revision and operating system;
- attack preconditions and impact;
- a minimal reproduction using synthetic data;
- whether secrets or private media may have been exposed;
- a suggested mitigation, if known.

## Security boundaries

- The local HTTP server enforces loopback binding plus loopback Host/Origin checks and CSRF tokens for mutations. These are local-browser defenses, not multi-user authentication. Do not proxy or expose it to an untrusted network.
- URL ingest downloads third-party content only when the URL/password are read from owner-only value files and the CLI risk is explicitly acknowledged. Confirm permission and treat redirects, later DNS resolution and remote metadata as untrusted; use an egress-restricted environment for untrusted URLs.
- Configuring an external vision provider can send selected frames outside the local machine.
- Project files and generated HTML may contain private media, transcripts, and model output. Store and share them accordingly.
- The preview server confines files to manifested project roots, requires professional report deliverables to belong to a canonical committed SHA-256-bound generation, sandboxes generated HTML, and refuses active SVG/XML/XHTML documents; do not weaken those boundaries when adding artifact types.
- CSV exports neutralize formula-like text, but recipients should still treat spreadsheets and all generated content as untrusted.
- Never commit `.env` files, provider credentials, password-protected source URLs, or `analysis-projects/` contents.
- Prefer environment variables for provider keys. File-backed settings live under the selected workspace with owner-only permissions on supported POSIX systems; changing a provider endpoint requires key re-entry.

If a credential appears in an issue, log, generated artifact, or commit, revoke and rotate it before continuing investigation.
