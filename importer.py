"""Import a class that DenSem Class didn't record: a transcript (file or pasted) or a recording, plus slides and
any notes taken. The app then builds what a recorded class has: timed transcript, slide links, part summaries,
the note helper (points, quotes, books, terms, examples, questions, due dates) and class notes in the
student's MyNotes template.
"""
import html
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import zipfile

from ai import ai_ready
from store import class_dir, load_class
from words import TfIdf, content_words, fmt, sentences

MAC = sys.platform == "darwin"
TEXT_EXT = {"txt", "md", "text", "docx", "pdf", "rtf", "doc", "odt", "html", "htm", "srt", "vtt", "sbv", "json"}
MEDIA_EXT = {"mp3", "m4a", "wav", "aac", "ogg", "oga", "flac", "wma", "aiff", "aif", "caf", "opus", "webm",
             "mp4", "mov", "m4v", "mkv", "avi"}
WPM = 140  # speaking pace used to give a plain transcript its timing
SEG_WORDS = 28
IMPORT_KEYS = ("segs", "points", "blocks", "overall", "topic", "pid", "bid", "q_idx", "aid_t", "elapsed", "imported")

STEPS = [("read", "Read the lecture"), ("slides", "Prepare the slides"), ("match", "Match the lecture to the slides"),
         ("helper", "Pick out points, quotes, books and terms"), ("summary", "Summarize each part"),
         ("notes", "Write the class notes")]


# ---------------------------------------------------------------- reading a transcript
TS = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?"
CUE = re.compile(r"^\s*" + TS + r"\s*-->\s*" + TS)
LEAD_TS = re.compile(r"^\s*[\[(]?" + TS + r"[\])]?\s*[-–:]?\s*")
SPEAKER_TS = re.compile(r"^\s*([^\n:()\[\]]{1,40}?)\s*[\[(]" + TS + r"[\])]\s*:?\s*$")
SPEAKER_LEAD = re.compile(r"^\s*(?:speaker\s*\d+|[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s*:\s+")


def _secs(h, m, s, ms=None):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + (int((ms or "0").ljust(3, "0")[:3]) / 1000)


