#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Met en page un support Markdown en PDF.

    python generate_pdf.py cours.md [--consignes modele.md] [--sortie cours.pdf] [--html]

Rien n'est supposé de la structure du document : le PDF rend ce que Claude a
produit en suivant les consignes, quelles qu'elles soient. Le bloc `pdf:` de
l'en-tête du fichier de consignes règle la mise en page (liste complète et
valeurs par défaut : entete.REGLAGES_PDF, et l'en-tête commenté du modèle
« Cours complet »). Sans --consignes, les réglages par défaut s'appliquent.

Reconnu automatiquement, quelle que soit la structure :
- les grandes sections : le plus haut niveau de titre répété au moins deux
  fois (ou `niveau_sections`), en bandeau, chacune sur une nouvelle page ;
- les cartes : titres contenant un mot-clé (`cartes`, « FICHE » par défaut),
  encadrés jusqu'au titre suivant de même niveau et jamais coupés ;
- les questions <details><summary>…</summary>…</details> : réponses
  visibles, masquées (fiche d'entraînement) ou regroupées en fin de document ;
- le titre de couverture : `{Champ}` lu dans un tableau « | Champ | Valeur | »
  du document, sinon le titre de niveau 1 s'il est unique, sinon le nom du
  fichier ;
- les formules $$…$$ sont préservées telles quelles, sans interprétation
  Markdown.

Rendu par Chromium (Playwright) pour garder les emojis en couleur.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import html as htmllib
import re
import sys
from pathlib import Path

import markdown

sys.path.insert(0, str(Path(__file__).resolve().parent))
import entete  # noqa: E402


# ----------------------------------------------------------------------
# Lecture du document
# ----------------------------------------------------------------------
_RE_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def _balayer(md: str):
    """(ligne, dans_un_bloc_de_code) pour chaque ligne."""
    ouverture = None
    for ligne in md.split("\n"):
        m = _RE_FENCE.match(ligne)
        if m:
            marque = m.group(1)[0] * 3
            if ouverture is None:
                ouverture = marque
                yield ligne, True
                continue
            if marque == ouverture:
                ouverture = None
                yield ligne, True
                continue
        yield ligne, ouverture is not None


def _plat(s: str) -> str:
    s = re.sub(r"[*_`]", "", s)
    return " ".join(entete.sans_accent(s).split())


def champs_du_document(md: str) -> dict[str, str]:
    """Champs lisibles dans le document : lignes de tableau « | Champ | Valeur | »
    et lignes « **Champ :** valeur ». La première occurrence l'emporte."""
    champs: dict[str, str] = {}
    for ligne, code in _balayer(md):
        if code:
            continue
        m = re.match(r"^\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|", ligne)
        if m and not re.fullmatch(r":?-{2,}:?", m.group(1)):
            champs.setdefault(_plat(m.group(1)), m.group(2).strip())
            continue
        m = re.match(r"^\s*(?:[-*]\s+)?\*\*([^*]{1,40}?)\s*:?\s*\*\*\s*:?\s*(\S.*)$", ligne)
        if m:
            champs.setdefault(_plat(m.group(1)), m.group(2).strip())
    return champs


def remplir(modele: str, champs: dict[str, str], extra: dict[str, str]) -> str:
    """Remplace les {Champ} ; les séparateurs orphelins (« · » d'un champ
    vide) disparaissent."""
    def valeur(m: re.Match) -> str:
        cle = _plat(m.group(1))
        return extra.get(cle) or champs.get(cle, "")

    s = re.sub(r"\{([^{}]+)\}", valeur, modele or "")
    s = re.sub(r"(\s*[·|•]\s*){2,}", " · ", s)
    s = re.sub(r"^[\s·|•,–—-]+|[\s·|•,–—-]+$", "", s)
    return s.strip()


def en_ligne(texte: str) -> str:
    """Markdown d'une ligne (gras, code…) vers HTML, sans paragraphe."""
    rendu = markdown.markdown(texte)
    return re.sub(r"^<p>|</p>$", "", rendu.strip())


def texte_brut(texte: str) -> str:
    return re.sub(r"[*_`]", "", texte).strip()


# ----------------------------------------------------------------------
# Préparation du Markdown
# ----------------------------------------------------------------------
def _genre(ligne: str, precedent: str) -> str:
    s = ligne.strip()
    if not s:
        return "vide"
    retrait = len(ligne) - len(ligne.lstrip())
    if re.match(r"^\s*([-*+]|\d{1,3}[.)])\s+", ligne):
        return "liste"
    if retrait and precedent in ("liste", "suite"):
        return "suite"
    if s.startswith(">"):
        return "citation"
    if s.startswith("|"):
        return "tableau"
    if s.startswith("#"):
        return "titre"
    if re.fullmatch(r"\*\*[^*]+\*\*\s*\S{0,3}", s):
        return "libelle"          # « **À retenir** », « **Piège fréquent** ⚡ »
    if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", s.replace(" ", "")):
        return "trait"
    return "texte"


# Transitions qui exigent une ligne vide pour que python-markdown ouvre un
# nouveau bloc. Claude écrit souvent « **À retenir**\n- point » : sans ligne
# vide, la liste resterait collée au paragraphe.
_SEPARER = {
    ("texte", "liste"), ("citation", "liste"), ("tableau", "liste"),
    ("texte", "citation"), ("liste", "citation"), ("suite", "citation"), ("tableau", "citation"),
    ("texte", "tableau"), ("liste", "tableau"), ("suite", "tableau"), ("citation", "tableau"),
    ("tableau", "texte"),
    ("texte", "trait"), ("tableau", "trait"), ("liste", "trait"), ("suite", "trait"),
    ("citation", "trait"),
    ("texte", "code"), ("citation", "code"), ("tableau", "code"),
    ("texte", "libelle"), ("liste", "libelle"), ("tableau", "libelle"),
} | {("libelle", g) for g in ("texte", "liste", "citation", "tableau", "trait", "code", "libelle")}


def aerer(md: str) -> str:
    sortie: list[str] = []
    precedent = "vide"
    ouverture = None
    for ligne in md.split("\n"):
        m = _RE_FENCE.match(ligne)
        if ouverture is not None:
            sortie.append(ligne)
            if m and m.group(1)[0] * 3 == ouverture:
                ouverture = None
                precedent = "texte"
            continue
        if m and not (ligne[:1].isspace() and precedent in ("liste", "suite")):
            if (precedent, "code") in _SEPARER:
                sortie.append("")
            ouverture = m.group(1)[0] * 3
            sortie.append(ligne)
            continue
        genre = _genre(ligne, precedent)
        if (precedent, genre) in _SEPARER:
            sortie.append("")
        sortie.append(ligne)
        precedent = genre
    return "\n".join(sortie)


_RE_DETAILS = re.compile(r"<details>\s*<summary>(.*?)</summary>(.*?)</details>", re.S | re.I)


def reponses_en_ligne(md: str) -> str:
    """<details> ne s'imprime pas : chaque réponse devient un bloc marqué
    .rep, que la mise en page affiche, masque ou déplace."""
    def conv(m: re.Match) -> str:
        etiquette = " ".join(m.group(1).split()) or "Réponse"
        reponse = " ".join(m.group(2).split())
        return (f'<span class="rep"><span class="rep-label">{htmllib.escape(etiquette)}</span> '
                f'{reponse}</span>')
    return _RE_DETAILS.sub(conv, md)


def proteger_formules(md: str) -> tuple[str, dict[str, str]]:
    """Les $$…$$ passent sans être lus comme du Markdown (les _ et \\ y
    sont fréquents). Une formule seule sur sa ligne devient un bloc centré."""
    formules: dict[str, str] = {}

    def garder(contenu: str, bloc: bool) -> str:
        cle = f"FORMULE{len(formules)}Z"
        classe = "formule" if bloc else "formule en-ligne"
        balise = "div" if bloc else "span"
        formules[cle] = (f'<{balise} class="{classe}">'
                         f'{htmllib.escape(" ".join(contenu.split()))}</{balise}>')
        return f"\n\n{cle}\n\n" if bloc else cle

    codes: dict[str, str] = {}

    def mettre_de_cote(m: re.Match) -> str:
        cle = f"CODEENLIGNE{len(codes)}Z"
        codes[cle] = m.group(0)
        return cle

    def traiter(bloc: str) -> str:
        # Le code (blocs ``` et `…`) garde ses $$ : PL/pgSQL, shell…
        bloc = re.sub(r"`[^`\n]+`", mettre_de_cote, bloc)
        bloc = re.sub(r"^[ \t]*\$\$((?:(?!\$\$).)+?)\$\$[ \t]*$",
                      lambda m: garder(m.group(1), True), bloc, flags=re.M | re.S)
        bloc = re.sub(r"\$\$((?:(?!\$\$)[^\n])+?)\$\$", lambda m: garder(m.group(1), False), bloc)
        for cle, code in codes.items():
            bloc = bloc.replace(cle, code)
        return bloc

    sortie: list[str] = []
    tampon: list[str] = []
    for ligne, code in _balayer(md):
        if code:
            if tampon:
                sortie.append(traiter("\n".join(tampon)))
                tampon = []
            sortie.append(ligne)
        else:
            tampon.append(ligne)
    if tampon:
        sortie.append(traiter("\n".join(tampon)))
    return "\n".join(sortie), formules


def titre_unique(md: str) -> tuple[str, str]:
    """Si le document a un seul titre de niveau 1 et qu'il ouvre le document,
    c'est son titre : on le renvoie et on le retire du corps."""
    h1 = [l for l, code in _balayer(md) if not code and re.match(r"^#\s+\S", l)]
    if len(h1) != 1:
        return "", md
    lignes = md.lstrip("\n").split("\n")
    if lignes and lignes[0] == h1[0]:
        return re.sub(r"^#\s+|\s+#*\s*$", "", h1[0]).strip(), "\n".join(lignes[1:])
    return "", md


# ----------------------------------------------------------------------
# Mise en forme
# ----------------------------------------------------------------------
def teinte(couleur: str, part: float) -> str:
    """Couleur mélangée au blanc (part = proportion de couleur)."""
    c = couleur.lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}", c):
        return f"color-mix(in srgb, {couleur} {part * 100:.0f}%, white)"
    if len(c) == 3:
        c = "".join(x * 2 for x in c)
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    m = lambda x: round(255 - (255 - x) * part)  # noqa: E731
    return f"#{m(r):02x}{m(g):02x}{m(b):02x}"


