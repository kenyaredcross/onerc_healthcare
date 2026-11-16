# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Maternity Admission: admission, lifecycle, beds and ward observations (Phase 5M M1; SDD §5.12.3, INP-12..18).

Contract: `docs/MATERNITY_CONTRACT.md` §5-§7. Status changes only through this module (D8): a module-level
`TRANSITIONS` dict and the append-only `status_log`.

| Action | From -> To | Who |
|---|---|---|
| `admit` | (new) -> Admitted | `MATERNITY_ROLES` (`admit`) |
| `start_labour` | Admitted -> In Labour | `MATERNITY_ROLES` |
| `record_delivery` | Admitted / In Labour -> Delivered | system: Delivery Record submit (M3) |
| `discharge` | Admitted / In Labour / Delivered -> Discharged | `MATERNITY_ROLES`; outcome Died needs `DIED_ROLES` and a cause |

**Admit** (`admit`, POST), one transaction, lock order Patient -> Visit -> (Referral) -> Admission -> Bed:
- with `visit`: the patient's visit, either open in Registered / In Triage / Routed to Department **Maternity**
  (the engine's `admit` closes it: Discharged, outcome Admitted, department Maternity), or already closed with
  outcome Admitted by a clinician's Complete Consultation (not by Close Visit), or by our own `admit` for an
  admission since archived (linked as it is, no transition). A visit routed to another department, In
  Consultation / Awaiting Laboratory / Awaiting Pharmacy is refused: the clinician completes the consultation
  with outcome Admitted, or refers the patient to Maternity internally (`visit_admit_refusal` gives the reason;
  `get_visit_state` / `get_patient_state` offer the buttons by the same rule);
- without `visit` (fast path, active labour): the patient's open visit is used when she has one (a requested
  facility other than that visit's is refused); otherwise a
  Patient Visit is inserted at the facility (`values["facility"]`, else the user's default facility, else the
  patient's registration facility) with every visit rule (attendance, queue number, one open visit) and then
  closed with `admit`;
- a Sent internal referral that created the visit (`referred_from`) is marked Received;
- refused while the patient has another open (not archived) admission, or when the visit already has one that
  is not archived;
- gravida (this pregnancy included) is required, >= 1; para "P" or "P+A" with P + A <= gravida - 1;
- dating by `edd_basis` (`set_pregnancy_dates`): LMP (default) -> EDD = LMP + 280, a typed EDD more than 14 days
  away refused; Ultrasound / Clinical estimate -> the typed EDD is kept and the gestation counted from it;
- ward: the facility's Maternity Ward (`maternity.setup.ward_for_facility`) unless given; bed optional: a Vacant
  leaf of type Bed directly under that ward, set Occupied by direct write (never by saving the unit: Marley's
  validate resets occupancy).

**Discharge** records outcome, condition, notes, cause of death (Died only), time and user, and frees the bed.
A Discharged admission is read-only, except `krcs_archived` (Medical In-Charge). Correction of a wrong admission
is archiving: an archived admission no longer counts as open, its bed is freed and its visit may carry a new
admission. Nothing is ever deleted. A superseded Delivery Record (`maternity.delivery.supersede`) adds a
`correct_delivery` log row (`log_delivery_correction`; status unchanged, not in TRANSITIONS).

**Ward observations** are Vital Signs with `krcs_maternity_admission` (no `krcs_visit`: the visit closed at
admission). `record_observation` inserts and submits one with the user's own permissions;
`maternity.vital_signs.validate_ward_observation` (Vital Signs validate hook) checks the link.
"""

from __future__ import annotations

from datetime import date, timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, nowdate, nowtime, today

from onerc_healthcare.constants import (
	ADMISSION_ADMITTED,
	ADMISSION_DELIVERED,
	ADMISSION_DISCHARGED,
	ADMISSION_IN_LABOUR,
	ADMISSION_OPEN_STATUSES,
	DELIVERY_RECORD_DOCTYPE,
	DEPT_MATERNITY,
	DIED_ROLES,
	MATERNITY_ADMISSION_DOCTYPE,
	MATERNITY_DIED,
	MATERNITY_DISCHARGE_OUTCOMES,
	MATERNITY_ROLES,
	OUTCOME_ADMITTED,
	PARTOGRAPH_DOCTYPE,
	REFERRAL_DOCTYPE,
	ROLE_MIC,
	VISIT_DOCTYPE,
	VISIT_STATUS_DISCHARGED,
	VISIT_STATUS_IN_TRIAGE,
	VISIT_STATUS_REGISTERED,
	VISIT_STATUS_ROUTED,
	VITAL_SIGNS_ADMISSION_FIELD,
)
from onerc_healthcare.privacy.lifecycle import (
	IllegalTransitionError,
	LockedFieldError,
	Transition,
	check_from_status,
	clean_text,
	guard_fields,
	guard_log,
	has_any_role,
)

ACTION_ADMIT = "admit"
ACTION_START_LABOUR = "start_labour"
ACTION_RECORD_DELIVERY = "record_delivery"
ACTION_DISCHARGE = "discharge"
# Log only, not a transition (the status stays): a submitted Delivery Record superseded by the Medical In-Charge.
ACTION_CORRECT_DELIVERY = "correct_delivery"

TRANSITIONS: dict[str, Transition] = {
	ACTION_ADMIT: Transition(_("Admit"), (), ADMISSION_ADMITTED, tuple(MATERNITY_ROLES)),
	ACTION_START_LABOUR: Transition(
		_("Start Labour"), (ADMISSION_ADMITTED,), ADMISSION_IN_LABOUR, tuple(MATERNITY_ROLES)
	),
	ACTION_RECORD_DELIVERY: Transition(
		_("Record Delivery"), (ADMISSION_ADMITTED, ADMISSION_IN_LABOUR), ADMISSION_DELIVERED
	),
	ACTION_DISCHARGE: Transition(
		_("Discharge"),
		tuple(ADMISSION_OPEN_STATUSES),
		ADMISSION_DISCHARGED,
		tuple(MATERNITY_ROLES),
		required=("discharge_outcome",),
	),
}
# Actions a user may run through `apply_action` (admit has its own endpoint; record_delivery is a system event).
HUMAN_ACTIONS = (ACTION_START_LABOUR, ACTION_DISCHARGE)

# Visit statuses the engine's `admit` may close from here (the other open statuses have a consultation running).
ADMITTABLE_VISIT_STATUSES = (VISIT_STATUS_REGISTERED, VISIT_STATUS_IN_TRIAGE, VISIT_STATUS_ROUTED)

LOG_TABLE = "status_log"
ARCHIVED_FIELD = "krcs_archived"
ARCHIVE_FIELDS = (ARCHIVED_FIELD, "krcs_archive_section")
ARCHIVE_ROLES = (ROLE_MIC,)
BED_UNIT_TYPE = "Bed"
HSU = "Healthcare Service Unit"

# Set by `admit` (and the lifecycle actions) only.
SERVER_FIELDS = (
	"status",
	"visit",
	"facility",
	"ward",
	"bed",
	"admitted_at",
	"admitted_by",
	"discharge_outcome",
	"discharge_condition",
	"death_reason",
	"discharged_at",
	"discharged_by",
	"discharge_notes",
)
# Admission fields the admit form / dialog may send in `values` (everything clinical; no server field).
INPUT_FIELDS = (
	"husband_name",
	"employer",
	"safe_motherhood_no",
	"gravida",
	"para",
	"anc_clinic",
	"lmp",
	"edd_basis",
	"edd",
	"medical_history",
	"family_history",
	"transfusion_history",
	"blood_group",
	"rhesus",
	"syphilis_rpr",
	"blood_group_result",
	"rhesus_result",
	"syphilis_result",
	"fundal_height_cm",
	"presentation",
	"engagement",
	"position",
	"foetal_heart_rate",
	"urine",
	"haemoglobin",
	"bp_systolic",
	"bp_diastolic",
	"temperature",
	"pulse",
	"respiratory_rate",
	"weight_kg",
	"vaginal_discharge",
	"genitals",
	"spleen",
	"abnormalities",
	"remarks",
)
INPUT_TABLES = {
	"obstetric_history": (
		"year",
		"place",
		"labour_duration",
		"delivery_type",
		"birth_weight_kg",
		"sex",
		"alive_or_dead",
		"feeding",
		"puerperium",
		"pelvic_assessment",
	),
	"diagnoses": ("code", "case_type", "is_primary", "note"),
}
DISCHARGE_INPUTS = ("discharge_outcome", "discharge_condition", "discharge_notes", "death_reason", "comment")

# Pregnancy dating (Naegele: EDD = LMP + 280 days).
NAEGELE_DAYS = 280
EDD_BASIS_LMP = "LMP"
EDD_BASIS_ULTRASOUND = "Ultrasound"
EDD_BASIS_CLINICAL = "Clinical estimate"
EDD_BASES = (EDD_BASIS_LMP, EDD_BASIS_ULTRASOUND, EDD_BASIS_CLINICAL)
# With basis LMP the EDD is LMP + 280; a typed EDD further than this from it needs another basis.
EDD_LMP_TOLERANCE_DAYS = 14
MAX_GESTATION_DAYS = 45 * 7  # beyond 45 weeks the LMP / EDD is a typing error
# Plausible (not normal) admission examination ranges; temperature, pulse, respiration, BP and weight reuse
# the triage ranges of visit/events.py.
FHR_RANGE = (60, 220)  # beats/min, as the partograph (M2)
FUNDAL_HEIGHT_RANGE = (5, 60)  # cm
HAEMOGLOBIN_RANGE = (2, 25)  # g/dL
BIRTH_WEIGHT_RANGE = (0.3, 7)  # kg (Obstetric History)
EARLIEST_PREGNANCY_YEAR = 1950
LAB_RESULT_FIELDS = ("blood_group_result", "rhesus_result", "syphilis_result")


class MaternityAdmissionError(frappe.ValidationError):
	"""The admission breaks a maternity rule (visit, open admission, ward, bed, values)."""


class BedUnavailableError(MaternityAdmissionError):
	"""The bed is occupied, not a bed, or not in the admission's ward."""


class OpenAdmissionExistsError(MaternityAdmissionError):
	"""The patient already has an open Maternity Admission."""


class ImplausibleAdmissionValueError(MaternityAdmissionError):
	"""An examination value, the pregnancy dates or gravida / para are not plausible."""


# ============================================================================ pregnancy dating


def edd_from_lmp(lmp) -> date:
	"""Naegele: the expected date of delivery is LMP + 280 days (calendar days, leap years included)."""
	return getdate(lmp) + timedelta(days=NAEGELE_DAYS)


def gestation_days(lmp=None, edd=None, on=None) -> int | None:
	"""Completed days of gestation on `on` (default today): from the LMP, else from the EDD (280 - days left)."""
	on = getdate(on or today())
	if lmp:
		return (on - getdate(lmp)).days
	if edd:
		return NAEGELE_DAYS - (getdate(edd) - on).days
	return None


def gestation(lmp=None, edd=None, on=None) -> tuple[int, int] | None:
	"""(weeks, days) of gestation on `on`, e.g. (39, 1); None without LMP or EDD."""
	days = gestation_days(lmp, edd, on)
	if days is None:
		return None
	return divmod(days, 7)


def admission_gestation_days(doc, on=None) -> int | None:
	"""Gestation of the admission on `on` (default: the admission date) by its EDD basis: from the EDD when
	there is one (with basis LMP the EDD is LMP + 280, so this equals the LMP count), else from the LMP."""
	on = on or _admission_date(doc)
	if doc.get("edd"):
		return gestation_days(None, doc.edd, on)
	return gestation_days(doc.get("lmp"), None, on)


def gestation_label(doc) -> str | None:
	"""'39+1 weeks' at admission, for the form (the same rule as `gestation_weeks`)."""
	days = admission_gestation_days(doc)
	ga = divmod(days, 7) if days is not None else None
	if not ga or ga[0] < 0:
		return None
	return _("{0}+{1} weeks").format(ga[0], ga[1])


def _admission_date(doc) -> date:
	return getdate(doc.admitted_at) if doc.get("admitted_at") else getdate(today())


def parse_para(value) -> tuple[int, int] | None:
	"""'2' -> (2, 0); '2+1' -> (2, 1) (births at or after viability + abortions). None when empty; raises when
	the text is not that form."""
	text = (str(value) if value is not None else "").replace(" ", "")
	if not text:
		return None
	parts = text.split("+")
	if len(parts) > 2 or not all(p.isdigit() for p in parts):
		frappe.throw(
			_("Para must be a number, or births + abortions such as 2+1; got {0}.").format(
				frappe.bold(value)
			),
			ImplausibleAdmissionValueError,
			title=_("Check the pregnancy details"),
		)
	return int(parts[0]), int(parts[1]) if len(parts) == 2 else 0


# ============================================================================ controller (validate)


class _DiagnosisRows:
	"""Adapter: `clinical.diagnosis.validate_diagnoses` reads `krcs_diagnoses`; the admission's table is
	`diagnoses` (the same EMR Encounter Diagnosis child)."""

	def __init__(self, doc):
		self._doc = doc

	def get(self, fieldname, default=None):
		from onerc_healthcare.clinical.diagnosis import DIAGNOSES_FIELD

		if fieldname == DIAGNOSES_FIELD:
			return self._doc.get("diagnoses")
		return self._doc.get(fieldname, default)


def before_insert(doc) -> None:
	"""No hand-made admissions (contract §5): only `admit` (and the W0 test factory) insert one."""
	if not (doc.flags.from_admit or doc.flags.from_factory):
		frappe.throw(
			_(
				"Admit the mother with Admit to Maternity (Patient Visit) or Admit in Labour (Patient), or use the"
				" Admit button on a new Maternity Admission."
			),
			frappe.PermissionError,
			title=_("Use Admit"),
		)


def validate_admission(doc) -> None:
	"""Maternity Admission validate: lifecycle guards, then the clinical values."""
	before = None if doc.is_new() else doc.get_doc_before_save()
	if before is not None:
		guard_log(doc, before, LOG_TABLE)
		if _guard_update(doc, before):
			return  # archive-only change
	set_pregnancy_dates(doc, before)
	validate_gravida_para(doc, before)
	validate_examination(doc)
	validate_obstetric_history(doc)
	validate_lab_links(doc)
	if doc.get("diagnoses"):
		from onerc_healthcare.clinical.diagnosis import validate_diagnoses

		validate_diagnoses(_DiagnosisRows(doc))


def _guard_update(doc, before) -> bool:
	"""Server fields and status only through this module; a Discharged admission is read-only; archiving is
	the Medical In-Charge's. Returns True when the save changes only the archive flag."""
	from onerc_healthcare.clinical.referral import changed_fields

	guard_fields(doc, before, SERVER_FIELDS)
	if doc.flags.from_workflow:
		return False
	changed = changed_fields(doc, before)
	tables = [t for t in INPUT_TABLES if _table_key(doc, t) != _table_key(before, t)]
	archive_changed = ARCHIVED_FIELD in changed
	if archive_changed and not has_any_role(ARCHIVE_ROLES):
		frappe.throw(
			_("Only the Medical In-Charge can archive or restore a Maternity Admission."),
			LockedFieldError,
			title=_("Not Allowed"),
		)
	others = [f for f in changed if f not in ARCHIVE_FIELDS] + tables
	if archive_changed and others:
		frappe.throw(
			_("Archive or restore the admission on its own, without other changes."),
			LockedFieldError,
			title=_("Not Editable"),
		)
	if others and before.status == ADMISSION_DISCHARGED:
		frappe.throw(
			_("Maternity Admission {0} is discharged and can no longer be edited.").format(doc.name),
			LockedFieldError,
			title=_("Not Editable"),
		)
	if archive_changed:
		_check_restore(doc)
		return True
	return False


def _table_key(doc, table: str) -> list[tuple]:
	fields = INPUT_TABLES[table]
	return [tuple(_plain(row.get(f)) for f in fields) for row in doc.get(table) or []]


def _plain(value):
	if isinstance(value, float) and value == int(value):
		return int(value)
	return value if value not in ("", None) else None


def _check_restore(doc) -> None:
	"""Un-archiving an open admission: the patient must have no other open admission and the bed must be free."""
	if cint(doc.get(ARCHIVED_FIELD)) or doc.status not in ADMISSION_OPEN_STATUSES:
		return
	other = open_admission(doc.patient, exclude=doc.name, for_update=True)
	if other:
		frappe.throw(
			_("Patient {0} has another open admission {1}; it cannot be restored.").format(
				frappe.bold(doc.patient_name or doc.patient), frappe.bold(other)
			),
			OpenAdmissionExistsError,
		)
	if doc.bed:
		occupy_bed(doc.bed, doc.name)


def on_admission_update(doc) -> None:
	"""Archiving an open admission frees its bed (the mother no longer occupies it on record)."""
	before = doc.get_doc_before_save()
	if (
		before is not None
		and doc.bed
		and cint(doc.get(ARCHIVED_FIELD))
		and not cint(before.get(ARCHIVED_FIELD))
		and doc.status in ADMISSION_OPEN_STATUSES
	):
		free_bed(doc.bed)


def _dating_changed(doc, before) -> bool:
	if before is None:
		return True
	return any(
		(getdate(doc.get(f)) if doc.get(f) else None) != (getdate(before.get(f)) if before.get(f) else None)
		for f in ("lmp", "edd")
	) or (doc.get("edd_basis") or EDD_BASIS_LMP) != (before.get("edd_basis") or EDD_BASIS_LMP)


def set_pregnancy_dates(doc, before=None) -> None:
	"""The EDD by its basis, then gestation (completed weeks) at the admission date (`admission_gestation_days`).

	- Basis LMP (the default): the EDD is always LMP + 280 (Naegele), recomputed whenever the LMP changes. A
	  typed EDD more than `EDD_LMP_TOLERANCE_DAYS` from LMP + 280 is refused: the EDD then comes from an
	  ultrasound or a clinical estimate, and the basis must say so. Without an LMP a typed EDD is kept.
	- Basis Ultrasound / Clinical estimate: the typed EDD is kept (required) and the gestation is computed from
	  it (280 - days to the EDD), whatever the LMP.

	The rules run on a new admission and whenever the LMP, the EDD or the basis changes; a save that leaves
	them alone keeps the stored dates (an admission dated before these rules is not rewritten)."""
	basis = doc.get("edd_basis") or EDD_BASIS_LMP
	if basis not in EDD_BASES:
		_implausible(_("The EDD basis must be one of {0}.").format(", ".join(_(b) for b in EDD_BASES)))
	doc.edd_basis = basis
	adm_date = _admission_date(doc)
	if _dating_changed(doc, before):
		lmp = getdate(doc.lmp) if doc.lmp else None
		if lmp and lmp > adm_date:
			_implausible(_("The LMP ({0}) cannot be after the admission date.").format(_fmt_date(lmp)))
		if basis == EDD_BASIS_LMP and lmp:
			computed = edd_from_lmp(lmp)
			typed = getdate(doc.edd) if doc.edd else None
			edd_typed = typed and (
				before is None or not before.get("edd") or typed != getdate(before.get("edd"))
			)
			if edd_typed and abs((typed - computed).days) > EDD_LMP_TOLERANCE_DAYS:
				_implausible(
					_(
						"The EDD {0} is {1} days from the date given by the LMP ({2} = LMP + 280 days). If the EDD"
						" comes from an ultrasound or a clinical estimate, set the EDD basis to {3} or {4};"
						" otherwise correct the LMP or clear the EDD."
					).format(
						_fmt_date(typed),
						abs((typed - computed).days),
						_fmt_date(computed),
						frappe.bold(_(EDD_BASIS_ULTRASOUND)),
						frappe.bold(_(EDD_BASIS_CLINICAL)),
					)
				)
			doc.edd = computed
		elif basis != EDD_BASIS_LMP and not doc.edd:
			_implausible(
				_("Enter the EDD given by the {0}, or set the EDD basis to LMP.").format(_(basis).lower())
			)
		if lmp and doc.edd and getdate(doc.edd) <= lmp:
			_implausible(_("The EDD must be after the LMP."))
	days = admission_gestation_days(doc, adm_date)
	if days is None:
		doc.gestation_weeks = None
		return
	if days < 0 or days > MAX_GESTATION_DAYS:
		_implausible(
			_(
				"The LMP / EDD give a gestation of {0} weeks at admission; expected 0 to {1}. Check the dates."
			).format(days // 7, MAX_GESTATION_DAYS // 7)
		)
	doc.gestation_weeks = days // 7


def validate_gravida_para(doc, before=None) -> None:
	"""Gravida counts every pregnancy including this one: required (>= 1) at admission and never cleared once
	entered. Para is "P" or "P+A" (births at or after viability + abortions), all from earlier pregnancies,
	so P + A <= gravida - 1."""
	gravida = cint(doc.gravida)
	if gravida < 0:
		_implausible(_("Gravida cannot be negative."))
	required = (doc.is_new() and doc.flags.from_admit) or (before is not None and cint(before.get("gravida")))
	para = parse_para(doc.para)
	if gravida < 1 and (required or para is not None):
		_implausible(
			_("Enter gravida: the number of pregnancies including this one (1 for a first pregnancy).")
		)
	if para is None:
		doc.para = None
		return
	doc.para = f"{para[0]}+{para[1]}" if para[1] else str(para[0])
	if sum(para) > gravida - 1:
		_implausible(
			_(
				"Para {0} counts {1} earlier pregnancies, but gravida {2} leaves {3} before this one"
				" (para births + abortions must be at most gravida - 1)."
			).format(frappe.bold(doc.para), sum(para), gravida, gravida - 1)
		)


def _ranges() -> list[tuple[str, float, float, str, bool]]:
	"""(fieldname, low, high, unit, whole) for the admission examination."""
	from onerc_healthcare.visit.events import NUMERIC_VITALS, TEXT_VITALS

	rows = [(v.fieldname, v.low, v.high, v.unit, v.whole_number) for v in TEXT_VITALS]
	weight = next(v for v in NUMERIC_VITALS if v.fieldname == "weight")
	rows.append(("weight_kg", weight.low, weight.high, weight.unit, False))
	rows += [
		("foetal_heart_rate", *FHR_RANGE, "beats/min", True),
		("fundal_height_cm", *FUNDAL_HEIGHT_RANGE, "cm", False),
		("haemoglobin", *HAEMOGLOBIN_RANGE, "g/dL", False),
	]
	return rows


def validate_examination(doc) -> None:
	"""Plausible ranges (0 or empty = not recorded); BP both or neither, systolic above diastolic."""
	errors = []
	label = doc.meta.get_label
	for fieldname, low, high, unit, _whole in _ranges():
		value = flt(doc.get(fieldname))
		if not value:
			continue
		if fieldname == "temperature" and 77 <= value <= 113:
			errors.append(
				_("{0} of {1} looks like degrees Fahrenheit. Enter it in °C, for example 36.8.").format(
					frappe.bold(_(label(fieldname))), value
				)
			)
		elif not low <= value <= high:
			errors.append(
				_("{0} of {1} {2} is not plausible. Expected between {3} and {4} {2}.").format(
					frappe.bold(_(label(fieldname))), value, unit, low, high
				)
			)
	systolic, diastolic = cint(doc.bp_systolic), cint(doc.bp_diastolic)
	if bool(systolic) != bool(diastolic):
		errors.append(_("Enter both systolic and diastolic blood pressure, or neither."))
	elif systolic and systolic <= diastolic:
		errors.append(
			_("Systolic blood pressure ({0}) must be higher than diastolic ({1}).").format(
				systolic, diastolic
			)
		)
	if errors:
		frappe.throw(errors, ImplausibleAdmissionValueError, title=_("Check the examination"), as_list=True)


def validate_obstetric_history(doc) -> None:
	this_year = getdate(today()).year
	low, high = BIRTH_WEIGHT_RANGE
	for row in doc.get("obstetric_history") or []:
		prefix = _("Previous pregnancies row {0}").format(row.idx)
		year = cint(row.year)
		if year and not EARLIEST_PREGNANCY_YEAR <= year <= this_year:
			_implausible(
				_("{0}: the year {1} is not plausible (expected {2} to {3}).").format(
					prefix, year, EARLIEST_PREGNANCY_YEAR, this_year
				)
			)
		weight = flt(row.birth_weight_kg)
		if weight and not low <= weight <= high:
			_implausible(
				_("{0}: a birth weight of {1} kg is not plausible (expected {2} to {3} kg).").format(
					prefix, weight, low, high
				)
			)


def validate_lab_links(doc) -> None:
	"""A linked laboratory result must be the mother's own."""
	for fieldname in LAB_RESULT_FIELDS:
		name = doc.get(fieldname)
		if not name:
			continue
		patient = frappe.db.get_value("Observation", name, "patient")
		if patient != doc.patient:
			_implausible(
				_("{0}: result {1} belongs to another patient.").format(
					_(doc.meta.get_label(fieldname)), frappe.bold(name)
				)
			)


def _implausible(message: str) -> None:
	frappe.throw(message, ImplausibleAdmissionValueError, title=_("Check the admission"))


def _fmt_date(value) -> str:
	return frappe.format(value, {"fieldtype": "Date"})


# ============================================================================ helpers: open admission, beds


def open_admission(patient: str, exclude: str | None = None, for_update: bool = False) -> str | None:
	"""The patient's open (not archived) Maternity Admission, or None. `for_update`: a locking read (after the
	Patient / Visit locks, D41)."""
	rows = frappe.db.sql(
		f"""SELECT name FROM `tab{MATERNITY_ADMISSION_DOCTYPE}`
		WHERE patient = %(patient)s AND status IN %(open)s AND IFNULL({ARCHIVED_FIELD}, 0) = 0
			AND name != %(exclude)s
		ORDER BY creation ASC LIMIT 1{" FOR UPDATE" if for_update else ""}""",
		{"patient": patient, "open": list(ADMISSION_OPEN_STATUSES), "exclude": exclude or ""},
	)
	return rows[0][0] if rows else None


def _bed_holder(bed: str, exclude: str | None = None) -> str | None:
	"""Another open, not archived admission occupying `bed`."""
	rows = frappe.db.sql(
		f"""SELECT name FROM `tab{MATERNITY_ADMISSION_DOCTYPE}`
		WHERE bed = %(bed)s AND status IN %(open)s AND IFNULL({ARCHIVED_FIELD}, 0) = 0 AND name != %(exclude)s
		LIMIT 1""",
		{"bed": bed, "open": list(ADMISSION_OPEN_STATUSES), "exclude": exclude or ""},
	)
	return rows[0][0] if rows else None


def _lock_unit(name: str) -> dict | None:
	rows = frappe.db.sql(
		"""SELECT name, is_group, parent_healthcare_service_unit AS parent, service_unit_type, occupancy_status
		FROM `tabHealthcare Service Unit` WHERE name = %s FOR UPDATE""",
		(name,),
		as_dict=True,
	)
	return rows[0] if rows else None


def check_bed(bed: str, ward: str | None, lock: bool = False) -> dict:
	"""The bed row when `bed` is a leaf of type Bed directly under `ward`; raises BedUnavailableError."""
	row = (
		_lock_unit(bed)
		if lock
		else frappe.db.get_value(
			HSU,
			bed,
			[
				"name",
				"is_group",
				"parent_healthcare_service_unit as parent",
				"service_unit_type",
				"occupancy_status",
			],
			as_dict=True,
		)
	)
	if not row:
		frappe.throw(_("Bed {0} does not exist.").format(frappe.bold(bed)), BedUnavailableError)
	if not ward:
		frappe.throw(_("Choose the ward before the bed."), BedUnavailableError)
	if cint(row.is_group) or row.service_unit_type != BED_UNIT_TYPE or row.parent != ward:
		frappe.throw(
			_("{0} is not a bed of {1}. Choose a Bed unit under that ward.").format(
				frappe.bold(bed), frappe.bold(ward)
			),
			BedUnavailableError,
			title=_("Wrong Bed"),
		)
	return row


def occupy_bed(bed: str, admission: str) -> None:
	"""Lock `bed` (lock order: after the admission), check it is a Vacant bed of the admission's ward and that no
	other open admission holds it, then set it Occupied by direct write."""
	ward = frappe.db.get_value(MATERNITY_ADMISSION_DOCTYPE, admission, "ward")
	row = check_bed(bed, ward, lock=True)
	holder = _bed_holder(bed, exclude=admission)
	if row.occupancy_status == "Occupied" or holder:
		frappe.throw(
			_("Bed {0} is occupied{1}. Choose another bed or admit without a bed.").format(
				frappe.bold(bed), _(" by admission {0}").format(holder) if holder else ""
			),
			BedUnavailableError,
			title=_("Bed Occupied"),
		)
	frappe.db.set_value(HSU, bed, "occupancy_status", "Occupied")


def free_bed(bed: str | None) -> None:
	"""Set `bed` Vacant by direct write, unless another open (not archived) admission holds it."""
	if not bed or not _lock_unit(bed):
		return
	if _bed_holder(bed):
		return
	frappe.db.set_value(HSU, bed, "occupancy_status", "Vacant")


def _check_ward(ward: str, facility: str) -> None:
	row = frappe.db.get_value(HSU, ward, ["is_group", "parent_healthcare_service_unit"], as_dict=True)
	if not row or not cint(row.is_group) or row.parent_healthcare_service_unit != facility:
		frappe.throw(
			_("{0} is not a ward of {1}.").format(frappe.bold(ward), frappe.bold(facility)),
			MaternityAdmissionError,
			title=_("Wrong Ward"),
		)


# ============================================================================ admit


def _require_maternity_role(label: str) -> None:
	if not has_any_role(MATERNITY_ROLES):
		frappe.throw(
			_("You are not allowed to {0}. Required role: {1}.").format(
				label, ", ".join(_(r) for r in MATERNITY_ROLES)
			),
			frappe.PermissionError,
		)


def _parse(values) -> dict:
	if not values:
		return {}
	if isinstance(values, str):
		values = frappe.parse_json(values)
	if not isinstance(values, dict):
		frappe.throw(_("Admission values must be an object."), MaternityAdmissionError)
	return dict(values)


def _clean_values(values: dict) -> dict:
	"""Only clinical input fields (and their child rows); a server field or unknown key is refused."""
	unknown = sorted(k for k in values if k not in INPUT_FIELDS and k not in INPUT_TABLES)
	if unknown:
		frappe.throw(
			_("These fields are not entered at admission: {0}.").format(", ".join(unknown)),
			MaternityAdmissionError,
		)
	out = {k: v for k, v in values.items() if k in INPUT_FIELDS}
	for table, fields in INPUT_TABLES.items():
		rows = values.get(table)
		if rows is None:
			continue
		if not isinstance(rows, list):
			frappe.throw(_("{0} must be a list of rows.").format(table), MaternityAdmissionError)
		out[table] = [{f: row.get(f) for f in fields if f in row} for row in rows if isinstance(row, dict)]
	return out


def default_facility(patient: str) -> str | None:
	"""Fast-path visit facility: the user's default facility, else the patient's registration facility, else the
	site's default registration facility (registration facilities only)."""
	from onerc_healthcare.registration.walk_in import (
		REGISTRATION_FACILITY_FIELD,
		get_default_registration_facility,
		is_registration_facility,
	)

	candidates = (
		frappe.defaults.get_user_default(HSU),
		frappe.db.get_value("Patient", patient, REGISTRATION_FACILITY_FIELD),
		get_default_registration_facility(),
	)
	return next((f for f in candidates if is_registration_facility(f)), None)


def _fast_path_facility(patient: str, requested: str | None) -> str:
	from onerc_healthcare.registration.walk_in import validate_facility

	if requested:
		validate_facility(requested)
		return requested
	facility = default_facility(patient)
	if not facility:
		frappe.throw(
			_("No facility found for the visit. Set the patient's registration facility."),
			MaternityAdmissionError,
		)
	return facility


def _refuse_other_facility(visit: str, facility: str, requested: str | None) -> None:
	"""The fast path uses the patient's open visit, so the admission is at that visit's facility."""
	if requested and requested != facility:
		frappe.throw(
			_(
				"The patient has an open visit {0} at {1}; she is admitted there, not at {2}. Close that visit"
				" first to admit her at another facility."
			).format(frappe.bold(visit), frappe.bold(facility), frappe.bold(requested)),
			MaternityAdmissionError,
			title=_("Wrong Facility"),
		)


def _check_patient(patient: str) -> None:
	from onerc_healthcare.visit.patients import assert_patient_active

	if not patient or not frappe.db.exists("Patient", patient):
		frappe.throw(_("Patient {0} does not exist.").format(patient), MaternityAdmissionError)
	frappe.has_permission("Patient", "read", doc=patient, throw=True)
	assert_patient_active(patient)
	if frappe.db.get_value("Patient", patient, "sex") == "Male":
		frappe.throw(_("A maternity admission is for a woman."), MaternityAdmissionError)


def _refuse_open_admission(patient: str, for_update: bool) -> None:
	existing = open_admission(patient, for_update=for_update)
	if existing:
		frappe.throw(
			_("{0} is already admitted ({1}). Continue that admission, or discharge it first.").format(
				frappe.bold(frappe.db.get_value("Patient", patient, "patient_name") or patient),
				frappe.bold(existing),
			),
			OpenAdmissionExistsError,
			title=_("Already Admitted"),
		)


def _locked_visit(visit: str):
	doc = frappe.get_doc(VISIT_DOCTYPE, visit, for_update=True)
	if frappe.session.user != "Administrator" and not doc.has_permission("read"):
		frappe.throw(_("You do not have access to visit {0}.").format(visit), frappe.PermissionError)
	return doc


def _close_visit_for_admission(visit: str, patient: str):
	"""Lock the visit and close it with the engine's `admit` (open visit) or accept it as already closed by
	the clinician with outcome Admitted. Returns the visit document."""
	from onerc_healthcare.visit import engine

	doc = _locked_visit(visit)
	if doc.patient != patient:
		frappe.throw(_("Visit {0} belongs to another patient.").format(visit), MaternityAdmissionError)
	existing = visit_admission(visit)
	if existing:
		frappe.throw(
			_("Visit {0} already has a maternity admission ({1}).").format(
				frappe.bold(visit), frappe.bold(existing)
			),
			OpenAdmissionExistsError,
			title=_("Already Admitted"),
		)
	refusal = visit_admit_refusal(doc.name, doc.status, doc.current_department, doc.outcome)
	if refusal:
		frappe.throw(refusal, MaternityAdmissionError, title=_("Cannot Admit From This Visit"))
	if doc.status in ADMITTABLE_VISIT_STATUSES:
		return engine.admit_visit(visit, comment=_("Admitted to Maternity"))
	return doc  # closed with outcome Admitted by Complete Consultation (or by an archived admission)


def visit_admission(visit: str) -> str | None:
	"""The visit's maternity admission that is not archived (an archived admission no longer holds its visit)."""
	rows = frappe.db.sql(
		f"""SELECT name FROM `tab{MATERNITY_ADMISSION_DOCTYPE}`
		WHERE visit = %s AND IFNULL({ARCHIVED_FIELD}, 0) = 0 ORDER BY creation ASC LIMIT 1""",
		(visit,),
	)
	return rows[0][0] if rows else None


def _closing_actions(visit: str) -> set[str]:
	return set(
		frappe.get_all(
			"Patient Visit Status Log",
			filters={"parent": visit, "parenttype": VISIT_DOCTYPE, "parentfield": "status_log"},
			pluck="action",
		)
	)


def visit_admit_refusal(visit: str, status: str, department: str | None, outcome: str | None) -> str | None:
	"""Why the visit cannot carry a new admission (None when it can), apart from an existing admission.

	- Registered / In Triage: yes (the fast path, an emergency); Routed to Department: only to Maternity;
	- Discharged with outcome Admitted: only when a clinician closed it with Complete Consultation (not Close
	  Visit), or when our own `admit` closed it for an admission since archived;
	- anything else (a consultation running, referred out, another outcome): no."""
	from onerc_healthcare.visit import engine

	label = frappe.bold(visit)
	if status in (VISIT_STATUS_REGISTERED, VISIT_STATUS_IN_TRIAGE):
		return None
	if status == VISIT_STATUS_ROUTED:
		if department == DEPT_MATERNITY:
			return None
		return _(
			"Visit {0} is routed to {1}. Admit from a visit routed to Maternity: ask triage to re-route the"
			" patient, or the clinician to refer her to Maternity."
		).format(label, frappe.bold(_(department or "")))
	if status == VISIT_STATUS_DISCHARGED and outcome == OUTCOME_ADMITTED:
		actions = _closing_actions(visit)
		if engine.ACTION_CLOSE_VISIT not in actions and (
			engine.ACTION_COMPLETE_CONSULTATION in actions or engine.ACTION_ADMIT in actions
		):
			return None
		return _(
			"Visit {0} was closed with Close Visit, not by a clinician's Complete Consultation. Admit from a new"
			" visit (Admit in Labour on the Patient form)."
		).format(label)
	return _(
		"Visit {0} is {1}. Admit from a visit that is waiting (Registered, In Triage or Routed to Maternity), or"
		" ask the clinician to complete the consultation with outcome Admitted or to refer the patient to"
		" Maternity."
	).format(label, frappe.bold(_(status)))


def _receive_referral(visit_doc) -> str | None:
	"""Mark a Sent internal referral that created this visit Received (contract §5)."""
	from onerc_healthcare.clinical import referral as ref

	name = visit_doc.get("referred_from")
	if not name:
		return None
	row = frappe.db.get_value(REFERRAL_DOCTYPE, name, ["status", "referral_type"], as_dict=True)
	if not row or row.status != ref.STATUS_SENT or row.referral_type != ref.TYPE_INTERNAL:
		return None
	ref.transition(name, ref.ACTION_RECEIVE, system=True)
	return name


def _insert_fast_path_visit(patient: str, facility: str) -> str:
	"""The fast-path visit: inserted with every Patient Visit rule (attendance, queue number, one open visit);
	ignore_permissions only because the Midwife holds no create right on visits (contract §1)."""
	doc = frappe.get_doc({"doctype": VISIT_DOCTYPE, "patient": patient, "facility": facility})
	doc.insert(ignore_permissions=True)
	return doc.name


def _resolve_ward(facility: str, ward: str | None, bed: str | None) -> str | None:
	"""The admission ward (given, else the facility's Maternity Ward), checked with the bed (no lock)."""
	from onerc_healthcare.maternity.setup import ward_for_facility

	ward = ward or ward_for_facility(facility)
	if ward:
		_check_ward(ward, facility)
	if bed:
		row = check_bed(bed, ward)
		if row.occupancy_status == "Occupied" or _bed_holder(bed):
			frappe.throw(
				_("Bed {0} is occupied. Choose another bed or admit without a bed.").format(frappe.bold(bed)),
				BedUnavailableError,
				title=_("Bed Occupied"),
			)
	return ward


@frappe.whitelist(methods=["POST"])
def admit(
	patient: str,
	visit: str | None = None,
	ward: str | None = None,
	bed: str | None = None,
	values: dict | str | None = None,
) -> dict:
	"""Admit `patient` to the labour ward (see the module docstring). Returns {name, status, visit, bed}.

	Atomic for server callers too: on any error everything it wrote (visit, referral, admission, bed) is rolled
	back to a savepoint."""
	from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import find_open_visit

	_require_maternity_role(_("admit to maternity"))
	values = _parse(values)
	requested_facility = values.pop("facility", None)
	values = _clean_values(values)
	ward = (ward or "").strip() or None
	bed = (bed or "").strip() or None
	visit = (visit or "").strip() or None
	_check_patient(patient)

	# Checks before any write, for a clear refusal (repeated under the locks below).
	_refuse_open_admission(patient, for_update=False)
	if visit:
		facility = frappe.db.get_value(VISIT_DOCTYPE, visit, "facility")
		if not facility:
			frappe.throw(_("Patient Visit {0} does not exist.").format(visit), MaternityAdmissionError)
	else:
		waiting = find_open_visit(patient)
		if waiting:
			facility = frappe.db.get_value(VISIT_DOCTYPE, waiting.name, "facility")
			_refuse_other_facility(waiting.name, facility, requested_facility)
		else:
			facility = _fast_path_facility(patient, requested_facility)
	ward = _resolve_ward(facility, ward, bed)

	savepoint = "krcs_maternity_admit"
	frappe.db.savepoint(savepoint)
	try:
		out = _admit_locked(patient, visit, facility, ward, bed, values)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	frappe.db.release_savepoint(savepoint)
	return out


def _admit_locked(
	patient: str, visit: str | None, facility: str, ward: str | None, bed: str | None, values: dict
):
	from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import find_open_visit
	from onerc_healthcare.visit.patients import lock_patient

	# Lock order: Patient -> Visit -> (Referral) -> Maternity Admission -> Bed.
	lock_patient(patient)
	if not visit:
		open_visit = find_open_visit(patient, for_update=True)
		visit = open_visit.name if open_visit else _insert_fast_path_visit(patient, facility)
	visit_doc = _close_visit_for_admission(visit, patient)
	referral = _receive_referral(visit_doc)
	_refuse_open_admission(patient, for_update=True)
	if visit_doc.facility != facility:  # another visit appeared meanwhile: its facility rules
		facility = visit_doc.facility
		ward = _resolve_ward(facility, None, bed)

	doc = frappe.get_doc(
		{
			"doctype": MATERNITY_ADMISSION_DOCTYPE,
			"patient": patient,
			"visit": visit,
			"facility": facility,
			"ward": ward,
			"bed": bed,
			"status": ADMISSION_ADMITTED,
			"admitted_at": now_datetime(),
			"admitted_by": frappe.session.user,
		}
	)
	for key, value in values.items():
		doc.set(key, value)
	comment = _("From visit {0}").format(visit)
	if referral:
		comment += "; " + _("internal referral {0} received").format(referral)
	_append_log(doc, "", ADMISSION_ADMITTED, ACTION_ADMIT, comment)
	doc.flags.from_admit = True
	# ignore_permissions: the Medical In-Charge admits but holds no create right (contract §1); the role and
	# patient checks above stand in for it. Every controller validation still runs.
	doc.insert(ignore_permissions=True)
	if bed:
		occupy_bed(bed, doc.name)  # locks the bed and checks it again
	return {"name": doc.name, "status": doc.status, "visit": visit, "bed": bed}


# ============================================================================ lifecycle


def _append_log(doc, from_status: str, to_status: str, action: str, comment: str | None = None) -> None:
	doc.append(
		LOG_TABLE,
		{
			"from_status": from_status or None,
			"to_status": to_status,
			"action": action,
			"user": frappe.session.user,
			"timestamp": now_datetime(),
			"comment": comment,
		},
	)


def _save(doc) -> None:
	doc.flags.from_workflow = True
	try:
		doc.save(ignore_permissions=True)
	finally:
		doc.flags.from_workflow = False


def _may(rule: Transition, user: str | None = None) -> bool:
	return not rule.is_system and has_any_role(rule.roles, user)


def _check_actor(rule: Transition, doc, system: bool) -> None:
	if system or frappe.session.user == "Administrator":
		return
	if rule.is_system:
		frappe.throw(
			_("{0} is a system event and cannot be performed by a user.").format(frappe.bold(rule.label)),
			frappe.PermissionError,
		)
	if not _may(rule):
		frappe.throw(
			_("You are not allowed to perform {0}. Required role: {1}.").format(
				frappe.bold(rule.label), ", ".join(_(r) for r in rule.roles)
			),
			frappe.PermissionError,
		)
	if not doc.has_permission("read"):
		frappe.throw(_("You do not have access to {0}.").format(doc.name), frappe.PermissionError)


def transition(admission: str, action: str, values: dict | None = None, system: bool = False):
	"""Apply lifecycle `action` to `admission` and return the saved document (locks the admission first)."""
	rule = TRANSITIONS.get(action)
	if not rule or action == ACTION_ADMIT:
		frappe.throw(_("Unknown admission action: {0}").format(action), IllegalTransitionError)
	values = dict(values or {})
	doc = frappe.get_doc(MATERNITY_ADMISSION_DOCTYPE, admission, for_update=True)
	_check_actor(rule, doc, system)
	if cint(doc.get(ARCHIVED_FIELD)):
		frappe.throw(
			_("Maternity Admission {0} is archived.").format(doc.name),
			IllegalTransitionError,
			title=_("Archived"),
		)
	check_from_status(rule, doc)
	comment = clean_text(values.get("comment"), _("comment"))
	from_status = doc.status
	if action == ACTION_DISCHARGE:
		comment = _apply_discharge(doc, values, system) or comment
	elif action == ACTION_RECORD_DELIVERY:
		comment = _apply_delivery(doc, values.get("delivery"))
	doc.status = rule.to_status
	_append_log(doc, from_status, rule.to_status, action, comment)
	_save(doc)
	if action == ACTION_DISCHARGE:
		free_bed(doc.bed)
	return doc


def _apply_discharge(doc, values: dict, system: bool) -> str | None:
	unknown = sorted(k for k in values if k not in DISCHARGE_INPUTS)
	if unknown:
		frappe.throw(
			_("These fields are not part of the discharge: {0}.").format(", ".join(unknown)),
			IllegalTransitionError,
		)
	outcome = (values.get("discharge_outcome") or "").strip()
	if outcome not in MATERNITY_DISCHARGE_OUTCOMES:
		frappe.throw(
			_("Choose the discharge outcome: {0}.").format(
				", ".join(_(o) for o in MATERNITY_DISCHARGE_OUTCOMES)
			),
			IllegalTransitionError,
			title=_("Outcome Required"),
		)
	death_reason = clean_text(values.get("death_reason"), _("cause of death"))
	if outcome == MATERNITY_DIED:
		if not system and not has_any_role(DIED_ROLES):
			frappe.throw(
				_("Only {0} can record a maternal death.").format(", ".join(_(r) for r in DIED_ROLES)),
				frappe.PermissionError,
			)
		if not death_reason:
			frappe.throw(
				_("Enter the cause of death."), IllegalTransitionError, title=_("Cause of Death Required")
			)
	else:
		death_reason = None
	doc.discharge_outcome = outcome
	doc.discharge_condition = clean_text(values.get("discharge_condition"), _("condition at discharge"))
	doc.discharge_notes = clean_text(values.get("discharge_notes"), _("discharge notes"))
	doc.death_reason = death_reason
	doc.discharged_at = now_datetime()
	doc.discharged_by = frappe.session.user
	return _(outcome)


def _apply_delivery(doc, delivery: str | None) -> str | None:
	if any(row.action == ACTION_RECORD_DELIVERY for row in doc.get(LOG_TABLE) or []):
		frappe.throw(
			_("A delivery is already recorded on Maternity Admission {0}.").format(doc.name),
			IllegalTransitionError,
		)
	return _("Delivery Record {0}").format(delivery) if delivery else None


def log_delivery_correction(admission: str, old: str, new: str, reason: str):
	"""Log row `correct_delivery` (status unchanged) when the Medical In-Charge supersedes Delivery Record `old`
	with the draft `new` (`maternity.delivery.supersede`). Server only. Returns the saved admission."""
	doc = frappe.get_doc(MATERNITY_ADMISSION_DOCTYPE, admission, for_update=True)
	comment = _("Delivery Record {0} superseded by {1}: {2}").format(old, new, reason)
	_append_log(doc, doc.status, doc.status, ACTION_CORRECT_DELIVERY, comment)
	_save(doc)
	return doc


def record_delivery(admission: str, delivery: str):
	"""System transition to Delivered, run by the Delivery Record submit (M3). Refuses a non-open admission and
	a second delivery. Returns the saved admission."""
	return transition(admission, ACTION_RECORD_DELIVERY, {"delivery": delivery}, system=True)


@frappe.whitelist(methods=["POST"])
def apply_action(admission: str, action: str, values: dict | str | None = None) -> dict:
	"""Run a user action (`start_labour`, `discharge`) on `admission`. Returns {name, status}."""
	if action not in HUMAN_ACTIONS:
		frappe.throw(
			_("{0} is not an action you can run on an admission.").format(frappe.bold(action)),
			IllegalTransitionError,
		)
	doc = transition(admission, action, _parse(values))
	return {"name": doc.name, "status": doc.status}


def allowed_actions(doc, user: str | None = None) -> list[dict]:
	"""Lifecycle actions the user may run on the form: [{action, label}]."""
	if cint(doc.get(ARCHIVED_FIELD)):
		return []
	return [
		{"action": action, "label": TRANSITIONS[action].label}
		for action in HUMAN_ACTIONS
		if doc.status in TRANSITIONS[action].from_statuses and _may(TRANSITIONS[action], user)
	]


# ============================================================================ form data


def ward_observations(admission: str) -> list[dict] | None:
	"""Submitted and draft ward observations (Vital Signs) of `admission`, oldest first; None without read
	access to Vital Signs."""
	if not frappe.has_permission("Vital Signs", "read"):
		return None
	return frappe.get_list(
		"Vital Signs",
		filters={VITAL_SIGNS_ADMISSION_FIELD: admission, "docstatus": ["<", 2]},
		fields=[
			"name",
			"docstatus",
			"signs_date",
			"signs_time",
			"temperature",
			"pulse",
			"respiratory_rate",
			"bp_systolic",
			"bp_diastolic",
			"vital_signs_note",
			"owner",
		],
		order_by="signs_date asc, signs_time asc, creation asc",
		limit=1000,
	)


def onload(doc) -> None:
	"""Form data: actions, gestation label, ward observations, partograph and delivery links."""
	user = frappe.session.user
	can_act = has_any_role(MATERNITY_ROLES)
	is_open = doc.status in ADMISSION_OPEN_STATUSES and not cint(doc.get(ARCHIVED_FIELD))
	partograph = (
		frappe.db.get_value(PARTOGRAPH_DOCTYPE, {"admission": doc.name}, "name")
		if frappe.db.table_exists(PARTOGRAPH_DOCTYPE)
		else None
	)
	deliveries = (
		frappe.get_all(
			DELIVERY_RECORD_DOCTYPE,
			filters={"admission": doc.name, "docstatus": ["<", 2]},
			fields=["name", "docstatus", ARCHIVED_FIELD],
			order_by="creation asc",
		)
		if frappe.db.table_exists(DELIVERY_RECORD_DOCTYPE)
		else []
	)
	doc.set_onload("allowed_actions", allowed_actions(doc, user))
	doc.set_onload("gestation_label", gestation_label(doc))
	doc.set_onload("observations", ward_observations(doc.name))
	doc.set_onload(
		"can_record_observations",
		is_open
		and frappe.has_permission("Vital Signs", "create")
		and frappe.has_permission("Vital Signs", "submit"),
	)
	doc.set_onload("partograph", partograph)
	doc.set_onload("can_open_partograph", is_open and can_act and doc.status != ADMISSION_DELIVERED)
	doc.set_onload("deliveries", deliveries)
	doc.set_onload(
		"can_record_delivery",
		is_open
		and doc.status in TRANSITIONS[ACTION_RECORD_DELIVERY].from_statuses
		and frappe.has_permission(DELIVERY_RECORD_DOCTYPE, "create")
		if frappe.db.table_exists(DELIVERY_RECORD_DOCTYPE)
		else False,
	)
	doc.set_onload("died_roles", list(DIED_ROLES))


@frappe.whitelist(methods=["POST"])
def record_observation(admission: str, values: dict | str) -> dict:
	"""Insert and submit a ward observation (Vital Signs linked to the admission) with the user's own
	permissions; the Vital Signs hooks (plausible ranges, the admission link) apply. Returns {name}."""
	values = _parse(values)
	allowed = ("temperature", "pulse", "respiratory_rate", "bp_systolic", "bp_diastolic", "vital_signs_note")
	unknown = sorted(k for k in values if k not in allowed)
	if unknown:
		frappe.throw(
			_("Unknown observation fields: {0}.").format(", ".join(unknown)), MaternityAdmissionError
		)
	adm = frappe.db.get_value(
		MATERNITY_ADMISSION_DOCTYPE,
		admission,
		["name", "patient", "facility", "status", ARCHIVED_FIELD],
		as_dict=True,
	)
	if not adm:
		frappe.throw(_("Maternity Admission {0} does not exist.").format(admission), MaternityAdmissionError)
	frappe.has_permission(MATERNITY_ADMISSION_DOCTYPE, "read", doc=admission, throw=True)
	if adm.status not in ADMISSION_OPEN_STATUSES or cint(adm.get(ARCHIVED_FIELD)):
		frappe.throw(
			_(
				"Maternity Admission {0} is {1}; ward observations are recorded while the mother is admitted."
			).format(admission, _("archived") if cint(adm.get(ARCHIVED_FIELD)) else _(adm.status)),
			MaternityAdmissionError,
			title=_("Admission Closed"),
		)
	recorded = [k for k in allowed[:-1] if str(values.get(k) or "").strip()]
	if not recorded:
		frappe.throw(_("Record at least one observation."), MaternityAdmissionError)
	doc = frappe.get_doc(
		{
			"doctype": "Vital Signs",
			"patient": adm.patient,
			VITAL_SIGNS_ADMISSION_FIELD: admission,
			"signs_date": nowdate(),
			"signs_time": nowtime(),
			"company": frappe.db.get_value(HSU, adm.facility, "company"),
			**{k: (str(v).strip() if v not in (None, "") else None) for k, v in values.items()},
		}
	)
	doc.insert()
	doc.submit()
	return {"name": doc.name}


# ============================================================================ Patient / Patient Visit buttons


@frappe.whitelist(methods=["GET"])
def get_patient_state(patient: str) -> dict:
	"""For the Patient form's "Admit in Labour": the open admission (if any), whether the user may admit now
	(`can_admit`) and, when not, why (`reason`). Offered only with no open admission and an open visit that is
	missing or admittable (Registered, In Triage, Routed to Maternity): the fast path would use that visit."""
	from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import find_open_visit

	frappe.has_permission("Patient", "read", doc=patient, throw=True)
	admission = open_admission(patient)
	reason = None
	if admission:
		reason = _("Already admitted ({0}).").format(admission)
	elif not has_any_role(MATERNITY_ROLES):
		reason = _("Admitting needs one of the roles {0}.").format(", ".join(_(r) for r in MATERNITY_ROLES))
	else:
		visit = find_open_visit(patient)
		if visit:
			department = frappe.db.get_value(VISIT_DOCTYPE, visit.name, "current_department")
			reason = visit_admit_refusal(visit.name, visit.status, department, None)
	return {
		"admission": admission,
		"can_admit": reason is None,
		"reason": reason,
		"department": DEPT_MATERNITY,
	}


@frappe.whitelist(methods=["GET"])
def get_visit_state(visit: str) -> dict:
	"""For the Patient Visit form's "Admit to Maternity": the visit's (not archived) admission, or whether it
	can be admitted (`can_admit`) and, when not, why (`reason`)."""
	row = frappe.db.get_value(
		VISIT_DOCTYPE, visit, ["name", "patient", "status", "outcome", "current_department"], as_dict=True
	)
	if not row:
		frappe.throw(_("Patient Visit {0} does not exist.").format(visit))
	frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit, throw=True)
	admission = visit_admission(visit)
	other = open_admission(row.patient)
	reason = None
	if admission:
		reason = _("Visit {0} already has a maternity admission ({1}).").format(visit, admission)
	elif other:
		reason = _("Already admitted ({0}).").format(other)
	elif not has_any_role(MATERNITY_ROLES):
		reason = _("Admitting needs one of the roles {0}.").format(", ".join(_(r) for r in MATERNITY_ROLES))
	else:
		reason = visit_admit_refusal(visit, row.status, row.current_department, row.outcome)
	return {
		"admission": admission,
		"open_admission": other,
		"can_admit": reason is None,
		"reason": reason,
		"maternity": row.current_department == DEPT_MATERNITY,
	}


@frappe.whitelist(methods=["GET"])
def get_admit_defaults(
	patient: str | None = None, visit: str | None = None, facility: str | None = None
) -> dict:
	"""Defaults for the admit dialogs: the facility and its Maternity Ward.

	The facility is the visit's; else, for the fast path, the patient's open visit's (the fast path admits from
	that visit, so its facility is fixed: `open_visit` and `message` say so); else `facility`; else the
	fast-path default."""
	from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import find_open_visit
	from onerc_healthcare.maternity.setup import ward_for_facility

	open_visit = message = None
	if visit:
		frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit, throw=True)
		facility = frappe.db.get_value(VISIT_DOCTYPE, visit, "facility")
	elif patient:
		frappe.has_permission("Patient", "read", doc=patient, throw=True)
		waiting = find_open_visit(patient)
		if waiting:
			open_visit = waiting.name
			facility = frappe.db.get_value(VISIT_DOCTYPE, open_visit, "facility")
			message = _(
				"The patient has an open visit {0} at {1}; she is admitted from that visit, there."
			).format(open_visit, facility)
		elif not facility:
			facility = default_facility(patient)
	return {
		"facility": facility,
		"ward": ward_for_facility(facility) if facility else None,
		"open_visit": open_visit,
		"message": message,
	}
