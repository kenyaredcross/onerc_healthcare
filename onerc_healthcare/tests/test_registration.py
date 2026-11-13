# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""A1 Registration and identity (FR-1 to FR-4, BR-1, C-6, NFR-9, NFR-10; SDD 4.2, 4.3, 4.5, 5.1)."""

import json
import random
import re
from unittest.mock import patch

import frappe
from frappe.utils import getdate, random_string

from onerc_healthcare.constants import (
	IDENTIFIER_FACILITY,
	IDENTIFIER_NATIONAL,
	IDENTIFIER_POL,
	IDENTIFIER_RATION,
	IDENTIFIER_TYPES,
	IDENTIFIER_UNHCR,
	PATIENT_IDENTIFIER_TYPE_DOCTYPE,
	PATIENT_IDENTIFIERS_FIELD,
	ROLE_HIS,
	ROLE_RECEPTIONIST,
	ROLE_SYSADMIN,
	ROLE_TRIAGE,
)
from onerc_healthcare.registration import api
from onerc_healthcare.registration.identifiers import (
	MERGE_LOG_FIELD,
	MERGED_INTO_FIELD,
	OVERRIDE_REASON_FIELD,
	POPULATION_GROUP_FIELD,
	REGISTRATION_FACILITY_FIELD,
	SEARCH_FIELD,
	UNIQUE_CONSTRAINT,
	DuplicateIdentifierError,
	FacilityIdentifierError,
	PossibleDuplicatePatientError,
	normalise,
)
from onerc_healthcare.registration.names import FIRST_NAME_KEY_FIELD, LAST_NAME_KEY_FIELD, name_key
from onerc_healthcare.registration.residence import (
	ensure_residence_root,
	get_residence_levels,
	get_residence_root_name,
)
from onerc_healthcare.registration.setup import (
	UNIQUE_INDEX_COLUMNS,
	_index_columns,
	backfill_name_keys,
	seed_identifier_types,
)
from onerc_healthcare.registration.similarity import find_similar
from onerc_healthcare.registration.walk_in import SERIES_NAMESPACE, SETTINGS_DOCTYPE
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility, make_patient, make_user


def ident(identifier_type: str, value: str | None = None, **kw) -> dict:
	return {"identifier_type": identifier_type, "identifier_value": value or f"T-{random_string(10)}", **kw}


def identifiers_of(patient: str) -> dict[str, str]:
	doc = frappe.get_doc("Patient", patient)
	return {r.identifier_type: r.identifier_value for r in doc.get(PATIENT_IDENTIFIERS_FIELD)}


def walk_in(**kw):
	return make_patient(krcs_is_walk_in=1, **kw)


def facility_id_of(patient: str) -> str:
	return identifiers_of(patient)[IDENTIFIER_FACILITY]


