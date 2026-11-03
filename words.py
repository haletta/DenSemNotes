"""Small text helpers shared by the live summary, slide matching and lessons."""
import math
import re

STOP = set(
    "the a an and or but if of to in on at for with is are was were be been it this that these those so as by "
    "from we you they i he she our your their there here what which who will can could would should do does did "
    "not no yes um uh like just really very okay ok right then than also about into over going get got have has "
    "had me my us them its now well kind sort thing things lot want know mean see say said let lets one two "
    "slide page today let's we're you're it's that's there's i'm don't".split()
)
WORD = re.compile(r"[a-zà-ɏ'’]+")
SENT = re.compile(r"(?<=[.?!])\s+")


def fmt(sec):
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return (f"{h}:{m:02d}" if h else f"{m:02d}") + f":{s:02d}"


def to_sec(mmss):
    total = 0
    for part in str(mmss).split(":"):
        try:
            total = total * 60 + int(float(part))
        except ValueError:
            total *= 60
    return total


def word_count(segs):
    return sum(len(s["text"].split()) for s in segs)


def chunk_text(segs):
    return "\n".join(f"[{fmt(s['t'])}] {s['text']}" for s in segs)


def stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def content_words(text):
    return [stem(w) for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 2]


def similarity(a, b):
    """Overlap of content words (0..1)."""
    sa, sb = set(content_words(a)), set(content_words(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def overlap(a, b):
    """Stricter than similarity(): shared content words over all content words (Jaccard)."""
    sa, sb = set(content_words(a)), set(content_words(b))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def sentences(text):
    return [x.strip() for x in SENT.split(text) if x.strip()]


def key_sentences(segs, n, skip=()):
    """Offline fallback: the n most informative sentences, in lecture order."""
    sents, seen = [], {x.lower() for x in skip}
    for s in segs:
        for x in sentences(s["text"]):
            if len(x.split()) >= 6 and x.lower() not in seen and not x.endswith("?"):
                seen.add(x.lower())
                sents.append((s["t"], x))
    freq = {}
    for _, x in sents:
        for w in content_words(x):
            freq[w] = freq.get(w, 0) + 1
    scored = []
    for t, x in sents:
        ws = content_words(x) or [""]
        scored.append((sum(freq.get(w, 0) for w in ws) / len(ws) ** 0.5 + 0.05 * len(ws), t, x))
    best = sorted(scored, reverse=True)[:n]
    return [(t, x) for _, t, x in sorted(best, key=lambda b: b[1])]


QUESTION_FILLER = re.compile(
    r"^(so,?\s+|and,?\s+|okay,?\s+|ok,?\s+|alright,?\s+|now,?\s+)*"
    r"(any (other )?questions|questions|does (that|this) make sense|make sense|right|okay|ok|yeah|you know|got it|"
    r"is (that|this) clear|(is )?everyone with me|are you (all )?with me|can (you|everyone|everybody) hear me|"
    r"see (that|this)|you see|isn't it|aren't they|don't you think|yes|no|what|huh|sorry|good|all right)\b[^?]{0,12}\?$",
    re.I,
)


def questions_in(text):
    """Real questions the speaker asked (not 'any questions?' or 'right?')."""
    out = []
    for x in sentences(text):
        if not x.endswith("?") or len(x.split()) < 4 or QUESTION_FILLER.match(x):
            continue
        out.append(x[:220])
    return out


DEADLINE = re.compile(
    r"\b(due|deadline|exam|quiz|midterm|final exam|assignment|homework|problem set|submit|turn (it )?in|essay|paper is|test (on|next|this))\b",
    re.I,
)


class TfIdf:
    """Tiny TF-IDF index used to guess which slide the lecturer is talking about."""

    def __init__(self, docs):
        self.n = len(docs)
        df = {}
        toks = [content_words(d) for d in docs]
        for ts in toks:
            for w in set(ts):
                df[w] = df.get(w, 0) + 1
        self.idf = {w: math.log((1 + self.n) / (1 + c)) + 1 for w, c in df.items()}
        self.vecs = [self._vec(ts) for ts in toks]

    def _vec(self, ts):
        tf = {}
        for w in ts:
            if w in self.idf:
                tf[w] = tf.get(w, 0) + 1
        v = {w: (1 + math.log(c)) * self.idf[w] for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1
        return {w: x / norm for w, x in v.items()}

    def rank(self, query):
        q = self._vec(content_words(query))
        return [sum(q.get(w, 0) * x for w, x in v.items()) for v in self.vecs]


# ---------------------------------------------------------------- live note helper (no AI needed)
_NAME = r"(?:[A-Z][a-zA-Z'’.-]+(?:\s+(?:de|van|von|of|the|St\.?|[A-Z][a-zA-Z'’.-]+)){0,3})"
_SAY = r"(?:says|said|writes|wrote|argues|argued|calls|called|describes|described|puts it|put it|reminds us|observes|" \
       r"suggests|teaches|taught|claims|insists|notes|defines|tells us|told us|explains)"
NOT_NAMES = {"I", "He", "She", "They", "We", "You", "It", "This", "That", "There", "Here", "What", "Which", "Who",
             "Someone", "Somebody", "Everyone", "Everybody", "People", "Nobody", "One", "And", "But", "So", "Now",
             "Then", "If", "When", "The", "A", "An", "My", "Our", "Your", "His", "Her", "Their", "Professor", "Okay",
             "Yes", "No", "Well", "Because", "Also", "Again", "Just", "Let", "Today", "Scripture", "Bible", "Text",
             "As", "In", "On", "For", "From", "Like", "Even", "Remember", "Think", "Notice", "See", "Look", "Dr", "Mr",
             "Mrs", "Ms", "Book", "Chapter"}
WORK_CUE = re.compile(
    r"\b(?:(?:in|from) (?:his|her|their|the|a|this|that) (?:book|article|essay|paper|letter|novel|poem|sermon|study|"
    r"commentary|lecture|work|classic)|(?:the |a |his |her |their )?book (?:called|titled|named|by)|"
    r"(?:wrote|written|writes) (?:a|the|this) (?:book|article|essay|paper)|author of|in (?:his|her|their) classic|"
    r"(?:his|her|their) (?:book|article|essay|paper))\b", re.I)
ACCORDING = re.compile(r"\b[Aa]ccording to (" + _NAME + r")")
ATTRIB = re.compile(r"(?:^|[\s,;(])(" + _NAME + r")(?:,[^,]{0,40},)?\s+" + _SAY + r"\b")
AS_ATTRIB = re.compile(r"\b[Aa]s (" + _NAME + r")\s+" + _SAY + r"\b")
NAME_BOOK = re.compile(r"(" + _NAME + r"),?\s+(?:in|from)\s+(?:his|her|their)\s+(?:book|article|essay|paper|letter|"
                       r"novel|poem|sermon|commentary|classic|work)\b")
BOOK_BY = re.compile(r"\b(?:book|article|essay|paper|novel|poem|commentary|classic)\b[^.]{0,60}?\bby (" + _NAME + r")")
QUOTE_CUE = re.compile(
    r"\b(?:and I quote|I quote|quote,?|unquote|famous(?:ly)? (?:said|says|line|quote|saying)|once said|"
    r"put it this way|in (?:his|her|their) (?:own )?words|the saying goes|as the saying|there'?s a saying|"
    r"a line from|the line)\b", re.I)
TITLE_AFTER = re.compile(r"\b(?:book|article|essay|paper|work|classic|novel|poem|commentary)(?: called| titled| named)?,?\s+"
                         r"[\"“']?((?:[A-Z][\w'’:-]*|of|the|and|in|on|a|to|for)(?:\s+(?:[A-Z][\w'’:-]*|of|the|and|in|on|a|to|for)){0,7})")
POINT_CUE = re.compile(
    r"\b(?:the (?:main|key|big|central|most important|crucial|real|whole) (?:point|idea|thing|question|takeaway|lesson|"
    r"issue|problem|message)|the point (?:is|here is|i'?m making)|(?:this|that|it) is (?:really |so |very |absolutely )?"
    r"(?:important|key|crucial|essential|central|critical|vital)|remember (?:this|that)|don'?t forget|"
    r"write (?:this|that|it) down|make sure you|you (?:need|have|want) to (?:know|understand|remember|see|grasp)|"
    r"if you (?:remember|get|take away) (?:one|only one|anything)|most important(?:ly)?|the bottom line|"
    r"what i want you to (?:see|know|understand|notice|remember|take)|notice (?:that|how)|here'?s the thing|"
    r"the heart of|the key (?:is|here)|in short|to sum (?:it )?up|in summary|the big idea|my point is)\b", re.I)
TERM_CUE = re.compile(
    r"\b(?:(?:the )?(?:greek|hebrew|latin|technical|theological)?\s?(?:word|term) (?:for (?:this|that|it) )?(?:is )?"
    r"[\"“']?([A-Za-z][\w'’-]*(?: [a-z][\w'’-]*)?)[\"”']?|(?:is|are|was) (?:what we |what i )?called [\"“']?"
    r"([A-Za-z][\w'’-]*(?: [A-Za-z][\w'’-]*){0,2})[\"”']?|([A-Za-z][\w'’-]*(?: [A-Za-z][\w'’-]*){0,2}) "
    r"(?:is defined as|refers to|literally means))", re.I)
EXAMPLE_CUE = re.compile(
    r"\b(?:for example|for instance|let me give you an example|let me tell you a story|here'?s an example|"
    r"imagine (?:a|that|you)|picture (?:a|this)|take the case of|a good example|an illustration|i remember (?:when|once))\b",
    re.I)
TERM_STOP = {"this", "that", "it", "which", "what", "something", "anything", "the", "a", "an", "and", "so", "who",
             "here", "there", "one", "thing", "word", "term", "is", "to", "be"}


def _clean_name(name):
    words = name.strip(" ,.").split()
    while words and words[0].rstrip(".") in NOT_NAMES:
        words.pop(0)
    return " ".join(words)


def _who(x):
    for rx in (NAME_BOOK, BOOK_BY, ACCORDING, AS_ATTRIB, ATTRIB):
        for m in rx.finditer(x):
            name = _clean_name(m.group(1))
            if not name or name.lower() in ("god", "lord") or len(name) < 3:
                continue
            return name
    return ""


def _work(x):
    m = TITLE_AFTER.search(x)
    if m:
        t = m.group(1).strip(" ,.\"”'")
        words = t.split()
        while words and words[-1].lower() in ("of", "the", "and", "in", "on", "a", "to", "for"):
            words.pop()
        if words and words[0][0].isupper():
            return " ".join(words)[:80]
    m = re.search(r"[\"“]([^\"”]{3,80})[\"”]", x)
    if m and WORK_CUE.search(x):
        return m.group(1)
    return ""


def aids_in(text):
    """Things worth writing down, found in a stretch of transcript without AI:
    [(kind, title, detail)] with kind in point | quote | source | term | example."""
    out = []
    for x in sentences(text):
        if len(x.split()) < 5 or x.endswith("?"):
            continue
        x = x[:260]
        who, work = _who(x), ""
        if WORK_CUE.search(x) or (who and re.search(r"\b(book|wrote|writes|written)\b", x, re.I)):
            work = _work(x)
            if work and who and (work in who or who in work):
                who = _who(x.replace(work, "")) if who in work else who
                if who and (who in work or work in who):
                    who = ""
            if who or work:
                out.append(("source", x, " · ".join(v for v in (who, work) if v)))
                continue
        if QUOTE_CUE.search(x) or who:
            q = x
            if who:
                q = re.sub(r"^(?:(?:and|so|now|well)\s+)?(?:[Aa]s|[Aa]ccording to)\s+" + re.escape(who) +
                           r"(?:\s+" + _SAY + r")?\s*[,:]\s*", "", q)
                q = re.sub(r"^" + re.escape(who) + r"\s+" + _SAY + r"(?:\s+that)?[,:]?\s+", "", q)
                q = (q[:1].upper() + q[1:]) if q else x
            out.append(("quote", q, who))
            continue
        m = TERM_CUE.search(x)
        if m:
            term = next((g for g in m.groups() if g), "").strip(" ,.\"”'")
            tw = term.split()
            while tw and tw[-1].lower() in TERM_STOP | {"means", "meaning", "which", "and", "or", "in", "of"}:
                tw.pop()
            if tw and tw[0].lower() in TERM_STOP | {"which", "that", "we", "you", "i", "they"}:
                tw = []
            term = " ".join(tw)
            if term and term.lower() not in TERM_STOP and not all(w.lower() in TERM_STOP for w in term.split()):
                out.append(("term", term[:60], x))
                continue
        if EXAMPLE_CUE.search(x):
            out.append(("example", x, ""))
            continue
        if POINT_CUE.search(x):
            out.append(("point", x, ""))
    return out
