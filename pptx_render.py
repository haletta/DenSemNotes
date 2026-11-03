"""Draw PowerPoint slides as pictures without Keynote or LibreOffice.

Used when Keynote can't convert a deck (for example when macOS hasn't allowed DenSem Class to use Keynote).
It reads the slide layout itself and draws, in order: the background, the master and layout artwork, then the
slide's shapes: pictures, filled shapes and lines, text with its sizes/colours/bullets, groups, tables,
simple charts and SmartArt (from the drawing PowerPoint stores with it). Animations, 3-D effects and
unusual fonts are approximated.
"""
import io
import math
import re

from lxml import etree

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dsp": "http://schemas.microsoft.com/office/drawing/2008/diagram",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
}
R_EMBED = "{%s}embed" % NS["r"]
R_ID = "{%s}id" % NS["r"]
RT_THEME = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"
RT_DIAGRAM_DRAWING = "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing"
EMU_PT = 12700
DEFAULT_THEME = {"dk1": (0, 0, 0), "lt1": (255, 255, 255), "dk2": (68, 84, 106), "lt2": (231, 230, 230),
                 "accent1": (68, 114, 196), "accent2": (237, 125, 49), "accent3": (165, 165, 165),
                 "accent4": (255, 192, 0), "accent5": (91, 155, 213), "accent6": (112, 173, 71),
                 "hlink": (5, 99, 193), "folHlink": (149, 79, 114)}
SCHEME_ALIAS = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}
PRESET = {"black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255),
          "yellow": (255, 255, 0), "gray": (128, 128, 128), "grey": (128, 128, 128), "orange": (255, 165, 0)}


def q(el, path):
    return el.find(path, NS) if el is not None else None


def qa(el, path):
    return el.findall(path, NS) if el is not None else []