class TestIdentifierRules(EMRTestCase):
	def test_identifier_types_seeded(self):
		"""FR-1 / C-6: UNHCR ID, PoL and Alien ID are primary; seeding is idempotent."""
		seed_identifier_types()
		seed_identifier_types()
		for itype in IDENTIFIER_TYPES:
			self.assertEqual(
				frappe.db.get_value(PATIENT_IDENTIFIER_TYPE_DOCTYPE, itype["name"], "is_primary"),
				itype["is_primary"],
			)

	def test_primary_identifier_required_unless_walk_in(self):
		"""FR-1, NFR-10: no identifier, or only a non-primary one, is refused with a clear message."""
		with self.assertRaisesRegex(frappe.ValidationError, "at least one primary identifier"):
			make_patient(**{PATIENT_IDENTIFIERS_FIELD: []})
		with self.assertRaisesRegex(frappe.ValidationError, "at least one primary identifier"):
			make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_NATIONAL)]})

		ok = make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_POL), ident(IDENTIFIER_NATIONAL)]})
		self.assertEqual(set(identifiers_of(ok.name)), {IDENTIFIER_POL, IDENTIFIER_NATIONAL})

		w = walk_in()
		self.assertIn(IDENTIFIER_FACILITY, identifiers_of(w.name))

	def test_normalisation(self):
		"""SDD 4.3: trim, upper case, strip punctuation and whitespace before storage."""
		self.assertEqual(normalise("  kak-12 345. "), "KAK12345")
		self.assertEqual(normalise("a/b_c\t9"), "ABC9")
		p = make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR, "  t-kak-12  345.x ")]})
		row = p.get(PATIENT_IDENTIFIERS_FIELD)[0]
		self.assertEqual(row.identifier_value, "T-KAK-12 345.X")
		self.assertEqual(row.normalised_value, "TKAK12345X")
		self.assertEqual(row.is_primary, 1)
		self.assertIn("TKAK12345X", p.get(SEARCH_FIELD))

	def test_duplicate_identifier_type_on_one_patient(self):
		"""NFR-10: the same identifier type twice on one patient is refused."""
		with self.assertRaisesRegex(frappe.ValidationError, "entered twice"):
			make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR), ident(IDENTIFIER_UNHCR)]})

	def test_value_pattern_and_verification(self):
		"""NFR-10: a configured value pattern is enforced; Verified needs a source and gets a date."""
		frappe.db.set_value(
			PATIENT_IDENTIFIER_TYPE_DOCTYPE,
			IDENTIFIER_POL,
			{"value_pattern": r"POL-\d+", "pattern_hint": "POL-1"},
		)
		with self.assertRaisesRegex(frappe.ValidationError, "expected format"):
			make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_POL, "XYZ")]})
		make_patient(
			**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_POL, f"pol-{random.randint(10**8, 10**9)}")]}
		)

		with self.assertRaisesRegex(frappe.ValidationError, "Verification Source"):
			make_patient(
				**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR, verification_status="Verified")]}
			)
		p = make_patient(
			**{
				PATIENT_IDENTIFIERS_FIELD: [
					ident(
						IDENTIFIER_UNHCR, verification_status="Verified", verification_source="Card sighted"
					)
				]
			}
		)
		self.assertEqual(getdate(p.get(PATIENT_IDENTIFIERS_FIELD)[0].verified_on), getdate())

	def test_population_group_defaults_from_identifier_type(self):
		"""FR-1: population group defaults from the (primary) identifier type; an explicit value wins."""
		refugee = make_patient(
			**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_NATIONAL), ident(IDENTIFIER_UNHCR)]}
		)
		self.assertEqual(refugee.get(POPULATION_GROUP_FIELD), "Refugee")
		other = make_patient(**{POPULATION_GROUP_FIELD: "Other"})
		self.assertEqual(other.get(POPULATION_GROUP_FIELD), "Other")


class TestDuplicatePrevention(EMRTestCase):
	def test_duplicate_blocked_by_validation(self):
		"""FR-2: the same identifier, spelt differently, is refused with the existing patient named."""
		value = f"T-KAK-{random_string(6)}"
		first = make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR, value)]})
		spelt_differently = " " + value.lower().replace("-", " ") + "."
		with self.assertRaises(DuplicateIdentifierError) as ctx:
			make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR, spelt_differently)]})
		self.assertIn(first.name, str(ctx.exception))
		# Same value under another identifier type is a different identifier.
		make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_POL, value)]})

	def test_duplicate_blocked_by_db_unique_index(self):
		"""FR-2 / SDD 4.3: with the validation bypassed (as in a race), the DB unique index still refuses."""
		self.assertEqual(_index_columns("tabPatient Identifier", UNIQUE_CONSTRAINT), UNIQUE_INDEX_COLUMNS)
		value = f"T-RACE-{random_string(6)}"
		make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR, value)]})
		with patch("onerc_healthcare.registration.identifiers.find_identifier_owner", return_value=None):
			with self.assertRaises(DuplicateIdentifierError):
				make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR, value)]})

		with self.assertRaises(Exception) as ctx:
			frappe.db.sql(
				"""insert into `tabPatient Identifier`
				(name, parent, parenttype, parentfield, identifier_type, identifier_value, normalised_value,
				unique_key)
				values (%s, 'T-NOBODY', 'Patient', %s, %s, %s, %s, %s)""",
				(
					random_string(10),
					PATIENT_IDENTIFIERS_FIELD,
					IDENTIFIER_UNHCR,
					value,
					normalise(value),
					normalise(value),
				),
			)
		self.assertTrue(frappe.db.is_unique_key_violation(ctx.exception))

	def test_similarity(self):
		"""FR-2 / SDD 4.3: name, sex, DOB and residence matches are found, swapped names too."""
		root = get_residence_root_name()
		last = f"Hassan{random_string(4).lower()}"
		p = make_patient(
			first_name="Amina", last_name=last, sex="Female", dob="1990-05-01", krcs_residence=root
		)

		def names(**kw):
			return [r["name"] for r in find_similar(**kw)]

		matches = find_similar("amina", last[:-1], "Female", "1990-05-01", root)
		match = next(r for r in matches if r["name"] == p.name)
		self.assertIn("same date of birth", match["reasons"])
		self.assertIn("same residence", match["reasons"])
		self.assertTrue(match["identifiers"])
		self.assertIn(p.name, names(first_name=last, last_name="Amina", sex="Female"))
		self.assertNotIn(p.name, names(first_name="Zebedee", last_name="Qwertyuiop", sex="Male"))
		self.assertNotIn(p.name, names(first_name="Amina", last_name=last, exclude=p.name))

		via_api = api.find_similar_patients(
			first_name="Amina", last_name=last, sex="Female", dob="1990-05-01"
		)
		self.assertEqual(via_api[0]["name"], p.name)

	def test_find_patient_by_identifier(self):
		"""SDD 5.1: registration starts with a search by identifier, in any spelling."""
		value = f"T-FIND-{random_string(6)}"
		p = make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR, value)]})
		found = api.find_patient_by_identifier(value.lower().replace("-", ""))
		self.assertEqual([f["patient"] for f in found], [p.name])
		self.assertEqual(found[0]["surviving_patient"], p.name)


