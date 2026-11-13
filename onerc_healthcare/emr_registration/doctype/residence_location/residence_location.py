# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Residence Location tree (SDD 4.5). Levels and root come from the operation's configuration."""

import frappe
from frappe import _
from frappe.utils.nestedset import NestedSet


class ResidenceLocation(NestedSet):
	nsm_parent_field = "parent_residence_location"

	def autoname(self) -> None:
		"""Top-level locations keep their name; deeper ones carry their parent's name to stay unique."""
		self.location_name = (self.location_name or "").strip()
		parent = self.parent_residence_location
		if parent and frappe.db.get_value(self.doctype, parent, "parent_residence_location"):
			self.name = f"{self.location_name} - {parent}"
		else:
			self.name = self.location_name

	def validate(self) -> None:
		from onerc_healthcare.registration.residence import get_residence_levels

		self.location_name = (self.location_name or "").strip()
		if not self.parent_residence_location:
			self.validate_one_root()
			self.level = None
			return

		parent = frappe.db.get_value(
			self.doctype, self.parent_residence_location, ["is_group", "level"], as_dict=True
		)
		if not parent:
			frappe.throw(_("Parent Location {0} does not exist.").format(self.parent_residence_location))
		if not parent.is_group:
			frappe.throw(
				_("Parent Location {0} is not a group. Tick 'Is Group' on it first.").format(
					frappe.bold(self.parent_residence_location)
				)
			)

		levels = get_residence_levels()
		if not self.level:
			frappe.throw(
				_("Level is required. Choose one of: {0}.").format(", ".join(levels)),
				title=_("Missing Level"),
			)
		if self.level not in levels:
			frappe.throw(
				_("Level {0} is not used in this operation. Choose one of: {1}.").format(
					frappe.bold(self.level), ", ".join(levels)
				)
			)
		parent_rank = levels.index(parent.level) if parent.level in levels else -1
		if levels.index(self.level) <= parent_rank:
			frappe.throw(
				_("A {0} cannot sit under a {1}. Pick a parent at a higher level.").format(
					self.level, parent.level
				)
			)
