"""Ask questions of your own material: this class, this course, or all courses.

Every class is cut into small passages (your notes by section, the lecture summaries, questions asked in class, and
the transcript in one-minute pieces). The passages that best match the question are handed to the AI, which answers
only from them and cites them. With no AI, the best passages are shown as they are.
"""
import re
import time

from ai import ask_ai, ai_ready
from store import load_class
from words import TfIdf, content_words, fmt, overlap

STRIP = re.compile(r"^\s*(#{1,6} |> \[!\w+\]\s*|> ?|[-*+] \[[ xX]\] |[-*+] |\d+\. )")


def _clean(line):
    line = STRIP.sub("", line)
    return re.sub(r"==|\*\*|`", "", line).strip()


def class_passages(kid, s, course, klass):
    """Cut one class into passages with enough information to jump back to them."""
    base = {"kid": kid, "class": klass, "course": course}
    out = []
    # my notes, grouped by heading or blank line, about 80 words each
    group, first, words = [], None, 0
    lines = s.get("lines", [])

    def flush():
        nonlocal group, first, words
        text = " ".join(_clean(lines[i]["text"]) for i in group if lines[i]["text"].strip())
        if text.strip():
            out.append({**base, "kind": "note", "line": first, "t": lines[first].get("t"), "text": text, "weight": 1.25})
        group, first, words = [], None, 0

    for i, l in enumerate(lines):
        x = l.get("text", "")
        if (re.match(r"^#{1,3} ", x) or not x.strip()) and group:
            flush()
        if x.strip():
            if first is None:
                first = i
            group.append(i)
            words += len(x.split())
            if words >= 80:
                flush()
    flush()
    # class prep: my prep notes and readings turned into notes, about 100 words each
    for p in s.get("prep", []):
        if p.get("status") != "ready":
            continue
        piece = []
        for x in p.get("text", "").splitlines() + [""]:
            if (re.match(r"^#{1,3} ", x) or not x.strip()) and sum(len(y.split()) for y in piece) >= 40 or len(piece) >= 12:
                text = " ".join(_clean(y) for y in piece if y.strip())
                if text:
                    out.append({**base, "kind": "prep", "prep": p["id"], "t": None, "title": p.get("title", ""),
                                "text": text, "weight": 1.05})
                piece = []
            if x.strip():
                piece.append(x)
        text = " ".join(_clean(y) for y in piece if y.strip())
        if text:
            out.append({**base, "kind": "prep", "prep": p["id"], "t": None, "title": p.get("title", ""), "text": text, "weight": 1.05})
    # lecture summaries
    ov = s.get("overall")
    if ov and (ov.get("summary") or ov.get("points")):
        out.append({**base, "kind": "summary", "t": 0, "block": "overall", "weight": 1.1,
                    "text": f"Whole lecture: {ov.get('title', '')}. {ov.get('summary', '')} " + " ".join(ov.get("points", []))})
    for b in s.get("blocks", []):
        if b.get("quiet"):
            continue
        out.append({**base, "kind": "summary", "t": b["start"], "block": b["id"], "weight": 1.1,
                    "text": f"{fmt(b['start'])}–{fmt(b['end'])} {b['title']}. {b['summary']} " + " ".join(b.get("details", []))})
    for p in s.get("points", []):
        lead = {"question": "Asked in class: ", "deadline": "Due: ", "quote": "Quoted in class: ",
                "source": "From a book/author mentioned in class: ", "term": "Key term: ", "example": "Example used: ",
                }.get(p.get("kind"), "")
        out.append({**base, "kind": "heard", "t": p["t"], "point": p["id"], "weight": 1.0,
                    "text": f"{lead}{p['title']} {p.get('detail', '')}".strip()})
    # transcript in ~60 s pieces
    piece, start = [], None
    for seg in s.get("segs", []):
        if start is None:
            start = seg["t"]
        piece.append(seg["text"])
        if seg["t"] - start >= 60 or sum(len(x.split()) for x in piece) >= 160:
            out.append({**base, "kind": "transcript", "t": start, "text": " ".join(piece), "weight": 0.85})
            piece, start = [], None
    if piece:
        out.append({**base, "kind": "transcript", "t": start, "text": " ".join(piece), "weight": 0.85})
    return out


