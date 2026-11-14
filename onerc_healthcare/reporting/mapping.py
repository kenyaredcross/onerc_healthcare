# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH 705A/705B disease categories and their ICD-11 mappings (R2; FR-29..31, SDD §5.8).

PROVISIONAL — HIS Officer to confirm (OI-5). The category list and every ICD-11 stem are seeded from
``reporting/data/moh_705_categories.json`` and can be corrected in the desk without a code change.

**Resolution of one ICD-11 code** (`resolve`, `Resolver`), first match wins:

1. an enabled **Exact code** mapping equal to the code;
2. the **longest** enabled **Prefix** mapping the code starts with;
3. the **deepest** enabled **Block** mapping among the code's WHO blocks (Code Value ``krcs_blocks``,
   outermost first, so the last mapped one is the deepest);
4. otherwise the **catch-all** category ("All other diseases"); the caller records it as an exception.

A code of another code system (not ICD-11), or an unknown code, also goes to the catch-all.

**Seeds** (`install`, on install and migrate; idempotent; never commits):

- a missing category or mapping is inserted with ``seed_marker`` "Seed v1" and the hash of its seeded
  values in ``seed_hash``;
- an existing row is updated to a newer seed **only while it is unedited**: its marker is still a seed
  marker and the hash of its current values equals ``seed_hash``. Saving a seeded row in the desk with a
  changed value sets the marker to "Edited" (controller), and a change made any other way is detected by
  the hash; either way the seed never touches that row again. Rows created in the desk have no marker.
- Nothing is ever deleted: a mapping is switched off with ``enabled``.

**Fingerprint** (`fingerprint`): a hash of every category and enabled mapping. A run stores it
(``krcs_report_fingerprints`` hook → `report_fingerprint`) so a later reader knows which mapping counted it.

**Endpoints:** `test_code` (GET; HIS Officer, Medical In-Charge) is the "Test code" button;
`map_codes` (POST; HIS Officer only) is "Map selected codes" on the MOH Unmapped Diagnoses report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint, cstr

from onerc_healthcare.coding.setup import ICD11_CODE_SYSTEM
from onerc_healthcare.constants import (
	ICD11_BLOCK_DOCTYPE,
	MOH_CATEGORY_DOCTYPE,
	MOH_DATA_ELEMENT_DOCTYPE,
	MOH_MAPPING_DOCTYPE,
	ROLE_HIS,
	ROLE_MIC,
)

SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "moh_705_categories.json")
PROVISIONAL_NOTE = "PROVISIONAL — HIS Officer to confirm (OI-5)"
SEED_EDITED = "Edited"
SEED_FLAG = "krcs_moh_seeding"  # frappe.flags key set while install() writes rows

MATCH_EXACT = "Exact code"
MATCH_PREFIX = "Prefix"
MATCH_BLOCK = "Block"
MATCH_TYPES = (MATCH_EXACT, MATCH_PREFIX, MATCH_BLOCK)
MATCHED_CATCH_ALL = "Catch-all"  # no mapping: the catch-all category
MATCHED_NONE = "Unmapped"  # no mapping and no catch-all category configured

FORM_705A = "705A"
FORM_705B = "705B"
FORM_BOTH = "Both"
FORMS = (FORM_BOTH, FORM_705A, FORM_705B)

TEST_ROLES = (ROLE_HIS, ROLE_MIC)  # "Test code" and the two reports
MAP_ROLES = (ROLE_HIS,)  # bulk "Map selected codes"
MAX_BULK_CODES = 200

# Values a seed owns, per DocType. The seed hash covers exactly these.
CATEGORY_SEED_FIELDS = (
	"title",
	"form",
	"category_group",
	"priority",
	"is_catch_all",
	"excluded",
	"sort_order",
	"requires_lab_confirmation",
	"fallback_category",
	"confirmation_tests",
	"positive_results",
	"note",
)
MAPPING_SEED_FIELDS = ("category", "enabled", "note")
_INT_FIELDS = {"priority", "is_catch_all", "excluded", "sort_order", "requires_lab_confirmation", "enabled"}

_NAME_PREFIX = {MATCH_EXACT: "EXACT", MATCH_PREFIX: "PREFIX", MATCH_BLOCK: "BLOCK"}


# --- names and values -----------------------------------------------------------------------------------


def mapping_name(match_type: str, value: str) -> str:
	"""Deterministic MOH Diagnosis Mapping name, e.g. ``PREFIX-1F4`` (one mapping per type and value)."""
	return f"{_NAME_PREFIX.get(match_type, 'MAP')}-{value}"


