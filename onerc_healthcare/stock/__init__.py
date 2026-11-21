# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 6S stock (D61 plan, OI-20): stores, receipts (GRN), S11 requisitions, adjustments and dispensing deduction.

Quantity only (C-5): ERPNext's stock engine (Warehouse, Item, Batch, Stock Entry, Stock Reconciliation, Stock Ledger
Entry) runs underneath with perpetual inventory off and every rate 0. Users never touch ERPNext stock screens; our
documents post Stock Entries server-side (`stock.postings`). The contract is docs/STOCK_CONTRACT.md.
"""
