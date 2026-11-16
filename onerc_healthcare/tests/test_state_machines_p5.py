# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""S5 state-machine audit of the Phase 5M build (Maternity: M1-M3 + W0). Never commit.

Every Maternity lifecycle is driven through its own API (and through the desk paths a user can reach), by every
relevant actor, from every status, and compared with an independent oracle written from
docs/MATERNITY_CONTRACT.md (§1 DocPerms, §5 visit integration, §6 lifecycles, §7 endpoints) and the module
docstrings. The tables are copied below as literals; meta-tests compare them with the code's introspectable
tables. Behaviour that contradicts the documented rules is kept as an ``@unittest.expectedFailure`` test that
asserts the *correct* behaviour (comment ``DEFECT-P5-n``); the matrices skip exactly the combinations such a test
covers, so a fix never breaks a matrix. DEFECT-P5-1..3 were fixed in the Phase 5M fix round: their tests now
pass as plain tests and the matrices no longer skip them.

Actors: Midwife, Clinical Officer, Medical In-Charge, HIS Officer, Mental Health Officer, Nurse Receptionist,
Triage Nurse, Pharmacist, Outreach Team Member, Administrator.

Machines:

1. Maternity Admission (`maternity/admission.py` TRANSITIONS): admit (endpoint) x prior admission status, and
   start_labour / discharge (each outcome; Died with and without a cause) / record_delivery (user, apply_action
   and system) / archive / unarchive / desk edits x status (Admitted, In Labour, Delivered, Discharged, archived)
   x actor; `allowed_actions` (the form's buttons, also through onload) == what works; the bed after every path.
2. Patient Visit x admit: admit from every visit status (and the fast path from every open-visit status); the
   visit's resulting status / outcome / department; the "Admit to Maternity" / "Admit in Labour" offers; second
   admission refusals.
3. Partograph: get_or_create / add_observation x admission status x actor (offered: get_chart.can_add, onload
   can_open_partograph); rows append-only through every desk / REST path.
4. Delivery Record: submit x admission status x actor (offered: onload can_record_delivery); one submitted per
   admission; cancel / amend / edit-after-submit refused; newborn registration idempotent (re-run hooks, twins,
   triplets, stillbirths, forged links) and the admission ends Delivered exactly once.
"""

from __future__ import annotations

import frappe
from frappe import client as frappe_client
from frappe.utils import cint, now_datetime

from onerc_healthcare import constants as c
from onerc_healthcare.maternity import admission as adm
from onerc_healthcare.maternity import delivery as dlv
from onerc_healthcare.maternity import partograph as ptg
from onerc_healthcare.maternity.admission import MaternityAdmissionError, OpenAdmissionExistsError
from onerc_healthcare.maternity.delivery import DeliveryRecordError
from onerc_healthcare.maternity.partograph import (
	ImplausiblePartographValueError,
	PartographError,
	PartographLockedError,
)
from onerc_healthcare.maternity.setup import ward_for_facility
from onerc_healthcare.privacy.lifecycle import IllegalTransitionError, LockedFieldError
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import add_diagnosis, routed_visit, start
from onerc_healthcare.tests.factories import get_facility, make_user
from onerc_healthcare.tests.maternity_factories import make_bed, make_maternity_patient, maternity_users
from onerc_healthcare.tests.sm_helpers import ADMIN, as_user, savepoint
from onerc_healthcare.tests.visit_factories import make_visit, make_visit_users
from onerc_healthcare.visit import engine

MAT = c.MATERNITY_ADMISSION_DOCTYPE
PTG = c.PARTOGRAPH_DOCTYPE
PTG_ROW = c.PARTOGRAPH_OBSERVATION_DOCTYPE
DR = c.DELIVERY_RECORD_DOCTYPE
HSU = "Healthcare Service Unit"
SYSTEM = "system"

ADMITTED, IN_LABOUR, DELIVERED, DISCHARGED = (
	c.ADMISSION_ADMITTED,
	c.ADMISSION_IN_LABOUR,
	c.ADMISSION_DELIVERED,
	c.ADMISSION_DISCHARGED,
)
ARCHIVED = "archived"  # an Admitted admission archived by the Medical In-Charge
ADM_STATUSES = (ADMITTED, IN_LABOUR, DELIVERED, DISCHARGED, ARCHIVED)
OCCUPIED, VACANT = "Occupied", "Vacant"
LIVE, FRESH, MACERATED = c.BABY_LIVE_BIRTH, c.BABY_FRESH_STILLBIRTH, c.BABY_MACERATED_STILLBIRTH

# =====================================================================================================
# Oracles (literal copies of the contract)
# =====================================================================================================

# Actor key -> role (one user each holding exactly that role).
ROLE_OF = {
	"midwife": "Midwife",
	"clinician": "Clinical Officer",
	"mic": "Medical In-Charge",
	"his": "HIS Officer",
	"mho": "Mental Health Officer",
	"reception": "Nurse Receptionist",
	"triage": "Triage Nurse",
	"pharmacist": "Pharmacist",
	"outreach": "Outreach Team Member",
	ADMIN: ADMIN,
}
ACTORS = tuple(ROLE_OF)
# §1: MATERNITY_ROLES admit, discharge, keep the partograph and record deliveries.
MATERNITY = ("Midwife", "Clinical Officer", "Medical In-Charge")
# §1 (constants): DIED_ROLES; a Midwife may record a maternal death.
DIED = ("Clinical Officer", "Mental Health Officer", "Medical In-Charge", "Midwife")
# §6.1: correction is archiving by the Medical In-Charge.
ARCHIVERS = ("Medical In-Charge",)
# §1 DocPerm table (same rows for the three DocTypes, plus submit on the Delivery Record).
READERS = ("Midwife", "Clinical Officer", "Medical In-Charge", "Mental Health Officer", "HIS Officer")
WRITERS = ("Midwife", "Clinical Officer", "Medical In-Charge")
DELIVERY_SUBMITTERS = ("Midwife", "Clinical Officer")

# §6.1 lifecycle table: action -> (from statuses, to status, roles or SYSTEM).
EXPECTED_ADMISSION: dict[str, tuple[tuple[str, ...], str, tuple[str, ...] | str]] = {
	"admit": ((), ADMITTED, MATERNITY),
	"start_labour": ((ADMITTED,), IN_LABOUR, MATERNITY),
	"record_delivery": ((ADMITTED, IN_LABOUR), DELIVERED, SYSTEM),
	"discharge": ((ADMITTED, IN_LABOUR, DELIVERED), DISCHARGED, MATERNITY),
}
OPEN = (ADMITTED, IN_LABOUR, DELIVERED)  # §3.1 ADMISSION_OPEN_STATUSES
OUTCOMES = ("Discharged home", "Referred out", "Died", "Absconded")
DIED_OUTCOME = "Died"
# §6.2: rows only while Admitted or In Labour. §6.3 / delivery docstring: a delivery while Admitted / In Labour.
PARTOGRAPH_OPEN = (ADMITTED, IN_LABOUR)
DELIVERY_OPEN = (ADMITTED, IN_LABOUR)


def has(actor: str, roles) -> bool:
	return actor == ADMIN or ROLE_OF[actor] in roles


def _try(fn, *args, **kwargs):
	try:
		return fn(*args, **kwargs), None
	except Exception as e:  # compared with the oracle by the caller
		return None, e


def admit_mother(patient, visit=None, ward=None, bed=None, values=None):
	"""adm.admit with gravida 1 (required at admission since the Phase 5M fix round, finding 10)."""
	return adm.admit(
		patient=patient, visit=visit, ward=ward, bed=bed, values={"gravida": 1, **(values or {})}
	)


def _discharge_values(outcome: str | None, cause: bool = True) -> dict:
	values = {"discharge_condition": "T- stable", "discharge_notes": "T- s5 notes"}
	if outcome:
		values["discharge_outcome"] = outcome
	if cause:
		values["death_reason"] = "T- PPH"
	return values


# Lifecycle operations: key -> (kind, action, values).
LIFECYCLE_OPS: dict[str, tuple[str, str | None, dict]] = {
	"start_labour": ("apply", "start_labour", {}),
	**{f"discharge:{o}": ("apply", "discharge", _discharge_values(o)) for o in OUTCOMES},
	"discharge:Died (no cause)": ("apply", "discharge", _discharge_values(DIED_OUTCOME, cause=False)),
	"discharge:(no outcome)": ("apply", "discharge", _discharge_values(None, cause=False)),
	"record_delivery (apply_action)": ("apply", "record_delivery", {}),
	"admit (apply_action)": ("apply", "admit", {}),
	"record_delivery (transition)": ("user_transition", "record_delivery", {"delivery": "T-S5-DEL"}),
	"archive": ("archive", None, {}),
	"unarchive": ("unarchive", None, {}),
	"edit_remarks": ("edit_remarks", None, {}),
	"edit_status": ("edit_status", None, {}),
}


def expect_lifecycle(status: str, op: str, actor: str):
	"""Expected exception class (or tuple) or the resulting status (str); None when the op means nothing in
	`status` (archive an archived admission / unarchive a live one / edit an archived one)."""
	kind, action, values = LIFECYCLE_OPS[op]
	archived = status == ARCHIVED
	real = ADMITTED if archived else status
	if kind == "apply":
		if action in ("record_delivery", "admit"):
			# A system event / the admit endpoint is never an apply_action action, for anyone.
			return (frappe.PermissionError, IllegalTransitionError)
		from_statuses, to_status, roles = EXPECTED_ADMISSION[action]
		if not has(actor, roles):
			return frappe.PermissionError
		if archived or real not in from_statuses:
			return IllegalTransitionError
		if action == "discharge":
			outcome = values.get("discharge_outcome")
			if outcome not in OUTCOMES:
				return IllegalTransitionError
			if outcome == DIED_OUTCOME:
				if not has(actor, DIED):
					return frappe.PermissionError
				if not values.get("death_reason"):
					return IllegalTransitionError
		return to_status
	if kind == "user_transition":
		# A system event: code (or Administrator, privacy.lifecycle.Transition) only.
		if actor != ADMIN:
			return frappe.PermissionError
		from_statuses, to_status, _roles = EXPECTED_ADMISSION[action]
		if archived or real not in from_statuses:
			return IllegalTransitionError
		return to_status
	if (kind == "archive" and archived) or (kind == "unarchive" and not archived):
		return None
	if kind == "edit_remarks" and archived:
		return None
	if not has(actor, WRITERS):
		return frappe.PermissionError
	if kind in ("archive", "unarchive"):
		return real if has(actor, ARCHIVERS) else LockedFieldError
	if kind == "edit_status":
		return LockedFieldError  # status only through the actions (§6.1)
	# edit_remarks: clinical fields stay editable while open; a Discharged admission is read-only.
	return LockedFieldError if real == DISCHARGED else real


def expect_admit(prior: str | None, actor: str):
	"""admit with a new Maternity visit for a patient whose latest admission is in `prior` (None: never admitted)."""
	if not has(actor, MATERNITY):
		return frappe.PermissionError
	if prior in OPEN:
		return OpenAdmissionExistsError
	return ADMITTED  # no admission, a Discharged one, or an archived (no longer open) one


# §5 / §7: what admit does with `visit` in each visit status.
CLOSE, LINK, REFUSE = "close", "link", "refuse"
VISIT_KINDS: dict[str, str] = {
	"Registered": CLOSE,
	"In Triage": CLOSE,
	"Routed (Maternity)": CLOSE,
	"Routed (Maternity, internal referral)": CLOSE,
	"Routed (Outpatient)": REFUSE,  # §7: "routed to Maternity"; DEFECT-P5-1 (fixed)
	"In Consultation": REFUSE,
	"Awaiting Laboratory": REFUSE,
	"Awaiting Pharmacy": REFUSE,
	"Discharged (Admitted, Complete Consultation)": LINK,
	# Fix round decision 14a: only a visit closed by Complete Consultation is linked, not one closed by Close Visit.
	"Discharged (Admitted, Close Visit)": REFUSE,
	"Discharged (Discharged home, Complete Consultation)": REFUSE,
	"Discharged (Left, Close Visit)": REFUSE,
	"Referred": REFUSE,
}
DEFECT_VISIT_KINDS: tuple[str, ...] = ()  # DEFECT-P5-1 fixed: the matrix covers every kind
# Fast path (no `visit`): admission docstring - the patient's open visit is used, else a new one is opened.
FAST_PATH_KINDS: dict[str, str] = {
	"Registered": CLOSE,
	"In Triage": CLOSE,
	"Routed (Maternity)": CLOSE,
	"In Consultation": REFUSE,
	"Awaiting Laboratory": REFUSE,
	"Awaiting Pharmacy": REFUSE,
	"no open visit": "new",
}


# =====================================================================================================
# Base case
# =====================================================================================================


class P5Case(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		mat = maternity_users()
		visit_users = make_visit_users()
		cls.users = {
			"midwife": mat["midwife"],
			"clinician": mat["clinician"],
			"mic": mat["mic"],
			"his": mat["his"],
			"mho": mat["mho"],
			"reception": mat["reception"],
			"triage": make_user("s5-triage", [c.ROLE_TRIAGE]),
			"pharmacist": make_user("s5-pharmacist", [c.ROLE_PHARMACIST]),
			"outreach": make_user("s5-outreach", [c.ROLE_OUTREACH]),
		}
		cls.consultant = visit_users["clinician"]
		frappe.set_user(ADMIN)

	def setUp(self):
		super().setUp()
		self.facility = get_facility(0)
		self.ward = ward_for_facility(self.facility)
		if not self.ward:
			self.skipTest("No Maternity Ward seeded on this site (maternity.setup.seed_maternity_wards).")

	def u(self, actor: str) -> str:
		return ADMIN if actor == ADMIN else self.users[actor]

	def act_as(self, actor: str, fn, *args, **kwargs):
		with as_user(self.u(actor)):
			return _try(fn, *args, **kwargs)

	def check_exc(self, err, exp, msg=""):
		self.assertIsNotNone(err, f"expected {exp}, got success {msg}")
		self.assertTrue(isinstance(err, exp), f"expected {exp}, got {err!r} {msg}")

	# -- admissions ------------------------------------------------------------------------------------
	def mother(self) -> str:
		return make_maternity_patient().name

	def bed_status(self, bed: str | None) -> str | None:
		return frappe.db.get_value(HSU, bed, "occupancy_status") if bed else None

	def admit_new(self, actor: str = "midwife", with_bed: bool = True) -> tuple[str, str | None, str]:
		"""(admission, bed, mother): a new mother admitted through the endpoint from a Maternity visit."""
		mother = self.mother()
		visit = routed_visit(c.DEPT_MATERNITY, patient=mother)
		bed = make_bed(self.ward) if with_bed else None
		with as_user(self.u(actor)):
			out = admit_mother(patient=mother, visit=visit.name, bed=bed)
		self.assertEqual(out["status"], ADMITTED)
		if bed:
			self.assertEqual(self.bed_status(bed), OCCUPIED)
		return out["name"], bed, mother

	def move(self, name: str, status: str) -> None:
		"""Move an Admitted admission to `status` through the module's own paths."""
		if status in (IN_LABOUR, DELIVERED):
			with as_user(self.u("midwife")):
				adm.apply_action(name, "start_labour")
		if status == DELIVERED:
			adm.record_delivery(name, "T-S5-DEL-0")
		if status == DISCHARGED:
			with as_user(self.u("midwife")):
				adm.apply_action(name, "discharge", {"discharge_outcome": c.MATERNITY_DISCHARGED_HOME})
		if status == ARCHIVED:
			self.set_archived(name, 1)
		self.assertEqual(self.status_of(name), status)

	def set_archived(self, name: str, value: int, actor: str = "mic") -> None:
		with as_user(self.u(actor)):
			doc = frappe.get_doc(MAT, name)
			doc.krcs_archived = value
			doc.save()

	def status_of(self, name: str) -> str:
		row = frappe.db.get_value(MAT, name, ["status", "krcs_archived"], as_dict=True)
		return ARCHIVED if cint(row.krcs_archived) else row.status

	def admission_in(self, status: str, with_bed: bool = True) -> tuple[str, str | None, str]:
		name, bed, mother = self.admit_new(with_bed=with_bed)
		self.move(name, status)
		if bed:  # the bed after every path: occupied while open, vacant after discharge / archive
			self.assertEqual(self.bed_status(bed), OCCUPIED if status in OPEN else VACANT, status)
		return name, bed, mother

	def adm_state(self, name: str, bed: bool = True) -> dict:
		doc = frappe.get_doc(MAT, name)
		state = {
			"status": doc.status,
			"archived": cint(doc.get("krcs_archived")),
			"log": len(doc.status_log),
			"outcome": doc.discharge_outcome,
			"death_reason": doc.death_reason,
			"discharged_by": doc.discharged_by,
			"remarks": doc.remarks,
		}
		if bed:
			state["bed"] = self.bed_status(doc.bed)
		return state

	def delivery_log_rows(self, name: str) -> int:
		return sum(1 for r in frappe.get_doc(MAT, name).status_log if r.action == "record_delivery")

	# -- visits ----------------------------------------------------------------------------------------
	def visit_snap(self, visit: str) -> dict:
		doc = frappe.get_doc(c.VISIT_DOCTYPE, visit)
		return {
			"status": doc.status,
			"outcome": doc.get("outcome"),
			"outcome_by": doc.get("outcome_by"),
			"dept": doc.current_department,
			"closed": bool(doc.closed_at),
			"lab": cint(doc.lab_open),
			"ph": cint(doc.pharmacy_open),
			"log": len(doc.status_log),
			"last_action": doc.status_log[-1].action if doc.status_log else None,
		}

	def consulting(self, mother: str) -> tuple[str, str]:
		visit = routed_visit(c.DEPT_OUTPATIENT, patient=mother).name
		return visit, start(visit, self.consultant)

	def complete(self, encounter: str, outcome: str | None = None) -> None:
		from onerc_healthcare.clinical.consultation import complete_consultation

		add_diagnosis(encounter)
		with as_user(self.consultant):
			complete_consultation(encounter, outcome=outcome)

	def visit_kind(self, kind: str, mother: str) -> tuple[str, str | None]:
		"""A visit of `mother` in the state named `kind` (VISIT_KINDS), built through the real paths.
		Returns (visit, Sent internal referral or None)."""
		referral = None
		if kind == "Registered":
			visit = make_visit(patient=mother).name
		elif kind == "In Triage":
			visit = make_visit(patient=mother).name
			engine.transition(visit, "start_triage", system=True)
		elif kind == "Routed (Maternity)":
			visit = routed_visit(c.DEPT_MATERNITY, patient=mother).name
		elif kind == "Routed (Outpatient)":
			visit = routed_visit(c.DEPT_OUTPATIENT, patient=mother).name
		elif kind == "Routed (Maternity, internal referral)":
			from onerc_healthcare.clinical import referral as ref

			_source, enc = self.consulting(mother)
			add_diagnosis(enc)
			with as_user(self.consultant):
				sent = ref.create_referral(
					enc, "Internal", "T- in labour", to_department=c.DEPT_MATERNITY, send=True
				)
			visit, referral = sent["target_visit"], sent["name"]
		elif kind in ("In Consultation", "Awaiting Laboratory", "Awaiting Pharmacy", "Referred"):
			visit, _enc = self.consulting(mother)
			if kind == "Awaiting Laboratory":
				engine.transition(visit, "request_lab", system=True)
			elif kind == "Awaiting Pharmacy":
				engine.transition(visit, "prescribe", system=True)
			elif kind == "Referred":
				engine.transition(visit, "refer", system=True, comment="T- s5 referred out")
		elif kind == "Discharged (Admitted, Complete Consultation)":
			visit, enc = self.consulting(mother)
			self.complete(enc, c.OUTCOME_ADMITTED)
		elif kind == "Discharged (Discharged home, Complete Consultation)":
			visit, enc = self.consulting(mother)
			self.complete(enc)
		elif kind == "Discharged (Admitted, Close Visit)":
			visit = make_visit(patient=mother).name
			engine.transition(
				visit,
				"close_visit",
				system=True,
				closure_reason="Other",
				outcome=c.OUTCOME_ADMITTED,
				comment="T- s5",
			)
		elif kind == "Discharged (Left, Close Visit)":
			visit = make_visit(patient=mother).name
			engine.transition(
				visit, "close_visit", system=True, closure_reason="Left before being seen", comment="T- s5"
			)
		else:
			raise AssertionError(kind)
		return visit, referral

	def visit_status_name(self, kind: str) -> str:
		return {
			"Registered": c.VISIT_STATUS_REGISTERED,
			"In Triage": c.VISIT_STATUS_IN_TRIAGE,
			"In Consultation": c.VISIT_STATUS_IN_CONSULTATION,
			"Awaiting Laboratory": c.VISIT_STATUS_AWAITING_LAB,
			"Awaiting Pharmacy": c.VISIT_STATUS_AWAITING_PHARMACY,
			"Referred": c.VISIT_STATUS_REFERRED,
		}.get(kind, c.VISIT_STATUS_DISCHARGED if kind.startswith("Discharged") else c.VISIT_STATUS_ROUTED)

	def admissions_of(self, patient: str) -> list[str]:
		return frappe.get_all(MAT, filters={"patient": patient}, pluck="name", order_by="creation asc")

	# -- partograph ------------------------------------------------------------------------------------
	def rows(self, partograph: str) -> list[tuple]:
		doc = frappe.get_doc(PTG, partograph)
		return [(r.name, *(str(r.get(f)) for f in ptg.ROW_FIELDS)) for r in doc.observations]

	# -- deliveries ------------------------------------------------------------------------------------
	@staticmethod
	def baby(outcome: str = LIVE, sex: str = "Female") -> dict:
		return {"outcome": outcome, "sex": sex if outcome == LIVE else None, "weight_kg": 3.0, "apgar_1": 8}

	def draft(self, admission: str, babies: list[dict] | None = None) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": DR,
					"admission": admission,
					"delivered_at": now_datetime(),
					"delivery_mode": c.DELIVERY_SVD,
					"babies": babies if babies is not None else [self.baby()],
				}
			)
			.insert()
			.name
		)

	def submit(self, delivery: str):
		doc = frappe.get_doc(DR, delivery)
		doc.submit()
		return doc

	def newborns(self, mother: str) -> list[str]:
		return frappe.get_all(
			"Patient", filters={c.PATIENT_MOTHER_FIELD: mother}, pluck="name", order_by="creation asc"
		)

	def offered_onload(self, doctype: str, name: str, actor: str, key: str):
		"""What the form shows `actor` (onload), or None when the actor cannot open the record."""
		if not has(actor, READERS):
			return None
		with as_user(self.u(actor)):
			doc = frappe.get_doc(doctype, name)
			doc.run_method("onload")
			return doc.get_onload().get(key)


