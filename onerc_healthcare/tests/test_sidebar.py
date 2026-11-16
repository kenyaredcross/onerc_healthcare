# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""EMR DocTypes open under Marley's sidebars, not bare EMR module sidebars (D25, D24)."""

import frappe
from frappe.boot import build_entity_module_map, get_module_sidebars

from onerc_healthcare import desk_sidebar
from onerc_healthcare.constants import ROLE_RECEPTIONIST
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import make_user

SETTINGS_PAGES = (
	"EMR Registration Settings",
	"EMR Naming Settings",
	"Patient Identifier Type",
	"Residence Location",
)


def entity_map() -> dict:
	frappe.clear_cache(user=frappe.session.user)
	return build_entity_module_map(get_module_sidebars())


class TestEMRSidebars(EMRTestCase):
	def setUp(self):
		super().setUp()
		# The savepoint rollback restores rows but not the cached boot/sidebars.
		self.addCleanup(frappe.clear_cache)

	def test_patient_visit_opens_under_healthcare(self):
		self.assertEqual(entity_map().get("Patient Visit"), "Healthcare")

	def test_settings_open_under_healthcare_setup(self):
		mapping = entity_map()
		for doctype in SETTINGS_PAGES:
			self.assertEqual(mapping.get(doctype), "Healthcare Setup", doctype)

	def test_patient_visit_sits_after_patient(self):
		items = frappe.get_all(
			"Sidebar Item",
			filters={"parent": "Healthcare"},
			fields=["link_to"],
			order_by="idx asc",
			pluck="link_to",
		)
		self.assertEqual(items[items.index("Patient") + 1], "Patient Visit")

	def test_receptionist_sees_patient_visit_under_healthcare(self):
		frappe.set_user(make_user("sidebar-reception", [ROLE_RECEPTIONIST]))
		self.assertEqual(entity_map().get("Patient Visit"), "Healthcare")

	def test_install_is_idempotent(self):
		desk_sidebar.install()
		self.assertFalse(desk_sidebar.install())

	def test_restored_after_marley_reload(self):
		"""A Marley upgrade reloads its sidebar without our rows; the next migrate puts them back once."""
		frappe.db.delete("Sidebar Item", {"parent": "Healthcare", "key": ("like", "krcs-%")})
		self.assertTrue(desk_sidebar.install())
		self.assertFalse(desk_sidebar.install())
		self.assertEqual(
			frappe.db.count("Sidebar Item", {"parent": "Healthcare", "link_to": "Patient Visit"}), 1
		)


OUTREACH_PAGES = (
	"Outreach Session",
	"Catchment Area",
	"Sync Conflict",
	"Outreach Sync Record",
	"Outreach Sync Batch",
	"Outreach Device",
	"Outreach Device Grant",
	"EMR Outreach Settings",
)


class TestOutreachArea(EMRTestCase):
	"""Outreach is its own area beside Outpatient, Inpatient and Rehabilitation (Phase 4)."""

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.clear_cache)

	def dock(self) -> list[str]:
		return frappe.get_all(
			"Dock Item",
			filters={"parent": desk_sidebar.MARLEY_DOCK, "parenttype": "Dock"},
			pluck="link_to",
			order_by="idx asc",
		)

	def test_outreach_in_marley_dock_after_rehabilitation(self):
		dock = self.dock()
		self.assertEqual(dock.count("Outreach"), 1)
		self.assertEqual(dock.index("Outreach"), dock.index("Rehabilitation") + 1)

	def test_outreach_pages_open_under_outreach(self):
		mapping = entity_map()
		for doctype in OUTREACH_PAGES:
			self.assertEqual(mapping.get(doctype), "Outreach", doctype)
		self.assertTrue(frappe.db.exists("Workspace", "Outreach"))

	def test_no_outreach_rows_left_in_healthcare(self):
		links = frappe.get_all(
			"Sidebar Item", filters={"parent": ["in", ["Healthcare", "Healthcare Setup"]]}, pluck="link_to"
		)
		self.assertFalse(set(links) & set(OUTREACH_PAGES))

	def test_dock_is_idempotent_and_repairs_order(self):
		self.assertFalse(desk_sidebar.apply_dock())
		row = frappe.db.get_value(
			"Dock Item", {"parent": desk_sidebar.MARLEY_DOCK, "link_to": "Outreach"}, "name"
		)
		frappe.db.set_value("Dock Item", row, "idx", 99, update_modified=False)
		self.assertTrue(desk_sidebar.apply_dock())
		dock = self.dock()
		self.assertEqual(dock.index("Outreach"), dock.index("Rehabilitation") + 1)
		self.assertFalse(desk_sidebar.apply_dock())

	def test_outreach_stays_on_marleys_rail(self):
		"""Our app mounts on Marley's dock (companion app), so opening Outreach keeps Marley's rail and switcher
		on screen instead of switching the desk to an app with no rail of its own."""
		from frappe.desk.doctype.dock.dock import get_app_base, mounted_apps

		frappe.local.dock_cache = {"app_dock": {}, "mounts": None}
		self.assertEqual(mounted_apps().get("onerc_healthcare"), desk_sidebar.MARLEY_DOCK)
		base = [r.get("link_to") for r in get_app_base(desk_sidebar.MARLEY_DOCK)]
		self.assertEqual(base.count("Outreach"), 1)
		self.assertEqual(base.index("Outreach"), base.index("Rehabilitation") + 1)


