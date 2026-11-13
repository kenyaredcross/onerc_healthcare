# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Access Log Entry permission hooks (wired in hooks.py). Owned by B4."""


def access_log_query(user: str | None = None, doctype: str | None = None) -> str:
	return ""


def access_log_has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	return True