# =====================================================================================================
# 1. Maternity Admission lifecycle
# =====================================================================================================


class TestAdmissionLifecycle(P5Case):
	def test_transition_table_matches_contract(self):
		self.assertEqual(set(adm.TRANSITIONS), set(EXPECTED_ADMISSION))
		for action, rule in adm.TRANSITIONS.items():
			from_statuses, to_status, roles = EXPECTED_ADMISSION[action]
			self.assertEqual(
				(set(rule.from_statuses), rule.to_status, SYSTEM if rule.is_system else set(rule.roles)),
				(set(from_statuses), to_status, roles if roles == SYSTEM else set(roles)),
				action,
			)
		self.assertEqual(set(adm.HUMAN_ACTIONS), {"start_labour", "discharge"})
		self.assertEqual(set(c.MATERNITY_ROLES), set(MATERNITY))
		self.assertEqual(set(c.DIED_ROLES), set(DIED))
		self.assertEqual(set(adm.ARCHIVE_ROLES), set(ARCHIVERS))
		self.assertEqual(tuple(c.ADMISSION_OPEN_STATUSES), OPEN)
		self.assertEqual(tuple(c.MATERNITY_DISCHARGE_OUTCOMES), OUTCOMES)
		self.assertEqual(set(ptg.PARTOGRAPH_OPEN_STATUSES), set(PARTOGRAPH_OPEN))
		self.assertEqual(set(dlv.DELIVERY_ADMISSION_STATUSES), set(DELIVERY_OPEN))
		self.assertEqual(set(dlv.DELIVERY_SUBMIT_ROLES), set(DELIVERY_SUBMITTERS))
		# §1 DocPerms: no delete / cancel / amend for anyone; create only for the Midwife and Clinical Officer.
		for doctype in (MAT, PTG, DR):
			for perm in frappe.get_meta(doctype).permissions:
				with self.subTest(doctype=doctype, role=perm.role):
					self.assertFalse(perm.delete or perm.cancel or perm.amend)
					if perm.create:
						self.assertIn(perm.role, ("Midwife", "Clinical Officer"))

	def run_op(self, name: str, op: str, actor: str, status: str):
		kind, action, values = LIFECYCLE_OPS[op]
		if kind == "apply":
			return self.act_as(actor, adm.apply_action, name, action, dict(values))
		if kind == "user_transition":
			return self.act_as(actor, adm.transition, name, action, dict(values))

		def desk_save():
			doc = frappe.get_doc(MAT, name)
			if kind == "archive":
				doc.krcs_archived = 1
			elif kind == "unarchive":
				doc.krcs_archived = 0
			elif kind == "edit_remarks":
				doc.remarks = "T- s5 desk edit"
			elif kind == "edit_status":
				doc.status = ADMITTED if doc.status == DISCHARGED else DISCHARGED
			doc.save()
			return doc

		return self.act_as(actor, desk_save)

	def check_op(self, name, op, actor, status, exp, before, err):
		after = self.adm_state(name)
		msg = f"[{status} / {op} / {actor}]"
		if not isinstance(exp, str):
			self.check_exc(err, exp, msg)
			self.assertEqual(after, before, f"refused action changed the admission {msg}")
			return False
		self.assertIsNone(err, f"expected success, got {err!r} {msg}")
		kind, action, values = LIFECYCLE_OPS[op]
		user = self.u(actor)
		if kind in ("apply", "user_transition"):
			self.assertEqual((after["status"], after["log"]), (exp, before["log"] + 1), msg)
			last = frappe.get_doc(MAT, name).status_log[-1]
			self.assertEqual(
				(last.from_status, last.to_status, last.action, last.user),
				(before["status"], exp, action, user),
				msg,
			)
			if action == "discharge":
				outcome = values["discharge_outcome"]
				doc = frappe.get_doc(MAT, name)
				self.assertEqual(
					(doc.discharge_outcome, doc.death_reason, doc.discharged_by, doc.discharge_condition),
					(outcome, "T- PPH" if outcome == DIED_OUTCOME else None, user, "T- stable"),
					msg,
				)
				self.assertTrue(doc.discharged_at, msg)
				self.assertEqual(after["bed"], VACANT, f"bed after discharge {msg}")
			else:
				self.assertEqual(after["bed"], OCCUPIED, f"bed while open {msg}")
				if action == "record_delivery":
					self.assertIn("T-S5-DEL", last.comment or "", msg)
			return True
		# desk saves
		self.assertEqual((after["status"], after["log"]), (before["status"], before["log"]), msg)
		if kind == "archive":
			self.assertEqual((after["archived"], after["bed"]), (1, VACANT), msg)
		elif kind == "unarchive":
			self.assertEqual(after["archived"], 0, msg)
			self.assertEqual(after["bed"], OCCUPIED if exp in OPEN else VACANT, msg)
		elif kind == "edit_remarks":
			self.assertEqual((after["remarks"], after["bed"]), ("T- s5 desk edit", before["bed"]), msg)
		return True

	def test_lifecycle_matrix_and_offered_actions(self):
		"""Every op x every status x every actor, compared with the contract; then `allowed_actions` (and the
		onload copy the form uses) == the human actions that worked."""
		for status in ADM_STATUSES:
			name, _bed, _mother = self.admission_in(status)
			for actor in ACTORS:
				works = set()
				for op in LIFECYCLE_OPS:
					exp = expect_lifecycle(status, op, actor)
					if exp is None:
						continue
					with self.subTest(status=status, op=op, actor=actor):
						with savepoint("s5_lifecycle"):
							before = self.adm_state(name)
							_out, err = self.run_op(name, op, actor, status)
							ok = self.check_op(name, op, actor, status, exp, before, err)
							if ok and op in ("start_labour", "discharge:Discharged home"):
								works.add(LIFECYCLE_OPS[op][1])
				with self.subTest(status=status, actor=actor, check="offered"):
					doc = frappe.get_doc(MAT, name)
					offered = {a["action"] for a in adm.allowed_actions(doc, self.u(actor))}
					self.assertEqual(offered, works)
					shown = self.offered_onload(MAT, name, actor, "allowed_actions")
					if shown is not None:
						self.assertEqual({a["action"] for a in shown}, works)
					died = self.offered_onload(MAT, name, actor, "died_roles")
					if died is not None:
						self.assertEqual(set(died), set(DIED))

	def test_record_delivery_system_event_matrix(self):
		"""record_delivery as the Delivery Record submit runs it (system): Admitted / In Labour only, never
		archived, exactly once; the bed stays occupied."""
		for status in ADM_STATUSES:
			name, _bed, _mother = self.admission_in(status)
			exp = DELIVERED if status in (ADMITTED, IN_LABOUR) else IllegalTransitionError
			with self.subTest(status=status), savepoint("s5_rd"):
				before = self.adm_state(name)
				_out, err = _try(adm.record_delivery, name, "T-S5-SYS")
				after = self.adm_state(name)
				if exp == DELIVERED:
					self.assertIsNone(err, repr(err))
					self.assertEqual(
						(after["status"], after["log"], after["bed"]),
						(DELIVERED, before["log"] + 1, OCCUPIED),
					)
					self.assertEqual(self.delivery_log_rows(name), 1)
					_out, err2 = _try(adm.record_delivery, name, "T-S5-SYS-2")  # a second delivery
					self.check_exc(err2, IllegalTransitionError)
					self.assertEqual(self.delivery_log_rows(name), 1)
				else:
					self.check_exc(err, exp)
					self.assertEqual(after, before)

	def test_died_only_for_died_roles(self):
		"""Discharge as Died: only a role in both MATERNITY_ROLES (may discharge) and DIED_ROLES (may record a
		death). The Mental Health Officer is a DIED role without the right to discharge."""
		name, bed, _mother = self.admission_in(IN_LABOUR)
		for actor in ACTORS:
			may = has(actor, MATERNITY) and has(actor, DIED)
			with self.subTest(actor=actor), savepoint("s5_died"):
				_out, err = self.act_as(
					actor, adm.apply_action, name, "discharge", _discharge_values(DIED_OUTCOME)
				)
				if may:
					self.assertIsNone(err, repr(err))
					doc = frappe.get_doc(MAT, name)
					self.assertEqual(
						(doc.status, doc.discharge_outcome, doc.death_reason),
						(DISCHARGED, DIED_OUTCOME, "T- PPH"),
					)
					self.assertEqual(self.bed_status(bed), VACANT)
				else:
					self.check_exc(err, frappe.PermissionError)
					self.assertEqual(self.bed_status(bed), OCCUPIED)

	def test_admit_matrix_by_prior_status(self):
		"""admit (endpoint) for a patient with no admission / an admission in each status, by every actor: a
		second open (not archived) admission is refused; the beds and the new visit follow the result."""
		for prior in (None, *ADM_STATUSES):
			if prior is None:
				mother, old, old_bed = self.mother(), None, None
			else:
				old, old_bed, mother = self.admission_in(prior)
			visit = routed_visit(c.DEPT_MATERNITY, patient=mother).name
			# A freed bed is reused; an open admission's bed stays with it, so use a new one there.
			bed = old_bed if prior in (DISCHARGED, ARCHIVED) else make_bed(self.ward)
			for actor in ACTORS:
				exp = expect_admit(prior, actor)
				with self.subTest(prior=prior, actor=actor), savepoint("s5_admit"):
					v_before = self.visit_snap(visit)
					old_before = self.adm_state(old, bed=False) if old else None
					bed_before = self.bed_status(bed)
					count = len(self.admissions_of(mother))
					out, err = self.act_as(actor, admit_mother, patient=mother, visit=visit, bed=bed)
					if exp == ADMITTED:
						self.assertIsNone(err, repr(err))
						self.assertEqual((out["status"], out["visit"], out["bed"]), (ADMITTED, visit, bed))
						self.assertEqual(self.bed_status(bed), OCCUPIED)
						v = self.visit_snap(visit)
						self.assertEqual(
							(v["status"], v["outcome"], v["dept"], v["closed"], v["last_action"]),
							(c.VISIT_STATUS_DISCHARGED, c.OUTCOME_ADMITTED, c.DEPT_MATERNITY, True, "admit"),
						)
						self.assertEqual(len(self.admissions_of(mother)), count + 1)
						doc = frappe.get_doc(MAT, out["name"])
						self.assertEqual(
							(doc.admitted_by, doc.status_log[0].action, doc.status_log[0].to_status),
							(self.u(actor), "admit", ADMITTED),
						)
					else:
						self.check_exc(err, exp)
						self.assertEqual(self.visit_snap(visit), v_before)
						self.assertEqual(self.bed_status(bed), bed_before)
						self.assertEqual(len(self.admissions_of(mother)), count)
					if old:
						self.assertEqual(
							self.adm_state(old, bed=False), old_before
						)  # the old admission untouched

	def test_unarchive_rules(self):
		"""Unarchive (Medical In-Charge): an open admission takes its bed back; refused while the patient has
		another open admission or the bed is taken; a Discharged admission's bed stays vacant."""
		# Discharged then archived / unarchived: bed stays vacant.
		name, bed, _m = self.admission_in(DISCHARGED)
		self.set_archived(name, 1)
		self.assertEqual(self.bed_status(bed), VACANT)
		self.set_archived(name, 0)
		self.assertEqual((self.status_of(name), self.bed_status(bed)), (DISCHARGED, VACANT))
		# Delivered archived: bed freed; unarchived: bed occupied again.
		name, bed, _m = self.admission_in(DELIVERED)
		self.set_archived(name, 1)
		self.assertEqual(self.bed_status(bed), VACANT)
		self.set_archived(name, 0)
		self.assertEqual((self.status_of(name), self.bed_status(bed)), (DELIVERED, OCCUPIED))
		# Bed taken by another mother meanwhile: restore refused, nothing changes.
		name, bed, _m = self.admission_in(ARCHIVED)
		other_mother = self.mother()
		other_visit = routed_visit(c.DEPT_MATERNITY, patient=other_mother).name
		with as_user(self.u("midwife")):
			admit_mother(patient=other_mother, visit=other_visit, bed=bed)
		with savepoint("s5_unarchive_bed"):
			_out, err = _try(self.set_archived, name, 0)
			self.check_exc(err, adm.BedUnavailableError)
			self.assertEqual((self.status_of(name), self.bed_status(bed)), (ARCHIVED, OCCUPIED))
		# The same patient re-admitted meanwhile: restore refused.
		name, bed, mother = self.admission_in(ARCHIVED)
		visit = routed_visit(c.DEPT_MATERNITY, patient=mother).name
		with as_user(self.u("midwife")):
			new = admit_mother(patient=mother, visit=visit)["name"]
		_out, err = _try(self.set_archived, name, 0)
		self.check_exc(err, OpenAdmissionExistsError)
		self.assertEqual(
			(self.status_of(name), self.bed_status(bed), self.status_of(new)), (ARCHIVED, VACANT, ADMITTED)
		)

	def test_status_log_and_never_deleted(self):
		name, bed, _m = self.admission_in(DELIVERED)
		for actor in (ADMIN, "mic", "midwife"):
			with self.subTest(actor=actor), savepoint("s5_del"):
				_out, err = self.act_as(actor, frappe.delete_doc, MAT, name)
				self.check_exc(err, frappe.PermissionError)
				self.assertTrue(frappe.db.exists(MAT, name))
				self.assertEqual(self.bed_status(bed), OCCUPIED)
		_out, err = _try(frappe.delete_doc, MAT, name, ignore_permissions=True)
		self.check_exc(err, frappe.PermissionError)
		# Hand-made admission (desk New / REST insert) refused for every actor.
		mother = self.mother()
		for actor in ACTORS:
			with self.subTest(actor=actor, path="insert"), savepoint("s5_ins"):
				_out, err = self.act_as(
					actor,
					lambda: frappe.get_doc(
						{"doctype": MAT, "patient": mother, "facility": self.facility, "status": ADMITTED}
					).insert(),
				)
				self.check_exc(err, frappe.PermissionError)
				self.assertEqual(self.admissions_of(mother), [])


