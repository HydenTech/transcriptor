#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Génère un PDF soigné à partir d'un support de révision Markdown
(structure du CLAUDE.md « Résumés de cours », sections 1 à 10).

Usage :
    python3 generate_pdf.py [chemin/vers/cours.md]

Si aucun chemin n'est fourni, le fichier par défaut ci-dessous est utilisé.
Le PDF est écrit à côté du .md source (même nom, extension .pdf).

Particularités :
- chaque chapitre (section 1 à 10) commence sur une NOUVELLE PAGE ;
- les fiches (section 5) sont présentées en cartes qui ne se coupent pas ;
- les flashcards <details> de la section 9 sont converties en Q/R visibles ;
- le titre de couverture est déduit automatiquement du tableau d'en-tête ;
- rendu via Chromium (Playwright) pour conserver les emojis en couleur.
"""
import os, re, sys, html as htmllib
import markdown

DEFAULT_SRC = "/home/claude/Resume-cours_Stockage-cote-client_16-04.md"
SRC = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC
OUT = os.path.splitext(SRC)[0] + ".pdf"

raw = open(SRC, encoding="utf-8").read()

# --- Couverture déduite du tableau d'en-tête (section 1) ---
def header_field(label):
    m = re.search(rf"\|\s*{re.escape(label)}\s*\|\s*(.*?)\s*\|", raw)
    return m.group(1).strip() if m else ""

matiere = header_field("Matière")
theme = header_field("Thème de la séance")
duree = header_field("Durée")

cover_title = htmllib.escape(theme or "Support de révision")
cover_sub = htmllib.escape("  ·  ".join([x for x in (matiere, duree) if x]))
foot_left = htmllib.escape(matiere or "Support de révision")

# --- Découpage : sections 1-8 | section 9 (flashcards) | section 10 ---
def _find(marker, default):
    i = raw.find(marker)
    return i if i != -1 else default

i9 = _find("### 9. Auto-évaluation", len(raw))
i10 = _find("### 10. Zones d'ombre", len(raw))
if i10 < i9:
    i10 = i9
part_A = raw[:i9]
part_9 = raw[i9:i10]
part_C = raw[i10:]

md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists"])

def convert(txt):
    md.reset()
    return md.convert(txt)

FIELDS = ["Définition", "À retenir", "Formule / schéma",
          "Exemple donné en cours", "Piège fréquent", "Lien avec le reste du cours"]

def fiche_fields(txt):
    # Force une ligne vide après chaque libellé de champ de fiche pour que
    # listes / code / citations soient rendus comme des blocs distincts.
    alt = "|".join(re.escape(l) for l in FIELDS)
    return re.sub(rf"(^\*\*(?:{alt})\*\*[^\n]*)\n(?!\n)", r"\1\n\n", txt, flags=re.M)

def tag_fields(html):
    for lab in FIELDS:
        html = html.replace(f"<p><strong>{lab}</strong>", f'<p class="field"><strong>{lab}</strong>')
    return html

def classify_h3(html):
    def repl(m):
        inner = m.group(1)
        cls = "fiche" if "FICHE" in inner else "section"
        return f'<h3 class="{cls}">{inner}</h3>'
    return re.sub(r"<h3>(.*?)</h3>", repl, html, flags=re.S)

html_A = classify_h3(tag_fields(convert(fiche_fields(part_A))))
html_C = classify_h3(convert(part_C))

# Chaque fiche (segments séparés par <hr/>) est enveloppée dans une carte.
segs = re.split(r"<hr\s*/?>", html_A)
html_A = "".join(f'<div class="card">{s}</div>' if "FICHE" in s else s for s in segs)

# --- Flashcards (section 9) : réponses visibles à l'impression ---
def inline(s):
    s = htmllib.escape(s, quote=False)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)

cards = []
pat = re.compile(
    r"^\s*(\d+)\.\s+\*\*Q :\*\*\s*(.+?)\s*\n\s*<details><summary>Réponse</summary>(.+?)</details>\s*$",
    re.M)
for m in pat.finditer(part_9):
    n, q, a = m.group(1), m.group(2).strip(), m.group(3).strip()
    cards.append(
        f'<div class="fc"><div class="fc-n">{n}</div><div class="fc-body">'
        f'<p class="q"><span class="q-label">Q</span> {inline(q)}</p>'
        f'<p class="a"><span class="a-label">Réponse</span> {inline(a)}</p>'
        f'</div></div>')
flash_html = '<h3 class="section">9. Auto-évaluation</h3>\n<div class="flashcards">' + "\n".join(cards) + "</div>"

cover = (
    f'<div class="cover"><div class="kicker">Support de révision</div>'
    f'<h1>{cover_title}</h1>'
    + (f'<div class="sub">{cover_sub}</div>' if cover_sub else "")
    + '</div>')

body = cover + html_A + flash_html + html_C

CSS = """
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font-family: "Noto Sans", "DejaVu Sans", "Noto Color Emoji", sans-serif;
  font-size: 10.5pt; line-height: 1.45; color: #1c1f24; margin: 0;
}
code, pre { font-family: "DejaVu Sans Mono", "Noto Sans Mono", monospace; }

