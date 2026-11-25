# -*- coding: utf-8 -*-
"""Mode année de bout en bout, avec Whisper et Claude simulés (tests/simulation.py) :
lecture de l'horaire, rangement des dossiers, classement, transcriptions, notes,
fiches et infos en Word, reprise sans rien refaire, classement manuel, arrêt,
limite d'utilisation de Claude.

    python -m pytest tests/test_annee_bout_en_bout.py -q
"""
from __future__ import annotations

import json
import re
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
from simulation import attendre, preparer_environnement  # noqa: E402

HORAIRE = {
    "annee": "2026-2027", "formation": "Bachelier en éducation spécialisée",
    "matieres": [
        {"nom": "Psychologie", "court": "Psychologie", "ue": "UE 2C", "couleur": "#E2F0D9"},
        {"nom": "Approche de l’éthique et déontologie", "court": "Éthique et déontologie",
         "ue": "UE 3C", "couleur": "#FFF2CC"},
        {"nom": "Terminologie et communication professionnelle écrite", "court": "Terminologie",
         "ue": "UE 3C", "couleur": "#FFF2CC"},
        {"nom": "Droit", "court": "Droit", "ue": "UE 1C", "couleur": "#DDEBF7"},
    ],
    "seances": [
        {"date": "2026-09-08", "debut": "08:30", "fin": "10:10", "matiere": "Psychologie", "enseignant": "Dupont"},
        {"date": "2026-09-08", "debut": "10:25", "fin": "12:05", "matiere": "Psychologie", "enseignant": "Dupont"},
        {"date": "2026-09-08", "debut": "12:55", "fin": "14:35",
         "matiere": "Approche de l’éthique et déontologie", "enseignant": "Lambert"},
        {"date": "2026-09-30", "debut": "08:30", "fin": "10:10",
         "matiere": "Terminologie et communication professionnelle écrite", "enseignant": "Leroy"},
        {"date": "2026-09-30", "debut": "10:25", "fin": "12:05", "matiere": "Droit", "enseignant": "Renard"},
    ],
}
AUDIOS = ["2026-09-08 08_33_08.ogg", "2026-09-08 10_29_54.mp3",
          "09-08 Cours magistral_ Éthique et déontologie professionnelles.ogg",
          "09-30 Cours magistral_ Terminologie de la communication professionnelle écrite.ogg",
          "1458cc06a4e24ede25b9732509133ed0.ogg"]


def _environnement(tmp: Path):
    (tmp / "horaire_claude.json").write_text(json.dumps(HORAIRE, ensure_ascii=False), encoding="utf-8")
    # TRANSCRIPTOR_MERMAID_TEST : un mermaid.min.js local pour dessiner les schémas.
    mermaid = os.environ.get("TRANSCRIPTOR_MERMAID_TEST")
    preparer_environnement(tmp / "env", horaire_json=tmp / "horaire_claude.json",
                           mermaid=Path(mermaid) if mermaid else None)
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome:
        os.environ["TRANSCRIPTOR_CHROMIUM"] = chrome
    sys.modules.pop("transcriptor", None)
    sys.path.insert(0, str(ICI.parent))
    import transcriptor
    return transcriptor


def _annee(tmp: Path) -> Path:
    annee = tmp / "Année 1"
    annee.mkdir()
    for nom in AUDIOS:
        (annee / nom).write_bytes(nom.encode("utf-8") + b"\0" * 2048)   # contenus distincts
    (annee / "Horaire_2026-2027.pdf").write_bytes(b"%PDF-1.4 pas un vrai PDF")
    (annee / "Psycho").mkdir()
    (annee / "Psycho" / "Plan du cours.txt").write_text(
        "Plan du cours de psychologie : Piaget, Erikson, Bowlby. Examen écrit en janvier.",
        encoding="utf-8")
    (annee / "Justificatif d'absence").mkdir()
    return annee


def _texte_docx(chemin: Path) -> str:
    with zipfile.ZipFile(chemin) as z:
        return z.read("word/document.xml").decode("utf-8")