# =====================================================================================================
# 2. Patient Visit x admit
# =====================================================================================================


class TestVisitAdmit(P5Case):
	def check_admit(self, kind, expected, actor, visit, referral, mother, bed, v_before, out, err):
		msg = f"[{kind} / {actor}]"
		if not has(actor, MATERNITY):
			self.check_exc(err, frappe.PermissionError, msg)
			expected = None
		elif expected == REFUSE:
			self.check_exc(err, MaternityAdmissionError, msg)
			self.assertNotIsInstance(err, OpenAdmissionExistsError, msg)
		if expected in (None, REFUSE):
			self.assertEqual(self.visit_snap(visit), v_before, msg)
			self.assertEqual(self.admissions_of(mother), [], msg)
			self.assertEqual(self.bed_status(bed), VACANT, msg)
			if referral:
				self.assertEqual(frappe.db.get_value(c.REFERRAL_DOCTYPE, referral, "status"), "Sent", msg)
			return False
		self.assertIsNone(err, f"expected success, got {err!r} {msg}")
		self.assertEqual((out["visit"], out["status"], out["bed"]), (visit, ADMITTED, bed), msg)
		self.assertEqual(self.bed_status(bed), OCCUPIED, msg)
		doc = frappe.get_doc(MAT, out["name"])
		self.assertEqual((doc.visit, doc.patient, doc.admitted_by), (visit, mother, self.u(actor)), msg)
		v = self.visit_snap(visit)
		if expected == CLOSE:
			self.assertEqual(
				(
					v["status"],
					v["outcome"],
					v["outcome_by"],
					v["dept"],
					v["closed"],
					v["log"],
					v["last_action"],
				),
				(
					c.VISIT_STATUS_DISCHARGED,
					c.OUTCOME_ADMITTED,
					self.u(actor),
					c.DEPT_MATERNITY,
					True,
					v_before["log"] + 1,
					"admit",
				),
				msg,
			)
		else:  # LINK: the closed visit is linked as it is
			self.assertEqual(v, v_before, msg)
			self.assertEqual(
				(v["status"], v["outcome"]), (c.VISIT_STATUS_DISCHARGED, c.OUTCOME_ADMITTED), msg
			)
		if referral:
			self.assertEqual(frappe.db.get_value(c.REFERRAL_DOCTYPE, referral, "status"), "Received", msg)
		return True

	def visit_offer(self, actor: str, visit: str) -> bool:
		"""The Patient Visit form's "Admit to Maternity" button (get_visit_state.can_admit)."""
		state, err = self.act_as(actor, adm.get_visit_state, visit)
		return bool(state and state["can_admit"]) if err is None else False

	def test_admit_from_every_visit_status(self):
		"""admit(visit=...) from every visit status by every actor (contract §5 / §7), the visit's resulting
		status / outcome / department, and the Patient Visit form's offer == what works."""
		for kind, expected in VISIT_KINDS.items():
			mother = self.mother()
			visit, referral = self.visit_kind(kind, mother)
			self.assertEqual(
				frappe.db.get_value(c.VISIT_DOCTYPE, visit, "status"), self.visit_status_name(kind)
			)
			bed = make_bed(self.ward)
			for actor in ACTORS:
				with self.subTest(kind=kind, actor=actor), savepoint("s5_visit"):
					offered = self.visit_offer(actor, visit)
					v_before = self.visit_snap(visit)
					out, err = self.act_as(actor, admit_mother, patient=mother, visit=visit, bed=bed)
					if kind in DEFECT_VISIT_KINDS and has(actor, MATERNITY):
						works = err is None  # behaviour: DEFECT-P5-1 test
					else:
						works = self.check_admit(
							kind, expected, actor, visit, referral, mother, bed, v_before, out, err
						)
					self.assertEqual(offered, works, f"offered != works [{kind} / {actor}]")

	# DEFECT-P5-1: admit accepts a visit routed to another department (fixed in the Phase 5M fix round)
	def test_defect_p5_1_admit_from_visit_routed_elsewhere(self):
		"""DEFECT-P5-1: contract §7 - with `visit`, it must be "routed to Maternity" (or Registered / In Triage,
		§5, or closed with outcome Admitted). `_close_visit_for_admission` accepts any Routed to Department
		visit, so a midwife can take a woman out of the Outpatient (or Mental Health) queue and close that visit
		as Admitted; `get_visit_state` offers the button there too."""
		mother = self.mother()
		visit = routed_visit(c.DEPT_OUTPATIENT, patient=mother).name
		before = self.visit_snap(visit)
		offered = self.visit_offer("midwife", visit)
		_out, err = self.act_as("midwife", admit_mother, patient=mother, visit=visit)
		self.assertEqual(
			(offered, isinstance(err, MaternityAdmissionError), self.visit_snap(visit)), (False, True, before)
		)

	def test_fast_path_from_every_open_visit_status(self):
		"""admit without `visit` (Admit in Labour): the patient's open visit is used (closed with admit) when it
		is waiting, refused when a consultation is running, a new visit opened and closed when there is none."""
		for kind, expected in FAST_PATH_KINDS.items():
			mother = self.mother()
			visit = None
			if kind == "no open visit":
				closed, _ref = self.visit_kind("Discharged (Left, Close Visit)", mother)
			else:
				visit, _ref = self.visit_kind(kind, mother)
			for actor in ("midwife", "clinician", "mic", ADMIN, "his", "reception"):
				with self.subTest(kind=kind, actor=actor), savepoint("s5_fast"):
					v_before = self.visit_snap(visit) if visit else None
					n_visits = frappe.db.count(c.VISIT_DOCTYPE, {"patient": mother})
					out, err = self.act_as(actor, admit_mother, patient=mother)
					msg = f"[{kind} / {actor}]"
					if not has(actor, MATERNITY):
						self.check_exc(err, frappe.PermissionError, msg)
					elif expected == REFUSE:
						self.check_exc(err, MaternityAdmissionError, msg)
					if not has(actor, MATERNITY) or expected == REFUSE:
						self.assertEqual(frappe.db.count(c.VISIT_DOCTYPE, {"patient": mother}), n_visits, msg)
						if visit:
							self.assertEqual(self.visit_snap(visit), v_before, msg)
						self.assertEqual(self.admissions_of(mother), [], msg)
						continue
					self.assertIsNone(err, f"{err!r} {msg}")
					if expected == "new":
						self.assertEqual(
							frappe.db.count(c.VISIT_DOCTYPE, {"patient": mother}), n_visits + 1, msg
						)
						self.assertNotEqual(out["visit"], closed, msg)
					else:
						self.assertEqual(
							(out["visit"], frappe.db.count(c.VISIT_DOCTYPE, {"patient": mother})),
							(visit, n_visits),
							msg,
						)
					v = self.visit_snap(out["visit"])
					self.assertEqual(
						(v["status"], v["outcome"], v["dept"], v["last_action"]),
						(c.VISIT_STATUS_DISCHARGED, c.OUTCOME_ADMITTED, c.DEPT_MATERNITY, "admit"),
						msg,
					)
					# the Patient form offered "Admit in Labour" (no open admission, maternity role)
					state, _e = self.act_as(actor, adm.get_patient_state, mother)
					self.assertIsNotNone(state, msg)

	# DEFECT-P5-3: "Admit in Labour" offered while the open visit is in consultation, then refused (fixed in the Phase 5M fix round)
	def test_defect_p5_3_patient_form_offers_admit_during_consultation(self):
		"""DEFECT-P5-3: `get_patient_state.can_admit` only checks the role (and the JS the open admission), so the
		Patient form offers "Admit in Labour" while the patient's open visit is In Consultation / Awaiting
		Laboratory / Awaiting Pharmacy; the fast path then uses that visit and refuses (MaternityAdmissionError).
		Offered != works (the S1-S3 rule)."""
		broken = []
		for kind in ("In Consultation", "Awaiting Laboratory", "Awaiting Pharmacy"):
			mother = self.mother()
			self.visit_kind(kind, mother)
			state, _e = self.act_as("midwife", adm.get_patient_state, mother)
			with savepoint("s5_p53"):
				_out, err = self.act_as("midwife", admit_mother, patient=mother)
			if state["can_admit"] and not state["admission"] and err is not None:
				broken.append(f"{kind}: offered but {err!r}")
		self.assertEqual(broken, [])

	def test_second_admission_refusals(self):
		"""One open admission per patient; a visit carries one admission, ever; the refusals leave the second
		visit, the beds and the first admission untouched."""
		mother = self.mother()
		v1 = routed_visit(c.DEPT_MATERNITY, patient=mother).name
		bed1, bed2 = make_bed(self.ward), make_bed(self.ward)
		with as_user(self.u("midwife")):
			a1 = admit_mother(patient=mother, visit=v1, bed=bed1)["name"]
		v2 = routed_visit(c.DEPT_MATERNITY, patient=mother).name
		for status in (ADMITTED, IN_LABOUR, DELIVERED):
			if status != ADMITTED:
				with as_user(self.u("midwife")):
					if status == IN_LABOUR:
						adm.apply_action(a1, "start_labour")
				if status == DELIVERED:
					adm.record_delivery(a1, "T-S5-DEL")
			for actor in ("midwife", "clinician", "mic", ADMIN):
				for label, kw in (
					("same visit", {"visit": v1, "bed": bed2}),
					("new visit", {"visit": v2, "bed": bed2}),
					("fast path", {"bed": bed2}),
				):
					with self.subTest(status=status, actor=actor, path=label), savepoint("s5_second"):
						before = (self.visit_snap(v1), self.visit_snap(v2), self.adm_state(a1))
						n_visits = frappe.db.count(c.VISIT_DOCTYPE, {"patient": mother})
						_out, err = self.act_as(actor, admit_mother, patient=mother, **kw)
						self.check_exc(err, OpenAdmissionExistsError)
						self.assertEqual(
							(self.visit_snap(v1), self.visit_snap(v2), self.adm_state(a1)), before
						)
						self.assertEqual(frappe.db.count(c.VISIT_DOCTYPE, {"patient": mother}), n_visits)
						self.assertEqual((self.bed_status(bed1), self.bed_status(bed2)), (OCCUPIED, VACANT))
						self.assertEqual(self.admissions_of(mother), [a1])
			with self.subTest(status=status, check="offers"):
				self.assertFalse(self.visit_offer("midwife", v2))
				self.assertFalse(self.visit_offer("midwife", v1))
				state, _e = self.act_as("midwife", adm.get_patient_state, mother)
				self.assertEqual(state["admission"], a1)
		# Discharged: the first visit can never carry a second admission; a new visit can.
		with as_user(self.u("midwife")):
			adm.apply_action(a1, "discharge", {"discharge_outcome": c.MATERNITY_DISCHARGED_HOME})
		self.assertEqual(self.bed_status(bed1), VACANT)
		_out, err = self.act_as("midwife", admit_mother, patient=mother, visit=v1, bed=bed1)
		self.check_exc(err, OpenAdmissionExistsError)
		self.assertEqual(self.bed_status(bed1), VACANT)
		self.assertFalse(self.visit_offer("midwife", v1))
		self.assertTrue(self.visit_offer("midwife", v2))
		with as_user(self.u("midwife")):
			a2 = admit_mother(patient=mother, visit=v2, bed=bed1)["name"]
		self.assertEqual((self.bed_status(bed1), self.status_of(a2)), (OCCUPIED, ADMITTED))

	def test_visit_of_another_patient_refused(self):
		mother, other = self.mother(), self.mother()
		visit = routed_visit(c.DEPT_MATERNITY, patient=other).name
		before = self.visit_snap(visit)
		_out, err = self.act_as("midwife", admit_mother, patient=mother, visit=visit)
		self.check_exc(err, MaternityAdmissionError)
		self.assertEqual(self.visit_snap(visit), before)
		self.assertEqual(self.admissions_of(mother), [])