class TestWalkIn(EMRTestCase):
	def test_walk_in_id_format_and_increment(self):
		"""FR-3: <FACILITY CODE>-<YYYY>-<#####>, incrementing per facility."""
		facility = get_facility(0)
		code = frappe.db.get_value("Healthcare Service Unit", facility, "krcs_facility_code")
		year = getdate().year
		first = walk_in(**{REGISTRATION_FACILITY_FIELD: facility})
		second = walk_in(**{REGISTRATION_FACILITY_FIELD: facility})
		pattern = rf"^{re.escape(code)}-{year}-(\d{{5}})$"
		n1 = int(re.match(pattern, facility_id_of(first.name)).group(1))
		n2 = int(re.match(pattern, facility_id_of(second.name)).group(1))
		self.assertEqual(n2, n1 + 1)

		other = get_facility(1)
		other_code = frappe.db.get_value("Healthcare Service Unit", other, "krcs_facility_code")
		self.assertTrue(
			facility_id_of(walk_in(**{REGISTRATION_FACILITY_FIELD: other}).name).startswith(other_code)
		)

		row = first.get(PATIENT_IDENTIFIERS_FIELD)[0]
		self.assertEqual(row.verification_status, "Verified")

	def test_walk_in_defaults_to_a_facility(self):
		"""FR-3: without an explicit facility the operation's first facility is used."""
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "default_registration_facility", None)
		p = walk_in()
		self.assertEqual(p.get(REGISTRATION_FACILITY_FIELD), get_facility(0))

	def test_walk_in_format_is_configurable(self):
		"""OI-13: the format lives in EMR Registration Settings and is validated."""
		settings = frappe.get_single(SETTINGS_DOCTYPE)
		settings.walk_in_id_format = "{facility_code}/{YY}/{###}"
		settings.save()
		facility = get_facility(0)
		code = frappe.db.get_value("Healthcare Service Unit", facility, "krcs_facility_code")
		value = facility_id_of(walk_in(**{REGISTRATION_FACILITY_FIELD: facility}).name)
		self.assertRegex(value, rf"^{re.escape(code)}/{getdate().strftime('%y')}/\d{{3}}$")

		for bad, message in (
			("{facility_code}-{YYYY}", "sequence placeholder"),
			("{site}-{#####}", "Unknown"),
		):
			settings = frappe.get_single(SETTINGS_DOCTYPE)
			settings.walk_in_id_format = bad
			with self.assertRaisesRegex(frappe.ValidationError, message):
				settings.save()

	def test_link_identifier(self):
		"""FR-3: Link Identifier adds a primary identifier later; a clash returns the holder for merging."""
		w = walk_in()
		value = f"T-LINK-{random_string(6)}"
		result = api.link_identifier(w.name, IDENTIFIER_UNHCR, value.lower())
		self.assertEqual(result["status"], "linked")
		ids = identifiers_of(w.name)
		self.assertEqual(ids[IDENTIFIER_UNHCR], value.upper())
		self.assertIn(IDENTIFIER_FACILITY, ids)

		self.assertEqual(api.link_identifier(w.name, IDENTIFIER_UNHCR, value)["status"], "exists")

		holder = make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_POL, "T-POL-CLASH-1")]})
		other_walk_in = walk_in()
		clash = api.link_identifier(other_walk_in.name, IDENTIFIER_POL, "t-pol clash 1")
		self.assertEqual(clash["status"], "conflict")
		self.assertEqual(clash["existing_patient"], holder.name)
		self.assertEqual(clash["merge_target"], holder.name)
		self.assertTrue(clash["can_merge"])
		self.assertNotIn(IDENTIFIER_POL, identifiers_of(other_walk_in.name))

		with self.assertRaises(frappe.ValidationError):
			api.link_identifier(w.name, IDENTIFIER_FACILITY, "X-1")


