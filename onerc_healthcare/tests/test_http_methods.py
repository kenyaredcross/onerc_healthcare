# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Contract test: every JS call to one of our whitelisted methods uses an HTTP verb the method allows.

Phase 2 review M2: the Data Access Request form called a GET-only method with `frappe.xcall`, which
sends POST by default, so Record Correction could never complete. `frappe.call` and `frappe.xcall`
both default to POST; GET must be asked for explicitly (`type: "GET"` / third xcall argument).
"""

import importlib
import os
import re

import frappe

from onerc_healthcare.tests.base import EMRTestCase

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALL_START = re.compile(r"frappe\s*\.\s*(call|xcall)\s*\(")
# A method expression: a literal, `${CONST}.fn`, CONST + ".fn", or a bare CONST.
_EXPR = r"""(?:["'`][^"'`]*["'`]|[\w.]+\s*\+\s*["'][^"']*["']|[\w.]+)"""
METHOD_IN_CALL = re.compile(r"method\s*:\s*(" + _EXPR + ")")
TYPE_IN_CALL = re.compile(r"""type\s*:\s*["'](GET|POST|PUT|DELETE)["']""", re.IGNORECASE)
XCALL_METHOD = re.compile(r"^\s*(" + _EXPR + ")")
XCALL_VERB = re.compile(r""",\s*["'](GET|POST|PUT|DELETE)["']\s*$""", re.IGNORECASE)
# JS constants holding our module paths, e.g. `krcs.privacy.DAR_API = "onerc_healthcare.privacy.dar"`.
CONSTANT = re.compile(r"""([\w.$]+)\s*[:=]\s*["'`](onerc_healthcare\.[\w.]+)["'`]""")


def resolve_expr(expr: str, constants: dict[str, str]) -> str | None:
	"""Turn a JS method expression into a dotted path, or None if it cannot be resolved statically."""
	expr = expr.strip()
	if expr[:1] in "\"'`":
		body = expr[1:-1]
		body = re.sub(r"\$\{\s*([\w.$]+)\s*\}", lambda m: constants.get(m.group(1), "${?}"), body)
		return None if "${?}" in body else body
	if "+" in expr:
		name, _, tail = expr.partition("+")
		base = constants.get(name.strip())
		return base + tail.strip().strip("\"'") if base else None
	return constants.get(expr)


def _balanced_args(text: str, start: int) -> str:
	"""The text between the call's opening parenthesis (at `start`) and its matching close."""
	depth, i, quote = 1, start, None
	while i < len(text) and depth:
		ch = text[i]
		if quote:
			if ch == "\\":
				i += 1
			elif ch == quote:
				quote = None
		elif ch in "\"'`":
			quote = ch
		elif ch == "(":
			depth += 1
		elif ch == ")":
			depth -= 1
		i += 1
	return text[start : i - 1]


def js_calls() -> list[tuple[str, str, str]]:
	"""(file, method, verb) for every call to an `onerc_healthcare.` method in our JS.

	Calls whose method expression cannot be resolved statically are reported by `unresolved_calls`.
	"""
	return [c for c in _scan() if c[1]]


def unresolved_calls() -> list[str]:
	return [f"{path}: {expr}" for path, method, expr in _scan(raw=True) if not method]


def _scan(raw: bool = False) -> list[tuple[str, str | None, str]]:
	sources = {}
	constants: dict[str, str] = {}
	for root, _dirs, files in os.walk(APP_DIR):
		if "node_modules" in root or "/public/dist" in root:
			continue
		for name in files:
			if name.endswith(".js"):
				path = os.path.join(root, name)
				with open(path, encoding="utf-8") as f:
					sources[path] = f.read()
	for text in sources.values():
		for m in CONSTANT.finditer(text):
			# Only UPPER_CASE names are constants (e.g. krcs.privacy.DAR_API); an object key such as
			# `method: "..."` inside a frappe.call({...}) is not.
			short = m.group(1).rsplit(".", 1)[-1]
			if not re.fullmatch(r"[A-Z][A-Z0-9_]*", short):
				continue
			constants[m.group(1)] = m.group(2)
			constants[short] = m.group(2)
	out = []
	for path, text in sources.items():
		for m in CALL_START.finditer(text):
			args = _balanced_args(text, m.end())
			finder, verb_finder = (
				(XCALL_METHOD, XCALL_VERB) if m.group(1) == "xcall" else (METHOD_IN_CALL, TYPE_IN_CALL)
			)
			found = finder.search(args)
			if not found:
				continue
			expr = found.group(1)
			method = resolve_expr(expr, constants)
			mentions_ours = "onerc_healthcare" in expr or any(k in expr for k in constants)
			if method is None and not mentions_ours:
				continue  # not one of our methods (frappe/Marley literal or unrelated variable)
			if method is not None and not method.startswith("onerc_healthcare."):
				continue
			verb = verb_finder.search(args)
			rel = os.path.relpath(path, APP_DIR)
			if method is None:
				out.append((rel, None, expr))
			else:
				out.append((rel, method, (verb.group(1) if verb else "POST").upper()))
	return out


def allowed_methods(dotted: str) -> list[str] | None:
	"""HTTP verbs the whitelisted function allows, or None if it is not whitelisted / not found."""
	module_path, _, fn_name = dotted.rpartition(".")
	try:
		fn = getattr(importlib.import_module(module_path), fn_name)
	except (ImportError, AttributeError):
		return None
	if fn not in frappe.whitelisted:
		return None
	return [m.upper() for m in frappe.allowed_http_methods_for_whitelisted_func.get(fn, ["GET", "POST"])]


class TestHTTPMethodContract(EMRTestCase):
	def test_js_calls_use_allowed_http_verbs(self):
		calls = js_calls()
		self.assertTrue(calls, "no JS calls found; the scanner is broken")
		problems = []
		for path, method, verb in calls:
			allowed = allowed_methods(method)
			if allowed is None:
				problems.append(f"{path}: {method} is not a whitelisted function")
			elif verb not in allowed:
				problems.append(f"{path}: {method} called with {verb}, allows {allowed}")
		self.assertFalse(problems, "\n".join(problems))

	def test_every_call_to_our_methods_is_resolvable(self):
		"""Keep method names static (literal or a module constant) so this contract can check them."""
		self.assertFalse(unresolved_calls(), "\n".join(unresolved_calls()))
