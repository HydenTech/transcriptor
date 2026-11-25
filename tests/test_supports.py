# -*- coding: utf-8 -*-
"""Supports de cours : rôle d'un document d'après son nom (syllabus,
diapositives), formats lus (y compris les variantes Office .ppsx, .pptm,
.docm), anciens formats signalés « à convertir », bloc remis à Claude.

    python -m pytest tests/test_supports.py -q
"""
from __future__ import annotations

import re
import sys
import tempfile
import zipfile
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI.parent / "scripts"))
import annee  # noqa: E402
import supports as sup  # noqa: E402


def test_roles_d_apres_le_nom():
    cas = {
        # syllabus : plan officiel, fiche UE, DIP, notes de cours de l'enseignant
        "Syllabus Psychologie du développement 2026-2027.pdf": "syllabus",
        "DIP UE13 BAC EDU 26-27.docx": "syllabus",
        "DIP_UE2.pdf": "syllabus",
        "Fiche_UE_2.pdf": "syllabus",
        "Fiche ECTS psycho.pdf": "syllabus",
        "Plan du cours.txt": "syllabus",
        "Modalités d'évaluation.docx": "syllabus",
        "Document d'intentions pédagogiques.pdf": "syllabus",
        "Dossier pédagogique UE 4.pdf": "syllabus",
        "Psycho developpement_cognitif_adulte_cours_imprimable.pdf": "syllabus",
        "Notes de cours.docx": "syllabus",
        "Support de cours séance 3.pdf": "syllabus",
        # diapositives : PowerPoint (même s'il s'appelle « support de cours »),
        # ou PDF dont le nom le dit
        "Cours 1 - Le développement cognitif.pptx": "diapositives",
        "Support de cours séance 3.pptx": "diapositives",
        "Diaporama.ppsx": "diapositives",
        "Diapos séance 2.pdf": "diapositives",
        "Presentation.pdf": "diapositives",
        "cours_3_ppt.pdf": "diapositives",
        # le reste : documents sans rôle particulier
        "Bac Educ-UE13-Carnet de la PP-26.27-VL.docx": "",
        "Convention de stage 1C EDUC.pdf": "",
        "ludwig-2007-pertes-et-gains-dans-le-developpement-adulte.pdf": "",
        "nihms-1563187.pdf": "",
        "dipole.pdf": "",
        "Sources_scientifiques_cognition_adulte_surlignees.txt": "",
        "photo tableau.jpg": "",
        "Cours 0 - Introduction.ppt": "",
    }
    for nom, role in cas.items():
        assert sup.role_document(nom) == role, (nom, sup.role_document(nom))


def test_formats_et_conversion():
    assert sup.genre_de(Path("a.ppsx")) == sup.genre_de(Path("a.pptm")) == "diapos"
    assert sup.genre_de(Path("a.docm")) == "document"
    assert sup.genre_de(Path("a.ppt")) == sup.genre_de(Path("a.odt")) == "a_convertir"
    assert sup.libelle_format(Path("x.pptx")) == "PowerPoint"
    assert sup.libelle_format(Path("x.ppsx")) == "PowerPoint"
    assert sup.libelle_format(Path("x.PDF")) == "PDF"
    assert sup.libelle_format(Path("x.docx")) == "Word"
    assert sup.libelle_format(Path("x.md")) == "Notes"
    assert sup.libelle_format(Path("x.heic")) == "Photo"
    assert sup.libelle_format(Path("x.ppt")) == "PowerPoint ancien format"
    conseil = sup.conversion(Path("Cours 0.ppt"))
    assert "illisible" in conseil and "Enregistrer sous" in conseil and ".pptx" in conseil
    assert ".docx" in sup.conversion(Path("vieux.doc"))
    # Les dossiers des matières sont fouillés avec les mêmes extensions.
    assert set(annee.EXT_SUPPORTS) == sup.EXT_SUPPORTS


