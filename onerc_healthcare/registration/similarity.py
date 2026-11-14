# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Possible-duplicate search on name, sex, date of birth and residence (FR-2, SDD 4.3).

Candidates come from the indexed phonetic name keys stored on Patient (`registration.names`), first and
last names possibly swapped, in three bounded sets that are merged before scoring:

1. both names match phonetically (straight or swapped): always scored;
2. one name matches phonetically and the date of birth and sex are equal: always scored;
3. one name matches phonetically: the newest `similarity_candidate_limit` (EMR Registration Settings).

So a common surname can push weak matches out of set 3 but never an exact duplicate. Each candidate is
scored; only those with a name match and a total of at least `MIN_SCORE` are returned, best first.
Merged records are left out.

Estimated dates of birth (Phase 3): when either record's DOB is estimated from an age, a DOB within
`ESTIMATED_DOB_WINDOW_YEARS` (±2 years) scores like the same year of birth, and set 2 above uses that
window instead of the exact date.
"""

from difflib import SequenceMatcher

import frappe
from frappe import _
from frappe.utils import add_years, cint, getdate

from onerc_healthcare.constants import (
	PATIENT_DOB_ESTIMATED_FIELD,
	PATIENT_IDENTIFIER_DOCTYPE,
	PATIENT_IDENTIFIERS_FIELD,
)
from onerc_healthcare.registration.identifiers import MERGED_INTO_FIELD, RESIDENCE_FIELD
from onerc_healthcare.registration.names import (
	FIRST_NAME_KEY_FIELD,
	LAST_NAME_KEY_FIELD,
	name_key,
	normalise_name,
)

MIN_SCORE = 4
MAX_RESULTS = 10
DEFAULT_CANDIDATE_LIMIT = 500
STRONG_LIMIT = 2000
SETTINGS_DOCTYPE = "EMR Registration Settings"

EXACT, SIMILAR = 3, 2
# Either DOB estimated from an age: births this many years apart still count as similar.
ESTIMATED_DOB_WINDOW_YEARS = 2


def within_estimate_window(a, b, years: int = ESTIMATED_DOB_WINDOW_YEARS) -> bool:
	"""`b` lies within ±`years` of `a` (calendar years, same day of month, clamped at month end)."""
	a, b = getdate(a), getdate(b)
	return getdate(add_years(a, -years)) <= b <= getdate(add_years(a, years))


def _clean(name: str | None) -> str:
	return normalise_name(name)


def _name_score(given: str, candidate: str, given_key: str, candidate_key: str) -> int:
	if not given or not candidate:
		return 0
	if given == candidate:
		return EXACT
	if (given_key and given_key == candidate_key) or SequenceMatcher(None, given, candidate).ratio() >= 0.8:
		return SIMILAR
	return 0


def get_candidate_limit() -> int:
	limit = cint(frappe.db.get_single_value(SETTINGS_DOCTYPE, "similarity_candidate_limit", cache=False))
	return limit if limit > 0 else DEFAULT_CANDIDATE_LIMIT


def _has_estimate_field() -> bool:
	return frappe.get_meta("Patient").has_field(PATIENT_DOB_ESTIMATED_FIELD)


def _fetch_candidates(
	first_key: str,
	last_key: str,
	sex: str | None,
	dob: str | None,
	exclude: str | None,
	dob_estimated: bool = False,
) -> list[frappe._dict]:
	keys = tuple(k for k in (first_key, last_key) if k)
	if not keys:
		return []
	has_estimate = _has_estimate_field()
	estimated_col = f"`{PATIENT_DOB_ESTIMATED_FIELD}`" if has_estimate else "0"
	fields = (
		f"name, patient_name, first_name, last_name, sex, dob, status, image, `{RESIDENCE_FIELD}` as residence, "
		f"`{FIRST_NAME_KEY_FIELD}` as first_key, `{LAST_NAME_KEY_FIELD}` as last_key, "
		f"{estimated_col} as dob_estimated"
	)
	base = f"ifnull(`{MERGED_INTO_FIELD}`, '') = '' and name != %(exclude)s"
	one_name = f"(`{FIRST_NAME_KEY_FIELD}` in %(keys)s or `{LAST_NAME_KEY_FIELD}` in %(keys)s)"
	values = {"exclude": exclude or "", "keys": keys, "fk": first_key, "lk": last_key, "sex": sex, "dob": dob}
	if dob:
		values["dob_lo"] = str(getdate(add_years(dob, -ESTIMATED_DOB_WINDOW_YEARS)))
		values["dob_hi"] = str(getdate(add_years(dob, ESTIMATED_DOB_WINDOW_YEARS)))
	# Set 2: the same DOB, or within the window when either DOB is estimated.
	if dob_estimated:
		dob_match = "dob between %(dob_lo)s and %(dob_hi)s"
	elif has_estimate:
		dob_match = (
			f"(dob = %(dob)s or (ifnull({estimated_col}, 0) = 1 and dob between %(dob_lo)s and %(dob_hi)s))"
		)
	else:
		dob_match = "dob = %(dob)s"
	queries = []
	if first_key and last_key:
		queries.append(
			(
				f"""(`{FIRST_NAME_KEY_FIELD}` = %(fk)s and `{LAST_NAME_KEY_FIELD}` = %(lk)s)
				or (`{FIRST_NAME_KEY_FIELD}` = %(lk)s and `{LAST_NAME_KEY_FIELD}` = %(fk)s)""",
				"",
				STRONG_LIMIT,
			)
		)
	if dob and sex:
		queries.append((f"{one_name} and {dob_match} and sex = %(sex)s", "", STRONG_LIMIT))
	queries.append((one_name, "order by creation desc", get_candidate_limit()))

	seen: dict[str, frappe._dict] = {}
	for where, order, limit in queries:
		for row in frappe.db.sql(
			f"select {fields} from `tabPatient` where {base} and ({where}) {order} limit {cint(limit)}",
			values,
			as_dict=True,
		):
			seen.setdefault(row.name, row)
	return list(seen.values())


def find_similar(
	first_name: str,
	last_name: str | None = None,
	sex: str | None = None,
	dob: str | None = None,
	residence: str | None = None,
	exclude: str | None = None,
	limit: int = MAX_RESULTS,
	dob_estimated: bool = False,
) -> list[dict]:
	"""Possible duplicates, best first. `dob_estimated`: the given DOB was estimated from an age."""
	first, last = _clean(first_name), _clean(last_name)
	if not first:
		frappe.throw(_("First Name is required to look for similar patients."), title=_("Missing Value"))

	first_sx, last_sx = name_key(first_name), name_key(last_name)
	candidates = _fetch_candidates(
		first_sx, last_sx, sex, str(getdate(dob)) if dob else None, exclude, bool(dob_estimated)
	)

	given_dob = getdate(dob) if dob else None
	results = []
	for c in candidates:
		c_first, c_last = _clean(c.first_name), _clean(c.last_name)
		straight = (
			_name_score(first, c_first, first_sx, c.first_key),
			_name_score(last, c_last, last_sx, c.last_key),
		)
		swapped = (
			_name_score(first, c_last, first_sx, c.last_key),
			_name_score(last, c_first, last_sx, c.first_key),
		)
		pair = max(straight, swapped, key=sum)
		name_score = sum(pair)
		if not name_score:
			continue
		reasons = []
		labels = ((_("first name"), pair[0]), (_("last name"), pair[1]))
		if pair is swapped:
			labels = (
				(_("first and last name swapped"), pair[0]),
				(_("last and first name swapped"), pair[1]),
			)
		for label, score in labels:
			if score == EXACT:
				reasons.append(_("same {0}").format(label))
			elif score == SIMILAR:
				reasons.append(_("similar {0}").format(label))

		score = name_score
		if sex and c.sex:
			if sex == c.sex:
				score += 1
				reasons.append(_("same sex"))
			else:
				score -= 3
		if given_dob and c.dob:
			c_dob = getdate(c.dob)
			if c_dob == given_dob:
				score += 3
				reasons.append(_("same date of birth"))
			elif c_dob.year == given_dob.year:
				score += 1
				reasons.append(_("same year of birth"))
			elif (dob_estimated or cint(c.dob_estimated)) and within_estimate_window(given_dob, c_dob):
				score += 1
				reasons.append(
					_("date of birth within {0} years (estimated)").format(ESTIMATED_DOB_WINDOW_YEARS)
				)
			elif abs(c_dob.year - given_dob.year) > 5:
				score -= 2
		if residence and c.residence and residence == c.residence:
			score += 2
			reasons.append(_("same residence"))
		if score < MIN_SCORE:
			continue
		results.append(
			{
				"name": c.name,
				"patient_name": c.patient_name,
				"sex": c.sex,
				"dob": c.dob,
				"dob_estimated": cint(c.dob_estimated),
				"residence": c.residence,
				"status": c.status,
				"image": c.image,
				"score": score,
				"reasons": reasons,
			}
		)

	if not results:
		return []
	# Respect the user's row-level access (User Permissions, permission query conditions).
	allowed = set(
		frappe.get_list("Patient", filters={"name": ["in", [r["name"] for r in results]]}, pluck="name")
	)
	results = [r for r in results if r["name"] in allowed]
	results.sort(key=lambda r: (-r["score"], r["patient_name"] or ""))
	results = results[:limit]
	_attach_identifiers(results)
	return results


def _attach_identifiers(results: list[dict]) -> None:
	if not results:
		return
	rows = frappe.get_all(
		PATIENT_IDENTIFIER_DOCTYPE,
		filters={
			"parenttype": "Patient",
			"parentfield": PATIENT_IDENTIFIERS_FIELD,
			"parent": ["in", [r["name"] for r in results]],
		},
		fields=["parent", "identifier_type", "identifier_value"],
		order_by="idx asc",
	)
	by_parent: dict[str, list[str]] = {}
	for row in rows:
		by_parent.setdefault(row.parent, []).append(f"{row.identifier_type}: {row.identifier_value}")
	for r in results:
		r["identifiers"] = by_parent.get(r["name"], [])
