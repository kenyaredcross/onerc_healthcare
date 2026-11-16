# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Partograph (M2, INP-15, SDD §5.12.3, D60): bedside hourly rows and the WHO modified partograph.

One Partograph per Maternity Admission (`get_or_create`). Rows (Partograph Observation) are only appended,
through `add_observation`, and never edited or removed; a correction is a new row with a `note`. When two
rows share the same `observed_at`, the chart uses the later-entered value of each field (a correction row
supersedes the original without changing it).

WHO modified partograph (`compute`):
- `started_at` is the time of the first observation with cervical dilatation >= 4 cm (active labour);
  `d0` is the dilatation then.
- The **alert line** starts at (started_at, d0) and rises 1 cm per hour to 10 cm.
- The **action line** is the alert line shifted 4 hours to the right.
- A point is **right of** (behind) a line when, at its time, the line already shows more dilatation than
  the point: elapsed hours since started_at > dilatation - d0 (alert) or > dilatation - d0 + 4 (action).
  A point exactly on a line has not crossed it.
- Chart hours are measured from the first observation (latent-phase rows, if any, come before the alert
  line's start).

Lock order: Maternity Admission, then Partograph (after the admission's own Visit lock in M1, and the same
order as the Delivery Record submit in M3).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from onerc_healthcare.clinical.utils import require_roles
from onerc_healthcare.constants import (
	ADMISSION_ADMITTED,
	ADMISSION_IN_LABOUR,
	CONTRACTION_DURATION_BANDS,
	LIQUOR_CODES,
	MATERNITY_ADMISSION_DOCTYPE,
	MATERNITY_ROLES,
	MOULDING_GRADES,
	PARTOGRAPH_ACTIVE_LABOUR_CM,
	PARTOGRAPH_DOCTYPE,
	PARTOGRAPH_OBSERVATION_DOCTYPE,
	URINE_DIPSTICK_VALUES,
)
from onerc_healthcare.visit.events import FUTURE_SKEW_MINUTES, TEXT_VITALS, VitalRange

# Rows are added only while the admission is in one of these statuses (contract §6.2).
PARTOGRAPH_OPEN_STATUSES = (ADMISSION_ADMITTED, ADMISSION_IN_LABOUR)
ARCHIVED_FIELD = "krcs_archived"
TABLE = "observations"
FULL_DILATATION_CM = 10
ACTION_LINE_OFFSET_HOURS = 4
# Plausible (not normal) ranges: they catch typing errors. Maternal vitals reuse the triage ranges.
RANGES: tuple[VitalRange, ...] = (
	VitalRange("foetal_heart_rate", "beats/min", 60, 220, "140", whole_number=True),
	VitalRange("cervical_dilatation_cm", "cm", 0, FULL_DILATATION_CM, "6"),
	VitalRange("descent_fifths", "fifths", 0, 5, "3", whole_number=True),
	VitalRange("contractions_per_10_min", "per 10 min", 0, 6, "3", whole_number=True),
	VitalRange("oxytocin_u_per_l", "U/L", 0, 20, "5"),
	VitalRange("oxytocin_drops_per_min", "drops/min", 0, 80, "20", whole_number=True),
	*TEXT_VITALS,  # temperature, pulse, respiratory_rate, bp_systolic, bp_diastolic
	VitalRange("urine_volume_ml", "mL", 0, 2000, "200", whole_number=True),
)
SELECTS: dict[str, tuple[str, ...]] = {
	"liquor": LIQUOR_CODES,
	"moulding": MOULDING_GRADES,
	"contraction_duration": CONTRACTION_DURATION_BANDS,
	"urine_protein": URINE_DIPSTICK_VALUES,
	"urine_acetone": URINE_DIPSTICK_VALUES,
}
TEXT_LIMITS = {"drugs_iv_fluids": 1000, "note": 1000}
# A row needs at least one of these (drugs given and a note are not measurements).
MEASURED_FIELDS = tuple(r.fieldname for r in RANGES) + tuple(SELECTS)
INPUT_FIELDS = ("observed_at", *MEASURED_FIELDS, *TEXT_LIMITS)
# Every stored field of a row (compared by the append-only guard).
ROW_FIELDS = ("observed_at", "recorded_by", *MEASURED_FIELDS, *TEXT_LIMITS)

# Warning thresholds (current values, i.e. the latest recorded value of each field)
FHR_LOW, FHR_HIGH = 110, 160
LIQUOR_WARNINGS = {"M": "red", "B": "red"}
MOULDING_WARNING = "+++"
SEVERE_SYSTOLIC, SEVERE_DIASTOLIC = 160, 110
FEVER_C = 38
EPSILON_HOURS = 1e-9


class PartographError(frappe.ValidationError):
	"""The partograph or its admission does not allow this action."""


class ImplausiblePartographValueError(frappe.ValidationError):
	"""A partograph value is missing, not a number, outside its plausible range or not a listed option."""


class PartographLockedError(frappe.ValidationError):
	"""Saved partograph rows and computed fields cannot be edited."""


# --------------------------------------------------------------------------------------------- endpoints


@frappe.whitelist(methods=["POST"])
def get_or_create(admission: str) -> str:
	"""The admission's Partograph name, created on the first call (MATERNITY_ROLES; Admitted or In Labour)."""
	require_roles(MATERNITY_ROLES, _("keep the partograph"))
	adm = _lock_admission(admission)
	existing = frappe.db.get_value(
		PARTOGRAPH_DOCTYPE, {"admission": adm.name}, "name", order_by="creation asc"
	)
	if existing:
		return existing
	_assert_admission_open(adm)
	doc = frappe.get_doc({"doctype": PARTOGRAPH_DOCTYPE, "admission": adm.name})
	doc.flags.from_partograph = True
	# The Medical In-Charge has no create right (contract §1); the role and the admission are checked above.
	doc.insert(ignore_permissions=True)
	return doc.name


@frappe.whitelist(methods=["POST"])
def add_observation(partograph: str, values: dict | str) -> dict:
	"""Append one hourly row. Returns the row name, the partograph summary and the current warnings."""
	require_roles(MATERNITY_ROLES, _("record partograph observations"))
	admission = frappe.db.get_value(PARTOGRAPH_DOCTYPE, partograph, "admission")
	if not admission:
		frappe.throw(_("Partograph {0} not found.").format(partograph), frappe.DoesNotExistError)
	adm = _lock_admission(admission)  # admission first (lock order), then the partograph
	_assert_admission_open(adm)
	doc = frappe.get_doc(PARTOGRAPH_DOCTYPE, partograph, for_update=True)
	if not doc.has_permission("write"):
		frappe.throw(_("You cannot add observations to {0}.").format(doc.name), frappe.PermissionError)

	row_values = clean_values(_parse(values), adm)
	_check_dilatation_order(doc.get(TABLE) or [], row_values)
	row = doc.append(TABLE, {**storage_values(row_values), "recorded_by": frappe.session.user})
	summary = compute(doc.get(TABLE))
	_apply_summary(doc, summary)
	doc.flags.from_partograph = True
	doc.save()
	return {
		"name": doc.name,
		"row": row.name,
		"started_at": doc.started_at,
		"alert_line_crossed": cint(doc.alert_line_crossed),
		"action_line_crossed": cint(doc.action_line_crossed),
		"warnings": [w["message"] for w in summary["warning_details"]],
		"warning_details": summary["warning_details"],
	}


@frappe.whitelist(methods=["GET"])
def get_chart(partograph: str) -> dict:
	"""Series for the chart, the alert and action lines and the current warnings (read permission)."""
	doc = frappe.get_doc(PARTOGRAPH_DOCTYPE, partograph)
	if not doc.has_permission("read"):
		frappe.throw(_("You do not have access to {0}.").format(doc.name), frappe.PermissionError)
	from onerc_healthcare.privacy.access_log import ACTION_READ_API, log_access

	log_access(
		ACTION_READ_API,
		PARTOGRAPH_DOCTYPE,
		doc.name,
		patient=doc.patient,
		detail="maternity.partograph.get_chart",
	)
	summary = compute(doc.get(TABLE))
	adm_status = frappe.db.get_value(MATERNITY_ADMISSION_DOCTYPE, doc.admission, "status")
	return {
		"started_at": summary["started_at"],
		"origin": summary["origin"],
		"points": summary["points"],
		"alert_line": summary["alert_line"],
		"action_line": summary["action_line"],
		"alert_line_crossed": summary["alert_line_crossed"],
		"action_line_crossed": summary["action_line_crossed"],
		"warnings": [w["message"] for w in summary["warning_details"]],
		"warning_details": summary["warning_details"],
		"last_values": summary["last_values"],
		"admission_status": adm_status,
		"can_add": bool(
			adm_status in PARTOGRAPH_OPEN_STATUSES
			and frappe.has_permission(PARTOGRAPH_DOCTYPE, "write", doc)
			and _has_maternity_role()
		),
	}


# ------------------------------------------------------------------------------------------- validation


def clean_values(values: dict, adm) -> dict:
	"""Checked and normalised row values (raises ImplausiblePartographValueError listing every problem)."""
	unknown = sorted(k for k in values if k not in INPUT_FIELDS)
	if unknown:
		frappe.throw(
			_("These values cannot be recorded on a partograph row: {0}.").format(", ".join(unknown)),
			ImplausiblePartographValueError,
		)
	errors: list[str] = []
	row: dict = {}
	label = _label

	for spec in RANGES:
		raw = values.get(spec.fieldname)
		if _blank(raw):
			continue
		number = _to_number(raw)
		if number is None:
			errors.append(
				_("{0} must be a number, for example {1}.").format(
					frappe.bold(label(spec.fieldname)), spec.example
				)
			)
			continue
		if spec.whole_number and number != int(number):
			errors.append(_("{0} must be a whole number.").format(frappe.bold(label(spec.fieldname))))
			continue
		if not spec.low <= number <= spec.high:
			errors.append(_range_message(spec, number))
			continue
		row[spec.fieldname] = int(number) if spec.whole_number else round(number, 1)

	for fieldname, options in SELECTS.items():
		raw = values.get(fieldname)
		if _blank(raw):
			continue
		text = str(raw).strip()
		if text not in options:
			errors.append(
				_("{0} must be one of {1}.").format(frappe.bold(label(fieldname)), ", ".join(options))
			)
			continue
		row[fieldname] = text

	for fieldname, limit in TEXT_LIMITS.items():
		text = str(values.get(fieldname) or "").strip()
		if len(text) > limit:
			errors.append(
				_("{0} is too long ({1} characters, at most {2}).").format(
					frappe.bold(label(fieldname)), len(text), limit
				)
			)
		elif text:
			row[fieldname] = text

	if _blank(values.get("bp_systolic")) != _blank(values.get("bp_diastolic")):
		errors.append(_("Enter both systolic and diastolic blood pressure, or neither."))
	elif "bp_systolic" in row and "bp_diastolic" in row and row["bp_systolic"] <= row["bp_diastolic"]:
		errors.append(
			_("Systolic blood pressure ({0}) must be higher than diastolic ({1}).").format(
				row["bp_systolic"], row["bp_diastolic"]
			)
		)
	if row.get("contraction_duration") and not row.get("contractions_per_10_min"):
		errors.append(_("Enter the number of contractions in 10 minutes with their duration."))

	observed_at, message = _clean_observed_at(values.get("observed_at"), adm)
	if message:
		errors.append(message)
	row["observed_at"] = observed_at

	if not errors and not any(f in row for f in MEASURED_FIELDS):
		errors.append(
			_("Record at least one measurement (for example foetal heart rate or cervical dilatation).")
		)
	if errors:
		frappe.throw(errors, ImplausiblePartographValueError, title=_("Check the observation"), as_list=True)
	return row


def _clean_observed_at(raw, adm) -> tuple[datetime | None, str | None]:
	if _blank(raw):
		return now_datetime().replace(microsecond=0), None
	try:
		observed_at = get_datetime(raw)
	except Exception:
		observed_at = None
	if observed_at is None:
		return None, _("{0} is not a valid date and time.").format(frappe.bold(_label("observed_at")))
	observed_at = observed_at.replace(microsecond=0)
	if observed_at > now_datetime() + timedelta(minutes=FUTURE_SKEW_MINUTES):
		return None, _("The observation time {0} is in the future.").format(frappe.bold(_fmt(observed_at)))
	admitted_at = get_datetime(adm.admitted_at) if adm.admitted_at else None
	if admitted_at and observed_at < admitted_at.replace(microsecond=0):
		return None, _("The observation time {0} is before the admission ({1}).").format(
			frappe.bold(_fmt(observed_at)), _fmt(admitted_at)
		)
	return observed_at, None


def _check_dilatation_order(rows, row_values: dict) -> None:
	"""Cervical dilatation never goes down over time unless the row's note explains why."""
	value = row_values.get("cervical_dilatation_cm")
	if value is None or row_values.get("note"):
		return
	at = row_values["observed_at"]
	timeline = _timeline(rows)
	before = [e for e in timeline if e["observed_at"] <= at and e.get("cervical_dilatation_cm") is not None]
	after = [e for e in timeline if e["observed_at"] > at and e.get("cervical_dilatation_cm") is not None]
	label = frappe.bold(_label("cervical_dilatation_cm"))
	if before and value < before[-1]["cervical_dilatation_cm"]:
		frappe.throw(
			_(
				"{0} of {1} cm is lower than the {2} cm recorded at {3}. If this is a correction, add a note "
				"explaining it."
			).format(
				label,
				_num(value),
				_num(before[-1]["cervical_dilatation_cm"]),
				_fmt(before[-1]["observed_at"]),
			),
			ImplausiblePartographValueError,
			title=_("Check the dilatation"),
		)
	if after and value > after[0]["cervical_dilatation_cm"]:
		frappe.throw(
			_(
				"{0} of {1} cm is higher than the {2} cm recorded later, at {3}. If this is a correction, add a "
				"note explaining it."
			).format(
				label, _num(value), _num(after[0]["cervical_dilatation_cm"]), _fmt(after[0]["observed_at"])
			),
			ImplausiblePartographValueError,
			title=_("Check the dilatation"),
		)


# ------------------------------------------------------------------------------------- controller hooks


def validate_partograph(doc) -> None:
	"""Partograph.validate: one per open admission on insert; afterwards rows are append-only and the
	computed fields change only through `add_observation`."""
	if doc.is_new():
		_validate_new(doc)
		return
	before = doc.get_doc_before_save()
	if before is None:
		return
	_guard_rows(doc, before)
	if not doc.flags.from_partograph:
		changed = [
			f
			for f in ("started_at", "alert_line_crossed", "action_line_crossed")
			if _norm(f, doc.get(f)) != _norm(f, before.get(f))
		]
		if changed:
			frappe.throw(
				_("{0} are computed from the observations and cannot be edited.").format(
					", ".join(frappe.bold(_(doc.meta.get_label(f))) for f in changed)
				),
				PartographLockedError,
				title=_("Not Editable"),
			)


def _validate_new(doc) -> None:
	if not doc.admission:
		frappe.throw(_("Select the Maternity Admission."), frappe.MandatoryError)
	adm = _lock_admission(doc.admission)
	_assert_admission_open(adm)
	existing = frappe.db.get_value(PARTOGRAPH_DOCTYPE, {"admission": adm.name}, "name")
	if existing:
		frappe.throw(
			_("Admission {0} already has partograph {1}.").format(adm.name, frappe.bold(existing)),
			PartographError,
			title=_("Partograph Exists"),
		)
	if doc.get(TABLE) and not doc.flags.from_partograph:
		frappe.throw(
			_("Add observations with the Add hourly observation button after saving the partograph."),
			PartographLockedError,
		)
	doc.started_at = None
	doc.alert_line_crossed = doc.action_line_crossed = 0


def _guard_rows(doc, before) -> None:
	old = before.get(TABLE) or []
	new = doc.get(TABLE) or []
	if len(new) < len(old) or [_row_key(r) for r in new[: len(old)]] != [_row_key(r) for r in old]:
		frappe.throw(
			_(
				"Saved partograph observations cannot be changed or removed. Add a new row with a note instead."
			),
			PartographLockedError,
			title=_("Not Editable"),
		)
	if len(new) > len(old) and not doc.flags.from_partograph:
		frappe.throw(
			_("Add partograph observations with the Add hourly observation button."),
			PartographLockedError,
			title=_("Not Editable"),
		)


def _row_key(row) -> tuple:
	return (row.get("name"), *(_norm(f, row.get(f)) for f in ROW_FIELDS))


def refuse_row_change(row, method=None) -> None:
	"""Partograph Observation validate / on_trash: a row is never saved or deleted on its own."""
	frappe.throw(
		_("Saved partograph observations cannot be changed or removed. Add a new row with a note instead."),
		PartographLockedError,
		title=_("Not Editable"),
	)


# -------------------------------------------------------------------------------------------- the chart


def compute(rows) -> dict:
	"""Chart series, alert/action lines, summary flags and current warnings from the rows (see module doc)."""
	timeline = _timeline(rows)
	origin = timeline[0]["observed_at"] if timeline else None
	start = next(
		(
			e
			for e in timeline
			if e.get("cervical_dilatation_cm") is not None
			and e["cervical_dilatation_cm"] >= PARTOGRAPH_ACTIVE_LABOUR_CM
		),
		None,
	)
	started_at = start["observed_at"] if start else None
	d0 = start["cervical_dilatation_cm"] if start else None

	def hours(at: datetime) -> float:
		return round((at - origin).total_seconds() / 3600, 2)

	alert_line, action_line = [], []
	if start:
		h0 = hours(started_at)
		rise = FULL_DILATATION_CM - d0
		alert_line = [[h0, d0], [round(h0 + rise, 2), FULL_DILATATION_CM]]
		action_line = [
			[round(h0 + ACTION_LINE_OFFSET_HOURS, 2), d0],
			[round(h0 + ACTION_LINE_OFFSET_HOURS + rise, 2), FULL_DILATATION_CM],
		]

	points, first_alert, first_action = [], None, None
	for e in timeline:
		dil = e.get("cervical_dilatation_cm")
		alert = action = False
		if start and dil is not None and e["observed_at"] >= started_at:
			behind = (e["observed_at"] - started_at).total_seconds() / 3600 - (dil - d0)
			alert = behind > EPSILON_HOURS
			action = behind - ACTION_LINE_OFFSET_HOURS > EPSILON_HOURS
		if alert and first_alert is None:
			first_alert = e
		if action and first_action is None:
			first_action = e
		points.append(
			{
				"hours": hours(e["observed_at"]),
				"observed_at": e["observed_at"],
				"dilatation": dil,
				"descent": e.get("descent_fifths"),
				"fhr": e.get("foetal_heart_rate"),
				"alert_crossed": alert,
				"action_crossed": action,
			}
		)

	last = _last_values(timeline)
	return {
		"origin": origin,
		"started_at": started_at,
		"points": points,
		"alert_line": alert_line,
		"action_line": action_line,
		"alert_line_crossed": 1 if first_alert else 0,
		"action_line_crossed": 1 if first_action else 0,
		"last_values": {k: {"value": v, "observed_at": t} for k, (v, t) in last.items()},
		"warning_details": _warnings(first_alert, first_action, last),
	}


def _warnings(first_alert, first_action, last: dict) -> list[dict]:
	out: list[dict] = []

	def add(code: str, level: str, message: str) -> None:
		out.append({"code": code, "level": level, "message": message})

	if first_action:
		add(
			"action_line",
			"red",
			_("Action line crossed: {0} cm at {1}. A clinician must review now.").format(
				_num(first_action["cervical_dilatation_cm"]), _fmt(first_action["observed_at"])
			),
		)
	elif first_alert:
		add(
			"alert_line",
			"orange",
			_("Alert line crossed: {0} cm at {1}. Labour is slower than 1 cm per hour.").format(
				_num(first_alert["cervical_dilatation_cm"]), _fmt(first_alert["observed_at"])
			),
		)
	if "foetal_heart_rate" in last:
		fhr, at = last["foetal_heart_rate"]
		if fhr < FHR_LOW:
			add(
				"fhr_low",
				"red",
				_("Foetal heart rate {0}/min at {1} is below {2}.").format(fhr, _fmt(at), FHR_LOW),
			)
		elif fhr > FHR_HIGH:
			add(
				"fhr_high",
				"red",
				_("Foetal heart rate {0}/min at {1} is above {2}.").format(fhr, _fmt(at), FHR_HIGH),
			)
	if "liquor" in last:
		liquor, at = last["liquor"]
		if liquor == "M":
			add(
				"liquor_meconium", LIQUOR_WARNINGS["M"], _("Meconium-stained liquor at {0}.").format(_fmt(at))
			)
		elif liquor == "B":
			add("liquor_blood", LIQUOR_WARNINGS["B"], _("Blood-stained liquor at {0}.").format(_fmt(at)))
	if "moulding" in last and last["moulding"][0] == MOULDING_WARNING:
		add("moulding", "red", _("Moulding +++ at {0}.").format(_fmt(last["moulding"][1])))
	if "bp" in last:
		(systolic, diastolic), at = last["bp"]
		if systolic >= SEVERE_SYSTOLIC or diastolic >= SEVERE_DIASTOLIC:
			add(
				"bp_severe",
				"red",
				_("Blood pressure {0}/{1} mmHg at {2}: severe hypertension.").format(
					systolic, diastolic, _fmt(at)
				),
			)
	if "temperature" in last:
		temp, at = last["temperature"]
		if temp >= FEVER_C:
			add("fever", "orange", _("Temperature {0} °C at {1}: fever.").format(_num(temp), _fmt(at)))
	return out


def _timeline(rows) -> list[dict]:
	"""One entry per distinct observed_at, oldest first. Within a time, rows are merged in entry (idx)
	order, so a later correction row's value replaces the earlier one for each field it records."""
	by_time: dict[datetime, dict] = {}
	ordered = sorted((r for r in rows or [] if r.get("observed_at")), key=lambda r: cint(r.get("idx")))
	for row in ordered:
		at = get_datetime(row.get("observed_at")).replace(microsecond=0)
		entry = by_time.setdefault(at, {"observed_at": at})
		for fieldname in MEASURED_FIELDS:
			value = recorded_value(row, fieldname)
			if value is not None:
				entry[fieldname] = value
	return [by_time[t] for t in sorted(by_time)]


# ------------------------------------------------------------------------------ stored values
# Values whose plausible range includes 0 are stored in string columns (Data, or the descent Select), so a
# blank stays blank and "0" is a recorded 0. They are stored normalised ("4.5", "4", "0").
STRING_NUMBER_FIELDS = (
	"cervical_dilatation_cm",
	"descent_fifths",
	"contractions_per_10_min",
	"oxytocin_u_per_l",
	"oxytocin_drops_per_min",
	"urine_volume_ml",
)
NUMERIC_FIELDS = {r.fieldname: r for r in RANGES}
# The other numbers (foetal heart rate, pulse, BP, temperature, respiration) are Int/Float columns, which
# Frappe stores NOT NULL DEFAULT 0. Their range excludes 0, so a stored 0 means "not recorded".
ZERO_IS_BLANK_FIELDS = tuple(f for f in NUMERIC_FIELDS if f not in STRING_NUMBER_FIELDS)


def storage_values(row: dict) -> dict:
	"""Checked values (numbers) as they are stored: the string-column numbers become normalised text."""
	out = dict(row)
	for fieldname in STRING_NUMBER_FIELDS:
		if out.get(fieldname) is not None:
			out[fieldname] = _num(out[fieldname])
	return out


def recorded_value(row, fieldname: str):
	"""The value a saved (or appended) row records for `fieldname` (a number for the numeric fields, text
	for the coded ones), or None when it records none."""
	raw = row.get(fieldname)
	if _blank(raw):
		return None
	spec = NUMERIC_FIELDS.get(fieldname)
	if not spec:
		return str(raw).strip()
	number = _to_number(raw)
	if number is None or (number == 0 and fieldname in ZERO_IS_BLANK_FIELDS):
		return None
	return int(number) if spec.whole_number else round(number, 1)


def _last_values(timeline: list[dict]) -> dict:
	"""Latest recorded value of each field: {field: (value, observed_at)}; BP as the pair of one entry."""
	last: dict = {}
	for e in timeline:
		for fieldname in MEASURED_FIELDS:
			if fieldname in e:
				last[fieldname] = (e[fieldname], e["observed_at"])
		if "bp_systolic" in e and "bp_diastolic" in e:
			last["bp"] = ((e["bp_systolic"], e["bp_diastolic"]), e["observed_at"])
	return last


def _apply_summary(doc, summary: dict) -> None:
	doc.started_at = summary["started_at"]
	doc.alert_line_crossed = summary["alert_line_crossed"]
	doc.action_line_crossed = summary["action_line_crossed"]


# ---------------------------------------------------------------------------------------------- helpers


def _lock_admission(admission: str):
	"""The admission row, locked (SELECT ... FOR UPDATE), after a read-permission check."""
	if not admission or not frappe.db.exists(MATERNITY_ADMISSION_DOCTYPE, admission):
		frappe.throw(_("Maternity Admission {0} not found.").format(admission), frappe.DoesNotExistError)
	adm = frappe.get_doc(MATERNITY_ADMISSION_DOCTYPE, admission, for_update=True)
	if not adm.has_permission("read"):
		frappe.throw(_("You do not have access to {0}.").format(adm.name), frappe.PermissionError)
	return adm


def _assert_admission_open(adm) -> None:
	if adm.get(ARCHIVED_FIELD):
		frappe.throw(_("Admission {0} is archived.").format(adm.name), PartographError)
	if adm.status not in PARTOGRAPH_OPEN_STATUSES:
		frappe.throw(
			_(
				"The partograph is kept only while the mother is Admitted or In Labour; admission {0} is {1}."
			).format(adm.name, frappe.bold(_(adm.status))),
			PartographError,
			title=_("Partograph Closed"),
		)


def _has_maternity_role() -> bool:
	from onerc_healthcare.clinical.utils import has_any_role

	return has_any_role(MATERNITY_ROLES)


def _parse(values) -> dict:
	if isinstance(values, str):
		try:
			values = json.loads(values or "{}")
		except ValueError:
			frappe.throw(_("The observation values are not valid."), ImplausiblePartographValueError)
	if not isinstance(values, dict):
		frappe.throw(_("The observation values are not valid."), ImplausiblePartographValueError)
	return values


def _blank(value) -> bool:
	return value is None or (isinstance(value, str) and not value.strip())


def _to_number(raw) -> float | None:
	if isinstance(raw, bool):
		return None
	if isinstance(raw, int | float):
		return float(raw)
	text = str(raw).strip().replace(",", ".")
	try:
		value = float(text)
	except ValueError:
		return None
	return value if value == value and value not in (float("inf"), float("-inf")) else None


def _norm(fieldname: str, value):
	if fieldname in ("observed_at", "started_at"):
		return get_datetime(value).replace(microsecond=0) if value else None
	if fieldname in ("alert_line_crossed", "action_line_crossed"):
		return cint(value)
	if fieldname in ZERO_IS_BLANK_FIELDS:
		return round(flt(value), 1)  # Int/Float columns store 0 for "not recorded"
	if fieldname in STRING_NUMBER_FIELDS:
		number = None if _blank(value) else _to_number(value)
		return round(number, 1) if number is not None else (None if _blank(value) else str(value).strip())
	return None if _blank(value) else str(value).strip()


def _range_message(spec: VitalRange, value: float) -> str:
	label = frappe.bold(_label(spec.fieldname))
	if spec.fieldname == "temperature" and 77 <= value <= 113:
		return _("{0} of {1} looks like degrees Fahrenheit. Enter it in °C, for example 36.8.").format(
			label, value
		)
	return _("{0} of {1} {2} is not plausible. Expected between {3} and {4} {2}.").format(
		label, _num(value), spec.unit, spec.low, spec.high
	)


def _label(fieldname: str) -> str:
	return _(frappe.get_meta(PARTOGRAPH_OBSERVATION_DOCTYPE).get_label(fieldname))


def _num(value) -> str:
	value = flt(value)
	return str(int(value)) if value == int(value) else f"{value:.1f}"


def _fmt(at) -> str:
	return get_datetime(at).strftime("%d-%m-%Y %H:%M")
