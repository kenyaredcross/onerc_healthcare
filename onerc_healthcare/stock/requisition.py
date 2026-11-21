# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Stock Requisition (S11) lifecycle and postings. Owned by S2.

To build: a module-level `TRANSITIONS` dict (constants S11_*): Draft -> Requested (S11_REQUESTER_ROLES[point_of_use]),
Requested -> Approved / Rejected (S11_APPROVER_ROLES; may lower `qty_approved`; reject needs a reason),
Approved -> Issued (S11_ISSUER_ROLES; may issue less than approved with `short_issue_reason`; Material Transfer
Main Store -> In Transit, FEFO), Issued -> Received (the requester roles of the destination; `qty_received`;
In Transit -> destination store, or a Material Issue out of In Transit for a point of use, which consumes on
receipt; a shortfall stays In Transit and sets `transit_shortfall`), Cancel (requester at Draft/Requested, the
approver at Approved). Signatures (`*_by` / `*_at`) at each step, append-only `log`, the S11 print format and the
form buttons. Stores via `stock.stores.store_for`; postings via `stock.postings`; pre-checks via `stock.balances`.
"""
