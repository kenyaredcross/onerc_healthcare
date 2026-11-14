# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH 705A/705B diagnosis counts: the "Diagnosis Category" source (R2; FR-29..31, SDD §5.8).

PROVISIONAL — HIS Officer to confirm (OI-5). Element rule: ``{"category": <MOH Disease Category>,
"age_max_months": 60}`` (705A, under 5) or ``{"category": ..., "age_min_months": 60}`` (705B, 5 and
over). ``age_min_months`` is inclusive and ``age_max_months`` exclusive, like `ages.AgeBand`: a child of
exactly 5 years (60 months) counts on 705B. Age is in completed months on the **encounter date**
(`ages.age_months_sql`).

**What is counted** (the user's rule, 2026-11-13):

1. Diagnoses of submitted encounters dated in the period that pass the base filters
   (`filters.encounter_conditions`: facility, company, archived, merged patients, "Registered in error" /
   "Superseded" visits, the ``krcs_report_exclusions`` hook). Draft and cancelled encounters never count.
2. **New** diagnoses only (``case_type`` "New"); primary and secondary both count.
3. Each code is resolved to a category (`mapping.Resolver`: Exact > longest Prefix > deepest Block >
   catch-all). Excluded categories are dropped. An unmapped code counts under the catch-all ("All other
   diseases") **and** is recorded as an exception.
4. **Laboratory confirmation**: a category that requires it (Confirmed Malaria) stays only when the same
   encounter has an approved (submitted) Observation of one of its confirmation tests whose result is one
   of its positive results; otherwise the diagnosis counts under its fallback (Clinical Malaria). The
   Observation belongs to the encounter through its Service Request (``order_group``) or its reference.
   The malaria tests are Select results with fixed options in the seeded catalogue
   (``setup/data/lab_catalogue.json``), so a positive is detected exactly; the test names and positive
   values are configurable on the category, so a catalogue change needs no code change.
5. **Episodes**: an encounter, plus every encounter linked to it by internal referrals (the target visit's
   ``referred_from`` → Referral ``source_encounter``), is one episode. Within an episode:
   - **group priority**: of the categories present, only the highest-priority ones of each group count
     (Confirmed beats Clinical Malaria; a specific injury beats Other Injuries; equal priorities all
     count);
   - each category counts **once**, at the episode's earliest encounter that has it (its age decides the
     form). A category whose earliest encounter is before the period was counted in an earlier report
     and is not counted again. (A report never revises an earlier period: a referral chain that crosses
     a month end and upgrades Clinical to Confirmed Malaria counts once in each month.)
6. **Form**: a category that is not a line of the patient's form (705B-only Hypertension for a child under
   5, 705A-only Neonatal Sepsis for an adult) counts under the catch-all instead, once per episode.
7. **Missing date of birth** (or one after the encounter date): the age is unknown, so the episode's
   diagnoses count on **neither** 705A nor 705B, and the encounter is listed as an exception so the HIS
   Officer can record the date of birth or an estimated age and regenerate. (Counting them on 705B would
   silently put children on the adult form; registration now requires a DOB or an estimated age.)
   An element with no age limit counts them.

**Queries**: one SQL pass for the period's New diagnoses (encounter, patient, code and referral link
together); then, only when needed, one indexed query for the malaria tests of those patients and one
per referral generation for referral sources outside the period (rare). Grouping is done in Python.
Read-only; independent of the user's permissions.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import frappe
from frappe.utils import cint, cstr, getdate

from onerc_healthcare.coding.setup import ICD11_CODE_SYSTEM
from onerc_healthcare.constants import ENCOUNTER_DIAGNOSIS_DOCTYPE, ENCOUNTER_DOCTYPE, REFERRAL_DOCTYPE
from onerc_healthcare.reporting import filters, mapping
from onerc_healthcare.reporting.ages import UNDER_FIVE_MONTHS, age_months_sql, band_for
from onerc_healthcare.reporting.registry import CellKey, ElementSpec, SourceContext, cell_key

