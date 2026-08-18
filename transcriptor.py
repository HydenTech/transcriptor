#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Transcriptor — application Windows.

Audio d'un cours -> transcription GPU -> support de révision -> PDF.
Fenêtre native, aucun terminal, aucun WSL.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

# Sous pythonw.exe, sys.stdout et sys.stderr valent None : la moindre
# ecriture d'une bibliotheque tierce leverait une exception.
for _flux in ("stdout", "stderr"):
    if getattr(sys, _flux, None) is None:
        setattr(sys, _flux, open(os.devnull, "w", encoding="utf-8"))

APP_DIR = Path(__file__).resolve().parent
CLAUDE_MD = APP_DIR / "CLAUDE.md"
PDF_SCRIPT = APP_DIR / "scripts" / "generate_pdf.py"

DOCUMENTS = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents"
SORTIE = Path(os.environ.get("TRANSCRIPTOR_SORTIE", DOCUMENTS / "Transcriptor"))

AUDIO_EXT = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".opus", ".mp4", ".aac",
             ".wma", ".mpeg", ".mpg", ".mpga", ".mp2", ".webm", ".mkv", ".mov",
             ".avi", ".wmv", ".aiff", ".aif", ".amr", ".3gp", ".m4b", ".m4v", ".ts")
STEPS = ("transcription", "synthese", "pdf")

# Masque la fenêtre noire des sous-processus.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

_model_cache: dict[str, Any] = {}


# ----------------------------------------------------------------------
# CUDA : les DLL arrivent par pip, il faut les déclarer à Windows
# ----------------------------------------------------------------------
def dossiers_dll_nvidia() -> list[Path]:
    """Les roues pip nvidia-* rangent leurs binaires sous nvidia/<paquet>/bin
    sur Windows et nvidia/<paquet>/lib sur Linux. On balaie les deux."""
    try:
        import nvidia
    except Exception:
        return []

    trouves: list[Path] = []
    for racine in (Path(p) for p in getattr(nvidia, "__path__", [])):
        if not racine.is_dir():
            continue
        for paquet in sorted(racine.iterdir()):
            for sous in ("bin", "lib"):
                d = paquet / sous
                if d.is_dir() and (any(d.glob("*.dll")) or any(d.glob("*.so*"))):
                    trouves.append(d)
    return trouves


def enregistrer_dll_cuda() -> None:
    dossiers = dossiers_dll_nvidia()
    if not dossiers:
        return
    for d in dossiers:
        try:
            os.add_dll_directory(str(d))  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
    # CTranslate2 resout cublas64_12.dll par l'ordre de recherche standard,
    # qui ignore add_dll_directory pour ses imports implicites : le PATH,
    # lui, est toujours consulte.
    os.environ["PATH"] = (os.pathsep.join(str(d) for d in dossiers)
                          + os.pathsep + os.environ.get("PATH", ""))


def hhmm(secondes: float) -> str:
    s = int(secondes)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


# ----------------------------------------------------------------------
# État du traitement
# ----------------------------------------------------------------------
class Traitement:
    def __init__(self, audio: Path, dossier: Path, options: dict[str, Any], notifier):
        self.audio = audio
        self.dossier = dossier
        self.options = options
        self.notifier = notifier
        self.etat = "en_cours"
        self.erreur: Optional[str] = None
        self.usage: dict[str, Any] = {}
        self.fichiers: dict[str, str] = {}
        self.etapes = {n: {"statut": "attente", "avancement": 0.0, "detail": ""} for n in STEPS}
        self._dernier_envoi = 0.0

    def instantane(self) -> dict[str, Any]:
        return {
            "etat": self.etat, "erreur": self.erreur, "usage": self.usage,
            "etapes": self.etapes, "fichiers": self.fichiers,
            "nom": self.audio.stem, "dossier": str(self.dossier),
        }

    def maj(self, etape: str, *, statut: str | None = None,
            avancement: float | None = None, detail: str | None = None) -> None:
        e = self.etapes[etape]
        if statut is not None:
            e["statut"] = statut
        if avancement is not None:
            e["avancement"] = max(0.0, min(1.0, avancement))
        if detail is not None:
            e["detail"] = detail
        # Un cours de 2 h produit des milliers de segments. Sans ce frein,
        # autant d'appels evaluate_js inter-threads figent la fenetre.
        maintenant = time.time()
        if statut is None and maintenant - self._dernier_envoi < 0.4:
            return
        self._dernier_envoi = maintenant
        self.notifier(self.instantane())

    def echec(self, etape: str, message: str) -> None:
        self.etat = "erreur"
        self.erreur = message
        self.etapes[etape]["statut"] = "erreur"
        self.etapes[etape]["detail"] = message
        self.notifier(self.instantane())


