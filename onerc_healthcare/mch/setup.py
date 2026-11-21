# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MCH install (W0): seed the KEPI vaccine schedule. Idempotent, never commits."""

from onerc_healthcare.mch import immunisation


def install() -> None:
	immunisation.seed_schedule()
