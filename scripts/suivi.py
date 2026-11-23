# -*- coding: utf-8 -*-
"""
Mode année : ce qui est déjà dans les fiches et dans les infos d'une matière,
pour n'y ajouter que le nouveau.

    <matière>\\_Transcriptor\\etat.json
    {"version": 2,
     "fiches": {"seances":  {"2026-09-08": {"notes": "<empreinte>", "taille": …, "mtime": …, "le": "…"}},
                "supports": {"Cours/plan.pdf": {"empreinte": "<taille>-<date>", "le": "…"}},
                "complet": "<date de la dernière rédaction complète>", "ajouts": 3},
     "infos":  {… même forme …}}

Une séance est « dans les fiches » tant que ses notes n'ont pas changé ; un
document, tant que sa taille et sa date n'ont pas changé.

Les fiches se travaillent en parties, une par catégorie (titre ##) :
010.md, 020.md… le lexique en 990.md. Claude complète les parties concernées
au lieu de tout réécrire.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

VERSION = 2
LEXIQUE = "990.md"


def maintenant() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def empreinte_texte(texte: str) -> str:
    return hashlib.sha1(texte.strip().encode("utf-8")).hexdigest()[:16]


def empreinte_fichier(p: Path) -> str:
    """Taille et date d'un document (même forme que supports._empreinte)."""
    try:
        st = p.stat()
        return f"{st.st_size}-{int(st.st_mtime)}"
    except OSError:
        return ""


def mots(texte: str) -> int:
    return len(texte.split())