def feuille_de_style(r: dict) -> str:
    polices = '"Noto Sans", "DejaVu Sans", Arial, sans-serif, "Segoe UI Emoji", "Noto Color Emoji"'
    if r["police"]:
        polices = f'"{r["police"]}", ' + polices
    return f"""
:root {{
  --accent: {r["couleur"]};
  --voile: {teinte(r["couleur"], .08)};
  --zebre: {teinte(r["couleur"], .045)};
  --encre: #1c1f24; --titre: #16233a; --gris: #5a6473; --filet: #d6dbe1;
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ font-family: {polices}; font-size: {r["taille_texte"]}; line-height: 1.45;
        color: var(--encre); margin: 0; }}
code, pre, .formule {{ font-family: "DejaVu Sans Mono", "Noto Sans Mono", Consolas, monospace; }}

.cover {{ border-bottom: 3px solid var(--accent); padding-bottom: 12px; margin-bottom: 16px; }}
.cover .kicker {{ font-size: .857em; letter-spacing: .14em; text-transform: uppercase;
                  color: var(--accent); font-weight: 700; }}
.cover .cover-titre {{ font-size: 1.81em; font-weight: 700; margin: 6px 0; color: var(--titre);
                       line-height: 1.2; }}
.cover .sub {{ font-size: .952em; color: var(--gris); }}

h1, h2, h3, h4, h5, h6 {{ color: var(--titre); line-height: 1.25; margin: 14px 0 6px;
                          break-after: avoid; page-break-after: avoid; }}
h1 {{ font-size: 1.62em; }} h2 {{ font-size: 1.33em; }} h3 {{ font-size: 1.19em; }}
h4 {{ font-size: 1.1em; }} h5, h6 {{ font-size: 1em; }}

.section {{ font-size: 1.24em; color: #fff; background: var(--accent);
            padding: 6px 11px; border-radius: 4px; margin: 4px 0 10px; }}
.section.saut {{ break-before: page; page-break-before: always; }}
.sous {{ font-size: 1.1em; border-left: 3px solid var(--accent); padding-left: 8px; }}

p {{ margin: 5px 0; }}
ul, ol {{ margin: 4px 0 9px; padding-left: 20px; }}
li {{ margin: 3px 0; }}
li > p {{ margin: 2px 0; }}

table {{ width: 100%; border-collapse: collapse; margin: 8px 0 12px; font-size: .905em; }}
thead {{ display: table-header-group; }}
tr {{ break-inside: avoid; }}
th {{ background: var(--accent); color: #fff; text-align: left; padding: 5px 8px; font-weight: 600; }}
td {{ border: 1px solid var(--filet); padding: 4px 8px; vertical-align: top; }}
tbody tr:nth-child(even) {{ background: var(--zebre); }}

blockquote {{ margin: 6px 0; padding: 6px 11px; background: var(--voile);
              border-left: 3px solid var(--accent); border-radius: 0 4px 4px 0; }}
blockquote p {{ margin: 2px 0; }}

code {{ background: #eceff3; padding: 1px 4px; border-radius: 3px; font-size: .867em; color: #b0345a; }}
pre {{ background: #f6f8fa; border: 1px solid #dfe3e8; border-radius: 4px; padding: 9px 11px;
       font-size: .838em; line-height: 1.4; white-space: pre-wrap; break-inside: avoid; }}
pre code {{ background: none; color: #24303f; padding: 0; font-size: 1em; }}
.formule {{ display: block; text-align: center; margin: 6px 0; padding: 4px 0; color: #24303f;
            font-size: .95em; }}
.formule.en-ligne {{ display: inline; padding: 0; }}

.carte {{ border: 1px solid #dfe3e8; border-left: 3px solid var(--accent); border-radius: 5px;
          padding: 8px 13px 5px; margin: 11px 0; break-inside: avoid; background: #fff; }}
.carte-titre {{ font-size: 1.14em; color: var(--accent); margin: 2px 0 8px; padding-bottom: 3px;
                border-bottom: 2px solid var(--accent); }}
p.champ {{ margin: 8px 0 2px; }}
p.champ strong {{ color: var(--accent); }}
p.champ + ul {{ margin-top: 2px; }}
p.champ + blockquote {{ margin-top: 3px; }}

.questions {{ list-style: none; padding-left: 0; margin-top: 6px; }}
.questions > li {{ position: relative; border: 1px solid #e3e6ea; border-radius: 5px;
                   padding: 7px 11px 7px 41px; margin: 7px 0; break-inside: avoid; }}
ul.questions > li {{ padding-left: 11px; }}
ol.questions > li::before {{ content: counter(list-item); position: absolute; left: 11px; top: 8px;
                   width: 21px; height: 21px; line-height: 21px; text-align: center;
                   background: var(--accent); color: #fff; border-radius: 50%;
                   font-size: .857em; font-weight: 700; }}
.q-label, .rep-label {{ font-size: .714em; font-weight: 700; text-transform: uppercase;
                        letter-spacing: .04em; padding: 1px 5px; border-radius: 3px; margin-right: 5px; }}
.q-label {{ background: var(--accent); color: #fff; }}
.rep-label {{ background: #e4efe5; color: #2f6b34; }}
.rep {{ display: block; margin-top: 3px; color: #313842; }}
ol.corrige > li {{ margin: 5px 0; break-inside: avoid; }}

.sommaire {{ border: 1px solid var(--filet); border-radius: 5px; padding: 8px 14px; margin: 0 0 14px; }}
.sommaire-titre {{ font-weight: 700; color: var(--accent); margin: 0 0 4px; }}
.sommaire ol {{ margin: 0; }}

hr {{ border: none; border-top: 1px solid #dde1e6; margin: 10px 0; }}
.longue {{ break-inside: auto !important; page-break-inside: auto !important; }}
img {{ display: block; max-width: 100%; max-height: 115mm; margin: 6px auto; object-fit: contain;
       border: 1px solid #dfe3e8; border-radius: 4px; break-inside: avoid; }}
.image-absente {{ color: var(--gris); font-style: italic; }}

{r["css"]}
"""


