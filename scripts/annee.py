# -*- coding: utf-8 -*-
"""
Mode année : un dossier par année d'études, rangé ainsi —

    Année 1\\
    ├── 00 - Audios et horaire\\     tous les enregistrements + l'horaire (PDF, image…)
    │   ├── horaire.pdf
    │   ├── horaire.json            l'horaire lu par Transcriptor (modifiable)
    │   ├── 2026-10-05 08_31_02.mp3 …  les nouveaux
    │   ├── Traités\\2026-10\\        ceux déjà dans les fiches, rangés par mois
    │   ├── Écartés\\                « pas un cours » et doublons
    │   └── Transcriptions\\         une transcription par enregistrement
    ├── Psychologie\\                un dossier par matière : supports (PDF Moodle…)
    │   ├── Psychologie - Fiches résumés.docx
    │   ├── Psychologie - Infos importantes.docx
    │   └── _Transcriptor\\          notes de séance, Markdown, journaux, versions
    └── _Transcriptor\\              registre des enregistrements, classement manuel, cache

Ce module ne transcrit rien et n'appelle pas Claude : il lit le dossier,
l'horaire et la date de chaque enregistrement, et dit à quelle matière — et
à quelle séance — appartient chaque audio.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

EXT_AUDIO = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".opus", ".mp4", ".aac",
             ".wma", ".mpeg", ".mpg", ".mpga", ".mp2", ".webm", ".mkv", ".mov",
             ".avi", ".wmv", ".aiff", ".aif", ".amr", ".3gp", ".m4b", ".m4v", ".ts")
EXT_HORAIRE = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
               ".heic", ".heif", ".xlsx", ".xlsm", ".csv", ".ics", ".docx", ".txt",
               ".html", ".htm", ".json")
# Supports de cours cherchés dans les dossiers des matières (même liste que
# supports.EXT_SUPPORTS) : les anciens formats (.ppt, .doc…) sont trouvés pour
# être signalés « à convertir ».
EXT_SUPPORTS = (".pdf", ".pptx", ".ppsx", ".pptm", ".ppsm", ".potx", ".ppt", ".pps",
                ".docx", ".docm", ".dotx", ".doc", ".rtf", ".odt", ".odp",
                ".txt", ".md", ".jpg", ".jpeg", ".png",
                ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif")

DOSSIER_AUDIOS = "00 - Audios et horaire"
DOSSIER_TECHNIQUE = "_Transcriptor"
DOSSIER_TRANSCRIPTIONS = "Transcriptions"
HORAIRE_JSON = "horaire.json"
CLASSEMENT_JSON = "classement.json"
CONFIG_JSON = "annee.json"
CACHE_AUDIOS = "audios.json"
REGISTRE_JSON = "registre.json"   # empreinte d'un audio -> sa transcription, son dernier chemin
DOSSIER_TRAITES = "Traités"       # audios déjà intégrés aux fiches, par mois
DOSSIER_ECARTES = "Écartés"       # « pas un cours » et doublons
FICHES = "Fiches résumés"
INFOS = "Infos importantes"
IGNORE = "__ignore__"          # classement manuel : « pas un cours »

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre")

_JOURS_ALIAS = {
    "lun": 0, "mon": 0, "monday": 0, "ma": 1, "mar": 1, "tue": 1, "tuesday": 1,
    "mer": 2, "wed": 2, "wednesday": 2, "jeu": 3, "thu": 3, "thursday": 3,
    "ven": 4, "fri": 4, "friday": 4, "sam": 5, "sat": 5, "saturday": 5,
    "dim": 6, "sun": 6, "sunday": 6,
}

# Couleurs nommées acceptées dans horaire.json (et pour les palettes).
COULEURS_NOMMEES = {
    "rose": "#F8BBD0", "bleu": "#BBDEFB", "violet": "#D1C4E9", "vert": "#C8E6C9",
    "orange": "#FFE0B2", "jaune": "#FFF59D", "rouge": "#FFCDD2", "turquoise": "#B2EBF2",
    "cyan": "#B2EBF2", "gris": "#E0E0E0", "marron": "#D7CCC8", "brun": "#D7CCC8",
    "beige": "#F5E6CC", "saumon": "#FFCCBC", "lilas": "#E1BEE7", "mauve": "#E1BEE7",
    "menthe": "#C8F0DC", "ciel": "#B3E5FC", "corail": "#FFAB91", "kaki": "#DCE3B5",
    "bordeaux": "#C48B9F", "magenta": "#F48FB1", "indigo": "#C5CAE9", "noir": "#424242",
    "blanc": "#FFFFFF", "pink": "#F8BBD0", "blue": "#BBDEFB", "purple": "#D1C4E9",
    "green": "#C8E6C9", "yellow": "#FFF59D", "red": "#FFCDD2", "grey": "#E0E0E0",
    "gray": "#E0E0E0",
}
# Attribuées dans l'ordre aux matières dont l'horaire ne donne pas la couleur.
PALETTE_MATIERES = ("#F8BBD0", "#BBDEFB", "#D1C4E9", "#C8E6C9", "#FFE0B2", "#B2EBF2",
                    "#FFF59D", "#D7CCC8", "#F0F4C3", "#FFCCBC", "#C5CAE9", "#DCEDC8")

# Mots qui désignent un horaire dans un nom de fichier. Pas « agenda » ni
# « planning » : un agenda d'étudiant n'est pas l'horaire des cours.
_MOTS_HORAIRE = ("horaire", "emploi du temps", "emploi_du_temps", "emploidutemps",
                 "timetable", "schedule")


# ----------------------------------------------------------------------
# Petits outils
# ----------------------------------------------------------------------
def plat(texte: Any) -> str:
    """Minuscules sans accents ni ponctuation : pour comparer des noms."""
    s = unicodedata.normalize("NFD", str(texte or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def nom_dossier(texte: str, limite: int = 60) -> str:
    """Nom de dossier sûr pour Windows (et lisible)."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", texte or "")
    s = " ".join(s.split()).strip(" .")[:limite].strip(" .")
    if not s:
        s = "Matière"
    if s.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                   *(f"LPT{i}" for i in range(1, 10))}:
        s += "_"
    return s


def date_fr(d: dt.date, *, jour: bool = True, annee: bool = True) -> str:
    """« lundi 5 octobre 2026 »."""
    texte = f"{d.day}{'er' if d.day == 1 else ''} {MOIS[d.month - 1]}"
    if annee:
        texte += f" {d.year}"
    return f"{JOURS[d.weekday()]} {texte}" if jour else texte


def hhmm_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def duree_lisible(secondes: float) -> str:
    s = int(round(secondes or 0))
    if s <= 0:
        return ""
    h, m = s // 3600, (s % 3600) // 60
    return f"{h} h {m:02d}" if h else f"{m} min"


def _lire_json(chemin: Path, defaut: Any) -> Any:
    try:
        return json.loads(chemin.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return defaut
    except Exception:  # noqa: BLE001
        return defaut


def _ecrire_json(chemin: Path, donnees: Any) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_name(chemin.name + ".tmp")
    tmp.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, chemin)


def signature(chemins: list[Path], extra: str = "") -> str:
    """Empreinte d'un ensemble de fichiers (noms, tailles, dates) : elle change
    dès qu'un fichier est ajouté, retiré ou modifié."""
    h = hashlib.sha1(extra.encode("utf-8"))
    for p in sorted(chemins, key=lambda x: str(x).lower()):
        try:
            st = p.stat()
            h.update(f"{p.name}|{st.st_size}|{int(st.st_mtime)}\n".encode("utf-8"))
        except OSError:
            h.update(f"{p.name}|absent\n".encode("utf-8"))
    return h.hexdigest()[:16]


# ----------------------------------------------------------------------
# Couleurs
# ----------------------------------------------------------------------
def couleur_normale(v: Any) -> str:
    """« #RRGGBB » en majuscules, ou "" si la valeur n'est pas une couleur."""
    s = str(v or "").strip()
    if not s:
        return ""
    if plat(s) in COULEURS_NOMMEES:
        return COULEURS_NOMMEES[plat(s)]
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})", s)
    if m:
        return "#" + m.group(1).upper()
    m = re.fullmatch(r"#?([0-9A-Fa-f])([0-9A-Fa-f])([0-9A-Fa-f])", s)
    if m:
        return "#" + "".join(x * 2 for x in m.groups()).upper()
    m = re.fullmatch(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3}).*\)", s, re.I)
    if m:
        r, g, b = (min(255, int(x)) for x in m.groups())
        return f"#{r:02X}{g:02X}{b:02X}"
    return ""


def rvb(hexa: str) -> tuple[int, int, int]:
    h = couleur_normale(hexa) or "#000000"
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def luminance(hexa: str) -> float:
    def canal(c: int) -> float:
        x = c / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = rvb(hexa)
    return 0.2126 * canal(r) + 0.7152 * canal(g) + 0.0722 * canal(b)


def texte_sur(fond: str) -> str:
    """Noir ou blanc, le plus lisible sur ce fond."""
    lf = luminance(fond)
    contraste_noir = (lf + 0.05) / 0.05
    contraste_blanc = 1.05 / (lf + 0.05)
    return "#1A1A1A" if contraste_noir >= contraste_blanc else "#FFFFFF"


def melanger(hexa: str, avec: str, part: float) -> str:
    """`part` de la couleur `hexa`, le reste de `avec`."""
    a, b = rvb(hexa), rvb(avec)
    c = [round(x * part + y * (1 - part)) for x, y in zip(a, b)]
    return "#{:02X}{:02X}{:02X}".format(*c)