# ----------------------------------------------------------------------
# Ce qu'une rédaction (fiches ou infos) contient déjà
# ----------------------------------------------------------------------
@dataclass
class Redaction:
    seances: dict[str, dict] = field(default_factory=dict)
    supports: dict[str, dict] = field(default_factory=dict)
    complet: str = ""        # date de la dernière rédaction complète
    ajouts: int = 0          # ajouts depuis

    @classmethod
    def depuis(cls, d: object) -> "Redaction":
        if not isinstance(d, dict):
            return cls()
        return cls(seances={str(k): v for k, v in (d.get("seances") or {}).items() if isinstance(v, dict)},
                   supports={str(k): v for k, v in (d.get("supports") or {}).items() if isinstance(v, dict)},
                   complet=str(d.get("complet") or ""), ajouts=int(d.get("ajouts") or 0))

    def vers_dict(self) -> dict:
        return {"seances": self.seances, "supports": self.supports,
                "complet": self.complet, "ajouts": self.ajouts}

    def vider(self) -> None:
        self.seances, self.supports, self.complet, self.ajouts = {}, {}, "", 0

    # -- séances -----------------------------------------------------------
    def seance_integree(self, cle: str, notes: Path) -> bool:
        """Les notes de cette séance sont-elles dans le document, telles quelles ?
        Taille et date d'abord ; le contenu seulement s'ils ont changé."""
        e = self.seances.get(cle)
        if not e:
            return False
        try:
            st = notes.stat()
        except OSError:
            return False
        if e.get("taille") == st.st_size and abs(float(e.get("mtime") or 0) - st.st_mtime) < 2:
            return True
        try:
            return e.get("notes") == empreinte_texte(notes.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False

    def noter_seance(self, cle: str, notes: Path, texte: Optional[str] = None, le: str = "") -> None:
        try:
            st = notes.stat()
            taille, mtime = st.st_size, st.st_mtime
        except OSError:
            taille, mtime = 0, 0.0
        if texte is None:
            texte = notes.read_text(encoding="utf-8", errors="replace")
        self.seances[cle] = {"notes": empreinte_texte(texte), "taille": taille, "mtime": mtime,
                             "le": le or maintenant()}

    # -- documents ---------------------------------------------------------
    def support_integre(self, rel: str, p: Path) -> bool:
        e = self.supports.get(rel)
        return bool(e) and e.get("empreinte") == empreinte_fichier(p)

    def noter_support(self, rel: str, p: Path, le: str = "") -> None:
        self.supports[rel] = {"empreinte": empreinte_fichier(p), "le": le or maintenant()}

    def date_seance(self, cle: str) -> str:
        return str((self.seances.get(cle) or {}).get("le") or "")


class EtatMatiere:
    """etat.json d'une matière : ce que contiennent ses fiches et ses infos."""

    def __init__(self, chemin: Path):
        self.chemin = chemin
        self.fiches = Redaction()
        self.infos = Redaction()

    @classmethod
    def charger(cls, travail: Path, notes: dict[str, Path], sources: dict[str, Path],
                signature_v1: Optional[Callable[[], str]] = None) -> "EtatMatiere":
        """`notes` : séance -> fichier de notes ; `sources` : document (chemin
        relatif au dossier de la matière) -> fichier. Un etat.json d'avant
        (version 1 : une signature globale) est traduit sans rien refaire :
        si rien n'a bougé depuis, tout est dedans ; sinon, est dedans ce qui
        est plus ancien que le document rédigé."""
        e = cls(travail / "etat.json")
        try:
            brut = json.loads(e.chemin.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            brut = {}
        if isinstance(brut, dict) and brut.get("version") == VERSION:
            e.fiches = Redaction.depuis(brut.get("fiches"))
            e.infos = Redaction.depuis(brut.get("infos"))
            return e
        exact = bool(isinstance(brut, dict) and brut.get("signature") and signature_v1
                     and brut["signature"] == signature_v1())
        le_v1 = str(brut.get("genere") or "") if isinstance(brut, dict) else ""
        for doc, nom in ((e.fiches, "fiches.md"), (e.infos, "infos.md")):
            try:
                t_doc = (travail / nom).stat().st_mtime
            except OSError:
                continue                                   # pas encore rédigé
            le = le_v1 or dt.datetime.fromtimestamp(t_doc).isoformat(timespec="seconds")
            doc.complet = le
            for cle, chemin in notes.items():
                try:
                    st = chemin.stat()
                except OSError:
                    continue
                if exact or st.st_mtime <= t_doc + 2:
                    doc.noter_seance(cle, chemin, le=le)
            for rel, p in sources.items():
                try:
                    plus_vieux = p.stat().st_mtime <= t_doc + 2
                except OSError:
                    continue
                if exact or plus_vieux:
                    doc.noter_support(rel, p, le=le)
        return e

    def vers_dict(self) -> dict:
        return {"version": VERSION, "fiches": self.fiches.vers_dict(), "infos": self.infos.vers_dict()}

    def sauver(self) -> None:
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.chemin.with_name(self.chemin.name + ".tmp")
        tmp.write_text(json.dumps(self.vers_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.chemin)


# ----------------------------------------------------------------------
# Les fiches en parties, une par catégorie
# ----------------------------------------------------------------------
_TITRE_CATEGORIE = re.compile(r"^##\s+(.+?)\s*#*\s*$")
_LEXIQUE = re.compile(r"^(lexique|glossaire|vocabulaire)\b", re.I)


def decouper(md: str) -> list[tuple[str, str]]:
    """[(titre de la catégorie, texte)], dans l'ordre ; ce qui précède la
    première catégorie a le titre « ». Les titres dans un bloc de code ne
    comptent pas."""
    parties: list[tuple[str, str]] = []
    titre, lignes = "", []
    dans_code = False
    for ligne in md.splitlines():
        if ligne.lstrip().startswith("```"):
            dans_code = not dans_code
        m = None if dans_code else _TITRE_CATEGORIE.match(ligne)
        if m:
            if "\n".join(lignes).strip():
                parties.append((titre, "\n".join(lignes).strip() + "\n"))
            titre, lignes = m.group(1).strip(), [ligne]
        else:
            lignes.append(ligne)
    if "\n".join(lignes).strip():
        parties.append((titre, "\n".join(lignes).strip() + "\n"))
    return parties


def nommer(parties: list[tuple[str, str]]) -> dict[str, str]:
    """Nom de fichier de chaque partie : 000.md pour un préambule, 010.md,
    020.md… (de 10 en 10, pour pouvoir glisser 015.md entre deux), le
    lexique en 990.md."""
    noms: dict[str, str] = {}
    rang = 0
    for titre, texte in parties:
        if not titre:
            nom = "000.md"
        elif _LEXIQUE.match(titre) and LEXIQUE not in noms:
            nom = LEXIQUE
        else:
            rang += 10
            nom = f"{min(rang, 980):03d}.md"
        while nom in noms:                       # garde-fou : jamais deux fois le même nom
            nom = f"{int(nom[:3]) + 1:03d}.md"
        noms[nom] = texte
    return noms


def _ordre(p: Path) -> tuple[int, str]:
    m = re.match(r"^(\d+)", p.stem)
    return (int(m.group(1)) if m else 985, p.name.lower())


def parties_dans(dossier: Path) -> list[Path]:
    """Les parties écrites par Claude dans le répertoire de travail, dans
    l'ordre de leur numéro."""
    return sorted((p for p in dossier.glob("*.md") if p.is_file()), key=_ordre)


def assembler(chemins: list[Path]) -> str:
    morceaux = [p.read_text(encoding="utf-8", errors="replace").strip() for p in chemins]
    return "\n\n".join(m for m in morceaux if m) + "\n"


def plan(chemins: list[Path]) -> str:
    """« - 010.md — ## Titre (420 mots) : sous-catégorie ; autre… » pour
    chaque partie : Claude sait où aller sans tout relire."""
    lignes = []
    for p in chemins:
        texte = p.read_text(encoding="utf-8", errors="replace")
        titre = next((m.group(1) for m in map(_TITRE_CATEGORIE.match, texte.splitlines()) if m), "")
        sous = [l[4:].strip() for l in texte.splitlines() if l.startswith("### ")]
        ligne = f"- {p.name} — " + (f"## {titre}" if titre else "(introduction)") + f" ({mots(texte)} mots)"
        if sous:
            ligne += " : " + " ; ".join(sous[:14]) + (" ; …" if len(sous) > 14 else "")
        lignes.append(ligne)
    return "\n".join(lignes)


def lots(tailles: list[tuple[str, int]], budget: int) -> list[list[str]]:
    """Découpe des séances (clé, taille de leurs notes), dans l'ordre, en lots
    d'au plus `budget` caractères ; une séance trop grosse fait un lot à elle
    seule."""
    sortie: list[list[str]] = []
    courant: list[str] = []
    total = 0
    for cle, taille in tailles:
        if courant and total + taille > budget:
            sortie.append(courant)
            courant, total = [], 0
        courant.append(cle)
        total += taille
    if courant:
        sortie.append(courant)
    return sortie
