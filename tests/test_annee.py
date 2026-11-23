# -*- coding: utf-8 -*-
"""Tests du mode année : python -m pytest tests (ou python tests/test_annee.py)."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import annee  # noqa: E402

HORAIRE = {
    "annee": "2026-2027",
    "matieres": [
        {"nom": "Psychologie du développement", "court": "Psychologie", "couleur": "#f8bbd0",
         "enseignant": "Mme Lambert"},
        {"nom": "Terminologie médicale", "court": "Terminologie", "couleur": "bleu"},
        {"nom": "Méthodologie du travail éducatif", "couleur": ""},
    ],
    "creneaux": [
        {"jour": "lundi", "debut": "08:30", "fin": "10:30", "matiere": "Psychologie du développement"},
        {"jour": "lundi", "debut": "10:45", "fin": "12:45", "matiere": "Terminologie médicale"},
        {"jour": "mardi", "debut": "13h30", "fin": "15h30", "matiere": "Méthodologie du travail éducatif",
         "semaines": "paires"},
        {"jour": "jeudi", "debut": "8:30", "fin": "12:30", "matiere": "Psychologie",
         "du": "2026-09-14", "au": "2026-12-18"},
        {"jour": "vendredi", "debut": "09:00", "fin": "11:00", "matiere": "Terminologie médicale",
         "dates": ["2026-10-09", "2026-10-23"]},
        {"jour": "mercredi", "debut": "10:00", "fin": "09:00", "matiere": "Psychologie"},
    ],
}


def faux_lecteur(durees: dict[str, float]):
    def lire(chemin: Path):
        return None, durees.get(chemin.name, 0.0)
    return lire


def test_dates_dans_les_noms():
    cas = {
        "2026-10-05 08_31_02.mp3": (dt.datetime(2026, 10, 5, 8, 31, 2), "heure"),
        "20261005083102.WAV": (dt.datetime(2026, 10, 5, 8, 31, 2), "heure"),
        "20261005 0831.mp3": (dt.datetime(2026, 10, 5, 8, 31), "heure"),
        "REC_2026-10-05_08-31-02.m4a": (dt.datetime(2026, 10, 5, 8, 31, 2), "heure"),
        "05-10-2026 08h31 psycho.mp3": (dt.datetime(2026, 10, 5, 8, 31), "heure"),
        "Cours 5.10.2026 à 8h31.mp3": (dt.datetime(2026, 10, 5, 8, 31), "heure"),
        "2026-10-05.mp3": (dt.datetime(2026, 10, 5), "jour"),
        "psycho 05-10-2026.mp3": (dt.datetime(2026, 10, 5), "jour"),
        "enregistrement.mp3": (None, ""),
        "2026-13-45 99_99.mp3": (None, ""),
    }
    for nom, attendu in cas.items():
        assert annee.date_depuis_nom(nom) == attendu, (nom, annee.date_depuis_nom(nom))


def test_noms_plaud():
    periode = (dt.date(2026, 9, 1), dt.date(2027, 6, 22))
    d, p = annee.date_depuis_nom("09-30 Cours magistral_ Terminologie de la communication.ogg", periode)
    assert (d, p) == (dt.datetime(2026, 9, 30), "jour")
    d, _ = annee.date_depuis_nom("03-15 Philosophie.mp3", periode)
    assert d == dt.datetime(2027, 3, 15)
    d, _ = annee.date_depuis_nom("09-30 x.mp3", None, dt.date(2026, 10, 6))
    assert d == dt.datetime(2026, 9, 30)
    assert annee.titre_depuis_nom("09-30 Cours magistral_ Terminologie - écrits.ogg") == \
        "Cours magistral: Terminologie - écrits"
    assert annee.titre_depuis_nom("2026-09-08 08_33_08.ogg") == ""
    assert annee.titre_depuis_nom("1458cc06a4e24ede25b9732509133ed0.ogg") == ""


def test_matiere_du_titre():
    h = annee.Horaire.depuis_dict({"matieres": [
        {"nom": "Approche de l'éthique et déontologie"}, {"nom": "Psychologie"},
        {"nom": "Terminologie et communication professionnelle écrite"},
        {"nom": "Découverte de la profession"},
        {"nom": "Communication interpersonnelle : théorie et méthodologie"}]})
    m, mots = annee.matiere_du_titre(
        "Cours magistral: Éthique et déontologie professionnelles dans le secteur social", h.matieres)
    assert m.nom == "Approche de l'éthique et déontologie" and "ethique" in mots
    m, _ = annee.matiere_du_titre("Cours magistral: Terminologie de la communication professionnelle "
                                  "écrite - secteurs marchand", h.matieres)
    assert m.nom.startswith("Terminologie")
    assert annee.matiere_du_titre("Conférence: Secteur du handicap et travail social",
                                  h.matieres)[0] is None


def test_par_elimination():
    h = annee.Horaire.depuis_dict({"matieres": [{"nom": "Terminologie"}, {"nom": "Découverte"}],
                                   "seances": [
        {"date": "2026-09-30", "debut": "08:30", "fin": "10:10", "matiere": "Terminologie"},
        {"date": "2026-09-30", "debut": "10:25", "fin": "12:05", "matiere": "Découverte"}]})
    infos = [annee.InfoAudio(Path("a.ogg"), "a.ogg", debut=dt.datetime(2026, 9, 30), precision="jour",
                             titre="Cours magistral: Terminologie"),
             annee.InfoAudio(Path("b.mp3"), "b.mp3", debut=dt.datetime(2026, 9, 30), precision="jour",
                             titre="Conférence: Secteur du handicap")]
    a, b = annee.classer(infos, h, {})
    assert a.matiere == "Terminologie" and "titre" in a.raison
    assert b.matiere == "Découverte" and "seul cours" in b.raison


def test_dates_de_copie_ecartees():
    base = dt.datetime(2026, 10, 6, 11, 34).timestamp()
    infos = [annee.InfoAudio(Path(f"{n}.mp3"), f"{n}.mp3", mtime=base + i * 30, debut=d,
                             precision="heure", source=s)
             for i, (n, d, s) in enumerate([
                 ("a", dt.datetime(2026, 9, 8, 8, 33), "nom du fichier"),
                 ("b", dt.datetime(2026, 9, 15, 8, 39), "nom du fichier"),
                 ("c", dt.datetime(2026, 10, 6, 9, 0), annee.SOURCE_FICHIER)])]
    annee.ecarter_dates_de_copie(infos)
    assert infos[2].debut is None and infos[0].debut


def test_metadonnees_en_heure_locale():
    os.environ["TZ"] = "Europe/Brussels"
    if hasattr(__import__("time"), "tzset"):
        __import__("time").tzset()
    assert annee._date_meta("2026-10-05T06:31:02.000000Z") == dt.datetime(2026, 10, 5, 8, 31, 2)
    assert annee._date_meta("UTC 2026-10-05 06:31:02") == dt.datetime(2026, 10, 5, 8, 31, 2)
    assert annee._date_meta("2026-10-05 08:31:02") == dt.datetime(2026, 10, 5, 8, 31, 2)
    assert annee._date_meta("2026-10-05T08:31:02+02:00") == dt.datetime(2026, 10, 5, 8, 31, 2)
    assert annee._date_meta("2026") is None
    assert annee._date_meta("") is None


def test_horaire_tolerant():
    h = annee.Horaire.depuis_dict(HORAIRE)
    assert [m.titre for m in h.matieres] == ["Psychologie", "Terminologie",
                                             "Méthodologie du travail éducatif"]
    assert h.matieres[0].couleur == "#F8BBD0"
    assert h.matieres[1].couleur == annee.COULEURS_NOMMEES["bleu"]
    assert h.matieres[2].couleur                      # couleur attribuée d'office
    assert len(h.creneaux) == 5                       # le créneau à l'envers est écarté
    assert any("créneau 6" in a for a in h.avertissements)
    # « Psychologie » (nom court) désigne bien la matière complète
    assert h.creneaux[3].matiere == "Psychologie du développement"
    # aller-retour JSON sans perte
    h2 = annee.Horaire.depuis_dict(json.loads(json.dumps(h.vers_dict())))
    assert h2.vers_dict() == h.vers_dict()


def test_creneaux_actifs():
    h = annee.Horaire.depuis_dict(HORAIRE)
    lundi = dt.date(2026, 10, 5)
    assert {c.matiere for c in h.creneaux_du(lundi)} == {"Psychologie du développement",
                                                       "Terminologie médicale"}
    mardi_pair = dt.date(2026, 10, 6)                 # semaine ISO 41 : impaire
    assert h.creneaux_du(mardi_pair) == []
    assert h.creneaux_du(dt.date(2026, 10, 13))       # semaine 42 : paire
    assert h.creneaux_du(dt.date(2026, 12, 24)) == []  # jeudi hors période
    assert h.creneaux_du(dt.date(2026, 10, 9))        # vendredi listé
    assert h.creneaux_du(dt.date(2026, 10, 16)) == []  # vendredi non listé


def info(nom, debut, duree=5400.0, precision="heure"):
    return annee.InfoAudio(chemin=Path(nom), relatif=nom, debut=debut, duree=duree,
                           precision=precision, source="test")


def test_classement():
    h = annee.Horaire.depuis_dict(HORAIRE)
    cas = [
        (info("a", dt.datetime(2026, 10, 5, 8, 33)), "Psychologie du développement", "sûr"),
        (info("b", dt.datetime(2026, 10, 5, 8, 10), duree=600), "Psychologie du développement", "probable"),
        (info("c", dt.datetime(2026, 10, 5, 10, 40)), "Terminologie médicale", "sûr"),
        (info("d", dt.datetime(2026, 10, 5, 18, 0)), "", ""),
        (info("e", dt.datetime(2026, 10, 7, 9, 0)), "", ""),
        (info("f", dt.datetime(2026, 10, 15, 0, 0), precision="jour"), "Psychologie du développement",
         "probable"),
        (info("g", dt.datetime(2026, 10, 5, 0, 0), precision="jour"), "", ""),
        (info("h", None), "", ""),
    ]
    for i, matiere, confiance in cas:
        c = annee.classer_un(i, h)
        assert (c.matiere, c.confiance) == (matiere, confiance), (i.relatif, c)
    # enregistrement qui déborde sur le cours suivant : la remarque le dit
    long = annee.classer_un(info("x", dt.datetime(2026, 10, 5, 9, 0), duree=3 * 3600), h)
    assert long.matiere == "Psychologie du développement" and "déborde" in long.raison


def test_seances_et_manuels():
    h = annee.Horaire.depuis_dict(HORAIRE)
    infos = [info("p1.mp3", dt.datetime(2026, 10, 5, 8, 31), 2400),
             info("p2.mp3", dt.datetime(2026, 10, 5, 9, 20), 3600),   # même séance, après une pause
             info("t1.mp3", dt.datetime(2026, 10, 5, 10, 50)),
             info("x.mp3", dt.datetime(2026, 10, 7, 9, 0)),          # hors horaire
             info("z.mp3", dt.datetime(2026, 10, 5, 13, 0))]         # écarté
    manuels = {"x.mp3": "Terminologie", "z.mp3": annee.IGNORE}
    classes = annee.classer(infos, h, manuels)
    par = {c.audio.relatif: c for c in classes}
    assert par["x.mp3"].matiere == "Terminologie médicale" and par["x.mp3"].confiance == "manuel"
    assert par["z.mp3"].ignore and not par["z.mp3"].classe
    s = annee.seances(classes)
    psy = s["Psychologie du développement"]
    assert len(psy) == 1 and [a.audio.relatif for a in psy[0].audios] == ["p1.mp3", "p2.mp3"]
    assert psy[0].cle == "2026-10-05"
    assert psy[0].libelle == "lundi 5 octobre 2026, 08:30–10:30"
    assert [x.cle for x in s["Terminologie médicale"]] == ["2026-10-05", "2026-10-07"]


def test_dossier_annee_complet():
    with tempfile.TemporaryDirectory() as tmp:
        racine = Path(tmp) / "Année 1"
        racine.mkdir()
        # audios et horaire posés à la racine, un dossier « Psycho » déjà créé
        for nom in ("2026-10-05 08_31_02.mp3", "2026-10-05 10_47_00.mp3", "notes.txt"):
            (racine / nom).write_bytes(nom.encode("utf-8") * 3)          # contenus distincts
        (racine / "Horaire Q1.pdf").write_bytes(b"%PDF")
        (racine / "Psycho").mkdir()
        a = annee.Annee(racine)
        assert {p.name for p in a.a_ranger()} == {"2026-10-05 08_31_02.mp3",
                                                   "2026-10-05 10_47_00.mp3", "Horaire Q1.pdf"}
        messages = a.ranger()
        assert len(messages) == 3 and (racine / "notes.txt").exists()
        assert a.dossier_audios().name == annee.DOSSIER_AUDIOS
        assert a.source_horaire().name == "Horaire Q1.pdf"

        h = annee.Horaire.depuis_dict(HORAIRE)
        a.enregistrer_horaire(h)
        crees = a.creer_structure(h)
        assert "Terminologie" in crees and "Méthodologie du travail éducatif" in crees
        assert "Psychologie" not in crees                       # « Psycho » existant réutilisé
        h = a.charger_horaire()
        assert h.matieres[0].dossier == "Psycho"

        # supports : PDF de Moodle oui, fiches produites et dossier technique non
        psy = a.dossier_matiere(h.matieres[0])
        (psy / "Chapitre 1.pdf").write_bytes(b"%PDF")
        (psy / "Moodle").mkdir()
        (psy / "Moodle" / "Plan du cours.docx").write_bytes(b"PK")
        (psy / "Psychologie - Fiches résumés.docx").write_bytes(b"PK")
        (psy / "~$ychologie - Fiches résumés.docx").write_bytes(b"PK")
        (psy / "_Transcriptor").mkdir()
        (psy / "_Transcriptor" / "fiches.md").write_text("x")
        assert [p.name for p in a.supports_matiere(h.matieres[0])] == ["Chapitre 1.pdf",
                                                                       "Plan du cours.docx"]

        lecteur = faux_lecteur({"2026-10-05 08_31_02.mp3": 6000, "2026-10-05 10_47_00.mp3": 6600})
        an = a.analyser(lecteur)
        assert [c.matiere for c in an.classements] == ["Psychologie du développement",
                                                       "Terminologie médicale"]
        assert an.seances["Psychologie du développement"][0].cle == "2026-10-05"
        audio = an.infos[0].chemin
        assert a.transcription_de(audio).parent.name == annee.DOSSIER_TRANSCRIPTIONS
        # le cache des métadonnées sert au passage suivant
        cache = json.loads((racine / "_Transcriptor" / annee.CACHE_AUDIOS).read_text("utf-8"))
        assert cache["2026-10-05 08_31_02.mp3"]["duree"] == 6000
        an2 = a.analyser(lambda p: (_ for _ in ()).throw(AssertionError("relu")))
        assert an2.infos[0].duree == 6000

        a.classer_manuellement("2026-10-05 10_47_00.mp3", annee.IGNORE)
        assert a.analyser(lecteur).classements[1].ignore
        a.classer_manuellement("2026-10-05 10_47_00.mp3", "")
        assert not a.analyser(lecteur).classements[1].ignore


def test_deux_matieres_un_dossier_proche():
    with tempfile.TemporaryDirectory() as tmp:
        racine = Path(tmp)
        (racine / "Psychologie").mkdir()
        h = annee.Horaire.depuis_dict({"matieres": [
            {"nom": "Psychologie du développement"}, {"nom": "Psychologie sociale"}]})
        a = annee.Annee(racine)
        a.creer_structure(h)
        dossiers = [m.dossier for m in h.matieres]
        assert len(set(dossiers)) == 2, dossiers


def test_ics():
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "BEGIN:VEVENT", "DTSTART:20261005T083000", "DTEND:20261005T103000",
        "SUMMARY:Psychologie du développement - Mme Lambert", "LOCATION:A12", "END:VEVENT",
        "BEGIN:VEVENT", "DTSTART:20261012T083000", "DTEND:20261012T103000",
        "SUMMARY:Psychologie du développement - Mme Lambert", "END:VEVENT",
        "BEGIN:VEVENT", "DTSTART:20261006T133000", "DTEND:20261006T153000",
        "SUMMARY:Terminologie (TP)", "END:VEVENT", "END:VCALENDAR"])
    h = annee.horaire_depuis_ics(ics)
    assert [m.nom for m in h.matieres] == ["Psychologie du développement", "Terminologie"]
    psy = h.creneaux_de("Psychologie du développement")
    assert len(psy) == 1 and psy[0].dates == [dt.date(2026, 10, 5), dt.date(2026, 10, 12)]
    assert h.creneaux_du(dt.date(2026, 10, 12))[0].plage == "08:30–10:30"


def test_json_dans_une_reponse():
    assert annee.json_dans('Voici :\n```json\n{"a": 1}\n```') == {"a": 1}
    assert annee.json_dans('bla {"a": {"b": "}"}} fin') == {"a": {"b": "}"}}
    try:
        annee.json_dans("rien")
    except ValueError:
        pass
    else:
        raise AssertionError


def test_couleurs():
    assert annee.couleur_normale("#abc") == "#AABBCC"
    assert annee.couleur_normale("rgb(248, 187, 208)") == "#F8BBD0"
    assert annee.couleur_normale("Rose") == annee.COULEURS_NOMMEES["rose"]
    assert annee.couleur_normale("pas une couleur") == ""
    assert annee.texte_sur("#F8BBD0") == "#1A1A1A" and annee.texte_sur("#1E4E8C") == "#FFFFFF"
    assert annee.luminance(annee.foncee("#F8BBD0")) <= 0.12


def test_nom_de_dossier():
    assert annee.nom_dossier('Psycho: "dév" / ados?') == "Psycho dév ados"
    assert annee.nom_dossier("CON") == "CON_"
    assert annee.nom_dossier("  ") == "Matière"


if __name__ == "__main__":
    import inspect
    for nom, f in sorted(globals().items()):
        if nom.startswith("test_") and inspect.isfunction(f):
            f()
            print("ok", nom)
