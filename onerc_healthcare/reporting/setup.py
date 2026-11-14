# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""R1: MOH reporting framework install (the DocTypes are synced from JSON).

`install()` runs on install and migrate (first of the reporting installs). It is idempotent and never
commits: it syncs the MOH Report definitions seeded in ``reporting/data`` (`definitions.sync_definitions`),
which inserts missing ones and never overwrites a definition the HIS Officer has edited.
"""

from onerc_healthcare.reporting.definitions import sync_definitions


def install() -> None:
	sync_definitions()
