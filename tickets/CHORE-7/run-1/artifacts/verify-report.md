---
artifact: verify-report.md
ticket: CHORE-7
run: 1
author: stub-reviewer (scripted demo, no AI)
inputs: [test-plan.md@17df49a3, changes@9b145ceb]
overlay_applied: []
status: ready
---

# Verify report (attempt 1)

Result: **FAIL**

## Coverage matrix
| AC | Repo | Test | Result |
|----|------|------|--------|
| AC-1 | shop-api | test_ac1_shipped_has_tracking_number | pass |
| AC-2 | shop-api | test_ac2_unshipped_has_null_tracking_number | pass |
| AC-3 | shop-web | test_ac3_shipped_shows_number | pass |
| AC-4 | shop-web | test_ac4_null_shows_not_yet_available | **FAIL** |
| AC-5 | shop-web | test_ac5_missing_field_shows_not_yet_available | **FAIL** |

## Failures
- shop-web: AC-4, AC-5
