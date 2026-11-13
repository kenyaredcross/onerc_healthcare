# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""A3 roles, permissions, billing lock-down, delete guard and session security.

Called by setup/install.py on after_install and after_migrate. Idempotent, never commits.

- Every run: Custom DocPerms from security.matrix (missing rows only), delete stripped from the clinical
  DocTypes only (NFR-9), the bulk-deletion routes closed (Transaction Deletion Record, Company delete),
  billing rights removed from EMR roles and selling price lists disabled (C-5), billing workspaces
  restricted, ``krcs_archived`` added to clinical DocTypes, billing fields hidden from forms (BIL-04).
- First run only (so admin edits survive): role settings, System Settings, Website Settings.
- One-off fix-ups (marker ``krcs_security_fixups_v2``): undo the over-reach of the first version.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import cint

from onerc_healthcare.constants import (
	ACCESS_LOG_DOCTYPE,
	ALL_ROLES,
	APP_MODULES,
	CLINICAL_DOCTYPES,
	ROLE_SYSADMIN,
	VISIT_DOCTYPE,
)
from onerc_healthcare.security import matrix
from onerc_healthcare.security.delete_guard import ARCHIVED_FIELD

ARCHIVE_SECTION_FIELD = "krcs_archive_section"
from onerc_healthcare.security.permissions import (
	apply_matrix,
	clear_perm_cache,
	doctype_exists,
	ensure_custom_perms,
	remove_role_perms,
	restore_standard_delete,
	set_perm_values,
	strip_delete,
)

ROLE_SETTINGS_MARKER = "krcs_security_role_settings_v1"
SYSTEM_SETTINGS_MARKER = "krcs_security_system_settings_v1"
FIXUPS_MARKER = "krcs_security_fixups_v2"


def install() -> None:
	apply_role_settings()
	apply_permissions()
	close_bulk_deletion()
	apply_fixups()
	lock_down_billing()
	hide_billing_fields()
	add_archived_fields()
	apply_session_security()
	apply_log_retention()


# ---------------------------------------------------------------------------
# Roles (SDD §6.1)
# ---------------------------------------------------------------------------
def apply_role_settings() -> None:
	"""Desk access, home page and 2FA flag per role. First run only."""
	if frappe.db.get_default(ROLE_SETTINGS_MARKER):
		return
	for role, values in matrix.ROLE_SETTINGS.items():
		if not frappe.db.exists("Role", role):
			continue
		doc = frappe.get_doc("Role", role)
		doc.update(values)
		doc.save(ignore_permissions=True)
	frappe.db.set_default(ROLE_SETTINGS_MARKER, "1")


# ---------------------------------------------------------------------------
# Permissions (FR-39) and delete lock-down (NFR-9)
# ---------------------------------------------------------------------------
def clinical_doctypes_with_custom_perms() -> list[str]:
	"""Marley/ERPNext clinical DocTypes whose perms we manage through Custom DocPerm.

	Our own DocTypes (any module in APP_MODULES, e.g. Patient Visit, Referral) are skipped: their perms
	live in their JSON, and copying them into Custom DocPerm would freeze later JSON changes.
	"""
	return [dt for dt in CLINICAL_DOCTYPES if doctype_exists(dt) and not is_app_doctype(dt)]


def is_app_doctype(doctype: str) -> bool:
	return frappe.db.get_value("DocType", doctype, "module") in APP_MODULES


def apply_permissions() -> None:
	touched = []
	touched += apply_matrix(matrix.CLINICAL_MATRIX)
	touched += apply_matrix(matrix.MASTER_MATRIX)
	touched += apply_matrix(matrix.ADMIN_MATRIX)

	# Clinical DocTypes outside the Phase 1 matrix still get Custom DocPerms, so delete can be removed
	# from System Manager and the Marley roles too (SDD §4.6).
	clinical = clinical_doctypes_with_custom_perms()
	for doctype in clinical:
		ensure_custom_perms(doctype)
	touched += clinical

	# Delete is removed from clinical DocTypes only (SDD §4.6). Admin and master DocTypes keep the
	# standard delete rights of System Manager / Healthcare Administrator; EMR roles never get delete.
	# Patient Visit keeps its JSON perms; only clear delete if someone added Custom DocPerms.
	strip_delete(list(CLINICAL_DOCTYPES))
	clear_perm_cache([*touched, VISIT_DOCTYPE])