# area -> (dock neighbour it follows, pages that open under it)
OWN_AREAS = {
	"Pharmacy": ("Diagnostics", ("Dispensing Record", "Medication Request")),
	"MOH Reporting": (
		"Outreach",
		("MOH Report Run", "MOH Report", "MOH Disease Category", "MOH Diagnosis Mapping", "ICD-11 Block"),
	),
}


class TestPharmacyAndMOHAreas(EMRTestCase):
	"""Pharmacy and MOH Reporting are areas of their own on Marley's rail, like Outreach."""

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.clear_cache)

	def test_areas_in_dock_after_their_neighbours(self):
		from frappe.desk.doctype.dock.dock import get_app_base

		frappe.local.dock_cache = {"app_dock": {}, "mounts": None}
		base = [r.get("link_to") for r in get_app_base(desk_sidebar.MARLEY_DOCK)]
		for area, (after, _pages) in OWN_AREAS.items():
			self.assertEqual(base.count(area), 1, area)
			self.assertEqual(base.index(area), base.index(after) + 1, area)

	def test_pages_open_under_their_area(self):
		mapping = entity_map()
		for area, (_after, pages) in OWN_AREAS.items():
			self.assertTrue(frappe.db.exists("Workspace", area), area)
			for page in pages:
				self.assertEqual(mapping.get(page), area, page)

	def test_moved_rows_gone_from_healthcare(self):
		moved = {page for _after, pages in OWN_AREAS.values() for page in pages} | {"Pharmacy Worklist"}
		links = frappe.get_all(
			"Sidebar Item", filters={"parent": ["in", ["Healthcare", "Healthcare Setup"]]}, pluck="link_to"
		)
		self.assertFalse(set(links) & moved)


class TestDropdowns(EMRTestCase):
	"""Our remaining Healthcare, Setup and Diagnostics rows are Marley-style dropdowns, clustered with Marley's."""

	def items(self, sidebar: str) -> list:
		return frappe.get_all(
			"Sidebar Item",
			filters={"parent": sidebar, "parenttype": "Sidebar"},
			fields=["type", "label", "link_to", "child", "indent", "keep_closed", "key"],
			order_by="idx asc",
		)

	def test_our_sections_fold_their_links(self):
		for sidebar in desk_sidebar.SIDEBAR_ROWS:
			rows = self.items(sidebar)
			section = None
			for row in rows:
				if row.type == "Section Break":
					section = row
					if (row.key or "").startswith(desk_sidebar.KEY_PREFIX):
						self.assertEqual((row.indent, row.keep_closed), (1, 1), row.label)
				elif (row.key or "").startswith(desk_sidebar.KEY_PREFIX) and section and section.key:
					self.assertEqual(row.child, 1, row.label)

	def test_healthcare_dropdowns_sit_with_marleys(self):
		rows = self.items("Healthcare")
		labels = [r.label for r in rows]
		self.assertEqual(labels.index("Patient Visit"), labels.index("Patient") + 1)
		first_section = next(i for i, r in enumerate(rows) if r.type == "Section Break")
		# No loose (non-child) link after the first dropdown: every link below it folds into a section.
		self.assertFalse([r.label for r in rows[first_section:] if r.type == "Link" and not r.child])
		self.assertEqual(rows[first_section].label, "Front Desk")

	def test_install_is_idempotent(self):
		self.assertFalse(desk_sidebar.install())
