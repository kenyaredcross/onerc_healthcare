# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH Report definitions seeded from ``reporting/data/*.json`` (shared format, see the Phase 3 brief).

`sync_definitions()` runs on install and migrate (`reporting.setup.install`):

- A definition that does not exist is inserted, and the hash of the saved record is stored in ``seed_hash``.
- An existing definition is updated to a newer seed **only while it is unedited**: the hash of what is
  in the database still equals the stored ``seed_hash``. Once the HIS Officer edits a definition in the
  desk (labels, rules, status "Confirmed", a new element...), seeds never touch it again.
- A definition created by hand (no ``seed_hash``) is never touched.

Which files are definitions: a JSON object with a ``"name"`` and an ``"elements"`` key is one definition;
a list, or an object with a ``"definitions"`` list, holds several. Other files in the folder (R2's
``moh_705_categories.json``) are skipped. A definition that fails validation (for example an unknown
source type) is reported and skipped; it never breaks a migrate.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os

import frappe
from frappe.utils import cint, cstr

from onerc_healthcare.constants import MOH_REPORT_DOCTYPE, MOH_REPORT_STATUSES
from onerc_healthcare.reporting.registry import AVAILABILITY_AVAILABLE, DISAGGREGATION_NONE

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HEADER_FIELDS = ("title", "status", "version", "period_type", "note", "blocked_reason")
BAND_FIELDS = ("label", "min_months", "max_months")
STATUS_BLOCKED = MOH_REPORT_STATUSES[2]
DEFAULT_PERIOD_TYPE = "Monthly"
ELEMENT_FIELDS = (
	"code",
	"label",
	"section",
	"sort_order",
	"source_type",
	"rule",
	"disaggregation",
	"availability",
	"gap_note",
	"dhis2_uid",
)

logger = frappe.logger("onerc_healthcare.reporting")


def parse_json(value, default=None):
	"""A JSON field value as Python: dict/list as is, text parsed, empty as `default`."""
	if value in (None, ""):
		return default
	if isinstance(value, dict | list):
		return value
	return json.loads(value)


def _text(value) -> str:
	return cstr(value).strip() if value is not None else ""


def _canonical_band(row) -> dict:
	return {
		"label": _text(row.get("label")),
		"min_months": cint(row.get("min_months")),
		"max_months": cint(row.get("max_months")) or None,
	}


def _canonical_element(row) -> dict:
	return {
		"code": _text(row.get("code")),
		"label": _text(row.get("label")),
		"section": _text(row.get("section")),
		"sort_order": cint(row.get("sort_order")),
		"source_type": _text(row.get("source_type")),
		"rule": parse_json(row.get("rule")) or None,
		"disaggregation": _text(row.get("disaggregation")) or DISAGGREGATION_NONE,
		"availability": _text(row.get("availability")) or AVAILABILITY_AVAILABLE,
		"gap_note": _text(row.get("gap_note")),
		"dhis2_uid": _text(row.get("dhis2_uid")),
	}


def canonical(definition) -> dict:
	"""The comparable content of a definition: a seed dict or a MOH Report document give the same
	result when they hold the same definition."""
	get = definition.get
	header = {f: _text(get(f)) for f in HEADER_FIELDS}
	# Same defaults and clean-up as the MOH Report controller, so a seed and the record it produced hash
	# the same.
	if header["status"] not in MOH_REPORT_STATUSES:
		header["status"] = MOH_REPORT_STATUSES[0]
	if header["status"] != STATUS_BLOCKED:
		header["blocked_reason"] = ""
	header["period_type"] = header["period_type"] or DEFAULT_PERIOD_TYPE
	return {
		"name": _text(get("name")),
		**header,
		"dimensions": parse_json(get("dimensions")) or None,
		"age_bands": [_canonical_band(r) for r in (get("age_bands") or [])],
		"elements": [_canonical_element(r) for r in (get("elements") or [])],
	}