def test_annee_de_bout_en_bout():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tr = _environnement(tmp)
        racine = _annee(tmp)
        p = tr.Passerelle()

        etat = p.ouvrir_annee(str(racine))
        assert "erreur" not in etat, etat
        assert len(etat["a_ranger"]) == 6 and not etat["horaire"]["lu"]

        # 1. Lecture de l'horaire (Claude simulé renvoie le JSON)
        etat = p.lire_horaire_annee({})
        attendre(p._chantier)
        assert p._chantier.etat == "fini", p._chantier.instantane()
        etat = p.etat_annee()
        assert etat["horaire"]["lu"] and len(etat["matieres"]) == 4
        assert etat["horaire"]["formation"] == "Bachelier en éducation spécialisée"

        # 2. Dossiers : audios et horaire rangés, un dossier par matière, « Psycho » réutilisé
        etat = p.preparer_dossiers_annee()
        assert "erreur" not in etat, etat
        assert not etat["a_ranger"] and etat["audios"]["existe"]
        dossiers = {m["court"]: m["dossier"] for m in etat["matieres"]}
        assert dossiers["Psychologie"] == "Psycho"
        assert all((racine / d).is_dir() for d in dossiers.values())
        assert (racine / "Justificatif d'absence").is_dir()

        # 3. Classement : horodatés, titre Plaud, inconnu
        par_nom = {e["nom"]: e for e in etat["enregistrements"]}
        assert par_nom["2026-09-08 08_33_08.ogg"]["matiere"] == "Psychologie"
        assert par_nom[AUDIOS[2]]["matiere"].startswith("Approche")
        assert par_nom[AUDIOS[3]]["matiere"].startswith("Terminologie")
        assert par_nom[AUDIOS[4]]["matiere"] == ""

        # 4. Tout traiter
        etat = p.lancer_annee({"modele": "large-v3"})
        assert "erreur" not in etat, etat
        attendre(p._chantier, 300)
        bilan = p._chantier.instantane()
        assert bilan["etat"] == "fini", bilan
        statuts = {t["cle"]: t["statut"] for t in bilan["taches"]}
        assert all(s == "fait" for s in statuts.values()), statuts
        assert sum(1 for c in statuts if c.startswith("t:")) == 4      # l'audio inconnu attend
        assert sum(1 for c in statuts if c.startswith("n:")) == 3      # Psychologie : 2 audios, 1 séance
        assert any("à classer" in r for r in bilan["remarques"])
        psy = racine / "Psycho"
        fiches = psy / "Psychologie - Fiches résumés.docx"
        infos = psy / "Psychologie - Infos importantes.docx"
        assert fiches.is_file() and infos.is_file()
        xml = _texte_docx(fiches)
        assert "Psychologie" in xml and "posture professionnelle" in xml and "Lexique" in xml
        assert "Déroulé de l" in _texte_docx(infos)
        assert (psy / "_Transcriptor" / "seances" / "2026-09-08.md").is_file()
        notes = (psy / "_Transcriptor" / "seances" / "2026-09-08.md").read_text(encoding="utf-8")
        assert "mardi 8 septembre 2026, 08:30–12:05" in notes
        assert (racine / "00 - Audios et horaire" / "Transcriptions" / "2026-09-08 08_33_08.txt").is_file()
        assert not (racine / "Droit" / "Droit - Fiches résumés.docx").exists()   # rien d'enregistré
        etat = p.etat_annee()
        m = {x["court"]: x for x in etat["matieres"]}
        assert m["Psychologie"]["a_jour"] and m["Psychologie"]["notes"] == 1
        assert m["Psychologie"]["supports"] == 1

        # la séance de Psychologie a reçu ses deux enregistrements
        appels = [json.loads(l) for l in (tmp / "env" / "appels_claude.jsonl").read_text("utf-8").splitlines()]
        assert any("--allowedTools" in a["args"] and "Read,Write,Edit" in a["args"] for a in appels)

        # 5. Relance : tout est à jour, rien n'est refait
        nb = len(appels)
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier)
        assert not p._chantier.instantane()["taches"]
        assert any("à jour" in r for r in p._chantier.remarques)
        assert len((tmp / "env" / "appels_claude.jsonl").read_text("utf-8").splitlines()) == nb

        # 6. Classement manuel de l'audio sans date : seule Terminologie est refaite
        etat = p.classer_audio_annee(AUDIOS[4], "Terminologie")
        assert {e["nom"]: e for e in etat["enregistrements"]}[AUDIOS[4]]["confiance"] == "manuel"
        ancien = (racine / "Terminologie" / "Terminologie - Fiches résumés.docx").stat().st_mtime
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier, 300)
        taches = p._chantier.instantane()["taches"]
        assert {t["matiere"] for t in taches} == {"Terminologie et communication professionnelle écrite"}
        assert all(t["statut"] == "fait" for t in taches), taches
        terminologie = racine / "Terminologie"
        assert (terminologie / "Terminologie - Fiches résumés.docx").stat().st_mtime >= ancien
        versions = list((terminologie / "_Transcriptor" / "versions").glob("*.docx"))
        assert versions, "la version précédente aurait dû être gardée"

        # 7. Remise en page Word seule
        p.refaire_word_annee("")
        attendre(p._chantier)
        taches = p._chantier.instantane()["taches"]
        assert taches and all(t["genre"] == "word" and t["statut"] == "fait" for t in taches), taches

        # 8. Matière sans enregistrement : rédigée à la demande, suivie ensuite
        droit = racine / "Droit"
        (droit / "Syllabus.txt").write_text("Droit de la jeunesse : aide, protection, mandats.",
                                            encoding="utf-8")
        m = {x["court"]: x for x in p.etat_annee()["matieres"]}
        assert m["Droit"]["supports"] == 1 and not m["Droit"]["seances"] and not m["Droit"]["a_jour"]
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier)
        assert not p._chantier.instantane()["taches"]          # « Tout traiter » ne l'invente pas
        p.lancer_annee({"modele": "large-v3", "matieres": ["Droit"], "forcer": True})
        attendre(p._chantier, 300)
        taches = p._chantier.instantane()["taches"]
        assert taches and all(t["statut"] == "fait" for t in taches), taches
        assert (droit / "Droit - Fiches résumés.docx").is_file()
        assert (droit / "Droit - Infos importantes.docx").is_file()
        assert {x["court"]: x for x in p.etat_annee()["matieres"]}["Droit"]["a_jour"]
        (droit / "Mandats.md").write_text("# Mandats\nSAJ, SPJ, tribunal de la jeunesse.", encoding="utf-8")
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier, 300)
        assert {t["cle"] for t in p._chantier.instantane()["taches"]} == {"f:Droit", "i:Droit"}

        # 9. Ouverture de chemins : seulement dans l'année
        assert p.ouvrir_annee_chemin(str(tmp / "env")).get("erreur")


