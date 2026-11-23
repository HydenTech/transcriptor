#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Met en page des fiches Markdown en document Word (.docx).

    python generate_docx.py fiches.md --sortie "Psychologie - Fiches résumés.docx"
        [--consignes "Fiches matière.md"] [--titre "Psychologie"] [--sous-titre "…"]
        [--couleur "#E2F0D9"] [--etiquette "UE 2C"] [--pied "…"] [--cache DOSSIER]

Conventions lues dans le Markdown (celles des consignes du mode année) :
- `##` catégorie, `###` sous-catégorie, `####` point précis : code couleur
  réglé par `docx.couleurs` — par niveau (rose, bleu, violet, vert…) ou une
  couleur par catégorie ;
- **gras** : mots-clés ;
- `> **Définition** :`, `> **Exemple** :`, `> **À retenir** :`,
  `> **Attention** :`, `> **Conseil** :` : encadrés colorés ;
- tableaux Markdown : tableaux Word, en-tête à la couleur de la section ;
- bloc ```mermaid : schéma dessiné par Chromium (mermaid.js) et inséré en
  image, suivi de sa légende `*Schéma — …*` ;
- `![légende](chemin)` : image locale.

Le nom de la matière ouvre le document dans un bandeau à la couleur de
l'horaire. Sortie : une ligne JSON sur stdout (fichier écrit, schémas…).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

import markdown

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entete  # noqa: E402
from annee import couleur_normale, foncee, luminance, melanger, texte_sur  # noqa: E402
from generate_pdf import aerer  # noqa: E402

import docx  # noqa: E402  (python-docx, installé à la demande par l'application)
from docx.enum.section import WD_ORIENT  # noqa: E402
from docx.enum.table import WD_TABLE_ALIGNMENT  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Cm, Pt, RGBColor  # noqa: E402

APP_DIR = Path(__file__).resolve().parents[1]
MERMAID_URLS = (
    "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js",
    "https://unpkg.com/mermaid@11/dist/mermaid.min.js",
)
MERMAID_NPM = "https://registry.npmjs.org/mermaid/latest"

# Ordre imposé par le schéma OOXML des enfants de ces éléments : Word refuse
# d'ouvrir un document dont les propriétés sont dans le désordre.
_ORDRE = {
    "pPr": ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl",
            "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens",
            "kinsoku", "wordWrap", "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN",
            "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind", "contextualSpacing",
            "mirrorIndents", "suppressOverlap", "jc", "textDirection", "textAlignment",
            "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange"],
    "rPr": ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike",
            "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish",
            "webHidden", "color", "spacing", "w", "kern", "position", "sz", "szCs", "highlight",
            "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang",
            "eastAsianLayout", "specVanish", "oMath"],
    "tcPr": ["cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap",
             "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark"],
    "tblPr": ["tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize",
              "tblStyleColBandSize", "tblW", "jc", "tblCellSpacing", "tblInd", "tblBorders",
              "shd", "tblLayout", "tblCellMar", "tblLook", "tblCaption", "tblDescription"],
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def poser(parent, nom: str, **attributs: str):
    """Ajoute (ou remplace) l'enfant w:`nom` de `parent` à sa place dans
    l'ordre du schéma, avec ces attributs w:… ; renvoie l'élément."""
    ordre = _ORDRE[_local(parent.tag)]
    for ancien in parent.findall(qn(f"w:{nom}")):
        parent.remove(ancien)
    el = OxmlElement(f"w:{nom}")
    for k, v in attributs.items():
        el.set(qn(f"w:{k}"), str(v))
    suivants = set(ordre[ordre.index(nom) + 1:])
    for i, enfant in enumerate(parent):
        if _local(enfant.tag) in suivants:
            parent.insert(i, el)
            return el
    parent.append(el)
    return el


def _hex(c: str) -> str:
    return (couleur_normale(c) or "#000000")[1:]


def rgb(c: str) -> RGBColor:
    return RGBColor.from_string(_hex(c))


# ----------------------------------------------------------------------
# HTML -> arbre (Python-Markdown produit un HTML propre)
# ----------------------------------------------------------------------
_VIDES = {"br", "hr", "img", "input", "meta", "link", "col", "source", "wbr"}


class Noeud:
    __slots__ = ("tag", "attrs", "enfants", "parent")

    def __init__(self, tag: str, attrs: Optional[dict] = None, parent: Optional["Noeud"] = None):
        self.tag, self.attrs, self.enfants, self.parent = tag, attrs or {}, [], parent

    def texte(self) -> str:
        return "".join(e if isinstance(e, str) else e.texte() for e in self.enfants)

    def blocs(self) -> list["Noeud"]:
        return [e for e in self.enfants if isinstance(e, Noeud)]


class _Lecteur(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.racine = Noeud("racine")
        self.courant = self.racine

    def handle_starttag(self, tag, attrs):
        n = Noeud(tag, dict(attrs), self.courant)
        self.courant.enfants.append(n)
        if tag not in _VIDES:
            self.courant = n

    def handle_startendtag(self, tag, attrs):
        self.courant.enfants.append(Noeud(tag, dict(attrs), self.courant))

    def handle_endtag(self, tag):
        n = self.courant
        while n is not self.racine and n.tag != tag:
            n = n.parent
        if n is not self.racine:
            self.courant = n.parent

    def handle_data(self, data):
        self.courant.enfants.append(data)


def arbre(html: str) -> Noeud:
    lecteur = _Lecteur()
    lecteur.feed(html)
    lecteur.close()
    return lecteur.racine


# ----------------------------------------------------------------------
# Schémas mermaid
# ----------------------------------------------------------------------
_RE_MERMAID = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*mermaid[^\n]*\n(.*?)^[ \t]*\1[ \t]*$",
                         re.M | re.S)
_MARQUE = "SCHEMATRANSCRIPTOR{}Z"


def normaliser_listes(md: str) -> str:
    """Python-Markdown n'imbrique une liste qu'à 4 espaces ; Claude en met
    souvent 2. Chaque niveau d'imbrication est ramené à 4 espaces."""
    sortie: list[str] = []
    pile: list[int] = []
    cloture = None
    for ligne in md.split("\n"):
        m = re.match(r"^\s*(`{3,}|~{3,})", ligne)
        if cloture is not None:
            sortie.append(ligne)
            if m and m.group(1)[0] * 3 == cloture:
                cloture = None
            continue
        if m:
            cloture = m.group(1)[0] * 3
            pile = []
            sortie.append(ligne)
            continue
        m = re.match(r"^( *)([-*+]|\d{1,3}[.)])\s+\S", ligne)
        if m:
            retrait = len(m.group(1))
            while pile and retrait < pile[-1]:
                pile.pop()
            if not pile or retrait > pile[-1]:
                pile.append(retrait)
            ligne = " " * (4 * (len(pile) - 1)) + ligne[retrait:]
        elif ligne.strip() and not ligne.startswith(" "):
            pile = []
        elif ligne.strip() and pile:
            ligne = " " * (4 * len(pile)) + ligne.lstrip()   # suite d'un élément
        sortie.append(ligne)
    return "\n".join(sortie)


def extraire_schemas(md: str) -> tuple[str, list[str]]:
    codes: list[str] = []

    def garder(m: re.Match) -> str:
        codes.append(m.group(2).strip())
        return "\n\n" + _MARQUE.format(len(codes) - 1) + "\n\n"
    return _RE_MERMAID.sub(garder, md), codes


