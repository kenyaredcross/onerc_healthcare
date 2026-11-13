# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Roles, permissions, billing lock-down, delete guard and session security.

Requirements: FR-39, NFR-5, NFR-8, NFR-9, C-5; SDD v0.1 §4.6, §6.1, §6.3.
"""

import frappe

from onerc_healthcare.constants import (
	ALL_ROLES,
	CLINICAL_DOCTYPES,
	ROLE_CLINICIAN,
	ROLE_HEAD_OPS,
	ROLE_HIS,
	ROLE_LAB,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_OUTREACH,
	ROLE_PHARMACIST,
	ROLE_RECEPTIONIST,
	ROLE_SYSADMIN,
	ROLE_TRIAGE,
)
from onerc_healthcare.security import matrix
from onerc_healthcare.security.delete_guard import ARCHIVED_FIELD, on_trash
from onerc_healthcare.security.setup import clinical_doctypes_with_custom_perms
from onerc_healthcare.security.user_guard import role_profile_validate, user_validate
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import make_patient, make_user

BILLING_CHECKED = (
	"Sales Invoice",
	"Payment Entry",
	"Journal Entry",
	"Price List",
	"Item Price",
	"POS Invoice",
	"Fee Validity",
)


def role_user(role: str) -> str:
	return make_user("sec-" + frappe.scrub(role).replace("_", "-"), [role])


def can(doctype: str, ptype: str, user: str) -> bool:
	return bool(frappe.has_permission(doctype, ptype, user=user))


class TestBillingLockDown(EMRTestCase):
	def test_no_emr_role_can_read_or_create_billing_documents(self):
		"""C-5: no EMR role has any right on invoices, payments, journals, price lists or fees."""
		for role in ALL_ROLES:
			user = role_user(role)
			for doctype in BILLING_CHECKED:
				if not frappe.db.exists("DocType", doctype):
					continue
				for ptype in ("read", "create", "write"):
					self.assertFalse(can(doctype, ptype, user), f"{role} can {ptype} {doctype}")

	def test_no_custom_docperm_for_emr_roles_on_billing(self):
		"""C-5: the lock-down removes any Custom DocPerm an EMR role holds on a billing DocType."""
		rows = frappe.get_all(
			"Custom DocPerm",
			filters={"parent": ("in", list(matrix.BILLING_DOCTYPES)), "role": ("in", list(ALL_ROLES))},
		)
		self.assertEqual(rows, [])

	def test_selling_price_lists_disabled(self):
		"""C-5: no enabled selling Price List; buying lists are left for pharmacy procurement."""
		self.assertEqual(frappe.get_all("Price List", filters={"enabled": 1, "selling": 1}), [])

	def test_billing_workspaces_hidden_from_emr_roles(self):
		"""C-5: billing / stock workspaces carry a roles table without any EMR role."""
		for workspace in matrix.RESTRICTED_WORKSPACES:
			if not frappe.db.exists("Workspace", workspace):
				continue
			roles = set(
				frappe.get_all(
					"Has Role", filters={"parenttype": "Workspace", "parent": workspace}, pluck="role"
				)
			)
			self.assertTrue(roles, workspace)
			self.assertFalse(roles & set(ALL_ROLES), workspace)

	def test_sysadmin_sees_users_workspace(self):
		"""SDD §6.1: the EMR System Administrator reaches user management."""
		roles = frappe.get_all(
			"Has Role", filters={"parenttype": "Workspace", "parent": "Users"}, pluck="role"
		)
		self.assertIn(ROLE_SYSADMIN, roles)


class TestDeleteGuard(EMRTestCase):
	def test_guard_refuses_each_clinical_doctype(self):
		"""NFR-9: on_trash refuses deletion of every clinical DocType, whatever the user."""
		for doctype in CLINICAL_DOCTYPES:
			if not frappe.db.exists("DocType", doctype):
				continue
			with self.assertRaises(frappe.PermissionError, msg=doctype):
				on_trash(frappe.new_doc(doctype))

	def test_guard_ignores_other_doctypes(self):
		on_trash(frappe.new_doc("ToDo"))

	def test_guard_allows_framework_maintenance(self):
		"""SDD §4.6: install / migrate / patch / uninstall may still remove rows."""
		for flag in ("in_install", "in_migrate", "in_patch", "in_uninstall"):
			old = frappe.flags.get(flag)
			frappe.flags[flag] = True
			try:
				on_trash(frappe.new_doc("Patient"))
			finally:
				frappe.flags[flag] = old

	def test_administrator_cannot_delete_patient(self):
		"""NFR-9: even Administrator (all permissions) cannot delete an inserted Patient."""
		patient = make_patient()
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("Patient", patient.name)
		self.assertTrue(frappe.db.exists("Patient", patient.name))

	def test_no_delete_permission_on_clinical_doctypes(self):
		"""NFR-9 / SDD §4.6: no Custom DocPerm on a clinical DocType grants delete."""
		rows = frappe.get_all(
			"Custom DocPerm",
			filters={"parent": ("in", list(CLINICAL_DOCTYPES)), "delete": 1},
			fields=["parent", "role"],
		)
		self.assertEqual(rows, [])

	def test_standard_delete_is_masked_by_custom_perms(self):
		"""SDD §4.6: Custom DocPerms exist, so Marley's standard delete rights no longer apply,
		including for System Manager."""
		sysmgr = make_user("sec-system-manager", ["System Manager", "Physician", "Nursing User"])
		for doctype in clinical_doctypes_with_custom_perms():
			self.assertTrue(frappe.db.exists("Custom DocPerm", {"parent": doctype}), doctype)
			self.assertFalse(can(doctype, "delete", sysmgr), doctype)

	def test_no_emr_role_has_delete_anywhere(self):
		"""Hard rule: no EMR role holds delete on any DocType."""
		rows = frappe.get_all(
			"Custom DocPerm",
			filters={"role": ("in", list(ALL_ROLES)), "delete": 1},
			fields=["parent", "role"],
		)
		self.assertEqual(rows, [])

	def test_admin_and_master_doctypes_keep_standard_delete(self):
		"""M7: delete is stripped from clinical DocTypes only. A System Manager still lifts a restriction
		by deleting its User Permission, and still cannot delete a Patient."""
		sysmgr = make_user("sec-system-manager-del", ["System Manager", "Healthcare Administrator"])
		for doctype in ("User Permission", "User", "Role Profile", "Medical Department", "Code Value"):
			self.assertTrue(can(doctype, "delete", sysmgr), doctype)
		self.assertFalse(can("Patient", "delete", sysmgr))

	def test_transaction_deletion_closed(self):
		"""NFR-9 / H2: no role may create a Transaction Deletion Record or delete a Company, and the
		record is refused even when inserted with ignore_permissions."""
		from onerc_healthcare.security.delete_guard import block_transaction_deletion

		sysmgr = make_user("sec-system-manager-tdr", ["System Manager", "Accounts Manager"])
		if frappe.db.exists("DocType", "Transaction Deletion Record"):
			self.assertFalse(can("Transaction Deletion Record", "create", sysmgr))
			self.assertFalse(can("Transaction Deletion Record", "submit", sysmgr))
			with self.assertRaises(frappe.PermissionError):
				block_transaction_deletion(frappe.new_doc("Transaction Deletion Record"))
		self.assertFalse(can("Company", "delete", sysmgr))

	def test_archived_field_on_clinical_doctypes(self):
		"""NFR-9: inactive records are flagged archived instead of deleted."""
		for doctype in CLINICAL_DOCTYPES:
			if frappe.db.exists("DocType", doctype):
				self.assertTrue(frappe.get_meta(doctype).has_field(ARCHIVED_FIELD), doctype)


class TestRoleMatrix(EMRTestCase):
	def test_patient_registration_rights(self):
		"""FR-39 / SDD §6.1: only the Nurse Receptionist registers; clinical roles read."""
		self.assertTrue(can("Patient", "create", role_user(ROLE_RECEPTIONIST)))
		self.assertTrue(can("Patient", "write", role_user(ROLE_RECEPTIONIST)))
		for role in (ROLE_TRIAGE, ROLE_CLINICIAN, ROLE_MHO, ROLE_LAB, ROLE_PHARMACIST, ROLE_HIS, ROLE_MIC):
			user = role_user(role)
			self.assertTrue(can("Patient", "read", user), role)
			self.assertFalse(can("Patient", "create", user), role)
			self.assertFalse(can("Patient", "write", user), role)

	def test_vital_signs_rights(self):
		"""FR-9: the Triage Nurse records and submits vitals; clinicians and the MIC read them."""
		triage = role_user(ROLE_TRIAGE)
		for ptype in ("read", "write", "create", "submit"):
			self.assertTrue(can("Vital Signs", ptype, triage), ptype)
		for role in (ROLE_CLINICIAN, ROLE_MIC):
			self.assertTrue(can("Vital Signs", "read", role_user(role)), role)
			self.assertFalse(can("Vital Signs", "submit", role_user(role)), role)
		self.assertFalse(can("Vital Signs", "read", role_user(ROLE_RECEPTIONIST)))

	def test_triage_has_no_diagnosis_or_prescription(self):
		triage = role_user(ROLE_TRIAGE)
		self.assertFalse(can("Patient Encounter", "read", triage))
		self.assertFalse(can("Medication Request", "read", triage))

	def test_lab_and_pharmacy_scope(self):
		"""SDD §6.1: the lab sees no consultation notes or prescriptions; pharmacy reads requests."""
		lab = role_user(ROLE_LAB)
		self.assertFalse(can("Patient Encounter", "read", lab))
		self.assertFalse(can("Medication Request", "read", lab))
		for doctype in ("Observation", "Sample Collection", "Diagnostic Report"):
			self.assertTrue(can(doctype, "write", lab), doctype)
		pharmacist = role_user(ROLE_PHARMACIST)
		self.assertTrue(can("Medication Request", "read", pharmacist))
		self.assertFalse(can("Medication Request", "write", pharmacist))
		self.assertFalse(can("Patient Encounter", "read", pharmacist))

	def test_clinician_consults_and_orders(self):
		clinician = role_user(ROLE_CLINICIAN)
		for doctype in ("Patient Encounter", "Service Request", "Medication Request"):
			self.assertTrue(can(doctype, "create", clinician), doctype)
			self.assertTrue(can(doctype, "submit", clinician), doctype)

	def test_his_officer_reads_but_does_not_edit_clinical_notes(self):
		his = role_user(ROLE_HIS)
		self.assertTrue(can("Patient Encounter", "read", his))
		self.assertFalse(can("Patient Encounter", "write", his))

	def test_system_administrator_has_no_clinical_content(self):
		"""SDD §6.1: user and role management, configuration, no clinical content."""
		admin = role_user(ROLE_SYSADMIN)
		for doctype in CLINICAL_DOCTYPES:
			if frappe.db.exists("DocType", doctype):
				self.assertFalse(can(doctype, "read", admin), doctype)
		self.assertTrue(can("User", "create", admin))
		self.assertTrue(can("Medical Department", "write", admin))
		self.assertFalse(can("Healthcare Settings", "write", admin))
		# L4: NFR-5 controls (password policy, lockout, 2FA, session expiry) are System Manager only.
		self.assertTrue(can("System Settings", "read", admin))
		self.assertFalse(can("System Settings", "write", admin))

	def test_head_of_operations_has_no_clinical_content(self):
		head = role_user(ROLE_HEAD_OPS)
		for doctype in matrix.CLINICAL_MATRIX:
			self.assertFalse(can(doctype, "read", head), doctype)
		self.assertTrue(can("Medical Department", "read", head))

	def test_outreach_has_no_desk_access(self):
		"""SDD §6.1: the Outreach Team Member gets no desk access to other records."""
		self.assertEqual(frappe.db.get_value("Role", ROLE_OUTREACH, "desk_access"), 0)
		user = role_user(ROLE_OUTREACH)
		self.assertEqual(frappe.db.get_value("User", user, "user_type"), "Website User")
		self.assertFalse(can("Patient", "read", user))

	def test_reference_data_readable(self):
		for role in matrix.FACILITY_ROLES:
			user = role_user(role)
			for doctype in ("Medical Department", "Healthcare Service Unit", "Code Value", "Code System"):
				self.assertTrue(can(doctype, "read", user), f"{role} {doctype}")

	def test_marley_roles_kept(self):
		"""Marley's own roles keep their access (we copy standard perms, minus delete)."""
		physician = make_user("sec-physician", ["Physician"])
		self.assertTrue(can("Patient Encounter", "create", physician))


