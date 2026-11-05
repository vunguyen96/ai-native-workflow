---
artifact: ticket-context.md
ticket: CHORE-7
run: 1
author: system
status: draft
---
# Ticket context

# DEMO-1: Show the tracking number on the order page

Kind: story. CHORE refs: CHORE-7#R2.

## Description
Customers ask support where their parcel is. Show the carrier tracking number on the order page.

## Jira criteria
| Id  | Criterion |
|-----|-----------|
| J-1 | The order API returns a tracking number once an order has shipped. |
| J-2 | The order page shows the tracking number for shipped orders. |
| J-3 | The order page shows "not yet available" while there is no tracking number. |

## Notes
Repos involved are not named by the requester. The Planner finds them (shop-api, shop-web).
