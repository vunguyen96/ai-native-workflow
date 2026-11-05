---
artifact: inspection-report.md
ticket: CHORE-7
run: 1
author: stub-inspect-adapt (scripted demo, no AI)
inputs: [review-report.md@7db876e5]
overlay_applied: []
status: ready
---

# Inspection report: CHORE-7

## What happened
- Kind: story. Lanes ran: planner, qe, dev (shop-api, shop-web), reviewer, inspect-adapt.
- Rework loops: 1 (shop-web failed verify once).
- Sessions with telemetry: 7. Tokens in/out (synthetic demo numbers): 218,997 / 26,796.

## Findings
1. **shop-web shipped a null bug.** Dev wrote one local test (shipped order only). The null and missing-field cases were found only at verify, which cost one extra Dev session and one extra Reviewer session.
   Evidence: verify-report.md (attempt 1), metrics/dev-shop-web-a1.json, metrics/dev-shop-web-a2.json.
2. Spec and test plan were clean: no `spec_gap`, no question raised.

## Adapt
One candidate for the Dev lane (see ledger L-2026-11-001). It is advisory only and removable.