# =====================================================================================================
# 3. Partograph
# =====================================================================================================


def expect_partograph(status: str, actor: str, exists: bool = True):
	"""add_observation (exists) / get_or_create on a new admission: role, then the admission's status."""
	if not has(actor, MATERNITY):
		return frappe.PermissionError
	if status == ARCHIVED or status not in PARTOGRAPH_OPEN:
		return PartographError
	return "ok"


class TestPartographMachine(P5Case):
	def partograph_in(self, status: str) -> tuple[str, str]:
		name, _bed, _m = self.admit_new()
		with as_user(self.u("midwife")):
			p = ptg.get_or_create(name)
			ptg.add_observation(p, {"foetal_heart_rate": 140, "cervical_dilatation_cm": 4})
		self.move(name, status)
		return name, p

	def chart_offer(self, actor: str, partograph: str) -> bool:
		chart, err = self.act_as(actor, ptg.get_chart, partograph)
		return bool(chart and chart["can_add"]) if err is None else False

	def test_add_observation_matrix(self):
		for status in ADM_STATUSES:
			_name, p = self.partograph_in(status)
			for actor in ACTORS:
				exp = expect_partograph(status, actor)
				with self.subTest(status=status, actor=actor), savepoint("s5_ptg"):
					offered = self.chart_offer(actor, p)
					before = self.rows(p)
					out, err = self.act_as(
						actor, ptg.add_observation, p, {"foetal_heart_rate": 150, "cervical_dilatation_cm": 5}
					)
					after = self.rows(p)
					if exp == "ok":
						self.assertIsNone(err, repr(err))
						self.assertEqual(after[: len(before)], before)  # earlier rows untouched
						self.assertEqual(len(after), len(before) + 1)
						row = frappe.get_doc(PTG, p).observations[-1]
						self.assertEqual((row.name, row.recorded_by), (out["row"], self.u(actor)))
					else:
						self.check_exc(err, exp)
						self.assertEqual(after, before)
					self.assertEqual(offered, exp == "ok", "get_chart.can_add != works")  # DEFECT-P5-2 fixed

	# DEFECT-P5-2: get_chart offers "Add hourly observation" on an archived admission (fixed in the Phase 5M fix round)
	def test_defect_p5_2_can_add_on_archived_admission(self):
		"""DEFECT-P5-2: `partograph.get_chart` computes `can_add` from the admission status, write permission and
		the role, but not `krcs_archived`; on an archived (Admitted / In Labour) admission the bedside dialog's
		button is offered and `add_observation` then refuses (PartographError "archived")."""
		_name, p = self.partograph_in(ARCHIVED)
		offered = self.chart_offer("midwife", p)
		with savepoint("s5_p52"):
			_out, err = self.act_as("midwife", ptg.add_observation, p, {"foetal_heart_rate": 150})
		self.assertEqual(offered, err is None)

	def test_get_or_create_matrix(self):
		"""get_or_create on an admission without a partograph; the admission form's offer
		(onload.can_open_partograph) == what works."""
		for status in ADM_STATUSES:
			name, _bed, _m = self.admission_in(status)
			for actor in ACTORS:
				exp = expect_partograph(status, actor, exists=False)
				with self.subTest(status=status, actor=actor), savepoint("s5_goc"):
					offered = bool(self.offered_onload(MAT, name, actor, "can_open_partograph"))
					out, err = self.act_as(actor, ptg.get_or_create, name)
					count = frappe.db.count(PTG, {"admission": name})
					if exp == "ok":
						self.assertIsNone(err, repr(err))
						self.assertEqual((count, frappe.db.get_value(PTG, out, "admission")), (1, name))
					else:
						self.check_exc(err, exp)
						self.assertEqual(count, 0)
					self.assertEqual(offered, exp == "ok", "onload.can_open_partograph != works")

	def test_rows_append_only_through_every_path(self):
		"""Desk save (edit / remove / reorder / append a row, computed fields), REST set_value / save / delete on
		the child row and on the parent, a second partograph, a hand-made one with rows: all refused for every
		writer (and Administrator), whatever the admission's status; the rows stay identical."""
		for status in (IN_LABOUR, DELIVERED, DISCHARGED):
			name, p = self.partograph_in(IN_LABOUR)
			with as_user(self.u("midwife")):
				ptg.add_observation(p, {"foetal_heart_rate": 138, "cervical_dilatation_cm": 6})
			if status != IN_LABOUR:
				self.move_from_labour(name, status)
			first_row = frappe.get_doc(PTG, p).observations[0].name

			def edit_row():
				doc = frappe.get_doc(PTG, p)
				doc.observations[0].foetal_heart_rate = 99
				doc.save()

			def edit_note():
				doc = frappe.get_doc(PTG, p)
				doc.observations[0].note = "T- rewritten"
				doc.save()

			def remove_row():
				doc = frappe.get_doc(PTG, p)
				doc.observations = doc.observations[1:]
				doc.save()

			def reorder_rows():
				doc = frappe.get_doc(PTG, p)
				doc.observations = list(reversed(doc.observations))
				for i, r in enumerate(doc.observations, 1):
					r.idx = i
				doc.save()

			def append_row():
				doc = frappe.get_doc(PTG, p)
				doc.append("observations", {"observed_at": now_datetime(), "foetal_heart_rate": 150})
				doc.save()

			def edit_computed():
				doc = frappe.get_doc(PTG, p)
				doc.started_at = None
				doc.alert_line_crossed = 1
				doc.save()

			def rest_set_value_child():
				frappe_client.set_value(PTG_ROW, first_row, "foetal_heart_rate", 99)

			def rest_save_parent():
				d = frappe.get_doc(PTG, p).as_dict()
				d["observations"][0]["cervical_dilatation_cm"] = "2"
				frappe_client.save(d)

			def child_save():
				row = frappe.get_doc(PTG_ROW, first_row)
				row.foetal_heart_rate = 99
				row.save()

			def child_delete():
				frappe_client.delete(PTG_ROW, first_row)

			def parent_delete():
				frappe_client.delete(PTG, p)

			def second_partograph():
				frappe_client.insert({"doctype": PTG, "admission": name})

			paths = (
				edit_row,
				edit_note,
				remove_row,
				reorder_rows,
				append_row,
				edit_computed,
				rest_set_value_child,
				rest_save_parent,
				child_save,
				child_delete,
				parent_delete,
				second_partograph,
			)
			for actor in ("midwife", "clinician", "mic", ADMIN, "his"):
				for path in paths:
					with self.subTest(status=status, actor=actor, path=path.__name__), savepoint("s5_append"):
						before = self.rows(p)
						_out, err = self.act_as(actor, path)
						self.assertIsNotNone(err, "path succeeded")
						if has(actor, WRITERS) and path.__name__ in (
							"edit_row",
							"edit_note",
							"remove_row",
							"reorder_rows",
							"append_row",
							"edit_computed",
							"rest_set_value_child",
							"rest_save_parent",
						):
							self.check_exc(err, PartographLockedError)
						self.assertEqual(self.rows(p), before)
						self.assertEqual(frappe.db.count(PTG, {"admission": name}), 1)
		# A hand-made partograph carrying rows (another admission) is refused.
		other, _bed, _m = self.admit_new()
		for actor in ("midwife", ADMIN):
			with self.subTest(path="insert with rows", actor=actor), savepoint("s5_ptg_rows"):
				_out, err = self.act_as(
					actor,
					lambda: frappe.get_doc(
						{
							"doctype": PTG,
							"admission": other,
							"observations": [{"observed_at": now_datetime(), "foetal_heart_rate": 140}],
						}
					).insert(),
				)
				self.check_exc(err, PartographLockedError)
				self.assertEqual(frappe.db.count(PTG, {"admission": other}), 0)

	def move_from_labour(self, name: str, status: str) -> None:
		if status == DELIVERED:
			adm.record_delivery(name, "T-S5-DEL-0")
		elif status == DISCHARGED:
			with as_user(self.u("midwife")):
				adm.apply_action(name, "discharge", {"discharge_outcome": c.MATERNITY_DISCHARGED_HOME})
		self.assertEqual(self.status_of(name), status)

	def test_correction_is_a_new_row(self):
		"""A lower dilatation is refused without a note; with a note it is a new row, the original kept."""
		_name, p = self.partograph_in(IN_LABOUR)
		before = self.rows(p)
		_out, err = self.act_as("midwife", ptg.add_observation, p, {"cervical_dilatation_cm": 3})
		self.check_exc(err, ImplausiblePartographValueError)
		self.assertEqual(self.rows(p), before)
		_out, err = self.act_as(
			"midwife", ptg.add_observation, p, {"cervical_dilatation_cm": 3, "note": "T- correction"}
		)
		self.assertIsNone(err, repr(err))
		after = self.rows(p)
		self.assertEqual((after[: len(before)], len(after)), (before, len(before) + 1))


