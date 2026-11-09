"""Live part of a class: microphone -> Whisper transcript -> summary points that update as the lecturer talks."""
import os
import queue
import re
import threading
import time
import traceback

import numpy as np

from ai import Offline, ask_ai, ai_ready, parse_json
from store import MODELS
import dictionary
from words import (DEADLINE, STOP, aids_in, chunk_text, content_words, fmt, key_sentences, overlap, questions_in,
                   sentences, similarity, to_sec, word_count)

SR = 16000
BLOCK = 1600  # 0.1 s
BOILER = re.compile(r"^(thank you\.?|thanks for watching.*|subtitles by.*|you\.?|\.+)$", re.I)
KINDS = ("point", "question", "deadline", "quote", "source", "term", "word", "example")
AID_KINDS = ("point", "quote", "source", "term", "word", "example")


def helper_on(cfg, kind):
    """Each note helper can be switched off in Preferences; an off helper isn't looked for and costs no AI."""
    return (cfg.get("helpers") or {}).get(kind, True) is not False


AID_LINES = {
    "point": '- "point": a main idea or claim the lecturer stressed, one short sentence, in the lecturer\'s words where possible.',
    "quote": '- "quote": a striking sentence the lecturer said or read out, word for word; "who" = the person quoted (empty if it\'s the lecturer\'s own words).',
    "source": '- "source": an idea the lecturer credited to a book, article or author; "who" = the author, "work" = the title if it was said; "text" = the idea in one sentence.',
    "term": '- "term": a key term of the subject that the lecturer used or explained; "text" = the term, "meaning" = its meaning as the lecturer explained it (empty if not explained), "dict" = a short dictionary definition.',
    "word": '- "word": a difficult English word the lecturer used that an average English speaker may not know (not a subject term already listed, not a name); "text" = the word in its dictionary form, "pos" = part of speech, "dict" = a plain dictionary definition in under 20 words.',
    "example": '- "example": a story, case or illustration the lecturer used, in one sentence.',
}


def slide_at(s, t):
    """The slide that was on screen at lecture time t (or None)."""
    found = None
    for lt, doc, page in s.get("slide_log", []):
        if lt <= t + 2:
            found = {"doc": doc, "page": page}
        else:
            break
    return found


# ---------------------------------------------------------------- speech to text
class Transcriber:
    def __init__(self, hub):
        self.hub = hub
        self.model = None
        self.name = None
        self.load_lock = threading.Lock()

    def wanted(self):
        name = self.hub.cfg["whisper"]
        if self.hub.cfg["language"] != "en" and name.endswith(".en"):
            name = name[:-3]
        return name

    def load(self):
        with self.load_lock:
            name = self.wanted()
            if self.model is not None and self.name == name:
                return True
            self.hub.model = {"state": "loading", "name": name}
            self.hub.emit("model", self.hub.model)
            try:
                from faster_whisper import WhisperModel

                m = WhisperModel(name, device="cpu", compute_type="int8", download_root=MODELS,
                                 cpu_threads=min(8, os.cpu_count() or 4))
            except Exception as e:
                self.hub.model = {"state": "error", "name": name, "error": str(e)[:300]}
                self.hub.emit("model", self.hub.model)
                return False
            self.model, self.name = m, name
            self.hub.model = {"state": "ready", "name": name}
            self.hub.emit("model", self.hub.model)
            return True

    def transcribe(self, audio, prompt):
        lang = self.hub.cfg["language"]
        segments, _ = self.model.transcribe(
            audio,
            language=None if lang == "auto" else lang,
            beam_size=3,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=400),
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )
        out = []
        for s in segments:
            text = s.text.strip()
            if not text or BOILER.match(text):
                continue
            if s.no_speech_prob > 0.6 and s.avg_logprob < -0.8:
                continue
            out.append((s.start, text))
        return out


