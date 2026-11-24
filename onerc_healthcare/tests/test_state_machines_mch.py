# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""S6 state-machine audit of the MCH build (W0 `8b78760` + MCH-A / MCH-B + integration). Never commit.

Every MCH lifecycle is driven through its own API and through the desk paths a user can reach (`insert`,
`save`, `submit`, `cancel` as the user), by every relevant actor, and compared with an independent oracle
written from docs/MCH_CONTRACT.md (§1 roles and DocPerms, §3 shared fields, §4 lifecycle, §5 ANC, §6 PNC,
§10 entry points). The contract tables are copied below as literals; meta-tests compare them with the code's
introspectable tables. Behaviour that contradicts the documented rules is kept as an
``@unittest.expectedFailure`` test that asserts the *correct* behaviour (comment ``DEFECT-MCH-n``); the
matrices skip exactly the combinations such a test covers, so a fix never breaks a matrix.

Actors: Clinical Officer, Midwife, Medical In-Charge, Triage Nurse, Mental Health Officer, HIS Officer, Nurse
Receptionist, Pharmacist, Laboratory Technician, Head of Operations, Outreach Team Member, Administrator.

Machines:

1. MCH records (ANC Contact, PNC Visit, Immunisation, Growth Monitoring): create / edit draft / submit / edit
   after submit / write the addendum or archive flag directly / cancel / add_addendum (draft and submitted) /
   archive (draft and submitted) x actor x visit state (open, closed).
2. ANC Pregnancy: book x prior pregnancy (none, open, closed, archived) x actor; close_pregnancy x state x
   reason x actor; archive x state x actor; edits of an open / closed / archived pregnancy; the Delivery Record
   submit closing it (idempotent, a later booking stays open); contacts after the close.
3. One per visit: a second record on the same visit (draft or submitted first) is refused; archiving the first
   frees the visit; a ward PNC is one per (admission, patient).
4. Offered == works: every action `mch.forms.visit_actions` offers succeeds for that user, including the
   browser's link validation of the prefilled defaults; a closed visit offers nothing.