class TestMerge(EMRTestCase):
	def test_merge_keeps_source(self):
		"""SDD 4.3 / NFR-9: links move to the target; the source stays, disabled, with merged_into set."""
		target = make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR)]})
		source = walk_in(
			mobile="+254700000001",
			**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_NATIONAL, f"T-NAT-{random_string(6)}")]},
		)
		source_ids = identifiers_of(source.name)
		relative = make_patient(patient_relation=[{"patient": source.name, "relation": "Family"}])
		contact = frappe.db.get_value(
			"Dynamic Link",
			{"link_doctype": "Patient", "link_name": source.name, "parenttype": "Contact"},
			"parent",
		)

		result = api.merge_patients(source.name, target.name, "Same person registered twice")

		self.assertTrue(frappe.db.exists("Patient", source.name))
		src = frappe.db.get_value("Patient", source.name, ["status", MERGED_INTO_FIELD], as_dict=True)
		self.assertEqual(src.status, "Disabled")
		self.assertEqual(src[MERGED_INTO_FIELD], target.name)

		self.assertEqual(frappe.get_doc("Patient", relative.name).patient_relation[0].patient, target.name)
		self.assertIn("Patient Relation.patient", result["relinked"])
		if contact:
			self.assertTrue(
				frappe.db.exists(
					"Dynamic Link", {"parent": contact, "link_doctype": "Patient", "link_name": target.name}
				)
			)

		target_ids = identifiers_of(target.name)
		self.assertEqual(target_ids[IDENTIFIER_NATIONAL], source_ids[IDENTIFIER_NATIONAL])
		self.assertEqual(target_ids[IDENTIFIER_FACILITY], source_ids[IDENTIFIER_FACILITY])
		self.assertIn(
			normalise(source_ids[IDENTIFIER_NATIONAL]),
			frappe.db.get_value("Patient", target.name, SEARCH_FIELD),
		)

		for name in (source.name, target.name):
			self.assertTrue(
				frappe.db.exists(
					"Comment",
					{
						"reference_doctype": "Patient",
						"reference_name": name,
						"content": ["like", "%Same person%"],
					},
				)
			)

		# The merged record is frozen and cannot be merged again.
		merged = frappe.get_doc("Patient", source.name)
		merged.first_name = "Changed"
		with self.assertRaisesRegex(frappe.ValidationError, "merged into"):
			merged.save()
		with self.assertRaisesRegex(frappe.ValidationError, "already merged"):
			api.merge_patients(source.name, target.name, "again")
		# Similarity no longer offers the merged record.
		self.assertNotIn(source.name, [r["name"] for r in find_similar(source.first_name, source.last_name)])

	def test_merge_keeps_conflicting_identifier_on_source(self):
		"""SDD 4.3: an identifier type the target already holds stays on the (disabled) source."""
		target = make_patient()
		source = make_patient()
		source_unhcr = identifiers_of(source.name)[IDENTIFIER_UNHCR]
		result = api.merge_patients(source.name, target.name, "duplicate")
		self.assertEqual(identifiers_of(source.name)[IDENTIFIER_UNHCR], source_unhcr)
		self.assertTrue(result["identifiers_kept"])
		found = api.find_patient_by_identifier(source_unhcr)
		self.assertEqual(found[0]["surviving_patient"], target.name)

	def test_merge_roles(self):
		"""SDD 6.1: only Medical In-Charge, HIS Officer and System Manager may merge."""
		a, b = make_patient(), make_patient()
		frappe.set_user(make_user("reg-merge-rec", [ROLE_RECEPTIONIST]))
		with self.assertRaises(frappe.PermissionError):
			api.merge_patients(a.name, b.name, "x")
		frappe.set_user(make_user("reg-merge-his", [ROLE_HIS]))
		self.assertEqual(api.merge_patients(a.name, b.name, "x")["target"], b.name)

	def test_merge_validation(self):
		"""NFR-10: reason is required; a patient cannot be merged into itself."""
		a, b = make_patient(), make_patient()
		with self.assertRaisesRegex(frappe.ValidationError, "reason"):
			api.merge_patients(a.name, b.name, "  ")
		with self.assertRaisesRegex(frappe.ValidationError, "two different"):
			api.merge_patients(a.name, a.name, "x")
		with self.assertRaisesRegex(frappe.ValidationError, "only by the Merge"):
			make_patient(**{MERGED_INTO_FIELD: b.name})


