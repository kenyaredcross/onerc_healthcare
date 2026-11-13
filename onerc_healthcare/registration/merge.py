# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Non-destructive patient merge (SDD 4.3, NFR-9).

Every Link field that points at Patient (standard DocField or Custom Field, read through
`frappe.get_meta`) is re-pointed from the source to the target, together with the fields fetched from
it (e.g. `patient_name`) and a copied `patient_identifier` (Patient Visit). Contact/Address Dynamic Links
follow. Every moved record (doctype, name, field) is written as JSON to the source's `krcs_merge_log`, so
the merge can be audited and, if ever needed, reversed record by record. Both patients are row-locked in
name order before the checks, so two concurrent merges of the same pair cannot both pass.

Identifiers move to the target unless the target already holds that identifier type; those stay on the
source. The source is never deleted
or renamed: it is set to Disabled with `krcs_merged_into` pointing at the target, and both records get
a timeline comment. Marley's `EmergencyRecord.merge_patient` and `frappe.rename_doc(merge=True)` are
deliberately not used: they remove the source record.
"""

import json
from collections import Counter

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.utils import escape_html, get_link_to_form, now_datetime

from onerc_healthcare.constants import (
	PATIENT_IDENTIFIER_DOCTYPE,
	PATIENT_IDENTIFIERS_FIELD,
	ROLE_HIS,
	ROLE_MIC,
)
from onerc_healthcare.registration.identifiers import (
	MERGE_LOG_FIELD,
	MERGED_INTO_FIELD,
	display_identifier,
	refresh_search_index,
)

MERGE_ROLES = (ROLE_MIC, ROLE_HIS, "System Manager")
DISABLED = "Disabled"


def can_merge(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(MERGE_ROLES) & set(frappe.get_roles(user)))


def get_patient_link_fields() -> list[tuple[str, str]]:
	"""(doctype, fieldname) of every Link field whose options is Patient, on real tables."""
	doctypes = set(
		frappe.get_all(
			"DocField",
			filters={"fieldtype": "Link", "options": "Patient", "parenttype": "DocType"},
			pluck="parent",
		)
	)
	doctypes |= set(
		frappe.get_all("Custom Field", filters={"fieldtype": "Link", "options": "Patient"}, pluck="dt")
	)
	fields = []
	for doctype in sorted(doctypes):
		if not frappe.db.exists("DocType", doctype):
			continue
		meta = frappe.get_meta(doctype)
		if meta.issingle or meta.is_virtual or not frappe.db.table_exists(doctype):
			continue
		for df in meta.get("fields", {"fieldtype": "Link", "options": "Patient"}):
			if not df.get("is_virtual"):
				fields.append((doctype, df.fieldname))
	return fields


def _fetched_fields(doctype: str, link_field: str) -> dict[str, str]:
	"""{fieldname: patient fieldname} of stored fields fetched through `link_field`."""
	patient_meta = frappe.get_meta("Patient")
	prefix = f"{link_field}."
	out = {}
	for df in frappe.get_meta(doctype).fields:
		if not df.fetch_from or not df.fetch_from.startswith(prefix):
			continue
		if df.fieldtype in no_value_fields or df.get("is_virtual"):
			continue
		source_field = df.fetch_from[len(prefix) :]
		source_df = patient_meta.get_field(source_field)
		if source_df and source_df.fieldtype not in no_value_fields and not source_df.get("is_virtual"):
			out[df.fieldname] = source_field
	return out


PATIENT_IDENTIFIER_FIELD_ON_LINKED = "patient_identifier"  # e.g. Patient Visit (A2), copied not fetched


def _relink(source: str, target: str) -> list[dict]:
	"""Re-point every record linked to `source`; return one log entry per record and field."""
	log: list[dict] = []
	target_values: dict = {}
	target_identifier = None
	for doctype, fieldname in get_patient_link_fields():
		meta = frappe.get_meta(doctype)
		table = frappe.qb.DocType(doctype)
		columns = [table.name]
		if meta.istable:
			columns += [table.parent, table.parenttype]
		rows = frappe.qb.from_(table).select(*columns).where(table[fieldname] == source).run(as_dict=True)
		if not rows:
			continue
		names = [r.name for r in rows]

		updates = {fieldname: target}
		fetched = _fetched_fields(doctype, fieldname)
		missing = [f for f in fetched.values() if f not in target_values]
		if missing:
			target_values.update(frappe.db.get_value("Patient", target, missing, as_dict=True) or {})
		for field, source_field in fetched.items():
			updates[field] = target_values.get(source_field)
		df = meta.get_field(PATIENT_IDENTIFIER_FIELD_ON_LINKED)
		if fieldname == "patient" and df and not df.fetch_from:
			if target_identifier is None:
				target_identifier = display_identifier(target) or ""
			updates[PATIENT_IDENTIFIER_FIELD_ON_LINKED] = target_identifier or None

		query = frappe.qb.update(table).where(table.name.isin(names))
		for field, value in updates.items():
			query = query.set(table[field], value)
		query.run()
		frappe.clear_document_cache(doctype)
		for r in rows:
			entry = {"doctype": doctype, "name": r.name, "field": fieldname}
			if meta.istable:
				entry.update(parent=r.parent, parenttype=r.parenttype)
			log.append(entry)

	link = frappe.qb.DocType("Dynamic Link")
	rows = (
		frappe.qb.from_(link)
		.select(link.name, link.parent, link.parenttype)
		.where((link.link_doctype == "Patient") & (link.link_name == source))
		.run(as_dict=True)
	)
	if rows:
		frappe.qb.update(link).set(link.link_name, target).where(link.name.isin([r.name for r in rows])).run()
		log.extend(
			{
				"doctype": "Dynamic Link",
				"name": r.name,
				"field": "link_name",
				"parent": r.parent,
				"parenttype": r.parenttype,
			}
			for r in rows
		)
	return log


def _move_identifiers(source_doc, target_doc) -> tuple[list[dict], list[dict]]:
	target_rows = target_doc.get(PATIENT_IDENTIFIERS_FIELD) or []
	target_types = {r.identifier_type for r in target_rows}
	next_idx = max((r.idx for r in target_rows), default=0) + 1
	moved, kept = [], []
	for row in source_doc.get(PATIENT_IDENTIFIERS_FIELD) or []:
		entry = {
			"name": row.name,
			"identifier_type": row.identifier_type,
			"identifier_value": row.identifier_value,
		}
		if row.identifier_type in target_types:
			kept.append(entry)
			continue
		frappe.db.set_value(
			PATIENT_IDENTIFIER_DOCTYPE,
			row.name,
			{"parent": target_doc.name, "idx": next_idx},
			update_modified=False,
		)
		next_idx += 1
		target_types.add(row.identifier_type)
		moved.append(entry)
	return moved, kept


def _label(entry: dict) -> str:
	return f"{entry['identifier_type']}: {entry['identifier_value']}"


def _summary(relinked: list[dict], moved: list[dict], kept: list[dict], reason: str) -> str:
	lines = [_("Reason: {0}").format(escape_html(reason))]
	counts = Counter(f"{e['doctype']}.{e['field']}" for e in relinked)
	if counts:
		lines.append(
			_("Records relinked: {0}").format(", ".join(f"{k} ({v})" for k, v in sorted(counts.items())))
		)
	else:
		lines.append(_("Records relinked: none"))
	if moved:
		lines.append(_("Identifiers moved: {0}").format(escape_html("; ".join(map(_label, moved)))))
	if kept:
		lines.append(
			_(
				"Identifiers left on the merged record (the surviving record already has that type): {0}"
			).format(escape_html("; ".join(map(_label, kept))))
		)
	lines.append(_("The full list of moved records is kept in the merged record's Merge Log."))
	return "<br>".join(lines)


def _lock_patients(*names: str) -> None:
	"""Row-lock both patients in a fixed order so concurrent merges (A->B, B->A) serialise (M5)."""
	for name in sorted(set(names)):
		frappe.db.sql("select name from `tabPatient` where name = %s for update", (name,))


def _refuse_open_visit(source: str) -> None:
	"""Phase 2 audit A2 #12: relinking moves visits by direct update, which would bypass the
	one-open-visit rule and leave the surviving patient with two open visits. Close it first."""
	from onerc_healthcare.constants import VISIT_DOCTYPE, VISIT_OPEN_STATUSES

	if not frappe.db.exists("DocType", VISIT_DOCTYPE):
		return
	open_visit = frappe.db.get_value(
		VISIT_DOCTYPE, {"patient": source, "status": ("in", VISIT_OPEN_STATUSES)}, "name"
	)
	if open_visit:
		frappe.throw(
			_("Patient {0} has an open visit {1}. Close or finish that visit before merging.").format(
				source, get_link_to_form(VISIT_DOCTYPE, open_visit)
			),
			title=_("Invalid Merge"),
		)


def merge_patients(source: str, target: str, reason: str) -> dict:
	"""Merge `source` into `target` without deleting anything. Caller checks roles."""
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter the reason for merging these records."), title=_("Missing Value"))
	if not source or not target or source == target:
		frappe.throw(_("Choose two different patients to merge."), title=_("Invalid Merge"))
	for name in (source, target):
		if not frappe.db.exists("Patient", name):
			frappe.throw(_("Patient {0} does not exist.").format(name), frappe.DoesNotExistError)

	_lock_patients(source, target)
	source_doc = frappe.get_doc("Patient", source)
	target_doc = frappe.get_doc("Patient", target)
	if source_doc.get(MERGED_INTO_FIELD):
		frappe.throw(
			_("Patient {0} was already merged into {1}.").format(
				source, get_link_to_form("Patient", source_doc.get(MERGED_INTO_FIELD))
			),
			title=_("Invalid Merge"),
		)
	if target_doc.get(MERGED_INTO_FIELD):
		frappe.throw(
			_("Patient {0} was itself merged into {1}. Merge into {1} instead.").format(
				target, get_link_to_form("Patient", target_doc.get(MERGED_INTO_FIELD))
			),
			title=_("Invalid Merge"),
		)
	if target_doc.status == DISABLED:
		frappe.throw(
			_("Patient {0} is disabled. Enable it before merging another record into it.").format(target),
			title=_("Invalid Merge"),
		)

	_refuse_open_visit(source)

	# Identifiers first, so records that copy the patient's identifier get the surviving one.
	moved, kept = _move_identifiers(source_doc, target_doc)
	relinked = _relink(source, target)
	merge_log = {
		"merged_into": target,
		"reason": reason,
		"merged_by": frappe.session.user,
		"merged_on": str(now_datetime()),
		"relinked": relinked,
		"identifiers_moved": moved,
		"identifiers_kept": kept,
	}

	source_doc = frappe.get_doc("Patient", source)
	source_doc.status = DISABLED
	source_doc.set(MERGED_INTO_FIELD, target)
	source_doc.set(MERGE_LOG_FIELD, json.dumps(merge_log, indent=1, default=str))
	source_doc.flags.krcs_merging = True
	source_doc.save(ignore_permissions=True)
	refresh_search_index(target)

	summary = _summary(relinked, moved, kept, reason)
	source_doc.add_comment(
		"Info",
		_("Merged into {0} by {1}. The record is kept, disabled.").format(
			get_link_to_form("Patient", target), frappe.session.user
		)
		+ "<br>"
		+ summary,
	)
	frappe.get_doc("Patient", target).add_comment(
		"Info",
		_("Patient {0} was merged into this record by {1}.").format(
			get_link_to_form("Patient", source), frappe.session.user
		)
		+ "<br>"
		+ summary,
	)
	return {
		"source": source,
		"target": target,
		"relinked": dict(Counter(f"{e['doctype']}.{e['field']}" for e in relinked)),
		"relinked_records": relinked,
		"identifiers_moved": [_label(e) for e in moved],
		"identifiers_kept": [_label(e) for e in kept],
	}