def test_limite_claude_et_arret():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tr = _environnement(tmp)
        racine = _annee(tmp)
        p = tr.Passerelle()
        p.ouvrir_annee(str(racine))
        p.lire_horaire_annee({})
        attendre(p._chantier)
        p.preparer_dossiers_annee()

        # Limite d'utilisation : les transcriptions continuent, Claude s'arrête net.
        os.environ["FAUX_CLAUDE_MODE"] = "limite"
        try:
            p.lancer_annee({"modele": "large-v3"})
            attendre(p._chantier, 300)
        finally:
            os.environ.pop("FAUX_CLAUDE_MODE", None)
        taches = p._chantier.instantane()["taches"]
        assert all(t["statut"] == "fait" for t in taches if t["genre"] == "transcription"), taches
        claude = [t for t in taches if t["genre"] != "transcription"]
        assert sum(1 for t in claude if t["statut"] == "erreur") == 1, claude
        assert all(t["statut"] in ("erreur", "ignore") for t in claude), claude
        assert any("limite" in (t["detail"] or "").lower() for t in claude)

        # Arrêt demandé : le chantier s'arrête proprement.
        p.lancer_annee({"modele": "large-v3"})
        p.arreter_annee()
        attendre(p._chantier, 120)
        assert p._chantier.etat in ("arrete", "fini")


def _lignes(chemin: Path) -> int:
    return len(chemin.read_text("utf-8").splitlines()) if chemin.is_file() else 0


