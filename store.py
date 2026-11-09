"""Where DenSemNotes keeps things: settings, the course/class library, and each class's data."""
import datetime
import json
import os
import re
import shutil
import sys
import time
import uuid

APP_NAME = "DenSemNotes"
if sys.platform == "darwin":
    _SUPPORT = os.path.expanduser("~/Library/Application Support")
    DATA = os.path.join(_SUPPORT, APP_NAME)
    RENAMED_FROM = os.path.join(_SUPPORT, "DenSem Class")
    OLD_DATA = [os.path.join(_SUPPORT, n) for n in ("DenSem Lecture", "Lecture Catch-Up")]
else:
    DATA = os.path.expanduser("~/.densemnotes")
    RENAMED_FROM = os.path.expanduser("~/.densem-class")
    OLD_DATA = [os.path.expanduser("~/.densem-lecture"), os.path.expanduser("~/.lecture-catchup")]


def _carry_over(old, new):
    """The app was called DenSem Class: move its folder to the new name once, keeping everything in it."""
    try:
        if os.path.isdir(old) and not os.path.exists(new):
            os.rename(old, new)
    except OSError:
        pass


_carry_over(RENAMED_FROM, DATA)
_carry_over(os.path.expanduser("~/Documents/DenSem Class"), os.path.expanduser(f"~/Documents/{APP_NAME}"))

MODELS = os.path.join(DATA, "models")
CFG_PATH = os.path.join(DATA, "config.json")
LIB_PATH = os.path.join(DATA, "library.json")
CLASSES = os.path.join(DATA, "classes")
TRASH = os.path.join(DATA, "trash")
EXPORT_ROOT = os.path.expanduser(f"~/Documents/{APP_NAME}")

EMPTY_CLASS = dict(
    segs=[],          # transcript: {t, text, sl}
    points=[],        # heard right away: questions and due dates {id, kind, t, title, detail, rev, sl}
    blocks=[],        # one summary per period: {id, start, end, title, summary, details, quiet, by, sl}
    bid=0,
    overall=None,     # whole-lecture summary: {title, summary, points, upto, end, by}
    asks=[],          # questions asked of my notes: {q, scope, answer, sources, at, mode}
    versions=[],      # earlier versions of my notes, e.g. before formatting as a lesson: {at, label, lines}
    formatted=None,   # set when my notes were turned into a MyNotes lesson: {at, note, blend}
    topic="",
    elapsed=0.0,
    sum_idx=0,        # segs already summarized
    q_idx=0,          # segs already scanned for questions
    aid_t=0.0,        # lecture time the AI note helper has looked at up to
    pid=0,            # last point number
    lines=[],         # my notes: {text, t, sl}
    lesson_md="",
    docs=[],          # uploaded slides/docs: {id, name, ext, status, pages, method, error}
    view=None,        # slide on screen: {doc, page}
    slide_log=[],     # [t, doc, page] whenever the slide on screen changed
)


def new_id():
    return uuid.uuid4().hex[:10]


def load_json(path, default):
    try:
        with open(path) as f:
            value = json.load(f)
        if isinstance(default, dict):
            return {**json.loads(json.dumps(default)), **value}
        return value
    except Exception:
        return json.loads(json.dumps(default))


def save_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, ensure_ascii=False)
    os.replace(tmp, path)


def safe_name(name, limit=80):
    name = re.sub(r'[/:\\?*"<>|\n\r\t]+', "-", str(name)).strip(" .-") or "Untitled"
    return name[:limit]


def class_dir(kid):
    return os.path.join(CLASSES, kid)



REVIEW_HEAD = re.compile(r"^#{2,3}\s+(review|review questions|questions|self[- ]check|check yourself|quiz)\s*:?\s*$", re.I)
HASHTAGS_ONLY = re.compile(r"^\s*(?:\**tags\**\s*:\s*)?(?:#[A-Za-z][\w/-]*[\s,]*)+$", re.I)


