"""Difficult words and dictionary definitions for the note helper.

Spotting: a word is "difficult" when it's a real English word (in pyspellchecker's offline list) that is rare in
everyday English. Defining: the Mac's built-in Dictionary (Dictionary Services, offline, no AI); when that has no
entry, the AI fills the definition in on its next pass.
"""
import re
import sys
import threading

from words import STOP

# frequency limits in pyspellchecker's English list (counts per ~1.6 billion words)
LEVELS = {"off": 0, "fewer": 300, "normal": 900, "more": 2500}
_TOK = re.compile(r"(?<![\w'’-])([A-Za-z][a-z]+(?:-[a-z]+)?)(?![\w'’-])")
_lock = threading.Lock()
_wf = None
_cache = {}
COMMON_SKIP = set("""
okay alright gonna wanna gotta yeah yep nope um uh hmm lecture lectures syllabus powerpoint slide slides
email emails online website google zoom chapter chapters verse verses
""".split())


def _freq():
    global _wf
    with _lock:
        if _wf is None:
            try:
                from spellchecker import SpellChecker
                _wf = SpellChecker(language="en", distance=1).word_frequency
            except Exception:
                _wf = False
        return _wf


def rarity(word):
    """How often the word appears in everyday English (0 = unknown word)."""
    wf = _freq()
    if not wf:
        return None
    w = word.lower()
    n = wf[w]
    if n:
        return n
    for suf, rep in (("ies", "y"), ("es", ""), ("s", ""), ("ed", ""), ("ed", "e"), ("ing", ""), ("ing", "e"),
                     ("ly", ""), ("ally", "al")):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            n = wf[w[: -len(suf)] + rep]
            if n:
                return n
    return 0


def _plain_compound(w):
    wf = _freq()
    for i in range(3, len(w) - 3):
        a, b = w[:i], w[i:]
        if wf[a] > 20000 and (rarity(b) or 0) > 1500:
            return True
    return False


def hard_words(text, level="normal", skip=()):
    """Rare real words in a stretch of transcript, first-seen order, at most 3."""
    limit = LEVELS.get(level, LEVELS["normal"])
    if not limit or _freq() is False:
        return []
    skip = {x.lower() for x in skip}
    out, seen = [], set()
    # true sentence starts only: not after an initial like "N. T." or a title like "Dr."
    starts = {m.end() for m in re.finditer(r"(?:^|(?<![A-Z])(?<!\b(?:Dr|Mr|Ms|St))[.?!]\s+|[\"“(]\s*)", text)}
    for m in _TOK.finditer(text):
        w = m.group(1)
        lw = w.lower()
        if len(lw) < 6 or lw in STOP or lw in COMMON_SKIP or lw in seen or lw in skip:
            continue
        if w[0].isupper() and m.start() not in starts:
            continue  # a name or a title, not a vocabulary word
        n = rarity(lw)
        if not n or n >= limit:
            continue  # unknown (often a mis-heard word or a name) or common enough
        if _plain_compound(lw):
            continue  # householder, overthinking: made of everyday words
        seen.add(lw)
        out.append(lw)
        if len(out) >= 3:
            break
    return out


# ---------------------------------------------------------------- the Mac's Dictionary
_dcs = None


def _mac():
    global _dcs
    if _dcs is not None:
        return _dcs
    _dcs = False
    if sys.platform != "darwin":
        return _dcs
    try:
        import ctypes

        class CFRange(ctypes.Structure):
            _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]

        cs = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cs.DCSCopyTextDefinition.restype = ctypes.c_void_p
        cs.DCSCopyTextDefinition.argtypes = [ctypes.c_void_p, ctypes.c_void_p, CFRange]
        _dcs = (ctypes, cs, cf, CFRange)
    except Exception:
        _dcs = False
    return _dcs


def _mac_raw(word):
    lib = _mac()
    if not lib:
        return ""
    ctypes, cs, cf, CFRange = lib
    utf8 = 0x08000100
    b = word.encode("utf-8")
    s = cf.CFStringCreateWithCString(None, b, utf8)
    if not s:
        return ""
    try:
        d = cs.DCSCopyTextDefinition(None, s, CFRange(0, cf.CFStringGetLength(s)))
        if not d:
            return ""
        try:
            size = cf.CFStringGetLength(d) * 4 + 1
            buf = ctypes.create_string_buffer(size)
            return buf.value.decode("utf-8", "replace") if cf.CFStringGetCString(d, buf, size, utf8) else ""
        finally:
            cf.CFRelease(d)
    finally:
        cf.CFRelease(s)


POS = ("noun", "verb", "adjective", "adverb", "preposition", "conjunction", "pronoun", "exclamation", "determiner",
       "abbreviation", "prefix", "suffix")
_CUT = re.compile(r"\s(?:DERIVATIVES|ORIGIN|PHRASES|PHRASAL VERBS|USAGE|NOTE)\b.*$", re.S)


def clean(raw, word=""):
    """Dictionary entry → (headword, part of speech, first sense). Works on the Mac's NOAD-style text."""
    raw = re.sub(r"\s+", " ", raw or "").strip()
    if not raw:
        return "", "", ""
    head = word
    if "|" in raw:
        parts = raw.split("|")
        head = re.sub(r"[·\d]", "", parts[0]).strip() or word
        raw = parts[2] if len(parts) >= 3 else parts[-1]
    raw = _CUT.sub("", raw).strip()
    pos = ""
    m = re.match(r"(" + "|".join(POS) + r")\b\s*", raw)
    if m:
        pos, raw = m.group(1), raw[m.end():]
    raw = re.sub(r"^\(?[A-Z][a-z]+(?: [A-Z][a-z]+)?\)?\s+(?=[a-z])", "", raw) if raw[:1] == "(" else raw
    raw = re.sub(r"\[[^\]]*\]\s*", "", raw)  # [with object], [mass noun]
    raw = re.sub(r"^1\s+", "", raw)
    raw = re.split(r"\s(?:•|▶|2\s)\s?", raw)[0]
    sense = raw.split(":")[0].strip(" ;,.")
    if len(sense) > 200:
        sense = sense[:200].rsplit(" ", 1)[0] + "…"
    return head, pos, (sense[:1].upper() + sense[1:]) if sense else ""


def define(word):
    """(part of speech, definition) from the Mac's Dictionary, or ("", "") when there's none."""
    key = word.lower().strip()
    if not key:
        return "", ""
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        _, pos, sense = clean(_mac_raw(key), key)
    except Exception:
        pos, sense = "", ""
    with _lock:
        _cache[key] = (pos, sense)
    return pos, sense


def available():
    return bool(_mac())