"""

from __future__ import annotations

import os
import re
import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.desk.search import search_widget
from frappe.utils import add_days, cint, getdate, today

from onerc_healthcare import constants as c
from onerc_healthcare.mch import anc, common, forms, growth, immunisation
from onerc_healthcare.mch.common import MCHRecordError
from onerc_healthcare.privacy import access_log
from onerc_healthcare.privacy.lifecycle import LockedFieldError
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import routed_visit, start
from onerc_healthcare.tests.factories import get_facility, make_patient, make_user
from onerc_healthcare.tests.mch_factories import (
	live_baby,
	make_child,
	make_delivery,
	make_mch_visit,
	make_mother,
	make_next_visit,
	make_pregnancy,
	mch_users,
)
from onerc_healthcare.tests.sm_helpers import ADMIN, as_user, savepoint
from onerc_healthcare.tests.visit_factories import make_visit_users

PREG = c.ANC_PREGNANCY_DOCTYPE
ANC = c.ANC_CONTACT_DOCTYPE
PNC = c.PNC_VISIT_DOCTYPE
IMM = c.IMMUNISATION_DOCTYPE
GM = c.GROWTH_MONITORING_DOCTYPE
KEPI = c.KEPI_SCHEDULE_DOCTYPE
RECORDS = (ANC, PNC, IMM, GM)
OPEN, CLOSED = "open", "closed"
VISIT_STATES = (OPEN, CLOSED)
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# =====================================================================================================
# Oracles (literal copies of the contract)
# =====================================================================================================

# Actor key -> role (one user each holding exactly that role).
ROLE_OF = {
	"clinician": "Clinical Officer",
	"midwife": "Midwife",
	"mic": "Medical In-Charge",
	"triage": "Triage Nurse",
	"mho": "Mental Health Officer",
	"his": "HIS Officer",
	"reception": "Nurse Receptionist",
	"pharmacist": "Pharmacist",
	"lab": "Laboratory Technician",
	"head_ops": "Head of Operations",
	"outreach": "Outreach Team Member",
	ADMIN: ADMIN,
}
ACTORS = tuple(ROLE_OF)
# §1: MCH_ROLES record every MCH record; CHILD_MCH_ROLES adds the Triage Nurse for Immunisation and Growth
# Monitoring; MCH_READ_ROLES read. Head of Operations, Receptionist, Lab, Pharmacist, Outreach: no access.
MCH = ("Clinical Officer", "Midwife", "Medical In-Charge")
CHILD = (*MCH, "Triage Nurse")
READ_ONLY = ("Mental Health Officer", "HIS Officer")
NO_ACCESS = (
	"Head of Operations",
	"Nurse Receptionist",
	"Laboratory Technician",
	"Pharmacist",
	"Outreach Team Member",
)
# §1 DocPerm table (no delete, cancel or amend for anyone, NFR-9).
R = frozenset({"read"})
RWC = frozenset({"read", "write", "create"})
RWCS = RWC | {"submit"}
EXPECTED_DOCPERMS: dict[str, dict[str, frozenset]] = {
	PREG: {**{r: RWC for r in MCH}, **{r: R for r in READ_ONLY}},
	ANC: {**{r: RWCS for r in MCH}, **{r: R for r in READ_ONLY}},
	PNC: {**{r: RWCS for r in MCH}, **{r: R for r in READ_ONLY}},
	IMM: {**{r: RWCS for r in CHILD}, **{r: R for r in READ_ONLY}},
	GM: {**{r: RWCS for r in CHILD}, **{r: R for r in READ_ONLY}},
	KEPI: {
		"Clinical Officer": R,
		"Midwife": R,
		"Triage Nurse": R,
		"Mental Health Officer": R,
		"HIS Officer": RWC,
		"Medical In-Charge": RWC,  # "The Medical In-Charge also writes KEPI rows."
		"EMR System Administrator": RWC,
	},
}
PTYPES = ("read", "write", "create", "submit", "cancel", "amend", "delete")
# §4: who adds an addendum (MCH_ROLES, or CHILD_MCH_ROLES for Immunisation and Growth Monitoring) and who
# archives (Medical In-Charge only).
RECORD_ROLES = {ANC: MCH, PNC: MCH, IMM: CHILD, GM: CHILD}
ARCHIVERS = ("Medical In-Charge",)
# §3 shared fields.
SET_ONCE = ("patient", "visit")
SERVER_SET = ("encounter", "facility", "recorded_by")
# §5: close_pregnancy reasons (a delivery closes it by itself).
USER_CLOSE_REASONS = ("Pregnancy ended", "Transferred out")
DELIVERED = "Delivered"

# Record actions of machine 1.
RECORD_ACTIONS = (
	"create",
	"edit_draft",
	"submit",
	"edit_after_submit",
	"write_addendum_field",
	"write_archived_field",
	"cancel",
	"addendum",
	"addendum_on_draft",
	"archive",
	"archive_draft",
)
# DEFECT combinations the matrices skip (covered by the expected-failure tests). The DEFECT-MCH-1..3 tests
# forge field values the matrix never sends, so no matrix combination is skipped.
DEFECT_RECORD_CASES: set[tuple[str, str, str, str]] = set()

# A plain, not allow-on-submit field per record type.
EDIT_FIELD = {ANC: "plan", PNC: "notes", IMM: "notes", GM: "notes"}


def has(actor: str, roles) -> bool:
	return actor == ADMIN or ROLE_OF[actor] in roles


def expect_record(actor: str, doctype: str, action: str, state: str) -> bool:
	"""True when `action` by `actor` on a record of `doctype` (its visit `state`) must work (§1, §3, §4)."""
	writer = has(actor, RECORD_ROLES[doctype])
	if action == "create":
		return writer and state == OPEN  # "An open visit of the same patient when the record is inserted"
	if action in ("edit_draft", "submit"):
		return writer  # "a draft may be submitted after the visit closes"
	if action in ("edit_after_submit", "write_addendum_field", "write_archived_field", "cancel"):
		return False  # NFR-9; the addendum grows only through add_addendum, the flag only through archive
	if action == "addendum":
		return writer
	if action == "addendum_on_draft":
		return False  # "Refuses an addendum on a draft"
	if action in ("archive", "archive_draft"):
		return has(actor, ARCHIVERS)
	raise ValueError(action)


def _try(fn, *args, **kwargs):
	try:
		return fn(*args, **kwargs), None
	except Exception as e:  # compared with the oracle by the caller
		return None, e


REFUSALS = (frappe.PermissionError, frappe.ValidationError)


def close_visit(name: str) -> None:
	frappe.db.set_value(c.VISIT_DOCTYPE, name, "status", c.VISIT_STATUS_DISCHARGED, update_modified=False)


class MCHCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = {
			**mch_users(),
			"pharmacist": make_user("s6-pharmacist", [c.ROLE_PHARMACIST]),
			"lab": make_user("s6-lab", [c.ROLE_LAB]),
			"head_ops": make_user("s6-head-ops", [c.ROLE_HEAD_OPS]),
			"outreach": make_user("s6-outreach", [c.ROLE_OUTREACH]),
		}
		cls.consultant = make_visit_users()["clinician"]
		frappe.set_user(ADMIN)

	def setUp(self):
		super().setUp()
		immunisation.seed_schedule()

	def u(self, actor: str) -> str:
		return ADMIN if actor == ADMIN else self.users[actor]

	def act_as(self, actor: str, fn, *args, **kwargs):
		with as_user(self.u(actor)):
			return _try(fn, *args, **kwargs)

	# -- record builders ------------------------------------------------------------------------------
	def record_values(self, doctype: str, patient: str | None = None, visit: str | None = None) -> dict:
		"""Valid insert values of a new `doctype` record on a new open visit (a new patient unless given)."""
		if doctype == ANC:
			patient = patient or make_mother().name
			if not anc.open_pregnancy(patient):
				make_pregnancy(patient)
			values = {"contact_date": today(), "bp_systolic": 110, "bp_diastolic": 70}
		elif doctype == PNC:
			patient = patient or make_mother().name
			values = {"subject": c.PNC_SUBJECT_MOTHER, "pnc_date": today(), "timing": "Within 48 hours"}
		elif doctype == IMM:
			patient = patient or make_child(age_days=120).name
			values = {"given_on": today(), "doses": [{"vaccine": "BCG", "status": c.DOSE_GIVEN}]}
		else:
			patient = patient or make_child(age_days=120).name
			values = {"measured_on": today(), "weight_kg": 6.1}
		visit = visit or make_mch_visit(patient).name
		return {"doctype": doctype, "patient": patient, "visit": visit, **values}

	def draft(self, doctype: str, **kw):
		return frappe.get_doc(self.record_values(doctype, **kw)).insert(ignore_permissions=True)

	def submitted(self, doctype: str, **kw):
		doc = self.draft(doctype, **kw)
		doc.submit()
		return doc

	def row(self, doctype: str, name: str, *fields):
		return frappe.db.get_value(doctype, name, list(fields), as_dict=True)


# =====================================================================================================
# 0. Meta: the oracles agree with the code's tables
# =====================================================================================================
class TestContractTables(MCHCase):
	def test_role_constants_match_contract(self):
		self.assertEqual(tuple(c.MCH_ROLES), MCH)
		self.assertEqual(tuple(c.CHILD_MCH_ROLES), CHILD)
		self.assertEqual(tuple(c.MCH_READ_ROLES), READ_ONLY)
		self.assertEqual(tuple(c.MCH_RECORD_DOCTYPES), RECORDS)
		self.assertEqual(tuple(common.ARCHIVE_ROLES), ARCHIVERS)
		self.assertEqual(tuple(anc.USER_CLOSE_REASONS), USER_CLOSE_REASONS)
		self.assertEqual(c.PREGNANCY_DELIVERED, DELIVERED)

	def test_docperms_match_contract(self):
		for doctype, expected in EXPECTED_DOCPERMS.items():
			actual: dict[str, set] = {}
			for p in frappe.get_meta(doctype).permissions:
				if cint(p.permlevel):
					continue
				actual.setdefault(p.role, set()).update(t for t in PTYPES if cint(p.get(t)))
			actual = {role: frozenset(t) for role, t in actual.items() if t}
			self.assertEqual(actual, expected, doctype)
			for role in NO_ACCESS:
				self.assertNotIn(role, actual, f"{role} has access to {doctype}")

	def test_shared_field_properties(self):
		for doctype in RECORDS:
			meta = frappe.get_meta(doctype)
			self.assertTrue(cint(meta.is_submittable), doctype)
			for f in SET_ONCE:
				self.assertTrue(cint(meta.get_field(f).set_only_once), f"{doctype}.{f} set once")
			for f in SERVER_SET:
				self.assertTrue(cint(meta.get_field(f).read_only), f"{doctype}.{f} read-only")
				self.assertFalse(meta.get_field(f).fetch_from, f"{doctype}.{f} not fetched in the browser")
			addendum = meta.get_field("addendum")
			self.assertTrue(cint(addendum.read_only) and cint(addendum.allow_on_submit), doctype)
			self.assertTrue(meta.has_field("krcs_archived"), doctype)
		self.assertFalse(cint(frappe.get_meta(PREG).is_submittable))

	def test_links_the_triage_nurse_cannot_read_are_server_set(self):
		"""§1: the Triage Nurse cannot read Patient Encounter, Maternity Admission or Delivery Record; such links
		on the forms she writes are read-only and never fetched in the browser."""
		triage = self.users["triage"]
		unreadable = [
			dt
			for dt in (c.ENCOUNTER_DOCTYPE, c.MATERNITY_ADMISSION_DOCTYPE, c.DELIVERY_RECORD_DOCTYPE)
			if not frappe.has_permission(dt, "read", user=triage)
		]
		self.assertEqual(len(unreadable), 3)
		for doctype in (IMM, GM):
			for df in frappe.get_meta(doctype).fields:
				if df.fieldtype == "Link" and df.options in unreadable:
					self.assertTrue(cint(df.read_only), f"{doctype}.{df.fieldname}")
					self.assertFalse(df.fetch_from, f"{doctype}.{df.fieldname}")


# =====================================================================================================
# 1. MCH record lifecycle matrix
# =====================================================================================================
class TestRecordMatrix(MCHCase):
	def run_case(self, actor: str, doctype: str, action: str, state: str) -> tuple[bool, str]:
		"""Run one combination in its own savepoint. Returns (worked, detail)."""
		with savepoint("s6_record"):
			values = self.record_values(doctype)
			visit = values["visit"]
			if action == "create":
				if state == CLOSED:
					close_visit(visit)
				_out, err = self.act_as(actor, lambda: frappe.get_doc(values).insert())
				n = frappe.db.count(doctype, {"visit": visit})
				return self._verdict(err, n == 1, n == 0)
			doc = frappe.get_doc(values).insert(ignore_permissions=True)
			name = doc.name
			if action not in ("edit_draft", "submit", "addendum_on_draft", "archive_draft"):
				doc.submit()
			if state == CLOSED:
				close_visit(visit)
			field = EDIT_FIELD[doctype]
			before = self.row(doctype, name, "docstatus", field, "addendum", "krcs_archived")

			def load():
				return frappe.get_doc(doctype, name)

			if action in ("edit_draft", "edit_after_submit"):

				def edit():
					d = load()
					d.set(field, "T- s6 edit")
					d.save()

				_out, err = self.act_as(actor, edit)
				after = self.row(doctype, name, field)
				return self._verdict(err, after[field] == "T- s6 edit", after[field] == before[field])
			if action == "write_addendum_field":

				def write_addendum():
					d = load()
					d.addendum = (d.addendum or "") + "T- forged"
					d.save()

				_out, err = self.act_as(actor, write_addendum)
				after = self.row(doctype, name, "addendum")
				return self._verdict(
					err, after.addendum != before.addendum, after.addendum == before.addendum
				)
			if action == "write_archived_field":

				def write_archived():
					d = load()
					d.krcs_archived = 1
					d.save()

				_out, err = self.act_as(actor, write_archived)
				after = self.row(doctype, name, "krcs_archived")
				return self._verdict(err, cint(after.krcs_archived) == 1, cint(after.krcs_archived) == 0)
			if action == "submit":
				_out, err = self.act_as(actor, lambda: load().submit())
				after = self.row(doctype, name, "docstatus")
				return self._verdict(err, after.docstatus == 1, after.docstatus == 0)
			if action == "cancel":
				_out, err = self.act_as(actor, lambda: load().cancel())
				after = self.row(doctype, name, "docstatus")
				return self._verdict(err, after.docstatus == 2, after.docstatus == 1)
			if action in ("addendum", "addendum_on_draft"):
				_out, err = self.act_as(actor, common.add_addendum, doctype, name, "T- s6 correction")
				after = self.row(doctype, name, "addendum")
				return self._verdict(
					err, "T- s6 correction" in (after.addendum or ""), after.addendum == before.addendum
				)
			if action in ("archive", "archive_draft"):
				_out, err = self.act_as(actor, common.archive, doctype, name, "T- s6 wrong patient")
				after = self.row(doctype, name, "krcs_archived", "docstatus")
				return self._verdict(
					err,
					cint(after.krcs_archived) == 1 and after.docstatus == before.docstatus,
					cint(after.krcs_archived) == 0,
				)
			raise ValueError(action)

	@staticmethod
	def _verdict(err, done: bool, unchanged: bool) -> tuple[bool, str]:
		if err is None:
			return done, "" if done else "no error but nothing changed"
		if not isinstance(err, REFUSALS):
			return False, f"unexpected error {err!r}"
		return False, "" if unchanged else f"refused ({err!r}) but the record changed"

	def check_doctype(self, doctype: str):
		problems = []
		for actor in ACTORS:
			for action in RECORD_ACTIONS:
				for state in VISIT_STATES:
					if (actor, doctype, action, state) in DEFECT_RECORD_CASES:
						continue
					expected = expect_record(actor, doctype, action, state)
					worked, detail = self.run_case(actor, doctype, action, state)
					if worked != expected or detail:
						problems.append(
							f"{actor} {action} ({state} visit): expected {expected}, got {worked} {detail}"
						)
		self.assertEqual(problems, [], f"{doctype}:\n" + "\n".join(problems))

	def test_anc_contact_matrix(self):
		self.check_doctype(ANC)

	def test_pnc_visit_matrix(self):
		self.check_doctype(PNC)

	def test_immunisation_matrix(self):
		self.check_doctype(IMM)

	def test_growth_monitoring_matrix(self):
		self.check_doctype(GM)

	def test_archived_draft_is_frozen(self):
		"""§4: an archived draft cannot be edited or submitted, by anyone."""
		for doctype in RECORDS:
			with savepoint("s6_frozen"):
				doc = self.draft(doctype)
				common.archive(doctype, doc.name, "T- s6 wrong visit")
				for actor in ("mic", ADMIN):

					def edit(name=doc.name, doctype=doctype):
						d = frappe.get_doc(doctype, name)
						d.set(EDIT_FIELD[doctype], "T- after archive")
						d.save()

					_out, err = self.act_as(actor, edit)
					self.assertIsInstance(err, REFUSALS, f"{doctype} edit by {actor}")
					_out, err = self.act_as(
						actor, lambda n=doc.name, dt=doctype: frappe.get_doc(dt, n).submit()
					)
					self.assertIsInstance(err, REFUSALS, f"{doctype} submit by {actor}")
				self.assertEqual(self.row(doctype, doc.name, "docstatus").docstatus, 0)

	def test_archive_is_one_way(self):
		"""§4: `krcs_archived` changes only through `archive`; nobody clears it (draft or submitted)."""
		for doctype in RECORDS:
			for submit in (False, True):
				with savepoint("s6_unarchive"):
					doc = self.submitted(doctype) if submit else self.draft(doctype)
					common.archive(doctype, doc.name, "T- s6 wrong visit")
					_out, err = self.act_as("mic", common.archive, doctype, doc.name, "again")
					self.assertIsInstance(err, MCHRecordError)
					for actor in ("mic", ADMIN):

						def unarchive(name=doc.name, doctype=doctype):
							d = frappe.get_doc(doctype, name)
							d.krcs_archived = 0
							d.save()

						_out, err = self.act_as(actor, unarchive)
						self.assertIsInstance(err, REFUSALS, f"{doctype} unarchive by {actor}")
					self.assertEqual(cint(self.row(doctype, doc.name, "krcs_archived").krcs_archived), 1)

	def test_addendum_is_append_only_and_signed(self):
		for doctype in RECORDS:
			with savepoint("s6_addendum"):
				doc = self.submitted(doctype)
				with as_user(self.users["mic"]):
					common.add_addendum(doctype, doc.name, "first")
					common.add_addendum(doctype, doc.name, "second")
				text = self.row(doctype, doc.name, "addendum").addendum
				self.assertLess(text.index("first"), text.index("second"))
				self.assertIn("T-mch-mic", text)
				for value in ("", "rewritten", text.replace("first", "1st")):
					d = frappe.get_doc(doctype, doc.name)
					d.addendum = value
					_out, err = _try(d.save)
					self.assertIsInstance(err, REFUSALS, f"{doctype} addendum -> {value!r}")
				self.assertEqual(self.row(doctype, doc.name, "addendum").addendum, text)

	# DEFECT-MCH-2: an addendum typed into a draft survives submit (guard_draft only checks docstatus 0)
	def test_defect_mch_2_addendum_written_on_draft_survives_submit(self):
		"""DEFECT-MCH-2: contract §3/§4 - the addendum is read-only and "grows only through
		`mch.common.add_addendum`"; `guard_draft` "refuses an addendum on a draft". But `guard_draft` tests
		``doc.docstatus == 0`` and `validate` runs on submit with ``docstatus == 1``: a client that sets
		``addendum`` on the draft and submits it in the same request (desk savedocs "Submit", or a REST insert
		with ``docstatus: 1``) stores an unsigned, undated "correction" that looks like an addendum."""
		forged = []
		for doctype in RECORDS:
			with savepoint("s6_d2"):
				doc = self.draft(doctype)
				with as_user(self.users["midwife"]):
					d = frappe.get_doc(doctype, doc.name)
					d.addendum = "T- forged correction"
					_try(d.submit)
				if self.row(doctype, doc.name, "addendum").addendum:
					forged.append(f"{doctype} (submit a draft)")
			with savepoint("s6_d2b"):
				values = {**self.record_values(doctype), "docstatus": 1, "addendum": "T- forged"}
				with as_user(self.users["midwife"]):
					_out, err = _try(lambda v=values: frappe.get_doc(v).insert())
				if err is None:
					forged.append(f"{doctype} (insert with docstatus 1)")
		self.assertEqual(forged, [])


# =====================================================================================================
# 1b. Server-set shared fields (§3)
# =====================================================================================================
class TestServerSetFields(MCHCase):
	def other_encounter(self) -> str:
		"""A draft consultation of another patient (MCH department)."""
		visit = routed_visit(c.DEPT_MCH, patient=make_mother().name)
		return start(visit.name, self.consultant)

	def test_server_fields_on_a_plain_insert(self):
		for doctype in RECORDS:
			with savepoint("s6_server"):
				values = self.record_values(doctype)
				with as_user(self.users["clinician"]):
					doc = frappe.get_doc(values).insert()
				visit = frappe.db.get_value(
					c.VISIT_DOCTYPE, values["visit"], ["facility", "encounter"], as_dict=True
				)
				self.assertEqual(doc.facility, visit.facility, doctype)
				self.assertEqual(doc.encounter or None, visit.encounter or None, doctype)
				self.assertEqual(doc.recorded_by, self.users["clinician"], doctype)

	def test_encounter_comes_from_the_visit_consultation(self):
		mother = make_mother().name
		make_pregnancy(mother)
		visit = routed_visit(c.DEPT_MCH, patient=mother)
		encounter = start(visit.name, self.consultant)
		with as_user(self.users["midwife"]):
			doc = frappe.get_doc(self.record_values(ANC, patient=mother, visit=visit.name)).insert()
		self.assertEqual(doc.encounter, encounter)

	# DEFECT-MCH-1: the "server-set" facility and encounter are taken from the client when sent
	def test_defect_mch_1_client_facility_and_encounter_kept(self):
		"""DEFECT-MCH-1: contract §3 - ``facility`` and ``encounter`` are read-only and "set by the server from
		the visit". `common.check_visit` only fills them when empty (``if not doc.get("facility")``), so a
		record inserted through the API with another facility or another patient's consultation keeps them:
		the MOH 717 B07-B09 counts (`reporting/mch.py`, by the record's facility) and the Due Immunisations
		list then place the record at the wrong facility, and the record links to a stranger's encounter."""
		forged = self.other_encounter()
		other_facility = get_facility(1)
		problems = []
		for doctype in RECORDS:
			with savepoint("s6_d1"):
				values = self.record_values(doctype)
				visit = frappe.db.get_value(
					c.VISIT_DOCTYPE, values["visit"], ["facility", "encounter"], as_dict=True
				)
				self.assertNotEqual(visit.facility, other_facility)
				values.update(facility=other_facility, encounter=forged)
				with as_user(self.users["clinician"]):
					doc = frappe.get_doc(values).insert()
				if doc.facility != visit.facility:
					problems.append(f"{doctype}: facility {doc.facility}")
				if doc.encounter == forged:
					problems.append(f"{doctype}: encounter {doc.encounter}")
		self.assertEqual(problems, [])

	# DEFECT-MCH-3: recorded_by can be rewritten on any later save of the draft
	def test_defect_mch_3_recorded_by_rewritten_on_draft_save(self):
		"""DEFECT-MCH-3: contract §3 - ``recorded_by`` is read-only, "the user who inserted the record".
		`common.set_recorded_by` keeps whatever value a later save sends (it only fills a new or empty value),
		so any writer can attribute the record to another clinician before submitting it."""
		problems = []
		for doctype in RECORDS:
			with savepoint("s6_d3"):
				values = self.record_values(doctype)
				with as_user(self.users["clinician"]):
					doc = frappe.get_doc(values).insert()
				with as_user(self.users["midwife"]):
					d = frappe.get_doc(doctype, doc.name)
					d.recorded_by = self.users["mic"]
					d.save()
				value = self.row(doctype, doc.name, "recorded_by").recorded_by
				if value != self.users["clinician"]:
					problems.append(f"{doctype}: {value}")
		self.assertEqual(problems, [])


# =====================================================================================================
# 2. ANC Pregnancy
# =====================================================================================================
class TestPregnancyMachine(MCHCase):
	PRIORS = ("none", "open", "closed", "archived")

	def book_values(self, patient: str) -> dict:
		return {
			"doctype": PREG,
			"patient": patient,
			"facility": get_facility(0),
			"booking_date": today(),
			"lmp": add_days(today(), -70),
			"gravida": 1,
			"para": "0",
		}

	def prior(self, kind: str) -> tuple[str, str | None]:
		mother = make_mother().name
		if kind == "none":
			return mother, None
		preg = make_pregnancy(mother).name
		if kind == "closed":
			anc.close_pregnancy(preg, c.PREGNANCY_ENDED)
		elif kind == "archived":
			common.archive(PREG, preg, "T- s6 booked in error")
		return mother, preg

	def test_book_matrix(self):
		problems = []
		for actor in ACTORS:
			for kind in self.PRIORS:
				with savepoint("s6_book"):
					mother, _preg = self.prior(kind)
					_out, err = self.act_as(
						actor, lambda m=mother: frappe.get_doc(self.book_values(m)).insert()
					)
					expected = has(actor, MCH) and kind != "open"
					if (err is None) != expected or (err is not None and not isinstance(err, REFUSALS)):
						problems.append(f"{actor} book ({kind}): expected {expected}, got {err!r}")
					opened = frappe.get_all(
						PREG, filters={"patient": mother, "status": "Open", "krcs_archived": 0}, pluck="name"
					)
					self.assertLessEqual(len(opened), 1, f"{actor} {kind}: two open pregnancies")
		self.assertEqual(problems, [])

	def test_close_pregnancy_matrix(self):
		problems = []
		for actor in ACTORS:
			for kind in ("open", "closed", "archived"):
				for reason in (*USER_CLOSE_REASONS, DELIVERED, "T- bogus"):
					with savepoint("s6_close"):
						_mother, preg = self.prior(kind)
						before = self.row(PREG, preg, "status", "closed_reason")
						_out, err = self.act_as(actor, anc.close_pregnancy, preg, reason, "T- note")
						expected = has(actor, MCH) and kind == "open" and reason in USER_CLOSE_REASONS
						after = self.row(
							PREG, preg, "status", "closed_reason", "closed_on", "closed_by", "closure_note"
						)
						if err is None:
							ok = (
								expected
								and after.status == c.PREGNANCY_CLOSED
								and after.closed_reason == reason
								and getdate(after.closed_on) == getdate(today())
								and after.closed_by == self.u(actor)
								and after.closure_note == "T- note"
							)
						else:
							ok = (
								not expected
								and isinstance(err, REFUSALS)
								and (after.status, after.closed_reason)
								== (before.status, before.closed_reason)
							)
						if not ok:
							problems.append(
								f"{actor} close ({kind}, {reason}): expected {expected}, got {err!r} {after}"
							)
		self.assertEqual(problems, [])

	def test_archive_pregnancy_matrix(self):
		problems = []
		for actor in ACTORS:
			for kind in ("open", "closed"):
				with savepoint("s6_parch"):
					mother, preg = self.prior(kind)
					_out, err = self.act_as(actor, common.archive, PREG, preg, "T- s6 wrong mother")
					expected = has(actor, ARCHIVERS)
					archived = cint(frappe.db.get_value(PREG, preg, "krcs_archived"))
					if (err is None) != expected or archived != int(expected):
						problems.append(f"{actor} archive ({kind}): expected {expected}, got {err!r}")
					if expected and kind == "open":
						# "Archiving an open pregnancy frees the mother for a new booking."
						self.assertIsNone(anc.open_pregnancy(mother))
		self.assertEqual(problems, [])

	def test_edits_by_state(self):
		"""Open: writers edit the clinical fields, never the server fields. Closed or archived: read-only for
		everyone (except the archive flag, through `archive`)."""
		for kind in ("open", "closed", "archived"):
			for actor in ("midwife", "mic", ADMIN):
				with savepoint("s6_pedit"):
					_mother, preg = self.prior(kind)

					def edit(name=preg):
						d = frappe.get_doc(PREG, name)
						d.remarks = "T- s6 remark"
						d.save()

					_out, err = self.act_as(actor, edit)
					if kind == "open":
						self.assertIsNone(err, f"{actor} edit open")
					else:
						self.assertIsInstance(err, REFUSALS, f"{actor} edit {kind}")
					for field, value in (
						("status", c.PREGNANCY_CLOSED if kind == "open" else c.PREGNANCY_OPEN),
						("closed_reason", c.PREGNANCY_ENDED if kind == "open" else c.PREGNANCY_TRANSFERRED),
						("closed_on", add_days(today(), -1)),
						("closure_note", "T- forged"),
					):

						def forge(name=preg, field=field, value=value):
							d = frappe.get_doc(PREG, name)
							d.set(field, value)
							d.save()

						before = frappe.db.get_value(PREG, preg, field)
						_out, err = self.act_as(actor, forge)
						self.assertIsInstance(err, REFUSALS, f"{actor} sets {field} on {kind}")
						if kind == "open":
							self.assertIsInstance(err, LockedFieldError, field)
						self.assertEqual(frappe.db.get_value(PREG, preg, field), before, field)

	def test_new_booking_cannot_preset_server_fields(self):
		with as_user(self.users["midwife"]):
			doc = frappe.get_doc(
				{
					**self.book_values(make_mother().name),
					"status": c.PREGNANCY_CLOSED,
					"closed_reason": DELIVERED,
					"closed_on": today(),
					"closure_note": "T- forged",
				}
			).insert()
		self.assertEqual((doc.status, doc.closed_reason, doc.closed_on), (c.PREGNANCY_OPEN, None, None))
		self.assertEqual(doc.recorded_by, self.users["midwife"])

	def test_delivery_closes_the_open_pregnancy_once(self):
		mother = make_mother().name
		preg = make_pregnancy(mother).name
		dr = make_delivery(mother, user=self.users["midwife"])
		fields = ("status", "closed_reason", "closed_on", "delivery_record", "closed_by", "modified")
		row = self.row(PREG, preg, *fields)
		self.assertEqual(
			(row.status, row.closed_reason, row.delivery_record), (c.PREGNANCY_CLOSED, DELIVERED, dr.name)
		)
		self.assertEqual(getdate(row.closed_on), getdate(dr.delivered_at))
		self.assertEqual(row.closed_by, self.users["midwife"])
		# Idempotent: running the hook again changes nothing.
		anc.close_on_delivery(frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, dr.name))
		self.assertEqual(self.row(PREG, preg, *fields), row)

	def test_delivery_leaves_later_or_archived_pregnancies_alone(self):
		mother = make_mother().name
		later = make_pregnancy(mother).name  # booked today
		make_delivery(mother, user=self.users["midwife"], days_ago=5)  # delivered 5 days before the booking
		self.assertEqual(frappe.db.get_value(PREG, later, "status"), c.PREGNANCY_OPEN)
		mother2 = make_mother().name
		archived = make_pregnancy(mother2).name
		common.archive(PREG, archived, "T- s6 wrong mother")
		make_delivery(mother2, user=self.users["midwife"])
		self.assertEqual(frappe.db.get_value(PREG, archived, "status"), c.PREGNANCY_OPEN)
		make_delivery(make_mother().name, user=self.users["midwife"])  # no pregnancy: nothing happens

	def test_contacts_after_close(self):
		mother = make_mother().name
		preg = make_pregnancy(mother).name
		with as_user(self.users["midwife"]):
			draft = frappe.get_doc(self.record_values(ANC, patient=mother)).insert()
		self.assertEqual((draft.pregnancy, draft.contact_number), (preg, 1))
		anc.close_pregnancy(preg, c.PREGNANCY_TRANSFERRED)
		with as_user(self.users["midwife"]):
			_out, err = _try(lambda: frappe.get_doc(ANC, draft.name).submit())
		self.assertIsInstance(err, MCHRecordError)
		visit = make_next_visit(mother).name
		with as_user(self.users["midwife"]):
			_out, err = _try(
				lambda: frappe.get_doc(
					{
						"doctype": ANC,
						"patient": mother,
						"visit": visit,
						"contact_date": today(),
					}
				).insert()
			)
		self.assertIsInstance(err, MCHRecordError)
		self.assertIn("Book the pregnancy first", str(err))

	def test_contact_numbers_skip_archived(self):
		mother = make_mother().name
		make_pregnancy(mother)
		first = self.submitted(ANC, patient=mother)
		second = self.submitted(ANC, patient=mother, visit=make_next_visit(mother).name)
		self.assertEqual((first.contact_number, second.contact_number), (1, 2))
		common.archive(ANC, second.name, "T- s6 wrong visit")
		third = self.draft(ANC, patient=mother, visit=make_next_visit(mother).name)
		self.assertEqual(third.contact_number, 2)

	# DEFECT-MCH-6: Close Pregnancy, Add Addendum and Archive work but are offered nowhere in the desk
	def test_defect_mch_6_endpoints_offered_in_the_desk(self):
		"""DEFECT-MCH-6 (offered == works, the other way): `anc.close_pregnancy`, `common.add_addendum` and
		`common.archive` are the only way to record a miscarriage or a transfer, correct a submitted record or
		archive one (contract §4, §5), and the errors tell the user to use them ("close it first (Close
		Pregnancy)", "Use Add Addendum to record a correction", "Only the Medical In-Charge archives an MCH record
		(Archive)"). No form script calls them: the MCH DocTypes have no .js and `mch/forms.js` only adds the
		visit / consultation buttons and the Patient panel. A pregnancy that ends without a Delivery Record stays
		Open, and the mother can never be booked again from the desk."""
		sources = []
		for root, dirs, files in os.walk(APP_DIR):
			dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", "tests")]
			for f in files:
				if f.endswith(".js"):
					with open(os.path.join(root, f), encoding="utf-8") as fh:
						sources.append(fh.read())
		text = "\n".join(sources)
		missing = [
			m
			for m in (
				"onerc_healthcare.mch.anc.close_pregnancy",
				"onerc_healthcare.mch.common.add_addendum",
				"onerc_healthcare.mch.common.archive",
			)
			if m not in text
		]
		self.assertEqual(missing, [])