def definition_hash(definition) -> str:
	payload = json.dumps(canonical(definition), sort_keys=True, separators=(",", ":"), default=str)
	return hashlib.sha256(payload.encode()).hexdigest()


def is_definition(data) -> bool:
	return isinstance(data, dict) and bool(data.get("name")) and "elements" in data


def definitions_in(data) -> list[dict]:
	"""The definitions held by one parsed seed file (see the module docstring)."""
	if isinstance(data, dict) and isinstance(data.get("definitions"), list):
		data = data["definitions"]
	if isinstance(data, list):
		return [d for d in data if is_definition(d)]
	return [data] if is_definition(data) else []


def load_seed_files(directory: str | None = None) -> list[dict]:
	"""Every definition in `directory` (default ``reporting/data``), in file-name order."""
	out = []
	for path in sorted(glob.glob(os.path.join(directory or DATA_DIR, "*.json"))):
		try:
			with open(path, encoding="utf-8") as fh:
				data = json.load(fh)
		except (OSError, ValueError) as e:
			_report(f"MOH report seed {os.path.basename(path)} skipped: {e}")
			continue
		out.extend(definitions_in(data))
	return out


def _apply(doc, definition: dict) -> None:
	"""Write `definition` onto a MOH Report document (header, bands, elements)."""
	data = canonical(definition)
	for f in HEADER_FIELDS:
		doc.set(f, data[f] or None)
	doc.dimensions = json.dumps(data["dimensions"]) if data["dimensions"] else None
	doc.set("age_bands", [])
	for band in data["age_bands"]:
		doc.append("age_bands", {**band, "max_months": band["max_months"] or 0})
	doc.set("elements", [])
	for el in data["elements"]:
		row = {k: (v if v != "" else None) for k, v in el.items()}
		row["rule"] = json.dumps(el["rule"]) if el["rule"] is not None else None
		doc.append("elements", row)


def sync_definition(definition: dict) -> str:
	"""Insert or update one seed definition. Returns "inserted", "updated", "unchanged" or "kept"
	(edited in the desk or created by hand, left alone)."""
	name = _text(definition.get("name"))
	seed_hash = definition_hash(definition)
	if not frappe.db.exists(MOH_REPORT_DOCTYPE, name):
		doc = frappe.new_doc(MOH_REPORT_DOCTYPE)
		doc.report_code = name
		_apply(doc, definition)
		doc.insert(ignore_permissions=True)
		_store_seed_hash(doc)
		return "inserted"
	doc = frappe.get_doc(MOH_REPORT_DOCTYPE, name)
	current = definition_hash(doc)
	if not doc.seed_hash or doc.seed_hash != current:
		return "kept"
	if current == seed_hash:
		return "unchanged"
	_apply(doc, definition)
	doc.save(ignore_permissions=True)
	_store_seed_hash(doc)
	return "updated"


def _store_seed_hash(doc) -> None:
	"""Remember what the seed produced (the saved record, after the controller's clean-up)."""
	doc.db_set("seed_hash", definition_hash(doc), update_modified=False)


def sync_definitions(definitions: list[dict] | None = None, directory: str | None = None) -> dict[str, str]:
	"""Sync `definitions` (default: every seed file) into MOH Report. Returns {name: outcome}.

	Each definition runs in its own savepoint: a failing one is reported ("error") and skipped.
	Never commits.
	"""
	if not frappe.db.table_exists(MOH_REPORT_DOCTYPE):
		return {}
	if definitions is None:
		definitions = load_seed_files(directory)
	results = {}
	for definition in definitions:
		name = _text(definition.get("name"))
		savepoint = "krcs_moh_definition"
		frappe.db.savepoint(savepoint)
		try:
			results[name] = sync_definition(definition)
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			results[name] = "error"
			_report(f"MOH report definition {name} not synced: {e}")
		else:
			frappe.db.release_savepoint(savepoint)
	return results


def _report(message: str) -> None:
	logger.warning(message)
	if frappe.flags.in_install or frappe.flags.in_migrate:
		print(f"onerc_healthcare: {message}")
