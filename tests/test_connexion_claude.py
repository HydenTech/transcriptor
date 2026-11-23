# -*- coding: utf-8 -*-
"""Claude Code non connecté au compte Claude : détection du message, bouton
« Se connecter à Claude » proposé (mode cours et mode année), fenêtre de
connexion, état relu par « claude auth status ». Claude simulé."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
from simulation import attendre, preparer_environnement  # noqa: E402
from test_annee_bout_en_bout import _annee, _environnement  # noqa: E402

CONNEXION = [
    "Not logged in · Please run /login",
    "Failed to authenticate. API Error: 401 OAuth token has expired. Please obtain a new token",
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "Invalid API key · Fix external API key",
    "Login expired · Please run /login",
    "OAuth token revoked · Please run /login",
    "Please run /login · API Error: 401 Invalid authentication credentials",
]
AUTRES = [
    'API Error: 529 {"type":"error","error":{"type":"overloaded_error"},"request_id":"req_011CV401abc"}',
    "Claude usage limit reached. Your limit will reset at 5pm.",
    "There's an issue with the selected model (opus). It may not exist or you may not have access to it.",
    "The catalog includes the dialog in chapter 3.",
    "error_max_turns",
]


def _tr(tmp: Path):
    (tmp / "h.json").write_text("{}", encoding="utf-8")
    preparer_environnement(tmp / "env", horaire_json=tmp / "h.json")
    sys.modules.pop("transcriptor", None)
    sys.path.insert(0, str(ICI.parent))
    import transcriptor
    return transcriptor


def test_messages_et_script():
    with tempfile.TemporaryDirectory() as t:
        tr = _tr(Path(t))
        for m in CONNEXION:
            assert tr._RE_CONNEXION.search(m), m
            texte = tr._expliquer(m, 1)
            assert "Se connecter à Claude" in texte and m[:40] in texte, texte
        for m in AUTRES:
            assert not tr._RE_CONNEXION.search(m), m
            assert "Se connecter" not in tr._expliquer(m, 1), m
        assert not tr._RE_CONNEXION_FORTE.search("TERMINÉ : 4 fichiers, 401 mots écrits.")
        for m in CONNEXION[:6]:
            assert tr._RE_CONNEXION_FORTE.search(m), m
        assert "surchargé" in tr._expliquer(AUTRES[0], 1)
        assert "Limite d'utilisation" in tr._expliquer(AUTRES[1], 1)

        # Une clé dans l'environnement passe avant la connexion en mode -p.
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        try:
            assert "ANTHROPIC_API_KEY" in tr._expliquer(CONNEXION[0], 1)
            cx = {"connu": True, "connecte": True, "methode": "claude.ai"}
            assert "ANTHROPIC_API_KEY" in tr.texte_connexion(cx)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY")

        script = tr.script_connexion(r"C:\Users\X\AppData\Roaming\npm\claude.cmd")
        assert script.startswith("@echo off\r\nchcp 65001 >nul\r\n")
        assert "\n" not in script.replace("\r\n", "")          # CRLF partout
        assert r'call "C:\Users\X\AppData\Roaming\npm\claude.cmd" auth login' in script
        assert "auth status --text" in script and script.rstrip().endswith("pause")
        for ligne in script.split("\r\n"):                        # pas de bloc cmd fragile
            if ligne.startswith("echo"):
                assert not set(ligne) & set("()<>|&^%"), ligne


def test_etat_de_connexion():
    with tempfile.TemporaryDirectory() as t:
        tr = _tr(Path(t))
        claude = tr.chemin_claude()
        assert tr.connexion_claude(claude) == {"connu": True, "connecte": True, "methode": "claude.ai"}
        os.environ["FAUX_CLAUDE_CONNEXION"] = "non"
        try:
            cx = tr.connexion_claude(claude)
            assert cx["connu"] and not cx["connecte"]
            tr._ETAT_CLAUDE.update(etat="ok", texte="Claude Code 9.9.9 · à jour.",
                                   base="Claude Code 9.9.9 · à jour", etat_base="ok")
            e = tr.verifier_connexion()
            assert e["connexion"] == "non" and e["etat"] == "connexion", e
            assert e["texte"] == "Claude Code 9.9.9 · à jour · non connecté à ton compte Claude.", e
        finally:
            os.environ.pop("FAUX_CLAUDE_CONNEXION")
        e2 = tr.verifier_connexion()
        assert e2["connexion"] == "oui" and e2["etat"] == "ok", e2
        assert e2["texte"].endswith("· connecté à ton abonnement Claude."), e2
        assert int(e2["verifie"]) == int(e["verifie"]) + 1
        # Hors Windows, pas de console : un message à la place.
        assert tr.ouvrir_connexion_claude().get("erreur")


def test_cours_non_connecte():
    with tempfile.TemporaryDirectory() as t:
        tr = _tr(Path(t))
        tr.installer_modeles()
        audio = Path(t) / "cours.mp3"
        audio.write_bytes(b"\0" * 8192)
        p = tr.Passerelle()
        os.environ["FAUX_CLAUDE_MODE"] = "connexion"
        try:
            r = p.lancer({"chemin": str(audio), "matiere": "Algorithmique", "termes": "",
                          "modele": "large-v3", "consignes": "Cours complet"})
            assert not r.get("erreur"), r
            fin = time.time() + 120
            while p._courant.etat == "en_cours" and time.time() < fin:
                time.sleep(0.1)
        finally:
            os.environ.pop("FAUX_CLAUDE_MODE")
        e = p._courant.instantane()
        assert e["etat"] == "erreur" and e["connexion"] is True, e
        assert "Se connecter à Claude" in e["erreur"] and "401" in e["erreur"], e["erreur"]
        # Une fois connecté, « Relancer la synthèse » repart sans l'erreur.
        r = p.relancer_synthese()
        assert r["connexion"] is False
        fin = time.time() + 120
        while p._courant.etat == "en_cours" and time.time() < fin:
            time.sleep(0.1)
        assert p._courant.instantane()["etat"] == "fini"


def test_annee_non_connectee():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        tr = _environnement(tmp)
        racine = _annee(tmp)
        p = tr.Passerelle()
        p.ouvrir_annee(str(racine))

        # Lecture de l'horaire : échec de connexion, bouton proposé.
        os.environ["FAUX_CLAUDE_MODE"] = "connexion"
        try:
            p.lire_horaire_annee({})
            attendre(p._chantier)
            c = p._chantier.instantane()
            assert c["etat"] == "erreur" and c["connexion"] is True, c
            assert "Se connecter à Claude" in c["erreur"], c["erreur"]
        finally:
            os.environ.pop("FAUX_CLAUDE_MODE")

        # Connecté : l'horaire se lit, puis « Tout traiter » sans connexion :
        # les transcriptions continuent, une seule étape Claude échoue.
        p.lire_horaire_annee({})
        attendre(p._chantier)
        assert p._chantier.instantane()["connexion"] is False
        p.preparer_dossiers_annee()
        os.environ["FAUX_CLAUDE_MODE"] = "connexion"
        try:
            p.lancer_annee({"modele": "large-v3"})
            attendre(p._chantier, 300)
        finally:
            os.environ.pop("FAUX_CLAUDE_MODE")
        c = p._chantier.instantane()
        assert c["connexion"] is True
        taches = c["taches"]
        assert all(x["statut"] == "fait" for x in taches if x["genre"] == "transcription"), taches
        claude = [x for x in taches if x["genre"] != "transcription"]
        assert sum(1 for x in claude if x["statut"] == "erreur") == 1, claude
        assert all(x["statut"] in ("erreur", "ignore") for x in claude), claude
        assert any("non connecté" in (x["detail"] or "") for x in claude), claude


if __name__ == "__main__":
    test_messages_et_script()
    test_etat_de_connexion()
    test_cours_non_connecte()
    test_annee_non_connectee()
    print("ok connexion")
