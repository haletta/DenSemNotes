"""Spell check for My notes. Uses pyspellchecker (offline); learned words are kept per course."""
import re
import sys
import threading

LANGS = {"en", "es", "fr", "de", "pt", "it", "nl", "ru", "ar", "fa", "lv", "eu"}
TOKEN = re.compile(r"[A-Za-zÀ-ɏЀ-ӿ؀-ۿ][A-Za-zÀ-ɏЀ-ӿ؀-ۿ'’]*")
SKIP_LINE = re.compile(r"^\s*(```|<|:::)")
SKIP_SPANS = re.compile(r"`[^`]*`|https?://\S+|\[!\w+\]|\[\[[^\]]*\]\]")
_checkers, _lock = {}, threading.Lock()


def _checker(lang):
    from spellchecker import SpellChecker

    with _lock:
        if lang not in _checkers:
            _checkers[lang] = SpellChecker(language=lang, distance=2)
        return _checkers[lang]


def available(lang):
    if lang not in LANGS:
        return False
    try:
        import spellchecker  # noqa: F401
        return True
    except ImportError:
        return False


def check(lines, lang, known=()):
    """Misspelled words in the notes, first-seen order, with up to 5 suggestions each."""
    if not available(lang):
        return None
    sp = _checker(lang)
    known = {w.lower() for w in known}
    counts, forms, in_fence = {}, {}, False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or SKIP_LINE.match(line):
            continue
        for m in TOKEN.finditer(SKIP_SPANS.sub(" ", line)):
            w = m.group(0).strip("'’")
            if len(w) < 2 or (w.isupper() and len(w) <= 6):
                continue
            key = w.lower()
            counts[key] = counts.get(key, 0) + 1
            forms.setdefault(key, w)
    with _lock:
        unknown = [w for w in counts if w not in known and w in sp.unknown([w])]
        out = []
        for w in unknown[:60]:
            sp.distance = 1 if len(w) > 10 else 2
            cands = sp.candidates(w) or set()
            cands = sorted((c for c in cands if c != w), key=lambda c: -sp.word_usage_frequency(c))[:5]
            orig = forms[w]
            if orig[:1].isupper():
                cands = [c[:1].upper() + c[1:] for c in cands]
            out.append({"word": orig, "count": counts[w], "suggestions": cands})
        sp.distance = 2
    return out


def learn_native(word):
    """Also teach the Mac's own spell checker, so the red underline goes away while typing."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSSpellChecker
        from PyObjCTools import AppHelper

        AppHelper.callAfter(lambda: NSSpellChecker.sharedSpellChecker().learnWord_(word))
    except Exception:
        pass


COMMON = {"teh": "the", "hte": "the", "adn": "and", "taht": "that", "wiht": "with", "thier": "their", "recieve": "receive",
          "seperate": "separate", "definately": "definitely", "becuase": "because", "beleive": "believe", "occured": "occurred",
          "untill": "until", "wich": "which", "alot": "a lot", "acn": "can", "jsut": "just", "waht": "what", "whta": "what"}


def autocorrect(word, lang, known=()):
    """A safe correction for a word just typed, or None. Only fixes lowercase words with one clear correction."""
    w = str(word)
    if w in COMMON and lang == "en":
        return COMMON[w]
    if len(w) < 4 or not w.isalpha() or not w.islower() or not available(lang):
        return None
    if w in {k.lower() for k in known}:
        return None
    sp = _checker(lang)
    with _lock:
        if not sp.unknown([w]):
            return None
        sp.distance = 1
        try:
            cands = [c for c in (sp.candidates(w) or set()) if c != w and c.isalpha()]
        finally:
            sp.distance = 2
        if not cands:
            return None
        ranked = sorted(cands, key=lambda c: -sp.word_usage_frequency(c))
        f1 = sp.word_usage_frequency(ranked[0])
        f2 = sp.word_usage_frequency(ranked[1]) if len(ranked) > 1 else 0
        if f1 <= 0 or (f2 and f1 < 4 * f2):
            return None
        return ranked[0]