# Transformations faites dans Chromium, sur le vrai DOM.
_SCRIPT = r"""
(o) => {
  const doc = document.getElementById('doc');
  const niv = h => +h.tagName[1];
  const estTitre = n => !!n && /^H[1-6]$/.test(n.tagName);
  const titresDirects = () => [...doc.children].filter(estTitre);

  // 1. Cartes : un titre à mot-clé et tout ce qui suit, jusqu'au titre de
  //    même niveau (ou plus haut) ou au trait horizontal suivant.
  //    Mot entier : « FICHE » ne prend pas « Fiches de révision ». Un titre
  //    qui en coiffe d'autres du même genre est une section, pas une carte.
  let cartes = 0;
  if (o.cartes.length) {
    const echap = m => m.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const motif = new RegExp('(?<![\\p{L}\\p{N}])(' + o.cartes.map(echap).join('|') +
                             ')(?![\\p{L}\\p{N}])', 'iu');
    const portee = h => {
      const r = [];
      for (let n = h.nextElementSibling; n; n = n.nextElementSibling) {
        if (n.tagName === 'HR' || (estTitre(n) && niv(n) <= niv(h))) break;
        r.push(n);
      }
      return r;
    };
    for (const h of titresDirects()) {
      if (h.parentElement !== doc || !motif.test(h.textContent)) continue;
      const contenu = portee(h);
      if (contenu.some(n => estTitre(n) && motif.test(n.textContent))) continue;
      const carte = document.createElement('div');
      carte.className = 'carte';
      h.before(carte);
      carte.append(h, ...contenu);
      h.classList.add('carte-titre');
      cartes++;
    }
    for (const hr of [...doc.querySelectorAll(':scope > hr')]) {
      const a = hr.previousElementSibling, b = hr.nextElementSibling;
      if ((a && a.classList.contains('carte')) || (b && b.classList.contains('carte'))) hr.remove();
    }
  }

  // 2. Grandes sections : niveau imposé, ou le plus haut niveau répété.
  let n = o.niveau;
  const titres = titresDirects();
  if (!n) {
    const c = {};
    titres.forEach(h => c[niv(h)] = (c[niv(h)] || 0) + 1);
    n = [1, 2, 3, 4].find(l => (c[l] || 0) >= 2) || 0;
  }
  const sections = n ? titres.filter(h => niv(h) === n) : [];
  for (const h of sections) {
    h.classList.add('section');
    if (!o.nouvellePage) continue;
    let contenuAvant = false;
    for (let p = h.previousElementSibling; p; p = p.previousElementSibling)
      if (!p.matches('.cover, hr')) { contenuAvant = true; break; }
    if (!contenuAvant) continue;
    h.classList.add('saut');
    const p = h.previousElementSibling;
    if (p && p.tagName === 'HR') p.remove();
  }
  if (n) titres.filter(h => niv(h) > n).forEach(h => h.classList.add('sous'));

  // 3. Libellés de champ : paragraphe qui n'est qu'un **Libellé** (± emoji).
  for (const p of doc.querySelectorAll('p')) {
    const s = p.firstElementChild;
    if (!s || s.tagName !== 'STRONG' || p.firstChild !== s) continue;
    if (p.textContent.slice(s.textContent.length).trim().length <= 3) p.classList.add('champ');
  }

  // 4. Questions / réponses.
  const reps = [...doc.querySelectorAll('.rep')];
  const listes = new Set();
  for (const r of reps) { const li = r.closest('li'); if (li) listes.add(li.parentElement); }
  for (const l of listes) {
    l.classList.add('questions');
    for (const li of l.children) {
      const s = li.querySelector('strong');
      if (s && /^\s*Q\s*:?\s*$/i.test(s.textContent)) {
        const e = document.createElement('span');
        e.className = 'q-label'; e.textContent = 'Q';
        s.replaceWith(e);
      }
    }
  }
  if (o.reponses === 'masquees') reps.forEach(r => r.remove());
  if (o.reponses === 'fin' && reps.length) {
    const h = document.createElement(n ? 'h' + n : 'h2');
    h.className = 'section' + (o.nouvellePage ? ' saut' : '');
    h.textContent = o.titreCorrige;
    const ol = document.createElement('ol');
    ol.className = 'corrige';
    reps.forEach((r, i) => {
      const li = r.closest('li');
      let num = i + 1;
      if (li && li.parentElement.tagName === 'OL') {
        const liste = li.parentElement;
        num = (+liste.getAttribute('start') || 1) + [...liste.children].indexOf(li);
      }
      const item = document.createElement('li');
      item.value = num;
      const lab = r.querySelector('.rep-label');
      if (lab) lab.remove();
      item.innerHTML = r.innerHTML.trim();
      r.remove();
      ol.appendChild(item);
    });
    doc.append(h, ol);
    sections.push(h);
  }

  // 5. Sommaire.
  if (o.sommaire && sections.length > 1) {
    const nav = document.createElement('nav');
    nav.className = 'sommaire';
    nav.innerHTML = '<p class="sommaire-titre">Sommaire</p><ol></ol>';
    for (const h of sections) {
      const li = document.createElement('li');
      li.textContent = h.textContent.replace(/^\s*\d+\s*[.)]\s*/, '');
      nav.querySelector('ol').appendChild(li);
    }
    const cover = doc.querySelector(':scope > .cover');
    if (cover) cover.after(nav); else doc.prepend(nav);
  }

  // 6. Un bloc insécable plus haut que ~80 % d'une page laisserait une page
  //    presque vide avant lui : on l'autorise à se couper.
  let longs = 0;
  for (const el of doc.querySelectorAll('.carte, pre, .questions > li, blockquote, img'))
    if (el.getBoundingClientRect().height > 0.8 * o.hauteurPage) { el.classList.add('longue'); longs++; }

  return {niveau: n, sections: sections.length, cartes, questions: reps.length, longs};
}
"""


