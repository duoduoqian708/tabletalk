# Security Policy

## Reporting a vulnerability

Do NOT file a public GitHub issue for security vulnerabilities.

Email the maintainers privately (or use GitHub's private "Report a vulnerability" advisory). Include steps to reproduce, affected versions, and potential impact. We will acknowledge within 3 business days and coordinate a fix before public disclosure.

## Scope

TableTalk's trust story depends on the local safety gate (`backend/app/safety/`) and controlled egress. The gate is model-independent, offline-capable, and fully unit-tested. Any bypass, privilege escalation, or exfiltration of row data outside the single AI pipeline (`backend/app/ai/context.py` → `gateway.py`) is treated as high severity.

## Supported versions

`main` is the supported line during the current Phase 0/1 development.