def foncee(hexa: str, cible_luminance: float = 0.12) -> str:
    """La même teinte, assombrie jusqu'à être lisible en texte sur du blanc."""
    c = couleur_normale(hexa) or "#444444"
    for _ in range(24):
        if luminance(c) <= cible_luminance:
            return c
        c = melanger(c, "#000000", 0.88)
    return c


# ----------------------------------------------------------------------
# Horaire
# ----------------------------------------------------------------------
@dataclass
class Matiere:
    nom: str
    court: str = ""
    couleur: str = ""
    enseignant: str = ""
    code: str = ""
    ue: str = ""                         # unité d'enseignement (« UE 2C »), qui donne souvent la couleur
    dossier: str = ""                    # nom du dossier de la matière dans l'année
    alias: list[str] = field(default_factory=list)
    vocabulaire: str = ""                # termes redonnés à Whisper (facultatif)

    @property
    def titre(self) -> str:
        return self.court or self.nom

    @property
    def nom_de_dossier(self) -> str:
        return nom_dossier(self.dossier or self.court or self.nom)

    def noms(self) -> list[str]:
        return [x for x in (self.nom, self.court, self.dossier, *self.alias) if x]


@dataclass
class Creneau:
    jour: int                            # 0 = lundi
    debut: int                           # minutes depuis minuit
    fin: int
    matiere: str
    salle: str = ""
    du: Optional[dt.date] = None
    au: Optional[dt.date] = None
    dates: list[dt.date] = field(default_factory=list)
    semaines: Any = ""                   # "", "paires", "impaires" ou [numéros ISO]

    def actif(self, d: dt.date) -> bool:
        if self.dates:
            return d in self.dates
        if d.weekday() != self.jour:
            return False
        if self.du and d < self.du:
            return False
        if self.au and d > self.au:
            return False
        semaine = d.isocalendar()[1]
        if self.semaines == "paires":
            return semaine % 2 == 0
        if self.semaines == "impaires":
            return semaine % 2 == 1
        if isinstance(self.semaines, list) and self.semaines:
            return semaine in self.semaines
        return True

    def bornes(self, d: dt.date) -> tuple[dt.datetime, dt.datetime]:
        minuit = dt.datetime.combine(d, dt.time())
        return minuit + dt.timedelta(minutes=self.debut), minuit + dt.timedelta(minutes=self.fin)

    @property
    def plage(self) -> str:
        return f"{hhmm_minutes(self.debut)}–{hhmm_minutes(self.fin)}"


def _heure(v: Any) -> int:
    s = str(v or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?:[:h.](\d{2})?)?", s)
    if not m:
        raise ValueError(f"heure incomprise : {v!r}")
    h, mn = int(m.group(1)), int(m.group(2) or 0)
    if h > 24 or mn > 59:
        raise ValueError(f"heure impossible : {v!r}")
    return h * 60 + mn


def _jour(v: Any) -> int:
    if isinstance(v, int) and 1 <= v <= 7:
        return v - 1                     # numérotation ISO : 1 = lundi
    s = plat(v)
    for i, nom in enumerate(JOURS):
        if s == plat(nom):
            return i
    if s in _JOURS_ALIAS:
        return _JOURS_ALIAS[s]
    if s[:3] in _JOURS_ALIAS and len(s) >= 3:
        return _JOURS_ALIAS[s[:3]]
    raise ValueError(f"jour incompris : {v!r}")


def _date(v: Any) -> Optional[dt.date]:
    if v in (None, ""):
        return None
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    for motif in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s[:10], motif).date()
        except ValueError:
            continue
    raise ValueError(f"date incomprise : {v!r}")


@dataclass
class Horaire:
    matieres: list[Matiere] = field(default_factory=list)
    creneaux: list[Creneau] = field(default_factory=list)
    annee: str = ""
    source: str = ""
    formation: str = ""                  # « Bachelier en éducation spécialisée »
    avertissements: list[str] = field(default_factory=list)

    # -- lecture tolérante ---------------------------------------------
    @classmethod
    def depuis_dict(cls, d: Any) -> "Horaire":
        h = cls()
        if not isinstance(d, dict):
            h.avertissements.append("horaire.json ne contient pas un objet JSON")
            return h
        h.annee = str(d.get("annee") or "")
        h.source = str(d.get("source") or "")
        h.formation = " ".join(str(d.get("formation") or "").split())
        vus: set[str] = set()
        for brut in d.get("matieres") or []:
            if isinstance(brut, str):
                brut = {"nom": brut}
            if not isinstance(brut, dict) or not str(brut.get("nom") or "").strip():
                h.avertissements.append(f"matière sans nom ignorée : {brut!r}"[:120])
                continue
            nom = " ".join(str(brut["nom"]).split())
            if plat(nom) in vus:
                continue
            vus.add(plat(nom))
            alias = brut.get("alias") or []
            m = Matiere(
                nom=nom,
                court=" ".join(str(brut.get("court") or "").split()),
                couleur=couleur_normale(brut.get("couleur")),
                enseignant=" ".join(str(brut.get("enseignant") or "").split()),
                code=str(brut.get("code") or "").strip(),
                ue=" ".join(str(brut.get("ue") or "").split()),
                dossier=nom_dossier(str(brut["dossier"])) if brut.get("dossier") else "",
                alias=[str(a) for a in (alias if isinstance(alias, list) else [alias]) if str(a).strip()],
                vocabulaire=str(brut.get("vocabulaire") or "").strip(),
            )
            if brut.get("couleur") and not m.couleur:
                h.avertissements.append(f"{nom} : couleur incomprise ({brut.get('couleur')})")
            h.matieres.append(m)

        for i, brut in enumerate(d.get("creneaux") or [], 1):
            if not isinstance(brut, dict):
                h.avertissements.append(f"créneau {i} ignoré : ce n'est pas un objet")
                continue
            try:
                nom = " ".join(str(brut.get("matiere") or "").split())
                m = h.matiere(nom)
                if not m:
                    if not nom:
                        raise ValueError("matière manquante")
                    m = Matiere(nom=nom)          # matière citée seulement dans un créneau
                    h.matieres.append(m)
                semaines = brut.get("semaines") or ""
                if isinstance(semaines, str):
                    s = plat(semaines)
                    semaines = ("paires" if s.startswith("pair") else
                                "impaires" if s.startswith("impair") else
                                [int(x) for x in re.findall(r"\d+", semaines)] if re.search(r"\d", s) else "")
                elif isinstance(semaines, list):
                    semaines = [int(x) for x in semaines if str(x).strip().isdigit()]
                dates = [x for x in (_date(v) for v in (brut.get("dates") or [])) if x]
                if brut.get("date") and not dates:
                    dates = [_date(brut["date"])]
                jour = _jour(brut["jour"]) if brut.get("jour") not in (None, "") else (
                    dates[0].weekday() if dates else None)
                if jour is None:
                    raise ValueError("jour manquant")
                c = Creneau(jour=jour, debut=_heure(brut.get("debut")), fin=_heure(brut.get("fin")),
                            matiere=m.nom, salle=str(brut.get("salle") or "").strip(),
                            du=_date(brut.get("du")), au=_date(brut.get("au")),
                            dates=dates, semaines=semaines)
                if c.fin <= c.debut:
                    raise ValueError(f"fin ({hhmm_minutes(c.fin)}) avant le début ({hhmm_minutes(c.debut)})")
                h.creneaux.append(c)
            except (KeyError, ValueError, TypeError) as exc:
                h.avertissements.append(f"créneau {i} ignoré : {exc}")

        # Horaire en liste datée (une ligne par cours et par jour) : regroupé en
        # créneaux, un par matière, jour de la semaine et plage horaire.
        regroupes: dict[tuple, Creneau] = {}
        profs: dict[str, list[str]] = {}
        for i, brut in enumerate(d.get("seances") or [], 1):
            if not isinstance(brut, dict):
                h.avertissements.append(f"séance {i} ignorée : ce n'est pas un objet")
                continue
            try:
                jour = _date(brut.get("date"))
                if not jour:
                    raise ValueError("date manquante")
                nom = " ".join(str(brut.get("matiere") or "").split())
                m = h.matiere(nom)
                if not m:
                    if not nom:
                        raise ValueError("matière manquante")
                    m = Matiere(nom=nom)
                    h.matieres.append(m)
                debut, fin = _heure(brut.get("debut")), _heure(brut.get("fin"))
                if fin <= debut:
                    raise ValueError(f"fin ({hhmm_minutes(fin)}) avant le début ({hhmm_minutes(debut)})")
                cle = (m.nom, jour.weekday(), debut, fin)
                c = regroupes.get(cle)
                if not c:
                    c = Creneau(jour=jour.weekday(), debut=debut, fin=fin, matiere=m.nom,
                                salle=str(brut.get("salle") or "").strip())
                    regroupes[cle] = c
                    h.creneaux.append(c)
                if jour not in c.dates:
                    c.dates.append(jour)
                prof = " ".join(str(brut.get("enseignant") or "").split())
                if prof and prof not in profs.setdefault(m.nom, []):
                    profs[m.nom].append(prof)
            except (ValueError, TypeError) as exc:
                h.avertissements.append(f"séance {i} ignorée : {exc}")
        for c in regroupes.values():
            c.dates.sort()
        for m in h.matieres:
            if not m.enseignant and profs.get(m.nom):
                m.enseignant = ", ".join(profs[m.nom])

        # Une couleur pour chaque matière, même si l'horaire n'en donne pas.
        prises = {m.couleur for m in h.matieres if m.couleur}
        libres = [c for c in PALETTE_MATIERES if c not in prises] or list(PALETTE_MATIERES)
        for m in h.matieres:
            if not m.couleur:
                m.couleur = libres.pop(0) if libres else PALETTE_MATIERES[0]
        return h

    def vers_dict(self) -> dict:
        def creneau(c: Creneau) -> dict:
            d: dict[str, Any] = {"jour": JOURS[c.jour], "debut": hhmm_minutes(c.debut),
                                 "fin": hhmm_minutes(c.fin), "matiere": c.matiere}
            if c.salle:
                d["salle"] = c.salle
            if c.du:
                d["du"] = c.du.isoformat()
            if c.au:
                d["au"] = c.au.isoformat()
            if c.dates:
                d["dates"] = [x.isoformat() for x in c.dates]
            if c.semaines:
                d["semaines"] = c.semaines
            return d

        def matiere(m: Matiere) -> dict:
            d: dict[str, Any] = {"nom": m.nom}
            for cle in ("court", "couleur", "ue", "enseignant", "code", "dossier", "vocabulaire"):
                if getattr(m, cle):
                    d[cle] = getattr(m, cle)
            if m.alias:
                d["alias"] = m.alias
            return d

        return {"annee": self.annee, "source": self.source, "formation": self.formation,
                "matieres": [matiere(m) for m in self.matieres],
                "creneaux": [creneau(c) for c in self.creneaux]}

    # -- consultation --------------------------------------------------
    def matiere(self, nom: str) -> Optional[Matiere]:
        cle = plat(nom)
        if not cle:
            return None
        for m in self.matieres:
            if cle in (plat(x) for x in m.noms()):
                return m
        return None

    def creneaux_du(self, d: dt.date) -> list[Creneau]:
        return [c for c in self.creneaux if c.actif(d)]

    def creneaux_de(self, nom: str) -> list[Creneau]:
        return [c for c in self.creneaux if c.matiere == nom]

    def periode(self) -> Optional[tuple[dt.date, dt.date]]:
        """Première et dernière date couvertes par l'horaire, si elles sont
        connues (dates explicites ou bornes du/au)."""
        dates = [x for c in self.creneaux for x in (*c.dates, c.du, c.au) if x]
        return (min(dates), max(dates)) if dates else None

    def seances_prevues(self, nom: str) -> int:
        """Nombre de jours de cours d'une matière d'après un horaire daté."""
        jours = {x for c in self.creneaux_de(nom) for x in c.dates}
        return len(jours)


