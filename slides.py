"""Uploaded slides, documents and PDFs: convert to PDF, render pages, pull out page text.

Conversion, best first:
  PowerPoint/Keynote slides -> Keynote (free on every Mac) -> LibreOffice -> our own slide drawing (pptx_render:
                               pictures, shapes, tables, charts, SmartArt) -> text-only slide cards
  Word/Pages/RTF documents  -> Pages -> LibreOffice -> our own pages with the text and pictures
  PDFs are used as they are; photos become one-page documents.
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import zipfile

from store import class_dir

SLIDE_EXT = {"ppt", "pptx", "pps", "ppsx", "key", "odp"}
DOC_EXT = {"doc", "docx", "pages", "rtf", "odt", "txt", "md", "html", "htm"}
IMG_EXT = {"png", "jpg", "jpeg", "gif", "webp", "heic", "tif", "tiff", "bmp"}
ACCEPT = SLIDE_EXT | DOC_EXT | IMG_EXT | {"pdf"}
WIDTHS = (240, 900, 1600, 2200)
PDF_LOCK = threading.Lock()   # pdfium is not thread-safe
APP_LOCK = threading.Lock()   # Keynote, Pages and LibreOffice convert one file at a time
MAC = sys.platform == "darwin"

KEYNOTE = "/Applications/Keynote.app"
PAGES = "/Applications/Pages.app"


def doc_dir(kid, did):
    return os.path.join(class_dir(kid), "docs", did)


def soffice():
    for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice", shutil.which("soffice"), shutil.which("libreoffice")):
        if p and os.path.exists(p):
            return p
    return None


def _run(cmd, timeout=240):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "failed").strip()[:300])
    return r.stdout


def _iwork_export(app, src, out):
    script = [
        "on run argv",
        f'tell application "{app}"',
        "set d to open (POSIX file (item 1 of argv))",
        "repeat 60 times",
        "if (count of documents) > 0 then exit repeat",
        "delay 0.5",
        "end repeat",
        "delay 1",
        "export d to (POSIX file (item 2 of argv)) as PDF",
        "close d saving no",
        "end tell",
        "end run",
    ]
    cmd = ["osascript"]
    for line in script:
        cmd += ["-e", line]
    with APP_LOCK:
        try:
            _run(cmd + [src, out], timeout=300)
        except Exception as e:
            msg = str(e)
            if "-1743" in msg or "Not authorized" in msg or "not allowed" in msg.lower():
                raise RuntimeError(f"macOS hasn't allowed DenSem Class to use {app}. Turn it on in System Settings › "
                                   f"Privacy & Security › Automation › DenSem Class › {app}, then choose Convert again.")
            raise
    if not os.path.exists(out) or os.path.getsize(out) < 100:
        raise RuntimeError(f"{app} did not export a PDF")


def _soffice_export(src, out):
    exe = soffice()
    if not exe:
        raise RuntimeError("LibreOffice is not installed")
    with APP_LOCK, tempfile.TemporaryDirectory() as tmp:
        profile = "file://" + os.path.join(tmp, "profile")
        _run([exe, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pdf", "--outdir", tmp, src],
             timeout=300)
        made = [f for f in os.listdir(tmp) if f.endswith(".pdf")]
        if not made:
            raise RuntimeError("LibreOffice did not produce a PDF")
        shutil.move(os.path.join(tmp, made[0]), out)


# ---------------------------------------------------------------- text-only fallback pages
def _font(size, bold=False):
    from PIL import ImageFont

    names = (
        ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf", "DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold
        else ["/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf", "DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw, text, font, width):
    lines = []
    for para in str(text).split("\n"):
        words, cur = para.split(), ""
        if not words:
            lines.append("")
            continue
        for w in words:
            trial = (cur + " " + w).strip()
            if draw.textlength(trial, font=font) <= width:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


def slide_cards(slides, name):
    """One 16:9 card per slide: title + bullet text."""
    from PIL import Image, ImageDraw

    W, H, M = 1600, 900, 90
    out = []
    for i, (title, body) in enumerate(slides, 1):
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 14], fill=(31, 107, 92))
        for size in (34, 30, 26, 22, 18):
            tf, bf = _font(int(size * 1.6), True), _font(size)
            tl = _wrap(d, title or f"Slide {i}", tf, W - 2 * M)
            bl = []
            for b in body:
                wrapped = _wrap(d, b, bf, W - 2 * M - 40)
                bl += [("• " if j == 0 else "   ") + x for j, x in enumerate(wrapped)]
            h = len(tl) * size * 2.1 + 30 + len(bl) * size * 1.5
            if h < H - 2 * M - 40:
                break
        y = M
        for x in tl:
            d.text((M, y), x, font=tf, fill=(29, 36, 33))
            y += int(size * 2.1)
        y += 30
        for x in bl:
            if y > H - M:
                break
            d.text((M, y), x, font=bf, fill=(60, 70, 66))
            y += int(size * 1.5)
        d.text((W - M - 200, H - 50), f"{name[:28]} · {i}", font=_font(18), fill=(150, 160, 155))
        out.append(img)
    return out


def text_pages(paragraphs):
    """A4-ish pages of flowing text."""
    from PIL import Image, ImageDraw

    W, H, M = 1240, 1754, 120
    pages, img, d, y = [], None, None, 0
    body, head = _font(26), _font(36, True)

    def new_page():
        nonlocal img, d, y
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        y = M
        pages.append(img)

    new_page()
    for kind, text in paragraphs:
        if kind == "img":
            try:
                import io
                from PIL import Image as _I

                im = _I.open(io.BytesIO(text)).convert("RGBA")
                scale = min(1.0, (W - 2 * M) / im.width, (H - 2 * M) / im.height)
                im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
                if y + im.height > H - M:
                    new_page()
                img.paste(im, (M + (W - 2 * M - im.width) // 2, y), im)
                y += im.height + 24
            except Exception:
                pass
            continue
        f = head if kind == "h" else body
        lh = 50 if kind == "h" else 40
        if kind == "h":
            y += 16
        for line in _wrap(d, text, f, W - 2 * M):
            if y + lh > H - M:
                new_page()
            d.text((M, y), line, font=f, fill=(29, 36, 33))
            y += lh
        y += 18
    return pages


def save_images_pdf(images, out):
    images = [im.convert("RGB") for im in images]
    images[0].save(out, "PDF", resolution=150, save_all=True, append_images=images[1:])


def pptx_slides(src):
    from pptx import Presentation

    out = []
    for slide in Presentation(src).slides:
        title, body = "", []
        for shp in slide.shapes:
            if not getattr(shp, "has_text_frame", False) or not shp.has_text_frame:
                continue
            text = "\n".join(p.text.strip() for p in shp.text_frame.paragraphs if p.text.strip())
            if not text:
                continue
            if shp == getattr(slide.shapes, "title", None) and not title:
                title = text.replace("\n", " ")
            else:
                body += text.split("\n")
        out.append((title, body))
    return out


def doc_paragraphs(src, ext):
    if ext == "docx":
        import docx

        out = []
        d = docx.Document(src)
        blip = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
        embed = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
        for p in d.paragraphs:
            if p.text.strip():
                out.append(("h" if (p.style.name or "").lower().startswith(("heading", "title")) else "p", p.text.strip()))
            for b in p._element.iter(blip):
                rid = b.get(embed)
                try:
                    out.append(("img", d.part.related_parts[rid].blob))
                except Exception:
                    continue
        return out
    if ext in ("txt", "md"):
        with open(src, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    elif ext in ("html", "htm"):
        with open(src, encoding="utf-8", errors="replace") as f:
            raw = html.unescape(re.sub(r"<[^>]+>", "\n", f.read()))
    elif MAC and ext in ("doc", "rtf", "odt"):
        raw = _run(["textutil", "-convert", "txt", "-stdout", src])
    elif ext == "odt":
        with zipfile.ZipFile(src) as z:
            raw = html.unescape(re.sub(r"<[^>]+>", "\n", z.read("content.xml").decode("utf-8", "replace")))
    else:
        raise RuntimeError("This file type needs Pages or LibreOffice to open. Export it as PDF and upload that.")
    out = []
    for line in raw.split("\n"):
        line = line.strip()
        if line:
            out.append(("h" if line.startswith("#") else "p", line.lstrip("# ")))
    return out


def image_pdf(src, ext, out):
    from PIL import Image

    path = src
    tmp = None
    if ext == "heic" and MAC:
        tmp = src + ".png"
        _run(["sips", "-s", "format", "png", src, "--out", tmp])
        path = tmp
    try:
        save_images_pdf([Image.open(path)], out)
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------- conversion entry point
def convert(kid, did, ext, report, name=""):
    """Make doc.pdf and text.json for an uploaded file. Returns (pages, method)."""
    folder = doc_dir(kid, did)
    src = os.path.join(folder, "source." + ext)
    pdf = os.path.join(folder, "doc.pdf")
    texts, method, errors = None, "", []

    def attempt(label, fn):
        nonlocal method
        if os.path.exists(pdf):
            return True
        try:
            report(f"Converting with {label}…")
            fn()
            method = label
            return True
        except Exception as e:
            errors.append(f"{label}: {e}")
            if os.path.exists(pdf):
                os.remove(pdf)
            return False

    if ext == "pdf":
        shutil.copy(src, pdf)
        method = "PDF"
    elif ext in IMG_EXT:
        attempt("image", lambda: image_pdf(src, ext, pdf))
    elif ext in SLIDE_EXT:
        if MAC and os.path.exists(KEYNOTE):
            attempt("Keynote", lambda: _iwork_export("Keynote", src, pdf))
        attempt("LibreOffice", lambda: _soffice_export(src, pdf)) if soffice() else None
        if not os.path.exists(pdf) and ext in ("pptx", "ppsx", "pptm", "potx"):
            def drawn():
                nonlocal texts
                import pptx_render

                imgs, tx = pptx_render.render_deck(src, _font)
                if not imgs:
                    raise RuntimeError("no slides found")
                texts = tx
                save_images_pdf(imgs, pdf)

            attempt("DenSem drawing", drawn)
        if not os.path.exists(pdf) and ext in ("pptx", "ppsx"):
            def cards():
                nonlocal texts
                sl = pptx_slides(src)
                if not sl:
                    raise RuntimeError("no slides found")
                texts = [(t + "\n" + "\n".join(b)).strip() for t, b in sl]
                save_images_pdf(slide_cards(sl, name or "slides"), pdf)

            attempt("text only", cards)
    elif ext in DOC_EXT:
        if MAC and os.path.exists(PAGES) and ext in ("doc", "docx", "pages", "rtf", "txt"):
            attempt("Pages", lambda: _iwork_export("Pages", src, pdf))
        attempt("LibreOffice", lambda: _soffice_export(src, pdf)) if soffice() else None
        if not os.path.exists(pdf):
            def pages():
                nonlocal texts
                paras = doc_paragraphs(src, ext)
                if not paras:
                    raise RuntimeError("the document has no text")
                save_images_pdf(text_pages(paras), pdf)
                texts = ["\n".join(t for k, t in paras if k != "img")]  # page-level split not known; matched as one block

            attempt("DenSem pages", pages)
    if not os.path.exists(pdf):
        raise RuntimeError(errors[-1].split(": ", 1)[-1] if errors else "This file type isn't supported.")

    report("Reading pages…")
    import pypdfium2 as pdfium

    with PDF_LOCK:
        doc = pdfium.PdfDocument(pdf)
        n = len(doc)
        page_texts = []
        for i in range(n):
            page = doc[i]
            try:
                tp = page.get_textpage()
                page_texts.append(tp.get_text_range().replace("\r", ""))
                tp.close()
            except Exception:
                page_texts.append("")
            page.close()
        doc.close()
    if texts and len(texts) == n:
        page_texts = texts
    elif texts and not any(t.strip() for t in page_texts):
        page_texts = texts + [""] * max(0, n - len(texts))
    with open(os.path.join(folder, "text.json"), "w") as f:
        json.dump(page_texts[:n], f)
    os.makedirs(os.path.join(folder, "pages"), exist_ok=True)
    return n, method


def reset(kid, did):
    """Forget a document's converted pages so it can be converted again."""
    folder = doc_dir(kid, did)
    for name in ("doc.pdf", "text.json"):
        try:
            os.remove(os.path.join(folder, name))
        except FileNotFoundError:
            pass
    shutil.rmtree(os.path.join(folder, "pages"), ignore_errors=True)


def page_texts(kid, did):
    try:
        with open(os.path.join(doc_dir(kid, did), "text.json")) as f:
            return json.load(f)
    except Exception:
        return []


def render(kid, did, page, width):
    """PNG of one page at roughly `width` pixels wide, cached on disk."""
    width = min(WIDTHS, key=lambda w: abs(w - width))
    folder = doc_dir(kid, did)
    out = os.path.join(folder, "pages", f"{page}-{width}.png")
    if os.path.exists(out):
        return out
    import pypdfium2 as pdfium

    with PDF_LOCK:
        doc = pdfium.PdfDocument(os.path.join(folder, "doc.pdf"))
        try:
            if not 1 <= page <= len(doc):
                raise ValueError("no such page")
            pg = doc[page - 1]
            img = pg.render(scale=width / pg.get_width()).to_pil()
            pg.close()
        finally:
            doc.close()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp.png"
    img.save(tmp, optimize=width <= 240)
    os.replace(tmp, out)
    return out
