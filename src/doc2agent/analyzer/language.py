from __future__ import annotations

from collections import Counter


def _script(char: str) -> str | None:
    code = ord(char)
    if 0x0590 <= code <= 0x05FF or 0xFB1D <= code <= 0xFB4F:
        return "he"
    if 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F:
        return "ar"
    if 0x0400 <= code <= 0x04FF:
        return "ru"
    if (0x0041 <= code <= 0x005A) or (0x0061 <= code <= 0x007A):
        return "en"
    return None


def detect_language(text: str) -> str:
    counts = Counter(filter(None, (_script(ch) for ch in text)))
    total = sum(counts.values())
    if total == 0:
        return "und"

    ordered = counts.most_common()
    primary, primary_count = ordered[0]
    if len(ordered) > 1 and ordered[1][1] / total >= 0.20:
        return "mixed"
    if primary_count / total >= 0.60:
        return primary
    return "mixed"


def detect_direction(text: str) -> str:
    rtl = 0
    ltr = 0
    for ch in text:
        script = _script(ch)
        if script in {"he", "ar"}:
            rtl += 1
        elif script in {"en", "ru"}:
            ltr += 1

    total = rtl + ltr
    if total == 0:
        return "auto"
    ratio = rtl / total
    if ratio >= 0.60:
        return "rtl"
    if ratio <= 0.40:
        return "ltr"
    return "mixed"
