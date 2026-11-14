# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH 705A/705B disease line (PROVISIONAL — HIS Officer to confirm, OI-5). Counting rules:
`reporting.diagnosis`; seeds and the resolver: `reporting.mapping`."""

import frappe
from frappe import _
from frappe.model.document import Document

from onerc_healthcare.constants import MOH_CATEGORY_DOCTYPE
from onerc_healthcare.reporting import mapping


class MOHDiseaseCategory(Document):
	def validate(self) -> None:
		self._validate_catch_all()
		self._validate_confirmation()
		mapping.mark_edited(self, mapping.CATEGORY_SEED_FIELDS)

	def _validate_catch_all(self) -> None:
		if not self.is_catch_all:
			return
		other = frappe.db.get_value(
			MOH_CATEGORY_DOCTYPE, {"is_catch_all": 1, "name": ["!=", self.name or ""]}, "name"
		)
		if other:
			frappe.throw(_("{0} is already the catch-all category.").format(frappe.bold(other)))
		if self.excluded or self.requires_lab_confirmation:
			frappe.throw(_("The catch-all category cannot be excluded or need laboratory confirmation."))
		if self.form != mapping.FORM_BOTH:
			frappe.throw(_("The catch-all category must be on both forms (Form: Both)."))

	def _validate_confirmation(self) -> None:
		if not self.requires_lab_confirmation:
			return
		if not self.fallback_category or self.fallback_category == self.name:
			frappe.throw(_("Choose a fallback category other than this one."))
		if frappe.db.get_value(MOH_CATEGORY_DOCTYPE, self.fallback_category, "requires_lab_confirmation"):
			frappe.throw(_("The fallback category must not itself need laboratory confirmation."))
		tests = mapping.split_lines(self.confirmation_tests)
		if not tests or not mapping.split_lines(self.positive_results):
			frappe.throw(_("List the confirmation tests and their positive results, one per line."))
		missing = [t for t in tests if not frappe.db.exists("Observation Template", t)]
		if missing:
			frappe.msgprint(
				_("These tests are not Observation Templates on this site: {0}").format(", ".join(missing)),
				indicator="orange",
				alert=True,
			)