SOURCE_TYPE = "Diagnosis Category"
CASE_NEW = "New"
DIAGNOSES_FIELD = "krcs_diagnoses"
REFERRAL_INTERNAL = "Internal"
SEX_UNKNOWN = "Unknown"

EXCEPTION_UNMAPPED = "Unmapped diagnosis"
EXCEPTION_NO_DOB = "Missing date of birth"
EXCEPTION_CONFIG = "Mapping configuration"

MAX_CHAIN = 20  # referral generations followed (guards against a cycle in bad data)
EARLIEST = date(1900, 1, 1)


@dataclass(frozen=True)
class _Spec:
	element: ElementSpec
	category: str
	min_months: int | None
	max_months: int | None

	@property
	def has_age_limit(self) -> bool:
		return self.min_months is not None or self.max_months is not None

	def accepts(self, months: int | None) -> bool:
		if not self.has_age_limit:
			return True
		if months is None:
			return False
		if self.min_months is not None and months < self.min_months:
			return False
		return self.max_months is None or months < self.max_months


@dataclass
class _Encounter:
	name: str
	date: date
	creation: str
	patient: str
	sex: str
	months: int | None
	parent: str | None  # source encounter of the internal referral that created this encounter's visit
	in_period: bool
	categories: set[str] = field(default_factory=set)
	unmapped: dict[str, str] = field(default_factory=dict)  # code value -> title

	@property
	def order(self) -> tuple:
		return (self.date, self.creation, self.name)


