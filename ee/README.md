# ee/ — Enterprise Edition (commercial, closed)

This directory is the open-core boundary.

- `ee/` ships under a **commercial license** (not Apache-2.0). Empty during Phase 0.
- OSS core (`backend/`, `frontend/`, `docs/product-handbook/`) stays Apache-2.0 per `LICENSE`.
- Future enterprise modules (Phase 3: credential vault, DML approval, unified AI egress, team audit) land here as `ee/` packages, imported by the core via stable interfaces (settings/audit/pool). They never leak into the core build.

Guards:
- Do not import `ee/` from OSS tests — core tests must stay green without `ee/`.
- Gate logic (`app/safety/`) never moves to `ee/`; it is the trust anchor and will be independently open-sourced as `tabletalk-gate`.
- Brand name & logo remain trademarks (see `LICENSE` §6).
