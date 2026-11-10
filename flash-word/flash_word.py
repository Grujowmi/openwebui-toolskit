"""
title: Flash Word
author: Grujowmi
version: 1.1.0
required_open_webui_version: 0.11.0
license: MIT
icon_url: data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9ImN1cnJlbnRDb2xvciIgc3Ryb2tlLXdpZHRoPSIyIiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiPjxwYXRoIGQ9Ik0xNCAzSDdhMiAyIDAgMCAwLTIgMnYxNGEyIDIgMCAwIDAgMiAyaDEwYTIgMiAwIDAgMCAyLTJWOHoiLz48cGF0aCBkPSJNMTQgM3Y1aDUiLz48cGF0aCBkPSJNOC41IDEzaDciLz48cGF0aCBkPSJNOC41IDE2LjVoNSIvPjwvc3ZnPg==
description: Transforme la réponse du chat en document Word (.docx) propre en un clic : titres, listes, tableaux, code, liens. Conversion directe sans appel au modèle, fidèle au texte. Aucun fichier stocké sur le serveur.
"""

# Fonctionnement
# - Un bouton d'action sous la réponse. Le Markdown de la réponse est converti
#   par du code (aucun appel au modèle) en .docx avec python-docx, déjà présent
#   dans Open WebUI. Résultat instantané et fidèle mot pour mot.
# - Retour en HTMLResponse inline (embed Rich UI) : une carte avec le bouton de
#   téléchargement et un aperçu du document (hauteur fixe, défilement interne).
#   Le .docx est intégré à la carte (lien data:), rien n'est
#   écrit sur le disque du serveur.
# - Aucune donnée de la conversation n'est envoyée ailleurs. Les journaux ne
#   contiennent que des métadonnées.
# - Pas de ligne "requirements" : aucune installation pip.

import asyncio
import base64
import html
import io
import logging
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("flash_word")

# --------------------------------------------------------------------------
# Textes d'interface
# --------------------------------------------------------------------------

UI = {
    "fr": {
        "toast_start": "Génération du document Word démarrée",
        "toast_done": "Génération du document Word terminée",
        "starting": "Flash Word : mise en forme du document…",
        "done": "Flash Word : terminé",
        "empty": "Rien à exporter dans ce message.",
        "failed": "Échec de la génération (détails dans les journaux du serveur).",
        "download": "Télécharger le document Word",
        "untitled": "Document",
        "question": "Question",
        "answer": "Réponse",
        "image": "Image",
        "page": "Page",
        "w_section": ("section", "sections"),
        "w_table": ("tableau", "tableaux"),
        "w_word": ("mot", "mots"),
        "months": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
        "lang_tag": "fr-FR",
    },
    "en": {
        "toast_start": "Word document generation started",
        "toast_done": "Word document generation finished",
        "starting": "Flash Word: formatting the document...",
        "done": "Flash Word: done",
        "empty": "Nothing to export in this message.",
        "failed": "Generation failed (see server logs for details).",
        "download": "Download Word document",
        "untitled": "Document",
        "question": "Question",
        "answer": "Answer",
        "image": "Image",
        "page": "Page",
        "w_section": ("section", "sections"),
        "w_table": ("table", "tables"),
        "w_word": ("word", "words"),
        "months": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
        "lang_tag": "en-GB",
    },
}

# --------------------------------------------------------------------------
# Nettoyage du texte source
# --------------------------------------------------------------------------

_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f￾￿\ud800-\udfff]")


def clean_source(t: str) -> str:
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"<details\b[^>]*>.*?</details>", "", t, flags=re.S | re.I)  # raisonnement, appels d'outils
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S | re.I)
    t = re.sub(r"```html\s*<!-- OPENWEBUI_PLUGIN_OUTPUT -->.*?```", "", t, flags=re.S)
    t = _XML_BAD.sub("", t)
    return t.strip()


# --------------------------------------------------------------------------
# Markdown : blocs
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+#.\-]*)")
_HEAD = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_LIST = re.compile(r"^(\s*)([-*+•]|\d{1,4}[.)])\s+(.*)$")
_TSEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")