def close_bulk_deletion() -> None:
	"""NFR-9: ERPNext's Transaction Deletion Record bulk-deletes company-linked rows with
	``frappe.db.delete``, bypassing on_trash. No role may create or submit one; Company delete (the
	permission ``create_transaction_deletion_request`` checks) is removed too. Enforced every run.
	The record itself is inserted with ignore_permissions by ERPNext, so the real stop is the
	``delete_guard.block_transaction_deletion`` hook plus ``company_data_to_be_ignored``.
	"""
	changed = []
	for doctype, values in matrix.BULK_DELETION_PERMS.items():
		if doctype_exists(doctype) and set_perm_values(doctype, values):
			changed.append(doctype)
	clear_perm_cache(changed)


def apply_fixups() -> None:
	"""One-off corrections of the first version, which over-reached. Marker-guarded so admin edits
	made afterwards survive."""
	if frappe.db.get_default(FIXUPS_MARKER):
		return
	# M7: restore the standard delete rights on the admin and master DocTypes.
	doctypes = [
		dt
		for dt in [*matrix.MASTER_MATRIX, *matrix.ADMIN_MATRIX]
		if dt not in CLINICAL_DOCTYPES and doctype_exists(dt)
	]
	restore_standard_delete(doctypes, exclude_roles=ALL_ROLES)
	# L4: the EMR System Administrator reads System Settings but cannot switch off NFR-5 controls.
	if doctype_exists("System Settings"):
		set_perm_values("System Settings", {"write": 0, "create": 0}, roles=[ROLE_SYSADMIN])
	# L5: only selling price lists are disabled (C-5); re-enable buying-only lists we disabled.
	for name in frappe.get_all("Price List", filters={"enabled": 0, "buying": 1, "selling": 0}, pluck="name"):
		frappe.db.set_value("Price List", name, "enabled", 1)
	clear_perm_cache([*doctypes, "System Settings"])
	frappe.db.set_default(FIXUPS_MARKER, "1")


def add_archived_fields() -> None:
	"""`krcs_archived` in its own "Archive" section at the end of each clinical DocType that exists.

	The section is re-anchored after the form's current last field on every run, so it stays at the
	bottom when Marley or other workstreams add fields later.
	"""
	fields = {}
	for dt in CLINICAL_DOCTYPES:
		# The Access Log Entry is append-only (no updates at all), so an archive flag means nothing there.
		if dt == ACCESS_LOG_DOCTYPE or not doctype_exists(dt):
			continue
		last = _last_field(dt)
		if not last:
			continue
		fields[dt] = [
			{
				"fieldname": ARCHIVE_SECTION_FIELD,
				"label": "Archive",
				"fieldtype": "Section Break",
				"insert_after": last,
			},
			{
				"fieldname": ARCHIVED_FIELD,
				"label": "Archived",
				"fieldtype": "Check",
				"default": "0",
				"allow_on_submit": 1,
				"no_copy": 1,
				"in_standard_filter": 1,
				"insert_after": ARCHIVE_SECTION_FIELD,
				"description": "Inactive record kept for retrieval; clinical records are never deleted (NFR-9).",
			},
		]
	create_custom_fields(fields, ignore_validate=True)


def _last_field(doctype: str) -> str | None:
	"""The form's last field, ignoring our own archive fields."""
	ours = (ARCHIVE_SECTION_FIELD, ARCHIVED_FIELD)
	names = [df.fieldname for df in frappe.get_meta(doctype).fields if df.fieldname not in ours]
	return names[-1] if names else None


