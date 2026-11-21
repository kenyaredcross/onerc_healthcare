# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH counts from the MCH records (MCH-A; provisional, OI-5): source type "MCH Records".

Rule keys (`workload.RULE_SCHEMAS[SOURCE_MCH]`): `record_types` (required, MCH_RECORD_DOCTYPES), `unit`
("records" default, "visits": distinct visits, "patients": distinct patients) and `pnc_subjects` (PNC Visit
only). Counts submitted, non-archived records of non-merged patients dated in the period at the run's
facilities. W0 stub: MCH-A implements `evaluate` and `rule_errors`.
"""

from __future__ import annotations

from onerc_healthcare.reporting.registry import CellKey, ElementSpec, SourceContext


def rule_errors(rule: dict, disaggregation: str | None = None) -> list[str]:
	return []


def evaluate(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	return {}
