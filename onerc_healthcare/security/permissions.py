# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Custom DocPerm helpers (FR-39). Idempotent; never commit.

Once a DocType has any Custom DocPerm, Frappe ignores its standard DocPerms. So the first time we
customise a DocType we copy its standard rows into Custom DocPerm (without delete) before adding ours.
"""

import frappe

PTYPE_CODES = {
	"r": ("read", "report", "print"),
	"w": ("write",),
	"c": ("create",),
	"s": ("submit",),
	"e": ("export",),
	"x": ("cancel",),
}
# DocPerm columns copied from the standard rows.
PERM_COLUMNS = (
	"role",
	"permlevel",
	"if_owner",
	"select",
	"read",
	"write",
	"create",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"import",
	"share",
	"print",
	"email",
)


def doctype_exists(doctype: str) -> bool:
	return bool(frappe.db.exists("DocType", doctype))


def has_custom_perms(doctype: str) -> bool:
	return bool(frappe.db.exists("Custom DocPerm", {"parent": doctype}))


def ensure_custom_perms(doctype: str) -> bool:
	"""Copy the standard DocPerms (minus delete) into Custom DocPerm if none exist. True if copied."""
	if has_custom_perms(doctype):
		return False
	for row in frappe.get_all("DocPerm", filters={"parent": doctype}, fields=["*"], order_by="idx"):
		_insert_custom_perm(doctype, {**{k: row.get(k) for k in PERM_COLUMNS}, "delete": 0})
	return True


def codes_to_ptypes(codes: str, submittable: bool) -> dict[str, int]:
	values: dict[str, int] = {}
	for code in codes:
		if code == "s" and not submittable:
			continue
		for ptype in PTYPE_CODES[code]:
			values[ptype] = 1
	return values


def ensure_role_perm(doctype: str, role: str, codes: str, permlevel: int = 0) -> bool:
	"""Add a Custom DocPerm row for `role` unless one already exists (an admin edit survives)."""
	ensure_custom_perms(doctype)
	if frappe.db.exists(
		"Custom DocPerm", {"parent": doctype, "role": role, "permlevel": permlevel, "if_owner": 0}
	):
		return False
	submittable = bool(frappe.get_meta(doctype).is_submittable)
	values = codes_to_ptypes(codes, submittable) if permlevel == 0 else _level_ptypes(codes)
	_insert_custom_perm(doctype, {"role": role, "permlevel": permlevel, "delete": 0, **values})
	return True


def _level_ptypes(codes: str) -> dict[str, int]:
	# Higher permlevels only know read and write.
	return {"read": 1, **({"write": 1} if "w" in codes else {})}


def apply_matrix(matrix: dict) -> list[str]:
	"""Apply {doctype: {role: codes}} or {doctype: {permlevel: {role: codes}}}. Returns touched doctypes."""
	touched = []
	for doctype, spec in matrix.items():
		if not doctype_exists(doctype):
			continue
		levels = spec if all(isinstance(k, int) for k in spec) else {0: spec}
		for permlevel, roles in levels.items():
			for role, codes in roles.items():
				if frappe.db.exists("Role", role):
					ensure_role_perm(doctype, role, codes, permlevel)
		touched.append(doctype)
	return touched


def strip_delete(doctypes) -> int:
	"""Clear delete on every Custom DocPerm of `doctypes`. Returns the number of rows changed."""
	names = frappe.get_all(
		"Custom DocPerm", filters={"parent": ("in", list(doctypes)), "delete": 1}, pluck="name"
	)
	for name in names:
		frappe.db.set_value("Custom DocPerm", name, "delete", 0, update_modified=False)
	return len(names)


def remove_role_perms(doctype: str, roles) -> int:
	"""Remove any Custom DocPerm rows of `roles` on `doctype`. Returns the number removed."""
	names = frappe.get_all(
		"Custom DocPerm", filters={"parent": doctype, "role": ("in", list(roles))}, pluck="name"
	)
	for name in names:
		frappe.delete_doc("Custom DocPerm", name, ignore_permissions=True, force=True)
	return len(names)


def clear_perm_cache(doctypes) -> None:
	for doctype in set(doctypes):
		frappe.clear_cache(doctype=doctype)


def _insert_custom_perm(doctype: str, values: dict) -> None:
	doc = frappe.new_doc("Custom DocPerm")
	doc.update(values)
	doc.update({"parent": doctype, "parenttype": "DocType", "parentfield": "permissions"})
	doc.insert(ignore_permissions=True)


def restore_standard_delete(doctypes, exclude_roles=()) -> int:
	"""Put delete back on copied Custom DocPerm rows whose standard DocPerm grants it.

	Earlier versions copied the standard perms of every customised DocType without delete; only clinical
	DocTypes should lose it (NFR-9). Roles in `exclude_roles` (the EMR roles) never get delete.
	"""
	restored = 0
	for doctype in doctypes:
		standard = frappe.get_all(
			"DocPerm",
			filters={"parent": doctype, "delete": 1},
			fields=["role", "permlevel", "if_owner"],
		)
		for perm in standard:
			if perm.role in exclude_roles:
				continue
			names = frappe.get_all(
				"Custom DocPerm",
				filters={
					"parent": doctype,
					"role": perm.role,
					"permlevel": perm.permlevel,
					"if_owner": perm.if_owner,
					"delete": 0,
				},
				pluck="name",
			)
			for name in names:
				frappe.db.set_value("Custom DocPerm", name, "delete", 1, update_modified=False)
				restored += 1
	return restored


def set_perm_values(doctype: str, values: dict[str, int], roles=None) -> int:
	"""Set `values` (e.g. {"create": 0}) on the Custom DocPerm rows of `doctype` (all roles, or `roles`).

	Copies the standard perms first if the DocType has no Custom DocPerm. Returns rows changed.
	"""
	ensure_custom_perms(doctype)
	filters = {"parent": doctype}
	if roles is not None:
		filters["role"] = ("in", list(roles))
	changed = 0
	for row in frappe.get_all("Custom DocPerm", filters=filters, fields=["name", *values]):
		diff = {k: v for k, v in values.items() if (row.get(k) or 0) != v}
		if diff:
			frappe.db.set_value("Custom DocPerm", row.name, diff, update_modified=False)
			changed += 1
	return changed
