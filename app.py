#!/usr/bin/env python3
"""DenSemNotes — listen, transcribe and take notes in class, all on your Mac.

Courses hold classes ("2026-10-26-Notes"). Each class has your notes, a live transcript, live notes that
update as the lecturer talks, and any slides/documents you upload, with notes wired to slide pages.
"""
import json
import signal
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ask  # noqa: E402
import slides  # noqa: E402
import spell  # noqa: E402
from ai import NAMES, ai_ready, ask_ai, ollama_info, ollama_pull  # noqa: E402
from lesson import LessonBuilder, export_class, save_lesson, template_info, template_path  # noqa: E402
from live import Recorder, Summarizer, Transcriber  # noqa: E402
from store import (APP_NAME, CFG_PATH, HEBREW_HINT, HEBREW_TERMS, Library, course_taught, class_meta, load_class, load_json, new_id, original_notes, save_class,
                   save_json)  # noqa: E402
from words import TfIdf, fmt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
import platform  # noqa: E402

ARM = platform.machine().lower() in ("arm64", "aarch64")
UI_DEFAULTS = {
    "theme": "system", "accent": "green", "translucent": True,
    "side": True, "insp": "sum", "inspW": 380, "focus": False,
    "font": "sans", "size": 17, "lh": 1.65, "width": 700,
    "marks": False, "gutter": True, "spell": True, "autocorrect": True, "follow": False,
    "tools": ["h2", "h3", "bold", "term", "bul", "num", "todo", "callout", "qa", "verse", "link", "spell", "format"],
    "blend": "notes", "bible": "", "readLessons": True, "slideLayout": False, "reviewEarlier": True,
    "filter": None,
    "keys": {},   # shortcut id -> combo, only the ones I changed
    "open": {},   # course id -> expanded in the sidebar
    "aidFilter": "all", "liveSplit": 0.5,
    "discreet": False, "zoomEdit": 1, "zoomRead": 1, "followNotes": True,
}
DEFAULTS = dict(
    course="",
    provider="ollama",  # ollama | gemini | groq | claude | none
    ollama_model="llama3.2",
    gemini_key="",
    lesson_ai="same",  # who writes class notes: same | claude_code (the Claude plan, no key) | claude_api
    lesson_model="sonnet",
    github_token="",  # only needed when the app updates from a private repository
    auto_update_check=True,
    pending_final=[],  # classes still recording when the app was quit: their final summary runs on next start
    gemini_model="gemini-flash-lite-latest",
    groq_key="",
    groq_model="llama-3.3-70b-versatile",
    api_key="",  # Claude
    claude_model="claude-haiku-5-5",
    whisper="small.en" if ARM else "base.en",
    language="en",
    period=0,  # minutes per live summary; 0 = only when the class is paused or finished
    aid_every=2,  # minutes between AI passes of the live note helper (0 = instant detection only)
    helpers={},  # note helper kind -> False for the ones switched off (they're not looked for and cost no AI)
    hard_words="normal",  # difficult words in the note helper: off | fewer | normal | more
    known_words=[],  # difficult words the student removed ("I know this one"): never flagged again
    device=None,
    ui=dict(UI_DEFAULTS),
)
MAX_UPLOAD = 300 * 1024 * 1024


def clone(x):
    return json.loads(json.dumps(x))


# ---------------------------------------------------------------- shared state + events



def _paragraphs(lines, size=90):
    """Join transcript lines into readable paragraphs of about `size` words, breaking after a sentence."""
    out, cur, n = [], [], 0
    for x in lines:
        if not x:
            continue
        cur.append(x)
        n += len(x.split())
        if n >= size and x.rstrip().endswith((".", "?", "!")):
            out.append(" ".join(cur)); cur, n = [], 0
    if cur:
        out.append(" ".join(cur))
    return out

