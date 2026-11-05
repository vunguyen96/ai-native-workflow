---
artifact: spec.md
ticket: CHORE-7
run: 1
author: stub-planner (scripted demo, no AI)
inputs: [ticket-context.md@f78cf75b]
overlay_applied: []
status: ready
---

# Spec: tracking number on the order page

## Behavior
Customers can see the carrier tracking number of a shipped order on the order page. Until the order ships the page says the number is not yet available.

## Acceptance criteria
| Id   | Criterion (given / when / then) | PRD requirement | Source |
|------|----------------------------------|-----------------|--------|
| AC-1 | Given a shipped order, when the order is fetched, then `tracking_number` is a string. | PRD-7#R2 | J-1 |
| AC-2 | Given an order that has not shipped, when it is fetched, then `tracking_number` is null. | PRD-7#R2 | J-1 |
| AC-3 | Given a shipped order, when the order page is shown, then it prints `Tracking: <number>`. | PRD-7#R2 | J-2 |
| AC-4 | Given a null tracking number, when the order page is shown, then it prints `Tracking: not yet available`. | PRD-7#R2 | J-3 |
| AC-5 | Given an order without the field (old API during a rolling deploy), when the page is shown, then it prints `Tracking: not yet available`. | PRD-7#R2 | added |

## Traceability
| Source id | Spec AC    | Status  | Reason |
|-----------|------------|---------|--------|
| J-1       | AC-1, AC-2 | changed | one criterion became two testable ones |
| J-2       | AC-3       | kept    | |
| J-3       | AC-4       | kept    | |
| none      | AC-5       | added   | the API and the web app are deployed one after the other, so the page must cope with the old response |

## Out of scope
Carrier links, tracking history, e-mail notifications.