def normalise_value(match_type: str, value: str | None) -> str:
	value = cstr(value).strip()
	if match_type in (MATCH_EXACT, MATCH_PREFIX):
		value = value.upper()
		if value.endswith(f"-{ICD11_CODE_SYSTEM}"):  # a Code Value name pasted in
			value = value[: -len(ICD11_CODE_SYSTEM) - 1]
	return value


_VALUE_PATTERNS = {
	MATCH_EXACT: re.compile(r"^[0-9A-Z][0-9A-Z.]*$"),
	MATCH_PREFIX: re.compile(r"^[0-9A-Z][0-9A-Z.]*$"),
	MATCH_BLOCK: re.compile(r"^Block[A-Za-z0-9-]+$"),
}


def value_pattern_ok(match_type: str, value: str) -> bool:
	pattern = _VALUE_PATTERNS.get(match_type)
	return bool(pattern and pattern.match(value or ""))


def split_lines(value: str | None) -> list[str]:
	return [line.strip() for line in cstr(value).splitlines() if line.strip()]


def value_info(match_type: str, value: str) -> tuple[int, str | None]:
	"""(number of ICD-11 codes the value matches, title of the code or block). 0 means it does not exist."""
	if not value_pattern_ok(match_type, value):
		return 0, None
	if match_type == MATCH_EXACT:
		title = frappe.db.get_value(
			"Code Value", {"code_system": ICD11_CODE_SYSTEM, "code_value": value}, "display"
		)
		return (1, title) if title is not None else (0, None)
	if match_type == MATCH_PREFIX:
		rows = frappe.db.sql(
			"""SELECT COUNT(*), MIN(code_value) FROM `tabCode Value`
			WHERE code_system = %s AND code_value LIKE %s""",
			(ICD11_CODE_SYSTEM, f"{value}%"),  # value_pattern_ok() allows no LIKE wildcard
		)
		count, first = rows[0] if rows else (0, None)
		if not count:
			return 0, None
		title = frappe.db.get_value(
			"Code Value", {"code_system": ICD11_CODE_SYSTEM, "code_value": first}, "display"
		)
		return cint(count), title
	if match_type == MATCH_BLOCK:
		title = frappe.db.get_value(ICD11_BLOCK_DOCTYPE, value, "title")
		if title is None:
			return 0, None
		count = frappe.db.sql(
			"""SELECT COUNT(*) FROM `tabCode Value`
			WHERE code_system = %s AND FIND_IN_SET(%s, krcs_blocks)""",
			(ICD11_CODE_SYSTEM, value),
		)[0][0]
		return cint(count) or 0, title
	return 0, None


# --- seed marker (used by the two controllers) ------------------------------------------------------------


def canonical(doc, fields: tuple[str, ...]) -> dict:
	out = {}
	for f in fields:
		v = doc.get(f)
		out[f] = cint(v) if f in _INT_FIELDS else (cstr(v).strip() or None)
	return out


def seed_hash(values: dict) -> str:
	return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def mark_edited(doc, fields: tuple[str, ...]) -> None:
	"""Controller hook: a seeded row saved in the desk with a different value becomes "Edited"."""
	if frappe.flags.get(SEED_FLAG) or doc.is_new():
		return
	if not doc.seed_marker or doc.seed_marker == SEED_EDITED:
		return
	if doc.seed_hash != seed_hash(canonical(doc, fields)):
		doc.seed_marker = SEED_EDITED


# --- resolver -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Category:
	name: str
	title: str
	form: str
	group: str
	priority: int
	is_catch_all: bool
	excluded: bool
	sort_order: int
	requires_lab_confirmation: bool
	fallback: str | None
	tests: tuple[str, ...]
	positives: frozenset[str]

	def on_form(self, form: str | None) -> bool:
		"""The category is a line of `form` (705A / 705B). None (age unknown): treated as on the form."""
		return form is None or self.form in (FORM_BOTH, form)


@dataclass(frozen=True)
class Resolution:
	category: str | None
	matched_by: str  # MATCH_* / MATCHED_CATCH_ALL / MATCHED_NONE
	value: str | None  # the mapping's value (None for the catch-all)

	@property
	def unmapped(self) -> bool:
		return self.matched_by in (MATCHED_CATCH_ALL, MATCHED_NONE)

	def as_tuple(self) -> tuple[str | None, str, str | None]:
		return self.category, self.matched_by, self.value


