# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Observation / Service Request / Sample Collection doc_events (wired in hooks.py). Owned by B2."""


def observation_on_submit(doc, method=None) -> None:
	pass


def observation_on_cancel(doc, method=None) -> None:
	pass


def service_request_on_cancel(doc, method=None) -> None:
	pass


def sample_collection_before_cancel(doc, method=None) -> None:
	pass