def decode(raw):
    """Text from bytes in whatever encoding a text editor used: UTF-8, UTF-16 (Windows 'Unicode', Word),
    Windows-1252 or old Mac Roman."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    sample = raw[:4000]
    if sample and sample.count(b"\x00") > len(sample) // 4:  # UTF-16 without a byte-order mark
        even, odd = sample[0::2].count(0), sample[1::2].count(0)
        return raw.decode("utf-16-be" if even > odd else "utf-16-le", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    text = raw.decode("cp1252", "replace")
    if sum(text.count(c) for c in "ÕÒÓÐÑ") > sum(text.count(c) for c in "’“”–—"):
        text = raw.decode("mac_roman", "replace")
    return text


def rtf_text(raw):
    if MAC:
        try:
            r = subprocess.run(["textutil", "-convert", "txt", "-stdout", "-stdin"], input=raw.encode("utf-8", "replace")
                               if isinstance(raw, str) else raw, capture_output=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode("utf-8", "replace")
        except Exception:
            pass
    if isinstance(raw, bytes):
        raw = decode(raw)
    raw = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), raw)
    raw = re.sub(r"\\(par|line)\b ?", "\n", raw)
    raw = re.sub(r"\{\\(fonttbl|colortbl|\*|stylesheet|info)[^{}]*(\{[^{}]*\}[^{}]*)*\}", "", raw)
    raw = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", "", raw)
    return raw


def read_text(path, ext):
    """Plain text of a transcript file."""
    if ext in ("txt", "md", "text", "srt", "vtt", "sbv", "json"):
        with open(path, "rb") as f:
            raw = f.read()
        if raw.lstrip().startswith(b"{\\rtf"):  # TextEdit RTF saved with a .txt name
            return rtf_text(raw)
        return decode(raw)
    if ext == "docx":
        import docx

        return "\n".join(p.text for p in docx.Document(path).paragraphs)
    if ext == "pdf":
        import pypdfium2 as pdfium
        from slides import PDF_LOCK

        with PDF_LOCK:
            doc = pdfium.PdfDocument(path)
            out = []
            for i in range(len(doc)):
                pg = doc[i]
                tp = pg.get_textpage()
                out.append(tp.get_text_range().replace("\r", ""))
                tp.close()
                pg.close()
            doc.close()
        return "\n".join(out)
    if ext in ("html", "htm"):
        with open(path, encoding="utf-8", errors="replace") as f:
            return html.unescape(re.sub(r"<[^>]+>", "\n", f.read()))
    if ext == "odt":
        with zipfile.ZipFile(path) as z:
            return html.unescape(re.sub(r"<[^>]+>", "\n", z.read("content.xml").decode("utf-8", "replace")))
    if ext in ("rtf", "doc") and MAC:
        return subprocess.run(["textutil", "-convert", "txt", "-stdout", path], capture_output=True, text=True,
                              timeout=120).stdout
    if ext == "rtf":
        with open(path, "rb") as f:
            return rtf_text(f.read())
    raise ValueError("DenSem Class can't read that file type. Save the transcript as text, Word or PDF.")


def parse_transcript(text):
    """[{t, text}] from a transcript. Uses its timestamps (subtitle files, Zoom/Teams/Otter exports, [mm:ss] lines)
    when it has them; otherwise gives sentences a speaking-pace timing. Returns (segs, timed)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    if text.lstrip().startswith("{") and '"segments"' in text:  # Whisper JSON
        try:
            import json

            data = json.loads(text)
            segs = [{"t": round(float(x["start"]), 1), "text": str(x["text"]).strip()} for x in data["segments"]
                    if str(x.get("text", "")).strip()]
            if segs:
                return _merge_short(segs), True
        except Exception:
            pass
    lines = text.split("\n")
    cues = []  # (t, text)
    cur_t, buf = None, []

    def flush():
        if cur_t is not None and buf:
            t = " ".join(buf).strip()
            if t:
                cues.append((cur_t, t))

    timed_lines = 0
    has_cues = any(CUE.match(l) for l in lines[:400])
    for raw in lines:
        line = raw.strip()
        if not line or line.upper().startswith("WEBVTT") or line.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        if re.fullmatch(r"\d+", line) and (has_cues or not buf):
            continue  # subtitle number
        m = CUE.match(line)
        if m:
            flush()
            cur_t, buf = _secs(*m.groups()[:4]), []
            timed_lines += 1
            continue
        m = SPEAKER_TS.match(line)
        if m:
            flush()
            g = m.groups()
            cur_t, buf = _secs(g[1], g[2], g[3], g[4]), []
            timed_lines += 1
            continue
        m = LEAD_TS.match(line)
        if m and (m.group(0).strip().startswith(("[", "(")) or re.match(r"^\s*\d{1,2}:\d{2}", line)):
            rest = line[m.end():].strip()
            flush()
            cur_t, buf = _secs(*m.groups()[:4]), []
            timed_lines += 1
            if rest:
                buf.append(SPEAKER_LEAD.sub("", rest))
            continue
        line = re.sub(r"<[^>]+>", "", line)  # vtt styling
        line = SPEAKER_LEAD.sub("", line) if cur_t is not None else line
        buf.append(line)
    flush()
    if timed_lines >= 3 and cues:
        segs = []
        for t, x in cues:
            if segs and segs[-1]["text"] == x:
                continue
            segs.append({"t": round(t, 1), "text": x})
        segs.sort(key=lambda s: s["t"])
        return _merge_short(segs), True
    # untimed: sentences at a speaking pace (lines that are only a number are counters, not speech)
    body = " ".join(SPEAKER_LEAD.sub("", l.strip()) for l in lines if l.strip() and not re.fullmatch(r"\d{1,4}", l.strip()))
    segs, words, cur = [], 0, []
    for snt in sentences(body) or [body]:
        cur.append(snt)
        n = sum(len(x.split()) for x in cur)
        if n >= SEG_WORDS:
            segs.append({"t": round(words / WPM * 60, 1), "text": " ".join(cur)})
            words += n
            cur = []
    if cur:
        segs.append({"t": round(words / WPM * 60, 1), "text": " ".join(cur)})
    return [s for s in segs if s["text"].strip()], False


def _merge_short(segs):
    """Subtitle cues are often a few words each: join them into ~10-second pieces like live recording makes."""
    out = []
    for s in segs:
        if out and (len(out[-1]["text"].split()) < 18 or s["t"] - out[-1]["t"] < 6) and s["t"] - out[-1]["t"] < 20:
            out[-1]["text"] = (out[-1]["text"] + " " + s["text"]).strip()
        else:
            out.append(dict(s))
    return out