class TestUserGuard(EMRTestCase):
	def _new_user(self, roles):
		doc = frappe.new_doc("User")
		doc.update({"email": "t-sec-new@test.krcs", "first_name": "T-new"})
		for role in roles:
			doc.append("roles", {"role": role})
		return doc

	def test_sysadmin_grants_only_emr_roles(self):
		"""FR-39: the EMR System Administrator cannot hand out System Manager."""
		frappe.set_user(role_user(ROLE_SYSADMIN))
		user_validate(self._new_user([ROLE_RECEPTIONIST]))
		with self.assertRaises(frappe.PermissionError):
			user_validate(self._new_user(["System Manager"]))

	def test_sysadmin_cannot_change_own_roles(self):
		admin = role_user(ROLE_SYSADMIN)
		frappe.set_user(admin)
		doc = frappe.get_doc("User", admin)
		doc.append("roles", {"role": ROLE_CLINICIAN})
		with self.assertRaises(frappe.PermissionError):
			user_validate(doc)

	def test_sysadmin_cannot_grant_administrator(self):
		frappe.set_user(role_user(ROLE_SYSADMIN))
		with self.assertRaises(frappe.PermissionError):
			user_validate(self._new_user(["Administrator"]))

	def test_sysadmin_cannot_edit_a_system_manager(self):
		target = make_user("sec-target-sm", ["System Manager"])
		frappe.set_user(role_user(ROLE_SYSADMIN))
		doc = frappe.get_doc("User", target)
		doc.enabled = 0
		with self.assertRaises(frappe.PermissionError):
			user_validate(doc)

	def test_sysadmin_cannot_take_over_non_emr_accounts(self):
		"""M2: password, email, enabled and roles of an account holding a non-EMR role are protected."""
		target = make_user("sec-target-hca", ["Healthcare Administrator"])
		frappe.set_user(role_user(ROLE_SYSADMIN))
		for field, value in (
			("new_password", "Kq7!xRv-29Lm#tPz"),
			("email", "t-sec-hijack@test.krcs"),
			("enabled", 0),
		):
			doc = frappe.get_doc("User", target)
			doc.set(field, value)
			with self.assertRaises(frappe.PermissionError, msg=field):
				user_validate(doc)

	def test_sysadmin_resets_emr_user_password(self):
		"""SDD §6.1 (accepted risk): the System Administrator may reset an EMR clinical user's password."""
		target = role_user(ROLE_RECEPTIONIST)
		frappe.set_user(role_user(ROLE_SYSADMIN))
		doc = frappe.get_doc("User", target)
		doc.new_password = "Kq7!xRv-29Lm#tPz"
		user_validate(doc)

	def test_unrelated_edit_of_protected_account_passes(self):
		target = make_user("sec-target-sm2", ["System Manager"])
		frappe.set_user(role_user(ROLE_SYSADMIN))
		doc = frappe.get_doc("User", target)
		doc.reset_password_key = ""
		user_validate(doc)

	def test_guest_password_reset_of_system_manager_passes(self):
		"""M3: the Guest reset-link and expiry flows save the User without touching access."""
		target = make_user("sec-target-sm3", ["System Manager"])
		frappe.set_user("Guest")
		doc = frappe.get_doc("User", target)
		doc.new_password = "Kq7!xRv-29Lm#tPz"
		doc.save(ignore_permissions=True)

	def test_guest_cannot_change_roles(self):
		target = make_user("sec-target-sm4", ["System Manager"])
		frappe.set_user("Guest")
		doc = frappe.get_doc("User", target)
		doc.append("roles", {"role": "Accounts Manager"})
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)