_TYPES_IMAGE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
                ".bmp": "image/bmp"}


def integrer_images(rendu: str, base: Path) -> str:
    """Les images citées en relatif (photos du tableau dans supports/…) sont
    embarquées dans la page : set_content n'a pas d'adresse de base."""
    def remplacer(m: re.Match) -> str:
        src = htmllib.unescape(m.group(2))
        if re.match(r"^(https?:|data:)", src, re.I):
            return m.group(0)
        chemin = (base / src.replace("\\", "/")).resolve()
        genre = _TYPES_IMAGE.get(chemin.suffix.lower())
        if not genre or not chemin.is_file():
            alt = re.search(r'alt="([^"]*)"', m.group(0))
            return (f'<span class="image-absente">[image introuvable : '
                    f'{htmllib.escape(src)}{" — " + alt.group(1) if alt and alt.group(1) else ""}]</span>')
        donnees = base64.b64encode(chemin.read_bytes()).decode("ascii")
        return f'{m.group(1)}data:{genre};base64,{donnees}{m.group(3)}'
    return re.sub(r'(<img\b[^>]*?\bsrc=")([^"]+)("[^>]*>)', remplacer, rendu)


def construire(md_brut: str, r: dict, nom: str, base: Path | None = None) -> tuple[str, dict, str]:
    """(page HTML, options pour le script, texte du pied de page)."""
    _, md_texte, _ = entete.separer(md_brut)   # un en-tête YAML n'est pas du contenu
    champs = champs_du_document(md_texte)
    extra = {"fichier": nom, "date": datetime.date.today().strftime("%d/%m/%Y")}

    titre = remplir(r["titre"], champs, extra)
    if not titre and r["couverture"]:
        titre, md_texte = titre_unique(md_texte)
    titre = titre or nom
    sous_titre = remplir(r["sous_titre"], champs, extra)
    pied = remplir(r["pied_de_page"], champs, extra)
    pied = re.split(r"\s*(?:⚠|\*\()", pied)[0]          # sans les remarques ⚠️ *(…)*
    pied = texte_brut(pied) or texte_brut(titre)
    if len(pied) > 90:
        pied = pied[:88].rstrip() + "…"

    corps, formules = proteger_formules(md_texte)
    corps = aerer(reponses_en_ligne(corps))
    rendu = markdown.markdown(corps, extensions=["tables", "fenced_code", "sane_lists"])
    for cle, balise in formules.items():
        rendu = rendu.replace(f"<p>{cle}</p>", balise).replace(cle, balise)
    if base is not None:
        rendu = integrer_images(rendu, base)

    couverture = ""
    if r["couverture"]:
        couverture = (
            '<div class="cover">'
            + (f'<div class="kicker">{htmllib.escape(r["surtitre"])}</div>' if r["surtitre"] else "")
            + f'<div class="cover-titre">{en_ligne(titre)}</div>'
            + (f'<div class="sub">{en_ligne(sous_titre)}</div>' if sous_titre else "")
            + "</div>")

    page = (f'<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">'
            f'<style>{feuille_de_style(r)}</style></head>'
            f'<body><main id="doc">{couverture}{rendu}</main></body></html>')

    options = {
        "cartes": r["cartes"],
        "niveau": 0 if r["niveau_sections"] == "auto" else int(r["niveau_sections"]),
        "nouvellePage": bool(r["nouvelle_page"]),
        "reponses": r["reponses"],
        "titreCorrige": r["titre_corrige"] or "Corrigé",
        "sommaire": bool(r["sommaire"]),
    }
    return page, options, pied


