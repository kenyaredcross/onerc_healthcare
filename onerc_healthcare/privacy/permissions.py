# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Access Log Entry permission hooks (wired in hooks.py). Owned by B4.

Only the Medical In-Charge and the EMR System Administrator read the access log (SDD §5.10, D15); the
log holds metadata only. Every other user gets ``1=0`` on lists and False on documents. Nobody may
create, write, delete, submit or share entries: only our code inserts them (ignore_permissions), and
Administrator is let through by Frappe before these hooks run.
"""

import frappe

from onerc_healthcare.constants import ROLE_MIC, ROLE_SYSADMIN

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