def horaire_depuis_ics(texte: str) -> Horaire:
    """Un calendrier .ics (export Hyperplanning, ADE, Google…) se lit sans
    Claude : un créneau par cours et par plage horaire, avec ses dates."""
    texte = re.sub(r"\r?\n[ \t]", "", texte)             # lignes repliées
    evenements: list[dict[str, str]] = []
    courant: Optional[dict[str, str]] = None
    for ligne in texte.splitlines():
        if ligne.strip() == "BEGIN:VEVENT":
            courant = {}
        elif ligne.strip() == "END:VEVENT":
            if courant is not None:
                evenements.append(courant)
            courant = None
        elif courant is not None and ":" in ligne:
            cle, _, valeur = ligne.partition(":")
            courant[cle.split(";")[0].upper()] = valeur.strip()

    def quand(v: str) -> Optional[dt.datetime]:
        m = re.match(r"(\d{8})T(\d{4})(\d{2})?(Z?)", v or "")
        if not m:
            return None
        d = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
        if m.group(4):                                      # UTC -> heure locale
            d = d.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
        return d

    regroupes: dict[tuple, Creneau] = {}
    matieres: dict[str, Matiere] = {}
    for ev in evenements:
        debut, fin = quand(ev.get("DTSTART", "")), quand(ev.get("DTEND", ""))
        resume = re.sub(r"\\([,;nN])", lambda m: " " if m.group(1) in "nN" else m.group(1),
                        ev.get("SUMMARY", "")).strip()
        if not debut or not fin or not resume:
            continue
        nom = " ".join(re.split(r"\s+[-–]\s+|\s*\(", resume)[0].split()) or resume
        matieres.setdefault(plat(nom), Matiere(nom=nom, couleur=couleur_normale(ev.get("COLOR"))))
        m = matieres[plat(nom)]
        cle = (m.nom, debut.weekday(), debut.hour * 60 + debut.minute, fin.hour * 60 + fin.minute)
        c = regroupes.get(cle)
        if not c:
            c = Creneau(jour=debut.weekday(), debut=cle[2], fin=cle[3], matiere=m.nom,
                        salle=ev.get("LOCATION", "").replace("\\,", ","))
            regroupes[cle] = c
        if debut.date() not in c.dates:
            c.dates.append(debut.date())
    h = Horaire.depuis_dict({
        "matieres": [{"nom": m.nom, "couleur": m.couleur} for m in matieres.values()],
        "creneaux": []})
    for c in regroupes.values():
        c.dates.sort()
        h.creneaux.append(c)
    return h


