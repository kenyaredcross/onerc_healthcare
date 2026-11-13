# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""frontdesk install hook (setup/install.py calls install() on after_install and after_migrate). Idempotent.

Seeds the default screening parameter (Temperature, flagged above 37.5 °C; provisional, FR-42). An existing
parameter is never edited, so the Medical In-Charge's changes survive migrate.
"""

from onerc_healthcare.frontdesk.screening import seed_default_parameters


def install() -> None:
	seed_default_parameters()