_PAGES_MM = {"A3": (297, 420), "A4": (210, 297), "A5": (148, 210),
             "Letter": (215.9, 279.4), "Legal": (215.9, 355.6)}


def _mm(valeur: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(mm|cm|in|px|pt)", valeur)
    n, u = float(m.group(1)), m.group(2)
    return n * {"mm": 1, "cm": 10, "in": 25.4, "px": 25.4 / 96, "pt": 25.4 / 72}[u]


def zone_utile_px(r: dict) -> tuple[int, int]:
    """Largeur et hauteur de la zone imprimable, en pixels CSS."""
    l, h = _PAGES_MM[r["format"]]
    if r["orientation"] == "paysage":
        l, h = h, l
    m = marges(r)
    l -= _mm(m["left"]) + _mm(m["right"])
    h -= _mm(m["top"]) + _mm(m["bottom"])
    return round(l * 96 / 25.4), round(h * 96 / 25.4)


def marges(r: dict) -> dict[str, str]:
    v = r["marges"].split()
    if len(v) == 1:
        v = v * 4
    elif len(v) == 2:
        v = [v[0], v[1], v[0], v[1]]
    elif len(v) == 3:
        v = [v[0], v[1], v[2], v[1]]
    haut, droite, bas, gauche = v[:4]
    return {"top": haut, "right": droite, "bottom": bas, "left": gauche}


def main() -> int:
    # Sous Windows, un tuyau reçoit l'encodage local (cp1252) : un nom
    # accentué ou un emoji dans un message ferait planter l'affichage.
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="Met en page un support Markdown en PDF.")
    ap.add_argument("source", help="le .md produit par la synthèse")
    ap.add_argument("--consignes", help="fichier de consignes dont l'en-tête règle la mise en page")
    ap.add_argument("--sortie", help="PDF à écrire (par défaut : à côté du .md)")
    ap.add_argument("--html", action="store_true", help="écrire aussi la page HTML intermédiaire")
    a = ap.parse_args()

    src = Path(a.source)
    sortie = Path(a.sortie) if a.sortie else src.with_suffix(".pdf")
    md = src.read_text(encoding="utf-8-sig", errors="replace")

    if a.consignes:
        c = entete.lire(a.consignes)
        reglages = c.pdf
        for av in c.avertissements:
            print(f"Consignes « {Path(a.consignes).stem} » : {av}", file=sys.stderr)
    else:
        reglages, _ = entete.reglages_pdf({})

    page, options, pied = construire(md, reglages, src.stem, src.resolve().parent)

    from playwright.sync_api import sync_playwright

    pied_html = (
        '<div style="font-size:8px;color:#8a94a3;width:100%;padding:0 14mm;'
        'display:flex;justify-content:space-between;font-family:Arial,sans-serif;">'
        f'<span>{htmllib.escape(pied)}</span>'
        '<span>Page <span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')

    with sync_playwright() as p:
        navigateur = p.chromium.launch(args=["--no-sandbox"])
        largeur, hauteur = zone_utile_px(reglages)
        # Mise en page à la largeur réelle de la feuille : les hauteurs
        # mesurées par le script sont alors celles de l'impression.
        onglet = navigateur.new_page(viewport={"width": largeur, "height": hauteur})
        onglet.set_content(page, wait_until="load")
        bilan = onglet.evaluate(_SCRIPT, dict(options, hauteurPage=hauteur))
        if a.html:
            src.with_suffix(".html").write_text(onglet.content(), encoding="utf-8")
        contenu = onglet.pdf(
            format=reglages["format"],
            landscape=reglages["orientation"] == "paysage",
            print_background=True, display_header_footer=True,
            header_template="<div></div>", footer_template=pied_html,
            margin=marges(reglages),
        )
        navigateur.close()

    try:
        sortie.write_bytes(contenu)
    except PermissionError:
        print(f"{sortie.name} est ouvert dans une autre application (lecteur PDF ?) : "
              "ferme-le, puis « Refaire le PDF ».", file=sys.stderr)
        return 1

    print(f"PDF écrit : {sortie} — sections : {bilan['sections']} (titres de niveau "
          f"{bilan['niveau'] or '—'}), cartes : {bilan['cartes']}, questions : {bilan['questions']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
