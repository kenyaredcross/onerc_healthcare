# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Archive closed data-protection records (NFR-9: flag, never delete). Medical In-Charge only.

| Record              | Archivable when             | Roles             |
|---------------------|-----------------------------|-------------------|
| Data Access Request | status Closed               | Medical In-Charge |
| Consent Statement   | status Active or Retired    | Medical In-Charge |

Only `krcs_archived` changes. The controllers refuse any other change of `krcs_archived` (it is saved
here with ``flags.from_workflow``), and the change is kept in the record's Version history.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from onerc_healthcare.constants import CONSENT_STATEMENT_DOCTYPE, DATA_ACCESS_REQUEST_DOCTYPE, ROLE_MIC
from onerc_healthcare.privacy.lifecycle import has_any_role, save_from_workflow

ARCHIVED_FIELD = "krcs_archived"
ARCHIVE_ROLES = (ROLE_MIC,)
# DocType -> statuses in which the record may be archived (or un-archived).
ARCHIVABLE: dict[str, tuple[str, ...]] = {
	DATA_ACCESS_REQUEST_DOCTYPE: ("Closed",),
	CONSENT_STATEMENT_DOCTYPE: ("Active", "Retired"),
}


class ArchiveNotAllowedError(frappe.ValidationError):
	"""The record cannot be archived in its current status."""


def can_archive(doc, user: str | None = None) -> bool:
	return (
		doc.doctype in ARCHIVABLE
		and doc.meta.has_field(ARCHIVED_FIELD)
		and doc.get("status") in ARCHIVABLE[doc.doctype]
		and has_any_role(ARCHIVE_ROLES, user)
	)


def set_archived(doctype: str, name: str, archived: bool = True):
	if doctype not in ARCHIVABLE:
		frappe.throw(_("{0} records cannot be archived here.").format(_(doctype)), ArchiveNotAllowedError)
	if not has_any_role(ARCHIVE_ROLES):
		frappe.throw(_("Only the Medical In-Charge can archive these records."), frappe.PermissionError)
	doc = frappe.get_doc(doctype, name, for_update=True)
	doc.check_permission("read")
	if not doc.meta.has_field(ARCHIVED_FIELD):
		frappe.throw(_("{0} has no Archived field on this site.").format(_(doctype)), ArchiveNotAllowedError)
	if doc.status not in ARCHIVABLE[doctype]:
		frappe.throw(
			_("{0} {1} is {2}; it can be archived only when {3}.").format(
				_(doctype), doc.name, _(doc.status), " / ".join(_(s) for s in ARCHIVABLE[doctype])
			),
			ArchiveNotAllowedError,
		)
	doc.set(ARCHIVED_FIELD, 1 if archived else 0)
	save_from_workflow(doc)
	return doc


@frappe.whitelist(methods=["POST"])
def archive_record(doctype: str, name: str, archived: int = 1) -> dict:
	"""Form button: archive (or un-archive) a closed Data Access Request or a non-Draft Consent Statement."""
	doc = set_archived(doctype, name, bool(cint(archived)))
	return {"name": doc.name, ARCHIVED_FIELD: doc.get(ARCHIVED_FIELD)}