# =====================================================================================================
# 3. One record per visit
# =====================================================================================================
class TestOnePerVisit(MCHCase):
	def test_second_record_on_a_visit_refused_until_archived(self):
		for doctype in RECORDS:
			for submit_first in (False, True):
				with savepoint("s6_one"):
					first = self.submitted(doctype) if submit_first else self.draft(doctype)
					again = self.record_values(doctype, patient=first.patient, visit=first.visit)
					if doctype == IMM:
						again["doses"] = [
							{"vaccine": "OPV 0", "status": c.DOSE_NOT_GIVEN, "reason_not_given": "T-"}
						]
					for actor in ("midwife", ADMIN):
						_out, err = self.act_as(actor, lambda v=again: frappe.get_doc(dict(v)).insert())
						self.assertIsInstance(err, MCHRecordError, f"{doctype} by {actor}")
						self.assertIn(first.name, str(err))
					self.assertEqual(frappe.db.count(doctype, {"visit": first.visit}), 1)
					common.archive(doctype, first.name, "T- s6 wrong record")
					with as_user(self.users["midwife"]):
						second = frappe.get_doc(dict(again)).insert()
						second.submit()
					self.assertEqual(frappe.db.count(doctype, {"visit": first.visit, "krcs_archived": 0}), 1)

	def test_ward_pnc_one_per_admission_and_patient(self):
		mother = make_mother().name
		dr = make_delivery(mother, user=self.users["midwife"], babies=[live_baby(), live_baby(sex="Male")])
		babies = [r.newborn_patient for r in frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, dr.name).babies]
		base = {
			"doctype": PNC,
			"maternity_admission": dr.admission,
			"pnc_date": today(),
			"timing": "Within 48 hours",
		}
		with as_user(self.users["midwife"]):
			frappe.get_doc({**base, "patient": mother, "subject": c.PNC_SUBJECT_MOTHER}).insert()
			for baby in babies:
				frappe.get_doc({**base, "patient": baby, "subject": c.PNC_SUBJECT_BABY}).insert()
			_out, err = _try(
				lambda: frappe.get_doc({**base, "patient": mother, "subject": c.PNC_SUBJECT_MOTHER}).insert()
			)
		self.assertIsInstance(err, MCHRecordError)
		self.assertEqual(frappe.db.count(PNC, {"maternity_admission": dr.admission}), 3)

	def test_pnc_needs_exactly_one_of_visit_and_admission(self):
		mother = make_mother().name
		dr = make_delivery(mother, user=self.users["midwife"])
		visit = make_next_visit(mother).name
		base = {
			"doctype": PNC,
			"patient": mother,
			"subject": c.PNC_SUBJECT_MOTHER,
			"pnc_date": today(),
			"timing": "Other",
		}
		with as_user(self.users["midwife"]):
			for extra in ({}, {"visit": visit, "maternity_admission": dr.admission}):
				_out, err = _try(lambda e=extra: frappe.get_doc({**base, **e}).insert())
				self.assertIsInstance(err, REFUSALS, str(extra))

	# DEFECT-MCH-4: the uniqueness reads after the visit / patient lock are not locking reads (D41)
	def test_defect_mch_4_uniqueness_reads_are_locking_reads(self):
		"""DEFECT-MCH-4: D41 / HANDOVER §11 - "every read after a lock is itself a locking read". The reads that
		decide "one per visit" (`common.guard_one_per_visit`, a plain `frappe.get_all`) and "this dose is already
		given" (`immunisation._given_doses`, plain SQL) run after the visit (and Patient) row lock without FOR
		UPDATE. Under REPEATABLE READ they read the request's snapshot, taken before the lock was granted, so the
		lock does not serialise them: two drafts of one child on two visits can both submit BCG (the module
		docstring's "so two drafts cannot both submit the same dose" does not hold)."""
		problems = []
		for doctype in RECORDS:
			with savepoint("s6_d4"):
				values = self.record_values(doctype)
				reads = self.reads_after_visit_lock(lambda v=values: frappe.get_doc(v).insert())
				own = [q for q in reads if f"FROM `tab{doctype}`" in q and "`visit`" in q]
				if doctype == IMM:
					own += [q for q in reads if "`tabImmunisation Dose`" in q]
				self.assertTrue(own, f"{doctype}: no uniqueness read seen")
				problems += [f"{doctype}: {q[:120]}" for q in own if "FOR UPDATE" not in q.upper()]
		self.assertEqual(problems, [])

	def reads_after_visit_lock(self, fn) -> list[str]:
		seen: list[str] = []
		orig = frappe.local.db.sql

		def spy(query, *args, **kwargs):
			seen.append(" ".join(str(query).split()))
			return orig(query, *args, **kwargs)

		with patch.object(frappe.local.db, "sql", side_effect=spy):
			fn()
		start_at = next(i for i, q in enumerate(seen) if "`tabPatient Visit`" in q and "FOR UPDATE" in q)
		return [q for q in seen[start_at + 1 :] if q.upper().startswith("SELECT")]

	# DEFECT-MCH-5: Immunisation locks Visit then Patient; visit registration and admission lock Patient first
	def test_defect_mch_5_patient_and_visit_lock_order(self):
		"""DEFECT-MCH-5: lock-order inversion. Registering a visit (`PatientVisit.validate_patient`:
		`lock_patient`, then `find_open_visit(for_update=True)`) and the maternity admit (`_admit_locked`:
		"Patient -> Visit") lock the Patient row before the visit rows. An Immunisation insert / save / submit
		locks the visit (`common.check_visit`) and then the child's Patient row (`immunisation._child(
		for_update=True)`). A receptionist registering the child while the nurse saves the immunisation
		deadlocks (one of them fails). The Patient row must be locked before the visit, or not at all."""
		child = make_child(age_days=120).name
		reg = self.lock_order(lambda: make_mch_visit(child))
		self.assertEqual(reg[:1], ["patient"], reg)  # registration: Patient first
		close_visit(frappe.db.get_value(c.VISIT_DOCTYPE, {"patient": child}, "name"))
		values = self.record_values(IMM, patient=child)
		imm = self.lock_order(lambda: frappe.get_doc(values).insert())
		if "patient" in imm:
			self.assertLess(imm.index("patient"), imm.index("visit"), imm)

	def lock_order(self, fn) -> list[str]:
		order: list[str] = []
		orig = frappe.local.db.sql

		def spy(query, *args, **kwargs):
			q = " ".join(str(query).split())
			if "FOR UPDATE" in q.upper():
				if re.search(r"FROM `tabPatient Visit`", q):
					order.append("visit")
				elif re.search(r"FROM `tabPatient`", q):
					order.append("patient")
			return orig(query, *args, **kwargs)

		with patch.object(frappe.local.db, "sql", side_effect=spy):
			fn()
		return order


