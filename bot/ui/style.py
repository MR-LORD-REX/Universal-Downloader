"""The bot's Telegram "design system".

Follows the project's Telegram formatting rules: Unicode symbols act as the UI
(no decorative emoji), one header symbol, one list symbol, one separator style
and one key/value separator per message.
"""

from __future__ import annotations

# ------------------------------------------------------------- vocabulary
HEADER = "\u2756"  # heavy outlined black heart -> header
SECTION = "\u2726"  # four pointed star -> section
ROW = "\u25b8"  # small right triangle -> list item
KV = "\u00b7"  # middle dot -> key/value
OK = "\u2713"
BAD = "\u2717"
IMPORTANT = "\u25c6"
PENDING = "\u25f7"
EMPTY = "\u25cb"
SUB = "\u2022"
RULE = "\u2501"  # heavy horizontal

# ------------------------------------------------------------ font styles
_BOLD_SANS_UPPER = 0x1D5D4
_BOLD_SANS_LOWER = 0x1D5EE
_BOLD_SANS_DIGIT = 0x1D7EC
_BOLD_UPPER = 0x1D400
_BOLD_LOWER = 0x1D41A
_BOLD_DIGIT = 0x1D7CE
_ITALIC_UPPER = 0x1D434
_ITALIC_LOWER = 0x1D44E
_ITALIC_HOLES = {"h": "\u210e"}

_SMALL_CAPS = {
    "a": "\u1d00",
    "b": "\u0299",
    "c": "\u1d04",
    "d": "\u1d05",
    "e": "\u1d07",
    "f": "\ua730",
    "g": "\u0262",
    "h": "\u029c",
    "i": "\u026a",
    "j": "\u1d0a",
    "k": "\u1d0b",
    "l": "\u029f",
    "m": "\u1d0d",
    "n": "\u0274",
    "o": "\u1d0f",
    "p": "\u1d18",
    "q": "\u01eb",
    "r": "\u0280",
    "s": "\ua731",
    "t": "\u1d1b",
    "u": "\u1d1c",
    "v": "\u1d20",
    "w": "\u1d21",
    "x": "x",
    "y": "\u028f",
    "z": "\u1d22",
}


def _convert(text: str, upper: int, lower: int, digit: int | None = None, holes: dict | None = None) -> str:
    out: list[str] = []
    for char in str(text):
        if holes and char in holes:
            out.append(holes[char])
            continue
        code = ord(char)
        if 65 <= code <= 90:
            out.append(chr(upper + code - 65))
        elif 97 <= code <= 122:
            out.append(chr(lower + code - 97))
        elif digit is not None and 48 <= code <= 57:
            out.append(chr(digit + code - 48))
        else:
            out.append(char)
    return "".join(out)


def bold_sans(text: str) -> str:
    """``Title`` -> ``U0001d5e7U0001d5f6U0001d601U0001d5f9U0001d5f2``."""
    return _convert(text, _BOLD_SANS_UPPER, _BOLD_SANS_LOWER, _BOLD_SANS_DIGIT)


def bold(text: str) -> str:
    """Serif mathematical bold."""
    return _convert(text, _BOLD_UPPER, _BOLD_LOWER, _BOLD_DIGIT)


def italic(text: str) -> str:
    """Mathematical italic (with the classic `h` hole fixed)."""
    return _convert(text, _ITALIC_UPPER, _ITALIC_LOWER, holes=_ITALIC_HOLES)


def small_caps(text: str) -> str:
    """``YouTube`` -> ``ʏU0001d0fU0001d1cU0001d1bU0001d1cʙU0001d07``."""
    return "".join(_SMALL_CAPS.get(char.lower(), char) for char in str(text))


# --------------------------------------------------------------- building
def rule(width: int = 18) -> str:
    """A horizontal separator sized for a phone screen."""
    return RULE * max(3, width)


def header(text: str) -> str:
    return f"{HEADER} {bold_sans(text)}"


def section(text: str) -> str:
    return f"{SECTION} {small_caps(text)}"


def row(key: str, value: object) -> str:
    return f"{ROW} {key} {KV} {value}"


def status(ok: bool, text: str) -> str:
    return f"{OK if ok else BAD} {text}"


__all__ = [
    "BAD",
    "EMPTY",
    "HEADER",
    "IMPORTANT",
    "KV",
    "OK",
    "PENDING",
    "ROW",
    "RULE",
    "SECTION",
    "SUB",
    "bold",
    "bold_sans",
    "header",
    "italic",
    "row",
    "rule",
    "section",
    "small_caps",
    "status",
]