class TestResidence(EMRTestCase):
	def test_residence_tree_seed(self):
		"""SDD 4.5: one idempotent root per operation; levels follow the operation's configuration."""
		root = get_residence_root_name()
		ensure_residence_root()
		ensure_residence_root()
		self.assertEqual(
			frappe.db.count("Residence Location", {"parent_residence_location": ["is", "not set"]}), 1
		)
		self.assertTrue(frappe.db.get_value("Residence Location", root, "is_group"))

		levels = get_residence_levels()
		options = frappe.get_meta("Residence Location").get_field("level").options.split("\n")
		for level in levels:
			self.assertIn(level, options)

		top = frappe.get_doc(
			{
				"doctype": "Residence Location",
				"location_name": "T-Top",
				"parent_residence_location": root,
				"level": levels[0],
				"is_group": 1,
			}
		).insert()
		self.assertEqual(top.name, "T-Top")
		with self.assertRaisesRegex(frappe.ValidationError, "Level is required"):
			frappe.get_doc(
				{
					"doctype": "Residence Location",
					"location_name": "T-NoLevel",
					"parent_residence_location": root,
				}
			).insert()
		if len(levels) > 1:
			child = frappe.get_doc(
				{
					"doctype": "Residence Location",
					"location_name": "T-Child",
					"parent_residence_location": top.name,
					"level": levels[1],
				}
			).insert()
			self.assertEqual(child.name, "T-Child - T-Top")
			with self.assertRaisesRegex(frappe.ValidationError, "cannot sit under"):
				frappe.get_doc(
					{
						"doctype": "Residence Location",
						"location_name": "T-Upside",
						"parent_residence_location": top.name,
						"level": levels[0],
					}
				).insert()


class TestRegistrationPermissions(EMRTestCase):
	def test_receptionist_can_create_triage_cannot(self):
		"""FR-1 / SDD 6.1: Nurse Receptionist registers patients; Triage Nurse cannot."""
		receptionist = make_user("reg-receptionist", [ROLE_RECEPTIONIST])
		triage = make_user("reg-triage", [ROLE_TRIAGE])
		if not frappe.has_permission("Patient", "create", user=receptionist):
			self.skipTest("Patient DocPerms for Nurse Receptionist not present (owned by A3)")

		def new_patient():
			return frappe.get_doc(
				{
					"doctype": "Patient",
					"first_name": f"T-{random_string(6)}",
					"sex": "Female",
					PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR)],
				}
			)

		frappe.set_user(receptionist)
		p = new_patient().insert()
		self.assertTrue(frappe.has_permission("Patient", "write", p.name, user=receptionist))
		frappe.set_user(triage)
		with self.assertRaises(frappe.PermissionError):
			new_patient().insert()
		with self.assertRaises(frappe.PermissionError):
			api.link_identifier(p.name, IDENTIFIER_POL, "T-POL-PERM")

	def test_registration_doctype_permissions(self):
		"""SDD 6.1: clinical roles read registration masters; EMR System Administrator has no access."""
		receptionist = make_user("reg-receptionist", [ROLE_RECEPTIONIST])
		sysadmin = make_user("reg-sysadmin", [ROLE_SYSADMIN])
		for doctype in (PATIENT_IDENTIFIER_TYPE_DOCTYPE, "Residence Location"):
			self.assertTrue(frappe.has_permission(doctype, "read", user=receptionist))
			self.assertFalse(frappe.has_permission(doctype, "create", user=receptionist))
			self.assertFalse(frappe.has_permission(doctype, "read", user=sysadmin))
		# M7: only System Manager may delete the two masters (Link checks protect used records);
		# nobody deletes settings.
		for doctype in (PATIENT_IDENTIFIER_TYPE_DOCTYPE, "Residence Location"):
			perms = frappe.get_meta(doctype).permissions
			self.assertEqual([p.role for p in perms if p.get("delete")], ["System Manager"], doctype)
		perms = frappe.get_meta(SETTINGS_DOCTYPE).permissions
		self.assertFalse([p.role for p in perms if p.get("delete")])


