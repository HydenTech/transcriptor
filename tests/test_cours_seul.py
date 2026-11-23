# -*- coding: utf-8 -*-
"""Le mode « un cours » après le passage au mode année : même parcours
qu'avant (supports, transcription, synthèse, PDF), Whisper et Claude simulés."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
from simulation import preparer_environnement  # noqa: E402


def test_cours_seul():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        (tmp / "h.json").write_text("{}", encoding="utf-8")
        preparer_environnement(tmp / "env", horaire_json=tmp / "h.json")
        sys.modules.pop("transcriptor", None)
        sys.path.insert(0, str(ICI.parent))
        import transcriptor as tr
        tr.installer_modeles()
        audio = tmp / "algo seance 4.mp3"
        audio.write_bytes(b"\0" * 8192)
        notes = tmp / "mes notes.txt"
        notes.write_text("Notes personnelles : tri rapide, pivot.", encoding="utf-8")
        p = tr.Passerelle()
        r = p.lancer({"chemin": str(audio), "matiere": "Algorithmique", "termes": "QuickSort",
                      "modele": "large-v3", "consignes": "Cours complet", "supports": [str(notes)]})
        assert not r.get("erreur"), r
        fin = time.time() + 120
        while p._courant.etat == "en_cours" and time.time() < fin:
            time.sleep(0.1)
        e = p._courant.instantane()
        assert e["etat"] == "fini", e
        assert e["etapes"]["transcription"]["statut"] == "fait"
        assert e["etapes"]["synthese"]["statut"] == "fait"
        assert e["etapes"]["supports"]["statut"] == "fait"
        dossier = Path(e["dossier"])
        txt = (dossier / "transcription.txt").read_text(encoding="utf-8")
        assert txt.startswith("Cours : Algorithmique\nFichier : algo seance 4.mp3")
        assert not list(dossier.glob("*.partiel"))
        assert (dossier / "algo seance 4.md").is_file()
        appel = json.loads((tmp / "env" / "appels_claude.jsonl").read_text("utf-8").splitlines()[-1])
        assert "-p" in appel["args"] and "--allowedTools" not in appel["args"]   # notes : pas de Read


if __name__ == "__main__":
    test_cours_seul()
    print("ok cours seul")
