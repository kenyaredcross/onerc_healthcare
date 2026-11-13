# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Screening Parameter: a value recorded at the facility entrance (FR-42), configured by the Medical
In-Charge. Thresholds flag a result as outside the expected range; the plausible range rejects entry
errors. A limit of 0 means "not set". Submitted Screening Records keep their own snapshot.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from onerc_healthcare.frontdesk.screening import TYPE_NUMERIC, TYPE_SELECT, lines


class ScreeningParameter(Document):
	def validate(self):
		self.label = (self.label or "").strip()
		if self.param_type == TYPE_NUMERIC:
			self.validate_numeric()
			self.options = self.abnormal_options = None
		else:
			self.unit = None
			self.low_threshold = self.high_threshold = self.plausible_min = self.plausible_max = 0
		if self.param_type == TYPE_SELECT:
			self.validate_options()
		elif self.param_type != TYPE_NUMERIC:
			self.options = self.abnormal_options = None

	def validate_numeric(self):
		lo, hi = flt(self.plausible_min), flt(self.plausible_max)
		low, high = flt(self.low_threshold), flt(self.high_threshold)
		if lo and hi and lo >= hi:
			frappe.throw(_("The plausible minimum must be below the plausible maximum."))
		if low and high and low >= high:
			frappe.throw(_("Flag Below must be lower than Flag Above."))
		for label, value in ((_("Flag Below"), low), (_("Flag Above"), high)):
			if value and ((lo and value < lo) or (hi and value > hi)):
				frappe.throw(_("{0} ({1}) must lie within the plausible range.").format(label, value))

	def validate_options(self):
		options = lines(self.options)
		if not options:
			frappe.throw(_("Enter the options, one per line."))
		unknown = [o for o in lines(self.abnormal_options) if o not in options]
		if unknown:
			frappe.throw(_("Not among the options: {0}.").format(", ".join(unknown)))
		self.options = "\n".join(options)
		self.abnormal_options = "\n".join(lines(self.abnormal_options)) or None
