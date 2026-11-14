# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Access Log Entry and File permission hooks (wired in hooks.py). Owned by B4.

Only the Medical In-Charge and the EMR System Administrator read the access log (SDD §5.10, D15); the
log holds metadata only. Every other user gets ``1=0`` on lists and False on documents. Nobody may
create, write, delete, submit or share entries: only our code inserts them (ignore_permissions), and
Administrator is let through by Frappe before these hooks run.
"""

import frappe

from onerc_healthcare.constants import DATA_ACCESS_REQUEST_DOCTYPE, ROLE_MIC, ROLE_SYSADMIN

ACCESS_LOG_READERS = (ROLE_MIC, ROLE_SYSADMIN)
READ_PTYPES = ("read", "select", "report", "export", "print")


def can_read_access_log(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(ACCESS_LOG_READERS) & set(frappe.get_roles(user)))


def access_log_query(user: str | None = None, doctype: str | None = None) -> str:
	return "" if can_read_access_log(user) else "1=0"


def access_log_has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	if not can_read_access_log(user):
		return False
	return (ptype or "read") in READ_PTYPES


def is_extract_file(doc) -> bool:
	"""A file attached to a Data Access Request, or the private extract a request points at."""
	if doc.get("attached_to_doctype") == DATA_ACCESS_REQUEST_DOCTYPE:
		return True
	url = doc.get("file_url")
	return bool(
		doc.get("is_private") and url and frappe.db.exists(DATA_ACCESS_REQUEST_DOCTYPE, {"extract_file": url})
	)


def file_has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	"""has_permission hook on File (H1): Data Access Request files are closed to every user.

	The record extract is served only by `privacy.extract.download_extract` (HIS Officer, Medical
	In-Charge), which logs each download. Administrator is let through by Frappe before this hook.
	"""
	return not is_extract_file(doc)