class Hub:
    def __init__(self):
        self.lock = threading.RLock()
        self.subs = []
        self.cfg = load_json(CFG_PATH, DEFAULTS)
        self.cfg["ui"] = {**UI_DEFAULTS, **(self.cfg.get("ui") or {})}
        try:  # very first versions only knew Claude
            with open(CFG_PATH) as f:
                if "provider" not in json.load(f) and self.cfg["api_key"]:
                    self.cfg["provider"] = "claude"
        except Exception:
            pass
        if not self.cfg.get("summary_on_demand"):  # summaries now come only when paused or finished, unless chosen
            self.cfg.update(period=0, summary_on_demand=True)
        self.lib = Library(self.cfg)
        self.cfg["ui"] = {**UI_DEFAULTS, **(self.cfg.get("ui") or {})}
        save_json(CFG_PATH, self.cfg)
        self.kid = self.lib.d["current"]
        self.s = load_class(self.kid)
        self.recording = False
        self.since = 0.0
        self.status = ""
        self.model = {"state": "loading", "name": ""}
        self.summ = None
        self.hints = {}
        self.index_cache = {}
        self.live_objs = {}  # classes being built in the background (imports): kid -> the class in memory
        self.lesson_hook = None

    # -- time & events
    def now(self):
        return self.s["elapsed"] + (time.time() - self.since if self.recording else 0)

    def emit(self, kind, data):
        msg = f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
        with self.lock:
            for q in list(self.subs):
                q.put(msg)

    def set_status(self, text):
        if text != self.status:
            self.status = text
            self.emit("status", {"text": text})

    def persist(self):
        with self.lock:
            save_class(self.kid, self.s)

    def save_and_emit(self, kid, s, kind, data):
        with self.lock:
            save_class(kid, s)
            if kid == self.kid:
                self.emit(kind, data)

    # -- class access
    def class_copy(self, kid):
        with self.lock:
            if kid == self.kid:
                s = clone(self.s)
                s["elapsed"] = self.now()
                return s
            if kid in self.live_objs:
                return clone(self.live_objs[kid])
        return load_class(kid)

    def update_class(self, kid, fn):
        with self.lock:
            s = self.s if kid == self.kid else self.live_objs.get(kid) or load_class(kid)
            result = fn(s)
            save_class(kid, s)
            return result

    def refresh_meta(self, kid, s=None):
        """Keep the document-list card (preview, words, tags…) in step with the class."""
        if not self.lib.klass(kid):
            return
        meta = class_meta(s if s is not None else self.class_copy(kid))
        if self.lib.set_meta(kid, meta):
            self.emit("libmeta", {"id": kid, **meta, "updated": self.lib.klass(kid).get("updated")})

    def page_texts(self, kid, did):
        return slides.page_texts(kid, did)

    def replace_with_lesson(self, kid, md, note, blend, links=None):
        """Turn my notes into the MyNotes lesson, keeping the original as a version I can restore."""
        texts = md.rstrip("\n").split("\n")
        links = links if links and len(links) == len(texts) else [None] * len(texts)
        lines = [{"text": x, "t": None, "sl": sl} for x, sl in zip(texts, links)]

        def fn(s):
            label = "Earlier lesson" if s.get("formatted") else "Before formatting"
            s.setdefault("versions", []).append({"at": time.time(), "label": label, "lines": s["lines"]})
            originals = [v for v in s["versions"] if v.get("label") == "Before formatting"][:1]
            rest = [v for v in s["versions"] if v not in originals]
            s["versions"] = originals + rest[-(10 - len(originals)):]  # never lose my original notes
            s["lines"] = lines
            s["lesson_md"] = md
            s["formatted"] = {"at": time.time(), "note": note, "blend": blend}

        self.update_class(kid, fn)
        s = self.class_copy(kid)
        self.emit("formatted", {"kid": kid, "lines": lines, "formatted": s["formatted"], "versions": len(s["versions"]), "note": note})
        self.refresh_meta(kid, s)

    def restore_version(self, kid):
        def fn(s):
            if not s.get("versions"):
                raise ValueError("There's no earlier version of these notes.")
            v = s["versions"].pop()
            if s.get("formatted"):
                s["versions"].append({"at": time.time(), "label": "Formatted lesson", "lines": s["lines"]})
            originals = [x for x in s["versions"] if x.get("label") == "Before formatting"][:1]
            rest = [x for x in s["versions"] if x not in originals]
            s["versions"] = originals + rest[-(10 - len(originals)):]
            s["lines"] = v["lines"]
            if v.get("label") in ("Earlier lesson", "Formatted lesson"):  # going back to a lesson: still a lesson
                s["formatted"] = s.get("formatted") or {"at": v.get("at", time.time()), "note": "", "blend": ""}
            else:
                s["formatted"] = None

        self.update_class(kid, fn)
        s = self.class_copy(kid)
        self.emit("formatted", {"kid": kid, "lines": s["lines"], "formatted": s.get("formatted"), "versions": len(s["versions"]),
                                "restored": not s.get("formatted"), "back": True})
        self.refresh_meta(kid, s)

    def course_of(self, kid):
        k = self.lib.klass(kid)
        return self.lib.course(k["course"]) if k else None

    def course_name(self, kid):
        c = self.course_of(kid)
        return c["name"] if c else ""

    def whisper_prompt(self, kid):
        c = self.course_of(kid)
        if not c:
            return ""
        words = ", ".join(c.get("words", [])[-30:])
        heb = (" A Hebrew class taught in English. Hebrew: " + HEBREW_HINT + ".") if course_taught(c) == "he" else ""
        return f"{c['name']}." + heb + (f" Terms: {words}." if words else "") + " "

    def hebrew_rules(self, kid, what):
        """Extra instructions for the live AI in a Hebrew class: summaries, note helper items, or a short catch-up."""
        if course_taught(self.course_of(kid)) != "he":
            return ""
        out = ("\n\nHEBREW CLASS: the lecture is in English with Hebrew words, forms and verses. The transcript writes "
               "the Hebrew as English-sounding words ('bet ray sheet' = bereshit, 'ha shamayim', 'kal', 'nifal'); work "
               "out the Hebrew from the context and the slides. Write each Hebrew word or form in Hebrew script followed "
               "by its transliteration in brackets, e.g. שָׁלוֹם (shalom); add vowel points only when sure. If you can't "
               "tell which Hebrew word was meant, write the transliteration alone; never guess a spelling. Hebrew on "
               "the slides is the authority. Grammar terms stay in English: Qal, Niphal, Piel, Hiphil, perfect, "
               "imperfect, wayyiqtol, construct, dagesh, shewa.")
        if what == "helper":
            out += ("\nFor the items: a Hebrew word or form the lecturer taught is a \"term\" with text = the Hebrew "
                    "and transliteration (\"שָׁלוֹם (shalom)\"), meaning = its English meaning as taught, dict = a short "
                    "gloss. A rule of Hebrew grammar the teacher gave is a \"point\" stating the rule. Never list Hebrew "
                    "words or transliterations as a difficult \"word\".")
        elif what == "prep":
            return ("\n\nHEBREW CLASS: keep the reading's Hebrew exactly as written (vowel points included). The first "
                    "time a Hebrew word appears, follow it with its transliteration in brackets and its meaning if the "
                    "reading gives it, e.g. שָׁלוֹם (shalom), \"peace\". Grammar terms stay in English.")
        elif what == "summary":
            out += ("\nIn the summary and details, keep the Hebrew words and forms the lecturer taught, each written "
                    "as above with its meaning, and any rule or paradigm they gave.")
        return out

    def course_brief(self, kid):
        """The course name plus what the student told us about it, for the AI's prompts."""
        c = self.course_of(kid)
        if not c:
            return ""
        out = c["name"]
        if course_taught(c) == "he":
            out += "; a Hebrew class taught in English"
        if c.get("context"):
            out += "; about this course: " + c["context"]
        return out

    # -- slides
    def slide_text(self, kid, sl):
        texts = slides.page_texts(kid, sl.get("doc"))
        p = int(sl.get("page") or 0)
        return texts[p - 1] if 1 <= p <= len(texts) else ""

    def page_index(self, kid, did):
        key = (kid, did)
        if key not in self.index_cache:
            texts = slides.page_texts(kid, did)
            self.index_cache[key] = TfIdf(texts) if sum(len(t.split()) for t in texts) > 20 and len(texts) > 1 else None
        return self.index_cache[key]

    def on_seg(self, kid, s, seg):
        with self.lock:
            s["segs"].append(seg)
            save_class(kid, s)
            if kid == self.kid:
                self.emit("seg", seg)
        if self.summ:
            self.summ.poke()
        try:
            self.check_slide(kid, s, seg["t"])
        except Exception:
            traceback.print_exc()

    def check_slide(self, kid, s, t):
        """If the lecturer sounds like they're on a different slide than the one on screen, say so."""
        view = s.get("view") or {}
        did = view.get("doc")
        if not did or kid != self.kid:
            return
        idx = self.page_index(kid, did)
        if not idx:
            return
        recent = " ".join(x["text"] for x in s["segs"] if x["t"] >= t - 45)
        scores = idx.rank(recent)
        best = max(range(len(scores)), key=scores.__getitem__)
        cur = int(view.get("page", 1)) - 1
        cur_score = scores[cur] if 0 <= cur < len(scores) else 0
        if best != cur and scores[best] >= 0.15 and scores[best] - cur_score >= 0.06:
            key = (kid, did, best)
            if time.time() - self.hints.get(key, 0) > 60:
                self.hints[key] = time.time()
                self.emit("slide_hint", {"kid": kid, "doc": did, "page": best + 1, "score": round(scores[best], 2)})

    # -- what the window gets
    def public_cfg(self):
        secret = ("api_key", "gemini_key", "groq_key", "github_token")
        c = {k: v for k, v in self.cfg.items() if k not in secret}
        c["has_key"] = {"claude": bool(self.cfg["api_key"]), "gemini": bool(self.cfg["gemini_key"]),
                        "groq": bool(self.cfg["groq_key"]), "github": bool(self.cfg.get("github_token"))}
        c["ai_ready"] = ai_ready(self.cfg)
        from ai import lesson_name, lesson_ready
        c["lesson_ready"], c["lesson_name"] = lesson_ready(self.cfg), lesson_name(self.cfg)
        c["spell_ok"] = spell.available(self.cfg["language"])
        return c

    def class_meta(self):
        k = self.lib.klass(self.kid) or {}
        c = self.lib.course(k.get("course")) or {}
        return {"id": self.kid, "name": k.get("name", ""), "date": k.get("date", ""), "course": c.get("id"),
                "course_name": c.get("name", ""), "order": self.lib.order_of(self.kid)}

    def clock(self):
        return dict(recording=self.recording, elapsed=self.s["elapsed"], since=self.since, kid=self.kid)

    def snapshot(self):
        with self.lock:
            return dict(cfg=self.public_cfg(), lib=self.lib.public(), meta=self.class_meta(), cls=self.s,
                        update=self.updater.public() if getattr(self, "updater", None) else None,
                        status=self.status, model=self.model, **self.clock())