# =====================================================================================================
# 4. Offered == works (mch.forms.visit_actions)
# =====================================================================================================
class TestOfferedWorks(MCHCase):
	KINDS = ("woman", "pregnant", "baby", "toddler", "man", "girl", "woman_with_record")

	def patient(self, kind: str) -> str:
		if kind in ("woman", "woman_with_record"):
			return make_mother().name
		if kind == "pregnant":
			mother = make_mother().name
			make_pregnancy(mother)
			return mother
		if kind == "baby":
			return make_child(age_days=40, sex="Female").name
		if kind == "toddler":
			return make_child(age_days=500).name
		if kind == "girl":
			return make_child(age_days=6 * 365, sex="Female").name
		return make_patient(sex="Male", dob=add_days(today(), -30 * 365)).name

	def new_values(self, action: dict) -> dict:
		defaults = dict(action["defaults"])
		if action["action"] == "book":
			return {
				"doctype": PREG,
				**defaults,
				"booking_date": today(),
				"lmp": add_days(today(), -70),
				"gravida": 1,
				"para": "0",
			}
		values = self.record_values(action["doctype"], patient=defaults["patient"], visit=defaults["visit"])
		values.update(defaults)
		return values

	def link_problems(self, action: dict) -> list[str]:
		"""The browser re-validates every prefilled Link (frappe.new_doc -> __run_link_triggers ->
		validate_link_and_fetch -> search_widget) as the user: each value must come back."""
		doctype = PREG if action["action"] == "book" else action["doctype"]
		meta = frappe.get_meta(doctype)
		out = []
		for field, value in action["defaults"].items():
			df = meta.get_field(field)
			if not df or df.fieldtype != "Link" or not value:
				continue
			rows = search_widget(
				df.options,
				txt=value,
				page_length=10,
				as_dict=False,
				for_link_validation=True,
				reference_doctype=doctype,
			)
			if not any(r[0] == value for r in rows or ()):
				out.append(f"{doctype}.{field}={value} not found by {frappe.session.user}")
		return out

	def test_offered_actions_work(self):
		problems = []
		for kind in self.KINDS:
			for actor in ACTORS:
				with savepoint("s6_offer"):
					patient = self.patient(kind)
					visit = make_mch_visit(patient).name
					if kind == "woman_with_record":
						self.draft(PNC, patient=patient, visit=visit)
					offered, err = self.act_as(actor, forms.visit_actions, visit)
					if err is not None:
						if not isinstance(err, frappe.PermissionError):
							problems.append(f"{kind}/{actor}: visit_actions raised {err!r}")
						continue
					for action in offered["actions"]:
						tag = f"{kind}/{actor}/{action['doctype']}/{action['action']}"
						if action["action"] == "open":
							if not frappe.has_permission(
								action["doctype"], "read", doc=action["existing"], user=self.u(actor)
							):
								problems.append(f"{tag}: cannot read {action['existing']}")
							continue
						with savepoint("s6_offer_one"):
							with as_user(self.u(actor)):
								problems += [f"{tag}: {p}" for p in self.link_problems(action)]
								_out, err = _try(lambda a=action: frappe.get_doc(self.new_values(a)).insert())
							if err is not None:
								problems.append(f"{tag}: {err!r}")
		self.assertEqual(problems, [])

	def test_closed_visit_offers_nothing(self):
		for kind in ("pregnant", "baby"):
			patient = self.patient(kind)
			visit = make_mch_visit(patient).name
			close_visit(visit)
			for actor in ("midwife", "triage", ADMIN):
				with as_user(self.u(actor)):
					self.assertEqual(forms.visit_actions(visit)["actions"], [], f"{kind}/{actor}")

	def test_offers_match_the_records_a_writer_can_make(self):
		"""The fitting records are offered to a writer: a mother ANC + PNC, a baby PNC + Immunisation + Growth,
		a toddler Immunisation + Growth, a man and a girl of 6 nothing; the Triage Nurse the child records."""
		fits = {
			"pregnant": {ANC, PNC},
			"woman": {ANC, PNC},
			"baby": {PNC, IMM, GM},
			"toddler": {IMM, GM},
			"man": set(),
			"girl": set(),
		}
		for kind, expected in fits.items():
			patient = self.patient(kind)
			visit = make_mch_visit(patient).name
			with as_user(self.u("mic")):
				got = {a["doctype"] for a in forms.visit_actions(visit)["actions"]}
			self.assertEqual(got, expected, kind)
			with as_user(self.u("triage")):
				got = {a["doctype"] for a in forms.visit_actions(visit)["actions"]}
			self.assertEqual(got, expected & {IMM, GM}, f"{kind} (triage)")