def split_row(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, in_code, k = [], "", False, 0
    while k < len(s):
        ch = s[k]
        if ch == "\\" and k + 1 < len(s) and s[k + 1] == "|":
            cur += "|"
            k += 2
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
        k += 1
    cells.append(cur.strip())
    return cells


def _is_block_start(line: str, nxt: Optional[str]) -> bool:
    return bool(
        _FENCE.match(line) or _HEAD.match(line) or _HR.match(line) or line.lstrip().startswith(">")
        or ("|" in line and nxt is not None and _TSEP.match(nxt))
    )


def parse_blocks(text: str) -> List[Dict[str, Any]]:
    lines = text.split("\n")
    n = len(lines)
    blocks: List[Dict[str, Any]] = []
    para: List[str] = []

    def flush():
        if para:
            blocks.append({"t": "p", "text": "\n".join(para)})
            para.clear()

    i = 0
    while i < n:
        line = lines[i]
        s = line.strip()
        nxt = lines[i + 1] if i + 1 < n else None
        fm = _FENCE.match(line)
        if fm:
            flush()
            mark = fm.group(1)
            code: List[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith(mark[0] * len(mark)):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append({"t": "code", "lang": fm.group(2), "text": "\n".join(code).rstrip("\n")})
            continue
        if not s:
            flush()
            i += 1
            continue
        hm = _HEAD.match(line)
        if hm:
            flush()
            if hm.group(2).strip():
                blocks.append({"t": "h", "level": len(hm.group(1)), "text": hm.group(2).strip()})
            i += 1
            continue
        if _HR.match(line):
            flush()
            blocks.append({"t": "hr"})
            i += 1
            continue
        if "|" in line and nxt is not None and _TSEP.match(nxt):
            flush()
            header = split_row(line)
            aligns = []
            for c in split_row(nxt):
                c = c.strip()
                aligns.append("c" if c.startswith(":") and c.endswith(":") else "r" if c.endswith(":") else "l")
            rows = []
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(split_row(lines[i]))
                i += 1
            nc = len(header)
            aligns = (aligns + ["l"] * nc)[:nc]
            rows = [(r + [""] * nc)[:nc] for r in rows]
            blocks.append({"t": "table", "header": header, "aligns": aligns, "rows": rows})
            continue
        if line.lstrip().startswith(">"):
            flush()
            q: List[str] = []
            while i < n and lines[i].lstrip().startswith(">"):
                q.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append({"t": "quote", "blocks": parse_blocks("\n".join(q))})
            continue
        lm = _LIST.match(line)
        if lm:
            flush()
            items: List[Dict[str, Any]] = []
            while i < n:
                cur = lines[i]
                m = _LIST.match(cur)
                if m:
                    mk = m.group(2)
                    txt = m.group(3)
                    check = None
                    cm = re.match(r"^\[([ xX])\]\s+(.*)$", txt)
                    if cm:
                        check, txt = cm.group(1).lower() == "x", cm.group(2)
                    items.append({
                        "indent": len(m.group(1).expandtabs(4)),
                        "ordered": mk[0].isdigit(),
                        "num": int(mk[:-1]) if mk[0].isdigit() else 1,
                        "text": txt.strip(),
                        "check": check,
                    })
                    i += 1
                    continue
                if not cur.strip():
                    j = i + 1
                    while j < n and not lines[j].strip():
                        j += 1
                    if j < n and (_LIST.match(lines[j]) or (lines[j].startswith("  ") and not _FENCE.match(lines[j]))):
                        i = j
                        continue
                    break
                nx = lines[i + 1] if i + 1 < n else None
                if _is_block_start(cur, nx):
                    break
                items[-1]["text"] += "\n" + cur.strip()
                i += 1
            blocks.append({"t": "list", "items": items})
            continue
        para.append(s)
        i += 1
    flush()
    return blocks


# --------------------------------------------------------------------------
# Markdown : en ligne
# --------------------------------------------------------------------------

_INLINE = re.compile(
    r"""
    (?P<code>`+)(?P<codetxt>.+?)(?P=code)
   |(?P<img>!\[(?P<imgalt>[^\]]*)\]\([^)]*\))
   |\[(?P<ltxt>[^\[\]]+)\]\((?P<lurl>(?:[^()\s]|\([^()\s]*\))+)(?:\s+"[^"]*")?\)
   |<(?P<auto>(?:https?://|mailto:)[^>\s]+)>
   |(?P<bare>https?://[^\s<>"'`]+[^\s<>"'`.,;:!?)\]])
   |\*\*\*(?P<bi>[^\s*](?:.*?[^\s*])?)\*\*\*
   |\*\*(?P<b1>[^\s*](?:.*?[^\s])?)\*\*
   |(?<![\w])__(?P<b2>[^\s_](?:.*?[^\s_])?)__(?![\w])
   |~~(?P<st>[^\s~](?:.*?[^\s~])?)~~
   |(?<![*\w\\])\*(?P<i1>[^\s*](?:[^*\n]*?[^\s*\\])?)\*(?![*\w])
   |(?<![\w\\])_(?P<i2>[^\s_](?:[^_\n]*?[^\s_\\])?)_(?![\w])
   |(?P<br><br\s*/?>)
   |\\(?P<esc>[\\`*_{}\[\]()#+\-.!|~>])
   |(?P<tag></?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?>)
    """,
    re.X,
)


def safe_url(u: str) -> Optional[str]:
    u = (u or "").strip()
    if len(u) > 2000 or re.search(r"[\s\x00-\x1f]", u):
        return None
    return u if re.match(r"^(https?://|mailto:)", u, re.I) else None


def _run(text, b=False, i=False, st=False, link=None, code=False, br=False):
    return {"text": text, "b": b, "i": i, "s": st, "link": link, "code": code, "br": br}


def parse_inline(s: str, b=False, i=False, st=False, link=None, ui=None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    pos = 0

    def plain(txt):
        if txt:
            out.append(_run(html.unescape(txt), b, i, st, link))

    for m in _INLINE.finditer(s):
        plain(s[pos : m.start()])
        g = m.group
        if g("code"):
            out.append(_run(g("codetxt"), b, i, st, link, code=True))
        elif g("img"):
            alt = g("imgalt").strip()
            label = (ui or UI["fr"])["image"]
            out.append(_run("[%s%s]" % (label, " : " + alt if alt else ""), b, True, st, link))
        elif g("ltxt") is not None:
            out += parse_inline(g("ltxt"), b, i, st, safe_url(g("lurl")) or link, ui)
        elif g("auto") or g("bare"):
            u = g("auto") or g("bare")
            out.append(_run(u, b, i, st, safe_url(u) or link))
        elif g("bi") is not None:
            out += parse_inline(g("bi"), True, True, st, link, ui)
        elif g("b1") is not None or g("b2") is not None:
            out += parse_inline(g("b1") if g("b1") is not None else g("b2"), True, i, st, link, ui)
        elif g("st") is not None:
            out += parse_inline(g("st"), b, i, True, link, ui)
        elif g("i1") is not None or g("i2") is not None:
            out += parse_inline(g("i1") if g("i1") is not None else g("i2"), b, True, st, link, ui)
        elif g("br"):
            out.append(_run("", br=True))
        elif g("esc"):
            plain(g("esc"))
        # balise HTML : ignorée
        pos = m.end()
    plain(s[pos:])
    return out


def plain_text(s: str) -> str:
    return "".join(r["text"] for r in parse_inline(s) if not r["br"]).strip()


# --------------------------------------------------------------------------
# Construction du .docx (python-docx)
# --------------------------------------------------------------------------

INK = "1E1B4B"
BODY = "1F2937"
MUTED = "64748B"
FAINT = "94A3B8"
LINE = "E2E8F0"
ZEBRA = "F8FAFC"
CODE_BG = "F1F5F9"
INDIGO = "4F46E5"
INDIGO_D = "4338CA"
INDIGO_200 = "C7D2FE"
QUOTE_TXT = "475569"
FONT = "Calibri"
MONO = "Consolas"
TEXT_W_CM = 17.0

PPR_ORDER = ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr", "suppressLineNumbers",
             "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct", "topLinePunct", "autoSpaceDE",
             "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents",
             "suppressOverlap", "jc", "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle",
             "rPr", "sectPr", "pPrChange"]
RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline", "shadow", "emboss",
             "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern", "position", "sz", "szCs",
             "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
             "specVanish", "oMath"]
TCPR_ORDER = ["cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar", "textDirection",
              "tcFitText", "vAlign", "hideMark"]
TBLPR_ORDER = ["tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize", "tblStyleColBandSize", "tblW", "jc",
               "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook", "tblCaption", "tblDescription"]


class DocxWriter:
    def __init__(self, lang: str, ui: Dict[str, Any]):
        from docx import Document
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        from docx.oxml import OxmlElement, parse_xml
        from docx.oxml.ns import nsdecls, qn
        from docx.shared import Cm, Pt, RGBColor

        self.OxmlElement, self.parse_xml, self.nsdecls, self.qn = OxmlElement, parse_xml, nsdecls, qn
        self.Cm, self.Pt, self.RGBColor, self.RT = Cm, Pt, RGBColor, RT
        self.ALIGN = {"l": WD_ALIGN_PARAGRAPH.LEFT, "c": WD_ALIGN_PARAGRAPH.CENTER, "r": WD_ALIGN_PARAGRAPH.RIGHT}
        self.TAB_RIGHT = WD_TAB_ALIGNMENT.RIGHT
        self.TBL_LEFT = WD_TABLE_ALIGNMENT.LEFT
        self.ui = ui
        self.lang = lang
        self.doc = Document()
        body = self.doc.element.body
        for p in list(body.findall(qn("w:p"))):  # modèle par défaut : retirer un éventuel paragraphe vide
            body.remove(p)
        self.numbering = None
        self.abstract_cache: Dict[Tuple[str, str], int] = {}
        self.stats = {"sections": 0, "tables": 0, "headings": []}
        self._setup()

    # -- XML utilitaires -------------------------------------------------

    def put(self, parent, el, order):
        """Insère el dans parent en respectant l'ordre du schéma (remplace un élément de même nom)."""
        tag = el.tag.split("}")[1]
        old = parent.find(self.qn("w:" + tag))
        if old is not None:
            parent.remove(old)
        idx = order.index(tag) if tag in order else len(order)
        for k, child in enumerate(parent):
            ctag = child.tag.split("}")[1] if "}" in child.tag else child.tag
            if ctag in order and order.index(ctag) > idx:
                parent.insert(k, el)
                return el
        parent.append(el)
        return el

    def el(self, tag, **attrs):
        e = self.OxmlElement(tag)
        for k, v in attrs.items():
            e.set(self.qn("w:" + k), str(v))
        return e

    def shd(self, fill):
        return self.el("w:shd", val="clear", color="auto", fill=fill)

    def borders(self, tag, **sides):
        b = self.el(tag)
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
            if side in sides:
                sz, color, space = sides[side]
                b.append(self.el("w:" + side, val="single", sz=sz, space=space, color=color))
        return b

    def p_pr(self, p):
        return p._p.get_or_add_pPr()

    # -- styles et page --------------------------------------------------

    def _fonts(self, style, name=FONT):
        rpr = style.element.get_or_add_rPr()
        rf = rpr.find(self.qn("w:rFonts"))
        if rf is None:
            rf = self.put(rpr, self.el("w:rFonts"), RPR_ORDER)
        for a in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            rf.attrib.pop(self.qn("w:" + a), None)
        for a in ("ascii", "hAnsi", "eastAsia", "cs"):
            rf.set(self.qn("w:" + a), name)
        return rpr

    def _color(self, style, hexc):
        style.font.color.rgb = self.RGBColor.from_string(hexc)
        c = style.element.get_or_add_rPr().find(self.qn("w:color"))
        if c is not None:
            for a in ("themeColor", "themeShade", "themeTint"):
                c.attrib.pop(self.qn("w:" + a), None)

    def _setup(self):
        Pt, Cm = self.Pt, self.Cm
        sec = self.doc.sections[0]
        sec.page_width, sec.page_height = Cm(21), Cm(29.7)
        sec.left_margin = sec.right_margin = Cm(2)
        sec.top_margin, sec.bottom_margin = Cm(2.2), Cm(2)
        sec.header_distance = sec.footer_distance = Cm(1)
        st = self.doc.styles
        normal = st["Normal"]
        rpr = self._fonts(normal)
        normal.font.size = Pt(11)
        self._color(normal, BODY)
        self.put(rpr, self.el("w:lang", val=self.ui["lang_tag"]), RPR_ORDER)
        pf = normal.paragraph_format
        pf.space_before, pf.space_after, pf.line_spacing = Pt(0), Pt(6), 1.15
        for lvl, (size, color, before, after) in {1: (18, INK, 18, 6), 2: (14, INDIGO_D, 14, 4), 3: (12, INK, 10, 3), 4: (11, BODY, 8, 2)}.items():
            hs = st["Heading %d" % lvl]
            self._fonts(hs)
            hs.font.size, hs.font.bold, hs.font.italic = Pt(size), True, False
            self._color(hs, color)
            f = hs.paragraph_format
            f.space_before, f.space_after, f.keep_with_next = Pt(before), Pt(after), True
        ts = st["Title"]
        self._fonts(ts)
        ts.font.size, ts.font.bold = Pt(24), True
        self._color(ts, INK)
        tpr = ts.element.get_or_add_pPr()
        for tag in ("w:pBdr",):
            old = tpr.find(self.qn(tag))
            if old is not None:
                tpr.remove(old)
        ts.paragraph_format.space_after = Pt(4)
        trpr = ts.element.get_or_add_rPr()
        for tag in ("w:spacing", "w:kern"):
            old = trpr.find(self.qn(tag))
            if old is not None:
                trpr.remove(old)

    def header_footer(self, title: str, date_txt: str):
        Pt, Cm = self.Pt, self.Cm
        sec = self.doc.sections[0]
        sec.header.is_linked_to_previous = False
        sec.footer.is_linked_to_previous = False
        hp = sec.header.paragraphs[0]
        hp.style = self.doc.styles["Normal"]
        self.put(self.p_pr(hp), self.borders("w:pBdr", bottom=(4, LINE, 6)), PPR_ORDER)
        hp.paragraph_format.space_after = Pt(0)
        hp.paragraph_format.tab_stops.add_tab_stop(Cm(TEXT_W_CM), self.TAB_RIGHT)
        r = hp.add_run(title[:90])
        r.font.size, r.font.color.rgb = Pt(9), self.RGBColor.from_string(MUTED)
        r = hp.add_run("\t" + date_txt)
        r.font.size, r.font.color.rgb = Pt(9), self.RGBColor.from_string(FAINT)
        fp = sec.footer.paragraphs[0]
        fp.style = self.doc.styles["Normal"]
        fp.alignment = self.ALIGN["c"]
        fp.paragraph_format.space_after = Pt(0)
        for part in (self.ui["page"] + " ", "PAGE", " / ", "NUMPAGES"):
            if part in ("PAGE", "NUMPAGES"):
                fld = self.el("w:fldSimple", instr=part)
                fld.append(self._raw_run("1", 9, FAINT))
                fp._p.append(fld)
            else:
                fp._p.append(self._raw_run(part, 9, FAINT))

    def _raw_run(self, text, size, color):
        r = self.el("w:r")
        rpr = self.el("w:rPr")
        rpr.append(self.el("w:color", val=color))
        rpr.append(self.el("w:sz", val=int(size * 2)))
        r.append(rpr)
        t = self.OxmlElement("w:t")
        t.set(self.qn("xml:space"), "preserve")
        t.text = text
        r.append(t)
        return r

    # -- texte en ligne ---------------------------------------------------

    def runs(self, p, runs, size=None, color=None, bold=None, italic=None):
        Pt, RGB = self.Pt, self.RGBColor.from_string
        hl, hl_url = None, None
        for spec in runs:
            if spec["br"]:
                p.add_run().add_break()
                hl = None
                continue
            if not spec["text"]:
                continue
            r = p.add_run(spec["text"])
            f = r.font
            if spec["b"] or bold:
                f.bold = True
            if spec["i"] or italic:
                f.italic = True
            if spec["s"]:
                f.strike = True
            if spec["code"]:
                f.name = MONO
                f.size = Pt(round((size or 11) * 0.9 * 2) / 2)
                f.color.rgb = RGB(INDIGO_D)
            else:
                if size:
                    f.size = Pt(size)
                if spec["link"]:
                    f.color.rgb = RGB(INDIGO)
                    f.underline = True
                elif color:
                    f.color.rgb = RGB(color)
            if spec["code"]:
                self.put(r._r.get_or_add_rPr(), self.shd(CODE_BG), RPR_ORDER)
            if spec["link"]:
                if hl is None or hl_url != spec["link"]:
                    rid = p.part.relate_to(spec["link"], self.RT.HYPERLINK, is_external=True)
                    hl = self.OxmlElement("w:hyperlink")
                    hl.set(self.qn("r:id"), rid)
                    r._r.addprevious(hl)
                    hl_url = spec["link"]
                hl.append(r._r)
            else:
                hl = None

    def text_lines(self, p, text, **kw):
        for k, line in enumerate(text.split("\n")):
            if k:
                p.add_run().add_break()
            self.runs(p, parse_inline(line, ui=self.ui), **kw)

    # -- blocs ------------------------------------------------------------

    def title(self, text: str):
        p = self.doc.add_paragraph(style="Title")
        self.put(self.p_pr(p), self.borders("w:pBdr", bottom=(12, INDIGO, 6)), PPR_ORDER)
        self.runs(p, parse_inline(text, ui=self.ui))
        p.paragraph_format.space_after = self.Pt(14)

    def label(self, text: str):
        p = self.doc.add_paragraph()
        p.paragraph_format.space_before = self.Pt(12)
        p.paragraph_format.keep_with_next = True
        r = p.add_run(text.upper())
        r.font.size, r.font.bold = self.Pt(9), True
        r.font.color.rgb = self.RGBColor.from_string(INDIGO)

    def heading(self, level: int, text: str):
        p = self.doc.add_paragraph(style="Heading %d" % max(1, min(4, level)))
        self.runs(p, parse_inline(text, ui=self.ui))
        self.stats["sections"] += 1
        if len(self.stats["headings"]) < 6:
            self.stats["headings"].append(plain_text(text))

    def paragraph(self, text: str, quote=False):
        p = self.doc.add_paragraph()
        if quote:
            self.put(self.p_pr(p), self.borders("w:pBdr", left=(18, INDIGO_200, 10)), PPR_ORDER)
            p.paragraph_format.left_indent = self.Cm(0.45)
            self.text_lines(p, text, color=QUOTE_TXT, italic=True)
        else:
            self.text_lines(p, text)
        return p

    def hr(self):
        p = self.doc.add_paragraph()
        self.put(self.p_pr(p), self.borders("w:pBdr", bottom=(6, LINE, 1)), PPR_ORDER)
        p.paragraph_format.space_after = self.Pt(10)

    def code(self, text: str):
        p = self.doc.add_paragraph()
        ppr = self.p_pr(p)
        self.put(ppr, self.borders("w:pBdr", left=(18, INDIGO_200, 8), top=(2, CODE_BG, 4), bottom=(2, CODE_BG, 4), right=(2, CODE_BG, 4)), PPR_ORDER)
        self.put(ppr, self.shd(CODE_BG), PPR_ORDER)
        f = p.paragraph_format
        f.space_before, f.space_after, f.line_spacing = self.Pt(4), self.Pt(10), 1.0
        f.left_indent, f.right_indent = self.Cm(0.3), self.Cm(0.2)
        r = p.add_run(text or " ")
        r.font.name = MONO
        r.font.size = self.Pt(9.5)
        r.font.color.rgb = self.RGBColor.from_string("0F172A")

    # -- listes -----------------------------------------------------------

    def _numbering(self):
        if self.numbering is None:
            self.numbering = self.doc.part.numbering_part.element
        return self.numbering

    def _ids(self, tag, attr):
        vals = [int(e.get(self.qn("w:" + attr))) for e in self._numbering().findall(self.qn(tag)) if e.get(self.qn("w:" + attr), "").isdigit()]
        return max(vals) + 1 if vals else 1

    def _abstract(self, t0: str, t1: str) -> int:
        key = (t0, t1)
        if key in self.abstract_cache:
            return self.abstract_cache[key]
        aid = self._ids("w:abstractNum", "abstractNumId")

        def lvl(il, typ):
            if typ == "bullet":
                fmt, txt, bold = "bullet", "•" if il == 0 else "◦", 0
            else:
                fmt, txt, bold = ("decimal" if il == 0 else "lowerLetter"), "%%%d." % (il + 1), 1
            return (
                '<w:lvl w:ilvl="%d"><w:start w:val="1"/><w:numFmt w:val="%s"/><w:lvlText w:val="%s"/><w:lvlJc w:val="left"/>'
                '<w:pPr><w:ind w:left="%d" w:hanging="300"/></w:pPr>'
                '<w:rPr><w:rFonts w:ascii="%s" w:hAnsi="%s"/><w:b w:val="%d"/><w:color w:val="%s"/></w:rPr></w:lvl>'
                % (il, fmt, txt, 400 + il * 400, FONT, FONT, bold, INDIGO)
            )

        xml = '<w:abstractNum %s w:abstractNumId="%d"><w:multiLevelType w:val="hybridMultilevel"/>%s%s</w:abstractNum>' % (
            self.nsdecls("w"), aid, lvl(0, t0), lvl(1, t1))
        el = self.parse_xml(xml)
        nums = self._numbering().findall(self.qn("w:num"))
        if nums:
            nums[0].addprevious(el)
        else:
            self._numbering().append(el)
        self.abstract_cache[key] = aid
        return aid

    def _num(self, aid: int, start: int) -> int:
        nid = self._ids("w:num", "numId")
        xml = ('<w:num %s w:numId="%d"><w:abstractNumId w:val="%d"/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="%d"/>'
               '</w:lvlOverride></w:num>') % (self.nsdecls("w"), nid, aid, max(1, start))
        self._numbering().append(self.parse_xml(xml))
        return nid

    def list_block(self, items: List[Dict[str, Any]], quote=False):
        base = items[0]["indent"]
        groups: List[List[Dict[str, Any]]] = []
        for it in items:
            it["level"] = 0 if it["indent"] <= base + 1 else 1
            if it["level"] == 0 and groups and groups[-1][0]["ordered"] != it["ordered"]:
                groups.append([it])
            elif not groups:
                groups.append([it])
            else:
                groups[-1].append(it)
        for g in groups:
            t0 = "num" if g[0]["ordered"] else "bullet"
            subs = [x for x in g if x["level"] == 1]
            t1 = "num" if subs and subs[0]["ordered"] else "bullet"
            nid = self._num(self._abstract(t0, t1), g[0]["num"] if g[0]["ordered"] else 1)
            for k, it in enumerate(g):
                p = self.doc.add_paragraph()
                ppr = self.p_pr(p)
                numpr = self.el("w:numPr")
                numpr.append(self.el("w:ilvl", val=it["level"]))
                numpr.append(self.el("w:numId", val=nid))
                self.put(ppr, numpr, PPR_ORDER)
                if quote:
                    self.put(ppr, self.borders("w:pBdr", left=(18, INDIGO_200, 10)), PPR_ORDER)
                nxt = g[k + 1] if k + 1 < len(g) else None
                p.paragraph_format.space_after = self.Pt(3 if nxt else 8)
                text = it["text"]
                if it["check"] is not None:
                    text = ("☒ " if it["check"] else "☐ ") + text
                self.text_lines(p, text, color=QUOTE_TXT if quote else None, italic=True if quote else None)

    # -- tableaux ---------------------------------------------------------

    def table(self, header: List[str], aligns: List[str], rows: List[List[str]]):
        Pt, Cm, qn = self.Pt, self.Cm, self.qn
        nc = len(header)
        t = self.doc.add_table(rows=1 + len(rows), cols=nc)
        try:
            t.style = self.doc.styles["Table Grid"]
        except KeyError:
            pass
        t.alignment = self.TBL_LEFT
        t.autofit = False
        tblpr = t._tbl.tblPr
        self.put(tblpr, self.el("w:tblW", w=int(TEXT_W_CM / 2.54 * 1440), type="dxa"), TBLPR_ORDER)
        self.put(tblpr, self.borders("w:tblBorders", top=(4, LINE, 0), left=(4, LINE, 0), bottom=(4, LINE, 0),
                                     right=(4, LINE, 0), insideH=(4, LINE, 0), insideV=(4, LINE, 0)), TBLPR_ORDER)
        mar = self.el("w:tblCellMar")
        for side, v in (("top", 70), ("left", 110), ("bottom", 70), ("right", 110)):
            mar.append(self.el("w:" + side, w=v, type="dxa"))
        self.put(tblpr, mar, TBLPR_ORDER)
        weights = []
        for c in range(nc):
            longest = max([len(plain_text(header[c]))] + [len(plain_text(r[c])) for r in rows] + [4])
            weights.append(min(longest, 45) + 4)
        total = float(sum(weights))
        widths = [max(TEXT_W_CM * 0.08, TEXT_W_CM * w / total) for w in weights]
        scale = TEXT_W_CM / sum(widths)
        widths = [w * scale for w in widths]
        grid = t._tbl.tblGrid
        for k, gc in enumerate(grid.findall(qn("w:gridCol"))):
            gc.set(qn("w:w"), str(int(widths[k] / 2.54 * 1440)))
        tr0 = t.rows[0]._tr
        trpr = tr0.get_or_add_trPr()
        trpr.append(self.el("w:cantSplit"))
        trpr.append(self.el("w:tblHeader"))
        for ri, row in enumerate([header] + rows):
            for ci in range(nc):
                cell = t.cell(ri, ci)
                cell.width = Cm(widths[ci])
                tcpr = cell._tc.get_or_add_tcPr()
                if ri == 0:
                    self.put(tcpr, self.shd(INDIGO), TCPR_ORDER)
                elif ri % 2 == 0:
                    self.put(tcpr, self.shd(ZEBRA), TCPR_ORDER)
                self.put(tcpr, self.el("w:vAlign", val="center"), TCPR_ORDER)
                p = cell.paragraphs[0]
                p.alignment = self.ALIGN[aligns[ci]]
                f = p.paragraph_format
                f.space_before, f.space_after, f.line_spacing = Pt(0), Pt(0), 1.1
                runs = parse_inline(row[ci], ui=self.ui)
                if ri == 0:
                    self.runs(p, runs, size=10, color="FFFFFF", bold=True)
                else:
                    self.runs(p, runs, size=10)
        spacer = self.doc.add_paragraph()
        spacer.paragraph_format.space_after = Pt(4)
        self.stats["tables"] += 1

    # -- assemblage -------------------------------------------------------

    def blocks(self, blocks: List[Dict[str, Any]], quote=False):
        for b in blocks:
            t = b["t"]
            if t == "h":
                if quote:
                    p = self.paragraph(b["text"], quote=True)
                    for r in p.runs:
                        r.font.bold = True
                else:
                    self.heading(b["level"], b["text"])
            elif t == "p":
                self.paragraph(b["text"], quote=quote)
            elif t == "list":
                self.list_block(b["items"], quote=quote)
            elif t == "table":
                self.table(b["header"], b["aligns"], b["rows"])
            elif t == "code":
                self.code(b["text"])
            elif t == "quote":
                self.blocks(b["blocks"], quote=True)
            elif t == "hr":
                self.hr()

    def save(self, title: str) -> bytes:
        body = self.doc.element.body
        last = [c for c in body if not c.tag.endswith("}sectPr")]
        if last and last[-1].tag.endswith("}tbl"):
            self.doc.add_paragraph()
        cp = self.doc.core_properties
        cp.title = title
        cp.author = "Flash Word"
        cp.last_modified_by = "Flash Word"
        cp.comments = ""
        cp.subject = ""
        cp.keywords = ""
        cp.revision = 1
        cp.language = self.ui["lang_tag"]
        buf = io.BytesIO()
        self.doc.save(buf)
        return buf.getvalue()


def normalize_levels(blocks: List[Dict[str, Any]]) -> None:
    levels = [b["level"] for b in blocks if b["t"] == "h"]
    if levels:
        shift = min(levels) - 1
        for b in blocks:
            if b["t"] == "h":
                b["level"] = max(1, b["level"] - shift)


def pick_title(blocks: List[Dict[str, Any]], question: str, ui) -> str:
    if blocks and blocks[0]["t"] == "h":
        return plain_text(blocks.pop(0)["text"])[:120]
    q = re.sub(r"\s+", " ", question or "").strip()
    if q and len(q) <= 120 and "\n" not in (question or "").strip():
        return plain_text(q)
    for b in blocks:
        if b["t"] == "p":
            first = re.split(r"(?<=[.!?:])\s", plain_text(b["text"].split("\n")[0]))[0].rstrip(":").strip()
            if 0 < len(first) <= 90:
                return first
            break
    return ui["untitled"]


def build_docx(entries: List[Tuple[str, str]], question: str, lang: str, header_footer: bool) -> Tuple[bytes, str, Dict[str, Any]]:
    ui = UI.get(lang, UI["en"])
    tm = time.localtime()
    date_txt = ("%d %s %d" % (tm.tm_mday, ui["months"][tm.tm_mon - 1], tm.tm_year)) if lang == "fr" else (
        "%s %d, %d" % (ui["months"][tm.tm_mon - 1], tm.tm_mday, tm.tm_year))
    parsed = [(role, parse_blocks(text)) for role, text in entries]
    if len(parsed) == 1:
        title = pick_title(parsed[0][1], question, ui)
        normalize_levels(parsed[0][1])
    else:
        for _, bl in parsed:
            normalize_levels(bl)
        title = plain_text(question)[:120] if question and len(question) <= 120 else ui["untitled"]
    w = DocxWriter(lang, ui)
    if header_footer:
        w.header_footer(title, date_txt)
    w.title(title)
    for role, bl in parsed:
        if len(parsed) > 1:
            w.label(ui["question"] if role == "user" else ui["answer"])
        w.blocks(bl)
    data = w.save(title)
    words = sum(len(re.findall(r"\w+", plain_text(t))) for _, t in entries)
    stats = {**w.stats, "words": words}
    return data, title, stats, preview_html(parsed, title, date_txt, ui, header_footer)


# --------------------------------------------------------------------------
# Aperçu HTML du document (même analyse que le .docx)
# --------------------------------------------------------------------------


def inline_html(s: str, ui) -> str:
    out = []
    for r in parse_inline(s, ui=ui):
        if r["br"]:
            out.append("<br>")
            continue
        t = html.escape(r["text"], quote=False)
        if r["code"]:
            t = "<code>%s</code>" % t
        if r["link"]:
            t = '<span class="a">%s</span>' % t  # affiché, non cliquable dans l'aperçu
        if r["s"]:
            t = "<s>%s</s>" % t
        if r["i"]:
            t = "<em>%s</em>" % t
        if r["b"]:
            t = "<strong>%s</strong>" % t
        out.append(t)
    return "".join(out)


def lines_html(text: str, ui) -> str:
    return "<br>".join(inline_html(line, ui) for line in text.split("\n"))


def _list_html(items: List[Dict[str, Any]], ui) -> str:
    base = items[0]["indent"]
    groups: List[List[Dict[str, Any]]] = []
    for it in items:
        lvl = 0 if it["indent"] <= base + 1 else 1
        if lvl == 0 and groups and groups[-1][0]["ordered"] != it["ordered"]:
            groups.append([])
        if not groups:
            groups.append([])
        groups[-1].append({**it, "lvl": lvl})
    out = []

    def item_text(it):
        t = lines_html(it["text"], ui)
        if it["check"] is not None:
            t = ("\u2612 " if it["check"] else "\u2610 ") + t
        return t

    for g in groups:
        tag0 = "ol" if g[0]["ordered"] else "ul"
        start = ' start="%d"' % g[0]["num"] if g[0]["ordered"] and g[0]["num"] != 1 else ""
        out.append("<%s%s>" % (tag0, start))
        k = 0
        while k < len(g):
            it = g[k]
            out.append("<li>%s" % item_text(it))
            k += 1
            subs = []
            while k < len(g) and g[k]["lvl"] == 1:
                subs.append(g[k])
                k += 1
            if subs:
                tag1 = "ol" if subs[0]["ordered"] else "ul"
                out.append('<%s class="sub">%s</%s>' % (tag1, "".join("<li>%s</li>" % item_text(x) for x in subs), tag1))
            out.append("</li>")
        out.append("</%s>" % tag0)
    return "".join(out)


def blocks_html(blocks: List[Dict[str, Any]], ui) -> str:
    out = []
    for b in blocks:
        t = b["t"]
        if t == "h":
            out.append('<div class="h%d">%s</div>' % (max(1, min(4, b["level"])), inline_html(b["text"], ui)))
        elif t == "p":
            out.append("<p>%s</p>" % lines_html(b["text"], ui))
        elif t == "list":
            out.append(_list_html(b["items"], ui))
        elif t == "table":
            al = {"l": "left", "c": "center", "r": "right"}
            head = "".join('<th style="text-align:%s">%s</th>' % (al[a], inline_html(c, ui)) for c, a in zip(b["header"], b["aligns"]))
            rows = "".join("<tr>%s</tr>" % "".join('<td style="text-align:%s">%s</td>' % (al[a], inline_html(c, ui))
                                                   for c, a in zip(r, b["aligns"])) for r in b["rows"])
            out.append('<div class="tw"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (head, rows))
        elif t == "code":
            out.append("<pre>%s</pre>" % html.escape(b["text"], quote=False))
        elif t == "quote":
            out.append("<blockquote>%s</blockquote>" % blocks_html(b["blocks"], ui))
        elif t == "hr":
            out.append("<hr>")
    return "".join(out)


def preview_html(parsed, title: str, date_txt: str, ui, header_footer: bool) -> str:
    esc = lambda x: html.escape(str(x), quote=True)
    out = []
    if header_footer:
        out.append('<div class="ph"><span>%s</span><span>%s</span></div>' % (esc(title[:90]), esc(date_txt)))
    out.append('<div class="title">%s</div>' % inline_html(title, ui))
    for role, bl in parsed:
        if len(parsed) > 1:
            out.append('<div class="lbl">%s</div>' % esc(ui["question"] if role == "user" else ui["answer"]))
        out.append(blocks_html(bl, ui))
    return "".join(out)


# --------------------------------------------------------------------------
# Carte affichée dans le chat
# --------------------------------------------------------------------------

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

ICON_DOC = ('<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="1.8" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
            '<path d="M14 3v5h5"/><path d="M8.5 13h7"/><path d="M8.5 16.5h5"/></svg>')
ICON_DL = ('<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
           'stroke-linejoin="round" aria-hidden="true"><path d="M12 4v11"/><path d="M7 10l5 5 5-5"/><path d="M5 20h14"/></svg>')

PAGE = """<!DOCTYPE html>
<html lang="__LANG__"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
html,body{margin:0;padding:0;background:transparent}
body{padding:6px;font-family:Calibri,"Segoe UI",system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;color:#0F172A}
.card{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:14px 16px;background:#fff;border:1px solid #E2E8F0;border-radius:14px;box-shadow:0 6px 18px rgba(15,23,42,.07)}
.ic{flex:0 0 46px;width:46px;height:46px;border-radius:12px;background:linear-gradient(135deg,#4F46E5,#06B6D4);display:flex;align-items:center;justify-content:center}
.tx{flex:1 1 220px;min-width:0}
.tt{font-size:15px;font-weight:700;color:#1E1B4B;line-height:1.3;overflow-wrap:anywhere}
.st{font-size:12px;color:#64748B;margin-top:3px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.chip{font-size:11px;color:#4338CA;background:#EEF2FF;border-radius:999px;padding:3px 9px;max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dl{display:inline-flex;align-items:center;gap:7px;padding:9px 15px;border-radius:999px;background:linear-gradient(90deg,#4F46E5,#06B6D4);color:#fff;font-size:12.5px;font-weight:600;text-decoration:none;box-shadow:0 4px 14px rgba(79,70,229,.28);white-space:nowrap}
.dl:hover{filter:brightness(1.06)}
.box{background:#fff;border:1px solid #E2E8F0;border-radius:14px;box-shadow:0 6px 18px rgba(15,23,42,.07);overflow:hidden}
.box .card{border:0;border-radius:0;box-shadow:none;border-bottom:1px solid #E2E8F0}
.viewer{position:relative;background:#F1F5F9}
.scroll{max-height:440px;overflow:auto;padding:18px 18px 34px}
.fade{position:absolute;left:0;right:0;bottom:0;height:34px;background:linear-gradient(rgba(241,245,249,0),#F1F5F9);pointer-events:none}
.page{max-width:720px;margin:0 auto;background:#fff;border-radius:4px;box-shadow:0 2px 10px rgba(15,23,42,.10);padding:26px 34px 30px;font-family:Calibri,Carlito,"Segoe UI",system-ui,sans-serif;font-size:13.5px;line-height:1.45;color:#1F2937;overflow-wrap:break-word}
.ph{display:flex;justify-content:space-between;gap:12px;font-size:10.5px;color:#64748B;border-bottom:1px solid #E2E8F0;padding-bottom:5px;margin-bottom:16px}
.ph span:last-child{color:#94A3B8;white-space:nowrap}
.title{font-size:26px;font-weight:700;color:#1E1B4B;line-height:1.2;border-bottom:2px solid #4F46E5;padding-bottom:6px;margin-bottom:14px}
.h1{font-size:20px;font-weight:700;color:#1E1B4B;margin:18px 0 6px}
.h2{font-size:16.5px;font-weight:700;color:#4338CA;margin:14px 0 5px}
.h3{font-size:14.5px;font-weight:700;color:#1E1B4B;margin:11px 0 4px}
.h4{font-size:13.5px;font-weight:700;color:#1F2937;margin:9px 0 3px}
.lbl{font-size:11px;font-weight:700;color:#4F46E5;letter-spacing:.04em;text-transform:uppercase;margin:14px 0 4px}
.page p{margin:0 0 8px}
.page ul,.page ol{margin:0 0 9px;padding-left:24px}
.page li{margin:0 0 3px}
.page li::marker{color:#4F46E5;font-weight:700}
.page ul.sub,.page ol.sub{margin:3px 0 0}
.page ol.sub{list-style:lower-alpha}
.page ul.sub{list-style:circle}
.page code{font-family:Consolas,"Cascadia Mono",ui-monospace,monospace;font-size:.9em;color:#4338CA;background:#F1F5F9;border-radius:3px;padding:0 3px}
.page pre{font-family:Consolas,"Cascadia Mono",ui-monospace,monospace;font-size:12px;line-height:1.35;background:#F1F5F9;border-left:3px solid #C7D2FE;padding:9px 12px;margin:4px 0 10px;white-space:pre-wrap;color:#0F172A}
.page blockquote{margin:0 0 9px;padding:2px 0 2px 12px;border-left:3px solid #C7D2FE;color:#475569;font-style:italic}
.page hr{border:0;border-top:1px solid #E2E8F0;margin:10px 0 12px}
.page .a{color:#4F46E5;text-decoration:underline}
.tw{overflow-x:auto;margin:2px 0 10px}
.page table{border-collapse:collapse;width:100%;font-size:12.5px}
.page th{background:#4F46E5;color:#fff;font-weight:700;padding:5px 8px;border:1px solid #E2E8F0}
.page td{padding:5px 8px;border:1px solid #E2E8F0;vertical-align:middle}
.page tbody tr:nth-child(even) td{background:#F8FAFC}
</style></head><body>
<div class="box"><div class="card"><div class="ic">__DOCICON__</div><div class="tx"><div class="tt">__TITLE__</div><div class="st">__META__</div></div>__DL__</div>
<div class="viewer"><div class="scroll"><div class="page">__PREVIEW__</div></div><div class="fade"></div></div></div>
</body></html>"""


def slugify(s: str) -> str:
    a = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()
    a = re.sub(r"[^a-z0-9]+", "-", a).strip("-")[:50].strip("-")
    return a or "document"


def build_card(data: bytes, title: str, stats: Dict[str, Any], lang: str, preview: str = "") -> str:
    ui = UI.get(lang, UI["en"])
    esc = lambda s: html.escape(str(s), quote=True)

    def plural(n, key):
        sing, plur = ui[key]
        return "%d %s" % (n, sing if n == 1 else plur)

    meta = []
    if stats["sections"]:
        meta.append(plural(stats["sections"], "w_section"))
    if stats["tables"]:
        meta.append(plural(stats["tables"], "w_table"))
    meta.append(plural(stats["words"], "w_word"))
    uri = "data:%s;base64,%s" % (DOCX_MIME, base64.b64encode(data).decode("ascii"))
    dl = '<a class="dl" download="%s.docx" href="%s">%s%s</a>' % (esc(slugify(title)), uri, ICON_DL, esc(ui["download"]))
    return (
        PAGE.replace("__LANG__", esc(lang))
        .replace("__DOCICON__", ICON_DOC)
        .replace("__TITLE__", esc(title))
        .replace("__META__", esc(" · ".join(meta)))
        .replace("__DL__", dl)
        .replace("__PREVIEW__", preview)
    )


# --------------------------------------------------------------------------
# Action Open WebUI
# --------------------------------------------------------------------------


class Action:
    class Valves(BaseModel):
        priority: int = Field(default=0, description="Ordre d'affichage du bouton (plus petit = plus à gauche).")
        MESSAGE_COUNT: int = Field(default=1, description="Nombre de messages récents exportés (1 = la réponse cliquée seule).")
        HEADER_FOOTER: bool = Field(default=True, description="En-tête (titre, date) et pied de page (Page X / Y).")
        MIN_TEXT_LENGTH: int = Field(default=20, description="Longueur minimale du texte (caractères).")
        LANGUAGE: str = Field(default="fr", description="Langue de l'interface et du document (fr, en).")
        SHOW_STATUS: bool = Field(default=True, description="Afficher la progression dans le chat.")

    def __init__(self):
        self.valves = self.Valves()

    @staticmethod
    def _text_of(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for it in content:
                if isinstance(it, dict) and it.get("type") == "text":
                    parts.append(it.get("text", ""))
                elif isinstance(it, str):
                    parts.append(it)
            return "\n".join(parts)
        return str(content) if content else ""

    def _select(self, body: dict, count: int) -> Tuple[List[Tuple[str, str]], str]:
        messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
        if not messages:
            return [], ""
        idx = len(messages) - 1
        mid = body.get("id")
        if mid:
            for i, m in enumerate(messages):
                if m.get("id") == mid:
                    idx = i
                    break
        question = ""
        for m in reversed(messages[:idx]):
            if m.get("role") == "user":
                question = clean_source(self._text_of(m.get("content")))
                break
        entries = []
        for m in messages[max(0, idx - max(count, 1) + 1) : idx + 1]:
            t = clean_source(self._text_of(m.get("content")))
            if t:
                entries.append((m.get("role") or "assistant", t))
        return entries, question

    async def _emit_status(self, emit, text: str, done: bool, v) -> None:
        if v.SHOW_STATUS and emit:
            await emit({"type": "status", "data": {"description": text, "done": done}})

    async def _toast(self, emit, text: str, kind: str = "info") -> None:
        if emit:
            await emit({"type": "notification", "data": {"type": kind, "content": text}})

    async def action(
        self,
        body: dict,
        __user__: Optional[Dict[str, Any]] = None,
        __event_emitter__: Optional[Any] = None,
    ) -> Any:
        v = self.valves.model_copy()  # le module est partagé entre requêtes
        emit = __event_emitter__
        if not emit:
            return body
        lang = (v.LANGUAGE or "fr").split("-")[0].lower()
        ui = UI.get(lang, UI["en"])
        t0 = time.monotonic()

        entries, question = self._select(body, v.MESSAGE_COUNT)
        if sum(len(t) for _, t in entries) < max(1, v.MIN_TEXT_LENGTH):
            await self._toast(emit, ui["empty"], "warning")
            return body

        try:
            await self._toast(emit, ui["toast_start"], "info")
            await self._emit_status(emit, ui["starting"], False, v)
            data, title, stats, preview = await asyncio.to_thread(build_docx, entries, question, lang, v.HEADER_FOOTER)
            card = build_card(data, title, stats, lang, preview)
            await self._emit_status(emit, ui["done"], True, v)
            await self._toast(emit, ui["toast_done"], "success")
            logger.info(
                "flash_word: ok sections=%d tables=%d words=%d size=%dko duration=%.2fs",
                stats["sections"], stats["tables"], stats["words"], len(data) // 1024, time.monotonic() - t0,
            )
            return HTMLResponse(content=card, headers={"Content-Disposition": "inline"})
        except Exception as e:
            logger.error("flash_word: erreur %s duration=%.2fs", type(e).__name__, time.monotonic() - t0)
            await self._emit_status(emit, ui["failed"], True, v)
            await self._toast(emit, ui["failed"], "error")
            return body