# ---------------------------------------------------------------------------
# Billing lock-down (C-5): enforced on every run; it is a hard constraint, not a preference.
# ---------------------------------------------------------------------------
def lock_down_billing() -> None:
	for doctype in matrix.BILLING_DOCTYPES:
		if doctype_exists(doctype) and remove_role_perms(doctype, ALL_ROLES):
			clear_perm_cache([doctype])
	# Selling price lists only: C-5 is about charging patients; buying lists may serve pharmacy procurement.
	for name in frappe.get_all("Price List", filters={"enabled": 1, "selling": 1}, pluck="name"):
		frappe.db.set_value("Price List", name, "enabled", 0)
	restrict_workspaces()


def restrict_workspaces() -> None:
	"""Give the billing/stock workspaces a roles table so EMR-only users never see them.

	Rows are inserted directly instead of saving the Workspace: in developer mode a save would export
	the change into the ERPNext / Marley JSON files. A workspace that already has roles is left alone.
	"""
	added = 0
	for workspace, roles in matrix.RESTRICTED_WORKSPACES.items():
		if frappe.db.exists("Workspace", workspace) and not _workspace_roles(workspace):
			added += _add_workspace_roles(workspace, roles)
	# The EMR System Administrator manages users, so it needs the (System Manager only) Users workspace.
	for workspace, roles in matrix.ADMIN_WORKSPACES.items():
		if frappe.db.exists("Workspace", workspace):
			added += _add_workspace_roles(workspace, roles)
	if added:
		from frappe.cache_manager import clear_user_cache

		clear_user_cache()


def _workspace_roles(workspace: str) -> list[str]:
	return frappe.get_all("Has Role", filters={"parenttype": "Workspace", "parent": workspace}, pluck="role")


def _add_workspace_roles(workspace: str, roles) -> int:
	"""Insert missing Has Role rows directly (no Workspace save, see restrict_workspaces)."""
	existing = _workspace_roles(workspace)
	idx = len(existing)
	for role in roles:
		if role in existing or not frappe.db.exists("Role", role):
			continue
		idx += 1
		row = frappe.new_doc("Has Role")
		row.update(
			{
				"name": frappe.generate_hash(length=10),
				"parent": workspace,
				"parenttype": "Workspace",
				"parentfield": "roles",
				"role": role,
				"idx": idx,
			}
		)
		row.db_insert()
	return idx - len(existing)


# ---------------------------------------------------------------------------
# Authentication and sessions (NFR-5, NFR-8, SDD §6.3). First run only.
# ---------------------------------------------------------------------------
def apply_session_security() -> None:
	if frappe.db.get_default(SYSTEM_SETTINGS_MARKER):
		return
	settings = frappe.get_single("System Settings")
	settings.update(matrix.SYSTEM_SETTINGS)
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)

	# Enabling 2FA makes Frappe switch it on for the "All" role, i.e. everyone. SDD §6.3 asks for it on
	# the System Administrator and HIS Officer only, and available to all, so keep it per role.
	frappe.db.set_value("Role", "All", "two_factor_auth", 0)
	for role, values in matrix.ROLE_SETTINGS.items():
		if frappe.db.exists("Role", role):
			frappe.db.set_value("Role", role, "two_factor_auth", values.get("two_factor_auth", 0))

	for field, value in matrix.WEBSITE_SETTINGS.items():
		frappe.db.set_single_value("Website Settings", field, value)
	frappe.db.set_default(SYSTEM_SETTINGS_MARKER, "1")


# ---------------------------------------------------------------------------
# Billing fields hidden from forms (C-5, BIL-04). Enforced on every run; UI only.
# ---------------------------------------------------------------------------
_BREAKS = ("Section Break", "Tab Break")
_LAYOUT_ONLY = ("Column Break", *_BREAKS)


