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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
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
ICONE = APP_DIR / "static" / "transcriptor.ico"

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
# Cycle de vie du processus
# ----------------------------------------------------------------------
BASE_LOCALE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Transcriptor"
JOURNAL = BASE_LOCALE / "journal.log"

_enfants: list[subprocess.Popen] = []
_verrou_enfants = threading.Lock()


def noter(message: str) -> None:
    """Trace horodatée : sous pythonw.exe, c'est le seul témoin qu'on ait."""
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        if JOURNAL.exists() and JOURNAL.stat().st_size > 512_000:
            JOURNAL.replace(JOURNAL.with_suffix(".log.1"))
        with JOURNAL.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {message}\n")
    except Exception:
        pass


def suivre(proc: subprocess.Popen) -> subprocess.Popen:
    """Enregistre un sous-processus pour pouvoir le tuer à la fermeture."""
    with _verrou_enfants:
        _enfants.append(proc)
    return proc


def tuer_enfants() -> None:
    with _verrou_enfants:
        enfants, _enfants[:] = list(_enfants), []
    for proc in enfants:
        if proc.poll() is not None:
            continue
        noter(f"arrêt du sous-processus {proc.pid}")
        try:
            if os.name == "nt":
                # claude.cmd lance node, playwright lance chromium :
                # sans /T les petits-enfants survivent au parent.
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, timeout=15,
                               creationflags=NO_WINDOW)
            else:
                proc.kill()
        except Exception as exc:  # noqa: BLE001
            noter(f"échec de l'arrêt de {proc.pid} : {exc}")


def arreter(code: int = 0) -> None:
    """Sortie franche. Les threads WinForms de pywebview ne sont pas des
    threads démons : sans cela le processus survit à sa propre fenêtre."""
    noter("fermeture demandée")
    tuer_enfants()
    noter("processus terminé")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def identifier_application() -> None:
    """Sans identite propre, Windows range la fenetre sous pythonw.exe :
    icone generique dans la barre des taches et epinglage impossible."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Hyden.Transcriptor")
    except Exception as exc:  # noqa: BLE001
        noter(f"identité d'application non posée : {exc}")


def poser_icone(titre: str = "Transcriptor", patience: float = 15.0) -> None:
    """pywebview ne pose pas d'icone sur Windows. On attend que la fenetre
    existe, puis on la lui envoie directement."""
    if os.name != "nt" or not ICONE.exists():
        return
    import ctypes

    u32 = ctypes.windll.user32
    u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    u32.FindWindowW.restype = ctypes.c_void_p
    u32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u32.LoadImageW.restype = ctypes.c_void_p
    u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                 ctypes.c_void_p, ctypes.c_void_p]
    u32.SendMessageW.restype = ctypes.c_void_p

    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1

    limite = time.time() + patience
    hwnd = None
    while time.time() < limite:
        hwnd = u32.FindWindowW(None, titre)
        if hwnd:
            break
        time.sleep(0.15)
    if not hwnd:
        noter("fenêtre introuvable, icône non posée")
        return

    for taille, lequel in ((16, ICON_SMALL), (32, ICON_BIG)):
        h = u32.LoadImageW(None, str(ICONE), IMAGE_ICON, taille, taille,
                           LR_LOADFROMFILE)
        if h:
            u32.SendMessageW(hwnd, WM_SETICON, ctypes.c_void_p(lequel),
                             ctypes.c_void_p(h))
    noter("icône posée")


def preparer_webview2() -> None:
    """Un profil WebView2 neuf à chaque lancement. Un processus fantôme ne
    peut donc plus verrouiller le profil de la session suivante — c'est ce
    verrou qui faisait s'ouvrir puis se figer la deuxième fenêtre."""
    if os.name != "nt":
        return
    base = BASE_LOCALE / "webview2"
    try:
        base.mkdir(parents=True, exist_ok=True)
        for vieux in base.glob("session-*"):
            if time.time() - vieux.stat().st_mtime > 6 * 3600:
                shutil.rmtree(vieux, ignore_errors=True)
    except Exception:
        pass
    try:
        profil = Path(tempfile.mkdtemp(prefix=f"session-{os.getpid()}-", dir=base))
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(profil)
        noter(f"profil WebView2 : {profil}")
    except Exception as exc:  # noqa: BLE001
        noter(f"profil WebView2 par défaut ({exc})")


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

# Sections de sortie attendues. Elles ne sont pas figées ici : elles sont
# relues dans CLAUDE.md à chaque lancement, pour que modifier CLAUDE.md
# suffise à changer la sortie sans toucher au code.
SECTIONS_DEFAUT = {
    1: "En-tête", 2: "Résumé exécutif", 3: "Plan du cours",
    4: "Points clés", 5: "Fiches de révision", 6: "Signaux examen",
    7: "Informations pratiques", 8: "Lexique", 9: "Auto-évaluation",
    10: "Zones d'ombre",
}

