# -*- coding: utf-8 -*-
"""
En-tête des fichiers de consignes : un bloc YAML entre deux lignes « --- ».

    ---
    description: Support complet en 10 sections
    pdf:
      couleur: "#1e4e8c"
      nouvelle_page: oui
    ---
    # Consignes …            <- le corps : seul texte envoyé à Claude

Partagé par transcriptor.py (liste des modèles, consignes envoyées à Claude)
et generate_pdf.py (réglages de mise en page). L'en-tête est facultatif :
sans lui, tout le fichier est la consigne et le PDF prend ses réglages par
défaut.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Réglages de mise en page reconnus sous `pdf:`, avec leur valeur par défaut.
# Les valeurs par défaut reproduisent la mise en page historique.
REGLAGES_PDF: dict[str, Any] = {
    "couverture": True,                       # bloc titre en haut de la page 1
    "surtitre": "Support de révision",        # petite ligne au-dessus du titre
    "titre": "{Thème de la séance}",          # {Champ} = valeur lue dans le document
    "sous_titre": "{Matière} · {Durée}",
    "pied_de_page": "{Matière}",
    "couleur": "#1e4e8c",
    "niveau_sections": "auto",                # 1 à 4, ou auto
    "nouvelle_page": True,                    # chaque grande section sur une nouvelle page
    "cartes": ["FICHE"],                      # titres encadrés en cartes insécables
    "reponses": "visibles",                   # visibles | masquees | fin
    "titre_corrige": "Corrigé",               # titre de la section si reponses: fin
    "sommaire": False,
    "format": "A4",                           # A3, A4, A5, Letter, Legal
    "orientation": "portrait",                # portrait | paysage
    "marges": "14mm 14mm 16mm 14mm",          # haut droite bas gauche (comme en CSS)
    "taille_texte": "10.5pt",
    "police": "",                             # vide = police habituelle
    "css": "",                                # CSS libre, appliqué en dernier
}

# Autres orthographes acceptées pour les clés.
_ALIAS = {
    "sous-titre": "sous_titre", "soustitre": "sous_titre",
    "pied": "pied_de_page", "pied-de-page": "pied_de_page", "pieddepage": "pied_de_page",
    "niveau": "niveau_sections", "sections": "niveau_sections",
    "saut_de_page": "nouvelle_page", "sauts_de_page": "nouvelle_page",
    "carte": "cartes", "fiches": "cartes",
    "reponse": "reponses", "taille": "taille_texte", "marge": "marges",
    "couleur_principale": "couleur", "corrige": "titre_corrige",
}

_FORMATS = {"a3": "A3", "a4": "A4", "a5": "A5", "letter": "Letter", "legal": "Legal"}

_RE_ENTETE = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def sans_accent(texte: str) -> str:
    plat = unicodedata.normalize("NFD", str(texte).lower())
    return "".join(c for c in plat if unicodedata.category(c) != "Mn")


def _cle(k: Any) -> str:
    c = re.sub(r"[\s-]+", "_", sans_accent(k).strip())
    return _ALIAS.get(c, _ALIAS.get(c.replace("_", "-"), c))


def vrai(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return sans_accent(v).strip() in ("oui", "o", "yes", "y", "true", "vrai", "1", "on")


@dataclass
class Consignes:
    chemin: Optional[Path]
    meta: dict
    corps: str
    pdf: dict
    avertissements: list[str] = field(default_factory=list)

    @property
    def description(self) -> str:
        return " ".join(str(self.meta.get("description") or "").split())


def lire(chemin: Path | str) -> Consignes:
    texte = Path(chemin).read_text(encoding="utf-8-sig", errors="replace")
    return depuis_texte(texte, Path(chemin))


def depuis_texte(texte: str, chemin: Optional[Path] = None) -> Consignes:
    meta, corps, avert = separer(texte)
    pdf, avert_pdf = reglages_pdf(meta)
    return Consignes(chemin, meta, corps, pdf, avert + avert_pdf)


def separer(texte: str) -> tuple[dict, str, list[str]]:
    """(en-tête, corps, avertissements).

    Un document peut commencer par un simple trait « --- » (Claude le fait
    avant une fiche) : le bloc n'est pris pour un en-tête que s'il se lit
    comme une suite de « clé: valeur ». Un en-tête visiblement voulu (il
    contient `pdf:` ou `description:`) mais mal écrit est retiré quand même
    et signalé : le corps reste utilisable, avec les réglages par défaut."""
    texte = texte.lstrip("\ufeff")
    m = _RE_ENTETE.match(texte)
    if not m:
        return {}, texte, []
    voulu = re.search(r"^\s*(pdf|description)\s*:", m.group(1), re.M) is not None
    try:
        meta = _yaml(m.group(1))
    except Exception as exc:  # noqa: BLE001
        if not voulu:
            return {}, texte, []
        premiere = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
        return {}, texte[m.end():], [
            f"en-tête illisible, réglages par défaut utilisés ({premiere[:120]})"]
    if isinstance(meta, dict) and meta and (
            voulu or all(re.fullmatch(r"[\w-]+", str(k)) for k in meta)):
        return meta, texte[m.end():], []
    if voulu:
        return {}, texte[m.end():], [
            "en-tête ignoré : ce n'est pas une suite de lignes « clé: valeur »"]
    return {}, texte, []


# ----------------------------------------------------------------------
# YAML : PyYAML s'il est là (il arrive avec faster-whisper), sinon un
# lecteur minimal suffisant pour ces en-têtes.
# ----------------------------------------------------------------------
def _yaml(brut: str) -> Any:
    brut = _guillemets_implicites(brut)
    try:
        import yaml  # type: ignore
    except ImportError:
        return _mini_yaml(brut)
    return yaml.safe_load(brut)


def _guillemets_implicites(brut: str) -> str:
    """Met entre guillemets les valeurs que YAML lirait de travers :
    `titre: {Matière} — {Durée}` (pris pour un dictionnaire) et
    `couleur: #1e4e8c` (pris pour un commentaire). Le contenu des blocs
    `|` (CSS libre) n'est pas touché."""
    sortie: list[str] = []
    bloc: Optional[int] = None
    for ligne in brut.splitlines():
        retrait = len(ligne) - len(ligne.lstrip())
        if bloc is not None:
            if not ligne.strip() or retrait > bloc:
                sortie.append(ligne)
                continue
            bloc = None
        m = re.match(r"^(\s*[^\s:#-][^:#]*:\s+)(\S.*)$", ligne)
        valeur = m.group(2) if m else ""
        if m and valeur.startswith("#"):
            c = re.match(r"#[0-9A-Fa-f]{3,8}\b", valeur)
            ligne = m.group(1) + json.dumps(c.group(0) if c else "")
        elif m and re.match(r"[|>][+-]?\s*(#.*)?$", valeur):
            bloc = retrait
        elif m and not valeur.startswith(("'", '"', "[")):
            valeur = re.split(r"\s+#\s", valeur, maxsplit=1)[0].rstrip()
            # « {Champ} … » serait lu comme un dictionnaire ; « Fiches : … »
            # (espace française avant les deux-points) comme une clé.
            if valeur.startswith("{") or ": " in valeur or valeur.endswith(":"):
                ligne = m.group(1) + json.dumps(valeur, ensure_ascii=False)
        sortie.append(ligne)
    return "\n".join(sortie)