def quiet_cut(buf, length, window=3 * SR):
    """Cut point inside the last `window` samples of buf[:length], at the quietest 0.1 s."""
    start = max(length // 2, length - window)
    region = buf[start:length]
    frames = len(region) // BLOCK
    if frames < 2:
        return length
    rms = np.sqrt(np.mean(region[: frames * BLOCK].reshape(frames, BLOCK) ** 2, axis=1))
    return start + int(np.argmin(rms)) * BLOCK + BLOCK // 2


class Recorder:
    """Records into one class. Text arrives every ~10 seconds."""

    CHUNK = 10 * SR

    def __init__(self, hub, tx, kid, s):
        self.hub, self.tx, self.kid, self.s = hub, tx, kid, s
        self.q = queue.Queue()
        self.stream = None
        self.running = False
        self.thread = None
        self.lag_warned = False

    def start(self):
        import sounddevice as sd

        dev = self.hub.cfg.get("device")
        dev = None if dev in ("", None, "default") else dev
        try:
            self.stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=dev,
                                         blocksize=BLOCK, callback=self._cb)
            self.stream.start()
        except Exception as e:
            raise RuntimeError(f"Could not open the microphone ({e}). Check the mic choice in Preferences.")
        self._begin()

    def _begin(self):
        self.base = self.s["elapsed"]
        self.running = True
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

    def _cb(self, indata, frames, t, status):
        self.q.put(indata[:, 0].copy())

    def stop(self):
        self.running = False
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass
        self.stream = None
        if self.thread:
            self.thread.join(timeout=120)

    def halt(self, wait=2.0):
        """Quitting the app: stop the microphone now and give the last piece of speech a moment, no more."""
        self.running = False
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass
        self.stream = None
        if self.thread:
            self.thread.join(timeout=wait)

    def _work(self):
        consumed = 0
        buf = np.zeros(0, np.float32)
        heard_at = time.time()
        last_level = 0.0
        silent_warned = False
        while True:
            try:
                block = self.q.get(timeout=0.2)
            except queue.Empty:
                if not self.running:
                    break
                continue
            buf = np.concatenate([buf, block])
            level = float(np.sqrt(np.mean(block**2)))
            if time.time() - last_level > 0.15:
                self.hub.emit("level", {"v": round(min(1.0, level * 14), 3)})
                last_level = time.time()
            if level > 1e-5:
                heard_at = time.time()
                if silent_warned:
                    silent_warned = False
                    self.hub.set_status("")
            elif time.time() - heard_at > 5 and not silent_warned:
                silent_warned = True
                self.hub.set_status("The microphone is sending silence. Allow it in System Settings › Privacy & "
                                    "Security › Microphone, then restart the app.")
            if len(buf) >= self.CHUNK:
                cut = quiet_cut(buf, self.CHUNK)
                piece, buf = buf[:cut], buf[cut:]
                self._do(piece, self.base + consumed / SR)
                consumed += cut
                self._check_lag(len(buf))
        while not self.q.empty():
            buf = np.concatenate([buf, self.q.get()])
        if len(buf) > SR:
            self._do(buf, self.base + consumed / SR)

    def _check_lag(self, buffered):
        behind = self.q.qsize() * BLOCK / SR + buffered / SR - self.CHUNK / SR
        if behind > 25 and not self.lag_warned:
            self.lag_warned = True
            self.hub.set_status(f"Transcription is {int(behind)} s behind the lecture. Choose a smaller speech "
                                "model in Preferences.")
        elif behind < 8 and self.lag_warned:
            self.lag_warned = False
            self.hub.set_status("")

    def _do(self, audio, t0):
        while self.tx.model is None:
            if self.hub.model["state"] == "error":
                return
            time.sleep(0.3)
        with self.hub.lock:
            tail = " ".join(x["text"] for x in self.s["segs"][-4:])[-200:]
        try:
            result = self.tx.transcribe(audio, self.hub.whisper_prompt(self.kid) + tail)
        except Exception as e:
            self.hub.set_status(f"Transcription error: {e}")
            return
        if not result:
            return
        t = round(t0 + result[0][0], 1)
        seg = {"t": t, "text": " ".join(x for _, x in result), "sl": slide_at(self.s, t)}
        self.hub.on_seg(self.kid, self.s, seg)


# ---------------------------------------------------------------- live summary
BLOCK_PROMPT = """You write the running summary of a lecture{course} for a student, one part at a time. The transcript is from speech recognition, so expect mis-heard words; infer the intended terms from context.
{earlier}
THIS PART ({start}–{end}) — transcript:
{text}
{slides}{questions}
Summarize only this part. Keep its specific content: facts, definitions, formulas, names, dates, examples, instructions.

Reply with JSON only:
{{"title":"what this part was about, max 8 words","summary":"2-3 sentences","details":["5-8 short bullets with the specific content of this part"],"answers":[{{"id":"p3","answer":"one sentence, only if this part answers that question"}}],"topic":"what the lecturer is discussing at the end of this part, max 8 words"}}"""