# ---------------------------------------------------------------- app controller
class App:
    def __init__(self):
        self.hub = Hub()
        self.tx = Transcriber(self.hub)
        self.summ = Summarizer(self.hub)
        self.hub.summ = self.summ
        from updater import Updater
        self.updater = Updater(self.hub)
        self.hub.updater = self.updater
        from importer import Importer
        self.importer = Importer(self)
        self.hub.lesson_hook = self.importer.notes_finished
        self.lesson = LessonBuilder(self.hub)
        from prep import Prep
        self.prep = Prep(self)
        self.rec = None

    # -- recording
    def start(self):
        hub = self.hub
        if hub.recording:
            return {"ok": True}
        if hub.model["state"] == "error":
            return {"ok": False, "error": "The speech model failed to load. Open Preferences and pick another model."}
        rec = Recorder(hub, self.tx, hub.kid, hub.s)
        try:
            rec.start()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self.rec = rec
        hub.since = time.time()
        hub.recording = True
        hub.emit("clock", hub.clock())
        return {"ok": True}

    def stop(self, wait=False):
        hub = self.hub
        if not hub.recording:
            return {"ok": True}
        with hub.lock:
            hub.s["elapsed"] = hub.now()
            hub.recording = False
            kid, s = hub.kid, hub.s
        hub.persist()
        hub.emit("clock", hub.clock())
        rec, self.rec = self.rec, None

        def finish():
            if rec:
                rec.stop()
            try:
                self.summ.tick(kid, s, True, s["elapsed"])
                self.hub.refresh_meta(kid, s)
            except Exception:
                traceback.print_exc()

        if wait:
            finish()
        else:
            threading.Thread(target=finish, daemon=True).start()
        return {"ok": True}

    # -- library
    def _lib_changed(self):
        self.hub.emit("library", {"lib": self.hub.lib.public(), "meta": self.hub.class_meta()})

    def open_class(self, kid):
        hub = self.hub
        if not hub.lib.klass(kid):
            return {"ok": False, "error": "That class no longer exists."}
        if kid != hub.kid:
            self.stop()
            with hub.lock:
                hub.persist()
                hub.kid = kid
                hub.s = hub.live_objs.get(kid) or load_class(kid)
                hub.lib.d["current"] = kid
                hub.lib.save()
        hub.emit("state", hub.snapshot())
        return {"ok": True}

    def peek_class(self, kid):
        """Another class, read-only, to keep beside the one being recorded. Doesn't touch the recording."""
        k = self.hub.lib.klass(kid)
        if not k:
            return {"ok": False, "error": "That class no longer exists."}
        s = self.hub.class_copy(kid)
        c = self.hub.lib.course(k["course"]) or {}
        return {"ok": True, "id": kid, "name": k["name"], "date": k.get("date", ""), "course": c.get("name", ""),
                "courseIcon": c.get("icon", ""), "formatted": bool(s.get("formatted")),
                "lines": [l.get("text", "") for l in s.get("lines", [])],
                "segs": [{"t": x["t"], "text": x["text"]} for x in s.get("segs", [])],
                "prep": [{"id": p["id"], "title": p.get("title", ""), "kind": p.get("kind"), "text": p.get("text", "")}
                         for p in s.get("prep", []) if p.get("status") == "ready"]}

    def new_class(self, cid):
        k = self.hub.lib.new_class(cid or (self.hub.course_of(self.hub.kid) or {}).get("id"))
        return self.open_class(k["id"])

    def lib_op(self, path, data):
        lib = self.hub.lib
        if path == "/course/new":
            c = lib.new_course(str(data.get("name") or "New course"))
            self._lib_changed()
            return {"ok": True, "id": c["id"]}
        if path == "/course/rename":
            lib.rename_course(data.get("id"), str(data.get("name", "")))
        elif path == "/course/delete":
            lib.delete_course(data.get("id"))
        elif path == "/course/info":
            lib.set_info(data.get("id"), data.get("taught"), data.get("context"))
        elif path == "/course/style":
            lib.set_style(data.get("id"), data.get("icon"), data.get("color"))
        elif path == "/course/toggle":
            lib.set_collapsed(data.get("id"), data.get("collapsed"))
        elif path == "/class/rename":
            lib.rename_class(data.get("id"), str(data.get("name", "")))
        elif path == "/class/move":
            lib.move_class(data.get("id"), data.get("course"))
        elif path == "/class/delete":
            kid = data.get("id")
            if kid == self.hub.kid:
                self.stop(wait=True)
                k = lib.klass(kid)
                others = [x for x in lib.classes_in(k["course"]) if x["id"] != kid] if k else []
                others = others or [x for x in lib.d["classes"] if x["id"] != kid]
                nxt = others[0] if others else lib.new_class(k["course"] if k else None)
                self.open_class(nxt["id"])
            lib.delete_class(kid)
        self._lib_changed()
        return {"ok": True}

    # -- notes, points, view
    def save_lines(self, data):
        kid = data.get("kid") or self.hub.kid
        lines = []
        for l in data.get("lines") or []:
            if not isinstance(l, dict):
                continue
            t = l.get("t")
            sl = l.get("sl")
            if isinstance(sl, dict) and sl.get("doc") and isinstance(sl.get("page"), int):
                sl = {"doc": str(sl["doc"]), "page": sl["page"]}
            else:
                sl = None
            lines.append({"text": str(l.get("text", "")), "t": float(t) if isinstance(t, (int, float)) else None, "sl": sl})
        if not self.hub.lib.klass(kid):
            return {"ok": False, "error": "That class no longer exists."}
        self.hub.update_class(kid, lambda s: s.__setitem__("lines", lines))
        self.hub.refresh_meta(kid)
        return {"ok": True}

    def delete_point(self, data):
        kid, pid = data.get("kid") or self.hub.kid, data.get("id")
        if data.get("known"):  # a difficult word the student already knows: don't flag it again in any class
            w = str(data["known"]).strip().lower()[:60]
            kw = self.hub.cfg.setdefault("known_words", [])
            if w and w not in kw:
                kw.append(w)
                del kw[:-2000]
                save_json(CFG_PATH, self.hub.cfg)
        self.hub.update_class(kid, lambda s: s.__setitem__("points", [p for p in s["points"] if p["id"] != pid]))
        if kid == self.hub.kid:
            self.hub.emit("points", {"add": [], "update": [], "remove": [pid], "topic": self.hub.s["topic"]})
        return {"ok": True}

    def set_view(self, data):
        hub = self.hub
        kid = data.get("kid")
        if kid != hub.kid:
            return {"ok": False}
        did, page = data.get("doc"), data.get("page")
        with hub.lock:
            s = hub.s
            if not did:
                s["view"] = None
            else:
                page = max(1, int(page or 1))
                s["view"] = {"doc": did, "page": page}
                t = round(hub.now(), 1)
                log = s.setdefault("slide_log", [])
                if log and t - log[-1][0] < 3:
                    log[-1] = [log[-1][0], did, page]
                elif not log or log[-1][1:] != [did, page]:
                    log.append([t, did, page])
            hub.persist()
        return {"ok": True}

    # -- uploads
    def upload(self, kid, name, body):
        hub = self.hub
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in slides.ACCEPT:
            return {"ok": False, "error": f'"{name}" isn\'t a file type DenSemNotes can show. Use PDF, PowerPoint, '
                                          "Keynote, Word, Pages, text or a photo."}
        if not hub.lib.klass(kid):
            return {"ok": False, "error": "That class no longer exists."}
        did = new_id()
        folder = slides.doc_dir(kid, did)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "source." + ext), "wb") as f:
            f.write(body)
        entry = {"id": did, "name": name[:120], "ext": ext, "status": "converting", "pages": 0, "method": "",
                 "error": "", "note": "Preparing…"}
        hub.update_class(kid, lambda s: s["docs"].append(entry))
        self._docs_changed(kid)
        threading.Thread(target=self._convert, args=(kid, did, ext, name), daemon=True).start()
        return {"ok": True, "id": did}

    def _docs_changed(self, kid):
        s = self.hub.class_copy(kid)
        self.hub.emit("docs", {"kid": kid, "docs": s["docs"], "view": s.get("view")})

    def _set_doc(self, kid, did, **kw):
        if kw.get("status") == "ready":
            t = threading.Timer(0.5, lambda: self.hub.refresh_meta(kid))
            t.daemon = True
            t.start()
        def fn(s):
            for d in s["docs"]:
                if d["id"] == did:
                    d.update(kw)
            if kw.get("status") == "ready" and not s.get("view"):
                s["view"] = {"doc": did, "page": 1}
        self.hub.update_class(kid, fn)
        self._docs_changed(kid)

    def _convert(self, kid, did, ext, name=""):
        last = [0.0]

        def report(text):
            if time.time() - last[0] > 0.3:
                last[0] = time.time()
                self._set_doc(kid, did, note=text)

        try:
            pages, method = slides.convert(kid, did, ext, report, os.path.splitext(name)[0])
            self.hub.index_cache.pop((kid, did), None)
            self._set_doc(kid, did, status="ready", pages=pages, method=method, note="")
            try:  # warm the first page so it shows instantly
                slides.render(kid, did, 1, 1600)
            except Exception:
                pass
        except Exception as e:
            traceback.print_exc()
            self._set_doc(kid, did, status="error", error=str(e)[:300], note="")

    def claude_code_test(self, data):
        from ai import claude_code_ask, claude_code_info

        info = claude_code_info()
        if not info["installed"]:
            return {"ok": False, "error": "Claude Code isn't installed yet.", "info": info}
        try:
            out = claude_code_ask("Reply with exactly: Ready to write your class notes.",
                                  str(data.get("model") or self.hub.cfg.get("lesson_model") or "sonnet"), timeout=180)
            return {"ok": True, "reply": out[:200], "info": info}
        except Exception as e:
            return {"ok": False, "error": str(e), "info": info}

    def link_lesson(self, data):
        """Tie an existing lesson's sections to the slides (for lessons made before slide links, or after
        slides are added)."""
        from lesson import slide_links

        kid = data.get("kid") or self.hub.kid
        s = self.hub.class_copy(kid)
        if not s.get("lines"):
            return {"ok": False, "error": "There's no note to link."}
        links = slide_links([l["text"] for l in s["lines"]], s, lambda did: self.hub.page_texts(kid, did))
        n = sum(1 for x in links if x)

        def fn(c):
            if len(c["lines"]) != len(links):
                return
            for l, sl in zip(c["lines"], links):
                if sl and not l.get("sl"):
                    l["sl"] = sl
            if isinstance(c.get("formatted"), dict):
                c["formatted"]["linked"] = 1

        self.hub.update_class(kid, fn)
        if kid == self.hub.kid:
            self.hub.emit("lines", {"kid": kid, "lines": self.hub.s["lines"]})
        return {"ok": True, "linked": n}

    def reconvert_doc(self, data):
        kid, did = data.get("kid"), data.get("doc")
        s = self.hub.class_copy(kid) if self.hub.lib.klass(kid) else None
        d = next((x for x in (s or {}).get("docs", []) if x["id"] == did), None)
        if not d:
            return {"ok": False, "error": "That document is no longer in this class."}
        if d["status"] == "converting":
            return {"ok": False, "error": "It's still converting."}
        slides.reset(kid, did)
        self.hub.index_cache.pop((kid, did), None)
        self._set_doc(kid, did, status="converting", pages=0, method="", error="", note="Preparing…")
        threading.Thread(target=self._convert, args=(kid, did, d["ext"], d["name"]), daemon=True).start()
        return {"ok": True}

    def delete_doc(self, data):
        kid, did = data.get("kid"), data.get("doc")

        def fn(s):
            s["docs"] = [d for d in s["docs"] if d["id"] != did]
            if (s.get("view") or {}).get("doc") == did:
                ready = [d for d in s["docs"] if d["status"] == "ready"]
                s["view"] = {"doc": ready[0]["id"], "page": 1} if ready else None

        self.hub.update_class(kid, fn)
        shutil.rmtree(slides.doc_dir(kid, did), ignore_errors=True)
        self._docs_changed(kid)
        return {"ok": True}

    # -- spelling
    def spell_check(self, data):
        kid = data.get("kid") or self.hub.kid
        c = self.hub.course_of(kid) or {}
        lines = [str(x) for x in data.get("lines") or []]
        issues = spell.check(lines, self.hub.cfg["language"], c.get("words", []) + (HEBREW_TERMS if course_taught(c) == "he" else []))
        if issues is None:
            return {"ok": True, "available": False, "issues": []}
        return {"ok": True, "available": True, "issues": issues}

    def spell_word(self, data):
        kid = data.get("kid") or self.hub.kid
        c = self.hub.course_of(kid) or {}
        if course_taught(c) == "he" and str(data.get("word", "")) not in spell.COMMON:
            return {"ok": True, "fix": None}  # Hebrew transliterations look like typos; only fix the usual English slips
        return {"ok": True, "fix": spell.autocorrect(str(data.get("word", ""))[:40], self.hub.cfg["language"], c.get("words", []))}

    def format_note(self, data):
        kid = data.get("kid") or self.hub.kid
        k = self.hub.lib.klass(kid)
        if not k:
            return {"ok": False, "error": "That class no longer exists."}
        blend = data.get("blend") or self.hub.cfg["ui"].get("blend") or "notes"
        s = self.hub.class_copy(kid)
        heading = next((re.sub(r"(?<!\w)#[A-Za-z][\w/-]*", "", l["text"].lstrip("#").strip()).strip()
                        for l in s.get("lines", []) if re.match(r"^#{1,3} ", l.get("text", ""))), "")
        fallback = heading or (s.get("overall") or {}).get("title") or k["name"]
        opts = {"title": str(data.get("title") or ""), "course": self.hub.course_name(kid), "order": self.hub.lib.order_of(kid),
                "tags": "", "bible": str(data.get("bible") or self.hub.cfg["ui"].get("bible") or ""), "class_name": fallback,
                "blend": "notes" if blend == "only_notes" else blend,
                "sources": ["wrote"] if blend == "only_notes" else ["wrote", "heard"]}
        want = data.get("slideLayout")
        if want is None:
            want = self.hub.cfg["ui"].get("slideLayout")
        if want and any(d.get("status") == "ready" for d in s.get("docs", [])):
            opts["layout"] = "slides"  # the slides are the outline; the lecture and my notes fill each slide in
        if data.get("source") == "material":
            # a new lesson from what the class already has: transcript, summaries, slides and documents, plus the
            # notes I wrote myself (as they were before any earlier formatting); never from an earlier lesson
            ready = [d for d in s.get("docs", []) if d.get("status") == "ready"]
            if not s.get("segs") and not ready and not original_notes(s):
                return {"ok": False, "error": "This class has no transcript, slides or notes to make a lesson from."}
            opts.update(source="material", blend="balanced",
                        class_name=(s.get("overall") or {}).get("title") or k["name"])
        return self.lesson.start(kid, opts, replace=True)

    def template_upload(self, body):
        from store import DATA
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "That file isn't plain text. Choose a .md file."}
        if len(text) > 400_000 or not text.strip():
            return {"ok": False, "error": "That file is empty or too large."}
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "template.md"), "w", encoding="utf-8") as f:
            f.write(text)
        info = template_info()
        if not info["rules"]:
            info["warning"] = ("This template has no “AI Conversion Prompt” section, so lessons use the built-in rules. "
                               "Add a heading “## AI Conversion Prompt” followed by a ```text block with the instructions.")
        return {"ok": True, **info}

    def template_reset(self, data):
        from store import DATA
        p = os.path.join(DATA, "template.md")
        if os.path.exists(p):
            os.remove(p)
        return {"ok": True, **template_info()}

    def restore(self, data):
        self.hub.restore_version(data.get("kid") or self.hub.kid)
        return {"ok": True}

    def spell_learn(self, data):
        kid = data.get("kid") or self.hub.kid
        word = str(data.get("word", "")).strip()[:60]
        c = self.hub.course_of(kid)
        if c and word:
            self.hub.lib.learn_word(c["id"], word)
            spell.learn_native(word)
            self._lib_changed()
        return {"ok": True}

    # -- settings & misc
    def settings(self, data):
        hub = self.hub
        old_model = self.tx.wanted()
        for k in ("claude_model", "whisper", "language", "provider", "ollama_model", "gemini_model", "groq_model"):
            if k in data and str(data[k]).strip():
                hub.cfg[k] = str(data[k]).strip()
        if str(data.get("period", "")).isdigit():
            hub.cfg["period"] = max(0, min(60, int(data["period"])))
        if str(data.get("aid_every", "")).isdigit():
            hub.cfg["aid_every"] = max(0, min(10, int(data["aid_every"])))
        if data.get("hard_words") in ("off", "fewer", "normal", "more"):
            hub.cfg["hard_words"] = data["hard_words"]
        if isinstance(data.get("helpers"), dict):
            from live import KINDS
            hub.cfg["helpers"] = {k: False for k, v in data["helpers"].items() if k in KINDS and v is False}
        if data.get("forget_known"):
            hub.cfg["known_words"] = []
        for which, field in (("claude", "api_key"), ("gemini", "gemini_key"), ("groq", "groq_key")):
            key = (data.get("keys") or {}).get(which, "").strip()
            if key:
                hub.cfg[field] = key
            if which in (data.get("remove_keys") or []):
                hub.cfg[field] = ""
        if data.get("lesson_ai") in ("same", "claude_code", "claude_api"):
            hub.cfg["lesson_ai"] = data["lesson_ai"]
        if data.get("lesson_model") in ("sonnet", "opus", "haiku"):
            hub.cfg["lesson_model"] = data["lesson_model"]
        if "github_token" in data and str(data["github_token"]).strip():
            hub.cfg["github_token"] = str(data["github_token"]).strip()
        if data.get("remove_github_token"):
            hub.cfg["github_token"] = ""
        if "auto_update_check" in data:
            hub.cfg["auto_update_check"] = bool(data["auto_update_check"])
        if "device" in data:
            d = data["device"]
            hub.cfg["device"] = int(d) if str(d).isdigit() else None
        self.summ.err = ""
        save_json(CFG_PATH, hub.cfg)
        if self.tx.wanted() != old_model:
            threading.Thread(target=self.tx.load, daemon=True).start()
        hub.emit("cfg", hub.public_cfg())
        return {"ok": True}

    def prefs(self, data):
        ui = self.hub.cfg["ui"]
        for k, v in (data.get("ui") or {}).items():
            if k in UI_DEFAULTS:
                ui[k] = v
        save_json(CFG_PATH, self.hub.cfg)
        return {"ok": True}

    def pull(self, model):
        if not re.fullmatch(r"[\w.\-:/]+", model or ""):
            return {"ok": False, "error": "Enter a model name, like llama3.2"}
        if not ollama_info()["running"]:
            return {"ok": False, "error": "Ollama isn't running. Install it from ollama.com and open it first."}
        threading.Thread(target=ollama_pull, args=(self.hub, model), daemon=True).start()
        return {"ok": True}

    LINKS = {"https://ollama.com/download", "https://aistudio.google.com/apikey", "https://console.groq.com/keys",
             "https://console.anthropic.com/", "https://www.apple.com/keynote/", "https://www.libreoffice.org/download/"}

    def open_link(self, url):
        if url.startswith("https://www.biblegateway.com/passage/?search=") and len(url) < 300:
            pass
        elif url not in self.LINKS:
            return {"ok": False, "error": "Link not allowed"}
        if sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            webbrowser.open(url)
        return {"ok": True}

    def test_ai(self):
        cfg = self.hub.cfg
        if not ai_ready(cfg):
            return {"ok": False, "error": "Pick an AI and fill in its details first."}
        try:
            reply = ask_ai(cfg, "Reply with exactly the word: ready", 20)
            return {"ok": True, "text": f"{NAMES.get(cfg['provider'])} is working (replied: {reply[:40]!r})."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def devices(self):
        try:
            import sounddevice as sd

            default_in = sd.default.device[0]
            return [{"id": i, "name": d["name"], "default": i == default_in}
                    for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
        except Exception:
            return []

    def summarize_now(self, data):
        kid = data.get("kid") or self.hub.kid
        if kid != self.hub.kid:
            return {"ok": False, "error": "Open the class first."}
        s = self.hub.s
        if not s["segs"]:
            return {"ok": False, "error": "Nothing has been transcribed yet."}

        def run():
            try:
                self.summ.tick(kid, s, True)
            except Exception:
                traceback.print_exc()
            self.hub.emit("summarized", {"kid": kid})

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def ask(self, data):
        kid = data.get("kid") or self.hub.kid
        r = ask.answer(self.hub, kid, str(data.get("q", "")), str(data.get("scope", "class")))
        if r.get("ok"):
            def keep(s):
                s.setdefault("asks", []).append(r["ask"])
                s["asks"] = s["asks"][-40:]
            self.hub.update_class(kid, keep)
        return r

    def forget_ask(self, data):
        kid, at = data.get("kid") or self.hub.kid, data.get("at")
        self.hub.update_class(kid, lambda s: s.__setitem__("asks", [x for x in s.get("asks", []) if x.get("at") != at]))
        return {"ok": True}

    def last_question(self, data):
        hub = self.hub
        return self.summ.last_question(hub.kid, hub.s, hub.now())

    def catch_up(self, data):
        hub = self.hub
        return self.summ.catch_up(hub.kid, hub.s, hub.now(), int(data.get("mins", 2)))

    def make_lesson(self, data):
        return self.lesson.start(data.get("kid") or self.hub.kid, data)

    def save_lesson(self, data):
        kid = data.get("kid") or self.hub.kid
        k = self.hub.lib.klass(kid) or {}
        return save_lesson(str(data.get("md", "")), self.hub.course_name(kid), k.get("name"))

    def copy_transcript(self, data):
        """The whole transcript of a class onto the Mac clipboard (plain, or with the time of each line)."""
        kid = data.get("kid") or self.hub.kid
        segs = self.hub.class_copy(kid).get("segs") or []
        if not segs:
            return {"ok": False, "error": "This class has no transcript yet."}
        if data.get("times"):
            text = "\n".join(f"[{fmt(x['t'])}] {x['text'].strip()}" for x in segs)
        else:
            text = "\n\n".join(_paragraphs([x["text"].strip() for x in segs]))
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=10)
        except Exception as e:
            return {"ok": False, "error": f"Couldn't copy: {e}", "text": text}
        return {"ok": True, "words": len(text.split())}

    def export(self, data):
        kid = data.get("kid") or self.hub.kid
        k = self.hub.lib.klass(kid) or {}
        return export_class(self.hub.class_copy(kid), self.hub.course_name(kid), k.get("name", "Class"))

    def shutdown(self):
        """Quit quickly: save everything, stop the microphone, close every program DenSemNotes started.
        The final summary of a lecture that was still recording is left for the next start, so quitting never
        waits on transcription or the AI."""
        if getattr(self, "_shut", False):
            return
        self._shut = True
        hub = self.hub
        try:
            if hub.recording:
                with hub.lock:
                    hub.s["elapsed"] = hub.now()
                    hub.recording = False
                    kid = hub.kid
                rec, self.rec = self.rec, None
                if rec:
                    rec.halt(2.0)
                pending = set(hub.cfg.get("pending_final") or [])
                pending.add(kid)
                hub.cfg["pending_final"] = sorted(pending)
                save_json(CFG_PATH, hub.cfg)
            hub.persist()
        except Exception:
            traceback.print_exc()
        try:
            import procs
            procs.close_all()
        except Exception:
            traceback.print_exc()
        try:  # a local AI keeps the model in memory for 30 minutes; let it go now
            if hub.cfg.get("provider") == "ollama" and hub.cfg.get("ollama_model"):
                import urllib.request
                req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=json.dumps(
                    {"model": hub.cfg["ollama_model"], "keep_alive": 0}).encode(), headers={"content-type": "application/json"})
                urllib.request.urlopen(req, timeout=1.5).read()
        except Exception:
            pass

    def finish_pending(self):
        """Lectures that were still recording when the app was last quit: make their final summary now."""
        time.sleep(5)
        hub = self.hub
        for kid in list(hub.cfg.get("pending_final") or []):
            try:
                if hub.lib.klass(kid):
                    with hub.lock:
                        s = hub.s if kid == hub.kid else load_class(kid)
                    self.summ.tick(kid, s, True, s["elapsed"])
                    if kid != hub.kid:
                        save_class(kid, s)
                    hub.refresh_meta(kid, s)
            except Exception:
                traceback.print_exc()
            hub.cfg["pending_final"] = [k for k in hub.cfg.get("pending_final") or [] if k != kid]
            save_json(CFG_PATH, hub.cfg)


