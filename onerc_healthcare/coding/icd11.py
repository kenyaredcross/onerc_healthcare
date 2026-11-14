# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""ICD-11 MMS importer (FR-11, BR-4, SDD §4.5).

Loads the WHO ICD-11 MMS *SimpleTabulation* file into ``Code Value`` under the ``ICD-11`` Code
System. The WHO 2025-01 release is bundled in ``coding/data/`` (unmodified, gzip-compressed; licence
and checksum in ``coding/data/NOTICE.md``, decision D11) and is imported automatically on install and
migrate by ``import_bundled()``. Another release file can be loaded by hand::

    bench --site <site> execute onerc_healthcare.coding.icd11.import_file \
        --kwargs '{"path": "/path/to/SimpleTabulation-ICD-11-MMS-en.txt"}'

Accepted formats (optionally ``.gz``-compressed): the tab-separated ``.txt`` / ``.tsv``, a ``.csv`` with the same columns, or the
``.xlsx`` export. Columns used: Code, BlockId, Title, ClassKind, DepthInKind, ChapterNo, BrowserLink,
isLeaf, Linearization URI. Titles carry one ``- `` per depth level; the depth gives the parent.

Only rows with a code (categories) become Code Values; chapters and blocks only give parents.

Block structure (Phase 3, R2): WHO's Grouping1-5 columns hold the block ids of each row's ancestors,
outermost first. Every block becomes an ``ICD-11 Block`` record (block id, title, chapter, parent block,
depth), and each code stores its ancestors' block ids in Code Value ``krcs_blocks`` (comma-separated,
outermost first) so an MOH Diagnosis Mapping can target a block. WHO's "Primary tabulation" flag is kept
in ``krcs_primary_tabulation`` for information only. ``ensure_structure()`` backfills both once per
release on sites imported before they existed, with set-based updates (no per-row saves).
The import is idempotent: existing codes are updated when the release changed them, else left alone.
New rows are bulk-inserted; when run outside tests the transaction is committed every ``batch_size``
rows so a 35k-row release does not hold one huge transaction.

