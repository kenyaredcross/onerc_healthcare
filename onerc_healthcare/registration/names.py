# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Name normalisation and phonetic keys for duplicate detection (FR-2, SDD 4.3).

Keys are computed in Python and stored on Patient (`krcs_first_name_key`, `krcs_last_name_key`, both
indexed), so candidate lookup is an indexed equality instead of a SOUNDEX() scan over the table.
"""

import re
import unicodedata

FIRST_NAME_KEY_FIELD = "krcs_first_name_key"
LAST_NAME_KEY_FIELD = "krcs_last_name_key"

_SOUNDEX_CODES = {
	**dict.fromkeys("BFPV", "1"),
	**dict.fromkeys("CGJKQSXZ", "2"),
	**dict.fromkeys("DT", "3"),
	"L": "4",
	**dict.fromkeys("MN", "5"),
	"R": "6",
}
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalise_name(name: str | None) -> str:
	"""Lower case, punctuation removed, whitespace collapsed: `  O'Brien-Ali ` -> `obrien ali`."""
	parts = (_NON_WORD.sub("", p) for p in (name or "").replace("-", " ").lower().split())
	return " ".join(p for p in parts if p)


def _ascii_letters(name: str) -> str:
	ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
	return "".join(c for c in ascii_name.upper() if "A" <= c <= "Z")


def soundex(name: str | None) -> str:
	"""American Soundex of the Latin letters in `name` (4 characters), or "" if it has none."""
	letters = _ascii_letters(name or "")
	if not letters:
		return ""
	out, prev = letters[0], _SOUNDEX_CODES.get(letters[0], "")
	for c in letters[1:]:
		code = _SOUNDEX_CODES.get(c, "")
		if code and code != prev:
			out += code
			if len(out) == 4:
				break
		if c not in "HW":
			prev = code
	return out.ljust(4, "0")


def name_key(name: str | None) -> str:
	"""Phonetic key of a name; names without Latin letters fall back to their normalised text."""
	key = soundex(name)
	if key:
		return key
	normalised = normalise_name(name).replace(" ", "")
	return f"~{normalised[:30]}" if normalised else ""
