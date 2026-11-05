---
artifact: tasks.md
ticket: CHORE-7
run: 1
author: stub-planner (scripted demo, no AI)
inputs: [ticket-context.md@f78cf75b, spec.md@bedf896c, design.md@4e75de4a]
overlay_applied: []
status: ready
---

# Tasks

## Tasks
| Task | Repo | Covers | Depends on |
|------|------|--------|------------|
| T-1 Add `tracking_number` to the order response | shop-api | AC-1, AC-2 | none |
| T-2 Render the tracking line on the order page | shop-web | AC-3, AC-4, AC-5 | T-1 (deploy order) |

## Release
deploy_order: ordered
sequence: shop-api, shop-web
reason: shop-web reads a field that only exists after shop-api is deployed (AC-5 softens this, but the order still matters for users).