def _scalaire(v: str) -> Any:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return json.loads(v) if v[0] == '"' else v[1:-1].replace("''", "'")
    bas = v.lower()
    if bas in ("true", "yes", "on"):
        return True
    if bas in ("false", "no", "off"):
        return False
    if bas in ("null", "~", ""):
        return None
    if v.startswith("[") and v.endswith("]"):
        return [_scalaire(x) for x in v[1:-1].split(",") if x.strip()]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _mini_yaml(brut: str) -> dict:
    racine: dict = {}
    pile: list[tuple[int, dict]] = [(-1, racine)]
    bloc: Optional[dict] = None

    def fermer(b: dict) -> None:
        lignes = b["lignes"]
        while lignes and not lignes[-1].strip():
            lignes.pop()
        retrait = min((len(l) - len(l.lstrip()) for l in lignes if l.strip()), default=0)
        b["cible"][b["cle"]] = "\n".join(l[retrait:] for l in lignes) + ("\n" if lignes else "")

    for ligne in brut.splitlines():
        retrait = len(ligne) - len(ligne.lstrip())
        if bloc is not None:
            if not ligne.strip() or retrait > bloc["retrait"]:
                bloc["lignes"].append(ligne)
                continue
            fermer(bloc)
            bloc = None
        if not ligne.strip() or ligne.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*([^:#]+?)\s*:(?:\s+(.*))?$", ligne)
        if not m:
            raise ValueError(f"ligne incomprise : {ligne.strip()[:60]}")
        cle, valeur = m.group(1), (m.group(2) or "").strip()
        cite = re.match(r'^("(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\')\s*(?:#.*)?$', valeur)
        if cite:
            valeur = cite.group(1)                                  # « "…"  # commentaire »
        else:
            valeur = re.split(r"\s+#", valeur, maxsplit=1)[0].strip()
        while pile[-1][0] >= retrait:
            pile.pop()
        parent = pile[-1][1]
        if re.fullmatch(r"[|>][+-]?", valeur):
            bloc = {"cle": cle, "cible": parent, "retrait": retrait, "lignes": []}
            parent[cle] = ""
        elif valeur == "":
            parent[cle] = {}
            pile.append((retrait, parent[cle]))
        else:
            parent[cle] = _scalaire(valeur)
    if bloc is not None:
        fermer(bloc)
    return racine


# ----------------------------------------------------------------------
# Réglages PDF : valeurs par défaut + ce que dit l'en-tête, types vérifiés.
# ----------------------------------------------------------------------
def _texte(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        # `titre: {Thème}` non protégé : YAML y voit {"Thème": None}.
        return " ".join("{%s}" % k for k in v)
    if isinstance(v, (list, tuple)):
        return " ".join(_texte(x) for x in v)
    return str(v)


def reglages_pdf(meta: dict) -> tuple[dict, list[str]]:
    r = {k: (list(v) if isinstance(v, list) else v) for k, v in REGLAGES_PDF.items()}
    avert: list[str] = []
    brut = meta.get("pdf") if isinstance(meta, dict) else None
    if brut is None:
        return r, avert
    if not isinstance(brut, dict):
        return r, ["« pdf: » doit être suivi de réglages indentés, un par ligne"]

    for k, v in brut.items():
        cle = _cle(k)
        if cle not in REGLAGES_PDF:
            avert.append(f"réglage pdf inconnu ignoré : {k}")
            continue
        try:
            r[cle] = _normaliser(cle, v)
        except ValueError as exc:
            avert.append(f"pdf.{k} : {exc} — valeur par défaut gardée")
    return r, avert


def _normaliser(cle: str, v: Any) -> Any:
    if cle in ("couverture", "nouvelle_page", "sommaire"):
        return vrai(v)
    if cle == "niveau_sections":
        if v is None or sans_accent(v).strip() in ("auto", "automatique", ""):
            return "auto"
        try:
            n = int(str(v).strip().lstrip("#") or 0) if not str(v).strip().startswith("#") \
                else len(str(v).strip())
        except ValueError:
            raise ValueError("attendu : auto, ou un niveau de 1 à 4") from None
        if not 1 <= n <= 4:
            raise ValueError("attendu : auto, ou un niveau de 1 à 4")
        return n
    if cle == "cartes":
        if v is None or v is False or sans_accent(_texte(v)).strip() in ("", "non", "aucune", "no", "false"):
            return []
        morceaux = v if isinstance(v, list) else str(v).split(",")
        return [str(x).strip() for x in morceaux if str(x).strip()]
    if cle == "reponses":
        s = sans_accent(_texte(v)).strip()
        if s.startswith(("vis", "oui", "affich")) or v is True:
            return "visibles"
        if s.startswith(("mas", "cach", "non")) or v is False:
            return "masquees"
        if "fin" in s or s.startswith("corrig"):
            return "fin"
        raise ValueError("attendu : visibles, masquées ou fin")
    if cle == "format":
        s = sans_accent(_texte(v)).strip()
        if s not in _FORMATS:
            raise ValueError("attendu : A3, A4, A5, Letter ou Legal")
        return _FORMATS[s]
    if cle == "orientation":
        s = sans_accent(_texte(v)).strip()
        if s.startswith(("pay", "land", "ita")):
            return "paysage"
        if s.startswith("port"):
            return "portrait"
        raise ValueError("attendu : portrait ou paysage")
    if cle == "couleur":
        s = _texte(v).strip()
        if not re.fullmatch(r"#[0-9A-Fa-f]{3}|#[0-9A-Fa-f]{6}|[a-zA-Z]{3,20}", s):
            raise ValueError("attendu : une couleur comme \"#1e4e8c\" ou darkred")
        return s
    if cle == "marges":
        parts = str(v).replace(",", " ").split() if not isinstance(v, (int, float)) else [str(v)]
        if not 1 <= len(parts) <= 4:
            raise ValueError("attendu : 1 à 4 valeurs, comme 14mm ou 12mm 15mm")
        norm = []
        for p in parts:
            if re.fullmatch(r"\d+(\.\d+)?", p):
                p += "mm"
            if not re.fullmatch(r"\d+(\.\d+)?(mm|cm|in|px|pt)", p):
                raise ValueError(f"marge incomprise : {p}")
            norm.append(p)
        return " ".join(norm)
    if cle == "taille_texte":
        s = str(v).strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            s += "pt"
        if not re.fullmatch(r"\d+(\.\d+)?(pt|px)", s):
            raise ValueError("attendu : une taille comme 10.5pt")
        return s
    if cle == "police":
        s = _texte(v).strip().strip("\"'")
        if re.search(r"[;{}<>]", s):
            raise ValueError("nom de police invalide")
        return s
    # textes libres : surtitre, titre, sous_titre, pied_de_page, titre_corrige, css
    return _texte(v)
