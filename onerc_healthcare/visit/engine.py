# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Visit state machine. Owned by A2. Status changes only through transition()."""


def transition(visit: str, action: str, **kwargs):
	raise NotImplementedError("Patient Visit transitions are implemented by workstream A2")