class Resolver:
	"""Every category and enabled mapping, loaded once (a few hundred rows): resolve many codes cheaply."""

	def __init__(self) -> None:
		self.categories: dict[str, Category] = {}
		for row in frappe.get_all(MOH_CATEGORY_DOCTYPE, fields=["name", *CATEGORY_SEED_FIELDS]):
			self.categories[row.name] = Category(
				name=row.name,
				title=row.title or row.name,
				form=row.form or FORM_BOTH,
				group=cstr(row.category_group).strip(),
				priority=cint(row.priority),
				is_catch_all=bool(row.is_catch_all),
				excluded=bool(row.excluded),
				sort_order=cint(row.sort_order),
				requires_lab_confirmation=bool(row.requires_lab_confirmation),
				fallback=row.fallback_category or None,
				tests=tuple(split_lines(row.confirmation_tests)),
				positives=frozenset(v.lower() for v in split_lines(row.positive_results)),
			)
		catch_all = sorted(c.name for c in self.categories.values() if c.is_catch_all)
		self.catch_all: str | None = catch_all[0] if catch_all else None
		self.maps: dict[str, dict[str, str]] = {t: {} for t in MATCH_TYPES}
		for row in frappe.get_all(
			MOH_MAPPING_DOCTYPE, filters={"enabled": 1}, fields=["match_type", "value", "category"]
		):
			if row.match_type in self.maps and row.category in self.categories:
				self.maps[row.match_type][row.value] = row.category
		self.max_prefix = max((len(v) for v in self.maps[MATCH_PREFIX]), default=0)

	def resolve_value(self, code: str | None, blocks: str | list[str] | None = None) -> Resolution:
		"""Resolve an ICD-11 code value (``1F40.Z``) with its block ids (``krcs_blocks``)."""
		if code:
			category = self.maps[MATCH_EXACT].get(code)
			if category:
				return Resolution(category, MATCH_EXACT, code)
			prefixes = self.maps[MATCH_PREFIX]
			for n in range(min(len(code), self.max_prefix), 0, -1):
				category = prefixes.get(code[:n])
				if category:
					return Resolution(category, MATCH_PREFIX, code[:n])
			if isinstance(blocks, str):
				blocks = [b for b in blocks.split(",") if b]
			for block in reversed(blocks or []):  # outermost first: the last mapped one is the deepest
				category = self.maps[MATCH_BLOCK].get(block)
				if category:
					return Resolution(category, MATCH_BLOCK, block)
		if self.catch_all:
			return Resolution(self.catch_all, MATCHED_CATCH_ALL, None)
		return Resolution(None, MATCHED_NONE, None)


def code_row(code: str) -> frappe._dict | None:
	"""Code Value fields for a Code Value name (``1F40.Z-ICD-11``) or an ICD-11 code value (``1F40.Z``)."""
	fields = ["name", "code_value", "code_system", "display", "krcs_blocks"]
	code = cstr(code).strip()
	if not code:
		return None
	row = frappe.db.get_value("Code Value", code, fields, as_dict=True)
	if not row:
		row = frappe.db.get_value(
			"Code Value",
			{"code_system": ICD11_CODE_SYSTEM, "code_value": normalise_value(MATCH_EXACT, code)},
			fields,
			as_dict=True,
		)
	return row


def resolve(code: str, resolver: Resolver | None = None) -> tuple[str | None, str, str | None]:
	"""(category, matched_by, value) for a Code Value name or ICD-11 code value (module docstring)."""
	resolver = resolver or Resolver()
	row = code_row(code)
	if not row or row.code_system != ICD11_CODE_SYSTEM:
		return resolver.resolve_value(None).as_tuple()
	return resolver.resolve_value(row.code_value, row.krcs_blocks).as_tuple()


# --- fingerprint ----------------------------------------------------------------------------------------


def fingerprint() -> str:
	"""Hash of every category (its counting fields) and every enabled mapping."""
	categories = [
		{"name": r.name, **canonical(r, tuple(f for f in CATEGORY_SEED_FIELDS if f != "note"))}
		for r in frappe.get_all(
			MOH_CATEGORY_DOCTYPE, fields=["name", *CATEGORY_SEED_FIELDS], order_by="name asc"
		)
	]
	mappings = [
		[r.match_type, r.value, r.category]
		for r in frappe.get_all(
			MOH_MAPPING_DOCTYPE,
			filters={"enabled": 1},
			fields=["match_type", "value", "category"],
			order_by="name asc",
		)
	]
	payload = json.dumps({"categories": categories, "mappings": mappings}, sort_keys=True, ensure_ascii=False)
	return hashlib.sha256(payload.encode()).hexdigest()[:16]


