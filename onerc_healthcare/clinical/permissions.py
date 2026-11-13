# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Encounter permission hooks (wired in hooks.py). Owned by B1. Mental-health visibility pending OI-7."""


def encounter_query(user: str | None = None, doctype: str | None = None) -> str:
	return ""


def encounter_has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	return True
