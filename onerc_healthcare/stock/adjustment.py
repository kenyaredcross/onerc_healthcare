# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Stock Adjustment lifecycle and posting. Owned by S1.

To build: a module-level `TRANSITIONS` dict (constants ADJUSTMENT_*): Draft -> Pending Approval (ADJUSTMENT_CREATOR_ROLES,
or ADJUSTMENT_PHARMACY_CREATOR_ROLES when the store is a Pharmacy store; sets `current_qty` per line), Pending Approval
-> Posted / Rejected (ADJUSTMENT_APPROVER_ROLES; reject needs a reason). Posting: Opening / Physical Count -> a Stock
Reconciliation at rate 0 per batch (new batches of an Opening Count via `stock.batches.ensure_batch`), Write-off ->
a Material Issue of the named batches (`stock.postings`). Every posting carries the reference fields.
"""