def json_dans(texte: str) -> Any:
    """Le premier objet JSON d'une réponse (bloc ```json ou texte brut)."""
    for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", texte or "", re.S):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    debut = (texte or "").find("{")
    while debut >= 0:
        profondeur, chaine, echap = 0, False, False
        for i in range(debut, len(texte)):
            c = texte[i]
            if chaine:
                if echap:
                    echap = False
                elif c == "\\":
                    echap = True
                elif c == '"':
                    chaine = False
            elif c == '"':
                chaine = True
            elif c == "{":
                profondeur += 1
            elif c == "}":
                profondeur -= 1
                if profondeur == 0:
                    try:
                        return json.loads(texte[debut:i + 1])
                    except json.JSONDecodeError:
                        break
        debut = texte.find("{", debut + 1)
    raise ValueError("aucun JSON lisible dans la réponse")


# ----------------------------------------------------------------------
# Date et durée d'un enregistrement
# ----------------------------------------------------------------------
@dataclass
class InfoAudio:
    chemin: Path
    relatif: str                         # chemin relatif au dossier des audios (clé)
    taille: int = 0
    mtime: float = 0.0
    debut: Optional[dt.datetime] = None  # début de l'enregistrement
    precision: str = ""                  # « heure », « jour » ou ""
    source: str = ""                     # nom du fichier | métadonnées | date du fichier
    duree: float = 0.0                   # secondes, 0 si inconnue
    titre: str = ""                      # ce que dit le nom, date retirée (titre Plaud…)
    empreinte: str = ""                  # identité par le contenu (voir empreinte_audio)
    doublon_de: str = ""                 # même contenu qu'un autre audio (son chemin relatif)

    @property
    def nom(self) -> str:
        return self.chemin.name

    @property
    def fin(self) -> Optional[dt.datetime]:
        if not self.debut:
            return None
        return self.debut + dt.timedelta(seconds=self.duree or 3600)


_RE_AMJ_HEURE = re.compile(
    r"(?<!\d)(20\d{2})[-_. ]?(0[1-9]|1[0-2])[-_. ]?(0[1-9]|[12]\d|3[01])"
    r"(?:[ T_.,-]*|\s*[àa@]\s*)(\d{1,2})[-_.:hH ]?([0-5]\d)(?:[-_.:mM ]?([0-5]\d))?(?!\d)")
_RE_JMA_HEURE = re.compile(
    r"(?<!\d)(0?[1-9]|[12]\d|3[01])[-_.](0?[1-9]|1[0-2])[-_.](20\d{2})"
    r"(?:[ T_.,-]+|\s*[àa@]\s*)(\d{1,2})[-_.:hH]([0-5]\d)(?:[-_.:mM]([0-5]\d))?(?!\d)")
_RE_AMJ = re.compile(r"(?<!\d)(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")
_RE_JMA = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[-_.](0?[1-9]|1[0-2])[-_.](20\d{2})(?!\d)")
# Titre donné par Plaud après une synthèse : « 09-30 Cours magistral_ … » —
# mois puis jour, sans année.
_RE_PLAUD = re.compile(r"^(\d{2})-(\d{2})\s+(?=\S)")


def _annee_probable(mois: int, jour: int, periode: Optional[tuple[dt.date, dt.date]],
                    repli: Optional[dt.date]) -> Optional[int]:
    """L'année d'une date « mois-jour » : celle qui tombe dans la période de
    l'horaire (à six semaines près), sinon la plus récente qui ne soit pas
    dans le futur."""
    if periode:
        debut, fin = periode[0] - dt.timedelta(days=45), periode[1] + dt.timedelta(days=45)
        for a in range(debut.year, fin.year + 1):
            try:
                if debut <= dt.date(a, mois, jour) <= fin:
                    return a
            except ValueError:
                return None
    ref = repli or dt.date.today()
    for a in (ref.year, ref.year - 1):
        try:
            if dt.date(a, mois, jour) <= ref + dt.timedelta(days=1):
                return a
        except ValueError:
            return None
    return None


def date_depuis_nom(nom: str, periode: Optional[tuple[dt.date, dt.date]] = None,
                    repli: Optional[dt.date] = None) -> tuple[Optional[dt.datetime], str]:
    """(date, précision) lue dans le nom d'un fichier : « 2026-10-05 08_31_02 »,
    « 20261005083102 », « 05-10-2026 08h31 », « 2026-10-05 » (jour seul),
    « 09-30 Cours magistral… » (titre Plaud : mois-jour, jour seul)."""
    base = Path(nom).stem
    for motif, ordre in ((_RE_AMJ_HEURE, "amj"), (_RE_JMA_HEURE, "jma")):
        for m in motif.finditer(base):
            g = m.groups()
            a, mo, j = (g[0], g[1], g[2]) if ordre == "amj" else (g[2], g[1], g[0])
            try:
                d = dt.datetime(int(a), int(mo), int(j), int(g[3]), int(g[4]), int(g[5] or 0))
            except ValueError:
                continue
            return d, "heure"
    for motif, ordre in ((_RE_AMJ, "amj"), (_RE_JMA, "jma")):
        for m in motif.finditer(base):
            g = m.groups()
            a, mo, j = (g[0], g[1], g[2]) if ordre == "amj" else (g[2], g[1], g[0])
            try:
                return dt.datetime(int(a), int(mo), int(j)), "jour"
            except ValueError:
                continue
    m = _RE_PLAUD.match(base)
    if m:
        mo, j = int(m.group(1)), int(m.group(2))
        if mo > 12 >= j:                       # « 30-09 » : jour puis mois
            mo, j = j, mo
        a = _annee_probable(mo, j, periode, repli) if 1 <= mo <= 12 else None
        if a:
            try:
                return dt.datetime(a, mo, j), "jour"
            except ValueError:
                pass
    return None, ""


def titre_depuis_nom(nom: str) -> str:
    """Le nom sans sa date ni son heure : souvent le titre donné par
    l'enregistreur (« Cours magistral: Éthique et déontologie… »)."""
    base = Path(nom).stem
    for motif in (_RE_AMJ_HEURE, _RE_JMA_HEURE, _RE_AMJ, _RE_JMA, _RE_PLAUD):
        base = motif.sub(" ", base, count=1)
    base = base.replace("_ ", ": ").replace("_", " ")
    base = " ".join(base.split()).strip(" -–:.,")
    # Un identifiant (hexadécimal, chiffres) n'est pas un titre.
    if not base or re.fullmatch(r"[0-9a-fA-F\-]{12,}|\d+|(rec|audio|record|enregistrement)\s*\d*",
                                base, re.I):
        return ""
    return base


def _date_meta(brut: Any) -> Optional[dt.datetime]:
    """Date d'une étiquette de métadonnées, ramenée à l'heure locale :
    « 2026-10-05T06:31:02.000000Z », « UTC 2026-10-05 06:31:02 »,
    « 2026-10-05 08:31:02+02:00 », « 2026:10:05 08:31:02 »."""
    s = str(brut or "").strip()
    utc = s.upper().startswith("UTC")
    s = re.sub(r"^UTC\s*", "", s, flags=re.I)
    m = re.match(r"(\d{4})[-:/](\d{2})[-:/](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?"
                 r"\s*(Z|[+-]\d{2}:?\d{2})?)?", s)
    if not m:
        return None
    a, mo, j, h, mi, se, fuseau = m.groups()
    try:
        d = dt.datetime(int(a), int(mo), int(j), int(h or 0), int(mi or 0), int(se or 0))
    except ValueError:
        return None
    if fuseau or utc:
        if not fuseau or fuseau == "Z":
            tz = dt.timezone.utc
        else:
            signe = 1 if fuseau[0] == "+" else -1
            chiffres = fuseau[1:].replace(":", "")
            tz = dt.timezone(signe * dt.timedelta(hours=int(chiffres[:2]), minutes=int(chiffres[2:4])))
        d = d.replace(tzinfo=tz).astimezone().replace(tzinfo=None)
    return d if 2000 <= d.year <= 2100 else None


def metadonnees(chemin: Path) -> tuple[Optional[dt.datetime], float]:
    """(date d'enregistrement portée par le fichier, durée en s) — par PyAV
    (fourni avec faster-whisper), sinon par ffprobe s'il est installé."""
    try:
        import av  # type: ignore

        with av.open(str(chemin)) as c:
            duree = float(c.duration / 1_000_000) if c.duration else 0.0
            if not duree:
                for f in c.streams:
                    if f.duration and f.time_base:
                        duree = max(duree, float(f.duration * f.time_base))
            etiquettes = dict(c.metadata or {})
            for f in c.streams:
                for k, v in (f.metadata or {}).items():
                    etiquettes.setdefault(k, v)
        for cle in ("creation_time", "date", "TDRC", "TDEN", "©day", "encoded_date",
                    "recorded_date", "DATE", "date_recorded"):
            d = _date_meta(etiquettes.get(cle))
            if d and (d.hour or d.minute):
                return d, duree
        return None, duree
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        return None, 0.0
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, 0.0
    try:
        sortie = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", str(chemin)],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        fmt = json.loads(sortie or "{}").get("format") or {}
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        d = next((x for x in (_date_meta(tags.get(k)) for k in ("creation_time", "date"))
                  if x and (x.hour or x.minute)), None)
        return d, float(fmt.get("duration") or 0)
    except Exception:  # noqa: BLE001
        return None, 0.0


def analyser_audio(chemin: Path, relatif: str,
                   lecteur: Callable[[Path], tuple[Optional[dt.datetime], float]] = metadonnees,
                   periode: Optional[tuple[dt.date, dt.date]] = None) -> InfoAudio:
    """Date de début et durée d'un enregistrement. Le nom du fichier passe en
    premier (Plaud et la plupart des dictaphones y écrivent l'heure de début ;
    les métadonnées, elles, portent parfois l'heure de l'export), puis les
    métadonnées, puis la date du fichier (≈ fin de l'enregistrement)."""
    st = chemin.stat()
    info = InfoAudio(chemin=chemin, relatif=relatif, taille=st.st_size, mtime=st.st_mtime,
                     titre=titre_depuis_nom(chemin.name))
    meta, info.duree = lecteur(chemin)
    du_nom, precision = date_depuis_nom(chemin.name, periode,
                                        dt.date.fromtimestamp(st.st_mtime))
    if du_nom and precision == "heure":
        info.debut, info.precision, info.source = du_nom, "heure", "nom du fichier"
    elif meta and (not du_nom or meta.date() == du_nom.date()):
        info.debut, info.precision, info.source = meta, "heure", "métadonnées"
    elif du_nom:
        info.debut, info.precision, info.source = du_nom, "jour", "nom du fichier (jour seul)"
    else:
        fin = dt.datetime.fromtimestamp(st.st_mtime)
        info.debut = fin - dt.timedelta(seconds=info.duree) if info.duree else fin
        info.precision, info.source = "heure", SOURCE_FICHIER
    return info


SOURCE_FICHIER = "date du fichier"

_BLOC_EMPREINTE = 1 << 20


def empreinte_audio(chemin: Path, taille: Optional[int] = None) -> str:
    """Identité d'un enregistrement par son contenu : taille, premier et
    dernier mégaoctet. Renommé ou déplacé, un audio garde son empreinte (et
    donc sa transcription, son classement…) ; deux copies du même fichier
    ont la même. Deux Mo lus, une seule fois par fichier (cache)."""
    taille = chemin.stat().st_size if taille is None else taille
    h = hashlib.sha1(str(taille).encode())
    with open(chemin, "rb") as f:
        h.update(f.read(_BLOC_EMPREINTE))
        if taille > 2 * _BLOC_EMPREINTE:
            f.seek(-_BLOC_EMPREINTE, os.SEEK_END)
            h.update(f.read(_BLOC_EMPREINTE))
    return h.hexdigest()[:20]


def ecarter_dates_de_copie(infos: list[InfoAudio]) -> None:
    """La date d'un fichier n'est celle de l'enregistrement que s'il vient
    tout droit du dictaphone. Quand plusieurs audios de jours différents
    portent la même heure de fichier, ils ont été copiés ensemble : cette
    date ne dit rien, l'audio est à classer à la main."""
    for i in infos:
        if i.source != SOURCE_FICHIER:
            continue
        voisins = [x for x in infos if x is not i and abs(x.mtime - i.mtime) <= 1800]
        jours = {x.debut.date() for x in voisins if x.debut and x.source != SOURCE_FICHIER}
        if len(voisins) >= 2 and (len(jours) > 1 or not jours):
            i.debut, i.precision = None, ""
            i.source = "inconnue (fichier copié avec d'autres)"


# ----------------------------------------------------------------------
# Classement : quel cours à cette heure-là ?
# ----------------------------------------------------------------------
TOLERANCE = 45          # min : un enregistrement lancé un peu avant ou fini un peu après

_MOTS_VIDES = {plat(x) for x in (
    "de du des la le les un une et ou en au aux pour par sur dans avec sans son sa ses "
    "leur leurs ce cet cette ces qui que quoi dont entre vers chez comme plus "
    "cours magistral magistrale séance partie introduction conférence notes "
    "enregistrement analyse théorie chapitre suite fin début").split()}


def _mots(texte: str) -> set[str]:
    return {w for w in plat(texte).split() if len(w) >= 4 and w not in _MOTS_VIDES}


def _proches(a: str, b: str) -> bool:
    """Même mot à la flexion près : « profession » / « professionnelle »,
    « méthodologie » / « méthodologies », « psychologie » / « psychologique »."""
    if a == b:
        return True
    commun = len(os.path.commonprefix([a, b]))
    return commun >= max(5, min(len(a), len(b)) - 3)


def matiere_du_titre(titre: str, matieres: list[Matiere]) -> tuple[Optional[Matiere], str]:
    """La matière qu'évoque un titre d'enregistrement (« Cours magistral:
    Éthique et déontologie… »), s'il en évoque clairement une parmi
    `matieres`. Les mots communs à plusieurs matières comptent moins."""
    mots_titre = _mots(titre)
    if not mots_titre or not matieres:
        return None, ""
    par_matiere = {m.nom: _mots(" ".join([m.nom, m.court, *m.alias])) for m in matieres}
    frequence: dict[str, int] = {}
    for mots in par_matiere.values():
        for w in mots:
            frequence[w] = frequence.get(w, 0) + 1
    notes = []
    for m in matieres:
        trouves = [w for w in par_matiere[m.nom] if any(_proches(w, t) for t in mots_titre)]
        notes.append((sum(1 / frequence[w] for w in trouves), m, trouves))
    notes.sort(key=lambda x: -x[0])
    meilleur = notes[0]
    second = notes[1][0] if len(notes) > 1 else 0.0
    if meilleur[0] >= 1.0 and meilleur[0] - second >= 0.5:
        return meilleur[1], ", ".join(sorted(meilleur[2]))
    return None, ""

@dataclass
class Classement:
    audio: InfoAudio
    matiere: str = ""                    # "" = à classer
    creneau: Optional[Creneau] = None
    confiance: str = ""                  # sûr | probable | manuel | ""
    raison: str = ""
    ignore: bool = False                 # écarté à la main (pas un cours) ou doublon
    seance: str = ""                     # « 2026-10-05 »
    doublon: str = ""                    # chemin de l'audio dont celui-ci est une copie

    @property
    def classe(self) -> bool:
        return bool(self.matiere) and not self.ignore


def _chevauchement(a0: dt.datetime, a1: dt.datetime, b0: dt.datetime, b1: dt.datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds() / 60)


def classer_un(info: InfoAudio, horaire: Optional[Horaire]) -> Classement:
    c = classer_par_horaire(info, horaire)
    if c.classe or not horaire or not info.titre:
        return c
    # Dernier recours : le titre de l'enregistrement, parmi les cours du jour
    # (ou parmi toutes les matières si la date est inconnue).
    if info.debut:
        noms = {x.matiere for x in horaire.creneaux_du(info.debut.date())}
        parmi = [m for m in horaire.matieres if m.nom in noms]
    else:
        parmi = list(horaire.matieres)
    m, mots = matiere_du_titre(info.titre, parmi)
    if m:
        c.matiere, c.confiance = m.nom, "probable"
        jour = info.debut.date() if info.debut else None
        c.creneau = min((x for x in horaire.creneaux_du(jour) if x.matiere == m.nom),
                        key=lambda x: x.debut, default=None) if jour else None
        c.raison = f"d'après le titre ({mots})" + ("" if jour else ", date inconnue")
    return c


def classer_par_horaire(info: InfoAudio, horaire: Optional[Horaire]) -> Classement:
    c = Classement(audio=info)
    if not horaire or not horaire.creneaux:
        c.raison = "pas d'horaire"
        return c
    if not info.debut:
        c.raison = "date d'enregistrement inconnue"
        return c
    jour = info.debut.date()
    candidats = horaire.creneaux_du(jour)
    if not candidats:
        c.raison = f"aucun cours le {date_fr(jour)} d'après l'horaire"
        return c

    if info.precision == "jour":
        noms = sorted({x.matiere for x in candidats})
        if len(noms) == 1:
            c.matiere, c.creneau = noms[0], min((x for x in candidats), key=lambda x: x.debut)
            c.confiance, c.raison = "probable", "seul cours de la journée (heure inconnue)"
        else:
            c.raison = f"heure inconnue, {len(noms)} cours ce jour-là"
        return c
    c = _classer_a_l_heure(c, info, jour, candidats)
    if c.confiance == "sûr" and info.source == SOURCE_FICHIER:
        c.confiance = "probable"                # la date du fichier reste une estimation
    return c


def _classer_a_l_heure(c: Classement, info: InfoAudio, jour: dt.date,
                       candidats: list[Creneau]) -> Classement:

    a0, a1 = info.debut, info.fin or info.debut + dt.timedelta(hours=1)
    duree_min = max(1.0, (a1 - a0).total_seconds() / 60)
    notes = sorted(((_chevauchement(a0, a1, *x.bornes(jour)), x) for x in candidats),
                   key=lambda p: -p[0])
    meilleur, creneau = notes[0]
    if meilleur > 0:
        c.matiere, c.creneau = creneau.matiere, creneau
        c.confiance = "sûr" if meilleur >= min(30, 0.5 * duree_min) else "probable"
        c.raison = f"{date_fr(jour)}, créneau {creneau.plage}"
        autres = [(n, x) for n, x in notes[1:] if x.matiere != creneau.matiere and n >= 20]
        if autres:
            c.raison += f" — déborde aussi sur {autres[0][1].matiere} ({int(autres[0][0])} min)"
        return c

    def ecart(x: Creneau) -> float:
        b0, b1 = x.bornes(jour)
        return min(abs((b0 - a1).total_seconds()), abs((a0 - b1).total_seconds())) / 60
    proche = min(candidats, key=ecart)
    if ecart(proche) <= TOLERANCE:
        b0, _ = proche.bornes(jour)
        sens = "avant" if a1 <= b0 else "après"
        c.matiere, c.creneau, c.confiance = proche.matiere, proche, "probable"
        c.raison = f"{date_fr(jour)}, {int(ecart(proche))} min {sens} le créneau {proche.plage}"
        return c
    c.raison = f"aucun cours à {a0:%H:%M} le {date_fr(jour)} d'après l'horaire"
    return c


def cle_seance(c: Classement) -> str:
    """Identifiant de séance : le jour (« 2026-10-05 »). Tous les audios d'une
    même matière le même jour forment une séance : deux créneaux consécutifs
    séparés par une pause, un dictaphone arrêté puis relancé…"""
    d = c.audio.debut
    if not d:
        return "sans-date_" + (plat(c.audio.chemin.stem).replace(" ", "-")[:30] or "audio")
    return f"{d:%Y-%m-%d}"


def classer(infos: list[InfoAudio], horaire: Optional[Horaire],
            manuels: dict[str, str]) -> list[Classement]:
    sortie = []
    for info in infos:
        if info.doublon_de:
            sortie.append(Classement(audio=info, ignore=True, confiance="doublon",
                                     raison="copie de « " + info.doublon_de.replace("/", "\\") + " »",
                                     doublon=info.doublon_de))
            continue
        # Classement manuel : retenu par empreinte (ancien format : par chemin).
        choix = (manuels.get(info.empreinte) if info.empreinte else None) or manuels.get(info.relatif)
        if choix == IGNORE:
            c = Classement(audio=info, ignore=True, confiance="manuel", raison="écarté à la main")
        elif choix:
            auto = classer_un(info, horaire)
            m = horaire.matiere(choix) if horaire else None
            c = Classement(audio=info, matiere=m.nom if m else choix, confiance="manuel",
                           raison="classé à la main")
            if auto.creneau and auto.matiere == c.matiere:
                c.creneau = auto.creneau
        else:
            c = classer_un(info, horaire)
        sortie.append(c)
    if horaire:
        _par_elimination(sortie, horaire)
    for c in sortie:
        if c.classe:
            c.seance = cle_seance(c)
    return sortie


def _par_elimination(classements: list[Classement], horaire: Horaire) -> None:
    """Un audio daté au jour près reste seul à classer ce jour-là, et un seul
    cours de ce jour n'a encore aucun enregistrement : c'est probablement lui."""
    par_jour: dict[dt.date, list[Classement]] = {}
    for c in classements:
        if c.audio.debut and not c.ignore:
            par_jour.setdefault(c.audio.debut.date(), []).append(c)
    for jour, groupe in par_jour.items():
        orphelins = [c for c in groupe if not c.classe and c.audio.precision == "jour"]
        if len(orphelins) != 1:
            continue
        restants = {x.matiere for x in horaire.creneaux_du(jour)} - {c.matiere for c in groupe
                                                                   if c.classe}
        if len(restants) == 1:
            c = orphelins[0]
            c.matiere = restants.pop()
            c.creneau = min((x for x in horaire.creneaux_du(jour) if x.matiere == c.matiere),
                            key=lambda x: x.debut, default=None)
            c.confiance = "probable"
            c.raison = "seul cours du jour sans autre enregistrement"


@dataclass
class Seance:
    matiere: str
    cle: str
    audios: list[Classement] = field(default_factory=list)

    @property
    def date(self) -> Optional[dt.date]:
        d = self.audios[0].audio.debut if self.audios else None
        return d.date() if d else None

    @property
    def creneau(self) -> Optional[Creneau]:
        return next((a.creneau for a in self.audios if a.creneau), None)

    @property
    def libelle(self) -> str:
        """« mardi 8 septembre 2026, 08:30–12:05 »."""
        d = self.date
        if not d:
            return "date inconnue"
        texte = date_fr(d)
        creneaux = [a.creneau for a in self.audios if a.creneau]
        if creneaux:
            debut = min(c.debut for c in creneaux)
            fin = max(c.fin for c in creneaux)
            texte += f", {hhmm_minutes(debut)}–{hhmm_minutes(fin)}"
        elif self.audios[0].audio.precision == "heure":
            texte += f", {self.audios[0].audio.debut:%H:%M}"
        return texte

    @property
    def titres(self) -> list[str]:
        return [a.audio.titre for a in self.audios if a.audio.titre]

    @property
    def duree(self) -> float:
        return sum(a.audio.duree for a in self.audios)


def seances(classements: list[Classement]) -> dict[str, list[Seance]]:
    """Les enregistrements d'une même matière, même jour, même créneau, sont
    les morceaux d'une seule séance (dictaphone mis en pause…)."""
    par: dict[str, dict[str, Seance]] = {}
    for c in classements:
        if not c.classe:
            continue
        s = par.setdefault(c.matiere, {}).setdefault(c.seance, Seance(c.matiere, c.seance))
        s.audios.append(c)
    sortie: dict[str, list[Seance]] = {}
    for matiere, groupe in par.items():
        for s in groupe.values():
            s.audios.sort(key=lambda a: (a.audio.debut or dt.datetime.max, a.audio.nom))
        sortie[matiere] = sorted(groupe.values(), key=lambda s: (s.date or dt.date.max, s.cle))
    return sortie


# ----------------------------------------------------------------------
# Le dossier de l'année
# ----------------------------------------------------------------------
@dataclass
class Analyse:
    horaire: Optional[Horaire]
    infos: list[InfoAudio]
    classements: list[Classement]
    seances: dict[str, list[Seance]]
    remarques: list[str] = field(default_factory=list)


# Fichiers de suivi lus et réécrits par l'interface et par le traitement en
# même temps : une lecture-modification-écriture à la fois.
_VERROU = threading.RLock()


class Annee:
    def __init__(self, racine: Path | str):
        self.racine = Path(racine)
        self.technique = self.racine / DOSSIER_TECHNIQUE
        self._registre: tuple[float, dict] = (-1.0, {})

    # -- emplacements --------------------------------------------------
    def config(self) -> dict:
        return _lire_json(self.technique / CONFIG_JSON, {})

    def regler(self, **valeurs: Any) -> dict:
        with _VERROU:
            cfg = self.config()
            cfg.update(valeurs)
            _ecrire_json(self.technique / CONFIG_JSON, cfg)
            return cfg

    def ranger_traites(self) -> bool:
        """Ranger les audios traités dans Traités\\AAAA-MM (réglage de l'année)."""
        return bool(self.config().get("ranger", True))

    def dossier_audios(self) -> Path:
        """Le dossier des audios : celui retenu la dernière fois, sinon un
        sous-dossier dont le nom parle d'audio, sinon le nom par défaut
        (pas encore créé)."""
        nom = self.config().get("dossier_audios")
        if nom and (self.racine / nom).is_dir():
            return self.racine / nom
        try:
            sous = sorted(p for p in self.racine.iterdir() if p.is_dir())
        except OSError:
            sous = []
        for p in sous:
            n = plat(p.name)
            if p.name != DOSSIER_TECHNIQUE and ("audio" in n or "enregistrement" in n):
                return p
        return self.racine / DOSSIER_AUDIOS

    def retenir_dossier_audios(self, dossier: Path) -> None:
        self.regler(dossier_audios=dossier.name)

    def horaire_json(self) -> Path:
        return self.dossier_audios() / HORAIRE_JSON

    def dossier_transcriptions(self) -> Path:
        return self.dossier_audios() / DOSSIER_TRANSCRIPTIONS

    def transcription_de(self, audio: Path) -> Path:
        """Transcriptions\\ reproduit les sous-dossiers éventuels des audios."""
        try:
            rel = audio.relative_to(self.dossier_audios())
        except ValueError:
            rel = Path(audio.name)
        dossier = self.dossier_transcriptions() / rel.parent
        # Deux audios de même nom mais d'extensions différentes : on les distingue.
        jumeaux = [p for p in audio.parent.glob(f"{glob_echappe(audio.stem)}.*")
                   if p.suffix.lower() in EXT_AUDIO and p != audio]
        nom = f"{audio.stem}{audio.suffix.lower()}.txt" if jumeaux else f"{audio.stem}.txt"
        return dossier / nom

    # -- registre des enregistrements -----------------------------------
    def registre(self) -> dict:
        """{"audios": {empreinte: {"relatif", "transcription", "vu"}}} — relu
        seulement quand le fichier a changé."""
        chemin = self.technique / REGISTRE_JSON
        try:
            mtime = chemin.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime != self._registre[0]:
            brut = _lire_json(chemin, {})
            if not isinstance(brut, dict) or not isinstance(brut.get("audios"), dict):
                brut = {"version": 1, "audios": {}}
            self._registre = (mtime, brut)
        return self._registre[1]

    def _modifier_registre(self, changer: Callable[[dict], bool]) -> None:
        """Applique `changer` (qui dit s'il a modifié quelque chose) au registre
        relu sous verrou, puis l'écrit."""
        with _VERROU:
            self._registre = (-1.0, {})
            reg = self.registre()
            if changer(reg["audios"]):
                reg["version"] = 1
                _ecrire_json(self.technique / REGISTRE_JSON, reg)
            self._registre = (-1.0, {})

    def noter_transcription(self, info: "InfoAudio", chemin: Path) -> None:
        if not info.empreinte:
            return
        try:
            rel = chemin.relative_to(self.dossier_audios()).as_posix()
        except ValueError:
            return

        def changer(audios: dict) -> bool:
            e = audios.setdefault(info.empreinte, {"relatif": info.relatif,
                                                   "vu": dt.datetime.now().isoformat(timespec="seconds")})
            if e.get("transcription") == rel:
                return False
            e["transcription"] = rel
            return True
        self._modifier_registre(changer)

    def transcriptions(self, infos: list["InfoAudio"], chercher: bool = False) -> dict[str, Path]:
        """Chemin de la transcription de chaque audio (clé : chemin relatif),
        qu'elle existe déjà ou qu'elle soit à écrire. Ordre de recherche : le
        registre (l'audio a pu être renommé ou rangé depuis), le chemin qui
        reproduit celui de l'audio, puis, avec `chercher`, une transcription de
        même nom n'importe où dans Transcriptions (en-tête « Fichier : » vérifié)."""
        base = self.dossier_audios()
        audios = self.registre().get("audios", {})
        sortie: dict[str, Path] = {}
        a_noter: dict[str, str] = {}
        index: Optional[dict[str, list[Path]]] = None
        for info in infos:
            connu = audios.get(info.empreinte, {}) if info.empreinte else {}
            if connu.get("transcription") and (base / connu["transcription"]).is_file():
                sortie[info.relatif] = base / connu["transcription"]
                continue
            miroir = self.transcription_de(info.chemin)
            trouve = miroir if miroir.is_file() else None
            if trouve is None and chercher:
                if index is None:
                    index = {}
                    racine_t = self.dossier_transcriptions()
                    if racine_t.is_dir():
                        for t in racine_t.rglob("*.txt"):
                            index.setdefault(t.name.lower(), []).append(t)
                for nom in (f"{info.chemin.stem}.txt", f"{info.chemin.stem}{info.chemin.suffix}.txt"):
                    for t in index.get(nom.lower(), []):
                        try:
                            tete = t.read_text(encoding="utf-8", errors="replace")[:400]
                        except OSError:
                            continue
                        if re.search(rf"^Fichier\s*:\s*{re.escape(info.chemin.name)}\s*$", tete, re.M):
                            trouve = t
                            break
                    if trouve:
                        break
            sortie[info.relatif] = trouve or miroir
            if trouve and info.empreinte:
                a_noter[info.empreinte] = trouve.relative_to(base).as_posix()
        if a_noter:
            maintenant = dt.datetime.now().isoformat(timespec="seconds")

            def changer(reg: dict) -> bool:
                change = False
                for emp, rel in a_noter.items():
                    e = reg.setdefault(emp, {"vu": maintenant})
                    if e.get("transcription") != rel:
                        e["transcription"] = rel
                        change = True
                return change
            self._modifier_registre(changer)
        return sortie

    def est_range(self, relatif: str) -> bool:
        """L'audio est-il déjà rangé (Traités\\… ou Écartés\\…) ?"""
        return relatif.split("/", 1)[0] in (DOSSIER_TRAITES, DOSSIER_ECARTES)

    def deplacer_audio(self, info: "InfoAudio", sous: str) -> Path:
        """Range un audio dans un sous-dossier du dossier des audios
        (« Traités/2026-09 », « Écartés »), sans écraser un fichier de même nom.
        Le cache et le registre suivent : rien n'est relu ni retranscrit."""
        base = self.dossier_audios()
        dossier = base.joinpath(*sous.split("/"))
        dossier.mkdir(parents=True, exist_ok=True)
        dest = dossier / info.chemin.name
        n = 2
        while dest.exists():
            dest = dossier / f"{info.chemin.stem} ({n}){info.chemin.suffix}"
            n += 1
        shutil.move(str(info.chemin), str(dest))
        nouveau = dest.relative_to(base).as_posix()
        with _VERROU:
            cache_chemin = self.technique / CACHE_AUDIOS
            cache = _lire_json(cache_chemin, {})
            if isinstance(cache, dict) and info.relatif in cache:
                cache[nouveau] = cache.pop(info.relatif)
                try:
                    _ecrire_json(cache_chemin, cache)
                except OSError:
                    pass
        if info.empreinte:
            def changer(audios: dict) -> bool:
                audios.setdefault(info.empreinte, {})["relatif"] = nouveau
                return True
            self._modifier_registre(changer)
        return dest

    def source_horaire(self) -> Optional[Path]:
        """Le fichier d'horaire (PDF, image, Excel, .ics…) : de préférence un
        nom qui le dit, dans le dossier des audios ou à la racine."""
        candidats: list[tuple[int, float, Path]] = []
        for dossier, bonus in ((self.dossier_audios(), 2), (self.racine, 0)):
            try:
                fichiers = [p for p in dossier.iterdir() if p.is_file()]
            except OSError:
                continue
            for p in fichiers:
                ext = p.suffix.lower()
                if (ext not in EXT_HORAIRE or p.name == HORAIRE_JSON
                        or p.name.startswith(("~$", ".")) or ext == ".tmp"):
                    continue
                dit_horaire = any(plat(m) in plat(p.stem) for m in _MOTS_HORAIRE)
                # Sans le mot « horaire » dans son nom, seul un PDF, une image,
                # un classeur ou un calendrier du dossier des audios est retenu.
                if not dit_horaire and (dossier == self.racine
                                        or ext in (".txt", ".json", ".html", ".htm", ".csv")):
                    continue
                score = bonus + (5 if dit_horaire else 0)
                candidats.append((score, p.stat().st_mtime, p))
        if not candidats:
            return None
        candidats.sort(key=lambda x: (-x[0], -x[1]))
        return candidats[0][2]

    def charger_horaire(self) -> Optional[Horaire]:
        chemin = self.horaire_json()
        if not chemin.is_file():
            return None
        try:
            brut = json.loads(chemin.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            h = Horaire()
            h.avertissements.append(f"horaire.json illisible : {exc}"[:200])
            return h
        return Horaire.depuis_dict(brut)

    def enregistrer_horaire(self, h: Horaire) -> Path:
        chemin = self.horaire_json()
        _ecrire_json(chemin, h.vers_dict())
        return chemin

    # -- fichiers ------------------------------------------------------
    def audios(self) -> list[Path]:
        racine = self.dossier_audios()
        if not racine.is_dir():
            return []
        trouves: list[Path] = []

        def balayer(d: Path, profondeur: int) -> None:
            try:
                entrees = sorted(d.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                return
            for p in entrees:
                if p.is_dir():
                    if profondeur < 3 and p.name not in (DOSSIER_TRANSCRIPTIONS, DOSSIER_TECHNIQUE) \
                            and not p.name.startswith("."):
                        balayer(p, profondeur + 1)
                elif p.suffix.lower() in EXT_AUDIO and not p.name.startswith(("~$", "._")):
                    trouves.append(p)
        balayer(racine, 0)
        return trouves

    def a_ranger(self) -> list[Path]:
        """Audios et horaire posés à la racine de l'année, à ranger dans le
        dossier des audios."""
        try:
            fichiers = [p for p in self.racine.iterdir() if p.is_file()]
        except OSError:
            return []
        sortie = [p for p in fichiers if p.suffix.lower() in EXT_AUDIO]
        for p in fichiers:
            if p.suffix.lower() in EXT_HORAIRE and p.suffix.lower() != ".json" \
                    and any(plat(m) in plat(p.stem) for m in _MOTS_HORAIRE):
                sortie.append(p)
        return sortie

    def ranger(self) -> list[str]:
        cible = self.dossier_audios()
        cible.mkdir(parents=True, exist_ok=True)
        self.retenir_dossier_audios(cible)
        messages = []
        for p in self.a_ranger():
            dest = cible / p.name
            if dest.exists():
                messages.append(f"{p.name} : déjà présent dans « {cible.name} », laissé à la racine")
                continue
            shutil.move(str(p), str(dest))
            messages.append(f"{p.name} → {cible.name}")
        return messages

    def dossier_matiere(self, m: Matiere, pris: Optional[set[str]] = None) -> Path:
        """Le dossier d'une matière : celui noté dans l'horaire s'il existe,
        sinon un dossier existant au nom proche (et pas déjà attribué à une
        autre matière), sinon le nom prévu."""
        if m.dossier and (self.racine / m.dossier).is_dir():
            return self.racine / m.dossier
        existant = self._dossier_proche(m, pris or set())
        return existant or self.racine / m.nom_de_dossier

    def _dossier_proche(self, m: Matiere, pris: set[str]) -> Optional[Path]:
        try:
            audios = self.dossier_audios()
            dossiers = [p for p in self.racine.iterdir() if p.is_dir()
                        and p.name != DOSSIER_TECHNIQUE and p != audios and p.name not in pris]
        except OSError:
            return None
        noms = {plat(x) for x in m.noms()} - {""}
        exacts = [p for p in dossiers if plat(p.name) in noms]
        if exacts:
            return exacts[0]
        proches = [p for p in dossiers if len(plat(p.name)) >= 5 and any(
            n.startswith(plat(p.name)) or plat(p.name).startswith(n) for n in noms if len(n) >= 5)]
        return proches[0] if len(proches) == 1 else None

    def creer_structure(self, horaire: Optional[Horaire]) -> list[str]:
        """Crée le dossier des audios et un dossier par matière de l'horaire ;
        le nom retenu pour chaque matière est noté dans horaire.json."""
        crees = []
        audios = self.dossier_audios()
        if not audios.is_dir():
            audios.mkdir(parents=True)
            crees.append(audios.name)
        self.retenir_dossier_audios(audios)
        if horaire:
            change = False
            pris: set[str] = {m.dossier for m in horaire.matieres
                              if m.dossier and (self.racine / m.dossier).is_dir()}
            for m in horaire.matieres:
                d = self.dossier_matiere(m, pris - {m.dossier})
                if d.name in pris and d.name != m.dossier:
                    d = self.racine / nom_dossier(m.nom)       # deux matières, un seul nom court
                pris.add(d.name)
                if not d.is_dir():
                    d.mkdir(parents=True)
                    crees.append(d.name)
                if m.dossier != d.name:
                    m.dossier = d.name
                    change = True
            if change and self.horaire_json().is_file():
                self.enregistrer_horaire(horaire)
        return crees

    def supports_matiere(self, m: Matiere) -> list[Path]:
        """Les documents posés dans le dossier de la matière (PDF de Moodle,
        diapos, notes, photos), sans les fiches produites ni le dossier
        technique."""
        d = self.dossier_matiere(m)
        if not d.is_dir():
            return []
        sortie = []
        for p in sorted(d.rglob("*"), key=lambda x: str(x).lower()):
            rel = p.relative_to(d).parts
            if any(x == DOSSIER_TECHNIQUE or x.startswith(("~$", ".")) for x in rel):
                continue
            if not p.is_file() or p.suffix.lower() not in EXT_SUPPORTS:
                continue
            if re.search(rf" - ({FICHES}|{INFOS})( \(.*\))?\.docx$", p.name, re.I):
                continue
            sortie.append(p)
        return sortie

    # -- classement manuel ---------------------------------------------
    def manuels(self) -> dict[str, str]:
        brut = _lire_json(self.technique / CLASSEMENT_JSON, {})
        return {str(k): str(v) for k, v in brut.items()} if isinstance(brut, dict) else {}

    def classer_manuellement(self, relatif: str, matiere: str) -> None:
        """Retenu par l'empreinte de l'audio : le choix le suit s'il est
        renommé ou rangé. `matiere` vide : retour au classement automatique."""
        cle = relatif
        chemin = self.dossier_audios() / relatif
        cache = _lire_json(self.technique / CACHE_AUDIOS, {})
        entree = cache.get(relatif) if isinstance(cache, dict) else None
        if isinstance(entree, dict) and entree.get("empreinte"):
            cle = entree["empreinte"]
        elif chemin.is_file():
            try:
                cle = empreinte_audio(chemin)
            except OSError:
                pass
        with _VERROU:
            brut = self.manuels()
            brut.pop(relatif, None)
            if matiere:
                brut[cle] = matiere
            else:
                brut.pop(cle, None)
            _ecrire_json(self.technique / CLASSEMENT_JSON, brut)

    def _migrer_manuels(self, infos: list["InfoAudio"]) -> dict[str, str]:
        """Les classements manuels d'avant le registre étaient notés par chemin :
        ils passent à l'empreinte (le chemin change quand l'audio est rangé)."""
        manuels = self.manuels()
        par_chemin = {i.relatif: i.empreinte for i in infos if i.empreinte}
        anciens = [k for k in manuels if k in par_chemin]
        if anciens:
            with _VERROU:
                manuels = self.manuels()
                for k in anciens:
                    if k in manuels:
                        manuels.setdefault(par_chemin[k], manuels[k])
                        manuels.pop(k)
                try:
                    _ecrire_json(self.technique / CLASSEMENT_JSON, manuels)
                except OSError:
                    pass
        return manuels

    # -- analyse complète ----------------------------------------------
    def infos_audios(self, lecteur=metadonnees,
                     periode: Optional[tuple[dt.date, dt.date]] = None) -> list[InfoAudio]:
        """Date et durée de chaque audio, en gardant en cache ce qui a déjà été
        lu (ouvrir 60 fichiers sur un NAS prend du temps)."""
        base = self.dossier_audios()
        cache_chemin = self.technique / CACHE_AUDIOS
        cache = _lire_json(cache_chemin, {})
        cle_periode = f"{periode[0]}/{periode[1]}" if periode else ""
        neuf: dict[str, Any] = {}
        sortie = []
        frais: set[str] = set()
        for p in self.audios():
            rel = p.relative_to(base).as_posix()
            try:
                st = p.stat()
            except OSError:
                continue
            c = cache.get(rel)
            meme_fichier = isinstance(c, dict) and c.get("taille") == st.st_size and \
                abs(float(c.get("mtime", 0)) - st.st_mtime) < 2
            if meme_fichier and c.get("periode", "") == cle_periode:
                info = InfoAudio(chemin=p, relatif=rel, taille=st.st_size, mtime=st.st_mtime,
                                 debut=dt.datetime.fromisoformat(c["debut"]) if c.get("debut") else None,
                                 precision=c.get("precision", ""), source=c.get("source", ""),
                                 duree=float(c.get("duree") or 0), titre=titre_depuis_nom(p.name))
            else:
                info = analyser_audio(p, rel, lecteur, periode)
                frais.add(rel)
            info.empreinte = c.get("empreinte", "") if meme_fichier else ""
            if not info.empreinte:
                try:
                    info.empreinte = empreinte_audio(p, st.st_size)
                except OSError:
                    info.empreinte = ""
            if rel in frais:
                self._date_connue(info)
            neuf[rel] = {"taille": info.taille, "mtime": info.mtime, "periode": cle_periode,
                         "debut": info.debut.isoformat(timespec="seconds") if info.debut else "",
                         "precision": info.precision, "source": info.source, "duree": info.duree,
                         "empreinte": info.empreinte}
            sortie.append(info)
        if neuf != cache:
            with _VERROU:
                try:
                    _ecrire_json(cache_chemin, neuf)
                except OSError:
                    pass
        ecarter_dates_de_copie(sortie)
        self._reperer_doublons(sortie)
        return sortie

    def _date_connue(self, info: InfoAudio) -> None:
        """Un audio renommé ou déplacé garde la date lue sous son premier nom
        (le nom portait souvent l'heure de début) : sa séance ne change pas,
        rien n'est refait. Sauf si la nouvelle lecture est plus sûre (le
        registre n'avait que la date du fichier, ou aucune)."""
        connu = self.registre().get("audios", {}).get(info.empreinte) if info.empreinte else None
        if not connu or not connu.get("debut"):
            return
        faible = lambda source, precision: source == SOURCE_FICHIER or precision != "heure"  # noqa: E731
        if info.debut and faible(connu.get("source", ""), connu.get("precision", "")) \
                and not faible(info.source, info.precision):
            return
        try:
            info.debut = dt.datetime.fromisoformat(connu["debut"])
        except ValueError:
            return
        info.precision = connu.get("precision", "") or info.precision
        info.source = connu.get("source", "") or info.source
        info.duree = float(connu.get("duree") or info.duree or 0)

    def _reperer_doublons(self, infos: list[InfoAudio]) -> None:
        """Même empreinte = même enregistrement. Garde celui que le registre
        connaît (ou, à défaut, un audio pas écarté, puis le premier par chemin) ;
        les autres sont des doublons. Le registre retient le chemin actuel de
        chaque enregistrement."""
        audios = self.registre().get("audios", {})
        groupes: dict[str, list[InfoAudio]] = {}
        for i in infos:
            if i.empreinte:
                groupes.setdefault(i.empreinte, []).append(i)
        a_noter: dict[str, dict] = {}
        for emp, groupe in groupes.items():
            entree = audios.get(emp, {})
            connu = entree.get("relatif")
            groupe.sort(key=lambda i: (i.relatif != connu,
                                       i.relatif.split("/", 1)[0] == DOSSIER_ECARTES, i.relatif.lower()))
            gardien = groupe[0]
            for autre in groupe[1:]:
                autre.doublon_de = gardien.relatif
            voulu = {"relatif": gardien.relatif}
            if gardien.debut:
                voulu.update(debut=gardien.debut.isoformat(timespec="seconds"),
                             precision=gardien.precision, source=gardien.source, duree=gardien.duree)
            if any(entree.get(k) != v for k, v in voulu.items()):
                a_noter[emp] = voulu
        if a_noter:
            maintenant = dt.datetime.now().isoformat(timespec="seconds")

            def changer(reg: dict) -> bool:
                for emp, voulu in a_noter.items():
                    reg.setdefault(emp, {"vu": maintenant}).update(voulu)
                return True
            try:
                self._modifier_registre(changer)
            except OSError:
                pass

    def analyser(self, lecteur=metadonnees) -> Analyse:
        horaire = self.charger_horaire()
        infos = self.infos_audios(lecteur, horaire.periode() if horaire else None)
        classements = classer(infos, horaire, self._migrer_manuels(infos))
        remarques = []
        if horaire:
            remarques += [f"Horaire : {a}" for a in horaire.avertissements]
        return Analyse(horaire, infos, classements, seances(classements), remarques)


def glob_echappe(texte: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", texte)


# ----------------------------------------------------------------------
# Horaire dans un format que Claude ne lit pas directement
# ----------------------------------------------------------------------
def horaire_en_texte(chemin: Path, importer: Callable[[str, str], Any]) -> Optional[str]:
    """Texte à donner à Claude pour un horaire Excel, Word, CSV, HTML ou texte
    (couleurs des cases comprises) ; None pour un PDF ou une image, que
    Claude lit lui-même avec l'outil Read."""
    ext = chemin.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        openpyxl = importer("openpyxl", "openpyxl")
        classeur = openpyxl.load_workbook(str(chemin), data_only=True)
        lignes = []
        for feuille in classeur.worksheets:
            lignes.append(f"## Feuille « {feuille.title} »")
            fusions = {}
            for plage in feuille.merged_cells.ranges:
                fusions[(plage.min_row, plage.min_col)] = str(plage)
            for rangee in feuille.iter_rows():
                for cel in rangee:
                    if cel.value in (None, ""):
                        continue
                    texte = " ".join(str(cel.value).split())
                    fond = ""
                    try:
                        f = cel.fill
                        if f and f.fill_type == "solid" and f.fgColor is not None \
                                and f.fgColor.type == "rgb" and isinstance(f.fgColor.rgb, str):
                            fond = "#" + f.fgColor.rgb[-6:].upper()
                    except Exception:  # noqa: BLE001
                        fond = ""
                    plage = fusions.get((cel.row, cel.column), cel.coordinate)
                    lignes.append(f"{plage}" + (f" [fond {fond}]" if fond and fond != "#000000" else "")
                                  + f" : {texte}")
        return "\n".join(lignes)
    if ext == ".docx":
        docx = importer("docx", "python-docx")
        doc = docx.Document(str(chemin))
        lignes = [p.text for p in doc.paragraphs if p.text.strip()]
        for t, table in enumerate(doc.tables, 1):
            lignes.append(f"## Tableau {t}")
            for r, rangee in enumerate(table.rows, 1):
                cellules = []
                for cel in rangee.cells:
                    fond = ""
                    shd = cel._tc.tcPr.find(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}shd") \
                        if cel._tc.tcPr is not None else None
                    if shd is not None:
                        v = shd.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}fill")
                        if v and v.lower() not in ("auto", "ffffff"):
                            fond = f" [fond #{v.upper()}]"
                    cellules.append(" ".join(cel.text.split()) + fond)
                lignes.append(f"L{r} | " + " | ".join(cellules))
        return "\n".join(lignes)
    if ext in (".csv", ".txt", ".html", ".htm"):
        texte = chemin.read_text(encoding="utf-8-sig", errors="replace")
        if ext in (".html", ".htm"):
            texte = re.sub(r"<(script|style)\b.*?</\1>", " ", texte, flags=re.S | re.I)
            texte = re.sub(r"<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", texte, flags=re.I)
            texte = re.sub(r"</t[dh]>", " | ", texte, flags=re.I)
            texte = re.sub(r"<[^>]+>", " ", texte)
        return texte[:200_000]
    return None


def _couleur_pdf(c: Any) -> str:
    """Couleur de remplissage pdfplumber (gris, RVB ou CMJN entre 0 et 1) en hexadécimal."""
    try:
        v = [float(x) for x in (c if isinstance(c, (list, tuple)) else [c])]
    except (TypeError, ValueError):
        return ""
    if len(v) == 1:
        v = v * 3
    elif len(v) == 4:
        cy, ma, ja, no = v
        v = [(1 - cy) * (1 - no), (1 - ma) * (1 - no), (1 - ja) * (1 - no)]
    if len(v) != 3:
        return ""
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(x * 255))) for x in v))