_RE_TITRE = re.compile(r"^#{1,4}[ \t]*(.+?)[ \t]*$", re.M)
_RE_NUMERO = re.compile(r"^(\d{1,2})[.)]")
_RE_SECTION_CONSIGNE = re.compile(r"^#{2,4}[ \t]*(\d{1,2})[.)][ \t]*(.+?)[ \t]*$", re.M)


def _sans_accent(texte: str) -> str:
    plat = unicodedata.normalize("NFD", texte.lower())
    return "".join(c for c in plat if unicodedata.category(c) != "Mn")


class Jalons:
    """Les titres de sections de CLAUDE.md, servant de repères d'avancement
    pendant que Claude rédige."""

    def __init__(self, sections: dict[int, str]):
        self.noms = sections or dict(SECTIONS_DEFAUT)
        self.total = max(self.noms)
        self._plats = {n: _sans_accent(v) for n, v in self.noms.items()}

    @classmethod
    def depuis(cls, *candidats: Path) -> "Jalons":
        for chemin in candidats:
            try:
                texte = chemin.read_text(encoding="utf-8")
            except Exception:
                continue
            trouve: dict[int, str] = {}
            for m in _RE_SECTION_CONSIGNE.finditer(texte):
                n = int(m.group(1))
                if 1 <= n <= 40:
                    trouve.setdefault(n, m.group(2))
            if len(trouve) >= 3:
                return cls(trouve)
        return cls(dict(SECTIONS_DEFAUT))

    def reperer(self, texte: str) -> int:
        """Numéro de la section la plus avancée présente dans un fragment."""
        vu = 0
        for titre in _RE_TITRE.findall(texte):
            m = _RE_NUMERO.match(titre)
            if m and 1 <= int(m.group(1)) <= self.total:
                vu = max(vu, int(m.group(1)))
                continue
            plat = _sans_accent(titre)
            for n, nom in self._plats.items():
                if nom and nom in plat:
                    vu = max(vu, n)
        return vu

    def etiquette(self, n: int) -> str:
        return f"Section {n}/{self.total} · {self.noms.get(n, '')}".rstrip(" ·")


