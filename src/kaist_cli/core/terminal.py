from __future__ import annotations

import re


# ECMA-48 control strings can end with a BEL, ST, or a final byte.  Remove the
# complete string before handling the remaining C0/C1 controls so hostile text
# cannot move the cursor, open an OSC hyperlink, or write to a clipboard.
_CONTROL_SEQUENCE_RE = re.compile(
    r"(?:[\x1b\x9d][^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)|[\x1b\x90\x98\x9e\x9fP^_][^\x1b\x9c]*(?:\x1b\\|\x9c)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-2A-Z])"
)
_BIDI_CONTROLS = {
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    "\u206a",
    "\u206b",
    "\u206c",
    "\u206d",
    "\u206e",
    "\u206f",
    "\u2060",
    "\ufeff",
}


def sanitize_terminal_text(value: object) -> str:
    """Return untrusted text safe for one human-readable terminal field.

    Newlines, carriage returns, tabs, and other C0/C1 controls are collapsed
    to spaces.  This keeps Korean and other Unicode text intact while making a
    remote value unable to redraw or impersonate surrounding CLI output.
    """

    text = _CONTROL_SEQUENCE_RE.sub("", str(value))
    out: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char in _BIDI_CONTROLS or codepoint == 0x7F or 0x80 <= codepoint <= 0x9F:
            continue
        if codepoint < 0x20:
            out.append(" ")
            continue
        out.append(char)
    return "".join(out)