def evaluate(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	"""Counts for the "Diagnosis Category" elements of one report (module docstring)."""
	resolver = mapping.Resolver()
	specs = _specs(ctx, elements, resolver)
	if not specs:
		return {}
	encounters: dict[str, _Encounter] = {}
	_collect(ctx, _query(ctx), resolver, encounters)
	_load_referral_sources(ctx, resolver, encounters)
	_apply_confirmation(resolver, encounters)
	attributed = _attribute(resolver, encounters)

	counts: dict[CellKey, int] = defaultdict(int)
	by_category: dict[str, list[_Spec]] = defaultdict(list)
	for spec in specs:
		by_category[spec.category].append(spec)
	for (_episode, category), enc in attributed.items():
		for spec in by_category.get(category, ()):
			if spec.accepts(enc.months):
				key = cell_key(spec.element, band_for(enc.months, ctx.age_bands), enc.sex or SEX_UNKNOWN)
				counts[key] += 1
	_record_exceptions(ctx, specs, resolver, encounters, attributed)
	return dict(counts)


# --- elements -------------------------------------------------------------------------------------------


def _specs(ctx: SourceContext, elements: list[ElementSpec], resolver: mapping.Resolver) -> list[_Spec]:
	specs = []
	for element in elements:
		rule = element.rule
		if isinstance(rule, str):
			rule = frappe.parse_json(rule) if rule.strip() else {}
		rule = rule or {}
		category = cstr(rule.get("category")).strip()
		if not category or category not in resolver.categories:
			ctx.add_exception(
				EXCEPTION_CONFIG,
				f"Element rule names no known MOH Disease Category ({category or 'none'}); not counted.",
				element=element.code,
			)
			continue
		low, high = rule.get("age_min_months"), rule.get("age_max_months")
		specs.append(
			_Spec(
				element=element,
				category=category,
				min_months=None if low in (None, "") else cint(low),
				max_months=None if high in (None, "") else cint(high),
			)
		)
	return specs


# --- loading --------------------------------------------------------------------------------------------


def _query(ctx: SourceContext, names: set[str] | None = None) -> list[frappe._dict]:
	"""New diagnoses with their encounter, patient age and referral link.

	`names`: only these encounters, and keep encounters without a New diagnosis (their referral link
	still chains an episode)."""
	where, params = filters.encounter_conditions(ctx)
	params.update(
		{
			"krcs_r2_parenttype": ENCOUNTER_DOCTYPE,
			"krcs_r2_field": DIAGNOSES_FIELD,
			"krcs_r2_new": CASE_NEW,
			"krcs_r2_internal": REFERRAL_INTERNAL,
		}
	)
	join = "JOIN"
	if names is not None:
		join = "LEFT JOIN"
		where += " AND enc.name IN %(krcs_r2_names)s"
		params["krcs_r2_names"] = sorted(names)
	return frappe.db.sql(
		f"""SELECT enc.name AS encounter, enc.encounter_date, enc.creation, pat.name AS patient, pat.sex,
			pat.dob,
			{age_months_sql("pat.dob", "enc.encounter_date")} AS age_months,
			ref.source_encounter, d.code, cv.code_value, cv.code_system, cv.display, cv.krcs_blocks
		FROM {filters.ENCOUNTER_FROM}
		{join} `tab{ENCOUNTER_DIAGNOSIS_DOCTYPE}` d ON d.parent = enc.name
			AND d.parenttype = %(krcs_r2_parenttype)s AND d.parentfield = %(krcs_r2_field)s
			AND d.case_type = %(krcs_r2_new)s
		LEFT JOIN `tabCode Value` cv ON cv.name = d.code
		LEFT JOIN `tab{REFERRAL_DOCTYPE}` ref ON ref.name = v.referred_from
			AND ref.referral_type = %(krcs_r2_internal)s
		WHERE {where}""",
		params,
		as_dict=True,
	)


def _collect(
	ctx: SourceContext, rows: list, resolver: mapping.Resolver, encounters: dict[str, _Encounter]
) -> None:
	from_date, to_date = getdate(ctx.from_date), getdate(ctx.to_date)
	for r in rows:
		enc = encounters.get(r.encounter)
		if enc is None:
			months = r.age_months
			if r.dob is None or months is None or months < 0:
				months = None
			day = getdate(r.encounter_date)
			enc = encounters[r.encounter] = _Encounter(
				name=r.encounter,
				date=day,
				creation=cstr(r.creation),
				patient=r.patient,
				sex=cstr(r.sex),
				months=None if months is None else cint(months),
				parent=r.source_encounter or None,
				in_period=from_date <= day <= to_date,
			)
		if not r.code:
			continue
		icd = r.code_system == ICD11_CODE_SYSTEM
		res = resolver.resolve_value(r.code_value if icd else None, r.krcs_blocks if icd else None)
		if res.category is None:  # no catch-all configured: reported by _record_exceptions
			enc.unmapped[r.code_value or r.code] = cstr(r.display)
			continue
		category = resolver.categories[res.category]
		if category.excluded:
			continue
		enc.categories.add(category.name)
		if res.unmapped:
			enc.unmapped[r.code_value or r.code] = cstr(r.display)


def _load_referral_sources(
	ctx: SourceContext, resolver: mapping.Resolver, encounters: dict[str, _Encounter]
) -> None:
	"""Referral-source encounters that the period query did not return (before the period, or without a
	New diagnosis), generation by generation. A source that fails the base filters ends the chain."""
	wide = dataclasses.replace(ctx, from_date=EARLIEST, exceptions=[])
	missing: set[str] = set()
	for _generation in range(MAX_CHAIN):
		wanted = {
			e.parent
			for e in encounters.values()
			if e.parent and e.parent not in encounters and e.parent not in missing
		}
		if not wanted:
			return
		_collect(ctx, _query(wide, wanted), resolver, encounters)
		missing |= wanted - set(encounters)


def _apply_confirmation(resolver: mapping.Resolver, encounters: dict[str, _Encounter]) -> None:
	"""Replace an unconfirmed lab-confirmed category by its fallback (module docstring, rule 4)."""
	needs = {
		name: e
		for name, e in encounters.items()
		if any(resolver.categories[c].requires_lab_confirmation for c in e.categories)
	}
	if not needs:
		return
	templates = sorted(
		{
			t
			for e in needs.values()
			for c in e.categories
			if resolver.categories[c].requires_lab_confirmation
			for t in resolver.categories[c].tests
		}
	)
	found: dict[str, set[tuple[str, str]]] = defaultdict(set)
	if templates:
		patients = sorted({e.patient for e in needs.values()})
		rows = frappe.db.sql(
			"""SELECT obs.observation_template, obs.result, obs.reference_doctype, obs.reference_docname,
				sr.order_group
			FROM `tabObservation` obs
			LEFT JOIN `tabService Request` sr ON sr.name = obs.service_request
			WHERE obs.patient IN %(patients)s AND obs.docstatus = 1
				AND obs.observation_template IN %(templates)s""",
			{"patients": patients or [""], "templates": templates},
			as_dict=True,
		)
		for r in rows:
			result = (r.observation_template, cstr(r.result).strip().lower())
			if r.order_group in needs:
				found[r.order_group].add(result)
			if r.reference_doctype == ENCOUNTER_DOCTYPE and r.reference_docname in needs:
				found[r.reference_docname].add(result)
	for enc in needs.values():
		for name in list(enc.categories):
			category = resolver.categories[name]
			if not category.requires_lab_confirmation:
				continue
			if any(t in category.tests and v in category.positives for t, v in found.get(enc.name, ())):
				continue
			enc.categories.discard(name)
			fallback = category.fallback if category.fallback in resolver.categories else resolver.catch_all
			if fallback and not resolver.categories[fallback].excluded:
				enc.categories.add(fallback)


# --- episodes -------------------------------------------------------------------------------------------


def _episode_root(name: str, encounters: dict[str, _Encounter]) -> str:
	seen = {name}
	current = name
	for _generation in range(MAX_CHAIN):
		parent = encounters[current].parent
		if not parent or parent not in encounters or parent in seen:
			break
		seen.add(parent)
		current = parent
	return current


def _winners(resolver: mapping.Resolver, names: set[str]) -> set[str]:
	"""Group priority: of each group keep only its highest-priority categories (ties all stay)."""
	best: dict[str, int] = {}
	for name in names:
		c = resolver.categories[name]
		if c.group:
			best[c.group] = max(best.get(c.group, c.priority), c.priority)
	return {
		n
		for n in names
		if not resolver.categories[n].group
		or resolver.categories[n].priority >= best[resolver.categories[n].group]
	}


def form_for(months: int | None) -> str | None:
	if months is None:
		return None
	return mapping.FORM_705A if months < UNDER_FIVE_MONTHS else mapping.FORM_705B


def _attribute(
	resolver: mapping.Resolver, encounters: dict[str, _Encounter]
) -> dict[tuple[str, str], _Encounter]:
	"""(episode root, final category) -> the in-period encounter it is counted at (rules 5 and 6)."""
	episodes: dict[str, list[_Encounter]] = defaultdict(list)
	for name, enc in encounters.items():
		episodes[_episode_root(name, encounters)].append(enc)
	attributed: dict[tuple[str, str], _Encounter] = {}
	for root, members in episodes.items():
		members.sort(key=lambda e: e.order)
		present = set().union(*(m.categories for m in members))
		for name in _winners(resolver, present):
			first = next(m for m in members if name in m.categories)
			if not first.in_period:
				continue  # counted by the report of an earlier period
			final = name
			if not resolver.categories[name].on_form(form_for(first.months)):
				final = resolver.catch_all
			if not final:
				continue
			key = (root, final)
			if key not in attributed or first.order < attributed[key].order:
				attributed[key] = first
	return attributed


# --- exceptions -----------------------------------------------------------------------------------------


def _record_exceptions(
	ctx: SourceContext,
	specs: list[_Spec],
	resolver: mapping.Resolver,
	encounters: dict[str, _Encounter],
	attributed: dict[tuple[str, str], _Encounter],
) -> None:
	"""Record IDs only (never patient names): the encounter is the reference."""
	if any(s.has_age_limit for s in specs):
		for enc in sorted(
			{e.name: e for e in attributed.values() if e.months is None}.values(), key=lambda e: e.order
		):
			ctx.add_exception(
				EXCEPTION_NO_DOB,
				"The patient has no date of birth (or one after the encounter date), so this encounter's "
				"diagnoses are not counted on an age-specific form. Record the date of birth or an "
				"estimated age, then regenerate.",
				ENCOUNTER_DOCTYPE,
				enc.name,
			)
	catch_all = resolver.categories.get(resolver.catch_all) if resolver.catch_all else None
	catch_all_specs = [s for s in specs if s.category == resolver.catch_all]
	reported_no_catch_all = False
	for enc in sorted(encounters.values(), key=lambda e: e.order):
		if not enc.in_period or not enc.unmapped:
			continue
		if enc.months is not None and not any(s.accepts(enc.months) for s in specs):
			continue  # the patient is not on this report's form
		element = next((s.element.code for s in catch_all_specs if s.accepts(enc.months)), None)
		for code, title in sorted(enc.unmapped.items()):
			if catch_all is None:
				if not reported_no_catch_all:
					ctx.add_exception(
						EXCEPTION_CONFIG,
						"No catch-all MOH Disease Category is set: unmapped diagnoses are not counted.",
					)
					reported_no_catch_all = True
				detail = f"ICD-11 {code} {title}: no MOH mapping; not counted."
			else:
				detail = f"ICD-11 {code} {title}: no MOH mapping; counted under {catch_all.title}."
			ctx.add_exception(EXCEPTION_UNMAPPED, detail[:500], ENCOUNTER_DOCTYPE, enc.name, element)


# --- code usage (MOH Unmapped Diagnoses, MOH Mapping Coverage) ------------------------------------------


def code_usage(ctx: SourceContext) -> list[frappe._dict]:
	"""Each code used as a New diagnosis in the period (base filters), with its diagnosis and encounter
	counts and its resolution (`category`, `matched_by`, `value`). Before laboratory confirmation and
	episode de-duplication: this is about the mapping, not the report's counts."""
	where, params = filters.encounter_conditions(ctx)
	params.update(
		{"krcs_r2_parenttype": ENCOUNTER_DOCTYPE, "krcs_r2_field": DIAGNOSES_FIELD, "krcs_r2_new": CASE_NEW}
	)
	rows = frappe.db.sql(
		f"""SELECT d.code, cv.code_value, cv.code_system, cv.display, cv.krcs_chapter, cv.krcs_blocks,
			COUNT(*) AS diagnoses, COUNT(DISTINCT enc.name) AS encounters
		FROM {filters.ENCOUNTER_FROM}
		JOIN `tab{ENCOUNTER_DIAGNOSIS_DOCTYPE}` d ON d.parent = enc.name
			AND d.parenttype = %(krcs_r2_parenttype)s AND d.parentfield = %(krcs_r2_field)s
			AND d.case_type = %(krcs_r2_new)s
		LEFT JOIN `tabCode Value` cv ON cv.name = d.code
		WHERE {where}
		GROUP BY d.code, cv.code_value, cv.code_system, cv.display, cv.krcs_chapter, cv.krcs_blocks""",
		params,
		as_dict=True,
	)
	resolver = mapping.Resolver()
	for r in rows:
		icd = r.code_system == ICD11_CODE_SYSTEM
		res = resolver.resolve_value(r.code_value if icd else None, r.krcs_blocks if icd else None)
		r.category, r.matched_by, r.value = res.as_tuple()
		r.unmapped = res.unmapped
		r.excluded = bool(res.category and resolver.categories[res.category].excluded)
	rows.sort(key=lambda r: (-cint(r.encounters), cstr(r.code_value or r.code)))
	return rows