def billing_fields_to_hide(doctype: str) -> tuple[set[str], list[str]]:
	"""Fields of matrix.BILLING_FIELDS[doctype] that can be hidden safely, and those refused.

	A field is refused when it is mandatory without a default, or mandatory on a condition that does
	not depend on another field we hide (e.g. ``is_billable``, which stays 0 once hidden).
	"""
	meta = frappe.get_meta(doctype)
	wanted = [fn for fn in matrix.BILLING_FIELDS.get(doctype, ()) if meta.get_field(fn)]
	hide, refused = set(), []
	for fieldname in wanted:
		df = meta.get_field(fieldname)
		condition = df.mandatory_depends_on or ""
		if (df.reqd and not df.default) or (
			condition and not any(other in condition for other in wanted if other != fieldname)
		):
			refused.append(f"{doctype}.{fieldname}")
		else:
			hide.add(fieldname)
	return hide, refused


def empty_containers(doctype: str, hiding: set[str]) -> set[str]:
	"""Section and Tab Breaks whose every content field is hidden once `hiding` is applied, and that
	contain at least one of `hiding` (other empty sections are not ours to touch)."""
	containers: dict[str, list] = {}
	section = tab = None
	for df in frappe.get_meta(doctype).fields:
		if df.fieldtype == "Tab Break":
			tab, section = df.fieldname, None
			containers[tab] = []
			continue
		if df.fieldtype == "Section Break":
			section = df.fieldname
			containers[section] = []
			continue
		if df.fieldtype in _LAYOUT_ONLY:
			continue
		for owner in (section, tab):
			if owner:
				containers[owner].append(df)
	return {
		name
		for name, fields in containers.items()
		if fields
		and any(df.fieldname in hiding for df in fields)
		and all(df.hidden or df.fieldname in hiding for df in fields)
	}


def hide_billing_fields() -> list[str]:
	"""Hide the BILLING_FIELDS (and sections/tabs they empty). Returns the refused fields."""
	refused_all = []
	for doctype in matrix.BILLING_FIELDS:
		if not doctype_exists(doctype):
			continue
		hide, refused = billing_fields_to_hide(doctype)
		refused_all += refused
		if not hide:
			continue
		meta = frappe.get_meta(doctype)
		changed = False
		for fieldname in hide | empty_containers(doctype, hide):
			df = meta.get_field(fieldname)
			changed |= _set_field_property(doctype, df, "hidden", 1)
			if df.fieldtype not in _BREAKS:
				changed |= _set_field_property(doctype, df, "print_hide", 1)
				if df.in_list_view:
					changed |= _set_field_property(doctype, df, "in_list_view", 0)
		if changed:
			frappe.clear_cache(doctype=doctype)
	return refused_all


def _set_field_property(doctype: str, df, prop: str, value: int) -> bool:
	if cint(df.get(prop)) == value:
		return False
	frappe.make_property_setter(
		{
			"doctype": doctype,
			"fieldname": df.fieldname,
			"property": prop,
			"value": value,
			"property_type": "Check",
		},
		validate_fields_for_doctype=False,
	)
	return True


# ---------------------------------------------------------------------------
# Log retention (NFR-7): logins (Activity Log) and prints/exports (Access Log) kept 10 years
# instead of Frappe's default auto-clear. First run only, so an admin's later choice survives.
# ---------------------------------------------------------------------------
LOG_RETENTION_MARKER = "krcs_log_retention_v1"
LOG_RETENTION_DAYS = 3650
RETAINED_LOGS = ("Activity Log", "Access Log")


def apply_log_retention() -> None:
	if frappe.db.get_default(LOG_RETENTION_MARKER) or not frappe.db.exists("DocType", "Log Settings"):
		return
	settings = frappe.get_single("Log Settings")
	rows = {row.ref_doctype: row for row in settings.get("logs_to_clear") or []}
	for doctype in RETAINED_LOGS:
		if doctype in rows:
			rows[doctype].days = LOG_RETENTION_DAYS
		else:
			settings.append("logs_to_clear", {"ref_doctype": doctype, "days": LOG_RETENTION_DAYS})
	settings.flags.ignore_permissions = True
	settings.flags.ignore_validate = True
	settings.save()
	frappe.db.set_default(LOG_RETENTION_MARKER, "1")
