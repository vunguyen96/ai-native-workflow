---
artifact: design.md
ticket: CHORE-7
run: 1
author: stub-planner (scripted demo, no AI)
inputs: [ticket-context.md@f78cf75b, spec.md@bedf896c]
overlay_applied: []
status: ready
---

# Design

- shop-api: add `tracking_number` to every order dict. Value is a string for shipped orders, `None` otherwise.
  This is an additive change to the response, so older clients keep working.
- shop-web: `render_order` appends one line, `Tracking: <number>` or `Tracking: not yet available`.
  Use `order.get("tracking_number")` so a missing field behaves like null (AC-5).
- Deploy shop-api first. If shop-web went first it would show "not yet available" for shipped orders until the API is live.
