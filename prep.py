"""Class prep: notes written before class, and PDFs or documents turned into notes.

Each class keeps a list s["prep"] of items:
  {"id", "title", "kind": "note" | "file", "text": Markdown, "status": "ready" | "converting" | "error",
   "note": progress text, "error", "src": {"name", "ext", "pages"}, "by": "ai" | "text", "created", "updated"}
An uploaded file is converted like slides (into the class's docs folder under "prep_<id>", so its pages can be
shown as they are), then its text is turned into study notes by the class-notes AI, or laid out plainly without AI.
"""
import os
import re
import threading
import time
import traceback

import slides
from ai import ai_ready, ask_lesson, lesson_ai, lesson_budget
from store import new_id

PREP_PROMPT = """Turn this reading into study notes a student will read before class{course}. Write Markdown in this format:
- Start with "# {title}" (shorten it if long), then a "> [!IMPORTANT]" callout with the main idea in 1-2 sentences.
- "## " sections that follow the reading's own order and headings, each opening with a sentence on what it covers, then short bullets, one idea each, with the key phrase in **bold**.
- Mark key terms ==term== on first use. Keep every definition, rule, date, name, number, quote (in quotation marks, with who said it) and reference (Bible references as "Eph 2:8-9").
- Rules or patterns as a "> [!TIP]" callout starting "**Rule:**"; charts, paradigms and lists of forms as Markdown tables; numbered lists for steps.
- Keep words in other languages (Hebrew, Greek…) exactly as written in the reading, including vowel points or accents.
- Leave out page numbers, running headers and footers, copyright lines, tables of contents and exercises' answer keys.
- Don't add anything the reading doesn't say. Notes, not a summary: keep the substance, drop the padding.
{part}
Output only the Markdown.

THE READING ({name}):
{text}"""


def _clean(pages):
    """Page texts -> paragraphs, without the hyphenation, running headers and page numbers PDFs carry."""
    lines_seen = {}
    for p in pages:
        for ln in {x.strip() for x in p.splitlines() if x.strip()}:
            lines_seen[ln] = lines_seen.get(ln, 0) + 1
    repeated = {ln for ln, n in lines_seen.items() if len(pages) >= 4 and n >= len(pages) * 0.6 and len(ln) < 80}
    out = []
    for i, p in enumerate(pages):
        keep = []
        for ln in p.splitlines():
            x = ln.strip()
            if not x or x in repeated or re.fullmatch(r"(page\s*)?\d{1,4}(\s*of\s*\d+)?", x, re.I):
                keep.append("")
                continue
            keep.append(x)
        text = "\n".join(keep)
        text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # hyphenated line breaks
        text = re.sub(r"(?<![.!?:;\n])\n(?!\n)", " ", text)     # lines inside a paragraph
        paras = [" ".join(x.split()) for x in re.split(r"\n\s*\n", text) if x.strip()]
        out.append(paras)
    return out


def plain_notes(title, pages):
    """Notes without AI: the reading's text in paragraphs, with a heading per page."""
    out = [f"# {title}", ""]
    for i, paras in enumerate(_clean(pages), 1):
        if not paras:
            continue
        if len(pages) > 1:
            out += [f"## Page {i}", ""]
        for p in paras:
            out += [p, ""]
    return "\n".join(out).strip() + "\n"


