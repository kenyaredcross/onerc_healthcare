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