def report_fingerprint(report: str, ctx=None) -> dict:
	"""``krcs_report_fingerprints`` hook (R1): the mapping fingerprint for reports that count diagnoses."""
	element_doctype = MOH_DATA_ELEMENT_DOCTYPE
	if (
		not report
		or not frappe.db.table_exists(element_doctype)
		or not frappe.db.exists(element_doctype, {"parent": report, "source_type": "Diagnosis Category"})
	):
		return {}
	return {"moh_diagnosis_mapping": fingerprint()}


# --- seeds ----------------------------------------------------------------------------------------------


def load_seed(path: str = SEED_FILE) -> dict:
	with open(path, encoding="utf-8") as f:
		return json.load(f)


def seed_rows(data: dict) -> tuple[list[dict], list[dict]]:
	"""(category rows, mapping rows) of a seed file, with the values a seed owns."""
	version = data.get("seed_version") or "Seed v1"
	note = data.get("note") or PROVISIONAL_NOTE
	categories, mappings = [], []
	for c in data.get("categories", []):
		row = {
			"name": c["name"],
			"title": c.get("title") or c["name"],
			"form": c.get("form") or FORM_BOTH,
			"category_group": c.get("group") or None,
			"priority": cint(c.get("priority")),
			"is_catch_all": cint(c.get("is_catch_all")),
			"excluded": cint(c.get("excluded")),
			"sort_order": cint(c.get("sort_order")),
			"requires_lab_confirmation": cint(c.get("requires_lab_confirmation")),
			"fallback_category": c.get("fallback_category") or None,
			"confirmation_tests": "\n".join(c.get("confirmation_tests") or []) or None,
			"positive_results": "\n".join(c.get("positive_results") or []) or None,
			"note": " ".join(x for x in (note, c.get("note")) if x),
			"seed_marker": version,
		}
		categories.append(row)
		for m in c.get("mappings", []):
			match_type = m["match_type"]
			value = normalise_value(match_type, m["value"])
			mappings.append(
				{
					"name": mapping_name(match_type, value),
					"match_type": match_type,
					"value": value,
					"category": m.get("category") or c["name"],
					"enabled": cint(m.get("enabled", 1)),
					"note": " ".join(x for x in (note, m.get("note")) if x),
					"seed_marker": version,
				}
			)
	# Categories a fallback points at go first, so the Link validates.
	categories.sort(key=lambda r: 1 if r["fallback_category"] else 0)
	return categories, mappings


def install(data: dict | None = None) -> dict:
	"""Load the seed categories and mappings (module docstring). Returns counts. Never commits."""
	if not frappe.db.table_exists(MOH_CATEGORY_DOCTYPE) or not frappe.db.table_exists(MOH_MAPPING_DOCTYPE):
		return {}
	data = data if data is not None else load_seed()
	categories, mappings = seed_rows(data)
	counts = {"created": 0, "updated": 0, "kept": 0, "unchanged": 0, "failed": 0}
	previous = frappe.flags.get(SEED_FLAG)
	frappe.flags[SEED_FLAG] = True
	try:
		for row in categories:
			_sync_row(MOH_CATEGORY_DOCTYPE, row, CATEGORY_SEED_FIELDS, counts, {"category_name": row["name"]})
		for row in mappings:
			_sync_row(
				MOH_MAPPING_DOCTYPE,
				row,
				MAPPING_SEED_FIELDS,
				counts,
				{"match_type": row["match_type"], "value": row["value"]},
			)
	finally:
		frappe.flags[SEED_FLAG] = previous
	return counts