def lesson_keep(texts):
    """Which lines of a lesson to keep: no tags (front matter or hashtag lines) and no question section at the end."""
    keep, n = [], len(texts)
    fm_end = -1
    if n and texts[0].strip() == "---":
        for i in range(1, min(n, 40)):
            if texts[i].strip() == "---":
                fm_end = i
                break
    skip_review = in_tags = False
    for i, x in enumerate(texts):
        if 0 < i < fm_end:
            if re.match(r"^\s*tags\s*:", x):
                in_tags = True
                continue
            if in_tags and re.match(r"^\s+-\s", x):
                continue
            in_tags = False
            keep.append(i)
            continue
        if i > fm_end:
            if REVIEW_HEAD.match(x.strip()):
                skip_review = True
                continue
            if skip_review and re.match(r"^#{1,2}\s", x):
                skip_review = False
            if skip_review:
                continue
            if HASHTAGS_ONLY.match(x) or re.match(r"^\s*\**tags\**\s*:", x, re.I):
                continue
        keep.append(i)
    # no double blank lines left behind
    out = []
    for i in keep:
        if out and not texts[i].strip() and not texts[out[-1]].strip():
            continue
        out.append(i)
    while out and not texts[out[-1]].strip():
        out.pop()
    return out


def tidy_lesson_lines(s):
    """Older lessons: take out tags and the end-of-lesson questions once."""
    f = s.get("formatted")
    if isinstance(f, dict) and not f.get("tidy"):
        texts = [str(l.get("text", "")) for l in s.get("lines", [])]
        s["lines"] = [s["lines"][i] for i in lesson_keep(texts)]
        f["tidy"] = 1
        return True
    return False

def original_notes(s):
    """The notes I wrote myself in a class: the current note, or, if it has been turned into a lesson, the note
    as it was before the first formatting."""
    if s.get("formatted"):
        for v in s.get("versions", []):
            if v.get("label") == "Before formatting":
                return [l for l in v.get("lines", []) if l.get("text", "").strip()] and v["lines"]
        return []
    return s.get("lines", []) if any(l.get("text", "").strip() for l in s.get("lines", [])) else []


def load_class(kid):
    s = load_json(os.path.join(class_dir(kid), "class.json"), EMPTY_CLASS)
    for p in s["points"]:  # older points had no id or kind
        if "id" not in p:
            s["pid"] += 1
            p["id"] = f"p{s['pid']}"
        if "kind" not in p:
            title = str(p.get("title", ""))
            p["kind"] = "deadline" if title.startswith("⚠") else "point"
            p["title"] = title.lstrip("⚠ ").strip() or title
        p.setdefault("detail", "")
        p.setdefault("rev", 0)
    if tidy_lesson_lines(s):
        save_json(os.path.join(class_dir(kid), "class.json"), s)
    return s


def save_class(kid, s):
    save_json(os.path.join(class_dir(kid), "class.json"), s)


TAG = re.compile(r"(?<![\w#&/])#([A-Za-z][\w/-]{0,30})")
MD_PREFIX = re.compile(r"^\s*(#{1,6} |> \[!\w+\]\s*|> ?|[-*+] \[[ xX]\] |[-*+] |\d+\. |[QA]: ?)")


