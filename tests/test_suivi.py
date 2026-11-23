# -*- coding: utf-8 -*-
"""Ce qui est déjà dans les fiches (scripts/suivi.py) et le registre des
enregistrements (scripts/annee.py) : empreintes, doublons, transcriptions
retrouvées après un renommage ou un rangement, fiches en parties, lots,
traduction d'un état d'avant.

    python -m pytest tests/test_suivi.py -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI.parent / "scripts"))
import annee  # noqa: E402
import suivi  # noqa: E402

FICHES = """## Les fondements

Texte **important**.

### Définitions
- un
- deux

```mermaid
flowchart TD
## pas un titre
A --> B
```
*Schéma — un schéma*

## Lexique

| Terme | Définition courte |
|---|---|
| Posture | Attitude |

## Les approches

Encore du texte.
"""


def test_parties_des_fiches():
    parties = suivi.decouper(FICHES)
    assert [t for t, _ in parties] == ["Les fondements", "Lexique", "Les approches"]
    noms = suivi.nommer(parties)
    assert list(noms) == ["010.md", "990.md", "020.md"]
    assert "## pas un titre" in noms["010.md"]                  # dans un bloc de code
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        for nom, texte in noms.items():
            (d / nom).write_text(texte, encoding="utf-8")
        (d / "015.md").write_text("## Intercalée\n\nNouvelle catégorie.\n", encoding="utf-8")
        ordre = [p.name for p in suivi.parties_dans(d)]
        assert ordre == ["010.md", "015.md", "020.md", "990.md"]   # le lexique reste à la fin
        md = suivi.assembler(suivi.parties_dans(d))
        assert md.index("## Intercalée") < md.index("## Les approches") < md.index("## Lexique")
        plan = suivi.plan(suivi.parties_dans(d))
        assert "- 010.md — ## Les fondements" in plan and ": Définitions" in plan
    # préambule éventuel
    assert list(suivi.nommer(suivi.decouper("Intro.\n\n## A\nx\n"))) == ["000.md", "010.md"]


def test_lots():
    assert suivi.lots([("a", 50), ("b", 60), ("c", 10), ("d", 200)], 100) == [["a"], ["b", "c"], ["d"]]
    assert suivi.lots([], 100) == []


def test_etat_et_traduction_d_un_etat_d_avant():
    with tempfile.TemporaryDirectory() as t:
        travail = Path(t) / "_Transcriptor"
        (travail / "seances").mkdir(parents=True)
        n1, n2 = travail / "seances" / "2026-09-08.md", travail / "seances" / "2026-09-15.md"
        n1.write_text("notes un", encoding="utf-8")
        doc = Path(t) / "plan.pdf"
        doc.write_bytes(b"%PDF")
        vieux = time.time() - 3600
        for p in (n1, doc):
            os.utime(p, (vieux, vieux))
        (travail / "fiches.md").write_text("## A\n", encoding="utf-8")
        # Version 1, signature périmée : est dedans ce qui est plus ancien que fiches.md.
        (travail / "etat.json").write_text(json.dumps({"signature": "x", "genere": "2026-11-22T22:10:00"}))
        n2.write_text("notes deux", encoding="utf-8")
        os.utime(n2, (time.time() + 60, time.time() + 60))      # écrites après les fiches
        notes = {"2026-09-08": n1, "2026-09-15": n2}
        e = suivi.EtatMatiere.charger(travail, notes, {"plan.pdf": doc}, signature_v1=lambda: "y")
        assert e.fiches.seance_integree("2026-09-08", n1)
        assert not e.fiches.seance_integree("2026-09-15", n2)
        assert e.fiches.support_integre("plan.pdf", doc)
        assert not e.infos.seances                                # pas d'infos.md : rien dedans
        # Signature identique : tout est dedans, même ce qui est plus récent.
        e = suivi.EtatMatiere.charger(travail, notes, {}, signature_v1=lambda: "x")
        assert e.fiches.seance_integree("2026-09-15", n2)
        # Notes réécrites à l'identique : toujours dedans ; changées : plus dedans.
        e.fiches.noter_seance("2026-09-15", n2)
        e.sauver()
        n2.write_text("notes deux", encoding="utf-8")
        os.utime(n2, (time.time() + 120, time.time() + 120))
        e = suivi.EtatMatiere.charger(travail, notes, {})
        assert json.loads((travail / "etat.json").read_text())["version"] == 2
        assert e.fiches.seance_integree("2026-09-15", n2)
        n2.write_text("notes deux, revues", encoding="utf-8")
        assert not e.fiches.seance_integree("2026-09-15", n2)
        # Document modifié : plus dedans.
        e.fiches.noter_support("plan.pdf", doc)
        doc.write_bytes(b"%PDF-2")
        assert not e.fiches.support_integre("plan.pdf", doc)


def _annee_avec(t: str, fichiers: dict[str, bytes]) -> annee.Annee:
    racine = Path(t) / "Année"
    base = racine / annee.DOSSIER_AUDIOS
    base.mkdir(parents=True)
    for nom, contenu in fichiers.items():
        (base / nom).parent.mkdir(parents=True, exist_ok=True)
        (base / nom).write_bytes(contenu)
    return annee.Annee(racine)


def _lecteur(_chemin):
    return None, 600.0


def test_registre_doublons_et_transcriptions():
    with tempfile.TemporaryDirectory() as t:
        a = _annee_avec(t, {"2026-09-08 08_33_08.ogg": b"A" * 5000, "copie.ogg": b"A" * 5000,
                            "2026-09-15 08_30_00.ogg": b"B" * 5000})
        base = a.dossier_audios()
        infos = {i.relatif: i for i in a.infos_audios(_lecteur)}
        assert infos["copie.ogg"].empreinte == infos["2026-09-08 08_33_08.ogg"].empreinte
        assert infos["copie.ogg"].doublon_de == "2026-09-08 08_33_08.ogg"
        assert not infos["2026-09-08 08_33_08.ogg"].doublon_de
        classes = {c.audio.relatif: c for c in annee.classer(list(infos.values()), None, {})}
        assert classes["copie.ogg"].ignore and classes["copie.ogg"].doublon

        # Une transcription au chemin « miroir » est retenue par le registre…
        t1 = base / "Transcriptions" / "2026-09-08 08_33_08.txt"
        t1.parent.mkdir(parents=True)
        t1.write_text("Cours : Psychologie\nFichier : 2026-09-08 08_33_08.ogg\n\ntexte", encoding="utf-8")
        trans = a.transcriptions(list(infos.values()))
        assert trans["2026-09-08 08_33_08.ogg"] == t1
        # … et retrouvée après un rangement par l'application,
        info = infos["2026-09-08 08_33_08.ogg"]
        dest = a.deplacer_audio(info, "Traités/2026-09")
        assert dest == base / "Traités" / "2026-09" / "2026-09-08 08_33_08.ogg"
        infos = {i.relatif: i for i in a.infos_audios(_lecteur)}
        rel = "Traités/2026-09/2026-09-08 08_33_08.ogg"
        assert a.est_range(rel) and rel in infos
        assert infos[rel].debut.strftime("%Y-%m-%d %H:%M") == "2026-09-08 08:33"
        assert a.transcriptions(list(infos.values()))[rel] == t1
        # le doublon pointe désormais vers le nouveau chemin
        assert infos["copie.ogg"].doublon_de == rel

        # … et après un renommage à la main : même date (lue sous le premier nom).
        (base / rel).rename(base / "Psy.ogg")
        infos = {i.relatif: i for i in a.infos_audios(_lecteur)}
        assert infos["Psy.ogg"].debut.strftime("%Y-%m-%d %H:%M") == "2026-09-08 08:33"
        assert a.transcriptions(list(infos.values()))["Psy.ogg"] == t1

        # Sans registre (renommé avant tout passage) : retrouvée par son en-tête.
        t2 = base / "Transcriptions" / "vieux" / "2026-09-15 08_30_00.txt"
        t2.parent.mkdir(parents=True)
        t2.write_text("Fichier : 2026-09-15 08_30_00.ogg\n\ntexte", encoding="utf-8")
        infos = {i.relatif: i for i in a.infos_audios(_lecteur)}
        assert a.transcriptions(list(infos.values()))["2026-09-15 08_30_00.ogg"] != t2
        assert a.transcriptions(list(infos.values()), chercher=True)["2026-09-15 08_30_00.ogg"] == t2

        # Rangement sans écraser un fichier de même nom.
        (base / "Écartés").mkdir()
        (base / "Écartés" / "copie.ogg").write_bytes(b"autre")
        dest = a.deplacer_audio(infos["copie.ogg"], "Écartés")
        assert dest.name == "copie (2).ogg"


def test_classement_manuel_par_empreinte():
    with tempfile.TemporaryDirectory() as t:
        a = _annee_avec(t, {"inconnu.ogg": b"C" * 3000})
        infos = a.infos_audios(_lecteur)
        # Ancien format (par chemin) : traduit à la première analyse.
        annee._ecrire_json(a.technique / annee.CLASSEMENT_JSON, {"inconnu.ogg": "Psychologie"})
        an_ = a.analyser(_lecteur)
        assert an_.classements[0].matiere == "Psychologie" and an_.classements[0].confiance == "manuel"
        assert infos[0].empreinte in a.manuels() and "inconnu.ogg" not in a.manuels()
        # Le choix suit l'audio rangé.
        a.deplacer_audio(an_.infos[0], "Traités/sans date")
        an_ = a.analyser(_lecteur)
        assert an_.classements[0].audio.relatif == "Traités/sans date/inconnu.ogg"
        assert an_.classements[0].matiere == "Psychologie"
        a.classer_manuellement("Traités/sans date/inconnu.ogg", "")
        assert not a.manuels()
