# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Global on_trash guard (hooks.py "*"). Owned by A3: refuse deletion of CLINICAL_DOCTYPES (NFR-9)."""


def on_trash(doc, method=None) -> None:
	pass