def _changer_type(src: Path, dst: Path, partie: str, type_: str) -> None:
    """Copie d'un paquet Office avec un autre type pour sa partie principale
    (ce qui distingue un .ppsx ou un .docm d'un .pptx ou d'un .docx)."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                texte = data.decode("utf-8")
                texte, n = re.subn(rf'(PartName="{re.escape(partie)}"\s+ContentType=")[^"]+',
                                   rf"\g<1>{type_}", texte)
                assert n == 1
                data = texte.encode("utf-8")
            zout.writestr(item, data)


def test_variantes_office_lues():
    import docx
    import pptx
    from pptx.util import Inches
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        pres = pptx.Presentation()
        diapo = pres.slides.add_slide(pres.slide_layouts[1])
        diapo.shapes.title.text = "La théorie de l'attachement"
        diapo.placeholders[1].text = "Bowlby : base de sécurité"
        diapo.notes_slide.notes_text_frame.text = "Insister sur Ainsworth"
        diapo.shapes.add_textbox(Inches(1), Inches(5), Inches(4), Inches(1)).text_frame.text = "Schéma"
        pres.save(tmp / "base.pptx")
        _changer_type(tmp / "base.pptx", tmp / "Diaporama attachement.ppsx", "/ppt/presentation.xml",
                      "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml")
        _changer_type(tmp / "base.pptx", tmp / "Avec macros.pptm", "/ppt/presentation.xml",
                      "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml")
        doc = docx.Document()
        doc.add_heading("Modalités d'évaluation", level=1)
        doc.add_paragraph("Examen écrit en janvier, questions ouvertes sur les théories vues au cours.")
        doc.save(tmp / "base.docx")
        _changer_type(tmp / "base.docx", tmp / "Syllabus.docm", "/word/document.xml",
                      "application/vnd.ms-word.document.macroEnabled.main+xml")
        (tmp / "Cours 0.ppt").write_bytes(b"\xd0\xcf\x11\xe0 ancien format")

        sources = [tmp / "Diaporama attachement.ppsx", tmp / "Avec macros.pptm",
                   tmp / "Syllabus.docm", tmp / "Cours 0.ppt"]
        supports, avert = sup.preparer(sources, tmp / "supports")
        lus = {s.origine: s for s in supports}
        assert lus["Diaporama attachement.ppsx"].genre == "diapos"
        assert lus["Avec macros.pptm"].genre == "diapos"
        assert lus["Syllabus.docm"].genre == "document"
        texte = (tmp / "supports" / lus["Diaporama attachement.ppsx"].fichier).read_text("utf-8")
        assert "La théorie de l'attachement" in texte and "Insister sur Ainsworth" in texte
        assert "Examen écrit en janvier" in (tmp / "supports" / lus["Syllabus.docm"].fichier).read_text("utf-8")
        # L'ancien format n'est pas lu : il est signalé avec quoi en faire.
        assert "Cours 0.ppt" not in lus
        assert any(a.startswith("Cours 0.ppt ignoré") and "Enregistrer sous" in a for a in avert), avert

        # Le bloc remis à Claude : le syllabus est annoncé comme tel, avec la
        # consigne sur ce qui fait foi.
        roles = {s.origine: sup.role_document(s.origine) for s in supports}
        bloc = sup.bloc_pour_claude(supports, tmp / "supports", roles=roles)
        assert sup.CONSIGNE_ROLES in bloc
        assert re.search(r"- supports/\S+ — Syllabus \(document de cours de l'enseignant", bloc), bloc
        assert re.search(r"- supports/\S+ — Diapositives de l'enseignant : texte et notes", bloc), bloc
        assert '<document type="syllabus — document Word" fichier="Syllabus.docm">' in bloc
        # Sans syllabus ni diapositives, pas de consigne inutile.
        assert sup.CONSIGNE_ROLES not in sup.bloc_pour_claude(supports, tmp / "supports")


def test_pages_d_un_pdf():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        (tmp / "une.pdf").write_bytes(b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF")
        (tmp / "deux.pdf").write_bytes(b"%PDF-1.4\n<< /Type /Page >>\n<< /Type /Page >>\n"
                                       b"<< /Type /Pages >>\n%%EOF")
        supports, _ = sup.preparer([tmp / "une.pdf", tmp / "deux.pdf"], tmp / "supports")
        detail = {s.origine: s.detail for s in supports}
        assert detail == {"une.pdf": "≈ 1 page", "deux.pdf": "≈ 2 pages"}


def test_documents_sans_place_et_sans_lecture():
    """Au-delà du budget, un document n'est plus reproduit : son début (titre,
    auteurs) l'est, pour le citer sans l'ouvrir. Sans lecture, rien n'invite
    Claude à ouvrir un fichier."""
    debut = sup.debut_texte("# PDF — nihms-987344.pdf\n\n## Page 1\nTrajectories of normal cognitive "
                            "aging\nTimothy A. Salthouse\n\nAbstract\n" + "mot " * 400, 120)
    assert debut.startswith("Trajectories of normal cognitive aging Timothy A. Salthouse Abstract")
    assert debut.endswith(" […]") and len(debut) <= 125 and "PDF" not in debut and "Page" not in debut
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        sources = []
        for i in (1, 2, 3):
            f = tmp / f"Article {i}.txt"
            f.write_text(f"Titre de l'article {i}\nAuteur {i}\n" + "texte " * 4000, encoding="utf-8")
            sources.append(f)
        supports, _ = sup.preparer(sources, tmp / "supports")
        bloc = sup.bloc_pour_claude(supports, tmp / "supports", budget=30_000)
        assert bloc.count("[… tronqué : voir le fichier complet avec Read]") == 1
        assert ('<document type="notes" fichier="Article 3.txt" extrait="début">\n'
                "Titre de l'article 3 Auteur 3 texte") in bloc
        assert "seul le début de ces documents est reproduit ; ouvre-les avec Read seulement" in bloc
        sans = sup.bloc_pour_claude(supports, tmp / "supports", budget=30_000, lecture=False)
        assert "Read" not in sans and "Ces fichiers ne sont pas à ouvrir" in sans
        assert "[… suite non reproduite]" in sans and 'extrait="début"' in sans