def couleurs_depuis_pdf(chemin: Path, horaire: Horaire, pdfplumber: Any, pages: int = 8) -> int:
    """Couleur exacte de chaque matière dans un horaire PDF : le fond de la
    case où son intitulé est écrit (remplissage vectoriel, sans approximation).
    Renvoie le nombre de couleurs changées."""
    changees = 0
    with pdfplumber.open(str(chemin)) as pdf:
        feuilles = []
        for page in pdf.pages[:pages]:
            rects = [r for r in page.rects if r.get("fill") and r.get("non_stroking_color") is not None]
            if rects:
                feuilles.append((page, rects))
        for m in horaire.matieres:
            mots = re.split(r"\s+", m.nom.strip())[:7]
            motif = r"\s+".join(re.sub(r"['’]", "['’]", re.escape(w)) for w in mots)
            trouvee = ""
            for page, rects in feuilles:
                try:
                    resultats = page.search(motif, regex=True, case=False)
                except Exception:  # noqa: BLE001  (pdfplumber ancien)
                    resultats = []
                for r in resultats[:3]:
                    cx, cy = (r["x0"] + r["x1"]) / 2, (r["top"] + r["bottom"]) / 2
                    cases = [x for x in rects if x["x0"] <= cx <= x["x1"] and x["top"] <= cy <= x["bottom"]]
                    if not cases:
                        continue
                    case = min(cases, key=lambda x: (x["x1"] - x["x0"]) * (x["bottom"] - x["top"]))
                    couleur = _couleur_pdf(case["non_stroking_color"])
                    if couleur and couleur not in ("#FFFFFF", "#000000"):
                        trouvee = couleur
                        break
                if trouvee:
                    break
            if trouvee and trouvee != m.couleur:
                m.couleur = trouvee
                changees += 1
    return changees