class TestRoleProfileGuard(EMRTestCase):
	def _profile(self, name, roles):
		doc = frappe.new_doc("Role Profile")
		doc.role_profile = name
		for role in roles:
			doc.append("roles", {"role": role})
		return doc

	def test_sysadmin_builds_emr_profiles_only(self):
		"""M1: System Manager cannot be slipped into a role profile."""
		frappe.set_user(role_user(ROLE_SYSADMIN))
		role_profile_validate(self._profile("T-EMR Front Desk", [ROLE_RECEPTIONIST]))
		with self.assertRaises(frappe.PermissionError):
			role_profile_validate(self._profile("T-Escalate", [ROLE_RECEPTIONIST, "System Manager"]))

	def test_sysadmin_cannot_edit_a_profile_with_other_roles(self):
		profile = self._profile("T-Finance", ["Accounts User"]).insert(ignore_permissions=True)
		frappe.set_user(role_user(ROLE_SYSADMIN))
		doc = frappe.get_doc("Role Profile", profile.name)
		doc.set("roles", [{"role": ROLE_RECEPTIONIST}])
		with self.assertRaises(frappe.PermissionError):
			role_profile_validate(doc)


class TestSessionSecurity(EMRTestCase):
	def test_password_and_lockout_policy(self):
		"""NFR-5: password complexity and lockout after failed attempts."""
		settings = frappe.get_single("System Settings")
		self.assertEqual(settings.enable_password_policy, 1)
		self.assertGreaterEqual(int(settings.minimum_password_score), 3)
		self.assertEqual(settings.allow_consecutive_login_attempts, 5)
		self.assertGreater(settings.allow_login_after_fail, 0)

	def test_session_expiry(self):
		"""SDD §6.3: sessions end after a period of inactivity."""
		hours, _minutes = frappe.db.get_single_value("System Settings", "session_expiry").split(":")
		self.assertLessEqual(int(hours), 12)

	def test_two_factor_for_admin_and_his_only(self):
		"""SDD §6.3: 2FA for the System Administrator and HIS Officer, available to all."""
		self.assertEqual(frappe.db.get_single_value("System Settings", "enable_two_factor_auth"), 1)
		for role in ALL_ROLES:
			expected = 1 if role in (ROLE_SYSADMIN, ROLE_HIS) else 0
			self.assertEqual(frappe.db.get_value("Role", role, "two_factor_auth"), expected, role)
		self.assertEqual(frappe.db.get_value("Role", "All", "two_factor_auth"), 0)

	def test_no_self_signup(self):
		"""NFR-8: no anonymous or self-created accounts."""
		self.assertEqual(frappe.db.get_single_value("Website Settings", "disable_signup"), 1)