def trouver_mermaid(cache: Optional[Path]) -> Optional[Path]:
    """mermaid.min.js : fourni avec l'application, déjà téléchargé, ou
    téléchargé une fois pour toutes dans le cache."""
    candidats = [Path(os.environ["TRANSCRIPTOR_MERMAID"])] if os.environ.get("TRANSCRIPTOR_MERMAID") else []
    candidats.append(APP_DIR / "static" / "vendor" / "mermaid.min.js")
    if cache:
        candidats.append(cache / "mermaid.min.js")
    for c in candidats:
        if c.is_file() and c.stat().st_size > 500_000:
            return c
    if not cache or os.environ.get("TRANSCRIPTOR_HORS_LIGNE"):
        return None
    cache.mkdir(parents=True, exist_ok=True)
    cible = cache / "mermaid.min.js"
    for url in MERMAID_URLS:
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                donnees = r.read()
            if len(donnees) > 500_000 and b"mermaid" in donnees[:4000]:
                tmp = cible.with_suffix(".tmp")
                tmp.write_bytes(donnees)
                os.replace(tmp, cible)
                return cible
        except Exception:  # noqa: BLE001
            continue
    try:                                    # dernier recours : le paquet npm
        with urllib.request.urlopen(MERMAID_NPM, timeout=30) as r:
            archive = json.loads(r.read())["dist"]["tarball"]
        with urllib.request.urlopen(archive, timeout=300) as r, \
                tarfile.open(fileobj=r, mode="r|gz") as tar:
            for membre in tar:
                if membre.name == "package/dist/mermaid.min.js":
                    tmp = cible.with_suffix(".tmp")
                    tmp.write_bytes(tar.extractfile(membre).read())
                    os.replace(tmp, cible)
                    return cible
    except Exception:  # noqa: BLE001
        pass
    return None


_JS_RENDU = r"""
async ({code, vars, id}) => {
  const zone = document.getElementById('zone');
  zone.innerHTML = '';
  mermaid.initialize({startOnLoad: false, securityLevel: 'strict', theme: 'base',
    themeVariables: vars, fontFamily: vars.fontFamily,
    flowchart: {useMaxWidth: false, htmlLabels: true, curve: 'basis', padding: 14,
                nodeSpacing: 40, rankSpacing: 46},
    mindmap: {useMaxWidth: false, padding: 14}, timeline: {useMaxWidth: false},
    sequence: {useMaxWidth: false}, gantt: {useMaxWidth: false}, quadrantChart: {useMaxWidth: false}});
  try { await mermaid.parse(code); } catch (e) { return {erreur: String((e && e.message) || e)}; }
  try {
    const {svg} = await mermaid.render(id, code);
    zone.innerHTML = svg;
  } catch (e) { return {erreur: String((e && e.message) || e)}; }
  const el = zone.querySelector('svg');
  if (!el) return {erreur: 'aucun dessin produit'};
  const r = el.getBoundingClientRect();
  return {largeur: r.width, hauteur: r.height};
}
"""


class Dessinateur:
    """Chromium ouvert à la première demande, fermé à la fin : les schémas
    d'un document se dessinent dans la même page."""

    def __init__(self, cache: Optional[Path], dossier: Path, police: str):
        self.cache, self.dossier, self.police = cache, dossier, police
        self._pw = self._nav = self._page = None
        self.indisponible = ""
        self.n = 0

    def _ouvrir(self) -> bool:
        if self._page is not None:
            return True
        if self.indisponible:
            return False
        js = trouver_mermaid(self.cache)
        if not js:
            self.indisponible = "mermaid.js introuvable (connexion ?)"
            return False
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            erreur: Optional[Exception] = None
            # Le Chromium de Playwright, sinon Chrome ou Edge déjà installés.
            for options in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
                try:
                    chemin = os.environ.get("TRANSCRIPTOR_CHROMIUM")
                    if chemin and not options:
                        options = {"executable_path": chemin}
                    self._nav = self._pw.chromium.launch(args=["--no-sandbox"], **options)
                    break
                except Exception as exc:  # noqa: BLE001
                    erreur = exc
            if self._nav is None:
                raise erreur or RuntimeError("Chromium indisponible")
            self._page = self._nav.new_page(viewport={"width": 1800, "height": 1400},
                                            device_scale_factor=2)
            self._page.set_content(
                "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
                "body{margin:0;background:#fff}#zone{display:inline-block;padding:6px}"
                "</style></head><body><div id='zone'></div></body></html>")
            self._page.add_script_tag(path=str(js))
            return True
        except Exception as exc:  # noqa: BLE001
            self.indisponible = f"Chromium indisponible ({str(exc).splitlines()[0][:120]})"
            self.fermer()
            return False

    def dessiner(self, code: str, fort: str, clair: str, palette: list[tuple[str, str]]
                 ) -> tuple[Optional[Path], str]:
        """(image PNG, erreur)."""
        if not self._ouvrir():
            return None, self.indisponible
        variables = {
            "fontFamily": f"{self.police}, Carlito, 'Segoe UI', Arial, sans-serif",
            "fontSize": "16px", "background": "#ffffff",
            "primaryColor": clair, "primaryBorderColor": fort, "primaryTextColor": "#1f1f1f",
            "secondaryColor": melanger(clair, "#ffffff", 0.6), "secondaryBorderColor": fort,
            "secondaryTextColor": "#1f1f1f", "tertiaryColor": "#ffffff",
            "tertiaryBorderColor": fort, "tertiaryTextColor": "#1f1f1f",
            "lineColor": fort, "textColor": "#1f1f1f", "mainBkg": clair, "nodeBorder": fort,
            "clusterBkg": melanger(clair, "#ffffff", 0.5), "clusterBorder": fort,
            "edgeLabelBackground": "#ffffff", "titleColor": fort,
        }
        for i in range(12):
            f, c = palette[i % len(palette)]
            variables[f"cScale{i}"] = melanger(f, c, 0.28)
            variables[f"cScaleLabel{i}"] = "#1f1f1f"
            variables[f"cScalePeer{i}"] = f
        self.n += 1
        essais = [code]
        # Une chaîne très longue de gauche à droite deviendrait illisible une
        # fois réduite à la largeur de la page : on la redessine de haut en bas.
        if re.match(r"\s*(flowchart|graph)\s+LR\b", code):
            essais.append(re.sub(r"^(\s*(?:flowchart|graph)\s+)LR\b", r"\1TD", code, count=1))
        try:
            mesure = None
            for i, essai in enumerate(essais):
                mesure = self._page.evaluate(_JS_RENDU, {"code": essai, "vars": variables,
                                                         "id": f"schema{self.n}_{i}"})
                if mesure.get("erreur") or i == len(essais) - 1:
                    break
                if mesure["largeur"] <= 900 or mesure["largeur"] < 2.6 * mesure["hauteur"]:
                    break
            if mesure.get("erreur"):
                return None, mesure["erreur"].splitlines()[0][:160]
            self.dossier.mkdir(parents=True, exist_ok=True)
            png = self.dossier / f"schema_{self.n:02d}.png"
            self._page.locator("#zone").screenshot(path=str(png))
            return png, ""
        except Exception as exc:  # noqa: BLE001
            return None, str(exc).splitlines()[0][:160]

    def fermer(self) -> None:
        for objet, methode in ((self._nav, "close"), (self._pw, "stop")):
            try:
                if objet is not None:
                    getattr(objet, methode)()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._nav = self._page = None