def _sync_row(doctype: str, row: dict, fields: tuple[str, ...], counts: dict, identity: dict) -> None:
	values = {f: row[f] for f in fields}
	new_hash = seed_hash(canonical(frappe._dict(values), fields))
	name = row["name"]
	current = frappe.db.get_value(doctype, name, ["name", "seed_marker", "seed_hash", *fields], as_dict=True)
	try:
		if current is None:
			doc = frappe.get_doc(
				{
					"doctype": doctype,
					**identity,
					**values,
					"seed_marker": row["seed_marker"],
					"seed_hash": new_hash,
				}
			)
			if doctype == MOH_MAPPING_DOCTYPE:
				doc.name = name
			doc.insert(ignore_permissions=True)
			counts["created"] += 1
			return
		if not current.seed_marker or current.seed_marker == SEED_EDITED:
			counts["kept"] += 1  # created or edited in the desk: never touched
			return
		if seed_hash(canonical(current, fields)) != current.seed_hash:
			# changed outside the form (no controller ran): record it, and keep it
			frappe.db.set_value(doctype, name, "seed_marker", SEED_EDITED, update_modified=False)
			counts["kept"] += 1
			return
		if current.seed_hash == new_hash and current.seed_marker == row["seed_marker"]:
			counts["unchanged"] += 1
			return
		doc = frappe.get_doc(doctype, name)
		doc.update(values)
		doc.seed_marker = row["seed_marker"]
		doc.seed_hash = new_hash
		doc.save(ignore_permissions=True)
		counts["updated"] += 1
	except frappe.ValidationError as e:
		# A seed row that no longer validates (for example a code missing from a newer release) is
		# reported and skipped; it never breaks a migrate.
		frappe.clear_last_message()
		counts["failed"] += 1
		frappe.logger("onerc_healthcare.reporting").warning(f"MOH seed {doctype} {name} skipped: {e}")
		print(f"onerc_healthcare: MOH seed {doctype} {name} skipped: {e}")


# --- endpoints ------------------------------------------------------------------------------------------


def _require_roles(roles: tuple[str, ...]) -> None:
	if not set(roles) & set(frappe.get_roles()):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist(methods=["GET"])
def test_code(code: str) -> dict:
	""" "Test code": which MOH category an ICD-11 code is counted under, and why. HIS Officer, MIC."""
	_require_roles(TEST_ROLES)
	resolver = Resolver()
	row = code_row(code)
	if not row:
		frappe.throw(_("Unknown code {0}.").format(frappe.bold(code)))
	if row.code_system != ICD11_CODE_SYSTEM:
		res = resolver.resolve_value(None)
	else:
		res = resolver.resolve_value(row.code_value, row.krcs_blocks)
	category = resolver.categories.get(res.category) if res.category else None
	return {
		"code": row.name,
		"code_value": row.code_value,
		"code_title": row.display,
		"blocks": [b for b in cstr(row.krcs_blocks).split(",") if b],
		"category": res.category,
		"category_title": category.title if category else None,
		"form": category.form if category else None,
		"matched_by": res.matched_by,
		"value": res.value,
		"mapping": mapping_name(res.matched_by, res.value) if res.matched_by in MATCH_TYPES else None,
		"excluded": bool(category and category.excluded),
		"unmapped": res.unmapped,
		"requires_lab_confirmation": bool(category and category.requires_lab_confirmation),
		"fallback_category": category.fallback if category else None,
	}


@frappe.whitelist(methods=["POST"])
def map_codes(codes: list | str, category: str, note: str | None = None) -> dict:
	""" "Map selected codes": an Exact code mapping to `category` for each code. HIS Officer only.

	A code that already has an Exact mapping is left alone and reported. Returns created / skipped.
	"""
	_require_roles(MAP_ROLES)
	if isinstance(codes, str):
		codes = frappe.parse_json(codes) if codes.strip().startswith("[") else [codes]
	codes = [cstr(c).strip() for c in (codes or []) if cstr(c).strip()]
	if not codes:
		frappe.throw(_("Select at least one code."))
	if len(codes) > MAX_BULK_CODES:
		frappe.throw(_("Map at most {0} codes at a time.").format(MAX_BULK_CODES))
	if not frappe.db.exists(MOH_CATEGORY_DOCTYPE, category):
		frappe.throw(_("Unknown MOH Disease Category {0}.").format(frappe.bold(category)))
	created, skipped = [], []
	for code in dict.fromkeys(codes):
		row = code_row(code)
		if not row or row.code_system != ICD11_CODE_SYSTEM:
			skipped.append({"code": code, "reason": _("not an ICD-11 code")})
			continue
		name = mapping_name(MATCH_EXACT, row.code_value)
		existing = frappe.db.get_value(MOH_MAPPING_DOCTYPE, name, "category")
		if existing:
			skipped.append({"code": row.code_value, "reason": _("already mapped to {0}").format(existing)})
			continue
		doc = frappe.get_doc(
			{
				"doctype": MOH_MAPPING_DOCTYPE,
				"match_type": MATCH_EXACT,
				"value": row.code_value,
				"category": category,
				"enabled": 1,
				"note": note or _("Mapped from MOH Unmapped Diagnoses"),
			}
		)
		doc.insert()  # the HIS Officer's own create permission applies
		created.append(doc.name)
	return {"created": created, "skipped": skipped, "fingerprint": fingerprint()}
