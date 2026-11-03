"""Make a MyNotes lesson from what the student wrote, what was said, and the slides they were on."""
import json
import os
import re
import subprocess
import sys
import threading

from ai import NAMES, ask_ai, ai_ready
from store import EXPORT_ROOT, lesson_keep, safe_name
from words import chunk_text, fmt, word_count

BLENDS = {
    "notes": "Build the lesson around (A), in the student's own words. Use (B), (C) and (D) only to clarify, correct "
    "and complete what the student wrote: finish a half-written definition, fix a mis-remembered detail, fill a "
    "short gap. Add a lecture-only section only where (A) has nothing for roughly 5 minutes or more of lecture.",
    "balanced": "Use (A) as the backbone and keep its wording. Add the important lecture content from (B) and (C) "
    "that (A) misses, each piece placed where it happened in time.",
    "lecture": "Build the lesson mainly from the lecture, (B) and (C). Weave every idea from (A) into the matching "
    "place, keeping the student's wording for those ideas.",
}

LESSON_RULES = """FORMAT RULES (MyNotes Markdown), follow exactly:
1. Start with front matter between --- lines: title, course, order{bible_rule}. No tags. Use these values where given: {fm}
2. One "# Title" (same as the front matter title). Split content into "## Sections" whose headings are 2-6 words, each short enough for one slide (3-6 bullets or 1-2 short paragraphs). Use "###" for sub-points. Never skip heading levels.
3. Right after the title, a "> [!IMPORTANT]" callout with the lesson's main idea in 1-2 sentences.
4. Use callouts where they fit: "> [!NOTE]" background, "> [!TIP]" application, "> [!WARNING]" common mistakes, "> [!EXAMPLE]" illustrations or stories the lecturer told. At most 2 IMPORTANT callouts.
5. Mark key terms with ==term== on first use, and end the file with a "## Glossary" section of "- term: definition" lines, spelled exactly as marked.
6. Bible references: standard form with chapter and verse (e.g. "Eph 2:8-9", "1 Cor 13:4-7"). Speech recognition writes them as words ("john three sixteen"); convert them. For the main passage of a section, a code block with language "verse" containing only the reference. Never quote Bible text. Skip this rule if the lecture has no Bible references.
7. Processes or sequences: a heading containing "Steps" with a numbered list.
8. Two or more views compared: "::: tabs" then "### Name" blocks, closed with ":::".
9. At least one ```mermaid diagram (flowchart, timeline or mindmap) where it clarifies relationships, order or history. Put labels containing parentheses, colons or quotes in double quotes.
10. Optional background or deep detail inside <details><summary>Title</summary> ... </details>, with blank lines inside.
11. After major sections, 1-2 self-check questions as a "Q:" line followed by an "A:" line (1-2 sentence answers). Questions the lecturer asked in class make good Q:/A: pairs. Then a "## Summary" section (3-5 bullet takeaways) right before the Glossary. No Review section and no questions at the end.
12. Keep any [[Lesson Title]] links the student wrote.
13. Output only the Markdown file. No code fence around it, nothing before or after it."""

CAPS = {"ollama": 6000, "groq": 3500, "gemini": 40000, "claude": 40000}

# ---------------------------------------------------------------- the student's MyNotes template
HERE = os.path.dirname(os.path.abspath(__file__))
BUILTIN_TEMPLATE = os.path.join(HERE, "template.md")


def template_path():
    from store import DATA
    custom = os.path.join(DATA, "template.md")
    return custom if os.path.exists(custom) else BUILTIN_TEMPLATE


def template_rules():
    """The 'AI Conversion Prompt' written in the template, without its 'My notes:' tail. None if not found."""
    try:
        with open(template_path(), encoding="utf-8") as f:
            md = f.read()
    except OSError:
        return None
    m = re.search(r"(?im)^#{1,3}\s*AI Conversion Prompt.*$", md)
    if not m:
        return None
    fence = re.search(r"(?m)^(`{3,})[\w-]*\s*\n(.*?)\n\1\s*$", md[m.end():], re.S)
    if not fence:
        return None
    text = re.split(r"(?m)^\s*My notes:\s*$", fence.group(2))[0].strip()
    return text if len(text) > 200 else None