def test_ajouts_rangement_et_reecriture():
    """Une année qui avance : les audios déjà dans les fiches sont rangés et
    jamais refaits (même renommés ou en double) ; une nouvelle séance est
    ajoutée aux fiches sans relire les anciennes ; « Tout réécrire » repart
    de zéro, par lots."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tr = _environnement(tmp)
        racine = _annee(tmp)
        p = tr.Passerelle()
        p.ouvrir_annee(str(racine))
        p.lire_horaire_annee({})
        attendre(p._chantier)
        p.preparer_dossiers_annee()
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier, 300)
        audios = racine / "00 - Audios et horaire"
        traites = audios / "Traités" / "2026-09"
        journal = tmp / "env" / "appels_claude.jsonl"
        psy = racine / "Psycho" / "_Transcriptor"

        # 1. Ce qui est dans les fiches est rangé par mois ; l'audio à classer reste.
        assert (traites / "2026-09-08 08_33_08.ogg").is_file()
        assert (traites / AUDIOS[3]).is_file()
        assert (audios / AUDIOS[4]).is_file() and not (audios / "2026-09-08 08_33_08.ogg").exists()
        assert any("Rangés" in r for r in p._chantier.remarques)
        etat = p.etat_annee()
        e = {x["nom"]: x for x in etat["enregistrements"]}
        assert e["2026-09-08 08_33_08.ogg"]["etape"] == "fiches"
        assert e["2026-09-08 08_33_08.ogg"]["range"] == "Traités/2026-09"
        assert e["2026-09-08 08_33_08.ogg"]["integre"] and e["2026-09-08 08_33_08.ogg"]["mois"] == "2026-09"
        assert e[AUDIOS[4]]["etape"] == "a_classer"
        m = {x["court"]: x for x in etat["matieres"]}
        assert m["Psychologie"]["dans_fiches"] == 1 and m["Psychologie"]["nouvelles"] == 0
        assert m["Psychologie"]["a_jour"]
        assert json.loads((psy / "etat.json").read_text("utf-8"))["version"] == 2

        # 2. Rangés, ils ne sont pas refaits : le registre retrouve leurs transcriptions.
        avant = _lignes(journal)
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier)
        assert not p._chantier.instantane()["taches"]
        assert _lignes(journal) == avant

        # 3. Une nouvelle séance de Psychologie : transcrite, mise au propre, puis
        #    ajoutée aux fiches et aux infos — seule, sans les anciennes notes.
        nouveau = "2026-09-15 08_35_00.ogg"
        (audios / nouveau).write_bytes(b"nouvelle seance" + b"\0" * 4096)
        p.classer_audio_annee(nouveau, "Psychologie")
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier, 300)
        taches = {x["cle"]: x for x in p._chantier.instantane()["taches"]}
        assert set(taches) == {f"t:{nouveau}", "n:Psychologie:2026-09-15", "f:Psychologie",
                               "i:Psychologie"}, taches
        assert all(x["statut"] == "fait" for x in taches.values()), taches
        assert taches["f:Psychologie"]["detail"].startswith("ajout : 1 séance")
        fiches = (psy / "fiches.md").read_text("utf-8")
        assert "Apports du 2026-09-15" in fiches and "2026-09-08, 2026-09-15" not in fiches
        assert fiches.index("## Les fondements") < fiches.index("## Repères pour 2026-09-15") \
            < fiches.index("## Lexique")
        assert fiches.count("## Lexique") == 1 and "Bientraitance" in fiches
        infos = (psy / "infos.md").read_text("utf-8")
        assert "Déroulé de l" in infos and "15 septembre 2026" in infos.split("## Mise à jour")[1]
        assert "8 septembre" not in infos.split("## Mise à jour")[1]       # seulement la nouvelle
        assert (traites / nouveau).is_file()
        etat_json = json.loads((psy / "etat.json").read_text("utf-8"))
        assert set(etat_json["fiches"]["seances"]) == {"2026-09-08", "2026-09-15"}
        assert etat_json["fiches"]["ajouts"] == 1
        assert {x["court"]: x for x in p.etat_annee()["matieres"]}["Psychologie"]["dans_fiches"] == 2

        # 4. Renommé et remis à la racine à la main : même date, même séance, rien de refait.
        shutil.move(str(traites / nouveau), str(audios / "Psy du 15.ogg"))
        e = {x["nom"]: x for x in p.etat_annee()["enregistrements"]}
        assert e["Psy du 15.ogg"]["matiere"] == "Psychologie" and e["Psy du 15.ogg"]["etape"] == "fiches"
        avant = _lignes(journal)
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier)
        assert not p._chantier.instantane()["taches"] and _lignes(journal) == avant
        assert (traites / "Psy du 15.ogg").is_file()                    # rangé de nouveau

        # 5. Une copie d'un audio déjà traité : doublon, ni transcrit ni rédigé, écarté.
        shutil.copy2(traites / "2026-09-08 08_33_08.ogg", audios / "copie.ogg")
        e = {x["nom"]: x for x in p.etat_annee()["enregistrements"]}
        assert e["copie.ogg"]["etape"] == "doublon" and e["copie.ogg"]["doublon"]
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier)
        assert not p._chantier.instantane()["taches"]
        assert (audios / "Écartés" / "copie.ogg").is_file()

        # 6. Un état d'avant (version 1, signature globale) : traduit, rien de refait.
        (psy / "etat.json").write_text(json.dumps({"signature": "ancienne", "genere": "2026-11-22T22:10:00",
                                                    "seances": ["2026-09-08", "2026-09-15"]}), "utf-8")
        avant = _lignes(journal)
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier)
        assert not p._chantier.instantane()["taches"] and _lignes(journal) == avant

        # 7. Tout réécrire, par lots (budget minuscule : une séance par passage).
        tr.BUDGET_NOTES_LOT = 10
        try:
            p.lancer_annee({"modele": "large-v3", "matieres": ["Psychologie"], "reecrire": True})
            attendre(p._chantier, 300)
        finally:
            tr.BUDGET_NOTES_LOT = 150_000
        taches = {x["cle"]: x for x in p._chantier.instantane()["taches"]}
        assert set(taches) == {"f:Psychologie", "i:Psychologie"}, taches
        assert all(x["statut"] == "fait" for x in taches.values()), taches
        assert taches["f:Psychologie"]["detail"].startswith("réécrites : 2 séance(s)")
        fiches = (psy / "fiches.md").read_text("utf-8")
        assert "Séances rédigées : 2026-09-08" in fiches                  # lot 1 : rédaction complète
        assert "Apports du 2026-09-15" in fiches and fiches.count("## Lexique") == 1   # lot 2 : ajout
        assert "## Mise à jour" not in (psy / "infos.md").read_text("utf-8")    # infos refaites de zéro
        etat_json = json.loads((psy / "etat.json").read_text("utf-8"))
        assert etat_json["fiches"]["ajouts"] == 1 and etat_json["infos"]["ajouts"] == 0
        assert list((psy / "versions").glob("fiches (*).md"))

        # 8. Sans rangement : les nouveaux restent où ils sont.
        p.regler_annee({"ranger": False})
        assert p.etat_annee()["ranger"] is False
        (audios / "2026-09-22 08_40_00.ogg").write_bytes(b"troisieme" + b"\0" * 4096)
        p.classer_audio_annee("2026-09-22 08_40_00.ogg", "Psychologie")
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier, 300)
        assert (audios / "2026-09-22 08_40_00.ogg").is_file()
        e = {x["nom"]: x for x in p.etat_annee()["enregistrements"]}
        assert e["2026-09-22 08_40_00.ogg"]["etape"] == "fiches" and not e["2026-09-22 08_40_00.ogg"]["range"]


def _diapos(chemin: Path, titre: str, texte: str) -> None:
    import pptx
    pres = pptx.Presentation()
    diapo = pres.slides.add_slide(pres.slide_layouts[1])
    diapo.shapes.title.text = titre
    diapo.placeholders[1].text = texte
    pres.save(chemin)


def _entrees(tmp: Path, genre: str) -> dict[str, str]:
    """Les entrées remises à Claude (fiches, infos…), par dossier d'atelier."""
    ateliers = (tmp / "env" / "local" / "Transcriptor" / "atelier").glob(f"annee-{genre}-*")
    return {a.name: (a / "entree_claude.txt").read_text("utf-8") for a in ateliers
            if (a / "entree_claude.txt").is_file()}


