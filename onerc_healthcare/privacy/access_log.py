# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Access logging hooks (wired in hooks.py). Owned by B4 (FR-40, NFR-7)."""


def log_open(doc, method=None) -> None:
	"""doc_events onload for every DocType in constants.ACCESS_LOGGED_DOCTYPES."""
	pass


def mirror_core_access_log(doc, method=None) -> None:
	"""doc_events after_insert on Frappe's core Access Log (prints, exports, reports)."""
	pass