.cover { border-bottom: 3px solid #1e4e8c; padding-bottom: 12px; margin-bottom: 16px; }
.cover .kicker { font-size: 9pt; letter-spacing: .14em; text-transform: uppercase; color: #1e4e8c; font-weight: 700; }
.cover h1 { font-size: 19pt; margin: 6px 0 6px; color: #16233a; line-height: 1.2; }
.cover .sub { font-size: 10pt; color: #5a6473; }

/* Chaque chapitre commence sur une nouvelle page (sauf le premier). */
h3.section {
  font-size: 13pt; color: #fff; background: #1e4e8c;
  padding: 6px 11px; border-radius: 4px; margin: 4px 0 10px;
  break-after: avoid; break-before: page; page-break-before: always;
}
.cover + h3.section { break-before: auto; page-break-before: auto; }

h3.fiche {
  font-size: 12pt; color: #1e4e8c; margin: 2px 0 8px;
  padding-bottom: 3px; border-bottom: 2px solid #1e4e8c; break-after: avoid;
}
h4 {
  font-size: 11.5pt; color: #16233a; margin: 14px 0 6px;
  border-left: 3px solid #1e4e8c; padding-left: 8px; break-after: avoid;
}
p { margin: 5px 0; }
ul { margin: 4px 0 9px; padding-left: 20px; }
li { margin: 3px 0; }

table { width: 100%; border-collapse: collapse; margin: 8px 0 12px; font-size: 9.5pt; }
thead { display: table-header-group; }
th { background: #1e4e8c; color: #fff; text-align: left; padding: 5px 8px; font-weight: 600; }
td { border: 1px solid #d6dbe1; padding: 4px 8px; vertical-align: top; }
tbody tr:nth-child(even) { background: #f4f7fb; }

blockquote {
  margin: 6px 0; padding: 6px 11px; background: #eef3fb;
  border-left: 3px solid #1e4e8c; border-radius: 0 4px 4px 0;
}
blockquote p { margin: 2px 0; }

code { background: #eceff3; padding: 1px 4px; border-radius: 3px; font-size: 9.1pt; color: #b0345a; }
pre { background: #f6f8fa; border: 1px solid #dfe3e8; border-radius: 4px;
      padding: 9px 11px; font-size: 8.8pt; line-height: 1.4; overflow: hidden; break-inside: avoid; }
pre code { background: none; color: #24303f; padding: 0; }

.card { border: 1px solid #dfe3e8; border-left: 3px solid #1e4e8c; border-radius: 5px;
        padding: 8px 13px 5px; margin: 11px 0; break-inside: avoid; background: #fff; }
.card p, .card ul { break-inside: avoid; }

p.field { margin: 8px 0 2px; }
p.field strong { color: #1e4e8c; }
p.field + ul { margin-top: 2px; }
p.field + blockquote { margin-top: 3px; }

.flashcards { margin-top: 6px; }
.fc { display: flex; gap: 9px; align-items: flex-start; border: 1px solid #e3e6ea;
      border-radius: 5px; padding: 7px 11px; margin: 7px 0; break-inside: avoid; }
.fc-n { flex: 0 0 21px; width: 21px; height: 21px; line-height: 21px; text-align: center;
        background: #1e4e8c; color: #fff; border-radius: 50%; font-size: 9pt; font-weight: 700; }
.fc-body { flex: 1; }
.fc .q { margin: 0 0 3px; }
.fc .a { margin: 0; color: #313842; }
.q-label, .a-label { font-size: 7.5pt; font-weight: 700; text-transform: uppercase;
        letter-spacing: .04em; padding: 1px 5px; border-radius: 3px; margin-right: 5px; }
.q-label { background: #1e4e8c; color: #fff; }
.a-label { background: #e4efe5; color: #2f6b34; }

hr { border: none; border-top: 1px solid #dde1e6; margin: 10px 0; }
"""

full = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><style>{CSS}</style></head>
<body>{body}</body></html>"""

from playwright.sync_api import sync_playwright

footer = ('<div style="font-size:8px;color:#8a94a3;width:100%;padding:0 14mm;'
          'display:flex;justify-content:space-between;">'
          f'<span>{foot_left}</span>'
          '<span>Page <span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page()
    page.set_content(full, wait_until="load")
    page.pdf(
        path=OUT, format="A4", print_background=True,
        display_header_footer=True,
        header_template="<div></div>",
        footer_template=footer,
        margin={"top": "14mm", "right": "14mm", "bottom": "16mm", "left": "14mm"},
    )
    browser.close()

print("PDF written:", OUT)
