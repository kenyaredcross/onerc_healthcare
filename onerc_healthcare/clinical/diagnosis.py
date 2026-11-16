# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""ICD-11 diagnoses on the consultation (FR-11, BR-4, SDD §5.3).

Rules (decision 2026-11-13):

- Every row is an ICD-11 MMS Code Value outside chapter X (extension codes cannot stand alone).
- No code twice on one encounter; exactly one primary diagnosis when there are rows (a single row is
  made primary automatically).
- A parent (non-leaf) code is accepted with a warning: a more specific child code exists.
- Completing a consultation needs at least one diagnosis or a reason for having none
  (`krcs_no_diagnosis_reason`), checked by `assert_completable`.

Marley's `codification_table` is rebuilt from these rows on every validate, so Marley's reports and
print formats keep working. `search_icd11` is the ranked link query used by the picker.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from onerc_healthcare.coding.setup import ICD11_CODE_SYSTEM

DIAGNOSES_FIELD = "krcs_diagnoses"
NO_DIAGNOSIS_REASON_FIELD = "krcs_no_diagnosis_reason"
EXCLUDED_CHAPTER = "X"


class DiagnosisError(frappe.ValidationError):
	"""An encounter diagnosis breaks the ICD-11 rules (FR-11)."""


def validate_diagnoses(doc) -> None:
	"""Check the rows, refresh code/title from the Code Value, warn on parent codes."""
	rows = doc.get(DIAGNOSES_FIELD) or []
	if not rows:
		return
	codes = [row.code for row in rows if row.code]
	values = {
		v.name: v
		for v in frappe.get_all(
			"Code Value",
			filters={"name": ["in", codes or [""]]},
			fields=["name", "code_system", "code_value", "display", "krcs_chapter", "krcs_is_leaf"],
		)
	}
	seen = set()
	parents = []
	for row in rows:
		label = _("Diagnosis row {0}").format(row.idx)
		value = values.get(row.code)
		if not row.code or not value:
			frappe.throw(_("{0}: select an ICD-11 code.").format(label), DiagnosisError)
		if value.code_system != ICD11_CODE_SYSTEM:
			frappe.throw(
				_("{0}: {1} is not an ICD-11 code.").format(label, frappe.bold(row.code)), DiagnosisError
			)
		if (value.krcs_chapter or "") == EXCLUDED_CHAPTER:
			frappe.throw(
				_("{0}: {1} is an ICD-11 extension code (chapter X) and cannot be a diagnosis.").format(
					label, frappe.bold(value.code_value)
				),
				DiagnosisError,
			)
		if row.code in seen:
			frappe.throw(
				_("{0}: {1} is already listed.").format(label, frappe.bold(value.code_value)), DiagnosisError
			)
		seen.add(row.code)
		row.code_value = value.code_value
		row.title = value.display
		if not cint(value.krcs_is_leaf):
			parents.append(value.code_value)

	if len(rows) == 1 and not cint(rows[0].is_primary):
		rows[0].is_primary = 1
	primaries = sum(1 for row in rows if cint(row.is_primary))
	if primaries != 1:
		frappe.throw(
			_("Mark exactly one diagnosis as primary ({0} marked).").format(primaries),
			DiagnosisError,
			title=_("Primary Diagnosis"),
		)
	if parents:
		frappe.msgprint(
			_("{0}: a more specific code exists. Use the most specific code that fits.").format(
				", ".join(frappe.bold(c) for c in parents)
			),
			title=_("Parent Code"),
			indicator="orange",
		)


def rebuild_codification(doc) -> None:
	"""Marley's `codification_table` mirrors the diagnosis rows."""
	rows = doc.get(DIAGNOSES_FIELD) or []
	if not rows and not doc.get("codification_table"):
		return
	system_uri = frappe.db.get_value("Code System", ICD11_CODE_SYSTEM, "uri")
	doc.set("codification_table", [])
	for row in rows:
		doc.append(
			"codification_table",
			{
				"code_system": ICD11_CODE_SYSTEM,
				"system": system_uri,
				"code_value": row.code,
				"code": row.code_value,
				"display": row.title,
			},
		)


def assert_completable(doc) -> None:
	"""FR-11: at least one diagnosis, or a reason for completing without one."""
	if doc.get(DIAGNOSES_FIELD):
		return
	if (doc.get(NO_DIAGNOSIS_REASON_FIELD) or "").strip():
		return
	frappe.throw(
		_(
			"Add at least one ICD-11 diagnosis, or give the reason there is none, to complete the consultation."
		),
		DiagnosisError,
		title=_("Diagnosis Required"),
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def search_icd11(
	doctype: str,
	txt: str,
	searchfield: str,
	start: int,
	page_len: int,
	filters: dict | str | None = None,
):
	"""Ranked ICD-11 picker: exact code, code prefix, title prefix, then contains; leaf codes first.

	Chapter X (extension codes) is excluded. Returns (name, title, hint) tuples for the link field.
	"""
	frappe.has_permission("Code Value", "read", throw=True)
	txt = (txt or "").strip()
	like = f"%{txt}%"
	prefix = f"{txt}%"
	rows = frappe.db.sql(
		"""
		select name, display, krcs_is_leaf
		from `tabCode Value`
		where code_system = %(system)s
			and ifnull(krcs_chapter, '') != %(excluded)s
			and (name = %(txt)s or code_value like %(like)s or display like %(like)s)
		order by
			case
				when name = %(txt)s or code_value = %(txt)s then 0
				when code_value like %(prefix)s then 1
				when display like %(prefix)s then 2
				else 3
			end,
			krcs_is_leaf desc,
			length(code_value),
			code_value
		limit %(start)s, %(page_len)s
		""",
		{
			"system": ICD11_CODE_SYSTEM,
			"excluded": EXCLUDED_CHAPTER,
			"like": like,
			"prefix": prefix,
			"txt": txt,
			"start": cint(start),
			"page_len": cint(page_len) or 20,
		},
	)
	parent_hint = _("has more specific codes")
	return tuple((name, display or "", "" if cint(leaf) else parent_hint) for name, display, leaf in rows)