def test_supports_de_cours():
    """Les supports posés dans le dossier d'une matière (syllabus, PowerPoint,
    notes…) : montrés matière par matière avec leur rôle et leur état, lus
    avec les séances — le syllabus d'abord ; un ancien format est signalé à
    convertir ; un support ajouté en cours d'année est intégré sans tout
    refaire."""
    import docx
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tr = _environnement(tmp)
        racine = _annee(tmp)
        psy = racine / "Psycho"
        doc = docx.Document()
        doc.add_heading("Psychologie du développement", level=1)
        doc.add_paragraph("Objectifs : situer les grandes théories du développement. "
                          "Évaluation : examen écrit en janvier, questions ouvertes.")
        doc.save(psy / "Syllabus Psychologie 2026-2027.docx")
        _diapos(psy / "Cours 1 - Attachement.pptx", "La théorie de l'attachement", "Bowlby, Ainsworth")
        (psy / "Cours 0 - Introduction.ppt").write_bytes(b"\xd0\xcf\x11\xe0 ancien format")
        (psy / "Travail de groupe").mkdir()
        (psy / "Travail de groupe" / "Sources.txt").write_text("Sources : Baltes, Salthouse.",
                                                              encoding="utf-8")
        p = tr.Passerelle()
        p.ouvrir_annee(str(racine))
        p.lire_horaire_annee({})
        attendre(p._chantier)
        p.preparer_dossiers_annee()

        # 1. Trouvés et montrés avec la matière : rôle, format, ancien format à convertir.
        m = {x["court"]: x for x in p.etat_annee()["matieres"]}["Psychologie"]
        docs = m["documents"]
        assert [d["relatif"] for d in docs] == [
            "Plan du cours.txt", "Syllabus Psychologie 2026-2027.docx", "Cours 1 - Attachement.pptx",
            "Travail de groupe/Sources.txt", "Cours 0 - Introduction.ppt"], docs
        assert [d["role"] for d in docs] == ["syllabus", "syllabus", "diapositives", "", ""]
        assert [d["format"] for d in docs] == ["Notes", "Word", "PowerPoint", "Notes",
                                               "PowerPoint ancien format"]
        assert docs[3]["sous_dossier"] == "Travail de groupe"
        assert [d["etat"] for d in docs] == ["nouveau"] * 4 + ["a_convertir"]
        assert "Enregistrer sous" in docs[4]["conseil"] and not docs[0]["conseil"]
        assert m["supports"] == 4 and m["a_convertir"] == 1

        # 2. Tout traiter : le syllabus est remis à Claude en premier, annoncé comme
        #    tel, pour les fiches comme pour les infos ; l'ancien format est signalé.
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier, 300)
        assert p._chantier.etat == "fini", p._chantier.instantane()
        assert any("Cours 0 - Introduction.ppt" in r and "Enregistrer sous" in r
                   for r in p._chantier.remarques), p._chantier.remarques
        fiches = [e for e in _entrees(tmp, "fiches").values() if "Syllabus Psychologie" in e]
        infos = [e for e in _entrees(tmp, "infos").values() if "Syllabus Psychologie" in e]
        assert len(fiches) == 1 and len(infos) == 1
        for entree in fiches + infos:
            assert tr.sup.CONSIGNE_ROLES in entree
            assert ('<document type="syllabus — document Word" fichier="Syllabus Psychologie 2026-2027.docx">'
                    in entree)
            assert "Évaluation : examen écrit en janvier" in entree
            assert (entree.index('fichier="Plan du cours.txt"')
                    < entree.index('fichier="Syllabus Psychologie 2026-2027.docx"')
                    < entree.index('<document type="diapositives" fichier="Cours 1 - Attachement.pptx">')
                    < entree.index('fichier="Travail de groupe/Sources.txt"'))
            assert "Cours 0 - Introduction" not in entree
        m = {x["court"]: x for x in p.etat_annee()["matieres"]}["Psychologie"]
        assert [d["etat"] for d in m["documents"]] == ["fiches"] * 4 + ["a_convertir"]
        assert all(d["integre"] for d in m["documents"][:4])
        assert m["a_jour"] and not m["docs_nouveaux"]
        assert [(s["date"], s["etat"], len(s["audios"])) for s in m["detail_seances"]] == [
            ("mar 08/09", "fiches", 2)]

        # 3. Un PowerPoint arrive en cours d'année : seul lui est ajouté aux fiches.
        _diapos(psy / "Cours 2 - Développement moral.pptx", "Kohlberg", "Les stades du jugement moral")
        m = {x["court"]: x for x in p.etat_annee()["matieres"]}["Psychologie"]
        etats = {d["nom"]: d["etat"] for d in m["documents"]}
        assert etats["Cours 2 - Développement moral.pptx"] == "nouveau" and m["docs_nouveaux"] == 1
        assert not m["a_jour"]
        avant = set(_entrees(tmp, "fiches"))
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier, 300)
        taches = {x["cle"]: x for x in p._chantier.instantane()["taches"]}
        assert set(taches) == {"f:Psychologie", "i:Psychologie"}, taches
        assert all(x["statut"] == "fait" for x in taches.values()), taches
        assert taches["f:Psychologie"]["detail"].startswith("ajout"), taches["f:Psychologie"]
        ajout = [e for n, e in _entrees(tmp, "fiches").items() if n not in avant]
        assert len(ajout) == 1 and "Les stades du jugement moral" in ajout[0]
        assert "La théorie de l'attachement" not in ajout[0]          # déjà dans les fiches
        m = {x["court"]: x for x in p.etat_annee()["matieres"]}["Psychologie"]
        assert {d["nom"]: d["etat"] for d in m["documents"]}["Cours 2 - Développement moral.pptx"] == "fiches"
        assert m["a_jour"]

        # 4. L'ancien format converti : il devient un support comme les autres.
        (psy / "Cours 0 - Introduction.ppt").unlink()
        _diapos(psy / "Cours 0 - Introduction.pptx", "Introduction", "Le développement tout au long de la vie")
        m = {x["court"]: x for x in p.etat_annee()["matieres"]}["Psychologie"]
        assert m["a_convertir"] == 0 and m["supports"] == 6
        assert {d["nom"]: d["etat"] for d in m["documents"]}["Cours 0 - Introduction.pptx"] == "nouveau"
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier, 300)
        assert not any("illisible" in r for r in p._chantier.remarques), p._chantier.remarques
        assert {x["court"]: x for x in p.etat_annee()["matieres"]}["Psychologie"]["a_jour"]