# ----------------------------------------------------------------------
# Étape 1 — transcription
# ----------------------------------------------------------------------
def amorce(matiere: str, termes: str) -> str:
    matiere = matiere.strip() or "cours magistral"
    texte = f"Cours magistral de {matiere}."
    termes = termes.strip().strip(",")
    if termes:
        texte += f" Vocabulaire : {termes}."
    return texte[:900]  # Whisper tronque au-delà d'environ 224 tokens.


def charger_modele(nom: str, *, cpu: bool = False):
    cle = f"{nom}|{'cpu' if cpu else 'cuda'}"
    if cle in _model_cache:
        return _model_cache[cle]
    from faster_whisper import WhisperModel

    if not cpu:
        try:
            _model_cache[cle] = WhisperModel(nom, device="cuda", compute_type="float16")
            return _model_cache[cle]
        except Exception:
            cle = f"{nom}|cpu"
            if cle in _model_cache:
                return _model_cache[cle]
    _model_cache[cle] = WhisperModel(nom, device="cpu", compute_type="int8")
    return _model_cache[cle]


def _panne_cuda(exc: Exception) -> bool:
    """CTranslate2 ne signale l'absence des DLL qu'au premier calcul reel,
    donc bien apres la construction du modele."""
    m = str(exc).lower()
    return any(x in m for x in ("cublas", "cudnn", "cuda", "libcu", "gpu"))


def transcrire(t: Traitement) -> Path:
    try:
        return _transcrire(t, cpu=False)
    except Exception as exc:  # noqa: BLE001
        if not _panne_cuda(exc):
            raise
    t.maj("transcription", statut="en_cours", avancement=0.0,
          detail="CUDA indisponible — reprise sur le processeur")
    return _transcrire(t, cpu=True)