# =====================================================================================================
# 4b. Reads of logged records through the MCH endpoints (contract §2, D44)
# =====================================================================================================
class TestReadLogging(MCHCase):
	def logged(self, actor: str, fn, *args) -> list[tuple]:
		"""(action, reference doctype, reference name) of every Access Log Entry `fn` builds as `actor`."""
		calls = []
		real = access_log.build_entry

		def spy(action, reference_doctype=None, reference_name=None, *a, **kw):
			calls.append((action, reference_doctype, reference_name))
			return real(action, reference_doctype, reference_name, *a, **kw)

		with patch.object(access_log, "build_entry", side_effect=spy), as_user(self.u(actor)):
			fn(*args)
		return calls

	def test_admit_prefill_is_logged(self):
		mother = make_mother().name
		preg = make_pregnancy(mother).name
		calls = self.logged("midwife", anc.admit_prefill, mother)
		self.assertIn((access_log.ACTION_READ_API, PREG, preg), calls)

	# DEFECT-MCH-7: schedule, chart_data and patient_summary read logged MCH records without an access log entry
	def test_defect_mch_7_panel_endpoints_log_their_reads(self):
		"""DEFECT-MCH-7: contract §2 - the MCH parents are ACCESS_LOGGED_DOCTYPES ("opens, API reads and lists
		logged"; D44 "every read of an ACCESS_LOGGED_DOCTYPES record"). `privacy.read_log` only wraps the
		frappe.client / reportview methods, so a custom endpoint must log its own reads (as `anc.admit_prefill`
		and `partograph.get_chart` do). `immunisation.schedule` (Immunisation names and dates),
		`growth.chart_data` (every submitted Growth Monitoring: weights and dates) and `forms.patient_summary`
		(the open pregnancy's LMP and EDD) return record data on every Patient form open and log nothing."""
		child = make_child(age_days=120).name
		self.submitted(IMM, patient=child)
		close_visit(frappe.db.get_value(c.VISIT_DOCTYPE, {"patient": child}, "name"))
		self.submitted(GM, patient=child)
		mother = make_mother().name
		make_pregnancy(mother)
		silent = []
		for label, fn, arg, doctype in (
			("schedule", immunisation.schedule, child, IMM),
			("chart_data", growth.chart_data, child, GM),
			("patient_summary", forms.patient_summary, mother, PREG),
		):
			if not any(dt == doctype for _a, dt, _n in self.logged("clinician", fn, arg)):
				silent.append(label)
		self.assertEqual(silent, [])