def class_meta(s):
    """What the document list shows for a class: preview text, word count, tags, length, slides."""
    lines = [str(l.get("text", "")) for l in s.get("lines", [])]
    tags, body, fm_tags, fm_title = [], lines, [], ""
    if lines and lines[0].strip() == "---" and "---" in [x.strip() for x in lines[1:]]:
        end = 1 + [x.strip() for x in lines[1:]].index("---")
        for x in lines[1:end]:
            t = re.match(r"^\s*title:\s*(.+)$", x)
            if t:
                fm_title = t.group(1).strip().strip("'\"")[:120]
            m = re.match(r"^\s*tags:\s*\[(.*)\]", x)
            if m:
                fm_tags = [t.strip().strip("'\"").lower() for t in m.group(1).split(",") if t.strip()]
        body = lines[end + 1:]
    text, fence = [], False
    for x in body:
        if x.strip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        tags += [t.lower() for t in TAG.findall(x)]
        clean = re.sub(r"==|\*+|`|\[\[|\]\]", "", MD_PREFIX.sub("", x)).strip()
        if clean:
            text.append(clean)
    preview = " ".join(text)
    if not preview:
        ov = s.get("overall") or {}
        blocks = [b for b in s.get("blocks", []) if not b.get("quiet")]
        preview = ov.get("summary") or (blocks[0]["summary"] if blocks else "") or " ".join(x["text"] for x in s.get("segs", [])[:3])
    words = sum(len(re.findall(r"[\w'’]+", t)) for t in text)
    return {
        "preview": preview[:220],
        "words": words,
        "tags": sorted(set(tags))[:12],
        "minutes": int(round((s.get("elapsed") or 0) / 60)),
        "docs": sum(1 for d in s.get("docs", []) if d.get("status") == "ready"),
        "formatted": bool(s.get("formatted")),
        "imported": bool(s.get("imported")),
        "title": fm_title,
    }


META_KEYS = ("preview", "words", "tags", "minutes", "docs", "formatted", "title", "imported")


def class_name_for(date):
    return f"{date}-Notes"