``coding/test_data/SimpleTabulation-ICD-11-MMS-en.sample.txt`` is the header and first 14 rows of the
WHO 2025-01 release file, unmodified (CC BY-ND 3.0 IGO), used by the tests only.
"""

import csv
import gzip
import os
import re
import time

import frappe
from frappe.utils import cint, now

from onerc_healthcare.coding.setup import ICD11_CODE_SYSTEM, ensure_code_system
from onerc_healthcare.constants import ICD11_BLOCK_DOCTYPE

DEFAULT_BATCH_SIZE = 2000

# The WHO release bundled with the app (unmodified, gzip-compressed; see coding/data/NOTICE.md).
BUNDLED_RELEASE = "2025-01"
BUNDLED_FILE = os.path.join(
	os.path.dirname(__file__), "data", f"SimpleTabulation-ICD-11-MMS-en-{BUNDLED_RELEASE}.txt.gz"
)
RELEASE_DEFAULT_KEY = "krcs_icd11_release"
# Release whose block structure (ICD-11 Block, Code Value krcs_blocks) is loaded on this site.
STRUCTURE_DEFAULT_KEY = "krcs_icd11_structure_release"
GROUPING_COLUMNS = ("Grouping1", "Grouping2", "Grouping3", "Grouping4", "Grouping5")
BLOCK_SEPARATOR = ","
# Code Value columns holding the block structure (custom fields from coding.setup).
STRUCTURE_FIELDS = ("krcs_blocks", "krcs_primary_tabulation")
_UPDATE_BATCH = 1000
_DEPTH_MARKER = re.compile(r"^(?:-\s)+|^-+\s*")
_HYPERLINK = re.compile(r'hyperlink\("([^"]+)"', re.IGNORECASE)
# Code Value columns written by the importer (besides the standard ones).
_VALUE_FIELDS = (
	"display",
	"system_uri",
	"official_url",
	"level",
	"krcs_parent_code",
	"krcs_chapter",
	"krcs_is_leaf",
)


def import_file(path: str, batch_size: int = DEFAULT_BATCH_SIZE, commit: bool | None = None) -> dict:
	"""Import a SimpleTabulation file into Code Value (and its blocks into ICD-11 Block). Returns counts.

	`commit` defaults to True outside tests; inside tests nothing is ever committed.
	"""
	if not os.path.isfile(path):
		frappe.throw(f"ICD-11 file not found: {path}")
	ensure_code_system()
	if commit is None:
		commit = not frappe.in_test
	commit = bool(commit) and not frappe.in_test
	batch_size = max(cint(batch_size), 1)
	structure = has_structure_fields()

	existing = _existing_codes(structure)
	counts = {"rows": 0, "with_code": 0, "created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
	pending: list[dict] = []
	seen: set[str] = set()
	entries = list(parse_rows(read_rows(path)))

	for entry in entries:
		counts["rows"] += 1
		code = entry["code"]
		if not code or code in seen:
			counts["skipped"] += 1
			continue
		seen.add(code)
		counts["with_code"] += 1
		values = {
			"display": entry["title"],
			"system_uri": entry["uri"],
			"official_url": entry["browser_link"],
			"level": entry["level"],
			"krcs_parent_code": entry["parent"],
			"krcs_chapter": entry["chapter"],
			"krcs_is_leaf": entry["is_leaf"],
		}
		current = existing.get(code)
		if current is None:
			pending.append({"code_value": code, **values, **_structure_values(entry)})
			if len(pending) >= batch_size:
				counts["created"] += _insert(pending, structure)
				pending = []
				if commit:
					frappe.db.commit()  # nosemgrep: CLI batch import only, never inside tests
		elif any(_differs(current.get(k), v) for k, v in values.items()):
			frappe.db.set_value("Code Value", current.name, values)
			counts["updated"] += 1
		else:
			counts["unchanged"] += 1

	counts["created"] += _insert(pending, structure)
	if structure:
		counts.update(_sync_structure(entries, existing))
	if commit:
		frappe.db.commit()  # nosemgrep: CLI batch import only, never inside tests
	return counts


def has_structure_fields() -> bool:
	"""The block-structure columns and DocType exist (coding.setup creates them before any import)."""
	meta = frappe.get_meta("Code Value")
	return all(meta.has_field(f) for f in STRUCTURE_FIELDS) and frappe.db.table_exists(ICD11_BLOCK_DOCTYPE)


def _existing_codes(structure: bool) -> dict:
	fields = ["name", "code_value", *_VALUE_FIELDS, *(STRUCTURE_FIELDS if structure else ())]
	return {
		row.code_value: row
		for row in frappe.get_all("Code Value", filters={"code_system": ICD11_CODE_SYSTEM}, fields=fields)
	}


def ensure_structure(force: bool = False, path: str = BUNDLED_FILE) -> dict | None:
	"""Backfill the block structure of an already imported release, once per site and release.

	Called from coding.setup.install() (install and migrate) and by the patch
	``onerc_healthcare.patches.icd11_block_backfill``. Skips when the structure of this release is already
	loaded, or when no ICD-11 code exists yet (the import itself then loads it). Idempotent, set-based,
	never commits.
	"""
	if not has_structure_fields():
		return None
	if (
		not force
		and frappe.db.get_default(STRUCTURE_DEFAULT_KEY) == BUNDLED_RELEASE
		and frappe.get_all(ICD11_BLOCK_DOCTYPE, limit=1)
	):
		return None
	if not frappe.db.exists("Code Value", {"code_system": ICD11_CODE_SYSTEM}):
		return None
	started = time.monotonic()
	counts = _sync_structure(list(parse_rows(read_rows(path))), _existing_codes(True))
	counts["seconds"] = round(time.monotonic() - started, 2)
	frappe.db.set_default(STRUCTURE_DEFAULT_KEY, BUNDLED_RELEASE)
	print(f"onerc_healthcare: ICD-11 {BUNDLED_RELEASE} block structure: {counts}")
	return counts


def _structure_values(entry: dict) -> dict:
	return {
		"krcs_blocks": BLOCK_SEPARATOR.join(entry["blocks"]),
		"krcs_primary_tabulation": entry["primary_tabulation"],
	}


def _sync_structure(entries: list[dict], existing: dict) -> dict:
	"""ICD-11 Block records plus Code Value krcs_blocks / krcs_primary_tabulation, set-based."""
	created, updated = _sync_blocks(entries)
	changed = []
	for entry in entries:
		row = existing.get(entry["code"]) if entry["code"] else None
		if row is None:
			continue
		values = _structure_values(entry)
		if (row.get("krcs_blocks") or "") != values["krcs_blocks"] or cint(
			row.get("krcs_primary_tabulation")
		) != values["krcs_primary_tabulation"]:
			changed.append((row.name, values["krcs_blocks"], values["krcs_primary_tabulation"]))
	for i in range(0, len(changed), _UPDATE_BATCH):
		_update_structure(changed[i : i + _UPDATE_BATCH])
	return {"blocks_created": created, "blocks_updated": updated, "codes_structured": len(changed)}


def _update_structure(rows: list[tuple[str, str, int]]) -> None:
	"""One UPDATE for a batch: CASE on the primary key (``modified`` is left alone: not a content edit)."""
	cases = " ".join(["WHEN %s THEN %s"] * len(rows))
	params: list = []
	for name, blocks, _primary in rows:
		params += [name, blocks]
	for name, _blocks, primary in rows:
		params += [name, primary]
	params += [r[0] for r in rows]
	frappe.db.sql(
		f"""UPDATE `tabCode Value`
		SET krcs_blocks = CASE name {cases} ELSE krcs_blocks END,
			krcs_primary_tabulation = CASE name {cases} ELSE krcs_primary_tabulation END
		WHERE name IN ({", ".join(["%s"] * len(rows))})""",
		params,
	)


def _sync_blocks(entries: list[dict]) -> tuple[int, int]:
	"""Insert missing ICD-11 Block records, update changed ones. Returns (created, updated)."""
	rows = {}
	for entry in entries:
		if entry["class_kind"] == "block" and entry["block"] and entry["block"] not in rows:
			rows[entry["block"]] = {
				"title": entry["title"][:255],
				"chapter": entry["chapter"],
				"parent_block": entry["blocks"][-1] if entry["blocks"] else None,
				"depth": entry["level"] or len(entry["blocks"]) + 1,
			}
	existing = {
		r.name: r
		for r in frappe.get_all(
			ICD11_BLOCK_DOCTYPE, fields=["name", "title", "chapter", "parent_block", "depth"]
		)
	}
	new, updated = [], 0
	for block_id, values in rows.items():
		current = existing.get(block_id)
		if current is None:
			new.append((block_id, values))
		elif any(_differs(current.get(k), v) for k, v in values.items()):
			frappe.db.set_value(ICD11_BLOCK_DOCTYPE, block_id, values, update_modified=False)
			updated += 1
	if new:
		ts, user = now(), frappe.session.user
		frappe.db.bulk_insert(
			ICD11_BLOCK_DOCTYPE,
			[
				"name",
				"block_id",
				"title",
				"chapter",
				"parent_block",
				"depth",
				"owner",
				"modified_by",
				"creation",
				"modified",
			],
			[
				(b, b, v["title"], v["chapter"], v["parent_block"], v["depth"], user, user, ts, ts)
				for b, v in new
			],
			ignore_duplicates=True,
		)
	return len(new), updated


def import_bundled(force: bool = False) -> dict | None:
	"""Import the WHO release bundled with the app, once per site and release.

	Called from coding.setup.install() on after_install / after_migrate. Skips if this release was
	already imported and ICD-11 codes exist, unless `force`. Never commits (install/migrate does).
	"""
	if (
		not force
		and frappe.db.get_default(RELEASE_DEFAULT_KEY) == BUNDLED_RELEASE
		and frappe.db.exists("Code Value", {"code_system": ICD11_CODE_SYSTEM})
	):
		return None
	counts = import_file(BUNDLED_FILE, commit=False)
	frappe.db.set_default(RELEASE_DEFAULT_KEY, BUNDLED_RELEASE)
	if has_structure_fields():
		frappe.db.set_default(STRUCTURE_DEFAULT_KEY, BUNDLED_RELEASE)
	print(f"onerc_healthcare: ICD-11 {BUNDLED_RELEASE} imported: {counts}")
	return counts


def read_rows(path: str):
	"""Yield each data row as a dict keyed by the header, for .txt/.tsv/.csv/.xlsx (optionally .gz)."""
	compressed = path.lower().endswith(".gz")
	ext = os.path.splitext(path[:-3] if compressed else path)[1].lower()
	if ext in (".xlsx", ".xlsm"):
		yield from _read_xlsx(path)
		return
	opener = gzip.open if compressed else open
	with opener(path, "rt", encoding="utf-8-sig", newline="") as f:
		first = f.readline()
		f.seek(0)
		delimiter = "\t" if ("\t" in first or ext in (".txt", ".tsv")) else ","
		for row in csv.DictReader(f, delimiter=delimiter):
			yield {(k or "").strip(): (v or "") for k, v in row.items()}


def _read_xlsx(path: str):
	from openpyxl import load_workbook

	wb = load_workbook(path, read_only=True, data_only=False)
	try:
		rows = wb.worksheets[0].iter_rows(values_only=True)
		header = [str(h or "").strip() for h in next(rows)]
		for row in rows:
			yield {h: "" if v is None else str(v) for h, v in zip(header, row, strict=False)}
	finally:
		wb.close()


def parse_rows(rows):
	"""Normalise raw rows and derive each row's parent from its depth (leading dashes of Title).

	The parent is the nearest earlier row one level up: a category code, a BlockId, or
	``Chapter NN`` for chapter rows (which have neither).
	"""
	stack: list[tuple[int, str]] = []  # (depth, identifier)
	for row in rows:
		raw_title = (row.get("Title") or "").strip()
		marker = _DEPTH_MARKER.match(raw_title)
		depth = marker.group(0).count("-") if marker else 0
		chapter = (row.get("ChapterNo") or "").strip()
		code = (row.get("Code") or "").strip()
		block = (row.get("BlockId") or "").strip()
		identifier = code or block or (f"Chapter {chapter}" if chapter else "")
		groupings = [g for g in ((row.get(c) or "").strip() for c in GROUPING_COLUMNS) if g]

		while stack and stack[-1][0] >= depth:
			stack.pop()
		parent = stack[-1][1] if stack else ""
		stack.append((depth, identifier))

		yield {
			"code": code,
			"block": block,
			"title": raw_title[marker.end() :].strip() if marker else raw_title,
			"class_kind": (row.get("ClassKind") or "").strip().lower(),
			"chapter": chapter,
			"parent": parent,
			"level": cint(row.get("DepthInKind")),
			"is_leaf": 1 if (row.get("isLeaf") or row.get("IsLeaf") or "").strip().lower() == "true" else 0,
			"uri": (row.get("Linearization URI") or row.get("Linearization (release) URI") or "").strip(),
			"browser_link": _browser_link(row.get("BrowserLink") or ""),
			"blocks": groupings,
			"primary_tabulation": 1 if (row.get("Primary tabulation") or "").strip().lower() == "true" else 0,
		}


def _browser_link(value: str) -> str:
	match = _HYPERLINK.search(value)
	return match.group(1) if match else value.strip()


def _differs(current, new) -> bool:
	if isinstance(new, int):
		return cint(current) != new
	return (current or "") != (new or "")


def _insert(rows: list[dict], structure: bool = False) -> int:
	"""Bulk insert new Code Values. Code Value has no controller logic beyond its name
	(``<code_value>-<code_system>``), which is reproduced here."""
	if not rows:
		return 0
	ts, user = now(), frappe.session.user
	value_fields = (*_VALUE_FIELDS, *(STRUCTURE_FIELDS if structure else ()))
	fields = [
		"name",
		"code_system",
		"code_value",
		*value_fields,
		"owner",
		"modified_by",
		"creation",
		"modified",
	]
	values = [
		(
			f"{r['code_value']}-{ICD11_CODE_SYSTEM}",
			ICD11_CODE_SYSTEM,
			r["code_value"],
			*(r[k] for k in value_fields),
			user,
			user,
			ts,
			ts,
		)
		for r in rows
	]
	frappe.db.bulk_insert("Code Value", fields, values, ignore_duplicates=True)
	return len(rows)
