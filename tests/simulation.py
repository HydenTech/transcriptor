# -*- coding: utf-8 -*-
"""
Banc d'essai du mode année, sans carte graphique ni compte Claude :
- un faux module faster_whisper (transcription instantanée) ;
- un faux `claude` qui joue les quatre étapes (horaire, notes, fiches, infos)
  en émettant le même flux stream-json que Claude Code — y compris les
  appels à l'outil Write pour les fiches.

    from simulation import preparer_environnement
    env = preparer_environnement(tmp, horaire_json=...)   # avant d'importer transcriptor
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

FAUX_WHISPER = '''
import os, types
class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text
class _Info:
    duration = 900.0
class WhisperModel:
    def __init__(self, nom, device="cpu", compute_type="int8"):
        self.nom = nom
    def transcribe(self, chemin, **reglages):
        if os.environ.get("FAUX_WHISPER_ECHEC") and os.environ["FAUX_WHISPER_ECHEC"] in chemin:
            raise RuntimeError("fichier audio illisible (simulé)")
        nom = os.path.basename(chemin)
        def gen():
            for i in range(15):
                yield _Seg(i * 60.0, i * 60.0 + 55, f"Phrase {i} du cours enregistré dans {nom}. "
                           "Bon, alors, aujourd'hui on parle de la posture professionnelle.")
        return gen(), _Info()
'''

FAUX_CLAUDE = r'''#!/usr/bin/env python3
import json, os, re, sys, time
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("9.9.9 (Claude Code simulé)"); sys.exit(0)
if args[:2] == ["auth", "status"]:
    # FAUX_CLAUDE_CONNEXION=non : Claude Code pas connecté au compte.
    if os.environ.get("FAUX_CLAUDE_CONNEXION") == "non":
        print(json.dumps({"loggedIn": False, "authMethod": "none"})); sys.exit(1)
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"})); sys.exit(0)
prompt = args[args.index("-p") + 1] if "-p" in args else ""
entree = sys.stdin.read()
journal = os.environ.get("FAUX_CLAUDE_JOURNAL")
if journal:
    with open(journal, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"args": args, "taille": len(entree)}, ensure_ascii=False) + "\n")

def emettre(ev):
    print(json.dumps(ev, ensure_ascii=False), flush=True)

emettre({"type": "system", "subtype": "init", "model": "claude-simule"})

def chercher(motif, defaut=""):
    m = re.search(motif, entree, re.M)
    return m.group(1).strip() if m else defaut

if "limite" in os.environ.get("FAUX_CLAUDE_MODE", ""):
    emettre({"type": "result", "subtype": "error", "is_error": True,
             "result": "Claude usage limit reached. Your limit will reset at 5pm."})
    sys.exit(1)
if "connexion" in os.environ.get("FAUX_CLAUDE_MODE", ""):
    # Ce qu'écrit Claude Code en mode -p quand sa connexion a expiré.
    emettre({"type": "result", "subtype": "success", "is_error": True,
             "result": "Failed to authenticate. API Error: 401 OAuth token has expired. "
                       "Please obtain a new token or refresh your existing token."})
    sys.exit(1)

# Ce qu'a fait le vrai Claude sur la fiche « Infos » d'une matière chargée
# d'articles : il les ouvre un à un pour en lire le titre, jusqu'à épuiser
# ses tours (--max-turns) sans avoir rien rédigé.
if "@@OUVRE_TOUT@@" in entree and "<infos>" in entree and "aucun fichier" not in prompt:
    for chemin in sorted(Path("supports").glob("*"))[:3]:
        emettre({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": str(chemin.resolve())}}]}})
    emettre({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 7,
             "errors": ["Reached maximum number of turns (6)"]})
    sys.exit(1)

if "JSON demandé" in prompt:
    texte = Path(os.environ["FAUX_HORAIRE"]).read_text(encoding="utf-8")
elif "fiches existantes" in prompt:
    # Ajout aux fiches : la catégorie 010.md et le lexique sont complétés
    # (outil Edit), une nouvelle catégorie 015.md est créée (outil Write).
    if os.environ.get("FAUX_CLAUDE_AJOUT") == "rien":
        texte = "RIEN À AJOUTER"
    else:
        dates = re.findall(r'<seance date="([^"]+)">', entree) or ["documents"]
        for nom, ajout in (("010.md", "\n### Apports du " + ", ".join(dates) + "\n\n- Nouvelle notion : la "
                            "**bientraitance** au quotidien.\n\n> **Exemple** : un éducateur "
                            "frappe avant d'entrer.\n"),
                           ("990.md", "| Bientraitance | Culture du respect de la personne |\n")):
            chemin = Path(nom)
            avant = chemin.read_text(encoding="utf-8") if chemin.exists() else ""
            chemin.write_text(avant.rstrip("\n") + "\n" + ajout, encoding="utf-8")
            emettre({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": str(chemin.resolve()),
                                                               "old_string": "x", "new_string": "y"}}]}})
        nouveau = Path("015.md")
        nouveau.write_text("## Repères pour " + ", ".join(dates) + "\n\nUne **nouvelle catégorie**.\n",
                           encoding="utf-8")
        emettre({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": str(nouveau.resolve()),
                                                            "content": "…"}}]}})
        texte = "TERMINÉ"
elif "010.md" in prompt:
    matiere = chercher(r"^Matière : (.+)$", "Matière")
    dates = re.findall(r'<seance date="([^"]+)">', entree)
    parties = {
        "010.md": f"""## Les fondements de {matiere}

