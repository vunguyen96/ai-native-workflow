---
artifact: test-plan.md
ticket: CHORE-7
run: 1
author: stub-qe (scripted demo, no AI)
inputs: [spec.md@bedf896c, tasks.md@15c69d13]
overlay_applied: []
status: ready
---

# Test plan

## Coverage matrix
| AC   | Level | Repo | Test |
|------|-------|------|------|
| AC-1 | unit  | shop-api | tests/shop-api/test_acceptance.py::test_ac1_shipped_has_tracking_number |
| AC-2 | unit  | shop-api | tests/shop-api/test_acceptance.py::test_ac2_unshipped_has_null_tracking_number |
| AC-3 | unit  | shop-web | tests/shop-web/test_acceptance.py::test_ac3_shipped_shows_number |
| AC-4 | unit  | shop-web | tests/shop-web/test_acceptance.py::test_ac4_null_shows_not_yet_available |
| AC-5 | unit  | shop-web | tests/shop-web/test_acceptance.py::test_ac5_missing_field_shows_not_yet_available |

## Integration scenarios
- Given the new API is live and the old web app is running, when a customer opens a shipped order, then the page still renders.
- Given both are live, when a customer opens a shipped order, then the tracking number is visible.

## Risks and gaps
None. Integration scenarios are listed for a manual check (placeholder).
