# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Catchment Area (OUT-02, SDD §5.6): the residence locations an outreach team serves.

Each listed Residence Location counts with its whole subtree (nested set `lft`/`rgt`), so the package
(`outreach.package.get_patients`) covers every patient whose `krcs_residence` is in one of the subtrees.

Rules (validate):
- `facility` is a registration facility group (a Healthcare Service Unit group with a Facility Code) of this
  site's operation.
- At least one location; no location twice; every location is inside this operation's residence tree.
- **No redundant location** (decision O1): a location inside another listed location's subtree is refused,
  naming both, because it adds nothing and would hide a mistake (the planner probably meant a sibling).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from onerc_healthcare.registration.residence import RESIDENCE_DOCTYPE, get_residence_root_name


class CatchmentArea(Document):
	def validate(self):
		self.area_name = (self.area_name or "").strip()
		validate_operation_facility(self.facility)
		self.validate_locations()

	def validate_locations(self):
		rows = [r for r in self.get("locations") or [] if r.residence_location]
		if not rows:
			frappe.throw(_("Add at least one residence location."), title=_("Locations Required"))
		names = [r.residence_location for r in rows]
		duplicates = sorted({n for n in names if names.count(n) > 1})
		if duplicates:
			frappe.throw(
				_("{0} is listed more than once.").format(", ".join(frappe.bold(d) for d in duplicates)),
				title=_("Duplicate Location"),
			)
		bounds = {
			r.name: r
			for r in frappe.get_all(
				RESIDENCE_DOCTYPE, filters={"name": ["in", names]}, fields=["name", "lft", "rgt"]
			)
		}
		missing = [n for n in names if n not in bounds]
		if missing:
			frappe.throw(_("Residence Location {0} does not exist.").format(frappe.bold(missing[0])))
		root = get_residence_root_name()
		root_row = (
			frappe.db.get_value(RESIDENCE_DOCTYPE, root, ["lft", "rgt"], as_dict=True) if root else None
		)
		for name in names:
			row = bounds[name]
			if root_row and not (root_row.lft <= row.lft and row.rgt <= root_row.rgt):
				frappe.throw(
					_("{0} is not in this operation's residence tree ({1}).").format(
						frappe.bold(name), frappe.bold(root)
					),
					title=_("Invalid Location"),
				)
			for other in names:
				outer = bounds[other]
				if other != name and outer.lft < row.lft and row.rgt < outer.rgt:
					frappe.throw(
						_(
							"{0} is already inside {1}, which is listed with all the locations under it. Remove {0}."
						).format(frappe.bold(name), frappe.bold(other)),
						title=_("Redundant Location"),
					)


def validate_operation_facility(facility: str | None) -> None:
	"""Refuse anything but a registration facility group of this site's operation."""
	from onerc_healthcare.registration.walk_in import is_registration_facility
	from onerc_healthcare.setup.operations import get_facilities, get_operation

	if not is_registration_facility(facility) or (get_operation() and facility not in get_facilities()):
		frappe.throw(
			_(
				"{0} is not a facility of this operation. Choose a facility group with a Facility Code."
			).format(frappe.bold(facility or "")),
			title=_("Invalid Facility"),
		)