def schema_en_texte(code: str) -> str:
    """Repli quand un schéma ne peut pas être dessiné : ses étiquettes, dans
    l'ordre, reliées par des flèches."""
    vus: list[str] = []
    for m in re.finditer(r"[\[\(\{>]{1,3}\s*\"?([^\]\)\}\"\n]{2,80}?)\"?\s*[\]\)\}]{1,3}", code):
        t = " ".join(m.group(1).split())
        if t and t not in vus:
            vus.append(t)
    if not vus:   # mindmap / timeline : une étiquette par ligne
        for ligne in code.splitlines()[1:]:
            t = re.sub(r"^\s*(root|section|title)\b", "", ligne).strip(" :()[]{}\"")
            if t and not t.startswith("%%") and t not in vus:
                vus.append(t)
    return " → ".join(vus[:14])


# ----------------------------------------------------------------------
# Couleurs : code couleur par niveau ou par catégorie
# ----------------------------------------------------------------------
class CodeCouleur:
    def __init__(self, reglages: dict):
        self.mode = reglages["couleurs"]
        self.palette = [entete.couleur_palette(n) for n in reglages["palette"]] or \
            [entete.PALETTE_DOCX["bleu"]]
        self.categorie = -1
        self.niveau_courant = 1

    def nouvelle_categorie(self) -> None:
        self.categorie += 1

    def niveau(self, n: int) -> tuple[str, str]:
        """(couleur forte, fond clair) d'un titre de niveau n (1 = catégorie)."""
        if self.mode == "categories":
            return self.palette[max(self.categorie, 0) % len(self.palette)]
        return self.palette[(max(n, 1) - 1) % len(self.palette)]

    def ici(self) -> tuple[str, str]:
        """La couleur de la section où l'on écrit (tableaux, encadrés)."""
        return self.niveau(self.niveau_courant)


# Encadrés : libellé -> (genre, couleur de bordure, fond). None = couleur de la section.
_ENCADRES = (
    (("definition", "definitions", "notion", "concept"), "definition", "#546E7A", "#F1F4F6"),
    (("exemple", "exemples", "illustration", "cas", "situation", "vignette", "en pratique"),
     "exemple", "#E0A100", "#FFF8E1"),
    (("a retenir", "retenir", "l essentiel", "essentiel", "en bref", "en une phrase", "synthese",
      "resume", "idee cle", "idee clef"), "retenir", None, None),
    (("attention", "piege", "pieges", "erreur frequente", "a eviter", "important", "vigilance",
      "danger"), "attention", "#C62828", "#FFEBEE"),
    (("conseil", "conseils", "astuce", "methode", "memo", "pour reviser", "truc"),
     "conseil", "#2E7D32", "#E8F5E9"),
    (("citation",), "citation", "#8D8D8D", "#F7F7F7"),
)


def genre_encadre(libelle: str) -> tuple[str, Optional[str], Optional[str]]:
    s = entete.sans_accent(libelle).strip(" :.-—–").replace("'", " ").replace("’", " ")
    s = " ".join(s.split())
    for cles, genre, bord, fond in _ENCADRES:
        if any(s == c or s.startswith(c + " ") or s.startswith(c + "(") for c in cles):
            return genre, bord, fond
    return "", None, None


# ----------------------------------------------------------------------
# Le document
# ----------------------------------------------------------------------
_RE_ESPACES = re.compile(r"[ \t\r\n]+")


def typographie(texte: str) -> str:
    """Espaces insécables du français (avant : ; ! ? », après «)."""
    texte = re.sub(r" ([:;!?»])", " \\1", texte)
    return re.sub(r"« ", "« ", texte)


class Format:
    __slots__ = ("gras", "italique", "code", "barre", "exposant", "indice", "couleur", "taille")

    def __init__(self, **k: Any):
        for s in self.__slots__:
            setattr(self, s, k.get(s))

    def avec(self, **k: Any) -> "Format":
        f = Format(**{s: getattr(self, s) for s in self.__slots__})
        for cle, v in k.items():
            setattr(f, cle, v)
        return f


