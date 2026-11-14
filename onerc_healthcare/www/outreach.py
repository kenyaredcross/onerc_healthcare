# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Host page of the offline outreach app (Phase 4, O3; SDD §5.6, OUT-04/05, NFR-18).

- `/outreach` and `/outreach/<path>` (hooks.website_route_rules) render the app shell: the hashed bundle
  from the Vite manifest (`public/outreach/.vite/manifest.json`, built by `bench build`) plus the page boot
  info (site name, timezone, service worker URL, build). The service worker caches this page for offline
  use, so it carries **no** CSRF token and **no** user (protocol §6 F7): the app reads those from
  `get_boot` when it is online.
- `/outreach/sw.js` returns the built service worker as JavaScript with `Service-Worker-Allowed:
  /outreach/` and `Cache-Control: no-cache`, so it can control the whole /outreach/ scope (a worker
  served from /assets/... could not).
"""

import json
import os

import frappe
from frappe.utils import get_fullname, get_system_timezone

no_cache = 1
sitemap = 0

ASSET_BASE = "/assets/onerc_healthcare/outreach/"
SW_URL = "/outreach/sw.js"
SCOPE = "/outreach/"
ENTRY = "index.html"

_manifest_cache: dict[str, tuple[float, dict]] = {}


def _build_dir() -> str:
	return frappe.get_app_path("onerc_healthcare", "public", "outreach")


def _read_manifest() -> dict | None:
	"""The Vite build manifest, re-read only when the file changes."""
	path = os.path.join(_build_dir(), ".vite", "manifest.json")
	try:
		mtime = os.path.getmtime(path)
	except OSError:
		return None
	cached = _manifest_cache.get(path)
	if cached and cached[0] == mtime:
		return cached[1]
	with open(path, encoding="utf-8") as f:
		data = json.load(f)
	_manifest_cache[path] = (mtime, data)
	return data


def get_assets() -> dict | None:
	"""`{"scripts": [...], "styles": [...]}` of the built entry, or None when the app is not built."""
	manifest = _read_manifest()
	if not manifest:
		return None
	entry = manifest.get(ENTRY) or next((c for c in manifest.values() if c.get("isEntry")), None)
	if not entry:
		return None
	styles: list[str] = []
	seen: set[str] = set()

	def collect_css(chunk: dict) -> None:
		for css in chunk.get("css", []):
			if css not in styles:
				styles.append(css)
		for key in chunk.get("imports", []):
			if key not in seen and key in manifest:
				seen.add(key)
				collect_css(manifest[key])

	collect_css(entry)
	return {
		"scripts": [ASSET_BASE + entry["file"]],
		"styles": [ASSET_BASE + css for css in styles],
	}


def _page_boot() -> dict:
	"""What the cached app shell may carry: nothing tied to a user or a browser session (F7)."""
	return {
		"site_name": frappe.local.site,
		"timezone": get_system_timezone(),
		"sw_url": SW_URL,
		"build": str(int(os.path.getmtime(os.path.join(_build_dir(), ".vite", "manifest.json"))))
		if _read_manifest()
		else None,
	}


def _boot() -> dict:
	user = frappe.session.user
	signed_in = user and user != "Guest"
	return {
		**_page_boot(),
		"csrf_token": frappe.sessions.get_csrf_token() if signed_in else None,
		"user": {"name": user, "full_name": get_fullname(user)} if signed_in else None,
	}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_boot() -> dict:
	"""Boot info with the CSRF token and the signed-in user. Never cached by the service worker (`/api/`);
	the app calls it at start when online and after an online sign-in."""
	return _boot()


def _script_json(value: dict) -> str:
	"""JSON safe to embed in an inline <script>: ASCII only (so U+2028/9 are escaped), no `<`, `>` or `&`."""
	return (
		json.dumps(value, separators=(",", ":"))
		.replace("<", "\\u003c")
		.replace(">", "\\u003e")
		.replace("&", "\\u0026")
	)


def _serve_service_worker(context) -> None:
	path = os.path.join(_build_dir(), "sw.js")
	try:
		with open(path, encoding="utf-8") as f:
			context.sw_source = f.read()
	except OSError:
		context.sw_source = "/* The outreach app is not built: run `bench build --app onerc_healthcare`. */"
		context.http_status_code = 404
	frappe.local.response_headers.set("Content-Type", "application/javascript; charset=utf-8")
	frappe.local.response_headers.set("Cache-Control", "no-cache")
	frappe.local.response_headers.set("Service-Worker-Allowed", SCOPE)


def get_context(context):
	context.no_cache = 1
	context.sw_source = None
	if (frappe.form_dict.get("app_path") or "").strip("/") == "sw.js":
		_serve_service_worker(context)
		return context
	frappe.local.response_headers.set("Cache-Control", "no-cache, no-store")
	frappe.local.response_headers.set("X-Robots-Tag", "noindex")
	context.title = "KRCS Outreach"
	context.asset_base = ASSET_BASE
	context.outreach_assets = get_assets()
	context.boot_json = _script_json(_page_boot())
	return context
