# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Stock Receipt (GRN) lifecycle and posting. Owned by S1.

To build: a module-level `TRANSITIONS` dict of `privacy.lifecycle.Transition` (Draft -> Posted by RECEIPT_POSTER_ROLES;
Posted -> Void by RECEIPT_VOID_ROLES with a reason), the append-only `log`, and the endpoints (type-annotated, POST):
`post(receipt: str) -> dict` and `void(receipt: str, reason: str) -> dict`. Posting sets `store` to the facility Main
Store (`stock.stores.store_for`), creates each Batch (`stock.batches.ensure_batch`, expiry required and not past),
writes `Stock Receipt Item.batch`, and posts a Material Receipt (`stock.postings.post_stock_entry`, reference this
receipt). Void cancels that Stock Entry (`stock.postings.cancel_stock_entry`), only if the stock has not moved on.
"""
