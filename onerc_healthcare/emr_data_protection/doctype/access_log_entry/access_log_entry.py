# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Access Log Entry: append-only audit of record opens, prints, exports, reports and downloads
(FR-40, NFR-7, SDD §5.10). Written only by onerc_healthcare.privacy.access_log.

- Never updated: `validate` refuses any save of an existing entry.
- Never deleted: listed in constants.CLINICAL_DOCTYPES, so the global on_trash guard refuses it.
- `patient` is plain Data, so a patient merge (registration.merge) never rewrites the audit trail.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from onerc_healthcare.privacy.access_log import ACTIONS


class AccessLogImmutableError(frappe.PermissionError):
	"""An existing Access Log Entry was saved again."""


class AccessLogEntry(Document):
	def validate(self):
		if not self.is_new():
			frappe.throw(
				_("Access Log Entries are append-only and cannot be changed."),
				AccessLogImmutableError,
				title=_("Not Allowed"),
			)
		if self.action not in ACTIONS:
			frappe.throw(_("Unknown access action {0}.").format(self.action))