class Prep:
    def __init__(self, app):
        self.app = app
        self.hub = app.hub

    # -- helpers
    def _emit(self, kid):
        s = self.hub.class_copy(kid)
        self.hub.emit("prep", {"kid": kid, "prep": s.get("prep", [])})

    def _set(self, kid, pid, **kw):
        def fn(s):
            for p in s.setdefault("prep", []):
                if p["id"] == pid:
                    p.update(kw)
                    p["updated"] = time.time()
        self.hub.update_class(kid, fn)
        self._emit(kid)

    def _check(self, kid):
        if not self.hub.lib.klass(kid):
            raise ValueError("That class no longer exists.")

    # -- notes
    def new(self, data):
        kid = data.get("kid") or self.hub.kid
        self._check(kid)
        n = len(self.hub.class_copy(kid).get("prep", [])) + 1
        item = {"id": new_id(), "title": str(data.get("title") or f"Prep note {n}")[:120], "kind": "note",
                "text": str(data.get("text") or ""), "status": "ready", "created": time.time(), "updated": time.time()}
        self.hub.update_class(kid, lambda s: s.setdefault("prep", []).append(item))
        self._emit(kid)
        return {"ok": True, "id": item["id"]}

    def save(self, data):
        kid = data.get("kid") or self.hub.kid
        self._check(kid)
        kw = {}
        if "title" in data:
            kw["title"] = " ".join(str(data["title"]).split())[:120] or "Untitled"
        if "text" in data:
            kw["text"] = str(data["text"])[:400000]
        found = []

        def fn(s):
            for p in s.setdefault("prep", []):
                if p["id"] == data.get("id"):
                    p.update(kw)
                    p["updated"] = time.time()
                    found.append(1)
        self.hub.update_class(kid, fn)
        if not found:
            return {"ok": False, "error": "That prep note no longer exists."}
        if data.get("emit", True):
            self._emit(kid)
        return {"ok": True}

    def delete(self, data):
        kid = data.get("kid") or self.hub.kid
        self._check(kid)
        pid = data.get("id")
        self.hub.update_class(kid, lambda s: s.__setitem__("prep", [p for p in s.get("prep", []) if p["id"] != pid]))
        import shutil
        shutil.rmtree(slides.doc_dir(kid, "prep_" + str(pid)), ignore_errors=True)
        self._emit(kid)
        return {"ok": True}

    def move(self, data):
        """Reorder: put item `id` at position `to`."""
        kid = data.get("kid") or self.hub.kid

        def fn(s):
            items = s.setdefault("prep", [])
            i = next((n for n, p in enumerate(items) if p["id"] == data.get("id")), None)
            if i is None:
                return
            p = items.pop(i)
            items.insert(max(0, min(len(items), int(data.get("to", 0)))), p)
        self.hub.update_class(kid, fn)
        self._emit(kid)
        return {"ok": True}

    # -- files
    def upload(self, kid, name, body):
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in slides.ACCEPT:
            return {"ok": False, "error": f'"{name}" can\'t be read. Use a PDF, Word, Pages, PowerPoint, Keynote or text file.'}
        self._check(kid)
        pid = new_id()
        folder = slides.doc_dir(kid, "prep_" + pid)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "source." + ext), "wb") as f:
            f.write(body)
        item = {"id": pid, "title": os.path.splitext(name)[0][:120] or "Reading", "kind": "file", "text": "",
                "status": "converting", "note": "Reading the file…", "error": "",
                "src": {"name": name[:120], "ext": ext, "pages": 0}, "created": time.time(), "updated": time.time()}
        self.hub.update_class(kid, lambda s: s.setdefault("prep", []).append(item))
        self._emit(kid)
        threading.Thread(target=self._convert, args=(kid, pid, ext, name), daemon=True).start()
        return {"ok": True, "id": pid}

    def reconvert(self, data):
        kid = data.get("kid") or self.hub.kid
        s = self.hub.class_copy(kid)
        p = next((x for x in s.get("prep", []) if x["id"] == data.get("id")), None)
        if not p or p["kind"] != "file":
            return {"ok": False, "error": "Only an uploaded file can be turned into notes again."}
        self._set(kid, p["id"], status="converting", note="Reading the file…", error="")
        threading.Thread(target=self._convert, args=(kid, p["id"], p["src"]["ext"], p["src"]["name"], True),
                         daemon=True).start()
        return {"ok": True}

    def _convert(self, kid, pid, ext, name, again=False):
        did = "prep_" + pid
        last = [0.0]

        def report(text):
            if time.time() - last[0] > 0.4:
                last[0] = time.time()
                self._set(kid, pid, note=text)
        try:
            if not os.path.exists(os.path.join(slides.doc_dir(kid, did), "text.json")):
                n, _ = slides.convert(kid, did, ext, report, os.path.splitext(name)[0])
            pages = slides.page_texts(kid, did)
            self._set(kid, pid, src={"name": name[:120], "ext": ext, "pages": len(pages)})
            words = sum(len(p.split()) for p in pages)
            title = next((p["title"] for p in self.hub.class_copy(kid).get("prep", []) if p["id"] == pid), "Reading")
            if words < 5:
                self._set(kid, pid, status="ready", by="text", note="",
                          text=f"# {title}\n\n> [!NOTE]\n> This file has no text to read (it may be scanned pictures). "
                               "Its pages are under **Original**. Type your notes here.\n")
                return
            text, by = None, "text"
            if ai_ready(self.hub.cfg) or lesson_ai(self.hub.cfg) in ("claude_code", "claude_api"):
                try:
                    text = self._ai_notes(kid, pid, title, name, pages, report)
                    by = "ai"
                except Exception as e:
                    traceback.print_exc()
                    report(f"The AI couldn't write notes ({str(e)[:120]}); laying out the text instead…")
            if not text:
                text = plain_notes(title, pages)
            self._set(kid, pid, status="ready", by=by, note="", text=text)
        except Exception as e:
            traceback.print_exc()
            self._set(kid, pid, status="error", note="", error=str(e)[:300])

    def _ai_notes(self, kid, pid, title, name, pages, report):
        budget = max(2500, min(lesson_budget(self.hub.cfg), 14000))
        parts, cur, n = [], [], 0
        for i, p in enumerate(pages, 1):
            w = len(p.split())
            if cur and n + w > budget:
                parts.append(cur)
                cur, n = [], 0
            cur.append((i, p))
            n += w
        if cur:
            parts.append(cur)
        course = self.hub.course_brief(kid) if hasattr(self.hub, "course_brief") else ""
        hebrew = self.hub.hebrew_rules(kid, "prep") if hasattr(self.hub, "hebrew_rules") else ""
        out = []
        for j, chunk in enumerate(parts, 1):
            report(f"Writing notes… part {j} of {len(parts)}" if len(parts) > 1 else "Writing notes…")
            text = "\n\n".join(f"[page {i}]\n{p.strip()}" for i, p in chunk if p.strip())
            part = ""
            if len(parts) > 1:
                part = (f"This is part {j} of {len(parts)} of the reading (pages {chunk[0][0]}-{chunk[-1][0]}). "
                        + ("" if j == 1 else "Don't repeat the title or the IMPORTANT callout; start with its sections.")
                        + "\n")
            md = ask_lesson(self.hub.cfg, PREP_PROMPT.format(course=f" in {course}" if course else "", title=title,
                                                          part=part, name=name, text=text) + hebrew, 6000)
            md = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", md.strip())
            if j > 1:
                md = re.sub(r"^#\s.*\n+", "", md)
            out.append(md.strip())
        return "\n\n".join(out) + "\n"


def prep_material(s, words):
    """The class prep as text for the class-notes AI, at most `words` words."""
    out, n = [], 0
    for p in s.get("prep", []):
        if p.get("status") != "ready" or not p.get("text", "").strip():
            continue
        body = p["text"].split()
        take = body[:max(0, words - n)]
        if not take:
            break
        out.append(f"[{p['title']}{' (my notes)' if p['kind'] == 'note' else ' (reading)'}]\n" + " ".join(take))
        n += len(take)
    return "\n\n".join(out)