class Library:
    """Courses (folders) holding classes. One class = one lecture, named "YYYY-MM-DD-Notes"."""

    def __init__(self, cfg):
        self.d = load_json(LIB_PATH, None) if os.path.exists(LIB_PATH) else None
        if not self.d:
            self.d = {"courses": [], "classes": [], "current": None}
            migrate_old(self, cfg)
        if not self.d["courses"]:
            self.new_course(cfg.get("course") or "General", save=False)
        for k in self.d["classes"]:  # older libraries have no card details yet
            if "words" not in k or (k.get("formatted") and k.get("metav") != 2):
                k.update(class_meta(load_class(k["id"])))
                k["metav"] = 2
        if not self.klass(self.d.get("current")):
            mine = self.classes_in(self.d["courses"][0]["id"])
            self.d["current"] = mine[0]["id"] if mine else self.new_class(self.d["courses"][0]["id"], save=False)["id"]
        self.save()

    # ---- lookups
    def save(self):
        save_json(LIB_PATH, self.d)

    def course(self, cid):
        return next((c for c in self.d["courses"] if c["id"] == cid), None)

    def klass(self, kid):
        return next((k for k in self.d["classes"] if k["id"] == kid), None)

    def classes_in(self, cid):
        mine = [k for k in self.d["classes"] if k["course"] == cid]
        return sorted(mine, key=lambda k: (k["date"], k["created"]), reverse=True)

    def order_of(self, kid):
        """1-based position of a class in its course, oldest first (used as the lesson 'order')."""
        k = self.klass(kid)
        if not k:
            return 1
        oldest_first = list(reversed(self.classes_in(k["course"])))
        return next((i + 1 for i, x in enumerate(oldest_first) if x["id"] == kid), 1)

    def _unique_class_name(self, cid, base, skip=None):
        taken = {k["name"].lower() for k in self.d["classes"] if k["course"] == cid and k["id"] != skip}
        name, n = base, 2
        while name.lower() in taken:
            name = f"{base}-{n}"
            n += 1
        return name

    def _unique_course_name(self, base, skip=None):
        taken = {c["name"].lower() for c in self.d["courses"] if c["id"] != skip}
        name, n = base, 2
        while name.lower() in taken:
            name = f"{base} {n}"
            n += 1
        return name

    # ---- courses
    def new_course(self, name, save=True):
        name = safe_name(name or "New course", 60)
        c = {"id": new_id(), "name": self._unique_course_name(name), "collapsed": False, "words": [], "icon": "", "color": ""}
        self.d["courses"].append(c)
        if save:
            self.save()
        return c

    def rename_course(self, cid, name):
        c = self.course(cid)
        if not c:
            raise ValueError("That course no longer exists.")
        c["name"] = self._unique_course_name(safe_name(name, 60), skip=cid)
        self.save()
        return c

    def delete_course(self, cid):
        if self.classes_in(cid):
            raise ValueError("Move or delete the classes in this course first.")
        if len(self.d["courses"]) == 1:
            raise ValueError("You need at least one course.")
        self.d["courses"] = [c for c in self.d["courses"] if c["id"] != cid]
        self.save()

    def set_collapsed(self, cid, collapsed):
        c = self.course(cid)
        if c:
            c["collapsed"] = bool(collapsed)
            self.save()

    def set_style(self, cid, icon=None, color=None):
        c = self.course(cid)
        if not c:
            raise ValueError("That course no longer exists.")
        if icon is not None:
            c["icon"] = re.sub(r"[^a-z]", "", str(icon))[:20]
        if color is not None:
            c["color"] = re.sub(r"[^a-z]", "", str(color))[:20]
        self.save()

    def set_info(self, cid, taught=None, context=None):
        """What kind of course it is: the language studied ("he" for a Hebrew class taught in English, "" for none)
        and a few lines of context for the AI (textbook, level, what the class covers)."""
        c = self.course(cid)
        if not c:
            raise ValueError("That course no longer exists.")
        if taught is not None:
            c["taught"] = str(taught) if str(taught) in ("he", "") else ""
        if context is not None:
            c["context"] = " ".join(str(context).split())[:600]
        self.save()

    def learn_word(self, cid, word):
        c = self.course(cid)
        if c and word and word.lower() not in {w.lower() for w in c.setdefault("words", [])}:
            c["words"].append(word)
            self.save()

    # ---- classes
    def new_class(self, cid, date=None, save=True):
        if not self.course(cid):
            cid = self.d["courses"][0]["id"]
        date = date or datetime.date.today().isoformat()
        k = {
            "id": new_id(),
            "course": cid,
            "date": date,
            "name": self._unique_class_name(cid, class_name_for(date)),
            "created": time.time(),
        }
        self.d["classes"].append(k)
        save_class(k["id"], json.loads(json.dumps(EMPTY_CLASS)))
        if save:
            self.save()
        return k

    def set_meta(self, kid, meta):
        k = self.klass(kid)
        if not k:
            return False
        changed = any(k.get(x) != meta.get(x) for x in META_KEYS)
        if changed:
            k.update({x: meta[x] for x in META_KEYS})
            k["updated"] = time.time()
            self.save()
        return changed

    def rename_class(self, kid, name):
        k = self.klass(kid)
        if not k:
            raise ValueError("That class no longer exists.")
        k["name"] = self._unique_class_name(k["course"], safe_name(name, 80), skip=kid)
        self.save()
        return k

    def move_class(self, kid, cid):
        k, c = self.klass(kid), self.course(cid)
        if not k or not c:
            raise ValueError("That class or course no longer exists.")
        if k["course"] != cid:
            k["course"] = cid
            k["name"] = self._unique_class_name(cid, k["name"], skip=kid)
            c["collapsed"] = False
            self.save()
        return k

    def delete_class(self, kid):
        """Moves the class folder to the app's trash folder rather than erasing it."""
        k = self.klass(kid)
        if not k:
            return
        self.d["classes"] = [x for x in self.d["classes"] if x["id"] != kid]
        src = class_dir(kid)
        if os.path.exists(src):
            os.makedirs(TRASH, exist_ok=True)
            shutil.move(src, os.path.join(TRASH, f"{safe_name(k['name'])}-{kid}"))
        self.save()

    def public(self):
        return {
            "current": self.d["current"],
            "courses": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "collapsed": c.get("collapsed", False),
                    "words": c.get("words", []),
                    "icon": c.get("icon", ""),
                    "color": c.get("color", ""),
                    **({"taught": c["taught"]} if "taught" in c else {}),
                    "context": c.get("context", ""),
                    "classes": [{"id": k["id"], "name": k["name"], "date": k["date"], "created": k.get("created", 0),
                                 "updated": k.get("updated", k.get("created", 0)), **{x: k.get(x) for x in META_KEYS}}
                                for k in self.classes_in(c["id"])],
                }
                for c in self.d["courses"]
            ],
        }