OVERALL_PROMPT = """Here are the part-by-part summaries of a whole lecture{course}. Put them together into one summary of the lecture that a student can review later.

{parts}
{questions}
Reply with JSON only:
{{"title":"the lecture's subject, max 10 words","summary":"4-6 sentences covering the whole lecture in order","key_points":["6-10 bullets: the most important things to remember, with specifics"]}}"""


AID_PROMPT = """You help a student take notes during a live lecture{course}. The transcript is from speech recognition: fix obvious mis-heard words from context.
{earlier}
WHAT THE LECTURER JUST SAID [{start}–{end}]:
{text}

List only what a student should write down from this stretch, 0 to 5 items, of these kinds only:
{kinds}
Skip small talk, class logistics and questions. Never invent names or titles that weren't said.{have}{questions}{define}
Reply with JSON only: {{"items":[{{"kind":"{kind_list}","text":"...","who":"","work":"","meaning":"","pos":"","dict":"","at":"mm:ss"}}],"questions":[{{"id":"","brief":"","context":""}}],"defs":[{{"word":"","pos":"","dict":""}}]}}"""


def aid_extra(s, start, end, cfg=None):
    """Extra parts of AID_PROMPT: questions asked in this stretch, and words still waiting for a definition."""
    qs = []  # questions are restated only when the student asks ("What was the question?")
    q_txt = ("\n\nQUESTIONS THE LECTURER ASKED in this stretch. For each, \"brief\" = the question restated in under 15 "
             "plain words so it makes sense on its own, and \"context\" = one sentence on what was being discussed when "
             "it was asked, for a student who wasn't listening:\n" + "\n".join(f"[{p['id']}] {p['title']}" for p in qs[:6])) if qs else ""
    need = [p for p in s["points"] if p["kind"] in ("word", "term") and helper_on(cfg or {}, p["kind"]) and not (p["detail"] if p["kind"] == "word" else p.get("dict"))]
    d_txt = ("\n\nGive a short dictionary definition (\"defs\") for each of these words from the lecture, as used there: "
             + ", ".join(p["title"] for p in need[-8:])) if need else ""
    return q_txt, d_txt


def q_context(segs, i, q):
    """What was said just before a question, so a student who drifted off can see what it's about."""
    before = []
    head = segs[i]["text"].split(q)[0] if q in segs[i]["text"] else ""
    for x in reversed(sentences(head)):
        before.insert(0, x)
    j = i - 1
    while j >= 0 and sum(len(x.split()) for x in before) < 35:
        for x in reversed(sentences(segs[j]["text"])):
            before.insert(0, x)
            if sum(len(y.split()) for y in before) >= 35:
                break
        j -= 1
    while before and sum(len(x.split()) for x in before) > 45:
        before.pop(0)
    txt = " ".join(x for x in before if not x.endswith("?") or len(x.split()) > 6)
    return txt[-320:].lstrip(" ,.")


def add_dict(p):
    """Put the Mac's dictionary sense on a key term (one or two words only)."""
    if len(p["title"].split()) <= 2:
        pos, sense = dictionary.define(p["title"])
        if sense:
            p.update(dict=(f"{pos} · " if pos else "") + sense, dsrc="dictionary")


def _strs(xs, n, size):
    return [str(x).strip()[:size] for x in (xs or []) if str(x).strip()][:n]