def _hex(v):
    v = (v or "000000")[-6:]
    try:
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def luminance(c):
    return (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255


# ---------------------------------------------------------------- colours
class Ctx:
    """What a deck needs while drawing: theme colours, scale, the fonts helper."""

    def __init__(self, prs, width, font):
        self.prs = prs
        self.sc = width / prs.slide_width
        self.W = width
        self.H = round(prs.slide_height * self.sc)
        self.font = font
        self.theme = dict(DEFAULT_THEME)
        try:
            master = prs.slide_masters[0]
            tp = master.part.part_related_by(RT_THEME)
            root = etree.fromstring(tp.blob)
            for el in qa(q(root, ".//a:clrScheme"), "*"):
                name = etree.QName(el).localname
                c = q(el, "a:srgbClr")
                s = q(el, "a:sysClr")
                if c is not None:
                    self.theme[name] = _hex(c.get("val"))
                elif s is not None:
                    self.theme[name] = _hex(s.get("lastClr"))
        except Exception:
            pass

    def px(self, emu):
        return (emu or 0) * self.sc

    def color(self, holder, ph=None):
        """Colour of a fill/colour holder element (solidFill, a colour child, fillRef…)."""
        if holder is None:
            return None
        for el in holder.iter():
            tag = etree.QName(el).localname
            base = None
            if tag == "srgbClr":
                base = _hex(el.get("val"))
            elif tag == "schemeClr":
                v = el.get("val")
                if v == "phClr":
                    base = ph
                else:
                    base = self.theme.get(SCHEME_ALIAS.get(v, v))
            elif tag == "sysClr":
                base = _hex(el.get("lastClr") or "000000")
            elif tag == "prstClr":
                base = PRESET.get(el.get("val"), (0, 0, 0))
            elif tag == "scrgbClr":
                base = tuple(int(int(el.get(k, "0")) / 100000 * 255) for k in ("r", "g", "b"))
            if base is None:
                continue
            return self._mods(base, el)
        return None

    @staticmethod
    def _mods(c, el):
        r, g, b = [x / 255 for x in c]
        for m in el:
            tag = etree.QName(m).localname
            val = int(m.get("val", "100000")) / 100000
            if tag == "lumMod":
                r, g, b = r * val, g * val, b * val
            elif tag == "lumOff":
                r, g, b = r + val, g + val, b + val
            elif tag == "tint":
                r, g, b = [x + (1 - x) * (1 - val) for x in (r, g, b)]
            elif tag == "shade":
                r, g, b = r * val, g * val, b * val
        return tuple(max(0, min(255, round(x * 255))) for x in (r, g, b))

    def alpha(self, holder):
        a = holder.find(".//a:alpha", NS) if holder is not None else None
        return int(a.get("val")) / 100000 if a is not None else 1.0


# ---------------------------------------------------------------- pictures
def image_from(part, rid):
    from PIL import Image

    try:
        blob = part.related_part(rid).blob
        im = Image.open(io.BytesIO(blob))
        im.load()
        return im.convert("RGBA")
    except Exception:
        return None


def paste_image(canvas, im, box, crop=None, rot=0, mask_shape=None, alpha=1.0):
    from PIL import Image, ImageDraw

    x, y, w, h = box
    if w < 1 or h < 1 or im is None:
        return
    if crop:
        l, t, r, b = crop
        W0, H0 = im.size
        left, top = W0 * l, H0 * t
        right, bottom = W0 * (1 - r), H0 * (1 - b)
        if right - left > 1 and bottom - top > 1:
            im = im.crop((round(left), round(top), round(right), round(bottom)))
    im = im.resize((max(1, round(w)), max(1, round(h))), Image.LANCZOS)
    if mask_shape in ("ellipse", "roundRect"):
        m = Image.new("L", im.size, 0)
        d = ImageDraw.Draw(m)
        if mask_shape == "ellipse":
            d.ellipse([0, 0, im.size[0] - 1, im.size[1] - 1], fill=255)
        else:
            d.rounded_rectangle([0, 0, im.size[0] - 1, im.size[1] - 1], radius=min(im.size) * 0.12, fill=255)
        a = im.getchannel("A")
        from PIL import ImageChops
        im.putalpha(ImageChops.multiply(a, m))
    if alpha < 1:
        a = im.getchannel("A").point(lambda v: int(v * alpha))
        im.putalpha(a)
    if rot:
        im = im.rotate(-rot, expand=True, resample=Image.BICUBIC)
        x += (w - im.size[0]) / 2
        y += (h - im.size[1]) / 2
    canvas.paste(im, (round(x), round(y)), im)


def _crop(blipfill):
    sr = q(blipfill, "a:srcRect")
    if sr is None:
        return None
    return tuple(int(sr.get(k, "0")) / 100000 for k in ("l", "t", "r", "b"))


# ---------------------------------------------------------------- geometry
def shape_points(prst, x, y, w, h):
    """Polygon for a few common preset shapes; None means draw as a rectangle/ellipse."""
    if prst == "triangle":
        return [(x + w / 2, y), (x + w, y + h), (x, y + h)]
    if prst == "rtTriangle":
        return [(x, y), (x + w, y + h), (x, y + h)]
    if prst == "diamond":
        return [(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)]
    if prst in ("rightArrow", "notchedRightArrow"):
        hw = min(w * 0.4, h)
        return [(x, y + h * .25), (x + w - hw, y + h * .25), (x + w - hw, y), (x + w, y + h / 2), (x + w - hw, y + h),
                (x + w - hw, y + h * .75), (x, y + h * .75)]
    if prst == "leftArrow":
        hw = min(w * 0.4, h)
        return [(x + w, y + h * .25), (x + hw, y + h * .25), (x + hw, y), (x, y + h / 2), (x + hw, y + h),
                (x + hw, y + h * .75), (x + w, y + h * .75)]
    if prst in ("chevron", "homePlate"):
        d = min(w * 0.35, h / 2)
        pts = [(x, y), (x + w - d, y), (x + w, y + h / 2), (x + w - d, y + h), (x, y + h)]
        if prst == "chevron":
            pts.append((x + d, y + h / 2))
        return pts
    if prst == "pentagon":
        return [(x + w / 2, y), (x + w, y + h * .38), (x + w * .81, y + h), (x + w * .19, y + h), (x, y + h * .38)]
    if prst == "hexagon":
        d = w * 0.25
        return [(x + d, y), (x + w - d, y), (x + w, y + h / 2), (x + w - d, y + h), (x + d, y + h), (x, y + h / 2)]
    if prst == "parallelogram":
        d = w * 0.25
        return [(x + d, y), (x + w, y), (x + w - d, y + h), (x, y + h)]
    if prst in ("star5", "star4", "star6"):
        n = int(prst[-1])
        pts = []
        for i in range(n * 2):
            ang = -math.pi / 2 + i * math.pi / n
            rr = 0.5 if i % 2 == 0 else 0.22
            pts.append((x + w / 2 + math.cos(ang) * w * rr * 2 / 2 * 1, y + h / 2 + math.sin(ang) * h * rr * 2 / 2 * 1))
        return pts
    return None


def text_rect(prst, box):
    """The part of a shape PowerPoint puts text in (inside notches, points and curves)."""
    x, y, w, h = box
    if prst == "chevron":
        d = min(w * 0.35, h / 2)
        return (x + d, y, w - 2 * d, h)
    if prst in ("homePlate", "rightArrow", "notchedRightArrow"):
        d = min(w * 0.35, h / 2) if prst == "homePlate" else min(w * 0.4, h)
        return (x, y + (h * .25 if prst != "homePlate" else 0), w - d, h * (.5 if prst != "homePlate" else 1))
    if prst == "leftArrow":
        d = min(w * 0.4, h)
        return (x + d, y + h * .25, w - d, h * .5)
    if prst in ("ellipse", "flowChartConnector"):
        return (x + w * .146, y + h * .146, w * .708, h * .708)
    if prst in ("diamond",):
        return (x + w / 4, y + h / 4, w / 2, h / 2)
    if prst == "triangle":
        return (x + w / 4, y + h / 2, w / 2, h / 2)
    return box


# ---------------------------------------------------------------- text
TITLE_TYPES = ("title", "ctrTitle")


class Styles:
    """Text style chain for a shape: its own list style, the layout and master placeholder, the master text styles."""

    def __init__(self, ctx, slide, sp):
        self.ctx = ctx
        self.chain = []  # lstStyle elements, nearest first
        self.anchor = None
        self.ph_type = None
        ph = sp.find("./p:nvSpPr/p:nvPr/p:ph", NS) if sp is not None else None
        if sp is not None:
            self._add(sp)
        if ph is not None and slide is not None:
            self.ph_type = ph.get("type", "body" if ph.get("idx") else "obj")
            layout = slide.slide_layout
            for holder in (layout, layout.slide_master):
                el = self._find_ph(holder, ph)
                if el is not None:
                    self._add(el)
            master = layout.slide_master._element
            key = "p:titleStyle" if self.ph_type in TITLE_TYPES else "p:bodyStyle"
            ts = master.find("./p:txStyles/" + key, NS)
            if ts is not None:
                self.chain.append(ts)
        elif slide is not None:
            ts = slide.slide_layout.slide_master._element.find("./p:txStyles/p:otherStyle", NS)
            if ts is not None:
                self.chain.append(ts)

    def _add(self, sp):
        tb = sp.find("./p:txBody", NS)
        if tb is None:
            tb = sp.find("./dsp:txBody", NS)
        if tb is not None:
            ls = tb.find("./a:lstStyle", NS)
            if ls is not None:
                self.chain.append(ls)
            bp = tb.find("./a:bodyPr", NS)
            if bp is not None and self.anchor is None and bp.get("anchor"):
                self.anchor = bp.get("anchor")

    @staticmethod
    def _find_ph(holder, ph):
        typ, idx = ph.get("type"), ph.get("idx")
        best = None
        for sp in holder._element.iterfind(".//p:sp", NS):
            p2 = sp.find("./p:nvSpPr/p:nvPr/p:ph", NS)
            if p2 is None:
                continue
            if idx is not None and p2.get("idx") == idx:
                return sp
            t2 = p2.get("type", "body" if p2.get("idx") else "obj")
            if typ and t2 == typ:
                best = best if best is not None else sp
            elif not typ and t2 in ("body", "obj") and best is None:
                best = sp
        if best is None and typ in ("ctrTitle",):
            return Styles._find_ph(holder, etree.fromstring('<p:ph xmlns:p="%s" type="title"/>' % NS["p"]))
        if best is None and typ in ("subTitle", "obj", None):
            return Styles._find_ph(holder, etree.fromstring('<p:ph xmlns:p="%s" type="body"/>' % NS["p"])) if typ else None
        return best

    def lvl(self, level, what):
        """First value found for a paragraph level: what = 'sz' | 'algn' | 'b' | 'color' | 'bullet' | 'indent'."""
        tag = "a:lvl%dpPr" % (level + 1)
        for ls in self.chain:
            p = ls.find(tag, NS)
            if p is None:
                continue
            d = p.find("a:defRPr", NS)
            if what == "sz" and d is not None and d.get("sz"):
                return int(d.get("sz")) / 100
            if what == "b" and d is not None and d.get("b") is not None:
                return d.get("b") in ("1", "true")
            if what == "algn" and p.get("algn"):
                return p.get("algn")
            if what == "color" and d is not None:
                c = self.ctx.color(d.find("a:solidFill", NS))
                if c:
                    return c
            if what == "bullet":
                if p.find("a:buNone", NS) is not None:
                    return ""
                bc = p.find("a:buChar", NS)
                if bc is not None:
                    return bc.get("char", "•")
                if p.find("a:buAutoNum", NS) is not None:
                    return "#"
            if what == "indent" and p.get("marL"):
                return int(p.get("marL"))
        return None


def text_runs(txbody):
    """[(paragraph pPr, level, [(text, rPr)])]"""
    out = []
    for p in qa(txbody, "a:p"):
        ppr = q(p, "a:pPr")
        lvl = int(ppr.get("lvl", "0")) if ppr is not None else 0
        runs = []
        for el in p:
            tag = etree.QName(el).localname
            if tag in ("r", "fld"):
                t = q(el, "a:t")
                runs.append(((t.text or "") if t is not None else "", q(el, "a:rPr")))
            elif tag == "br":
                runs.append(("\n", q(el, "a:rPr")))
        out.append((ppr, lvl, runs, q(p, "a:endParaRPr")))
    return out


def draw_text(img, ctx, slide, sp, txbody, box, fill_rgb, bg_rgb, default_anchor=None, table_cell=False, font_col=None):
    from PIL import ImageDraw

    if txbody is None:
        return
    paras = text_runs(txbody)
    if not any(r[0].strip() for _, _, runs, _ in paras for r in runs):
        return
    st = Styles(ctx, slide, sp) if not table_cell else Styles(ctx, None, None)
    bp = q(txbody, "a:bodyPr")
    x, y, w, h = box
    ins = [91440, 45720, 91440, 45720]
    if bp is not None:
        for i, k in enumerate(("lIns", "tIns", "rIns", "bIns")):
            if bp.get(k) is not None:
                ins[i] = int(bp.get(k))
    x += ctx.px(ins[0])
    y += ctx.px(ins[1])
    w -= ctx.px(ins[0] + ins[2])
    h -= ctx.px(ins[1] + ins[3])
    if w < 4:
        return
    scale = 1.0
    na = bp.find("a:normAutofit", NS) if bp is not None else None
    if na is not None and na.get("fontScale"):
        scale = int(na.get("fontScale")) / 100000
    anchor = (bp.get("anchor") if bp is not None else None) or st.anchor or default_anchor or (
        "ctr" if st.ph_type in TITLE_TYPES else "t")
    behind = fill_rgb or bg_rgb
    auto_col = (255, 255, 255) if luminance(behind) < 0.5 else ctx.theme["dk1"]
    if luminance(ctx.theme["dk1"]) > 0.5 and luminance(behind) > 0.5:
        auto_col = (20, 20, 20)
    is_body = st.ph_type in ("body", "obj", None) and not table_cell and st.ph_type is not None
    numbers = {}

    def layout(f):
        lines = []  # (pieces [(text, font, colour)], height, align, indent, bullet)
        d = ImageDraw.Draw(img)
        for ppr, lvl, runs, endr in paras:
            text_all = "".join(t for t, _ in runs)
            dsz = st.lvl(lvl, "sz") or (40 if st.ph_type in TITLE_TYPES else 24 if is_body and lvl == 0 else
                                       20 if is_body else 14 if table_cell else 18)
            if not text_all.strip():
                sz = (int(endr.get("sz")) / 100) if endr is not None and endr.get("sz") else dsz
                lines.append(([], sz * f * scale * 1.2 * ctx.sc * EMU_PT, "l", 0, ""))
                continue
            algn = (ppr.get("algn") if ppr is not None else None) or st.lvl(lvl, "algn") or (
                "ctr" if st.ph_type in ("ctrTitle", "subTitle") else "l")
            bullet = ""
            if ppr is not None and q(ppr, "a:buNone") is not None:
                bullet = ""
            elif ppr is not None and q(ppr, "a:buChar") is not None:
                bullet = q(ppr, "a:buChar").get("char", "•")
            elif ppr is not None and q(ppr, "a:buAutoNum") is not None:
                bullet = "#"
            else:
                b = st.lvl(lvl, "bullet")
                bullet = b if b is not None else ""
            if bullet == "#":
                numbers[lvl] = numbers.get(lvl, 0) + 1
                bullet = f"{numbers[lvl]}."
            if bullet in ("§", "Ø", "ü", "q", "n", "Ä"):  # Wingdings bullets
                bullet = "•"
            words = []  # (word, font, colour, size)
            for t, rpr in runs:
                sz = (int(rpr.get("sz")) / 100) if rpr is not None and rpr.get("sz") else dsz
                bold = (rpr.get("b") in ("1", "true")) if rpr is not None and rpr.get("b") is not None else bool(st.lvl(lvl, "b"))
                col = ctx.color(q(rpr, "a:solidFill")) if rpr is not None else None
                col = col or (font_col if font_col and st.ph_type is None else None) or st.lvl(lvl, "color") or font_col or auto_col
                if fill_rgb is None and luminance(col) < 0.35 and luminance(bg_rgb) < 0.35:
                    col = (240, 240, 240)
                px = max(6, round(sz * f * scale * EMU_PT * ctx.sc))
                fnt = ctx.font(px, bold)
                parts = re.split(r"(\s+|\n)", t)
                for part in parts:
                    if part:
                        words.append((part, fnt, col, px))
            indent = ctx.px(st.lvl(lvl, "indent") or (342900 * lvl if is_body else 0)) if not table_cell else 0
            bw = 0
            if bullet:
                bfont = words[0][1] if words else ctx.font(20)
                bw = d.textlength(bullet + " ", font=bfont) if words else 0
            avail = max(10, w - indent - bw)
            cur, curw, maxh = [], 0, 0
            first = True
            for word, fnt, col, px in words:
                if word == "\n":
                    lines.append((cur, max(maxh, px) * 1.2, algn, indent, bullet if first else "", bw))
                    cur, curw, maxh, first = [], 0, 0, False
                    continue
                ww = d.textlength(word, font=fnt)
                if curw + ww > avail and cur and word.strip():
                    lines.append((cur, max(maxh, px) * 1.2, algn, indent, bullet if first else "", bw))
                    cur, curw, maxh, first = [], 0, 0, False
                if not cur and not word.strip():
                    continue
                cur.append((word, fnt, col))
                curw += ww
                maxh = max(maxh, px)
            if cur:
                lines.append((cur, maxh * 1.2, algn, indent, bullet if first else "", bw))
            if lines:
                last = lines[-1]
                lines[-1] = last[:1] + (last[1] + (maxh * 0.25 if is_body else 0),) + last[2:]
        return lines

    f = 1.0
    lines = layout(f)
    total = sum(l[1] for l in lines)
    while total > h * 1.04 and f > 0.45:
        f -= 0.08
        lines = layout(f)
        total = sum(l[1] for l in lines)
    yy = y + (h - total) / 2 if anchor == "ctr" else (y + h - total if anchor == "b" else y)
    yy = max(yy, y - 2) if anchor != "b" else yy
    d = ImageDraw.Draw(img)
    for item in lines:
        pieces, lh, algn, indent, bullet = item[:5]
        bw = item[5] if len(item) > 5 else 0
        if not pieces:
            yy += lh
            continue
        width = sum(d.textlength(t, font=fn) for t, fn, _ in pieces)
        xx = x + indent + bw
        if algn == "ctr":
            xx = x + indent + (w - indent - width) / 2
        elif algn == "r":
            xx = x + w - width
        base = yy + lh / 1.2 * 0.95
        if bullet:
            fn0, c0 = pieces[0][1], pieces[0][2]
            d.text((xx - bw, base), bullet, font=fn0, fill=c0, anchor="ls")
        for t, fn, col in pieces:
            d.text((xx, base), t, font=fn, fill=col, anchor="ls")
            xx += d.textlength(t, font=fn)
        yy += lh


# ---------------------------------------------------------------- shapes
class Drawer:
    def __init__(self, ctx, img, slide, part, bg_rgb):
        self.ctx, self.img, self.slide, self.part, self.bg = ctx, img, slide, part, bg_rgb
        from PIL import ImageDraw
        self.d = ImageDraw.Draw(img)

    def xfrm_box(self, el, tf):
        xf = el.find("./p:spPr/a:xfrm", NS)
        if xf is None:
            xf = el.find("./dsp:spPr/a:xfrm", NS)
        if xf is None:
            xf = el.find("./p:xfrm", NS)
        if xf is None:
            xf = el.find("./p:grpSpPr/a:xfrm", NS)
        if xf is None:
            return None, 0, False, False
        off, ext = q(xf, "a:off"), q(xf, "a:ext")
        if off is None or ext is None:
            return None, 0, False, False
        x, y = tf(int(off.get("x")), int(off.get("y")))
        x2, y2 = tf(int(off.get("x")) + int(ext.get("cx")), int(off.get("y")) + int(ext.get("cy")))
        rot = int(xf.get("rot", "0")) / 60000
        return (x, y, x2 - x, y2 - y), rot, xf.get("flipH") == "1", xf.get("flipV") == "1"

    def base_tf(self):
        sc = self.ctx.sc
        return lambda X, Y: (X * sc, Y * sc)

    def draw_tree(self, parent, tf, shape_objs=None):
        """Draw the children of an spTree / grpSp in order."""
        objs = {}
        if shape_objs is not None:
            for s in shape_objs:
                objs[id(s._element)] = s
        for el in parent:
            tag = etree.QName(el).localname
            try:
                if tag == "sp":
                    self.sp(el, tf, objs.get(id(el)))
                elif tag == "pic":
                    self.pic(el, tf, objs.get(id(el)))
                elif tag == "grpSp":
                    self.group(el, tf)
                elif tag == "cxnSp":
                    self.line(el, tf)
                elif tag == "graphicFrame":
                    self.frame(el, tf, objs.get(id(el)))
                elif tag == "AlternateContent":
                    fb = el.find("{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback")
                    if fb is not None:
                        self.draw_tree(fb, tf)
            except Exception:
                continue

    def ph_box(self, el, tf):
        ph = el.find("./p:nvSpPr/p:nvPr/p:ph", NS)
        if ph is None:
            ph = el.find("./p:nvPicPr/p:nvPr/p:ph", NS)
        if ph is None or self.slide is None:
            return None
        layout = self.slide.slide_layout
        for holder in (layout, layout.slide_master):
            src = Styles._find_ph(holder, ph)
            if src is not None:
                b, _, _, _ = self.xfrm_box(src, tf)
                if b is not None and b[2] > 0 and b[3] > 0:
                    return b
        return None

    def box_of(self, el, tf, obj):
        box, rot, fh, fv = self.xfrm_box(el, tf)
        own = el.find("./p:spPr/a:xfrm", NS)
        if own is None:
            own = el.find("./p:spPr/a:xfrm", NS)
        if box is None or box[2] <= 0 or box[3] <= 0:
            inh = self.ph_box(el, tf)
            if inh is not None:
                if own is not None and q(own, "a:ext") is not None and box is None:
                    ext = q(own, "a:ext")
                    w, h = self.ctx.px(int(ext.get("cx", "0"))), self.ctx.px(int(ext.get("cy", "0")))
                    box = (inh[0], inh[1], w or inh[2], h or inh[3])
                elif box is not None:
                    box = (box[0], box[1], box[2] or inh[2], box[3] or inh[3])
                else:
                    box = inh
                return box, rot, fh, fv
        if box is None and obj is not None:
            try:
                if obj.left is not None and obj.width:
                    box = (self.ctx.px(obj.left), self.ctx.px(obj.top), self.ctx.px(obj.width), self.ctx.px(obj.height))
                    rot = getattr(obj, "rotation", 0) or 0
            except Exception:
                box = None
        return box, rot, fh, fv

    def group(self, el, tf):
        xf = el.find("./p:grpSpPr/a:xfrm", NS)
        if xf is None:
            return self.draw_tree(el, tf)
        off, ext, choff, chext = (q(xf, k) for k in ("a:off", "a:ext", "a:chOff", "a:chExt"))
        if None in (off, ext, choff, chext):
            return self.draw_tree(el, tf)
        ox, oy, cx, cy = int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy"))
        chx, chy, chcx, chcy = int(choff.get("x")), int(choff.get("y")), int(chext.get("cx")) or 1, int(chext.get("cy")) or 1

        def inner(X, Y):
            return tf(ox + (X - chx) * cx / chcx, oy + (Y - chy) * cy / chcy)

        self.draw_tree(el, inner)

    def fill_of(self, sppr, style):
        """('none'|'solid'|'image'|None, colour or (part rid, crop), alpha)"""
        if sppr is not None:
            if q(sppr, "a:noFill") is not None:
                return "none", None, 1
            sf = q(sppr, "a:solidFill")
            if sf is not None:
                return "solid", self.ctx.color(sf), self.ctx.alpha(sf)
            gf = q(sppr, "a:gradFill")
            if gf is not None:
                stops = qa(gf, "a:gsLst/a:gs")
                cols = [self.ctx.color(s) for s in stops if self.ctx.color(s)]
                if cols:
                    mid = tuple(sum(c[i] for c in cols) // len(cols) for i in range(3))
                    return "solid", mid, 1
            bf = q(sppr, "a:blipFill")
            if bf is not None:
                blip = q(bf, "a:blip")
                if blip is not None and blip.get(R_EMBED):
                    return "image", (blip.get(R_EMBED), _crop(bf)), 1
        if style is not None:
            fr = q(style, "a:fillRef")
            if fr is not None and fr.get("idx") not in ("0", None):
                return "solid", self.ctx.color(fr), 1
        return None, None, 1

    def line_of(self, sppr, style):
        ln = q(sppr, "a:ln") if sppr is not None else None
        if ln is not None:
            if q(ln, "a:noFill") is not None:
                return None, 0
            col = self.ctx.color(q(ln, "a:solidFill"))
            wid = max(1, self.ctx.px(int(ln.get("w", "12700"))))
            if col:
                return col, wid
        if style is not None:
            lr = q(style, "a:lnRef")
            if lr is not None and lr.get("idx") not in ("0", None):
                col = self.ctx.color(lr)
                if col:
                    w = 12700 * (1 + int(lr.get("idx", "1")) // 2)
                    return col, max(1, self.ctx.px(w))
        return None, 0

    def shape_fill(self, box, prst, kind, val, alpha, line, rot):
        from PIL import Image, ImageDraw

        x, y, w, h = box
        if w < 1 and h < 1:
            return
        layer = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        col = (val + (round(255 * alpha),)) if kind == "solid" and val else None
        lc, lw = line
        lcol = (lc + (255,)) if lc else None
        pts = shape_points(prst, x, y, w, h)
        rect = [x, y, x + max(w, 1), y + max(h, 1)]
        if pts:
            if rot:
                cx, cy = x + w / 2, y + h / 2
                a = math.radians(rot)
                pts = [(cx + (px - cx) * math.cos(a) - (py - cy) * math.sin(a), cy + (px - cx) * math.sin(a) + (py - cy) * math.cos(a)) for px, py in pts]
            d.polygon(pts, fill=col, outline=lcol, width=round(lw) if lcol else 0)
        elif prst in ("ellipse", "flowChartConnector"):
            d.ellipse(rect, fill=col, outline=lcol, width=round(lw) if lcol else 0)
        elif prst in ("roundRect", "round2SameRect", "flowChartAlternateProcess", "snipRoundRect"):
            d.rounded_rectangle(rect, radius=min(w, h) * 0.16, fill=col, outline=lcol, width=round(lw) if lcol else 0)
        else:
            if rot and abs(rot) % 180 not in (0,):
                cx, cy = x + w / 2, y + h / 2
                a = math.radians(rot)
                corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
                corners = [(cx + (px - cx) * math.cos(a) - (py - cy) * math.sin(a), cy + (px - cx) * math.sin(a) + (py - cy) * math.cos(a)) for px, py in corners]
                d.polygon(corners, fill=col, outline=lcol, width=round(lw) if lcol else 0)
            else:
                d.rectangle(rect, fill=col, outline=lcol, width=round(lw) if lcol else 0)
        self.img.alpha_composite(layer)

    def sp(self, el, tf, obj, part=None):
        part = part or self.part
        box, rot, fh, fv = self.box_of(el, tf, obj)
        if box is None:
            return
        sppr = el.find("./p:spPr", NS)
        if sppr is None:
            sppr = el.find("./dsp:spPr", NS)
        style = el.find("./p:style", NS)
        if style is None:
            style = el.find("./dsp:style", NS)
        geom = q(sppr, "a:prstGeom")
        prst = geom.get("prst") if geom is not None else ("rect" if q(sppr, "a:custGeom") is not None else "rect")
        kind, val, alpha = self.fill_of(sppr, style)
        line = self.line_of(sppr, style)
        is_ph = el.find("./p:nvSpPr/p:nvPr/p:ph", NS) is not None
        if prst in ("line", "straightConnector1") or ((box[3] == 0 or box[2] == 0) and not is_ph):
            col = line[0] or (val if kind == "solid" else None)
            if col:
                self.d.line([(box[0], box[1]), (box[0] + box[2], box[1] + box[3])], fill=col, width=max(1, round(line[1] or 2)))
            return
        if kind == "image":
            rid, crop = val
            paste_image(self.img, image_from(part, rid), box, crop, rot, prst)
            if line[0]:
                self.shape_fill(box, prst, None, None, 1, line, rot)
        elif kind == "solid" or line[0]:
            self.shape_fill(box, prst, kind, val, alpha, line, rot)
        txb = el.find("./p:txBody", NS)
        if txb is None:
            txb = el.find("./dsp:txBody", NS)
            tb_box = el.find("./dsp:txXfrm", NS)
            if tb_box is not None and q(tb_box, "a:off") is not None:
                off, ext = q(tb_box, "a:off"), q(tb_box, "a:ext")
                x, y = tf(int(off.get("x")), int(off.get("y")))
                x2, y2 = tf(int(off.get("x")) + int(ext.get("cx")), int(off.get("y")) + int(ext.get("cy")))
                box = (x, y, x2 - x, y2 - y)
        box = text_rect(prst, box)
        fill_rgb = val if kind == "solid" else None
        font_col = None
        if style is not None:
            fr = q(style, "a:fontRef")
            if fr is not None:
                font_col = self.ctx.color(fr)
                behind = fill_rgb or self.bg
                if font_col and abs(luminance(font_col) - luminance(behind)) < 0.25:
                    font_col = None  # unreadable on this fill: let the automatic colour decide
        draw_text(self.img, self.ctx, self.slide, el, txb, box, fill_rgb, self.bg,
                  default_anchor=None if is_ph or (kind is None and not line[0]) else "ctr", font_col=font_col)

    def pic(self, el, tf, obj):
        box, rot, fh, fv = self.box_of(el, tf, obj)
        if box is None:
            return
        bf = el.find("./p:blipFill", NS)
        blip = q(bf, "a:blip")
        if blip is None or not blip.get(R_EMBED):
            return
        im = image_from(self.part, blip.get(R_EMBED))
        if im is None:
            self.placeholder(box, "Picture")
            return
        if fh:
            from PIL import ImageOps
            im = ImageOps.mirror(im)
        if fv:
            from PIL import ImageOps
            im = ImageOps.flip(im)
        geom = el.find("./p:spPr/a:prstGeom", NS)
        paste_image(self.img, im, box, _crop(bf), rot, geom.get("prst") if geom is not None else None,
                    self.ctx.alpha(blip))
        line = self.line_of(el.find("./p:spPr", NS), el.find("./p:style", NS))
        if line[0]:
            self.shape_fill(box, "rect", None, None, 1, line, rot)

    def placeholder(self, box, label):
        x, y, w, h = box
        self.d.rectangle([x, y, x + w, y + h], outline=(200, 200, 200), width=2)
        f = self.ctx.font(max(12, round(min(w, h) / 10)))
        self.d.text((x + w / 2, y + h / 2), label, font=f, fill=(150, 150, 150), anchor="mm")

    def line(self, el, tf):
        box, rot, fh, fv = self.xfrm_box(el, tf)
        if box is None:
            return
        col, wid = self.line_of(el.find("./p:spPr", NS), el.find("./p:style", NS))
        col = col or (90, 90, 90)
        x, y, w, h = box
        x1, y1, x2, y2 = x, y, x + w, y + h
        if fh:
            x1, x2 = x2, x1
        if fv:
            y1, y2 = y2, y1
        self.d.line([(x1, y1), (x2, y2)], fill=col, width=max(1, round(wid or 2)))
        ln = el.find("./p:spPr/a:ln", NS)
        if ln is not None and q(ln, "a:tailEnd") is not None and q(ln, "a:tailEnd").get("type", "none") != "none":
            self.arrow(x1, y1, x2, y2, col, wid)
        if ln is not None and q(ln, "a:headEnd") is not None and q(ln, "a:headEnd").get("type", "none") != "none":
            self.arrow(x2, y2, x1, y1, col, wid)

    def arrow(self, x1, y1, x2, y2, col, wid):
        ang = math.atan2(y2 - y1, x2 - x1)
        s = max(8, (wid or 2) * 4)
        pts = [(x2, y2), (x2 - s * math.cos(ang - 0.45), y2 - s * math.sin(ang - 0.45)),
               (x2 - s * math.cos(ang + 0.45), y2 - s * math.sin(ang + 0.45))]
        self.d.polygon(pts, fill=col)

    def frame(self, el, tf, obj):
        box, rot, fh, fv = self.box_of(el, tf, obj)
        if box is None:
            return
        gd = el.find("./a:graphic/a:graphicData", NS)
        uri = gd.get("uri", "") if gd is not None else ""
        if uri.endswith("/table"):
            self.table(gd.find("a:tbl", NS), box)
        elif uri.endswith("/chart"):
            ch = gd.find("c:chart", NS)
            try:
                self.chart(self.part.related_part(ch.get(R_ID)), box)
            except Exception:
                self.placeholder(box, "Chart")
        elif uri.endswith("/diagram"):
            if not self.smartart(gd, box):
                self.placeholder(box, "Diagram")
        elif "picture" in uri or gd is not None and gd.find(".//a:blip", NS) is not None:
            blip = gd.find(".//a:blip", NS)
            if blip is not None and blip.get(R_EMBED):
                paste_image(self.img, image_from(self.part, blip.get(R_EMBED)), box)
        else:
            fb = el.find(".//p:pic", NS)
            if fb is not None:
                self.pic(fb, self.base_tf(), None)

    def table(self, tbl, box):
        x, y, w, h = box
        cols = [int(g.get("w")) for g in qa(tbl, "a:tblGrid/a:gridCol")]
        rows = qa(tbl, "a:tr")
        if not cols or not rows:
            return
        tw = sum(cols) or 1
        cw = [w * c / tw for c in cols]
        rh = [max(self.ctx.px(int(r.get("h", "0"))), 10) for r in rows]
        tpr = q(tbl, "a:tblPr")
        first = tpr is not None and tpr.get("firstRow") == "1"
        band = tpr is not None and tpr.get("bandRow") == "1"
        acc = self.ctx.theme["accent1"]
        light = tuple(round(c + (255 - c) * 0.8) for c in acc)
        lighter = tuple(round(c + (255 - c) * 0.9) for c in acc)
        yy = y
        for ri, r in enumerate(rows):
            xx = x
            for ci, tc in enumerate(qa(r, "a:tc")):
                if ci >= len(cw):
                    break
                span = int(tc.get("gridSpan", "1"))
                cwid = sum(cw[ci:ci + span])
                if tc.get("hMerge") == "1" or tc.get("vMerge") == "1":
                    xx += cw[ci]
                    continue
                tcpr = q(tc, "a:tcPr")
                fill = None
                sf = q(tcpr, "a:solidFill") if tcpr is not None else None
                if sf is not None:
                    fill = self.ctx.color(sf)
                elif first and ri == 0:
                    fill = acc
                elif band:
                    fill = light if ri % 2 == 1 else lighter
                if fill:
                    self.d.rectangle([xx, yy, xx + cwid, yy + rh[ri]], fill=fill)
                self.d.rectangle([xx, yy, xx + cwid, yy + rh[ri]], outline=(255, 255, 255) if fill else (190, 190, 190), width=1)
                draw_text(self.img, self.ctx, None, None, q(tc, "a:txBody"), (xx, yy, cwid, rh[ri]), fill, self.bg,
                          default_anchor="ctr" if tcpr is not None and tcpr.get("anchor") == "ctr" else "t", table_cell=True)
                xx += cw[ci]
            yy += rh[ri]

    def chart(self, cpart, box):
        root = etree.fromstring(cpart.blob)
        x, y, w, h = box
        title = "".join(t.text or "" for t in root.findall(".//c:title//a:t", NS))
        plot = root.find(".//c:plotArea", NS)
        kinds = [etree.QName(c).localname for c in plot if etree.QName(c).localname.endswith("Chart")] if plot is not None else []
        kind = kinds[0] if kinds else "barChart"
        series = []
        cats = []
        for ser in root.findall(".//c:ser", NS):
            vals = [float(v.text) for v in ser.findall("./c:val//c:v", NS) if v.text and _num(v.text)]
            if not cats:
                cats = [v.text or "" for v in ser.findall("./c:cat//c:v", NS)]
            name = "".join(v.text or "" for v in ser.findall("./c:tx//c:v", NS))
            col = self.ctx.color(ser.find("./c:spPr/a:solidFill", NS))
            series.append((name, vals, col))
        if not series:
            return self.placeholder(box, "Chart")
        accents = [self.ctx.theme[f"accent{i}"] for i in range(1, 7)]
        fs = max(10, round(min(w, h) / 22))
        f, fb = self.ctx.font(fs), self.ctx.font(round(fs * 1.25), True)
        tc = (255, 255, 255) if luminance(self.bg) < 0.5 else (60, 60, 60)
        top = y + 8
        if title:
            self.d.text((x + w / 2, top), title, font=fb, fill=tc, anchor="mt")
            top += fs * 2
        if kind in ("pieChart", "doughnutChart", "pie3DChart"):
            vals = series[0][1]
            tot = sum(v for v in vals if v > 0) or 1
            r = min(w * 0.45, (y + h - top) * 0.9) / 2
            cx, cy = x + w * 0.35, top + (y + h - top) / 2
            a0 = -90
            for i, v in enumerate(vals):
                if v <= 0:
                    continue
                a1 = a0 + 360 * v / tot
                self.d.pieslice([cx - r, cy - r, cx + r, cy + r], a0, a1, fill=accents[i % 6], outline=(255, 255, 255))
                a0 = a1
            ly = cy - len(vals) * fs * 0.8
            for i, v in enumerate(vals):
                self.d.rectangle([x + w * 0.65, ly, x + w * 0.65 + fs, ly + fs], fill=accents[i % 6])
                self.d.text((x + w * 0.65 + fs * 1.5, ly), (cats[i] if i < len(cats) else str(i + 1))[:24], font=f, fill=tc)
                ly += fs * 1.6
            return
        allv = [v for _, vals, _ in series for v in vals] or [0]
        hi, lo = max(max(allv), 0), min(min(allv), 0)
        rng = (hi - lo) or 1
        left, right, bottom = x + fs * 3, x + w - 10, y + h - fs * 2.2
        if len(series) > 1:
            right -= w * 0.2
        ph = bottom - top
        zero = bottom - (0 - lo) / rng * ph
        self.d.line([(left, top), (left, bottom)], fill=(170, 170, 170))
        self.d.line([(left, zero), (right, zero)], fill=(170, 170, 170))
        for k in range(5):
            v = lo + rng * k / 4
            yy = bottom - (v - lo) / rng * ph
            self.d.text((left - 6, yy), _short(v), font=f, fill=tc, anchor="rm")
        n = max(len(s[1]) for s in series) or 1
        gw = (right - left) / n
        horizontal = kind == "barChart" and root.find(".//c:barDir", NS) is not None and root.find(".//c:barDir", NS).get("val") == "bar"
        for si, (name, vals, col) in enumerate(series):
            col = col or accents[si % 6]
            if kind in ("lineChart", "line3DChart", "scatterChart", "areaChart"):
                pts = [(left + gw * (i + 0.5), bottom - (v - lo) / rng * ph) for i, v in enumerate(vals)]
                if len(pts) > 1:
                    self.d.line(pts, fill=col, width=max(2, fs // 5))
                for p in pts:
                    self.d.ellipse([p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3], fill=col)
            else:
                bw = gw * 0.7 / len(series)
                for i, v in enumerate(vals):
                    bx = left + gw * i + gw * 0.15 + bw * si
                    by = bottom - (v - lo) / rng * ph
                    self.d.rectangle([bx, min(by, zero), bx + bw - 1, max(by, zero)], fill=col)
            if len(series) > 1:
                ly = top + si * fs * 1.6
                self.d.rectangle([right + 12, ly, right + 12 + fs, ly + fs], fill=col)
                self.d.text((right + 12 + fs * 1.5, ly), name[:18], font=f, fill=tc)
        for i, c in enumerate(cats[:n]):
            self.d.text((left + gw * (i + 0.5), bottom + 6), c[:14], font=f, fill=tc, anchor="mt")
        _ = horizontal

    def smartart(self, gd, box):
        rel = gd.find("dgm:relIds", NS)
        if rel is None:
            return False
        data_rid = rel.get("{%s}dm" % NS["r"])
        try:
            data_part = self.part.related_part(data_rid)
            drawing = None
            for r in data_part.rels.values():
                if r.reltype == RT_DIAGRAM_DRAWING:
                    drawing = r.target_part
            if drawing is None:
                for r in self.part.rels.values():
                    if r.reltype == RT_DIAGRAM_DRAWING:
                        drawing = r.target_part
                        break
            if drawing is None:
                return False
            root = etree.fromstring(drawing.blob)
        except Exception:
            return False
        tree = root.find("dsp:spTree", NS)
        if tree is None:
            return False
        x0, y0 = box[0], box[1]
        sc = self.ctx.sc

        def tf(X, Y):
            return (x0 + X * sc, y0 + Y * sc)

        for sp in tree.findall("dsp:sp", NS):
            try:
                self.sp(sp, tf, None, part=drawing)
            except Exception:
                continue
        return True


def _num(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def _short(v):
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.0f}k"
    return f"{v:g}" if abs(v) >= 10 or v == int(v) else f"{v:.1f}"


# ---------------------------------------------------------------- background and slides
def background(ctx, slide):
    """(colour, image or None) for a slide, from the slide, its layout or its master."""
    for holder in (slide, slide.slide_layout, slide.slide_layout.slide_master):
        bg = holder._element.find("./p:cSld/p:bg", NS)
        if bg is None:
            continue
        pr = q(bg, "p:bgPr")
        if pr is not None:
            bf = q(pr, "a:blipFill")
            if bf is not None and q(bf, "a:blip") is not None:
                im = image_from(holder.part, q(bf, "a:blip").get(R_EMBED))
                if im is not None:
                    return ctx.theme["lt1"], (im, _crop(bf))
            sf = q(pr, "a:solidFill")
            if sf is not None:
                return ctx.color(sf) or ctx.theme["lt1"], None
            gf = q(pr, "a:gradFill")
            if gf is not None:
                cols = [ctx.color(s) for s in qa(gf, "a:gsLst/a:gs")]
                cols = [c for c in cols if c]
                if cols:
                    return tuple(sum(c[i] for c in cols) // len(cols) for i in range(3)), None
        ref = q(bg, "p:bgRef")
        if ref is not None:
            c = ctx.color(ref)
            if c:
                return c, None
    return ctx.theme["lt1"], None


def render_deck(src, font, width=1920):
    """Pictures (PIL images) of every slide and the text of each slide."""
    from PIL import Image
    from pptx import Presentation

    prs = Presentation(src)
    ctx = Ctx(prs, width, font)
    images, texts = [], []
    for slide in prs.slides:
        bg, bg_img = background(ctx, slide)
        img = Image.new("RGBA", (ctx.W, ctx.H), bg + (255,))
        if bg_img:
            paste_image(img, bg_img[0], (0, 0, ctx.W, ctx.H), bg_img[1])
        layout = slide.slide_layout
        master = layout.slide_master
        show_master = layout._element.get("showMasterSp", "1") not in ("0", "false") and \
            slide._element.get("showMasterSp", "1") not in ("0", "false")
        layers = ([master] if show_master else []) + [layout]
        for holder in layers:
            tree = holder._element.find("./p:cSld/p:spTree", NS)
            if tree is None:
                continue
            art = etree.Element("art")
            for el in tree:
                if el.find(".//p:nvPr/p:ph", NS) is not None:
                    continue  # placeholders on the master/layout are prompts like "Click to add title"
                art.append(_copy(el))
            Drawer(ctx, img, slide, holder.part, bg).draw_tree(art, Drawer(ctx, img, slide, holder.part, bg).base_tf())
        dr = Drawer(ctx, img, slide, slide.part, bg)
        dr.draw_tree(slide.shapes._spTree, dr.base_tf(), list(slide.shapes))
        images.append(img.convert("RGB"))
        texts.append(slide_text(slide))
    return images, texts


def _copy(el):
    from copy import deepcopy
    return deepcopy(el)


def slide_text(slide):
    title, body = "", []
    for shp in slide.shapes:
        if getattr(shp, "has_text_frame", False) and shp.has_text_frame:
            t = "\n".join(p.text.strip() for p in shp.text_frame.paragraphs if p.text.strip())
            if not t:
                continue
            if shp == getattr(slide.shapes, "title", None) and not title:
                title = t.replace("\n", " ")
            else:
                body.append(t)
        elif getattr(shp, "has_table", False) and shp.has_table:
            for r in shp.table.rows:
                body.append(" | ".join(c.text for c in r.cells))
    try:
        notes = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ""
    except Exception:
        notes = ""
    return (title + "\n" + "\n".join(body) + ("\n" + notes if notes else "")).strip()