def walk_in_series_current(facility: str) -> int:
	code = frappe.db.get_value("Healthcare Service Unit", facility, "krcs_facility_code")
	key = f"{SERIES_NAMESPACE}{code}-{getdate().year}-#"
	rows = frappe.db.sql("select `current` from `tabSeries` where name = %s", (key,))
	return frappe.utils.cint(rows[0][0]) if rows else 0


def insert_identifier_row(parent: str, identifier_type: str, value: str, unique: bool = True) -> None:
	"""Raw row, as left by an import or direct DB work (bypasses validation)."""
	frappe.db.sql(
		"""insert into `tabPatient Identifier`
		(name, parent, parenttype, parentfield, identifier_type, identifier_value, normalised_value, unique_key)
		values (%s, %s, 'Patient', %s, %s, %s, %s, %s)""",
		(
			random_string(10),
			parent,
			PATIENT_IDENTIFIERS_FIELD,
			identifier_type,
			value,
			normalise(value),
			normalise(value) if unique else None,
		),
	)


class TestReviewFixes(EMRTestCase):
	def test_facility_identifier_cannot_be_typed_edited_or_removed(self):
		"""H1 / FR-3: Facility-generated identifiers are system-assigned only."""
		with self.assertRaises(FacilityIdentifierError):
			make_patient(
				**{
					PATIENT_IDENTIFIERS_FIELD: [
						ident(IDENTIFIER_UNHCR),
						ident(IDENTIFIER_FACILITY, "NTK-2026-00042"),
					]
				}
			)
		with self.assertRaises(FacilityIdentifierError):
			make_patient(
				krcs_is_walk_in=1, **{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_FACILITY, "X-1")]}
			)

		w = walk_in()
		row = next(r for r in w.get(PATIENT_IDENTIFIERS_FIELD) if r.identifier_type == IDENTIFIER_FACILITY)
		row.identifier_value = "NTK-2099-99999"
		with self.assertRaises(FacilityIdentifierError):
			w.save()

		w = frappe.get_doc("Patient", w.name)
		w.set(PATIENT_IDENTIFIERS_FIELD, [ident(IDENTIFIER_UNHCR)])
		with self.assertRaises(FacilityIdentifierError):
			w.save()

		# Other edits of a walk-in still save.
		w = frappe.get_doc("Patient", w.name)
		w.append(PATIENT_IDENTIFIERS_FIELD, ident(IDENTIFIER_UNHCR))
		w.save()

	def test_generator_skips_values_already_held(self):
		"""H1: a value already present (import, DB work) is skipped instead of blocking walk-ins."""
		facility = get_facility(0)
		first = facility_id_of(walk_in(**{REGISTRATION_FACILITY_FIELD: facility}).name)
		prefix, number = first.rsplit("-", 1)
		taken = f"{prefix}-{int(number) + 1:05d}"
		insert_identifier_row("T-IMPORTED", IDENTIFIER_FACILITY, taken)
		second = facility_id_of(walk_in(**{REGISTRATION_FACILITY_FIELD: facility}).name)
		self.assertEqual(second, f"{prefix}-{int(number) + 2:05d}")

	def test_merging_a_walk_in_mints_no_new_identifier(self):
		"""M4: the disabled walk-in source gets no new facility identifier and uses no sequence number."""
		facility = get_facility(0)
		target = make_patient()
		source = walk_in(**{REGISTRATION_FACILITY_FIELD: facility})
		source_facility_id = facility_id_of(source.name)
		before = walk_in_series_current(facility)
		api.merge_patients(source.name, target.name, "walk-in was already registered")
		self.assertEqual(walk_in_series_current(facility), before)
		self.assertNotIn(IDENTIFIER_FACILITY, identifiers_of(source.name))
		self.assertEqual(identifiers_of(target.name)[IDENTIFIER_FACILITY], source_facility_id)

	def test_merge_log_lists_moved_records(self):
		"""M5 / NFR-9: the merged record keeps a structured list of every moved record."""
		target = make_patient()
		source = make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_POL)]})
		relative = make_patient(patient_relation=[{"patient": source.name, "relation": "Family"}])
		relation_row = frappe.get_doc("Patient", relative.name).patient_relation[0].name
		source_row = source.get(PATIENT_IDENTIFIERS_FIELD)[0].name

		api.merge_patients(source.name, target.name, "duplicate")
		log = json.loads(frappe.db.get_value("Patient", source.name, MERGE_LOG_FIELD))
		self.assertEqual(log["merged_into"], target.name)
		self.assertIn(
			{
				"doctype": "Patient Relation",
				"name": relation_row,
				"field": "patient",
				"parent": relative.name,
				"parenttype": "Patient",
			},
			log["relinked"],
		)
		self.assertEqual([e["name"] for e in log["identifiers_moved"]], [source_row])

	def test_merge_refreshes_copied_patient_identifier_on_visits(self):
		"""L6: visits moved to the target show the target's name and identifier."""
		if not frappe.db.exists("DocType", "Patient Visit") or not frappe.get_meta("Patient Visit").has_field(
			"patient_identifier"
		):
			self.skipTest("Patient Visit (A2) not installed")
		target = make_patient()
		source = make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_POL)]})
		visit = frappe.get_doc(
			{"doctype": "Patient Visit", "patient": source.name, "facility": get_facility(0)}
		)
		visit.insert(ignore_permissions=True)
		self.assertIn(IDENTIFIER_POL, visit.patient_identifier)
		# Phase 2: a patient with an open visit cannot be merged; close it first.
		with self.assertRaises(frappe.ValidationError):
			api.merge_patients(source.name, target.name, "duplicate")
		from onerc_healthcare.visit.engine import transition

		transition(
			visit.name, "close_visit", closure_reason="Registered in error", comment="Duplicate record"
		)
		api.merge_patients(source.name, target.name, "duplicate")
		row = frappe.db.get_value(
			"Patient Visit", visit.name, ["patient", "patient_name", "patient_identifier"], as_dict=True
		)
		self.assertEqual(row.patient, target.name)
		self.assertEqual(row.patient_name, target.patient_name)
		self.assertEqual(
			row.patient_identifier, f"{IDENTIFIER_UNHCR}: {identifiers_of(target.name)[IDENTIFIER_UNHCR]}"
		)

	def test_old_exact_duplicate_found_past_candidate_limit(self):
		"""M6: an exact (or same-DOB-and-sex) match is never dropped by the candidate limit."""
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "similarity_candidate_limit", 3)
		surname = f"Odhiambo{random_string(5).lower()}"
		old = make_patient(first_name="Amina", last_name=surname, sex="Female", dob="1988-02-03")
		for i in range(6):
			make_patient(
				first_name=f"T-{random_string(6)}", last_name=surname, sex="Male", dob=f"200{i}-01-01"
			)

		def found(*args):
			return old.name in [r["name"] for r in find_similar(*args)]

		self.assertTrue(found("Amina", surname, "Female", "1988-02-03"))
		# First name completely different: still found through same surname + DOB + sex.
		self.assertTrue(found("Zainab", surname, "Female", "1988-02-03"))
		# Weak match only (no DOB): the candidate limit applies, newest first.
		self.assertFalse(found("Zainab", surname))

	def test_name_keys_stored_and_backfilled(self):
		"""M6: indexed phonetic keys are set on save and backfilled by install."""
		p = make_patient(first_name="Mohamed", last_name="Abdi")
		self.assertEqual(p.get(FIRST_NAME_KEY_FIELD), name_key("Mohamed"))
		frappe.db.set_value("Patient", p.name, {FIRST_NAME_KEY_FIELD: None, LAST_NAME_KEY_FIELD: None})
		backfill_name_keys()
		self.assertEqual(frappe.db.get_value("Patient", p.name, LAST_NAME_KEY_FIELD), name_key("Abdi"))

	def test_shared_ration_card(self):
		"""M9: household ration cards may be shared; unique identifiers are still blocked."""
		self.assertFalse(frappe.db.get_value(PATIENT_IDENTIFIER_TYPE_DOCTYPE, IDENTIFIER_RATION, "is_unique"))
		card = f"T-RC-{random_string(6)}"
		rows = [ident(IDENTIFIER_UNHCR), ident(IDENTIFIER_RATION, card)]
		a = make_patient(**{PATIENT_IDENTIFIERS_FIELD: rows})
		b = make_patient(
			**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR), ident(IDENTIFIER_RATION, card)]}
		)
		self.assertIsNone(b.get(PATIENT_IDENTIFIERS_FIELD)[1].unique_key)
		holders = {f["patient"] for f in api.find_patient_by_identifier(card, IDENTIFIER_RATION)}
		self.assertEqual(holders, {a.name, b.name})
		unhcr = identifiers_of(a.name)[IDENTIFIER_UNHCR]
		with self.assertRaises(DuplicateIdentifierError):
			make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_UNHCR, unhcr)]})
		# A shared type cannot be made unique while values are shared.
		itype = frappe.get_doc(PATIENT_IDENTIFIER_TYPE_DOCTYPE, IDENTIFIER_RATION)
		itype.is_unique = 1
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be made unique"):
			itype.save()

	def test_strong_duplicate_needs_reason(self):
		"""L2 / FR-2: same name, DOB and sex as an existing patient requires a recorded reason."""
		first = f"Halima{random_string(4).lower()}"
		existing = make_patient(first_name=first, last_name="Ali", sex="Female", dob="1995-07-07")
		with self.assertRaises(PossibleDuplicatePatientError) as ctx:
			make_patient(first_name=f" {first.upper()} ", last_name="ali", sex="Female", dob="1995-07-07")
		self.assertIn(existing.name, str(ctx.exception))
		make_patient(first_name=first, last_name="Ali", sex="Male", dob="1995-07-07")
		twin = make_patient(
			first_name=first,
			last_name="Ali",
			sex="Female",
			dob="1995-07-07",
			**{OVERRIDE_REASON_FIELD: "Twin sister, different UNHCR ID"},
		)
		self.assertTrue(
			frappe.db.exists("Comment", {"reference_name": twin.name, "content": ["like", "%Twin sister%"]})
		)