class Summarizer:
    """Questions and due dates appear the moment they're heard. Every `period` minutes the part just heard is
    summarized (title, summary, details); when class is paused or finished, all parts become one whole-lecture summary."""

    def __init__(self, hub):
        self.hub = hub
        self.ev = threading.Event()
        self.lock = threading.Lock()
        self.err = ""

    def poke(self):
        self.ev.set()

    def period(self):
        """Length of one summary part. With "only when I pause or finish" (period 0) the lecture is still
        summarized in 10-minute parts, but all at once at the end."""
        try:
            return max(1, int(self.hub.cfg.get("period") or 10)) * 60
        except (TypeError, ValueError):
            return 600

    def live_parts(self):
        """True when parts are summarized during class; False (the default) means only when paused or finished."""
        try:
            return int(self.hub.cfg.get("period") or 0) > 0
        except (TypeError, ValueError):
            return False

    def loop(self):
        while True:
            self.ev.wait(5)
            self.ev.clear()
            if not self.hub.recording:
                continue
            try:
                with self.hub.lock:
                    kid, s = self.hub.kid, self.hub.s
                self.tick(kid, s, False)
            except Exception:
                traceback.print_exc()

    def tick(self, kid, s, final, t_now=None):
        """final=True: class paused/finished or "Summarize now": close the part in progress and build the whole summary."""
        with self.lock:
            self._questions(kid, s)
            if t_now is None:
                t_now = self.hub.now() if kid == self.hub.kid else s["elapsed"]
            self._aids_ai(kid, s, t_now, final)
            made = False
            while final or self.live_parts():  # catch up on every whole period that has passed
                start = s["blocks"][-1]["end"] if s.get("blocks") else 0.0
                if t_now - start >= self.period():
                    self._close_block(kid, s, start, start + self.period())
                    made = True
                    continue
                break
            if final:
                start = s["blocks"][-1]["end"] if s.get("blocks") else 0.0
                segs = [x for x in s["segs"] if x["t"] >= start]
                if word_count(segs) >= 20:
                    self._close_block(kid, s, start, max(t_now, segs[-1]["t"] + 1))
                    made = True
                if s.get("blocks") and (made or not s.get("overall")):
                    self._overall(kid, s)

    # -- instant: questions and due dates
    def _questions(self, kid, s):
        new, upd = [], []
        with self.hub.lock:
            segs = s["segs"][s["q_idx"]:]
            s["q_idx"] = len(s["segs"])
            level = str(self.hub.cfg.get("hard_words") or "normal")
            cfg = self.hub.cfg
            first = len(s["segs"]) - len(segs)
            for i, seg in enumerate(segs):
                for q in (questions_in(seg["text"]) if helper_on(cfg, "question") else []):
                    if any(p["kind"] == "question" and similarity(p["title"], q) > 0.8 for p in s["points"][-10:] + new):
                        continue
                    p = self._new_point(s, seg["t"], "question", q, "", seg.get("sl"))
                    p["ctx"] = q_context(s["segs"], first + i, q)
                    new.append(p)
                for kind, title, detail in aids_in(seg["text"]):
                    if not helper_on(cfg, kind):
                        continue
                    recent = [p for p in s["points"][-25:] + new if p["kind"] in AID_KINDS]
                    if any(similarity(p["title"], title) > 0.6 or (kind == "term" and p["kind"] in ("term", "word") and
                                                                   p["title"].lower() == title.lower()) for p in recent):
                        old = next((p for p in s["points"] if kind == "term" and p["kind"] == "word"
                                    and p["title"].lower() == title.lower()), None)
                        if old:  # the lecturer explained a word we'd flagged as difficult: it's a key term now
                            old.update(kind="term", dict=old.get("detail", ""), detail=detail)
                            old["rev"] = old.get("rev", 0) + 1
                            upd.append(dict(old))
                        continue
                    p = self._new_point(s, seg["t"], kind, title, detail, seg.get("sl"))
                    if kind == "term":
                        add_dict(p)
                    new.append(p)
                known = {p["title"].lower() for p in s["points"] + new if p["kind"] in ("term", "word")}
                known.update(self.hub.cfg.get("known_words") or [])
                for w in (dictionary.hard_words(seg["text"], level, known) if helper_on(cfg, "word") else []):
                    pos, sense = dictionary.define(w)
                    p = self._new_point(s, seg["t"], "word", w, sense, seg.get("sl"))
                    p.update(pos=pos, dsrc="dictionary" if sense else "")
                    new.append(p)
                for x in sentences(seg["text"]):
                    if x.endswith("?") or len(x.split()) < 5 or not DEADLINE.search(x) or not helper_on(cfg, "deadline"):
                        continue
                    if any(p["kind"] == "deadline" and overlap(p["title"], x) > 0.5 for p in s["points"][-12:] + new):
                        continue
                    new.append(self._new_point(s, seg["t"], "deadline", x, "", seg.get("sl")))
            s["points"].extend(new)
        if new or upd:
            self.hub.save_and_emit(kid, s, "points", {"add": new, "update": upd, "topic": s["topic"]})

    # -- every minute or two: the AI tidies and adds to the note helper
    def aid_every(self):
        try:
            return max(0, int(self.hub.cfg.get("aid_every", 2))) * 60
        except (TypeError, ValueError):
            return 120

    def _aids_ai(self, kid, s, t_now, final):
        hub = self.hub
        every = self.aid_every()
        if not every or not ai_ready(hub.cfg):
            return
        start = float(s.get("aid_t") or 0)
        if t_now - start > every * 3:  # resumed an older class: only look at the last stretch
            start = max(start, t_now - every * 2)
        if t_now - start < every and not final:
            return
        with hub.lock:
            segs = [x for x in s["segs"] if x["t"] >= start]
        if word_count(segs) < (15 if final else 40):
            if final:
                s["aid_t"] = t_now
            return
        end = segs[-1]["t"] + 1
        have = [p for p in s["points"] if p["kind"] in AID_KINDS and p["t"] >= start - 180]
        have_txt = ("\nAlready noted (don't repeat):\n" + "\n".join(f"- {p['title']}" for p in have[-12:])) if have else ""
        earlier = [b for b in s.get("blocks", []) if not b["quiet"]][-1:]
        earlier_txt = (f"\nThe lecture so far: {earlier[0]['title']}: {earlier[0]['summary']}\n") if earlier else ""
        course = hub.course_name(kid)
        kinds = [k for k in AID_KINDS if helper_on(hub.cfg, k)]
        q_txt, d_txt = aid_extra(s, start, end, hub.cfg)
        if not kinds and not d_txt:  # every AI helper is off: no AI call at all
            s["aid_t"] = end
            return
        prompt = AID_PROMPT.format(kinds="\n".join(AID_LINES[k] for k in kinds) or "(none: return no items)",
                                   kind_list="|".join(kinds) or "none", course=f" ({course})" if course else "", earlier=earlier_txt, start=fmt(start),
                                   end=fmt(end), text=chunk_text(segs), have=have_txt, questions=q_txt, define=d_txt)
        try:
            out = parse_json(ask_ai(hub.cfg, prompt, 1100, json_mode=True))
        except Exception:
            s["aid_t"] = start + max(every / 2, 30)  # try again soon, without hammering a slow or missing AI
            return
        s["aid_t"] = end
        self.merge_aids(kid, s, out.get("items") or [], start, end, segs, out)

    def merge_aids(self, kid, s, items, start, end, segs, out=None):
        add, upd = [], []
        out = out if isinstance(out, dict) else {}
        with self.hub.lock:
            byid = {p["id"]: p for p in s["points"]}
            for q in (out.get("questions") or [])[:8]:
                p = byid.get(str((q or {}).get("id") or "").strip("[] ")) if isinstance(q, dict) else None
                if not p or p["kind"] != "question":
                    continue
                brief, ctx = str(q.get("brief") or "").strip()[:160], str(q.get("context") or "").strip()[:300]
                if brief or ctx:
                    p.update(brief=brief or p.get("brief", ""), ctx=ctx or p.get("ctx", ""))
                    p["rev"] = p.get("rev", 0) + 1
                    upd.append(dict(p))
            for d in (out.get("defs") or [])[:10]:
                if not isinstance(d, dict):
                    continue
                w, sense = str(d.get("word") or "").strip().lower(), str(d.get("dict") or "").strip()[:240]
                for p in s["points"]:
                    if not sense or p["title"].lower() != w:
                        continue
                    if p["kind"] == "word" and not p["detail"]:
                        p.update(detail=sense, pos=p.get("pos") or str(d.get("pos") or "")[:20], dsrc="ai")
                    elif p["kind"] == "term" and not p.get("dict"):
                        p.update(dict=sense, dsrc="ai")
                    else:
                        continue
                    upd.append(dict(p))
            pool = [p for p in s["points"] if p["kind"] in AID_KINDS and start - 60 <= p["t"] <= end + 5]
            for it in items[:6]:
                if not isinstance(it, dict):
                    continue
                kind = str(it.get("kind") or "").lower()
                text = str(it.get("text") or "").strip()
                if kind not in AID_KINDS or len(text) < 2 or not helper_on(self.hub.cfg, kind):
                    continue
                who, work, meaning = (str(it.get(k) or "").strip() for k in ("who", "work", "meaning"))
                sense, pos = str(it.get("dict") or "").strip()[:240], str(it.get("pos") or "").strip()[:20]
                if kind == "word":
                    text = text.lower()
                    if len(text.split()) > 3 or text in (self.hub.cfg.get("known_words") or []):
                        continue
                    old = next((p for p in s["points"] + add if p["kind"] in ("term", "word") and p["title"].lower() == text), None)
                    if old is not None:  # already listed: only fill in a missing definition
                        if sense and old["kind"] == "word" and not old["detail"]:
                            old.update(detail=sense, pos=old.get("pos") or pos, dsrc="ai")
                            upd.append(dict(old))
                        elif sense and old["kind"] == "term" and not old.get("dict"):
                            old.update(dict=sense, dsrc="ai")
                            upd.append(dict(old))
                        continue
                if kind in ("term", "word"):
                    title, detail = text[:60], (meaning if kind == "term" else sense)
                else:
                    title, detail = text, " · ".join(v for v in (who, work) if v)
                t = to_sec(it.get("at")) if it.get("at") else 0
                if not start <= t <= end:
                    hit = max(segs, key=lambda x: overlap(x["text"], title)) if segs else None
                    t = hit["t"] if hit else start
                sl = next((x.get("sl") for x in segs if abs(x["t"] - t) < 12 and x.get("sl")), None)
                same = None
                for p in pool + add:
                    if (similarity(p["title"], title) > 0.45 or (kind == "term" and p["kind"] == "term" and
                                                                p["title"].lower() == title.lower())
                            or (kind in ("quote", "source") and p["kind"] in ("quote", "source") and abs(p["t"] - t) < 40
                                and overlap(p["title"], title) > 0.3)):
                        same = p
                        break
                if same is not None:
                    if same.get("by") != "ai" and same in pool:
                        keep_word = same["kind"] == "word" and kind == "word"
                        same.update(kind=kind, title=title[:220], by="ai",
                                    detail=(same.get("detail") if keep_word and same.get("detail") else detail[:500])
                                    or same.get("detail", ""))
                        if kind == "term" and sense and not same.get("dict"):
                            same.update(dict=sense, dsrc="ai")
                        same["rev"] = same.get("rev", 0) + 1
                        upd.append(dict(same))
                    continue
                p = self._new_point(s, t, kind, title, detail, sl)
                p["by"] = "ai"
                if kind == "word":
                    mpos, msense = dictionary.define(title)
                    if msense:  # the Mac's dictionary wins over the AI's wording
                        p.update(detail=msense, pos=mpos or pos, dsrc="dictionary")
                    else:
                        p.update(pos=pos, dsrc="ai" if detail else "")
                elif kind == "term":
                    add_dict(p)
                    if not p.get("dict") and sense:
                        p.update(dict=sense, dsrc="ai")
                add.append(p)
            s["points"].extend(add)
            s["points"].sort(key=lambda p: p["t"])
        if add or upd:
            self.hub.save_and_emit(kid, s, "points", {"add": add, "update": upd, "topic": s["topic"]})

    def _new_point(self, s, t, kind, title, detail, sl=None):
        s["pid"] = s.get("pid", 0) + 1
        return {"id": f"p{s['pid']}", "kind": kind if kind in KINDS else "point", "t": round(t, 1),
                "title": str(title).strip()[:220], "detail": str(detail or "").strip()[:500], "rev": 0, "sl": sl}

    # -- every N minutes
    def _close_block(self, kid, s, start, end):
        hub = self.hub
        with hub.lock:
            segs = [x for x in s["segs"] if start <= x["t"] < end]
            qs = [p for p in s["points"] if p["kind"] == "question" and start - 30 <= p["t"] < end]
        s.setdefault("bid", 0)
        block = {"id": f"b{s['bid'] + 1}", "start": round(start, 1), "end": round(end, 1), "title": "", "summary": "",
                 "details": [], "quiet": False, "by": "", "sl": None}
        answers = []
        if word_count(segs) < 15:
            block["quiet"] = True
        else:
            pages = []
            for x in segs:
                if x.get("sl") and x["sl"] not in pages:
                    pages.append(x["sl"])
            block["sl"] = pages[0] if pages else None
            done = False
            if ai_ready(hub.cfg):
                try:
                    out, topic = self._ask_block(kid, s, start, end, segs, qs, pages)
                    block.update(title=str(out.get("title") or "")[:90], summary=str(out.get("summary") or "")[:700],
                                 details=_strs(out.get("details"), 10, 260), by="ai")
                    answers = [a for a in out.get("answers") or [] if isinstance(a, dict)]
                    if topic:
                        s["topic"] = topic
                    done = bool(block["summary"] or block["details"])
                    if self.err and hub.status == self.err:
                        hub.set_status("")
                    self.err = ""
                except ValueError:
                    pass
                except Exception as e:
                    self.err = str(e) if isinstance(e, (Offline, RuntimeError)) else f"Summary problem: {e}"
                    hub.set_status(self.err)
            if not done:
                picks = key_sentences(segs, 6)
                block.update(details=[x for _, x in picks][:6], by="basic")
                block["summary"] = picks[0][1] if picks else ""
                lead = picks[0][1] if picks else ""
                words = [w for w in re.findall(r"[A-Za-zÀ-ɏ][\w'’-]*", lead) if w.lower() not in STOP and len(w) > 2][:6]
                title = " ".join(dict.fromkeys(words))
                block["title"] = (title[:1].upper() + title[1:]) if title else "Lecture"
        updates = []
        with hub.lock:
            s["bid"] = s.get("bid", 0) + 1
            s["blocks"].append(block)
            byid = {p["id"]: p for p in s["points"]}
            for a in answers:
                p = byid.get(str(a.get("id", "")))
                ans = str(a.get("answer") or "").strip()
                if p and p["kind"] == "question" and ans and not p["detail"]:
                    p["detail"] = "Answer: " + ans[:400]
                    p["rev"] = p.get("rev", 0) + 1
                    updates.append(dict(p))
        hub.save_and_emit(kid, s, "blocks", {"blocks": s["blocks"], "overall": s.get("overall"), "topic": s["topic"]})
        if updates:
            hub.save_and_emit(kid, s, "points", {"add": [], "update": updates, "topic": s["topic"]})

    def _ask_block(self, kid, s, start, end, segs, qs, pages):
        hub = self.hub
        course = hub.course_name(kid)
        earlier = [b for b in s["blocks"] if not b["quiet"]][-3:]
        earlier_txt = ("\nEARLIER PARTS (context only):\n" + "\n".join(
            f"[{fmt(b['start'])}–{fmt(b['end'])}] {b['title']}: {b['summary']}" for b in earlier) + "\n") if earlier else ""
        slides = ""
        for sl in pages[:4]:
            text = re.sub(r"\s+", " ", hub.slide_text(kid, sl) or "").strip()
            if text:
                slides += f"[slide {sl['page']}] {text[:350]}\n"
        if slides:
            slides = "\nSlides on screen during this part (for spelling and context):\n" + slides
        questions = ""
        if qs:
            questions = "\nQuestions the lecturer asked (answer only if this part answers them):\n" + "\n".join(
                f"[{p['id']}] {p['title']}" for p in qs) + "\n"
        prompt = BLOCK_PROMPT.format(course=f" ({course})" if course else "", earlier=earlier_txt, start=fmt(start),
                                     end=fmt(end), text=chunk_text(segs), slides=slides, questions=questions)
        out = parse_json(ask_ai(hub.cfg, prompt, 1200, json_mode=True))
        return out, str(out.get("topic") or "")[:80]

    # -- the whole lecture
    def _overall(self, kid, s):
        hub = self.hub
        blocks = [b for b in s["blocks"] if not b["quiet"]]
        if not blocks:
            return
        overall = None
        qs = [p for p in s["points"] if p["kind"] in ("question", "deadline")]
        if ai_ready(hub.cfg):
            parts = "\n\n".join(f"[{fmt(b['start'])}–{fmt(b['end'])}] {b['title']}\n{b['summary']}\n"
                                + "\n".join("- " + d for d in b["details"]) for b in blocks)
            qtxt = ("\nAsked or announced in class:\n" + "\n".join(
                f"- {'Due: ' if p['kind'] == 'deadline' else 'Q: '}{p['title']} {p['detail']}".strip() for p in qs) + "\n") if qs else ""
            course = hub.course_name(kid)
            try:
                out = parse_json(ask_ai(hub.cfg, OVERALL_PROMPT.format(course=f" ({course})" if course else "",
                                                                     parts=parts[:24000], questions=qtxt), 1500, json_mode=True))
                overall = {"title": str(out.get("title") or "")[:100], "summary": str(out.get("summary") or "")[:1500],
                           "points": _strs(out.get("key_points"), 12, 300), "by": "ai"}
                if not overall["summary"] and not overall["points"]:
                    overall = None
            except Exception as e:
                hub.set_status(str(e) if isinstance(e, (Offline, RuntimeError)) else f"Summary problem: {e}")
        if overall is None:
            overall = {"title": blocks[0]["title"] if len(blocks) == 1 else " · ".join(b["title"] for b in blocks[:4]),
                       "summary": " ".join(b["summary"] for b in blocks if b["summary"])[:1500],
                       "points": [b["details"][0] for b in blocks if b["details"]][:10], "by": "basic"}
        overall.update(upto=len(s["blocks"]), end=blocks[-1]["end"])
        with hub.lock:
            s["overall"] = overall
        hub.save_and_emit(kid, s, "blocks", {"blocks": s["blocks"], "overall": overall, "topic": s["topic"]})

    # -- "What did I miss?"
    def last_question(self, kid, s, now):
        """The latest question the lecturer asked, restated briefly with what it was about. On demand only."""
        hub = self.hub
        with hub.lock:
            qs = [p for p in s["points"] if p["kind"] == "question"]
            if not qs:
                return {"title": "Last question", "text": "No question heard yet in this class."}
            p = qs[-1]
            segs = [x for x in s["segs"] if p["t"] - 120 <= x["t"] <= p["t"] + 20]
        ago = max(0, int(now - p["t"]))
        when = f"{fmt(p['t'])} · {ago // 60} min ago" if ago >= 60 else f"{fmt(p['t'])} · just now"
        if not p.get("brief") and ai_ready(hub.cfg) and segs:
            course = hub.course_name(kid)
            prompt = f"""A student{f' in {course}' if course else ''} wasn't listening when the lecturer asked the class a question and may be called on. Speech-recognition transcript around it (may contain mis-heard words):

{chunk_text(segs)}

The question, at {fmt(p['t'])}: {p['title']}

Reply with JSON only: {{"brief":"the question restated in under 15 plain words so it makes sense on its own","context":"one or two sentences on what was being discussed that led to it","hint":"one short line on what a good answer would touch on, from what was said"}}"""
            try:
                out = parse_json(ask_ai(hub.cfg, prompt, 300, json_mode=True))
                with hub.lock:
                    p["brief"] = str(out.get("brief") or "").strip()[:160]
                    p["ctx"] = str(out.get("context") or "").strip()[:320] or p.get("ctx", "")
                    p["hint"] = str(out.get("hint") or "").strip()[:200]
                hub.save_and_emit(kid, s, "points", {"add": [], "update": [dict(p)], "topic": s["topic"]})
            except Exception:
                pass
        text = f"Question: {p.get('brief') or p['title']}"
        if p.get("brief") and p["brief"] != p["title"]:
            text += f"\nAsked: “{p['title']}”"
        if p.get("ctx"):
            text += f"\nAbout: {p['ctx']}"
        if p.get("hint"):
            text += f"\nAnswer could cover: {p['hint']}"
        if len(qs) > 1:
            text += "\n\nEarlier: " + " · ".join(f"[{fmt(q['t'])}] {q.get('brief') or q['title']}" for q in qs[-3:-1])
        return {"title": f"Last question · {when}", "text": text}

    def catch_up(self, kid, s, end, mins):
        hub = self.hub
        start = max(0, end - mins * 60)
        with hub.lock:
            segs = [x for x in s["segs"] if x["t"] >= start]
        title = f"Last {mins} min · {fmt(start)}–{fmt(end)}"
        if not segs:
            return {"title": title, "text": "Nothing transcribed in that window yet."}
        note = ""
        if ai_ready(hub.cfg):
            course = hub.course_name(kid)
            prompt = f"""A student{f' in {course}' if course else ''} lost focus during a lecture and needs to catch up fast. Here is the speech-recognition transcript of the last {mins} minute(s) (it may contain mis-heard words):

{chunk_text(segs)}

If the lecturer asked the class a question in this stretch, begin with a line starting "Question:" that restates the latest one briefly so it makes sense on its own, then a line starting "About:" saying in one sentence what was being discussed, so the student could answer if called on.
Then in 2-4 short bullet points (start each with "• "), tell them what they missed: the main idea, any key term or definition, any example or formula, and anything they were asked to do. Finish with one line starting "Right now:" saying what the lecturer is in the middle of. Plain text, no preamble."""
            try:
                return {"title": title, "text": ask_ai(hub.cfg, prompt, 400)}
            except Exception as e:
                note = str(e)
        picks = key_sentences(segs, 3)
        text = "\n".join("• " + x for _, x in picks) or " ".join(x["text"] for x in segs[-3:])
        qp = [p for p in s["points"] if p["kind"] == "question" and start <= p["t"] <= end + 5]
        qs = [q for x in segs for q in questions_in(x["text"])]
        if qp:
            q = qp[-1]
            text = f"Question: {q.get('brief') or q['title']}" + (f"\nAbout: {q['ctx']}" if q.get("ctx") else "") + "\n" + text
        elif qs:
            text += "\n• Asked: " + qs[-1]
        if note:
            text += f"\n\n({note} This recap is from key sentences.)"
        return {"title": title, "text": text}