def _fragment(ev: dict) -> str:
    """Texte écrit par Claude dans un événement du flux JSON."""
    genre = ev.get("type")
    if genre == "stream_event":
        e = ev.get("event") or {}
        if e.get("type") == "content_block_delta":
            d = e.get("delta") or {}
            if d.get("type") == "text_delta":
                return d.get("text") or ""
        return ""
    if genre == "assistant":
        blocs = (ev.get("message") or {}).get("content") or []
        return "".join(b.get("text") or "" for b in blocs
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


class _OptionInconnue(RuntimeError):
    """La version de Claude Code installée ne connaît pas ce drapeau."""


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


def _appeler_claude(t: Traitement, commande: list[str], transcription: Path,
                    jalons: Jalons) -> tuple[str, dict[str, Any]]:
    debut = time.time()
    with transcription.open("rb") as stdin:
        proc = suivre(subprocess.Popen(
            commande, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=t.dossier, creationflags=NO_WINDOW, shell=False,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        ))

    minuterie = threading.Timer(1800, proc.kill)
    minuterie.daemon = True
    minuterie.start()

    lignes: list[str] = []
    resultat: Optional[dict[str, Any]] = None
    partiel = ""
    section = 0

    try:
        for ligne in proc.stdout:  # type: ignore[union-attr]
            ligne = ligne.strip()
            if not ligne:
                continue
            lignes.append(ligne)
            try:
                ev = json.loads(ligne)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "result":
                resultat = ev

            morceau = _fragment(ev)
            if not morceau:
                continue
            partiel += morceau
            if "\n" in partiel:
                complet, _, partiel = partiel.rpartition("\n")
                section = max(section, jalons.reperer(complet))
            ecoule = int(time.time() - debut)
            t.maj("synthese",
                  avancement=section / jalons.total,
                  detail=(jalons.etiquette(section) if section
                          else f"Rédaction… {ecoule} s"))
    finally:
        minuterie.cancel()
        erreurs = proc.stderr.read() if proc.stderr else ""  # type: ignore[union-attr]
        proc.wait()

    if proc.returncode != 0:
        message = (erreurs or "\n".join(lignes)).strip()
        if re.search(r"unknown (option|argument)|unrecognized|--\w[\w-]* is not",
                     message, re.I):
            raise _OptionInconnue(message)
        if "login" in message.lower() or "auth" in message.lower():
            message = ("Claude Code n'est pas connecté. Ouvre une invite de commandes, "
                       "tape 'claude' et suis la procédure de connexion.")
        raise RuntimeError(message[:400] or "Claude Code s'est arrêté sans message.")

    if resultat is None:  # sortie JSON d'un seul bloc, non streamée
        try:
            charge = json.loads("\n".join(lignes))
            resultat = charge if isinstance(charge, dict) else None
        except json.JSONDecodeError:
            resultat = None

    if resultat is None:
        return "\n".join(lignes), {}

    markdown = resultat.get("result") or ""
    u = resultat.get("usage") or {}
    usage = {"entree": u.get("input_tokens"), "sortie": u.get("output_tokens"),
             "duree": resultat.get("duration_ms")}
    return markdown, usage


def synthetiser(t: Traitement, transcription: Path) -> Path:
    claude = chemin_claude()
    if not claude:
        raise RuntimeError(
            "Claude Code est introuvable. Relance Installer.bat, ou installe-le "
            "avec : npm install -g @anthropic-ai/claude-code")

    t.maj("synthese", statut="en_cours", detail="Claude lit la transcription")

    # La copie posée dans le dossier du cours fait foi ; le fichier de
    # l'application sert de secours.
    jalons = Jalons.depuis(t.dossier / "CLAUDE.md", CLAUDE_MD)

    base = [claude, "-p", CONSIGNE, "--max-turns", "6"]
    # Du plus bavard au plus sobre : chaque repli perd un peu d'avancement
    # mais reste fonctionnel sur une version plus ancienne du CLI.
    tentatives = [
        base + ["--output-format", "stream-json", "--verbose",
                "--include-partial-messages"],
        base + ["--output-format", "stream-json", "--verbose"],
        base + ["--output-format", "json"],
    ]

    derniere: Optional[Exception] = None
    for commande in tentatives:
        try:
            markdown, usage = _appeler_claude(t, commande, transcription, jalons)
            break
        except _OptionInconnue as exc:
            derniere = exc
    else:
        raise RuntimeError(str(derniere)[:400] if derniere else
                           "Claude Code n'a accepté aucun format de sortie.")

    if not markdown.strip():
        raise RuntimeError("Claude a renvoyé une réponse vide.")

    t.usage = usage
    sortie = t.dossier / f"{t.audio.stem}.md"
    sortie.write_text(markdown.strip() + "\n", encoding="utf-8")
    t.fichiers["md"] = str(sortie)
    t.maj("synthese", statut="fait", avancement=1.0,
          detail=f"{len(markdown.split())} mots")
    return sortie


# ----------------------------------------------------------------------
# Étape 3 — mise en page PDF
# ----------------------------------------------------------------------
def mettre_en_page(t: Traitement, markdown: Path) -> None:
    if not PDF_SCRIPT.exists():
        t.maj("pdf", statut="ignore", detail="Script de mise en page absent")
        return

    t.maj("pdf", statut="en_cours", detail="Rendu en cours")
    proc = suivre(subprocess.Popen(
        [sys.executable, str(PDF_SCRIPT), str(markdown)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW,
    ))
    try:
        _, err = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    pdf = markdown.with_suffix(".pdf")
    if proc.returncode != 0 or not pdf.exists():
        lignes = (err or b"").decode("utf-8", "replace").strip().splitlines()
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
    noter("démarrage")
    identifier_application()
    enregistrer_dll_cuda()
    SORTIE.mkdir(parents=True, exist_ok=True)
    preparer_webview2()

    import webview

    noter(f"pywebview {getattr(webview, '__version__', '?')}")
    passerelle = Passerelle()
    fenetre = webview.create_window(
        "Transcriptor",
        str(APP_DIR / "static" / "index.html"),
        js_api=passerelle,
        width=980, height=860, min_size=(560, 620),
        background_color="#EDEFE9",
    )
    passerelle.fenetre = fenetre

    # Filet de sécurité : si webview.start() ne rend jamais la main — ça
    # arrive — la fermeture de la fenêtre coupe quand même le processus.
    try:
        fenetre.events.closed += lambda: arreter(0)
    except Exception as exc:  # noqa: BLE001
        noter(f"événement closed indisponible : {exc}")

    # La fenetre n'existe pas encore : un guetteur la reconnaitra a son titre.
    threading.Thread(target=poser_icone, daemon=True).start()

    noter("fenêtre créée, ouverture")
    webview.start()
    noter("webview.start() a rendu la main")

    arreter(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        noter("échec au démarrage :\n" + traceback.format_exc())
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