def migrate_old(lib, cfg):
    """Bring over settings, the speech model and the last lecture from DenSem Lecture / Lecture Catch-Up."""
    for old in OLD_DATA:
        if not os.path.isdir(old):
            continue
        if not os.path.exists(CFG_PATH) and os.path.exists(os.path.join(old, "config.json")):
            os.makedirs(DATA, exist_ok=True)
            shutil.copy(os.path.join(old, "config.json"), CFG_PATH)
            cfg.update(load_json(CFG_PATH, {}))
        if not os.path.isdir(MODELS) and os.path.isdir(os.path.join(old, "models")):
            os.makedirs(DATA, exist_ok=True)
            shutil.move(os.path.join(old, "models"), MODELS)
    for folder in [DATA] + OLD_DATA:
        path = os.path.join(folder, "session.json")
        if not os.path.exists(path):
            continue
        old = load_json(path, {})
        if not old.get("segs") and not str(old.get("notes", "")).strip():
            continue
        course = lib.new_course(cfg.get("course") or "General", save=False)
        date = datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat()
        k = lib.new_class(course["id"], date, save=False)
        s = json.loads(json.dumps(EMPTY_CLASS))
        for key in ("segs", "points", "topic", "elapsed", "sum_idx", "lesson_md"):
            if key in old:
                s[key] = old[key]
        s["q_idx"] = len(s["segs"])
        lines = old.get("note_lines") or [{"text": x, "t": None} for x in str(old.get("notes", "")).split("\n")]
        s["lines"] = [{"text": str(l.get("text", "")), "t": l.get("t"), "sl": None} for l in lines]
        save_class(k["id"], s)
        s = load_class(k["id"])  # gives old points ids
        save_class(k["id"], s)
        lib.d["current"] = k["id"]
        os.replace(path, path + ".migrated")
        break


def course_taught(c):
    """'he' for a Hebrew class taught in English. Until the student says, a course named Hebrew counts as one
    (but not the letter to the Hebrews)."""
    if not c:
        return ""
    if "taught" in c:
        return c["taught"]
    return "he" if re.search(r"\bhebrew\b(?!s)", c.get("name", ""), re.I) and not re.search(r"\bhebrews\b", c.get("name", ""), re.I) else ""

HEBREW_HINT = ("aleph, bet, gimel, dalet, he, vav, zayin, chet, tet, yod, kaf, lamed, mem, nun, samekh, ayin, pe, tsade, qof, "
               "resh, shin, tav, niqqud, qamets, patach, tsere, segol, hireq, holem, qibbuts, shureq, shewa, dagesh, mappiq, "
               "maqqef, Qal, Niphal, Piel, Pual, Hiphil, Hophal, Hithpael, wayyiqtol, qatal, yiqtol, Elohim, Adonai, YHWH, Torah")
HEBREW_TERMS = [w.strip() for w in HEBREW_HINT.split(",")] + [
    "begadkephat", "shva", "sheva", "hatef", "metheg", "sof", "pasuq", "athnach", "silluq", "niphal", "piel", "pual",
    "hiphil", "hophal", "hithpael", "qatal", "yiqtol", "weqatal", "wayyiqtol", "vayyiqtol", "cohortative", "jussive",
    "infinitive", "construct", "absolute", "segholate", "guttural", "gutturals", "furtive", "aleph", "ayin", "qof",
    "tsade", "samekh", "chet", "yod", "qamets", "qamats", "patach", "pathach", "tsere", "segol", "hireq", "hiriq",
    "holem", "cholem", "qibbuts", "shureq", "shuruq", "dagesh", "mappiq", "maqqef", "niqqud", "nikkud", "shalom",
    "elohim", "adonai", "torah", "bereshit", "hashamayim", "haaretz", "masoretic", "tanakh", "ketiv", "qere"]