class Redacteur:
    def __init__(self, reglages: dict, *, titre: str, surtitre: str, sous_titre: str,
                 couleur_matiere: str, etiquette: str, pied: str, base: Path,
                 cache: Optional[Path], dossier_images: Path):
        self.r = reglages
        self.base = base
        self.couleurs = CodeCouleur(reglages)
        self.police = reglages["police"] or "Calibri"
        self.taille = float(reglages["taille_texte"])
        self.matiere = couleur_normale(couleur_matiere) or self.couleurs.palette[0][1]
        self.titre, self.surtitre, self.sous_titre = titre, surtitre, sous_titre
        self.etiquette, self.pied = etiquette, pied
        self.dessinateur = Dessinateur(cache, dossier_images, self.police) if reglages["schemas"] else None
        self.avertissements: list[str] = []
        self.schemas = self.schemas_rates = 0
        self.doc = docx.Document()
        self.largeur = self._mise_en_page()
        self._styles()
        self.premiere_categorie = True
        self.base_titres = 2
        self.codes: list[str] = []
        self.conteneur = self.doc            # corps, ou cellule d'un encadré
        self._cellule_vierge = False
        self.largeur_dispo = self.largeur

    def _para(self, style: Optional[str] = None):
        """Nouveau paragraphe dans le conteneur courant (le premier paragraphe,
        vide, d'une cellule neuve est réutilisé)."""
        if self.conteneur is not self.doc and self._cellule_vierge:
            self._cellule_vierge = False
            p = self.conteneur.paragraphs[0]
            if style:
                p.style = self.doc.styles[style]
            return p
        return self.conteneur.add_paragraph(style=style) if style else self.conteneur.add_paragraph()

    def _table(self, lignes: int, colonnes: int, largeurs: list[float]):
        """Tableau à largeurs fixes (grille, cellules et tableau renseignés :
        Word et LibreOffice lisent chacun l'un ou l'autre)."""
        if self.conteneur is self.doc:
            t = self.doc.add_table(rows=lignes, cols=colonnes)
        else:
            t = self.conteneur.add_table(rows=lignes, cols=colonnes)
            self._cellule_vierge = False
        t.autofit = False
        tblpr = t._tbl.tblPr
        poser(tblpr, "tblW", w=str(round(sum(largeurs) * 567)), type="dxa")
        poser(tblpr, "tblLayout", type="fixed")
        for col, cm in zip(t._tbl.tblGrid.findall(qn("w:gridCol")), largeurs):
            col.set(qn("w:w"), str(round(cm * 567)))
        for ligne in t.rows:
            for cel, cm in zip(ligne.cells, largeurs):
                cel.width = Cm(cm)
        return t

    # -- page et styles ---------------------------------------------------
    def _mise_en_page(self) -> float:
        sec = self.doc.sections[0]
        l, h = {"A3": (29.7, 42.0), "A4": (21.0, 29.7), "A5": (14.8, 21.0),
                "Letter": (21.59, 27.94), "Legal": (21.59, 35.56)}[self.r["format"]]
        if self.r["orientation"] == "paysage":
            l, h = h, l
            sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width, sec.page_height = Cm(l), Cm(h)
        valeurs = [self._cm(v) for v in self.r["marges"].split()]
        if len(valeurs) == 1:
            valeurs *= 4
        elif len(valeurs) == 2:
            valeurs = [valeurs[0], valeurs[1], valeurs[0], valeurs[1]]
        elif len(valeurs) == 3:
            valeurs = [valeurs[0], valeurs[1], valeurs[2], valeurs[1]]
        haut, droite, bas, gauche = valeurs[:4]
        sec.top_margin, sec.right_margin = Cm(haut), Cm(droite)
        sec.bottom_margin, sec.left_margin = Cm(bas), Cm(gauche)
        sec.header_distance, sec.footer_distance = Cm(0.9), Cm(0.9)
        return l - gauche - droite

    @staticmethod
    def _cm(v: str) -> float:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(mm|cm|in|pt)", v)
        n, u = float(m.group(1)), m.group(2)
        return n * {"mm": 0.1, "cm": 1, "in": 2.54, "pt": 2.54 / 72}[u]

    def _police(self, element_rpr, taille: Optional[float] = None) -> None:
        polices = poser(element_rpr, "rFonts", ascii=self.police, hAnsi=self.police,
                        eastAsia=self.police, cs=self.police)
        del polices  # attributs de thème retirés : la police choisie s'applique
        if taille:
            poser(element_rpr, "sz", val=str(round(taille * 2)))
            poser(element_rpr, "szCs", val=str(round(taille * 2)))

    def _styles(self) -> None:
        st = self.doc.styles
        normal = st["Normal"]
        rpr = normal.element.get_or_add_rPr()
        self._police(rpr, self.taille)
        poser(rpr, "lang", val="fr-BE", eastAsia="fr-BE", bidi="ar-SA")
        normal.font.color.rgb = RGBColor(0x22, 0x22, 0x22)
        pf = normal.paragraph_format
        pf.space_before, pf.space_after, pf.line_spacing = Pt(0), Pt(4), 1.12

        tailles = {1: (self.taille + 4.5, 14, 8), 2: (self.taille + 2.5, 12, 4),
                   3: (self.taille + 1, 9, 3), 4: (self.taille + 0.5, 7, 2)}
        for n, (taille, avant, apres) in tailles.items():
            s = st[f"Heading {n}"]
            rpr = s.element.get_or_add_rPr()
            self._police(rpr, taille)
            s.font.bold = True
            s.font.italic = n == 4
            s.font.color.rgb = RGBColor(0x26, 0x26, 0x26)
            p = s.paragraph_format
            p.space_before, p.space_after = Pt(avant), Pt(apres)
            p.keep_with_next = True
            p.line_spacing = 1.0
        for nom in ("List Bullet", "List Bullet 2", "List Bullet 3", "List Number",
                    "List Number 2", "List Number 3", "List Continue", "List Continue 2",
                    "List Continue 3"):
            p = st[nom].paragraph_format
            p.space_before, p.space_after = Pt(0), Pt(2)
        cap = st["Caption"]
        self._police(cap.element.get_or_add_rPr(), self.taille - 1.5)
        cap.font.italic, cap.font.bold = True, False
        cap.font.color.rgb = RGBColor(0x5F, 0x63, 0x68)
        cap.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.space_before, cap.paragraph_format.space_after = Pt(2), Pt(10)
        for nom in ("Header", "Footer"):
            self._police(st[nom].element.get_or_add_rPr(), 8.5)
            st[nom].paragraph_format.tab_stops.clear_all()

    # -- éléments de mise en forme -------------------------------------------
    @staticmethod
    def fond(paragraphe, couleur: str) -> None:
        poser(paragraphe._p.get_or_add_pPr(), "shd", val="clear", color="auto", fill=_hex(couleur))

    @staticmethod
    def bordures(paragraphe, **cotes: tuple[str, int, int]) -> None:
        """cotes : gauche/haut/droite/bas = (couleur, épaisseur en 1/8 pt, espace en pt)."""
        pbdr = poser(paragraphe._p.get_or_add_pPr(), "pBdr")
        for cote in ("top", "left", "bottom", "right"):
            francais = {"top": "haut", "left": "gauche", "bottom": "bas", "right": "droite"}[cote]
            if francais in cotes:
                couleur, epaisseur, espace = cotes[francais]
                el = OxmlElement(f"w:{cote}")
                for k, v in (("val", "single"), ("sz", epaisseur), ("space", espace),
                             ("color", _hex(couleur))):
                    el.set(qn(f"w:{k}"), str(v))
                pbdr.append(el)

    def runs(self, paragraphe, noeud: Noeud, fmt: Optional[Format] = None,
             sauter: int = 0) -> None:
        """Le contenu en ligne de `noeud` (gras, italique, code, liens…) dans
        `paragraphe`. `sauter` : nombre d'enfants à ignorer au début."""
        fmt = fmt or Format()
        for enfant in noeud.enfants[sauter:]:
            if isinstance(enfant, str):
                texte = typographie(_RE_ESPACES.sub(" ", enfant))
                if texte:
                    self._run(paragraphe, texte, fmt)
                continue
            t = enfant.tag
            if t in ("strong", "b"):
                self.runs(paragraphe, enfant, fmt.avec(gras=True))
            elif t in ("em", "i", "cite"):
                self.runs(paragraphe, enfant, fmt.avec(italique=True))
            elif t == "code":
                self._run(paragraphe, enfant.texte(), fmt.avec(code=True))
            elif t in ("del", "s"):
                self.runs(paragraphe, enfant, fmt.avec(barre=True))
            elif t == "sup":
                self.runs(paragraphe, enfant, fmt.avec(exposant=True))
            elif t == "sub":
                self.runs(paragraphe, enfant, fmt.avec(indice=True))
            elif t == "br":
                paragraphe.add_run().add_break()
            elif t == "img":
                self._run(paragraphe, f"[{enfant.attrs.get('alt') or 'image'}]", fmt.avec(italique=True))
            elif t == "a":
                self.runs(paragraphe, enfant, fmt)
                lien = enfant.attrs.get("href", "")
                if lien.startswith("http") and lien.strip("/") not in enfant.texte():
                    self._run(paragraphe, f" ({lien})", fmt.avec(taille=self.taille - 2))
            else:
                self.runs(paragraphe, enfant, fmt)

    def _run(self, paragraphe, texte: str, fmt: Format):
        r = paragraphe.add_run(texte)
        if fmt.gras:
            r.bold = True
        if fmt.italique:
            r.italic = True
        if fmt.barre:
            r.font.strike = True
        if fmt.exposant:
            r.font.superscript = True
        if fmt.indice:
            r.font.subscript = True
        if fmt.code:
            r.font.name = "Consolas"
            r.font.size = Pt(self.taille - 1)
            r.font.color.rgb = RGBColor(0xA6, 0x1E, 0x4D)
        if fmt.couleur:
            r.font.color.rgb = rgb(fmt.couleur)
        if fmt.taille:
            r.font.size = Pt(fmt.taille)
        return r

    @staticmethod
    def nettoyer_bords(paragraphe) -> None:
        """Pas d'espace en tête ni en fin de paragraphe (sauts de ligne Markdown)."""
        runs = paragraphe.runs
        if runs and runs[0].text:
            runs[0].text = runs[0].text.lstrip()
        if runs and runs[-1].text:
            runs[-1].text = runs[-1].text.rstrip()

    # -- en-tête du document ------------------------------------------------
    def bandeau(self) -> None:
        fond = self.matiere
        sombre = luminance(fond) < 0.35
        encre = texte_sur(fond) if sombre else foncee(fond, 0.07)
        t = self._table(1, 1, [self.largeur])
        t.alignment = WD_TABLE_ALIGNMENT.LEFT
        cel = t.cell(0, 0)
        tcpr = cel._tc.get_or_add_tcPr()
        poser(tcpr, "shd", val="clear", color="auto", fill=_hex(fond))
        bords = poser(tcpr, "tcBorders")
        for cote in ("top", "bottom", "right"):
            el = OxmlElement(f"w:{cote}")
            el.set(qn("w:val"), "nil")
            bords.append(el)
        gauche = OxmlElement("w:left")
        for k, v in (("val", "single"), ("sz", "48"), ("space", "0"),
                     ("color", _hex(foncee(fond, 0.2) if not sombre else fond))):
            gauche.set(qn(f"w:{k}"), v)
        bords.insert(1, gauche)
        marges = poser(tcpr, "tcMar")
        for cote, v in (("top", 200), ("left", 260), ("bottom", 200), ("right", 260)):
            el = OxmlElement(f"w:{cote}")
            el.set(qn("w:w"), str(v))
            el.set(qn("w:type"), "dxa")
            marges.append(el)
        self._tableau_sans_bordure(t)

        p = cel.paragraphs[0]
        ligne = " · ".join(x for x in (self.surtitre.upper(), self.etiquette) if x)
        if ligne:
            r = p.add_run(ligne)
            r.bold, r.font.size = True, Pt(8.5)
            r.font.color.rgb = rgb(encre)
            poser(r._r.get_or_add_rPr(), "spacing", val="30")
            p.paragraph_format.space_after = Pt(3)
        p2 = cel.add_paragraph()
        r = p2.add_run(self.titre)
        r.bold, r.font.size = True, Pt(self.taille + 12)
        r.font.color.rgb = rgb(encre)
        p2.paragraph_format.space_after = Pt(2)
        p2.paragraph_format.line_spacing = 1.0
        if self.sous_titre:
            p3 = cel.add_paragraph()
            r = p3.add_run(self.sous_titre)
            r.font.size = Pt(self.taille - 1)
            r.font.color.rgb = rgb(encre if sombre else melanger(encre, fond, 0.8))
            p3.paragraph_format.space_after = Pt(0)
        espace = self.doc.add_paragraph()
        espace.paragraph_format.space_after = Pt(6)

    def _tableau_sans_bordure(self, t) -> None:
        tblpr = t._tbl.tblPr
        bords = poser(tblpr, "tblBorders")
        for cote in ("top", "left", "bottom", "right", "insideH", "insideV"):
            el = OxmlElement(f"w:{cote}")
            el.set(qn("w:val"), "nil")
            bords.append(el)

    def sommaire(self, titres: list[tuple[int, str]]) -> None:
        """Sommaire des catégories (et sous-catégories) : un champ TOC dont le
        contenu initial est déjà écrit. Dans Word, « Mettre à jour la table »
        y ajoute les numéros de page."""
        categories = [t for n, t in titres if n == 1]
        if len(categories) < 2:
            return
        p = self.doc.add_paragraph()
        r = p.add_run("SOMMAIRE")
        r.bold, r.font.size = True, Pt(8.5)
        r.font.color.rgb = RGBColor(0x5F, 0x63, 0x68)
        poser(r._r.get_or_add_rPr(), "spacing", val="30")
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.keep_with_next = True

        code = CodeCouleur(self.r)
        premiers = True
        entrees = [(n, t) for n, t in titres if n <= 2]
        for i, (n, texte) in enumerate(entrees):
            e = self.doc.add_paragraph()
            pf = e.paragraph_format
            pf.space_after = Pt(1 if n == 2 else 2)
            pf.space_before = Pt(3 if n == 1 and not premiers else 0)
            pf.left_indent = Cm(0 if n == 1 else 0.7)
            if premiers:
                self._champ(e, "begin")
                instr = e.add_run()
                el = OxmlElement("w:instrText")
                el.set(qn("xml:space"), "preserve")
                el.text = ' TOC \\o "1-2" \\h \\z \\u '
                instr._r.append(el)
                self._champ(e, "separate")
                premiers = False
            if n == 1:
                code.nouvelle_categorie()
            fort, _ = code.niveau(n)
            if n == 1:
                puce = e.add_run("■  ")
                puce.font.color.rgb = rgb(fort)
                puce.font.size = Pt(self.taille - 1)
            r = e.add_run(texte)
            r.bold = n == 1
            r.font.size = Pt(self.taille - (0 if n == 1 else 1))
            r.font.color.rgb = rgb(fort if n == 1 else "#4A4F57")
            if i == len(entrees) - 1:
                self._champ(e, "end")
        fin = self.doc.add_paragraph()
        fin.paragraph_format.space_after = Pt(4)

    @staticmethod
    def _champ(paragraphe, genre: str) -> None:
        r = paragraphe.add_run()
        el = OxmlElement("w:fldChar")
        el.set(qn("w:fldCharType"), genre)
        r._r.append(el)

    def entete_pied(self) -> None:
        sec = self.doc.sections[0]
        sec.different_first_page_header_footer = True
        fond = self.matiere
        trait = melanger(fond, foncee(fond, 0.2), 0.45) if luminance(fond) > 0.35 else fond
        h = sec.header.paragraphs[0]
        h.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        r = h.add_run(" · ".join(x for x in (self.titre, self.etiquette) if x))
        r.font.size = Pt(8.5)
        r.font.color.rgb = RGBColor(0x5F, 0x63, 0x68)
        self.bordures(h, bas=(trait, 12, 2))

        for pied in (sec.footer, sec.first_page_footer):
            p = pied.paragraphs[0]
            p.paragraph_format.tab_stops.add_tab_stop(Cm(self.largeur), WD_TAB_ALIGNMENT.RIGHT)
            r = p.add_run(self.pied)
            r.font.size = Pt(8)
            r.font.color.rgb = RGBColor(0x80, 0x86, 0x8F)
            p.add_run("\t")
            for morceau in ("Page ", "PAGE", " / ", "NUMPAGES"):
                if morceau in ("PAGE", "NUMPAGES"):
                    self._champ(p, "begin")
                    ri = p.add_run()
                    el = OxmlElement("w:instrText")
                    el.set(qn("xml:space"), "preserve")
                    el.text = f" {morceau} "
                    ri._r.append(el)
                    self._champ(p, "separate")
                    rv = p.add_run("1")
                    rv.font.size = Pt(8)
                    rv.font.color.rgb = RGBColor(0x80, 0x86, 0x8F)
                    self._champ(p, "end")
                else:
                    rr = p.add_run(morceau)
                    rr.font.size = Pt(8)
                    rr.font.color.rgb = RGBColor(0x80, 0x86, 0x8F)

    # -- blocs ----------------------------------------------------------------
    def rediger(self, racine: Noeud) -> None:
        blocs = racine.blocs()
        niveaux = [int(b.tag[1]) for b in blocs if re.fullmatch(r"h[1-6]", b.tag)]
        # Le plus haut niveau de titre présent sert de « catégorie ».
        self.base_titres = min(niveaux) if niveaux else 2
        titres = [(int(b.tag[1]) - self.base_titres + 1, " ".join(b.texte().split()))
                  for b in blocs if re.fullmatch(r"h[1-6]", b.tag)]
        if self.r["sommaire"]:
            self.sommaire(titres)
        self.suite(blocs)

    def suite(self, blocs: list[Noeud]) -> None:
        i = 0
        while i < len(blocs):
            suivant = blocs[i + 1] if i + 1 < len(blocs) else None
            i += 2 if self.bloc(blocs[i], suivant) else 1

    def bloc(self, b: Noeud, suivant: Optional[Noeud]) -> bool:
        """Écrit un bloc ; renvoie True si le bloc suivant (légende) a été utilisé."""
        t = b.tag
        if re.fullmatch(r"h[1-6]", t):
            if self.conteneur is self.doc:
                self.titre_section(int(t[1]) - self.base_titres + 1, b)
            else:                                   # titre dans un encadré : simple gras
                p = self._para()
                self.runs(p, b, Format(gras=True))
                self.nettoyer_bords(p)
        elif t == "p":
            marque = re.fullmatch(r"SCHEMATRANSCRIPTOR(\d+)Z", b.texte().strip())
            if marque:
                return self.schema(int(marque.group(1)), suivant)
            images = [e for e in b.blocs() if e.tag == "img"]
            if images and not b.texte().strip():
                return self.image_locale(images[0], suivant)
            self.paragraphe(b)
        elif t in ("ul", "ol"):
            self.liste(b, 0, t == "ol")
        elif t == "blockquote":
            self.encadre(b)
        elif t == "table":
            self.tableau(b)
        elif t == "pre":
            self.code(b)
        elif t in ("div", "details", "section", "article"):
            self.suite(b.blocs())
        elif t == "summary":
            p = self._para()
            self.runs(p, b, Format(gras=True))
            self.nettoyer_bords(p)
        elif t != "hr":
            texte = " ".join(b.texte().split())
            if texte:
                self._run(self._para(), texte, Format())
        return False

    def titre_section(self, n: int, noeud: Noeud) -> None:
        n = max(1, min(4, n))
        if n == 1:
            self.couleurs.nouvelle_categorie()
        self.couleurs.niveau_courant = n
        fort, clair = self.couleurs.niveau(n)
        p = self._para(f"Heading {n}")
        if n == 1 and self.r["nouvelle_page"] and not self.premiere_categorie:
            p.paragraph_format.page_break_before = True
        if n == 1:
            self.premiere_categorie = False
        self.runs(p, noeud, Format(couleur=fort))
        self.nettoyer_bords(p)
        if n == 1:
            self.fond(p, clair)
            self.bordures(p, gauche=(fort, 36, 6))
            p.paragraph_format.left_indent = Cm(0.15)
        elif n == 2:
            self.bordures(p, bas=(fort, 8, 2))
        elif n == 3:
            self.bordures(p, gauche=(fort, 18, 5))
            p.paragraph_format.left_indent = Cm(0.1)

    def paragraphe(self, noeud: Noeud):
        p = self._para()
        self.runs(p, noeud)
        self.nettoyer_bords(p)
        return p

    def liste(self, noeud: Noeud, profondeur: int, ordonnee: bool) -> None:
        niveau = min(profondeur, 2)
        suffixe = "" if niveau == 0 else f" {niveau + 1}"
        style = ("List Number" if ordonnee else "List Bullet") + suffixe
        num = self._nouvelle_numerotation(style) if ordonnee else None
        for li in (e for e in noeud.blocs() if e.tag == "li"):
            p = self._para(style)
            if num is not None:
                numpr = poser(p._p.get_or_add_pPr(), "numPr")
                ilvl = OxmlElement("w:ilvl")
                ilvl.set(qn("w:val"), "0")
                numid = OxmlElement("w:numId")
                numid.set(qn("w:val"), str(num))
                numpr.append(ilvl)
                numpr.append(numid)
            courant: Optional[Any] = p             # paragraphe qui reçoit le texte en ligne
            en_ligne = Noeud("li")

            def vider() -> None:
                nonlocal courant, en_ligne
                if en_ligne.enfants and en_ligne.texte().strip():
                    cible = courant or self._para("List Continue" + suffixe)
                    self.runs(cible, en_ligne)
                    self.nettoyer_bords(cible)
                    courant = None
                en_ligne = Noeud("li")

            for e in li.enfants:
                if isinstance(e, Noeud) and e.tag in ("ul", "ol"):
                    vider()
                    courant = None
                    self.liste(e, profondeur + 1, e.tag == "ol")
                elif isinstance(e, Noeud) and e.tag == "p":
                    vider()
                    cible = courant or self._para("List Continue" + suffixe)
                    self.runs(cible, e)
                    self.nettoyer_bords(cible)
                    courant = None
                else:
                    en_ligne.enfants.append(e)
            vider()

    def _nouvelle_numerotation(self, style: str) -> Optional[int]:
        """Une liste numérotée repart à 1 : nouvelle instance de la
        numérotation du style, avec un redémarrage explicite."""
        try:
            numerotation = self.doc.part.numbering_part.numbering_definitions._numbering
            base = self.doc.styles[style].element.pPr.numPr.numId.val
            abstrait = numerotation.num_having_numId(base).abstractNumId.val
            num = numerotation.add_num(abstrait)
            surcharge = num.add_lvlOverride(ilvl=0)
            surcharge.add_startOverride(1)
            return num.numId
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _libelle(p: Noeud) -> tuple[str, int]:
        """(« Exemple », index du premier enfant après le libellé) si le
        paragraphe commence par un libellé en gras."""
        if p.tag != "p":
            return "", 0
        enfants = p.enfants
        k = 0
        while k < len(enfants) and isinstance(enfants[k], str) and not enfants[k].strip():
            k += 1
        if k < len(enfants) and isinstance(enfants[k], Noeud) and enfants[k].tag in ("strong", "b"):
            return " ".join(enfants[k].texte().split()), k + 1
        return "", 0

    def encadre(self, bq: Noeud) -> None:
        """Python-Markdown réunit en un seul bloc des citations séparées par une
        ligne vide : chaque paragraphe qui s'ouvre sur un libellé connu
        (« Exemple », « À retenir »…) recommence donc un encadré."""
        groupes: list[list[Noeud]] = []
        for b in bq.blocs():
            libelle, _ = self._libelle(b)
            if not groupes or (libelle and genre_encadre(libelle)[0]):
                groupes.append([b])
            else:
                groupes[-1].append(b)
        for g in groupes:
            boite = Noeud("blockquote")
            boite.enfants = list(g)
            self._encadre(boite)

    def _encadre(self, bq: Noeud) -> None:
        """Un encadré : tableau d'une cellule, fond clair et filet à gauche ; le
        libellé en gras qui l'ouvre (« Exemple », « Définition »…) en donne la
        couleur."""
        blocs = bq.blocs()
        if not blocs:
            return
        premier = blocs[0]
        libelle, sauter = self._libelle(premier)
        genre, bord, fond = genre_encadre(libelle) if libelle else ("", None, None)
        fort, clair = self.couleurs.ici()
        if genre == "retenir":
            bord, fond = fort, clair
        elif not genre:
            bord, fond = "#9E9E9E", "#F5F5F5"

        retrait = 0.25 if self.conteneur is self.doc else 0.0
        largeur = self.largeur_dispo - retrait
        t = self._table(1, 1, [largeur])
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        self._tableau_sans_bordure(t)
        cel = t.cell(0, 0)
        tcpr = cel._tc.get_or_add_tcPr()
        poser(tcpr, "shd", val="clear", color="auto", fill=_hex(fond))
        bords = poser(tcpr, "tcBorders")
        for cote in ("top", "left", "bottom", "right"):
            el = OxmlElement(f"w:{cote}")
            if cote == "left":
                for k, v in (("val", "single"), ("sz", "24"), ("space", "0"), ("color", _hex(bord))):
                    el.set(qn(f"w:{k}"), v)
            else:
                el.set(qn("w:val"), "nil")
            bords.append(el)
        marges = poser(tcpr, "tcMar")
        for cote, v in (("top", 90), ("left", 170), ("bottom", 90), ("right", 150)):
            el = OxmlElement(f"w:{cote}")
            el.set(qn("w:w"), str(v))
            el.set(qn("w:type"), "dxa")
            marges.append(el)
        if len(blocs) <= 5 and len(bq.texte()) < 900:     # un petit encadré ne se coupe pas
            t.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))

        avant = (self.conteneur, self._cellule_vierge, self.largeur_dispo)
        self.conteneur, self._cellule_vierge = cel, True
        self.largeur_dispo = largeur - 0.6
        try:
            if premier.tag == "p":
                p = self._para()
                if libelle and genre:
                    r = p.add_run(libelle.rstrip(" :\u00a0") + "\u00a0: ")
                    r.bold = True
                    r.font.color.rgb = rgb(foncee(bord, 0.1))
                    reste = Noeud("p")
                    reste.enfants = list(premier.enfants[sauter:])
                    if reste.enfants and isinstance(reste.enfants[0], str):
                        reste.enfants[0] = re.sub(r"^\s*[:\-—–]\s*", "", reste.enfants[0])
                    self.runs(p, reste, Format(italique=True) if genre == "citation" else None)
                else:
                    self.runs(p, premier)
                self.nettoyer_bords(p)
                self.suite(blocs[1:])
            else:
                self.suite(blocs)
            for q in cel.paragraphs:
                q.paragraph_format.space_after = Pt(2)
            if cel.paragraphs:
                cel.paragraphs[-1].paragraph_format.space_after = Pt(0)
        finally:
            self.conteneur, self._cellule_vierge, self.largeur_dispo = avant
        self._apres_tableau()

    def _apres_tableau(self) -> None:
        """Un petit espace après un tableau (Word colle sinon le paragraphe
        suivant, ou fusionne deux tableaux consécutifs)."""
        p = self._para()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = Pt(7)
        r = p.add_run("")
        r.font.size = Pt(4)

    def tableau(self, noeud: Noeud) -> None:
        rangees: list[tuple[bool, list[Noeud]]] = []

        def collecter(n: Noeud, en_tete: bool) -> None:
            for e in n.blocs():
                if e.tag == "tr":
                    cellules = [c for c in e.blocs() if c.tag in ("td", "th")]
                    rangees.append((en_tete or all(c.tag == "th" for c in cellules), cellules))
                elif e.tag in ("thead", "tbody", "tfoot"):
                    collecter(e, e.tag == "thead")
        collecter(noeud, False)
        if not rangees:
            return
        ncol = max(len(c) for _, c in rangees)
        fort, clair = self.couleurs.ici()

        # Largeurs : proportionnelles au texte des colonnes, 2,2 cm au moins.
        largeur = self.largeur_dispo
        longueurs = []
        for j in range(ncol):
            textes = [len(" ".join(c[j].texte().split())) for _, c in rangees if j < len(c)]
            longueurs.append(max(4.0, 0.6 * max(textes or [4]) + 0.4 * (sum(textes) / max(1, len(textes)))))
        mini = min(2.2, largeur / ncol)
        largeurs = [max(mini, largeur * x / sum(longueurs)) for x in longueurs]
        for _ in range(4):                       # ramène le total à la largeur disponible
            exces = sum(largeurs) - largeur
            souples = [k for k, w in enumerate(largeurs) if w > mini + 0.01]
            if exces <= 0.01 or not souples:
                break
            for k in souples:
                largeurs[k] = max(mini, largeurs[k] - exces / len(souples))

        t = self._table(len(rangees), ncol, largeurs)
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        tblpr = t._tbl.tblPr
        bords = poser(tblpr, "tblBorders")
        for cote in ("top", "left", "bottom", "right", "insideH", "insideV"):
            el = OxmlElement(f"w:{cote}")
            for k, v in (("val", "single"), ("sz", "4"), ("space", "0"), ("color", "C9CDD2")):
                el.set(qn(f"w:{k}"), v)
            bords.append(el)
        marges = poser(tblpr, "tblCellMar")
        for cote, v in (("top", 45), ("left", 100), ("bottom", 45), ("right", 100)):
            el = OxmlElement(f"w:{cote}")
            el.set(qn("w:w"), str(v))
            el.set(qn("w:type"), "dxa")
            marges.append(el)

        for i, (est_entete, cellules) in enumerate(rangees):
            ligne = t.rows[i]
            trpr = ligne._tr.get_or_add_trPr()
            trpr.append(OxmlElement("w:cantSplit"))
            if est_entete:
                trpr.append(OxmlElement("w:tblHeader"))
            for j in range(ncol):
                cel = ligne.cells[j]
                p = cel.paragraphs[0]
                p.paragraph_format.space_after = Pt(1)
                p.paragraph_format.line_spacing = 1.05
                if j < len(cellules):
                    c = cellules[j]
                    alignement = c.attrs.get("style", "") + " " + c.attrs.get("align", "")
                    for k, morceau in enumerate(self._en_paragraphes(c)):
                        q = p if k == 0 else cel.add_paragraph()
                        q.paragraph_format.space_after = Pt(1)
                        if "center" in alignement:
                            q.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        elif "right" in alignement:
                            q.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                        self.runs(q, morceau, Format(gras=True, couleur="#FFFFFF",
                                                     taille=self.taille - 1) if est_entete
                                  else Format(taille=self.taille - 1))
                        self.nettoyer_bords(q)
                tcpr = cel._tc.get_or_add_tcPr()
                if est_entete:
                    poser(tcpr, "shd", val="clear", color="auto", fill=_hex(fort))
                elif i % 2 == 0:
                    poser(tcpr, "shd", val="clear", color="auto",
                          fill=_hex(melanger(clair, "#FFFFFF", 0.55)))
        self._apres_tableau()

    @staticmethod
    def _en_paragraphes(cellule: Noeud) -> list[Noeud]:
        """Le contenu d'une cellule, découpé en paragraphes (<p>, <li>)."""
        blocs = [e for e in cellule.enfants if isinstance(e, Noeud) and e.tag in ("p", "ul", "ol")]
        if not blocs:
            return [cellule]
        sortie: list[Noeud] = []
        for e in blocs:
            if e.tag == "p":
                sortie.append(e)
            else:
                for li in (x for x in e.blocs() if x.tag == "li"):
                    puce = Noeud("li")
                    puce.enfants = ["• ", *li.enfants]
                    sortie.append(puce)
        return sortie

    def code(self, noeud: Noeud) -> None:
        texte = noeud.texte().rstrip("\n")
        for ligne in texte.split("\n") or [""]:
            p = self._para()
            r = p.add_run(ligne or " ")
            r.font.name = "Consolas"
            r.font.size = Pt(self.taille - 1.5)
            self.fond(p, "#F4F5F7")
            pf = p.paragraph_format
            pf.space_after, pf.space_before, pf.line_spacing = Pt(0), Pt(0), 1.0
            pf.left_indent = Cm(0.3)

    # -- images ---------------------------------------------------------------
    def _legende(self, suivant: Optional[Noeud]) -> Optional[str]:
        """Une ligne en italique juste après l'image (« *Schéma — …* »)."""
        if suivant is None or suivant.tag != "p":
            return None
        utiles = [e for e in suivant.enfants if not (isinstance(e, str) and not e.strip())]
        if len(utiles) == 1 and isinstance(utiles[0], Noeud) and utiles[0].tag in ("em", "i"):
            return " ".join(utiles[0].texte().split())
        return None

    def _inserer_image(self, png: Path, largeur_css: Optional[float], legende: Optional[str]) -> None:
        from docx.image.image import Image as ImageDocx
        try:
            info = ImageDocx.from_file(str(png))
            px_l, px_h = info.px_width, info.px_height
            dpi = info.horz_dpi or 96
        except Exception:  # noqa: BLE001
            px_l, px_h, dpi = 1000, 600, 96
        # largeur naturelle en cm : px CSS (schémas dessinés à l'échelle 2) ou px/dpi
        naturelle = (largeur_css * 2.54 / 96) if largeur_css else px_l * 2.54 / max(dpi, 72)
        largeur = min(self.largeur_dispo - 0.4, max(4.0, naturelle))
        hauteur = largeur * px_h / max(px_l, 1)
        if hauteur > 17.5:
            largeur *= 17.5 / hauteur
        p = self._para()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(2 if legende else 8)
        p.paragraph_format.keep_with_next = bool(legende)
        p.add_run().add_picture(str(png), width=Cm(largeur))
        if legende:
            self._para("Caption").add_run(typographie(legende))

    def schema(self, i: int, suivant: Optional[Noeud]) -> bool:
        code = self.codes[i]
        legende = self._legende(suivant)
        png, erreur = (None, "schémas désactivés dans les consignes")
        if self.dessinateur:
            fort, clair = self.couleurs.ici()
            png, erreur = self.dessinateur.dessiner(code, fort, clair, self.couleurs.palette)
        if png:
            from docx.image.image import Image as ImageDocx
            px = ImageDocx.from_file(str(png)).px_width
            self._inserer_image(png, px / 2, legende)
            self.schemas += 1
            return legende is not None
        self.schemas_rates += 1
        self.avertissements.append(f"schéma {i + 1} non dessiné : {erreur}")
        fort, _ = self.couleurs.ici()
        p = self._para()
        r = p.add_run("Schéma : ")
        r.bold = True
        r.font.color.rgb = rgb(fort)
        self._run(p, schema_en_texte(code) or "(non disponible)", Format(italique=True))
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after = Pt(2 if legende else 6)
        if legende:
            self._para("Caption").add_run(typographie(legende))
        return legende is not None

    def image_locale(self, img: Noeud, suivant: Optional[Noeud]) -> bool:
        src = (img.attrs.get("src") or "").replace("\\", "/")
        chemin = (self.base / src).resolve() if not re.match(r"^[a-z]+:", src, re.I) else None
        legende = self._legende(suivant)
        if not chemin or not chemin.is_file() or chemin.suffix.lower() not in (
                ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"):
            p = self._para()
            self._run(p, f"[image introuvable : {src}]", Format(italique=True, couleur="#7A7F87"))
            return legende is not None
        self._inserer_image(chemin, None, legende or (img.attrs.get("alt") or None))
        return legende is not None

    # -- tout -------------------------------------------------------------------
    def produire(self, md: str) -> None:
        _, md, _ = entete.separer(md)
        md, self.codes = extraire_schemas(md)
        # Un titre « # » unique en tête : c'est le nom du document, déjà dans le bandeau.
        lignes = md.lstrip("\n").split("\n")
        h1 = [l for l in lignes if re.match(r"^#\s+\S", l)]
        if len(h1) == 1 and lignes and lignes[0] == h1[0]:
            if not self.titre:
                self.titre = re.sub(r"^#\s+|\s+#*\s*$", "", h1[0]).strip()
            md = "\n".join(lignes[1:])
        html = markdown.markdown(aerer(normaliser_listes(md)),
                                 extensions=["tables", "fenced_code", "sane_lists"])
        self.bandeau()
        self.entete_pied()
        try:
            self.rediger(arbre(html))
        finally:
            if self.dessinateur:
                self.dessinateur.fermer()
        paquet = self.doc.part.package
        for rid, rel in list(paquet.rels.items()):
            if rel.reltype.endswith("/thumbnail"):
                del paquet.rels[rid]
        cp = self.doc.core_properties
        cp.title = " — ".join(x for x in (self.titre, self.surtitre) if x)
        cp.author = "Transcriptor"
        cp.language = "fr-BE"

    def enregistrer(self, sortie: Path) -> Path:
        """Écrit à côté puis remplace : un document ouvert dans Word n'est pas
        corrompu ; s'il est verrouillé, la nouvelle version prend un autre nom."""
        sortie.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".docx", dir=str(sortie.parent))
        os.close(fd)
        try:
            self.doc.save(tmp)
            try:
                os.chmod(tmp, 0o644)            # mkstemp crée en 600 (sans effet sous Windows)
            except OSError:
                pass
            try:
                os.replace(tmp, sortie)
                return sortie
            except PermissionError:
                autre = sortie.with_name(f"{sortie.stem} (nouvelle version){sortie.suffix}")
                os.replace(tmp, autre)
                self.avertissements.append(f"{sortie.name} est ouvert dans Word : nouvelle version "
                                           f"écrite sous « {autre.name} »")
                return autre
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def main() -> int:
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="Met en page des fiches Markdown en document Word.")
    ap.add_argument("source")
    ap.add_argument("--sortie", required=True)
    ap.add_argument("--consignes", help="fichier de consignes dont l'en-tête docx: règle le document")
    ap.add_argument("--titre", default="")
    ap.add_argument("--surtitre")
    ap.add_argument("--sous-titre", default="")
    ap.add_argument("--couleur", default="", help="couleur de la matière (horaire)")
    ap.add_argument("--etiquette", default="", help="ex. « UE 2C », à côté du surtitre")
    ap.add_argument("--pied", default="")
    ap.add_argument("--cache", help="dossier où garder mermaid.min.js")
    ap.add_argument("--images", help="dossier des images de schémas (par défaut : à côté du .md)")
    a = ap.parse_args()

    src = Path(a.source)
    avert: list[str] = []
    if a.consignes:
        c = entete.lire(a.consignes)
        reglages = c.docx
        avert += [f"Consignes « {Path(a.consignes).stem} » : {x}" for x in c.avertissements]
    else:
        reglages, _ = entete.reglages_docx({})
    red = Redacteur(
        reglages, titre=a.titre, surtitre=a.surtitre if a.surtitre is not None else reglages["surtitre"],
        sous_titre=a.sous_titre, couleur_matiere=a.couleur, etiquette=a.etiquette,
        pied=a.pied or " — ".join(x for x in (a.titre, reglages["surtitre"]) if x),
        base=src.resolve().parent, cache=Path(a.cache) if a.cache else None,
        dossier_images=Path(a.images) if a.images else src.resolve().parent / "schemas")
    red.produire(src.read_text(encoding="utf-8-sig", errors="replace"))
    ecrit = red.enregistrer(Path(a.sortie))
    print(json.dumps({"sortie": str(ecrit), "schemas": red.schemas, "schemas_rates": red.schemas_rates,
                      "avertissements": avert + red.avertissements}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