def test_infos_tours_epuises():
    """Cas vu sur une vraie matière : six articles dans le dossier, dont le
    texte dépasse ce que reçoit la fiche « Infos ». Claude les ouvrait un à un
    pour en lire le titre et épuisait ses 6 tours sans rien rédiger. Il a
    maintenant des tours selon le nombre de documents, le début des articles
    sans place est reproduit, et s'il manque encore de tours, la fiche est
    refaite d'après le texte reproduit, sans rien ouvrir."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tr = _environnement(tmp)
        racine = _annee(tmp)
        groupe = racine / "Psycho" / "Travail de groupe"
        groupe.mkdir()
        corps = "Le vieillissement cognitif normal touche surtout la vitesse de traitement. " * 600
        for i in (1, 2, 3):
            (groupe / f"Article {i}.txt").write_text(
                f"Article {i} : Trajectoires du vieillissement cognitif\nAuteur {i}, Université de Liège\n"
                + ("@@OUVRE_TOUT@@ " if i == 1 else "") + corps, encoding="utf-8")
        p = tr.Passerelle()
        p.ouvrir_annee(str(racine))
        p.lire_horaire_annee({})
        attendre(p._chantier)
        p.preparer_dossiers_annee()
        p.lancer_annee({"modele": "large-v3"})
        attendre(p._chantier, 300)
        taches = {x["cle"]: x for x in p._chantier.instantane()["taches"]}
        assert taches["i:Psychologie"]["statut"] == "fait", taches["i:Psychologie"]
        assert any("Psychologie : infos rédigées d'après le texte des documents" in r
                   for r in p._chantier.remarques), p._chantier.remarques
        assert (racine / "Psycho" / "Psychologie - Infos importantes.docx").is_file()

        # Premier essai : Read permis, tours selon les 4 documents remis ;
        # reprise : 6 tours, aucun outil, consigne de ne rien ouvrir.
        appels = [json.loads(l)["args"] for l in
                  (tmp / "env" / "appels_claude.jsonl").read_text("utf-8").splitlines()]
        infos = [a for a in appels if "infos, les informations pratiques" in a[a.index("-p") + 1]]
        premier = [a for a in infos if "--allowedTools" in a]
        reprise = [a for a in infos if "N'ouvre aucun fichier" in a[a.index("-p") + 1]]
        assert len(premier) == 1 and len(reprise) == 1, infos
        premier, reprise = premier[0], reprise[0]
        assert premier[premier.index("--max-turns") + 1] == "16"
        assert premier[premier.index("--allowedTools") + 1] == "Read"
        assert "N'ouvre un document avec Read que pour" in premier[premier.index("-p") + 1]
        assert reprise[reprise.index("--max-turns") + 1] == "6" and "--allowedTools" not in reprise
        assert infos.index(reprise) == infos.index(premier) + 1        # reprise aussitôt

        # Ce que Claude a reçu : le 3e article n'a plus de place, son début
        # (titre, auteur) est donné ; la reprise ne l'invite à rien ouvrir.
        entrees = [e for e in _entrees(tmp, "infos").values() if "@@OUVRE_TOUT@@" in e]
        assert len(entrees) == 2
        for e in entrees:
            assert ('<document type="notes" fichier="Travail de groupe/Article 3.txt" extrait="début">\n'
                    "Article 3 : Trajectoires du vieillissement cognitif Auteur 3, Université de Liège") in e
        lecture = [e for e in entrees if "avec l'outil Read" in e]
        sans = [e for e in entrees if "Ces fichiers ne sont pas à ouvrir" in e]
        assert len(lecture) == 1 and len(sans) == 1
        assert "Read" not in sans[0].split("<supports>")[1]
        assert "[… suite non reproduite]" in sans[0]
        ateliers = (tmp / "env" / "local" / "Transcriptor" / "atelier").glob("annee-infos-*")
        sans_copie = [a for a in ateliers if "Ces fichiers ne sont pas à ouvrir"
                      in (a / "entree_claude.txt").read_text("utf-8")]
        assert sans_copie and not any((a / "supports").exists() for a in sans_copie)

        # Le journal dit ce qui s'est passé, avec le bon nombre de supports.
        journal = (racine / "Psycho" / "_Transcriptor" / "claude.log").read_text("utf-8")
        assert "supports : 4 fichier(s)" in journal
        appli = (tmp / "env" / "local" / "Transcriptor" / "journal.log").read_text("utf-8")
        assert re.search(r"Claude a utilisé ses 16 tours sans finir : il a ouvert ou parcouru "
                         r"3 fichier\(s\) \(01_Plan_du_cours\.notes\.txt, 02_Article_1.*reprise sans ouvrir", appli), appli[-1500:]


# Le nom réel d'un enregistrement Plaud de Carole : 191 caractères. Avec
# « Z:\Bachelier Educateur Carole\Année 1\00 - Audios et horaire\ » devant,
# l'audio tient (252) mais sa transcription (267, et 275 en « .partiel ») et
# sa place dans Traités\2026-09 (268) dépassaient les 259 de Windows.
NOM_LONG = ("09-30 Cours magistral_ Terminologie de la communication professionnelle écrite - "
            "secteurs marchand_non marchand, écrits professionnels, méthodologies "
            "d'intervention, Maslow, santé mentale.ogg")


def test_noms_trop_longs_pour_windows():
    """Aucun fichier créé ou déplacé par l'app ne dépasse la limite de
    Windows : la transcription d'un audio au nom très long reçoit un nom
    raccourci (et se retrouve ensuite), l'audio n'est pas rangé là où son
    chemin serait trop long."""
    assert len(NOM_LONG) == 191
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tr = _environnement(tmp)
        racine = _annee(tmp)
        (racine / AUDIOS[3]).rename(racine / NOM_LONG)
        audios = racine / "00 - Audios et horaire"
        limite = tr.an.LIMITE_CHEMIN
        tr.an.LIMITE_CHEMIN = len(str(audios)) + 1 + (259 - 61)    # même géométrie que sur Z:
        try:
            p = tr.Passerelle()
            p.ouvrir_annee(str(racine))
            p.lire_horaire_annee({})
            attendre(p._chantier)
            p.preparer_dossiers_annee()
            assert (audios / NOM_LONG).is_file()
            p.lancer_annee({"modele": "large-v3"})
            attendre(p._chantier, 300)
            taches = p._chantier.instantane()["taches"]
            assert all(x["statut"] == "fait" for x in taches), [x for x in taches if x["statut"] != "fait"]

            # La transcription : nom raccourci, marqué, sous la limite même en « .partiel ».
            txt = [f for f in (audios / "Transcriptions").rglob("*.txt") if "Terminologie" in f.name]
            assert len(txt) == 1, txt
            assert len(str(txt[0])) + len(".partiel") <= tr.an.LIMITE_CHEMIN
            assert txt[0].name.startswith("09-30 Cours magistral_ Terminologie de la communication")
            assert txt[0].name.endswith(tr.an.marque_courte(NOM_LONG) + ".txt")
            assert f"Fichier : {NOM_LONG}" in txt[0].read_text("utf-8")

            # Rangement : les autres audios vont dans Traités, celui-ci reste
            # en place (son chemin y dépasserait la limite), sans remarque.
            assert (audios / NOM_LONG).is_file()
            assert list((audios / "Traités").rglob("*.ogg"))
            assert not any("pas rangé" in r for r in p._chantier.remarques), p._chantier.remarques
            for f in racine.rglob("*"):
                if f.name != NOM_LONG:
                    assert len(str(f)) <= tr.an.LIMITE_CHEMIN, f

            # Rien n'est refait ensuite.
            p.lancer_annee({"modele": "large-v3"})
            attendre(p._chantier, 300)
            assert not [x for x in p._chantier.instantane()["taches"] if x["genre"] == "transcription"]

            # Rangé ailleurs à la main et registre perdu : la transcription au
            # nom raccourci est retrouvée par sa marque.
            (audios / "Septembre").mkdir()
            (audios / NOM_LONG).rename(audios / "Septembre" / NOM_LONG)
            (racine / "_Transcriptor" / "registre.json").unlink()
            a = p._annee
            analyse = a.analyser()
            trouve = a.transcriptions(analyse.infos, chercher=True)
            assert trouve["Septembre/" + NOM_LONG] == txt[0]
        finally:
            tr.an.LIMITE_CHEMIN = limite


def test_messages_fichier_absent():
    """« No such file or directory » n'est pas un fichier illisible : le
    message dit si le chemin est trop long pour Windows ou si le fichier a
    disparu, et ne parle plus de conversion."""
    with tempfile.TemporaryDirectory() as t:
        tr = _environnement(Path(t))
        long_ = "Z:\\Bachelier Educateur Carole\\Année 1\\00 - Audios et horaire\\Traités\\2026-09\\" + NOM_LONG
        m = tr._lisible(FileNotFoundError(2, "No such file or directory", long_))
        assert m.startswith("Chemin trop long pour Windows (268 caractères, 259 au plus) : « 09-30 Cours")
        assert "Convertis" not in m
        m = tr._lisible(OSError("[Errno 2] No such file or directory: 'Z:\\\\Année 1\\\\cours.ogg'"))
        assert m.startswith("Fichier introuvable : « cours.ogg » — déplacé, renommé ou supprimé"), m
        m = tr._lisible(ValueError("Invalid data found when processing input"))
        assert m.startswith("Ce fichier n'a pas pu être décodé. Convertis-le en MP3 ou WAV")


if __name__ == "__main__":
    test_annee_de_bout_en_bout()
    print("ok bout en bout")
    test_limite_claude_et_arret()
    print("ok limite et arrêt")
    test_ajouts_rangement_et_reecriture()
    print("ok ajouts, rangement, réécriture")
    test_supports_de_cours()
    print("ok supports de cours")
    test_infos_tours_epuises()
    print("ok infos : tours épuisés, reprise sans lecture")
    test_noms_trop_longs_pour_windows()
    print("ok noms trop longs pour Windows")
    test_messages_fichier_absent()
    print("ok messages fichier absent")