def affiner_couleurs(horaire: Horaire, image: Any, *, seuil: float = 10.0) -> int:
    """Horaire en image : recale une couleur estimée par Claude sur l'aplat
    de l'image qui en est très proche (à `seuil` près en RVB) — la nuance la
    plus étendue du voisinage, pas ses variantes lissées au bord des cases.
    Les pastels d'un horaire se ressemblent : au-delà, la couleur estimée
    est gardée. Renvoie le nombre de couleurs recalées."""
    im = image.convert("RGB")
    if im.width * im.height > 2_500_000:                  # sans mélange de couleurs
        facteur = (2_500_000 / (im.width * im.height)) ** 0.5
        im = im.resize((max(1, int(im.width * facteur)), max(1, int(im.height * facteur))), 0)
    comptes = im.getcolors(maxcolors=im.width * im.height) or []
    aplats = [(n, c) for n, c in comptes if n >= 150 and max(c) - min(c) >= 12 and min(c) < 252]
    recalees = 0
    for m in horaire.matieres:
        if not m.couleur or not aplats:
            continue
        ici = rvb(m.couleur)
        voisins = [(n, sum((x - y) ** 2 for x, y in zip(ici, c)) ** 0.5, c) for n, c in aplats]
        voisins = [v for v in voisins if v[1] <= seuil]
        if voisins:
            _, dist, c = max(voisins, key=lambda v: (v[0], -v[1]))
            if dist > 0:
                m.couleur = "#{:02X}{:02X}{:02X}".format(*c)
                recalees += 1
    return recalees
