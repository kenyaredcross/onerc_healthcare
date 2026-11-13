# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Global on_trash guard (hooks.py "*"): clinical records are never deleted (NFR-9, SDD §4.6).

Delete permission is also stripped from every DocPerm on clinical DocTypes (security.setup); this guard
is the server-side backstop that also stops Administrator and ``ignore_permissions`` code paths. Only
framework maintenance (install, migrate, patches, uninstall) may bypass it. Tests are not exempt:
EMRTestCase rolls back instead of deleting.

A record that is no longer active is flagged ``krcs_archived`` instead.
"""

import frappe
from frappe import _

from onerc_healthcare.constants import CLINICAL_DOCTYPES

ARCHIVED_FIELD = "krcs_archived"


def is_maintenance() -> bool:
	"""True while the framework itself installs, migrates, patches or uninstalls apps."""
	flags = frappe.flags
	return bool(flags.in_install or flags.in_migrate or flags.in_patch or flags.in_uninstall)


def on_trash(doc, method=None) -> None:
	if doc.doctype not in CLINICAL_DOCTYPES or is_maintenance():
		return
	frappe.throw(
		_("{0} {1} is a clinical record and cannot be deleted. Mark it as archived instead.").format(
			_(doc.doctype), frappe.bold(doc.name)
		),
		frappe.PermissionError,
		title=_("Deletion not allowed"),
	)


def block_transaction_deletion(doc, method=None) -> None:
	"""Hook: Transaction Deletion Record ``before_insert`` / ``before_submit``.

	ERPNext's company "Delete Transactions" bulk-deletes company-linked rows with ``frappe.db.delete``,
	which never reaches on_trash, and it inserts the record with ignore_permissions. Refuse it outright
	(NFR-9); clearing a test company is a maintenance job for a bench console, not the desk.
	"""
	if is_maintenance():
		return
	frappe.throw(
		_("Transaction deletion is disabled on the EMR: clinical records are never deleted."),
		frappe.PermissionError,
		title=_("Deletion not allowed"),
	)