def template_info():
    custom = template_path() != BUILTIN_TEMPLATE
    return {"custom": custom, "name": "Your template" if custom else "MyNotes guide (built in)",
            "rules": bool(template_rules())}


STYLE = """HOW THE LESSON SHOULD READ (it is shown like a clean study page):
- Mostly short bullets under each ## section; a short paragraph only to introduce a list or a quote.
- Bold the key phrase of a bullet with **...**, and use *italics* for words the lecturer stressed.
- When the lecturer quotes or credits someone (an author, a book), name them: "Henri Nouwen on ==spiritual reading==:". Lines marked QUOTE: keep word for word as a "> " quote with "— Name" after it. Lines marked FROM A BOOK/AUTHOR: name the author and the *Book Title* in italics. Lines marked KEY TERM: mark the term with ==term== and add it to the Glossary.
- Give a section an "Example:" bullet when the lecturer told a story or gave a case, or use > [!EXAMPLE].
- Use > [!WARNING] for a mistake or danger the lecturer warned about, phrased as one clear sentence.
- Keep the lecturer's own words and images where they are striking (for example "sheep first").
- Section headings are the lecture's main ideas in 2-6 words, in the order the lecturer taught them."""


OVERRIDES = """THE STUDENT'S CHANGES (these win over anything above):
- No tags anywhere: leave "tags" out of the front matter and write no #hashtags.
- No questions at the end: no "## Review" section and no Q:/A: pairs after the Summary. End with "## Summary", then "## Glossary"."""


def _slide_tag(sl, docs):
    if not sl:
        return ""
    if len(docs) > 1:
        name = next((d["name"] for d in docs if d["id"] == sl.get("doc")), "slides")
        return f" · {os.path.splitext(name)[0][:24]} p{sl['page']}"
    return f" · slide {sl['page']}"