# =====================================================================================================
# 5. KEPI and z-score spot checks against published WHO / KEPI values
# =====================================================================================================
class TestMathsSpotChecks(MCHCase):
	def test_who_birth_weight_lines(self):
		"""WHO Child Growth Standards, weight-for-age at birth (simplified field tables, 1 decimal)."""
		published = {"boys": (2.1, 2.5, 3.3, 4.4, 5.0), "girls": (2.0, 2.4, 3.2, 4.2, 4.8)}
		for sex, kgs in published.items():
			L, M, S = growth.lms("wfa", sex, 0)
			got = tuple(round(growth.value_at(L, M, S, z), 1) for z in (-3, -2, 0, 2, 3))
			self.assertEqual(got, kgs, sex)
			self.assertAlmostEqual(growth.zscore("wfa", sex, 0, M), 0, places=6)
		# SD23: beyond +-3 SD the distance is measured in (SD3 - SD2) units on that side.
		L, M, S = growth.lms("wfa", "boys", 0)
		sd2, sd3 = growth.value_at(L, M, S, 2), growth.value_at(L, M, S, 3)
		self.assertAlmostEqual(growth.zscore("wfa", "boys", 0, sd3 + (sd3 - sd2)), 4.0, places=6)
		sd2n, sd3n = growth.value_at(L, M, S, -2), growth.value_at(L, M, S, -3)
		self.assertAlmostEqual(growth.zscore("wfa", "boys", 0, sd3n - (sd2n - sd3n)), -4.0, places=6)

	def test_kepi_due_date_follows_the_previous_dose(self):
		"""§7: due_date = max(dob + due_age_days, previous dose + min_interval_days); Due for 28 days."""
		rows = immunisation._schedule_rows()
		dob = getdate(add_days(today(), -200))
		given = {"OPV 1": (dob + timedelta(days=60), "T-IMM")}
		on = dob + timedelta(days=88)
		sched = {r["vaccine"]: r for r in immunisation.compute_schedule(rows, dob, given, on)}
		self.assertEqual(sched["OPV 2"]["due_date"], on)  # max(dob + 70, day 60 + 28)
		self.assertEqual(sched["OPV 2"]["status"], c.SCHEDULE_DUE)
		later = on + timedelta(days=29)
		sched = {r["vaccine"]: r for r in immunisation.compute_schedule(rows, dob, given, later)}
		self.assertEqual(sched["OPV 2"]["status"], c.SCHEDULE_OVERDUE)