def _transcrire(t: Traitement, *, cpu: bool) -> Path:
    t.maj("transcription", statut="en_cours",
          detail="Chargement du modèle" + (" (processeur)" if cpu else ""))
    modele = charger_modele(t.options["modele"], cpu=cpu)

    t.maj("transcription", detail="Analyse de l'audio")
    segments, info = modele.transcribe(
        str(t.audio),
        language="fr",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=amorce(t.options["matiere"], t.options["termes"]),
    )

    total = max(float(info.duration or 0.0), 1.0)
    sortie = t.dossier / "transcription.txt"
    depart = time.time()
    minute_courante = -1
    tampon: list[str] = []

    with sortie.open("w", encoding="utf-8") as fh:
        fh.write(f"Cours : {t.options['matiere'] or 'à préciser'}\n")
        fh.write(f"Fichier : {t.audio.name}\nDurée : {hhmm(total)}\n\n")

        for seg in segments:
            minute = int(seg.start // 60)
            if minute != minute_courante:
                if tampon:
                    fh.write(" ".join(tampon).strip() + "\n\n")
                    tampon = []
                fh.write(f"[{hhmm(seg.start)}] ")
                minute_courante = minute
            tampon.append(seg.text.strip())

            part = seg.end / total
            ecoule = time.time() - depart
            reste = ecoule / part - ecoule if part > 0.02 else 0
            detail = f"{hhmm(seg.end)} / {hhmm(total)}"
            if reste > 60:
                detail += f" · reste ~{int(reste // 60)} min"
            t.maj("transcription", avancement=part, detail=detail)

        if tampon:
            fh.write(" ".join(tampon).strip() + "\n")

    mots = len(sortie.read_text(encoding="utf-8").split())
    t.fichiers["txt"] = str(sortie)
    t.maj("transcription", statut="fait", avancement=1.0,
          detail=f"{mots} mots".replace(",", " "))
    return sortie


# ----------------------------------------------------------------------
# Étape 2 — synthèse par Claude Code
# ----------------------------------------------------------------------
CONSIGNE = (
    "La transcription brute d'un cours t'est transmise sur l'entrée standard. "
    "Produis le support de révision complet en suivant strictement les instructions "
    "de CLAUDE.md. Réponds uniquement par le Markdown final, sans préambule ni "
    "commentaire, en commençant directement par la section 1."
)


def chemin_claude() -> Optional[str]:
    trouve = shutil.which("claude") or shutil.which("claude.cmd")
    if trouve:
        return trouve
    for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for nom in ("npm/claude.cmd", "npm/claude"):
            p = Path(base) / nom
            if p.exists():
                return str(p)
    return None


def synthetiser(t: Traitement, transcription: Path) -> Path:
    claude = chemin_claude()
    if not claude:
        raise RuntimeError(
            "Claude Code est introuvable. Relance Installer.bat, ou installe-le "
            "avec : npm install -g @anthropic-ai/claude-code")

    t.maj("synthese", statut="en_cours", detail="Claude lit la transcription")

    with transcription.open("rb") as stdin:
        proc = subprocess.run(
            [claude, "-p", CONSIGNE, "--output-format", "json", "--max-turns", "6"],
            stdin=stdin, capture_output=True, cwd=t.dossier,
            timeout=1800, creationflags=NO_WINDOW, shell=False,
        )

    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        if "login" in message.lower() or "auth" in message.lower():
            message = ("Claude Code n'est pas connecté. Ouvre une invite de commandes, "
                       "tape 'claude' et suis la procédure de connexion.")
        raise RuntimeError(message[:400] or "Claude Code s'est arrêté sans message.")

    brut = proc.stdout.decode("utf-8", "replace").strip()
    markdown = brut
    try:
        charge = json.loads(brut)
        markdown = charge.get("result", brut)
        u = charge.get("usage") or {}
        t.usage = {"entree": u.get("input_tokens"), "sortie": u.get("output_tokens"),
                   "duree": charge.get("duration_ms")}
    except json.JSONDecodeError:
        pass

    if not markdown.strip():
        raise RuntimeError("Claude a renvoyé une réponse vide.")

    sortie = t.dossier / f"{t.audio.stem}.md"
    sortie.write_text(markdown.strip() + "\n", encoding="utf-8")
    t.fichiers["md"] = str(sortie)
    t.maj("synthese", statut="fait", avancement=1.0, detail=f"{len(markdown.split())} mots")
    return sortie


# ----------------------------------------------------------------------
# Étape 3 — mise en page PDF
# ----------------------------------------------------------------------
def mettre_en_page(t: Traitement, markdown: Path) -> None:
    if not PDF_SCRIPT.exists():
        t.maj("pdf", statut="ignore", detail="Script de mise en page absent")
        return

    t.maj("pdf", statut="en_cours", detail="Rendu en cours")
    proc = subprocess.run(
        [sys.executable, str(PDF_SCRIPT), str(markdown)],
        capture_output=True, timeout=600, creationflags=NO_WINDOW,
    )
    pdf = markdown.with_suffix(".pdf")
    if proc.returncode != 0 or not pdf.exists():
        lignes = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        t.maj("pdf", statut="ignore",
              detail=lignes[-1][:150] if lignes else "Chromium indisponible")
        return

    t.fichiers["pdf"] = str(pdf)
    t.maj("pdf", statut="fait", avancement=1.0, detail=f"{pdf.stat().st_size / 1024:.0f} Ko")


# ----------------------------------------------------------------------
# Passerelle exposée à l'interface
# ----------------------------------------------------------------------
class Passerelle:
    def __init__(self):
        self.fenetre = None
        self.courant: Optional[Traitement] = None

    # -- appelée depuis le JavaScript ---------------------------------
    def choisir_fichier(self) -> Optional[dict[str, Any]]:
        import webview

        motifs = ("Fichiers audio (" + ";".join(f"*{e}" for e in AUDIO_EXT) + ")",
                  "Tous les fichiers (*.*)")
        resultat = self.fenetre.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=motifs)
        if not resultat:
            return None
        return self._decrire(resultat[0])

    def fichier_initial(self) -> Optional[dict[str, Any]]:
        """Audio déposé sur le raccourci du bureau ou passé en argument."""
        if len(sys.argv) > 1:
            return self._decrire(sys.argv[1])
        return None

    def _decrire(self, chemin: str) -> Optional[dict[str, Any]]:
        p = Path(chemin)
        if not p.is_file():
            return {"erreur": "Fichier introuvable."}
        # FFmpeg lit bien plus de formats que la liste ci-dessus : on laisse
        # passer, c'est le decodage qui tranchera avec un message clair.
        if p.suffix.lower() not in AUDIO_EXT and p.stat().st_size < 4096:
            return {"erreur": f"Fichier {p.suffix or 'sans extension'} trop petit "
                              "pour un enregistrement audio."}
        return {"chemin": str(p), "nom": p.name,
                "taille": round(p.stat().st_size / 1048576)}

    def lancer(self, options: dict[str, Any]) -> dict[str, Any]:
        audio = Path(options.get("chemin", ""))
        if not audio.exists():
            return {"erreur": "Fichier introuvable."}

        dossier = SORTIE / f"{time.strftime('%Y-%m-%d')}_{audio.stem[:60]}"
        n = 2
        while dossier.exists():
            dossier = dossier.with_name(f"{dossier.name.rstrip('0123456789_')}_{n}")
            n += 1
        dossier.mkdir(parents=True, exist_ok=True)
        if CLAUDE_MD.exists():
            shutil.copy(CLAUDE_MD, dossier / "CLAUDE.md")

        self.courant = Traitement(audio, dossier, options, self._pousser)
        threading.Thread(target=self._pipeline, args=(self.courant,), daemon=True).start()
        return self.courant.instantane()

    def lire(self, genre: str) -> str:
        if not self.courant:
            return ""
        chemin = self.courant.fichiers.get(genre)
        if not chemin or not Path(chemin).exists():
            return ""
        return Path(chemin).read_text(encoding="utf-8")

    def ouvrir(self, cible: str) -> None:
        if not self.courant:
            return
        chemin = self.courant.fichiers.get(cible) or str(self.courant.dossier)
        try:
            os.startfile(chemin)  # noqa: S606
        except Exception:
            subprocess.Popen(["explorer", chemin], creationflags=NO_WINDOW)

    # -- interne -------------------------------------------------------
    def _pousser(self, instantane: dict[str, Any]) -> None:
        if not self.fenetre:
            return
        charge = json.dumps(instantane, ensure_ascii=False)
        try:
            self.fenetre.evaluate_js(f"window.majEtat({charge})")
        except Exception:
            pass

    def _pipeline(self, t: Traitement) -> None:
        try:
            transcription = transcrire(t)
        except Exception as exc:  # noqa: BLE001
            t.echec("transcription", _lisible(exc))
            return
        try:
            markdown = synthetiser(t, transcription)
        except Exception as exc:  # noqa: BLE001
            t.echec("synthese", _lisible(exc))
            return
        try:
            mettre_en_page(t, markdown)
        except Exception as exc:  # noqa: BLE001
            t.maj("pdf", statut="ignore", detail=_lisible(exc)[:150])

        t.etat = "fini"
        t.notifier(t.instantane())