Séances rédigées : {", ".join(dates) or "aucune"}.

Le cours pose les **bases** de la matière et la **posture professionnelle** de l'éducateur.

### Définitions essentielles

- La **posture professionnelle** : manière d'être et d'agir dans la relation d'aide.
- La **distance relationnelle** : juste proximité avec la personne accompagnée.
  - Ni fusion, ni froideur.
  - Elle s'ajuste à chaque situation.

> **Définition** : la **posture professionnelle** est l'ensemble des attitudes que l'éducateur adopte pour accompagner sans se substituer.

> **Exemple** : face à Léa, 15 ans, qui refuse d'aller en classe, l'éducateur **écoute** avant de **cadrer**.

| Notion | Ce qu'elle désigne | Risque si absente |
|---|---|---|
| Posture | Attitude d'ensemble | Agir au hasard |
| Distance | Juste proximité | Fusion ou rejet |

```mermaid
flowchart TD
  A[Observer] --> B[Analyser]
  B --> C[Agir]
  C --> D[Évaluer]
  D --> A
```
*Schéma — Le cycle de l'intervention éducative*

> **À retenir** : la posture se construit par la **réflexivité**.
""",
        "990.md": f"""## Lexique

| Terme | Définition courte |
|---|---|
| Distance relationnelle | Juste proximité dans la relation d'aide |
| Posture | Attitude professionnelle d'ensemble |
""",
    }
    mots = 0
    for nom, contenu in parties.items():
        Path(nom).write_text(contenu, encoding="utf-8")
        emettre({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": str(Path(nom).resolve()),
                                                            "content": contenu}}]}})
        time.sleep(0.05)
    texte = "TERMINÉ"
elif "infos_actuelles" in prompt:
    actuelles = re.search(r"<infos_actuelles>\n(.*?)\n</infos_actuelles>", entree, re.S).group(1)
    dates = re.findall(r"^### Séance du (.+)$", entree, re.M)
    texte = actuelles + "\n\n## Mise à jour\n- Nouvelles informations : " + (", ".join(dates) or "documents") + "\n"
elif "infos, les informations pratiques" in prompt:
    texte = """## Déroulé de l'examen
Examen **écrit** de deux heures *(séance du 08/09)*.

> **Attention** : aucune note de cours autorisée.

## Travaux et échéances
| Travail | Consignes | Échéance |
|---|---|---|
| Portfolio | Trois situations analysées | 15 janvier |