class TestSearchFirst(EMRTestCase):
	def test_registration_defaults_include_search_first_flag(self):
		"""REG-24 / SDD 5.1: the search-first dialog is on by default and can be switched off."""
		frappe.db.sql(
			"delete from `tabSingles` where doctype = %s and field = 'search_first_on_new_patient'",
			(SETTINGS_DOCTYPE,),
		)
		defaults = api.get_registration_defaults()
		self.assertIs(defaults["search_first_on_new_patient"], True)
		self.assertEqual(defaults["default_identifier_type"], IDENTIFIER_UNHCR)
		self.assertIn("registration_facility", defaults)

		frappe.db.set_single_value(SETTINGS_DOCTYPE, "search_first_on_new_patient", 0)
		self.assertIs(api.get_registration_defaults()["search_first_on_new_patient"], False)

	def test_registration_defaults_need_create_permission(self):
		"""SDD 6.1: only users who may register patients get the defaults."""
		frappe.set_user(make_user("reg-search-sysadmin", [ROLE_SYSADMIN]))
		with self.assertRaises(frappe.PermissionError):
			api.get_registration_defaults()

	def test_search_normalises_and_returns_surviving_record(self):
		"""REG-24 / FR-2: any spelling finds the patient; a merged holder resolves to the survivor."""
		value = f"T-KAK/{random_string(5)}"
		target = make_patient(sex="Male", dob="1980-01-01")
		source = make_patient(**{PATIENT_IDENTIFIERS_FIELD: [ident(IDENTIFIER_POL, value)]})
		found = api.find_patient_by_identifier(f"  {value.lower().replace('/', ' .')} ", IDENTIFIER_POL)
		self.assertEqual([f["patient"] for f in found], [source.name])
		self.assertEqual(found[0]["surviving_patient"], source.name)

		# Merge keeps PoL on the target (target has no PoL), so look it up there.
		api.merge_patients(source.name, target.name, "duplicate")
		found = api.find_patient_by_identifier(value.lower(), IDENTIFIER_POL)
		self.assertEqual(found[0]["surviving_patient"], target.name)
		self.assertEqual(found[0]["surviving_patient_name"], target.patient_name)
		self.assertEqual(found[0]["surviving_sex"], "Male")
		self.assertEqual(str(found[0]["surviving_dob"]), "1980-01-01")
		self.assertEqual(api.find_patient_by_identifier("T-NOT-THERE", IDENTIFIER_POL), [])