# ---------------------------------------------------------------- local web server for the window
def make_handler(app):
    ui_path = os.path.join(HERE, "ui.html")
    page_re = re.compile(r"^/page/(\w+)/(\w+)/(\d+)$")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype, code=200, cache=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if cache:
                self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False).encode(), "application/json", code)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            path = url.path
            if path == "/":
                with open(ui_path, encoding="utf-8") as f:
                    body = f.read()
                u = app.hub.cfg["ui"]
                attrs = f' data-accent="{u.get("accent") or "green"}"'
                if u.get("theme") in ("light", "dark"):
                    attrs += f' data-theme="{u["theme"]}"'
                if (q.get("shell") or [""])[0] == "mac":
                    attrs += ' class="shell-mac' + (' vibrant' if u.get("translucent", True) else '') + '"'
                body = body.replace('<html lang="en">', f'<html lang="en"{attrs}>', 1)
                self._send(body.encode(), "text/html; charset=utf-8")
            elif path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                qq = queue.Queue()
                with app.hub.lock:
                    app.hub.subs.append(qq)
                    first = f"event: state\ndata: {json.dumps(app.hub.snapshot(), ensure_ascii=False)}\n\n".encode()
                try:
                    self.wfile.write(first)
                    self.wfile.flush()
                    while True:
                        try:
                            msg = qq.get(timeout=15)
                        except queue.Empty:
                            msg = b": ping\n\n"
                        self.wfile.write(msg)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with app.hub.lock:
                        if qq in app.hub.subs:
                            app.hub.subs.remove(qq)
            elif path == "/logo.svg":
                for p in (os.path.join(HERE, "logo.svg"), os.path.join(HERE, "assets", "logo.svg")):
                    if os.path.exists(p):
                        with open(p, "rb") as f:
                            return self._send(f.read(), "image/svg+xml", cache="max-age=86400")
                self._json({"error": "not found"}, 404)
            elif page_re.match(path):
                kid, did, n = page_re.match(path).groups()
                try:
                    w = int((q.get("w") or ["1600"])[0])
                    out = slides.render(kid, did, int(n), w)
                    with open(out, "rb") as f:
                        self._send(f.read(), "image/png", cache="max-age=31536000, immutable")
                except Exception as e:
                    self._json({"error": str(e)}, 404)
            elif path == "/vendor/mermaid.min.js":
                p = os.path.join(HERE, "vendor", "mermaid.min.js")
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        return self._send(f.read(), "text/javascript; charset=utf-8", cache="max-age=31536000")
                self._json({"error": "not found"}, 404)
            elif path == "/template":
                self._json(template_info())
            elif path == "/template.md":
                with open(template_path(), "rb") as f:
                    self._send(f.read(), "text/markdown; charset=utf-8")
            elif path == "/devices":
                self._json(app.devices())
            elif path == "/ollama":
                self._json(ollama_info())
            elif path == "/claude_code":
                from ai import claude_code_info
                self._json(claude_code_info())
            else:
                self._json({"error": "not found"}, 404)

        def read_body(self, limit=None):
            """The request body, whether it came with a length or in chunks (WebKit can send files either way)."""
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                parts, total = [], 0
                while True:
                    size = int((self.rfile.readline().split(b";")[0].strip() or b"0"), 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        break
                    parts.append(self.rfile.read(size))
                    self.rfile.readline()
                    total += size
                    if limit and total > limit:
                        raise ValueError("too large")
                return b"".join(parts)
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if url.path == "/template/upload":
                return self._json(app.template_upload(self.read_body(500_000)[:500_000]))
            if url.path == "/import/file":
                q = urllib.parse.parse_qs(url.query)
                if n > MAX_UPLOAD * 4:
                    self.rfile.read(n)
                    return self._json({"ok": False, "error": "That file is over 1.2 GB."})
                try:
                    body = self.read_body(MAX_UPLOAD * 4)
                except ValueError:
                    return self._json({"ok": False, "error": "That file is over 1.2 GB."})
                return self._json(app.importer.lecture_file((q.get("kid") or [""])[0], (q.get("name") or ["lecture.txt"])[0], body))
            if url.path == "/prep/upload":
                q = urllib.parse.parse_qs(url.query)
                if n > MAX_UPLOAD:
                    self.rfile.read(n)
                    return self._json({"ok": False, "error": "That file is over 300 MB."})
                try:
                    body = self.read_body(MAX_UPLOAD)
                    return self._json(app.prep.upload((q.get("kid") or [""])[0], (q.get("name") or ["file"])[0], body))
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e) if "class" in str(e) else "That file is over 300 MB."})
                except Exception as e:
                    traceback.print_exc()
                    return self._json({"ok": False, "error": str(e)}, 500)
            if url.path == "/upload":
                if n > MAX_UPLOAD:
                    self.rfile.read(n)
                    return self._json({"ok": False, "error": "That file is over 300 MB."})
                q = urllib.parse.parse_qs(url.query)
                try:
                    body = self.read_body(MAX_UPLOAD)
                except ValueError:
                    return self._json({"ok": False, "error": "That file is over 300 MB."})
                try:
                    return self._json(app.upload((q.get("kid") or [""])[0], (q.get("name") or ["file"])[0], body))
                except Exception as e:
                    traceback.print_exc()
                    return self._json({"ok": False, "error": str(e)}, 500)
            try:
                data = json.loads(self.read_body() or b"{}")
            except ValueError:
                data = {}
            routes = {
                "/start": app.start,
                "/stop": app.stop,
                "/catchup": lambda: app.catch_up(data),
                "/lastq": lambda: app.last_question(data),
                "/settings": lambda: app.settings(data),
                "/prefs": lambda: app.prefs(data),
                "/ollama/pull": lambda: app.pull(str(data.get("model", "")).strip()),
                "/open": lambda: app.open_link(str(data.get("url", ""))),
                "/test-ai": app.test_ai,
                "/notes": lambda: app.save_lines(data),
                "/point/delete": lambda: app.delete_point(data),
                "/view": lambda: app.set_view(data),
                "/doc/delete": lambda: app.delete_doc(data),
                "/import/new": lambda: app.importer.create(data),
                "/update/check": lambda: app.updater.check(),
                "/claude_code/test": lambda: app.claude_code_test(data),
                "/update/install": lambda: app.updater.update(),
                "/update/restore": lambda: app.updater.restore(),
                "/update/restart": lambda: app.updater.restart(),
                "/import/text": lambda: app.importer.lecture_text(data.get("kid"), data.get("text", "")),
                "/import/run": lambda: app.importer.run(data),
                "/doc/reconvert": lambda: app.reconvert_doc(data),
                "/lesson/link": lambda: app.link_lesson(data),
                "/spell": lambda: app.spell_check(data),
                "/spell/learn": lambda: app.spell_learn(data),
                "/spell/word": lambda: app.spell_word(data),
                "/format": lambda: app.format_note(data),
                "/restore": lambda: app.restore(data),
                "/template/reset": lambda: app.template_reset(data),
                "/lesson": lambda: app.make_lesson(data),
                "/lesson/save": lambda: app.save_lesson(data),
                "/export": lambda: app.export(data),
                "/class/peek": lambda: app.peek_class(data.get("id")),
                "/prep/new": lambda: app.prep.new(data),
                "/prep/save": lambda: app.prep.save(data),
                "/prep/delete": lambda: app.prep.delete(data),
                "/prep/move": lambda: app.prep.move(data),
                "/prep/reconvert": lambda: app.prep.reconvert(data),
                "/copy-transcript": lambda: app.copy_transcript(data),
                "/class/open": lambda: app.open_class(data.get("id")),
                "/class/new": lambda: app.new_class(data.get("course")),
                "/summarize": lambda: app.summarize_now(data),
                "/ask": lambda: app.ask(data),
                "/ask/forget": lambda: app.forget_ask(data),
            }
            try:
                if url.path in routes:
                    return self._json(routes[url.path]())
                if url.path in ("/course/new", "/course/rename", "/course/delete", "/course/toggle", "/course/style", "/course/info", "/class/rename",
                                "/class/move", "/class/delete"):
                    return self._json(app.lib_op(url.path, data))
                self._json({"error": "not found"}, 404)
            except ValueError as e:
                self._json({"ok": False, "error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self._json({"ok": False, "error": str(e)}, 500)

    return Handler


class WindowApi:
    """The traffic-light buttons in DenSemNotes' own title bar call these."""

    def __init__(self):
        self.window = None

    def close(self):
        self.window.destroy()

    def minimize(self):
        self.window.minimize()

    def zoom(self):
        self.window.toggle_fullscreen()


def mac_branding():
    """Show the DenSemNotes name and icon in the menu bar and Dock instead of Python's."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle

        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = APP_NAME
        for p in (os.path.join(HERE, "AppIcon.icns"), os.path.join(HERE, "assets", "AppIcon.icns")):
            if os.path.exists(p):
                NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(p))
                break
    except Exception:
        pass


def main():
    if "--download-model" in sys.argv:
        hub = Hub()
        tx = Transcriber(hub)
        print(f"Preparing speech model '{tx.wanted()}'…", flush=True)
        ok = tx.load()
        print("Speech model ready." if ok else f"Could not load the model: {hub.model.get('error')}")
        sys.exit(0 if ok else 1)

    app = App()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=app.tx.load, daemon=True).start()
    threading.Thread(target=app.summ.loop, daemon=True).start()
    threading.Thread(target=app.updater.loop, daemon=True).start()
    threading.Thread(target=app.finish_pending, daemon=True).start()
    hub_quit = lambda *a: quit_now(app)  # noqa: E731
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            signal.signal(sig, hub_quit)
        except Exception:
            pass
    app.hub.quit_now = lambda: quit_now(app)

    try:
        if "--browser" in sys.argv:
            raise ImportError
        import webview

        mac_branding()
        api = WindowApi()
        kw = dict(width=1440, height=900, min_size=(900, 580), js_api=api)
        if sys.platform == "darwin":
            kw.update(frameless=True, easy_drag=False)
            try:
                api.window = webview.create_window(APP_NAME, url + "?shell=mac", vibrancy=True, transparent=True, **kw)
            except TypeError:  # older pywebview without vibrancy
                api.window = webview.create_window(APP_NAME, url + "?shell=mac", **kw)
        else:
            api.window = webview.create_window(APP_NAME, url, **kw)
        menu = None
        try:  # a Help menu in the Mac menu bar: Check for Updates…, the GitHub page
            import webview.menu as wm

            def js(code):
                return lambda: api.window and api.window.evaluate_js(code)

            menu = [wm.Menu("Help", [
                wm.MenuAction("Check for Updates…", js("window.DenSem && window.DenSem.checkUpdates()")),
                wm.MenuAction("Preferences…", js("window.DenSem && window.DenSem.prefs()")),
                wm.MenuSeparator(),
                wm.MenuAction(f"{APP_NAME} on GitHub", lambda: webbrowser.open(
                    "https://github.com/" + (__import__("updater").source()[0] or "haletta/DenSemNotes"))),
            ])]
        except Exception:
            menu = None
        try:
            webview.start(menu=menu) if menu else webview.start()
        except TypeError:  # older pywebview without menus
            webview.start()
    except ImportError:
        print(f"{APP_NAME} is running at {url}  (press Ctrl+C to quit)", flush=True)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        quit_now(app)


def quit_now(app, code=0):
    """Save, close what we started, and end the process for sure (a stuck thread or the audio driver can't
    keep the app alive and 'not responding' after its window is gone)."""
    guard = threading.Timer(6.0, lambda: os._exit(code))
    guard.daemon = True
    guard.start()
    try:
        app.shutdown()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)


if __name__ == "__main__":
    main()