# =====================================================================================================
# 4. Delivery Record
# =====================================================================================================


def expect_submit(status: str, actor: str):
	if not has(actor, DELIVERY_SUBMITTERS):
		return frappe.PermissionError
	if status == ARCHIVED or status not in DELIVERY_OPEN:
		return DeliveryRecordError
	return DELIVERED


class TestDeliveryMachine(P5Case):
	def test_submit_matrix(self):
		"""Submit a draft Delivery Record x admission status x actor; the admission form's offer
		(onload.can_record_delivery) == a new record can be created and submitted by that actor."""
		for status in ADM_STATUSES:
			name, bed, mother = self.admit_new()
			draft = self.draft(name)
			if status == IN_LABOUR:
				self.move(name, IN_LABOUR)
			elif status == DELIVERED:
				with as_user(self.u("midwife")):
					self.submit(self.draft(name))
			elif status in (DISCHARGED, ARCHIVED):
				self.move(name, status)
			self.assertEqual(self.status_of(name), status)
			first_babies = len(self.newborns(mother))
			for actor in ACTORS:
				exp = expect_submit(status, actor)
				with self.subTest(status=status, actor=actor), savepoint("s5_dr"):
					offered = bool(self.offered_onload(MAT, name, actor, "can_record_delivery"))
					before = self.adm_state(name)
					log_rows = self.delivery_log_rows(name)
					_out, err = self.act_as(actor, self.submit, draft)
					if exp == DELIVERED:
						self.assertIsNone(err, repr(err))
						after = self.adm_state(name)
						self.assertEqual(frappe.db.get_value(DR, draft, "docstatus"), 1)
						self.assertEqual(
							(after["status"], after["log"], after["bed"], self.delivery_log_rows(name)),
							(DELIVERED, before["log"] + 1, OCCUPIED, 1),
						)
						last = frappe.get_doc(MAT, name).status_log[-1]
						self.assertEqual((last.from_status, last.action), (status, "record_delivery"))
						self.assertIn(draft, last.comment)
						self.assertEqual(len(self.newborns(mother)), 1)
						works = True
					else:
						self.check_exc(err, exp)
						self.assertEqual(frappe.db.get_value(DR, draft, "docstatus"), 0)
						self.assertEqual(self.adm_state(name), before)
						self.assertEqual(self.delivery_log_rows(name), log_rows)
						self.assertEqual(len(self.newborns(mother)), first_babies)
						works = False
					# the form offers "Record Delivery" (a new record) when the actor could create and submit one
					self.assertEqual(offered, works, "onload.can_record_delivery != works")
			self.assertEqual(self.bed_status(bed), OCCUPIED if status in OPEN else VACANT)

	def test_one_submitted_per_admission_and_no_cancel_or_amend(self):
		name, bed, mother = self.admit_new()
		d1, d2 = self.draft(name), self.draft(name)
		with as_user(self.u("midwife")):
			self.submit(d1)
		self.assertEqual((self.status_of(name), self.delivery_log_rows(name)), (DELIVERED, 1))
		kids = self.newborns(mother)
		self.assertEqual(len(kids), 1)
		for actor in ("midwife", "clinician", ADMIN):
			with self.subTest(actor=actor, path="second submit"), savepoint("s5_second_dr"):
				_out, err = self.act_as(actor, self.submit, d2)
				self.check_exc(err, DeliveryRecordError)
			with self.subTest(actor=actor, path="new draft"), savepoint("s5_new_dr"):
				_out, err = self.act_as(actor, self.draft, name)
				self.check_exc(err, DeliveryRecordError)

		def cancel():
			frappe.get_doc(DR, d1).cancel()

		def amend():
			new = frappe.copy_doc(frappe.get_doc(DR, d1))
			new.amended_from = d1
			new.insert()
			new.submit()

		def edit_after_submit():
			doc = frappe.get_doc(DR, d1)
			doc.delivery_mode = c.DELIVERY_CAESAREAN
			doc.save()

		def forge_addendum():
			doc = frappe.get_doc(DR, d1)
			doc.addendum = "T- forged"
			doc.save()

		def delete():
			frappe_client.delete(DR, d1)

		for actor in ("midwife", "clinician", "mic", ADMIN, "his"):
			for path in (cancel, amend, edit_after_submit, forge_addendum, delete):
				with self.subTest(actor=actor, path=path.__name__), savepoint("s5_cancel"):
					_out, err = self.act_as(actor, path)
					self.assertIsNotNone(err, "path succeeded")
					self.assertEqual(
						frappe.db.get_value(DR, d1, ["docstatus", "delivery_mode"]), (1, c.DELIVERY_SVD)
					)
					self.assertEqual(frappe.db.count(DR, {"admission": name, "docstatus": 1}), 1)
					self.assertFalse(frappe.db.get_value(DR, d1, "addendum"))
					self.assertEqual((self.status_of(name), self.delivery_log_rows(name)), (DELIVERED, 1))
					self.assertEqual(self.newborns(mother), kids)
		# The documented correction path: add_addendum (append-only).
		with as_user(self.u("mic")):
			dlv.add_addendum(d1, "T- weight was 3.2 kg")
		self.assertIn("T- weight was 3.2 kg", frappe.db.get_value(DR, d1, "addendum"))
		# Discharge ends the stay; still one delivery.
		with as_user(self.u("midwife")):
			adm.apply_action(name, "discharge", {"discharge_outcome": c.MATERNITY_DISCHARGED_HOME})
		self.assertEqual(
			(self.status_of(name), self.delivery_log_rows(name), self.bed_status(bed)),
			(DISCHARGED, 1, VACANT),
		)

	def test_newborns_by_baby_outcomes(self):
		"""One Patient per live birth (twins / triplets share name, DOB and sex), none for a stillbirth; the
		admission ends Delivered exactly once whatever the outcomes."""
		cases = {
			"single live": [LIVE],
			"twins": [LIVE, LIVE],
			"triplets": [LIVE, LIVE, LIVE],
			"fresh stillbirth": [FRESH],
			"macerated stillbirth": [MACERATED],
			"live + fresh stillbirth": [LIVE, FRESH],
			"stillborn twins": [FRESH, MACERATED],
		}
		for label, outcomes in cases.items():
			with self.subTest(case=label):
				name, _bed, mother = self.admission_in(IN_LABOUR)
				d = self.draft(name, [self.baby(o) for o in outcomes])
				with as_user(self.u("clinician")):
					doc = self.submit(d)
				kids = self.newborns(mother)
				self.assertEqual(len(kids), outcomes.count(LIVE))
				rows = frappe.get_doc(DR, d).babies
				self.assertEqual([bool(r.newborn_patient) for r in rows], [o == LIVE for o in outcomes])
				self.assertEqual(sorted(r.newborn_patient for r in rows if r.newborn_patient), sorted(kids))
				self.assertEqual([r.newborn_patient for r in doc.babies], [r.newborn_patient for r in rows])
				for kid in kids:
					p = frappe.get_doc("Patient", kid)
					self.assertEqual((p.first_name, p.get(c.PATIENT_MOTHER_FIELD)), ("Baby", mother))
					self.assertEqual(
						[(r.relation, r.patient) for r in p.patient_relation], [("Mother", mother)]
					)
				self.assertEqual((self.status_of(name), self.delivery_log_rows(name)), (DELIVERED, 1))

	def test_newborn_registration_idempotent(self):
		"""register_newborns again (in memory and from the database), the submit hooks re-run, submit again:
		no second Patient, no second record_delivery; only the submitting roles may run it."""
		name, _bed, mother = self.admission_in(ADMITTED)
		d = self.draft(name, [self.baby(LIVE, "Female"), self.baby(LIVE, "Female")])
		with as_user(self.u("midwife")):
			self.submit(d)
		kids = self.newborns(mother)
		self.assertEqual(len(kids), 2)

		def again_in_memory():
			return dlv.register_newborns(frappe.get_doc(DR, d))

		def again_cleared():
			doc = frappe.get_doc(DR, d)
			for r in doc.babies:
				r.newborn_patient = None  # the database still has the link
			return dlv.register_newborns(doc)

		def hooks_again():
			doc = frappe.get_doc(DR, d)
			doc.run_method("on_submit")

		def submit_again():
			frappe.get_doc(DR, d).submit()

		for actor in ("midwife", "clinician", ADMIN):
			for fn in (again_in_memory, again_cleared):
				with self.subTest(actor=actor, path=fn.__name__), savepoint("s5_idem"):
					out, err = self.act_as(actor, fn)
					self.assertIsNone(err, repr(err))
					self.assertEqual(out, kids)
					self.assertEqual(self.newborns(mother), kids)
			for fn in (hooks_again, submit_again):
				with self.subTest(actor=actor, path=fn.__name__), savepoint("s5_idem"):
					_out, _err = self.act_as(actor, fn)  # refused or a no-op: either way nothing new
					self.assertEqual(self.newborns(mother), kids)
					self.assertEqual((self.status_of(name), self.delivery_log_rows(name)), (DELIVERED, 1))
		for actor in ("mic", "his", "reception"):
			with self.subTest(actor=actor, path="register_newborns"), savepoint("s5_idem_role"):
				_out, err = self.act_as(actor, again_cleared)
				self.check_exc(err, frappe.PermissionError)
				self.assertEqual(self.newborns(mother), kids)
		# A draft is never a source of newborns.
		other, _b, other_mother = self.admission_in(ADMITTED)
		draft = frappe.get_doc(DR, self.draft(other))
		_out, err = _try(dlv.register_newborns, draft)
		self.check_exc(err, DeliveryRecordError)
		self.assertEqual(self.newborns(other_mother), [])

	def test_forged_newborn_link_ignored(self):
		"""A draft carrying `newborn_patient` (read-only, set by the server) does not skip registration."""
		name, _bed, mother = self.admission_in(IN_LABOUR)
		doc = frappe.get_doc(
			{
				"doctype": DR,
				"admission": name,
				"delivered_at": now_datetime(),
				"delivery_mode": c.DELIVERY_SVD,
				"babies": [{**self.baby(), "newborn_patient": mother}],
			}
		).insert()
		self.assertFalse(doc.babies[0].newborn_patient)
		with as_user(self.u("midwife")):
			self.submit(doc.name)
		kids = self.newborns(mother)
		self.assertEqual(len(kids), 1)
		self.assertEqual(
			frappe.db.get_value(c.DELIVERY_BABY_DOCTYPE, doc.babies[0].name, "newborn_patient"), kids[0]
		)
		self.assertNotEqual(kids[0], mother)