def gather(hub, kid, scope):
    lib = hub.lib
    k = lib.klass(kid)
    if scope == "class" or not k:
        kids = [kid]
    elif scope == "course":
        kids = [x["id"] for x in lib.classes_in(k["course"])]
    else:
        kids = [x["id"] for c in lib.d["courses"] for x in lib.classes_in(c["id"])]
    passages = []
    for x in kids:
        kk = lib.klass(x)
        if not kk:
            continue
        s = hub.class_copy(x) if x == hub.kid else load_class(x)
        passages += class_passages(x, s, (lib.course(kk["course"]) or {}).get("name", ""), kk["name"])
    return passages, len(kids)


def best(passages, question, budget_words, limit=14):
    if not passages:
        return []
    idx = TfIdf([p["text"] for p in passages])
    scores = idx.rank(question)
    q = set(content_words(question))
    ranked = []
    for p, sc in zip(passages, scores):
        if sc <= 0.02:
            continue
        ranked.append((sc * p["weight"], p))
    ranked.sort(key=lambda r: -r[0])
    chosen, used = [], 0
    for sc, p in ranked:
        n = len(p["text"].split())
        if used + n > budget_words and chosen:
            continue
        if any(overlap(p["text"], c["text"]) > 0.7 for c in chosen):  # lecturer repeating themselves
            continue
        chosen.append(dict(p, score=round(sc, 3)))
        used += n
        if len(chosen) >= limit:
            break
    if not chosen and q:  # nothing scored: fall back to plain word overlap
        for p in passages:
            if q & set(content_words(p["text"])):
                chosen.append(dict(p, score=0))
                if len(chosen) >= 6:
                    break
    return chosen


KIND_LABEL = {"note": "My notes", "summary": "Lecture summary", "heard": "Heard in class", "transcript": "Transcript"}
SCOPE_LABEL = {"class": "this class", "course": "this course", "all": "all courses"}
BUDGET = {"ollama": 2200, "groq": 2500, "gemini": 9000, "claude": 9000}


def answer(hub, kid, question, scope):
    question = question.strip()[:500]
    if not question:
        return {"ok": False, "error": "Type a question first."}
    scope = scope if scope in SCOPE_LABEL else "class"
    passages, n_classes = gather(hub, kid, scope)
    found = best(passages, question, BUDGET.get(hub.cfg.get("provider"), 2500))
    sources = []
    for i, p in enumerate(found, 1):
        src = {k: p.get(k) for k in ("kid", "class", "course", "kind", "line", "t", "block", "point")}
        src.update(n=i, text=p["text"][:400])
        sources.append(src)
    rec = {"q": question, "scope": scope, "at": time.time(), "sources": sources, "classes": n_classes}
    if not found:
        rec.update(mode="none", answer=f"Nothing in {SCOPE_LABEL[scope]} matches that yet. Try other words, or a wider scope.")
    elif ai_ready(hub.cfg):
        material = "\n\n".join(
            f"[{s['n']}] ({KIND_LABEL[s['kind']]} · {s['course']} › {s['class']}"
            + (f" · {fmt(s['t'])}" if s.get("t") else "") + f")\n{p['text']}" for s, p in zip(sources, found))
        prompt = f"""A student is asking about their own class material. Answer using ONLY the material below: their notes, lecture summaries, things heard in class and transcript excerpts (speech recognition, so some words may be mis-heard). Cite the material you use with its number in square brackets, like [2]. If the material doesn't answer the question, say so in one sentence and mention what is closest. Keep it short: 2-6 sentences or a few bullets ("- "). Plain text only.

Question: {question}

Material:
{material}"""
        try:
            rec.update(mode="ai", answer=ask_ai(hub.cfg, prompt, 700).strip())
        except Exception as e:
            rec.update(mode="search", answer=f"{e} Here are the closest passages instead.")
    else:
        rec.update(mode="search", answer="No AI is set up, so here are the closest passages from your material. "
                                         "Choose a free AI in Preferences to get written answers.")
    return {"ok": True, "ask": rec}