def _lisible(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    bas = message.lower()
    if "invalid data" in bas or "moov atom" in bas or "does not contain" in bas \
            or "no such file" in bas or "av." in bas.split(":")[0]:
        message = ("Ce fichier n'a pas pu etre decode. Convertis-le en MP3 ou WAV, "
                   "puis relance. (" + message[:120] + ")")
    if "cublas" in bas or "cudnn" in bas:
        message += (" — les bibliothèques CUDA de pip sont introuvables. "
                    "Réinstalle-les : pip install --force-reinstall "
                    "nvidia-cublas-cu12 \"nvidia-cudnn-cu12>=9,<10\"")
    elif "cuda" in bas:
        message += (" — la transcription bascule sur le processeur, "
                    "vérifie le pilote NVIDIA si c'est trop lent.")
    return message[:400]


def main() -> None:
    enregistrer_dll_cuda()
    SORTIE.mkdir(parents=True, exist_ok=True)

    import webview

    passerelle = Passerelle()
    passerelle.fenetre = webview.create_window(
        "Transcriptor",
        str(APP_DIR / "static" / "index.html"),
        js_api=passerelle,
        width=980, height=860, min_size=(560, 620),
        background_color="#EDEFE9",
    )
    webview.start()

    # WebView2 verrouille son dossier de donnees. Un processus fantome laisse
    # apres la fermeture empeche le lancement suivant : la fenetre s'ouvre
    # puis se fige. On coupe net plutot que d'attendre les threads restants.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        rapport = SORTIE / "erreur.txt"
        rapport.parent.mkdir(parents=True, exist_ok=True)
        rapport.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, f"Transcriptor n'a pas pu démarrer.\n\nDétail : {rapport}",
                "Transcriptor", 0x10)
        except Exception:
            raise
