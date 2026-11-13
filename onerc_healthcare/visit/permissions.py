# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Visit permission hooks (wired in hooks.py). Owned by A2."""


def patient_visit_query(user: str | None = None, doctype: str | None = None) -> str:
	return ""


def patient_visit_has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	return True