def match_slides(segs, pages_by_doc, index_for):
    """Give each transcript piece the slide it most likely belongs to (lectures move forward through slides,
    so a jump needs a clear win)."""
    if not pages_by_doc:
        return 0
    found = 0
    cur = None
    for i, sg in enumerate(segs):
        window = " ".join(x["text"] for x in segs[max(0, i - 1):i + 2])
        if len(content_words(window)) < 4:
            sg["sl"] = cur
            continue
        best, best_score = None, 0.0
        for did, n in pages_by_doc:
            idx = index_for(did)
            if not idx:
                continue
            for p, sc in enumerate(idx.rank(window), 1):
                if cur and cur["doc"] == did and cur["page"] <= p <= cur["page"] + 1:
                    sc *= 1.25  # staying or moving to the next slide is most likely
                if sc > best_score:
                    best, best_score = {"doc": did, "page": p}, sc
        if best and best_score >= 0.16 and (cur is None or best != cur and best_score >= 0.22 or best == cur):
            cur = best
        sg["sl"] = cur
        if cur:
            found += 1
    return found


# ---------------------------------------------------------------- the importer
class Importer:
    def __init__(self, app):
        self.app = app
        self.hub = app.hub
        self.jobs = {}  # kid -> progress dict
        self.lock = threading.Lock()

    # -- state shared with the rest of the app while a class is being built
    def progress(self, kid):
        return self.jobs.get(kid)

    def _emit(self, kid):
        job = self.jobs.get(kid)
        if job:
            self.hub.emit("import", {"kid": kid, **{k: v for k, v in job.items() if k != "s"}})

    def _step(self, kid, sid, state, detail=""):
        job = self.jobs[kid]
        for st in job["steps"]:
            if st["id"] == sid:
                st["state"], st["detail"] = state, detail
        if state == "working":
            name = self.hub.lib.klass(kid)["name"] if self.hub.lib.klass(kid) else "class"
            label = next((st["label"] for st in job["steps"] if st["id"] == sid), "")
            self.hub.set_status(f"Importing {name}: {label.lower()}{' · ' + detail if detail else ''}…")
        self._emit(kid)

    # -- 1. the class
    def create(self, data):
        lib = self.hub.lib
        cid = data.get("course") or (self.hub.course_of(self.hub.kid) or {}).get("id")
        date = str(data.get("date") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date = None
        k = lib.new_class(cid, date)
        name = str(data.get("name") or "").strip()
        if name and name != k["name"]:
            lib.rename_class(k["id"], name)
        notes = str(data.get("notes") or "").strip()
        s = load_class(k["id"])
        if notes:
            s["lines"] = [{"text": x, "t": None, "sl": None} for x in notes.split("\n")]
        s["imported"] = {"at": time.time(), "source": "", "kind": "", "timed": False}
        from store import save_class

        save_class(k["id"], s)
        self.jobs[k["id"]] = {"steps": [{"id": a, "label": b, "state": "waiting", "detail": ""} for a, b in STEPS],
                              "done": False, "error": "", "s": s, "pct": 0}
        self.hub.live_objs[k["id"]] = s
        self.app._lib_changed()
        return {"ok": True, "kid": k["id"], "name": lib.klass(k["id"])["name"]}

    def working_obj(self, kid):
        job = self.jobs.get(kid)
        return job["s"] if job and not job["done"] else None

    # -- 2. the lecture: a transcript file, pasted text, or a recording
    def lecture_file(self, kid, name, body):
        job = self.jobs.get(kid)
        if not job:
            return {"ok": False, "error": "Start the import again."}
        ext = (os.path.splitext(name)[1][1:] or "txt").lower()
        if not body:
            return {"ok": False, "error": f'"{name}" arrived empty. Try again, or open it and paste the text into '
                                          "\"Or paste the transcript\"."}
        if ext not in TEXT_EXT | MEDIA_EXT:
            return {"ok": False, "error": f'"{name}" isn\'t a transcript or recording DenSem Class can read. Use text, '
                                          "Word, PDF, subtitles (.srt/.vtt) or an audio/video file."}
        folder = os.path.join(class_dir(kid), "import")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "lecture." + ext)
        with open(path, "wb") as f:
            f.write(body)
        job["lecture"] = {"path": path, "ext": ext, "name": name, "media": ext in MEDIA_EXT}
        return {"ok": True, "media": ext in MEDIA_EXT}

    def lecture_text(self, kid, text):
        job = self.jobs.get(kid)
        if not job:
            return {"ok": False, "error": "Start the import again."}
        folder = os.path.join(class_dir(kid), "import")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "lecture.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(text))
        job["lecture"] = {"path": path, "ext": "txt", "name": "pasted transcript", "media": False}
        return {"ok": True}

    # -- 3. build everything
    def run(self, data):
        kid = data.get("kid")
        job = self.jobs.get(kid)
        if not job:
            return {"ok": False, "error": "Start the import again."}
        if job.get("running"):
            return {"ok": False, "error": "This class is already being imported."}
        job["running"] = True
        job["opts"] = {"summary": data.get("summary", True) is not False, "notes": data.get("notes", True) is not False}
        threading.Thread(target=self._run, args=(kid,), daemon=True).start()
        return {"ok": True}

    def _sync(self, kid, s):
        """Save the class (the working object is the one the app shows if the class is open)."""
        with self.hub.lock:
            from store import save_class

            save_class(kid, s)
            if kid == self.hub.kid:
                self.hub.emit("state", self.hub.snapshot())
        self.hub.refresh_meta(kid, s)

    def _run(self, kid):
        hub, job = self.hub, self.jobs[kid]
        s = job["s"]
        try:
            lec = job.get("lecture")
            # read / transcribe
            if lec:
                self._step(kid, "read", "working", "transcribing the recording" if lec["media"] else lec["name"])
                if lec["media"]:
                    segs = self._transcribe(kid, lec["path"])
                    timed = True
                else:
                    text = read_text(lec["path"], lec["ext"])
                    segs, timed = parse_transcript(text)
                if not segs:
                    size = os.path.getsize(lec["path"]) if os.path.exists(lec["path"]) else 0
                    if lec["media"]:
                        raise ValueError(f"No speech was found in {lec['name']}.")
                    if lec["ext"] == "pdf":
                        raise ValueError(f"{lec['name']} has no text to read (it may be scanned pictures). Copy the text "
                                         "out of it, or import the recording instead.")
                    raise ValueError(f"No words were found in {lec['name']} ({size:,} bytes). Open it, copy the text and "
                                     "paste it into \"Or paste the transcript\".")
                with hub.lock:
                    for x in segs:
                        x.setdefault("sl", None)
                    s["segs"] = segs
                    s["elapsed"] = float(segs[-1]["t"] + max(10, len(segs[-1]["text"].split()) / WPM * 60))
                    s["q_idx"], s["aid_t"] = 0, 0.0
                    s["imported"].update(source=lec["name"], kind="recording" if lec["media"] else "transcript",
                                         timed=timed)
                words = sum(len(x["text"].split()) for x in segs)
                self._step(kid, "read", "done", f"{words:,} words · {fmt(s['elapsed'])}"
                           + ("" if timed else " (timing estimated)"))
                self._sync(kid, s)
            else:
                self._step(kid, "read", "skipped", "no transcript or recording")
            # slides
            self._step(kid, "slides", "working")
            waited = 0
            while waited < 900:
                docs = s.get("docs", [])
                if not any(d["status"] == "converting" for d in docs):
                    break
                time.sleep(1)
                waited += 1
            ready = [d for d in s.get("docs", []) if d["status"] == "ready"]
            self._step(kid, "slides", "done" if ready else "skipped",
                       f"{sum(d['pages'] for d in ready)} pages" if ready else "no slides")
            # match
            if ready and s["segs"]:
                self._step(kid, "match", "working")
                n = match_slides(s["segs"], [(d["id"], d["pages"]) for d in ready], lambda did: hub.page_index(kid, did))
                log, last = [], None
                for x in s["segs"]:
                    if x.get("sl") and x["sl"] != last:
                        log.append([x["t"], x["sl"]["doc"], x["sl"]["page"]])
                        last = x["sl"]
                s["slide_log"] = log
                self._step(kid, "match", "done", f"{n} of {len(s['segs'])} parts linked to a slide")
                self._sync(kid, s)
            else:
                self._step(kid, "match", "skipped")
            summ = self.app.summ
            if s["segs"] and job["opts"]["summary"]:
                # note helper: instant check on everything, then the AI in 5-minute windows
                self._step(kid, "helper", "working")
                summ._questions(kid, s)
                if ai_ready(hub.cfg) and int(hub.cfg.get("aid_every", 2) or 0):
                    end, start, win = s["elapsed"], 0.0, 300.0
                    while start < end:
                        self._step(kid, "helper", "working", f"{fmt(start)}–{fmt(min(end, start + win))}")
                        segs = [x for x in s["segs"] if start <= x["t"] < start + win]
                        if sum(len(x["text"].split()) for x in segs) >= 30:
                            self._aid_window(kid, s, segs, start, start + win)
                        start += win
                s["aid_t"] = s["elapsed"]
                kinds = {}
                for p in s["points"]:
                    kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
                self._step(kid, "helper", "done", ", ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in kinds.items())
                           or "nothing stood out")
                self._sync(kid, s)
                # part summaries and the whole-lecture summary
                parts = max(1, int(s["elapsed"] // summ.period()) + 1)
                self._step(kid, "summary", "working", f"{parts} part{'s' if parts > 1 else ''}")
                summ.tick(kid, s, True, t_now=s["elapsed"])
                self._step(kid, "summary", "done", (s.get("overall") or {}).get("title", "") or
                           f"{len([b for b in s['blocks'] if not b['quiet']])} parts")
                self._sync(kid, s)
            else:
                self._step(kid, "helper", "skipped")
                self._step(kid, "summary", "skipped")
            # class notes in the student's template
            if job["opts"]["notes"] and (s["segs"] or any(l.get("text", "").strip() for l in s.get("lines", []))):
                self._step(kid, "notes", "working", "this can take a few minutes with a local AI")
                job["notes_wait"] = True
                r = self.app.format_note({"kid": kid, "blend": "balanced"})
                if not r.get("ok"):
                    self._step(kid, "notes", "error", r.get("error", ""))
                    job["notes_wait"] = False
                else:
                    t0 = time.time()
                    while job.get("notes_wait") and time.time() - t0 < 1800:
                        time.sleep(0.5)
                    st = next(x for x in job["steps"] if x["id"] == "notes")
                    if st["state"] == "working":
                        self._step(kid, "notes", "done")
            else:
                self._step(kid, "notes", "skipped")
            job["done"] = True
            hub.set_status("")
            self._emit(kid)
        except Exception as e:
            traceback.print_exc()
            job["error"] = str(e)[:300]
            job["done"] = True
            for st in job["steps"]:
                if st["state"] == "working":
                    st["state"] = "error"
            hub.set_status("")
            self._emit(kid)
        finally:
            job["running"] = False
            self._sync(kid, s)
            if job["done"]:
                job.pop("s", None)
                hub.live_objs.pop(kid, None)

    def notes_finished(self, kid, ok, text=""):
        job = self.jobs.get(kid)
        if job and job.get("notes_wait"):
            job["notes_wait"] = False
            self._step(kid, "notes", "done" if ok else "error", text[:200])

    def _aid_window(self, kid, s, segs, start, end):
        from ai import ask_ai, parse_json
        from live import AID_PROMPT
        from words import chunk_text

        hub = self.hub
        have = [p for p in s["points"] if p["kind"] in ("point", "quote", "source", "term", "example")
                and start - 60 <= p["t"] < end]
        have_txt = ("\nAlready noted (don't repeat):\n" + "\n".join(f"- {p['title']}" for p in have[-12:])) if have else ""
        course = hub.course_name(kid)
        prompt = AID_PROMPT.format(course=f" ({course})" if course else "", earlier="", start=fmt(start), end=fmt(end),
                                   text=chunk_text(segs), have=have_txt)
        try:
            out = parse_json(ask_ai(hub.cfg, prompt, 700, json_mode=True))
        except Exception:
            return
        self.app.summ.merge_aids(kid, s, out.get("items") or [], start, end, segs)

    def _transcribe(self, kid, path):
        tx = self.app.tx
        if not tx.load():
            raise RuntimeError("The speech model couldn't load: " + str(self.hub.model.get("error", "")))
        lang = self.hub.cfg["language"]
        prompt = self.hub.whisper_prompt(kid)
        segments, info = tx.model.transcribe(path, language=None if lang == "auto" else lang, beam_size=3,
                                             vad_filter=True, condition_on_previous_text=True,
                                             initial_prompt=prompt or None)
        total = float(getattr(info, "duration", 0) or 0)
        out, last = [], 0.0
        for sg in segments:
            text = sg.text.strip()
            if not text:
                continue
            out.append({"t": round(sg.start, 1), "text": text})
            if time.time() - last > 1.5:
                last = time.time()
                pct = int(sg.end / total * 100) if total else 0
                self._step(kid, "read", "working", f"transcribing the recording · {pct}%"
                           + (f" of {fmt(total)}" if total else ""))
        return _merge_short(out)