def lesson_material(s, sources, cap_words):
    """Collect timed material for the prompt, trimming the transcript to fit the AI's budget."""
    docs = s.get("docs", [])
    wrote = [((l.get("t") or 0), l["text"] + "", l.get("sl")) for l in s.get("lines", []) if l.get("text", "").strip()]
    lead = {"question": "QUESTION ASKED: ", "deadline": "DUE: ", "quote": "QUOTE: ", "source": "FROM A BOOK/AUTHOR: ",
            "term": "KEY TERM: ", "example": "EXAMPLE: ", "point": "KEY POINT: "}
    points = [(p["t"], lead.get(p["kind"], "") + p["title"]
               + (f" — {p['detail']}" if p.get("detail") else ""), p.get("sl")) for p in s["points"]]
    segs = s["segs"]
    trimmed = False
    if "heard" in sources and word_count(segs) > cap_words:
        anchors = [t for t, _, _ in wrote] or [segs[i]["t"] for i in range(0, len(segs), max(1, len(segs) // 20))]
        keep, used = set(), 0
        for window in (45, 90, 180):
            for a in anchors:
                for i, sg in enumerate(segs):
                    if i not in keep and a - window <= sg["t"] <= a + window / 3:
                        n = len(sg["text"].split())
                        if used + n > cap_words:
                            break
                        keep.add(i)
                        used += n
        segs = [segs[i] for i in sorted(keep)]
        trimmed = True
    return wrote, points, segs, trimmed, docs


def build_prompt(s, opts, cap_words, slide_text):
    sources = opts.get("sources") or ["wrote", "heard"]
    wrote, points, segs, trimmed, docs = lesson_material(s, sources, cap_words)
    fm = [f"{k}: {str(opts.get(k)).strip()}" for k in ("title", "course", "order", "tags", "bible")
          if str(opts.get(k) or "").strip()]
    fm_txt = "; ".join(fm) if fm else "(none given: choose a clear title; leave course blank; order: 1)"
    bible_rule = ", and bible (translation code) if given" if opts.get("bible") else ""
    parts = ["Turn a student's lecture into one lesson file for the MyNotes app.",
             f"The lecture: {opts.get('course') or 'not named'}. Total length {fmt(s['elapsed'])}."]
    if "wrote" in sources and wrote:
        parts.append(
            "(A) THE STUDENT'S WRITTEN NOTES, typed during the lecture; [mm:ss · slide N] is when each line was written "
            "and which slide was on screen. These are the student's own words: keep every idea in them, even personal "
            "reflections and questions. If a line already uses MyNotes syntax (callouts, ==terms==, Q:/A:, verse blocks, "
            "[[links]], checklists), keep it.\n"
            + "\n".join(f"[{fmt(t)}{_slide_tag(sl, docs)}] {x}" for t, x, sl in wrote))
    blocks = [b for b in s.get("blocks", []) if not b.get("quiet")]
    if "heard" in sources and (blocks or points):
        txt = ""
        ov = s.get("overall")
        if ov and ov.get("summary"):
            txt += f"WHOLE LECTURE: {ov.get('title', '')}\n{ov['summary']}\n" + "\n".join("- " + x for x in ov.get("points", [])) + "\n\n"
        for b in blocks:
            txt += f"[{fmt(b['start'])}–{fmt(b['end'])}{_slide_tag(b.get('sl'), docs)}] {b['title']}\n{b['summary']}\n"
            txt += "".join(f"- {d}\n" for d in b.get("details", []))
        if points:
            txt += "\n" + "\n".join(f"[{fmt(t)}{_slide_tag(sl, docs)}] {x}" for t, x, sl in points)
        parts.append("(B) LECTURE SUMMARIES written during class (part by part), and things heard in class:\n" + txt.strip())
    if "heard" in sources and segs:
        note = " (excerpts around the moments the student wrote notes; the full recording was longer)" if trimmed else ""
        parts.append(f"(C) LECTURE TRANSCRIPT from speech recognition{note}. Words may be mis-heard: fix obvious "
                     "errors from context.\n" + chunk_text(segs))
    used = []
    for _, _, sl in wrote + points:
        if sl and (sl["doc"], sl["page"]) not in used:
            used.append((sl["doc"], sl["page"]))
    if used:
        budget, blocks = 2500, []
        for doc, page in used:
            text = re.sub(r"\s+", " ", slide_text({"doc": doc, "page": page}) or "").strip()
            if not text:
                continue
            text = text[:400]
            budget -= len(text.split())
            if budget < 0:
                break
            blocks.append(f"[{_slide_tag({'doc': doc, 'page': page}, docs).strip(' ·')}] {text}")
        if blocks:
            parts.append("(D) TEXT OF THE SLIDES the notes are linked to. Use it to order the sections the way the "
                         "slides go and to spell terms correctly; don't copy slides wholesale.\n" + "\n".join(blocks))
    if "wrote" in sources and "heard" in sources:
        parts.append(
            "HOW TO COMBINE THE WRITTEN AND HEARD VERSIONS:\n"
            f"- {BLENDS.get(opts.get('blend'), BLENDS['balanced'])}\n"
            "- Match them by time and slide: a note written at 12:40 on slide 7 belongs with what was said just before "
            "12:40 about slide 7.\n"
            "- When the student's note and the lecture disagree on a fact, keep the student's line and add the "
            'lecturer\'s version right after it in a "> [!NOTE]" callout starting "Lecture said:".\n'
            "- Don't repeat the same point twice when both versions say it; merge them into one, in the student's wording.\n"
            '- Keep the student\'s own questions; answer them in a "> [!NOTE]" if the lecture answered them.')
    elif "wrote" in sources:
        parts.append("Keep ALL of the student's content and meaning: reorganize and format it, don't add new teaching.")
    parts.append("Don't invent facts that are in none of the sources.")
    rules = template_rules()
    if rules:
        parts.append("THE STUDENT'S MyNotes TEMPLATE INSTRUCTIONS (\"my notes\" means everything above, blended as described). "
                     f"Follow them exactly. Use these front matter values where given: {fm_txt}"
                     + (" and add bible: " + str(opts.get("bible")) if opts.get("bible") else "") + "\n\n" + rules)
    else:
        parts.append(LESSON_RULES.format(fm=fm_txt, bible_rule=bible_rule))
    parts.append(STYLE)
    parts.append(OVERRIDES)
    return "\n\n".join(parts)


def clean_lesson(md, opts):
    md = md.strip()
    m = re.match(r"^(`{3,})[\w-]*\n(.*)\n\1\s*$", md, re.S)
    if m:
        md = m.group(2).strip()
    given = {k: str(opts.get(k)).strip() for k in ("title", "course", "order", "bible")
             if str(opts.get(k) or "").strip()}
    if given.get("tags") and not given["tags"].startswith("["):
        given["tags"] = "[" + ", ".join(t.strip().lower() for t in given["tags"].split(",") if t.strip()) + "]"
    fm = re.match(r"^---\n(.*?)\n---\n", md, re.S)
    if fm:
        lines = fm.group(1).splitlines()
        for k in given:
            lines = [l for l in lines if not l.startswith(k + ":")]
        lines = [f"{k}: {given[k]}" for k in ("title", "course", "order", "bible") if k in given] + lines
        md = "---\n" + "\n".join(lines) + "\n---\n" + md[fm.end():]
    elif given or not md.startswith("---"):
        md = "---\n" + "\n".join(f"{k}: {v}" for k, v in given.items()) + "\n---\n\n" + md
    if given.get("title"):
        md = re.sub(r"(?m)^# .*$", "# " + given["title"], md, count=1)
    texts = md.rstrip().split("\n")
    md = "\n".join(texts[i] for i in lesson_keep(texts))
    return md.rstrip() + "\n"


def basic_lesson(s, opts):
    """No-AI fallback: a valid MyNotes skeleton from the notes and the lecture summaries, in time order."""
    blocks = [b for b in s.get("blocks", []) if not b.get("quiet")]
    if blocks:
        return _basic_from_blocks(s, opts, blocks)
    title = opts.get("title") or opts.get("class_name") or "Lecture Notes"
    wrote = [((l.get("t") or 0), l["text"]) for l in s.get("lines", []) if l.get("text", "").strip()]
    pts = [p for p in s["points"]]
    end = max([s["elapsed"]] + [t for t, _ in wrote] + [p["t"] for p in pts] + [0])
    span = 600 if end > 900 else max(300, end + 1)
    out = [f"# {title}", ""]
    main = next((p for p in pts if p["kind"] == "point"), None)
    if main:
        out += ["> [!IMPORTANT]", f"> {main['title']}", ""]
    t0 = 0
    while t0 <= end:
        bp = [p for p in pts if t0 <= p["t"] < t0 + span and p["kind"] != "question"]
        bq = [p for p in pts if t0 <= p["t"] < t0 + span and p["kind"] == "question"]
        bw = [x for t, x in wrote if t0 <= t < t0 + span]
        bw = [("## " + x[2:]) if x.startswith("# ") else x for x in bw]  # only one # per lesson
        if bp or bw or bq:
            if not (bw and bw[0].startswith("## ")):
                head = " ".join((bp[0]["title"] if bp else (bw[0] if bw else bq[0]["title"]).lstrip("#-*> ")).split()[:6])
                out += [f"## {head.rstrip('.:,;?')}", ""]
            if bw:
                out += bw + [""]
            if bp:
                if bw:
                    out += ["> [!NOTE]", "> From the lecture:"]
                    out += [f"> - {p['title']}" + (f": {p['detail']}" if p.get("detail") else "") for p in bp]
                else:
                    out += [f"- {p['title']}" + (f": {p['detail']}" if p.get("detail") else "") for p in bp]
                out.append("")
            for q in bq:
                ans = q["detail"][7:].strip() if q.get("detail", "").startswith("Answer:") else "(answer from your notes)"
                out += [f"Q: {q['title']}", f"A: {ans}", ""]
        t0 += span
    keep = [p for p in pts if p["kind"] != "question"][:5]
    if keep:
        out += ["## Summary"] + [f"- {p['title']}" for p in keep] + [""]
    return clean_lesson("\n".join(out), {**opts, "title": title})


def _basic_from_blocks(s, opts, blocks):
    title = opts.get("title") or opts.get("class_name") or "Lecture Notes"
    wrote = [((l.get("t") or 0), l["text"]) for l in s.get("lines", []) if l.get("text", "").strip()]
    ov = s.get("overall") or {}
    out = [f"# {title}", ""]
    main = ov.get("summary") or blocks[0]["summary"]
    if main:
        out += ["> [!IMPORTANT]", f"> {main.split('. ')[0].rstrip('.')}.", ""]
    for i, b in enumerate(blocks):
        end = blocks[i + 1]["start"] if i + 1 < len(blocks) else 10 ** 9
        bw = [x for t, x in wrote if (b["start"] if i else -1) <= t < end]
        bw = [("## " + x[2:]) if x.startswith("# ") else x for x in bw]
        if not (bw and bw[0].startswith("## ")):
            out += [f"## {' '.join((b['title'] or 'Part ' + str(i + 1)).split()[:6])}", ""]
        if bw:
            out += bw + [""]
        if b.get("details"):
            out += (["> [!NOTE]", "> From the lecture:"] + [f"> - {d}" for d in b["details"]]) if bw else [f"- {d}" for d in b["details"]]
            out.append("")
        for q in s["points"]:
            if q["kind"] == "question" and b["start"] <= q["t"] < end:
                ans = q["detail"][7:].strip() if q.get("detail", "").startswith("Answer:") else "(answer from your notes)"
                out += [f"Q: {q['title']}", f"A: {ans}", ""]
    keep = ov.get("points") or [b["title"] for b in blocks]
    out += ["## Summary"] + [f"- {x}" for x in keep[:5]] + [""]
    return clean_lesson("\n".join(out), {**opts, "title": title})


class LessonBuilder:
    def __init__(self, hub):
        self.hub = hub
        self.busy = threading.Lock()

    def start(self, kid, opts, replace=False):
        if not self.busy.acquire(blocking=False):
            return {"ok": False, "error": "A lesson is already being made."}
        threading.Thread(target=self._run, args=(kid, opts, replace), daemon=True).start()
        return {"ok": True}

    def _run(self, kid, opts, replace=False):
        hub = self.hub
        try:
            s = hub.class_copy(kid)
            has_notes = any(l.get("text", "").strip() for l in s.get("lines", []))
            if not s["segs"] and not has_notes:
                hub.emit("lesson", {"state": "error", "kid": kid,
                                    "text": "There's nothing yet: record some lecture or write some notes first."})
                if getattr(hub, "lesson_hook", None):
                    hub.lesson_hook(kid, False, "nothing to write from")
                return
            note, md = "", None
            if ai_ready(hub.cfg):
                name = NAMES.get(hub.cfg["provider"])
                hub.emit("lesson", {"state": "working", "kid": kid,
                                    "text": f"{name} is writing the lesson. On Ollama this can take a few minutes…"})
                try:
                    prompt = build_prompt(s, opts, CAPS.get(hub.cfg["provider"], 6000),
                                          lambda sl: hub.slide_text(kid, sl))
                    md = clean_lesson(ask_ai(hub.cfg, prompt, 4000, long=True), opts)
                    if md.count("\n") < 8:
                        raise RuntimeError("The AI's answer was too short to be a lesson.")
                except Exception as e:
                    note, md = f"{e} Made a basic version from your notes and the summary points instead.", None
            if md is None:
                md = basic_lesson(s, opts)
                note = note or ("Basic version (no AI selected). Choose an AI in Preferences for a full lesson with "
                                "questions, glossary and diagrams.")
            if replace:
                hub.replace_with_lesson(kid, md, note, opts.get("blend"))
                if getattr(hub, "lesson_hook", None):
                    hub.lesson_hook(kid, True, note)
            else:
                hub.update_class(kid, lambda c: c.__setitem__("lesson_md", md))
                hub.emit("lesson", {"state": "done", "kid": kid, "md": md, "text": note})
        except Exception as e:
            hub.emit("lesson", {"state": "error", "kid": kid, "text": f"Could not make the lesson: {e}"})
            if getattr(hub, "lesson_hook", None):
                hub.lesson_hook(kid, False, str(e))
        finally:
            self.busy.release()


def course_folder(course_name):
    path = os.path.join(EXPORT_ROOT, safe_name(course_name or "General", 60))
    os.makedirs(path, exist_ok=True)
    return path


def unique_path(folder, name, ext=".md"):
    path = os.path.join(folder, safe_name(name) + ext)
    base, n = path[: -len(ext)], 2
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    return path


def reveal(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    return path.replace(os.path.expanduser("~"), "~")


def save_lesson(md, course_name, fallback):
    m = re.search(r"(?m)^title:\s*(.+)$", md) or re.search(r"(?m)^# (.+)$", md)
    name = (m.group(1).strip() if m else "") or fallback or "Lesson"
    path = unique_path(course_folder(course_name), name)
    with open(path, "w") as f:
        f.write(md)
    return {"ok": True, "path": reveal(path)}


def export_class(s, course_name, class_name, show=True):
    """Everything raw: my notes (with times and slides), live notes, and the full transcript."""
    if not s["segs"] and not any(l.get("text", "").strip() for l in s.get("lines", [])):
        return {"ok": False, "error": "Nothing to save yet."}
    docs = s.get("docs", [])
    mine = "\n".join(l["text"] for l in s.get("lines", [])).strip() or "_none_"
    ov = s.get("overall") or {}
    summ = ""
    if ov.get("summary"):
        summ += f"### {ov.get('title') or 'Whole lecture'}\n\n{ov['summary']}\n\n" + "".join(f"- {x}\n" for x in ov.get("points", [])) + "\n"
    for b in s.get("blocks", []):
        if not b.get("quiet"):
            summ += f"### {fmt(b['start'])}–{fmt(b['end'])} · {b['title']}\n\n{b['summary']}\n\n" + "".join(f"- {d}\n" for d in b.get("details", [])) + "\n"
    icon = {"question": "❓ ", "deadline": "⚠ ", "point": ""}
    pts = "\n".join(
        f"- **[{fmt(p['t'])}{_slide_tag(p.get('sl'), docs)}] {icon.get(p['kind'], '')}{p['title']}**"
        + (f" — {p['detail']}" if p.get("detail") else "") for p in s["points"]) or "_none_"
    tx = "\n".join(f"[{fmt(x['t'])}{_slide_tag(x.get('sl'), docs)}] {x['text']}" for x in s["segs"]) or "_none_"
    files = "\n".join(f"- {d['name']}" for d in docs) or "_none_"
    path = os.path.join(course_folder(course_name), safe_name(class_name) + ".md")
    with open(path, "w") as f:
        f.write(f"# {course_name} · {class_name}\n\n## My notes\n\n{mine}\n\n## Lecture summary\n\n{summ.strip() or '_none_'}\n\n"
                f"## Questions and due dates heard in class\n\n{pts}\n\n"
                f"## Slides and documents\n\n{files}\n\n## Full transcript\n\n{tx}\n")
    return {"ok": True, "path": reveal(path) if show else path.replace(os.path.expanduser("~"), "~")}