## Conseils de l'enseignant
- Relire ses **notes** chaque semaine.

## Ce sur quoi l'enseignant a insisté
- La **posture professionnelle** *(séance du 08/09)*.

## Ressources
*Non précisé dans les cours enregistrés ni dans les supports.*

## Organisation
- Enseignant : voir l'horaire.
"""
else:
    matiere = chercher(r"^Matière : (.+)$", "Matière")
    date = chercher(r"^Date : (.+)$", "date inconnue")
    texte = f"""# {matiere} — séance du {date}

## Contenu
### La posture professionnelle
- La **posture** se construit par l'expérience. [00:05]
- 🎯 Retenez la définition de la **distance relationnelle** (répété deux fois). [00:12]

## Infos pratiques et examen
- Examen écrit de deux heures. [00:14]

## Zones d'ombre
*Aucune.*
"""

for i in range(0, len(texte), 400):
    emettre({"type": "stream_event", "event": {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": texte[i:i + 400]}}})
emettre({"type": "result", "subtype": "success", "is_error": False, "result": texte,
         "usage": {"input_tokens": len(entree) // 4, "output_tokens": len(texte) // 4},
         "duration_ms": 120, "modelUsage": {"claude-simule": {}}})
'''


def preparer_environnement(racine: Path, *, horaire_json: Path, mermaid: Path | None = None) -> dict:
    """Variables d'environnement, faux modules et faux CLI. À appeler avant
    d'importer transcriptor."""
    racine.mkdir(parents=True, exist_ok=True)
    modules = racine / "modules"
    (modules / "faster_whisper").mkdir(parents=True, exist_ok=True)
    (modules / "faster_whisper" / "__init__.py").write_text(FAUX_WHISPER, encoding="utf-8")
    bin_ = racine / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "faux_claude.py").write_text(FAUX_CLAUDE, encoding="utf-8")
    # Le faux CLI passe devant le vrai dans le PATH : jamais d'appel au vrai Claude.
    if os.name == "nt":
        (bin_ / "claude.cmd").write_text(
            f'@"{sys.executable}" "{bin_ / "faux_claude.py"}" %*\r\n', encoding="utf-8")
    else:
        claude = bin_ / "claude"
        claude.write_text(f"#!/bin/sh\nexec {sys.executable} {bin_ / 'faux_claude.py'} \"$@\"\n",
                          encoding="utf-8")
        os.chmod(claude, 0o755)
    local = racine / "local"
    env = {
        "LOCALAPPDATA": str(local), "USERPROFILE": str(racine / "profil"),
        "APPDATA": str(racine / "appdata"),          # écarte %APPDATA%\npm\claude.cmd
        "TRANSCRIPTOR_SORTIE": str(racine / "profil" / "Documents" / "Transcriptor"),
        "PATH": str(bin_) + os.pathsep + os.environ.get("PATH", ""),
        "FAUX_HORAIRE": str(horaire_json), "FAUX_CLAUDE_JOURNAL": str(racine / "appels_claude.jsonl"),
        "PYTHONPATH": str(modules) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    os.environ.update(env)
    sys.path.insert(0, str(modules))
    if mermaid and mermaid.is_file():
        (local / "Transcriptor" / "vendor").mkdir(parents=True, exist_ok=True)
        cible = local / "Transcriptor" / "vendor" / "mermaid.min.js"
        if not cible.exists():
            cible.write_bytes(mermaid.read_bytes())
    else:
        # Sans mermaid.min.js fourni, pas de téléchargement pendant les tests :
        # les schémas sont remplacés par leur texte.
        os.environ["TRANSCRIPTOR_HORS_LIGNE"] = "1"
    return env


def attendre(chantier, delai: float = 120.0) -> None:
    import time
    fin = time.time() + delai
    while chantier.en_cours() and time.time() < fin:
        time.sleep(0.1)
    assert not chantier.en_cours(), "le chantier ne s'est pas terminé à temps"


def texte_utile(x: str) -> str:
    return textwrap.dedent(x).strip()
