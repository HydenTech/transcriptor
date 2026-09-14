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


def brancher_journal() -> None:
    """Renvoie les logs des bibliothèques vers le journal. pywebview signale
    ses erreurs par logging.exception : sous pythonw, sans destinataire,
    elles disparaissaient purement et simplement."""
    import logging

    class _VersJournal(logging.Handler):
        def emit(self, record: "logging.LogRecord") -> None:
            try:
                noter(f"[{record.name}] {self.format(record)}")
            except Exception:
                pass

    poste = _VersJournal()
    poste.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    racine = logging.getLogger()
    racine.addHandler(poste)
    racine.setLevel(logging.WARNING)
    logging.getLogger("pywebview").setLevel(logging.DEBUG)


def suivre(proc: subprocess.Popen) -> subprocess.Popen:
    """Enregistre un sous-processus pour pouvoir le tuer à la fermeture."""
    with _verrou_enfants:
        _enfants.append(proc)
    return proc


def tuer(proc: subprocess.Popen, motif: str = "") -> None:
    """Arrête un sous-processus ET sa descendance.

    Un simple proc.kill() ne suffit pas sur Windows : claude.cmd n'est qu'un
    wrapper, c'est node qui travaille ; playwright lance chromium. Sans /T,
    les petits-enfants survivent — et continuent d'écrire dans nos tuyaux.
    """
    if proc.poll() is not None:
        return
    noter(f"arrêt du sous-processus {proc.pid}" + (f" ({motif})" if motif else ""))
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
        else:
            proc.kill()
    except Exception as exc:  # noqa: BLE001
        noter(f"échec de l'arrêt de {proc.pid} : {exc}")


def tuer_enfants() -> None:
    with _verrou_enfants:
        enfants, _enfants[:] = list(_enfants), []
    for proc in enfants:
        tuer(proc, "fermeture")


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


# Handle de la fenetre principale, releve une fois qu'elle existe.
_HANDLE_FENETRE: dict[str, Any] = {}


# Masques du filtre de la boîte « Ouvrir ».
def _filtre_audio() -> list[tuple[str, str]]:
    return [("Fichiers audio", ";".join(f"*{e}" for e in AUDIO_EXT)),
            ("Transcription déjà produite", "*.txt"),
            ("Tous les fichiers", "*.*")]


def choisir_fichier_natif(titre: str = "Choisir l'enregistrement") -> Optional[str]:
    """Boîte « Ouvrir » de Windows, affichée sur un thread STA dédié.

    pywebview exécute les appels venus du JavaScript sur un thread Python
    ordinaire — donc MTA côté .NET — puis y ouvre WinForms.OpenFileDialog en
    lui passant un formulaire appartenant à un autre thread. Double faute :
    violation d'apartment COM et accès inter-thread à un contrôle. C'est ce
    qui fait tomber l'application au clic sur « choisir l'enregistrement ».
    On ouvre donc la boîte nous-mêmes, sur un thread correctement initialisé.

    Renvoie le chemin choisi, None si annulé, et lève RuntimeError si la
    boîte n'a pas pu s'ouvrir — l'appelant retombera alors sur pywebview.
    """
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID), ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", wintypes.LPVOID), ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    filtre = "".join(f"{nom}\0{masque}\0" for nom, masque in _filtre_audio()) + "\0"
    tampon = ctypes.create_unicode_buffer(8192)
    issue: dict[str, Any] = {}

    OFN = 0x0008_180C  # EXPLORER | FILEMUSTEXIST | PATHMUSTEXIST | NOCHANGEDIR | HIDEREADONLY
    COINIT_APARTMENTTHREADED = 0x2

    def _montrer() -> None:
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        try:
            ofn = OPENFILENAMEW()
            ofn.lStructSize = ctypes.sizeof(ofn)
            # Sans propriétaire, la boîte peut s'ouvrir derrière la fenêtre :
            # l'utilisateur clique et croit que rien ne se passe.
            proprietaire = _HANDLE_FENETRE.get("hwnd")
            if proprietaire:
                ofn.hwndOwner = proprietaire
            ofn.lpstrFilter = filtre
            ofn.lpstrFile = ctypes.cast(tampon, wintypes.LPWSTR)
            ofn.nMaxFile = len(tampon)
            ofn.lpstrTitle = titre
            ofn.Flags = OFN
            if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
                issue["chemin"] = tampon.value
            else:
                code = ctypes.windll.comdlg32.CommDlgExtendedError()
                if code:                       # 0 = simple annulation
                    issue["erreur"] = f"CommDlgExtendedError 0x{code:04X}"
        except Exception as exc:  # noqa: BLE001
            issue["erreur"] = str(exc)
        finally:
            ole32.CoUninitialize()

    fil = threading.Thread(target=_montrer, daemon=True)
    fil.start()
    fil.join(600)
    if fil.is_alive():
        raise RuntimeError("la boîte de dialogue ne répond pas")
    if "erreur" in issue:
        raise RuntimeError(issue["erreur"])
    return issue.get("chemin")


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
    _HANDLE_FENETRE["hwnd"] = hwnd

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
    "de CLAUDE.md. Traite la transcription en entier, en une seule réponse : "
    "personne ne pourra te dire de continuer. Réponds uniquement par le Markdown "
    "final, sans préambule ni commentaire, en commençant directement par la section 1."
)

# Garde-fous de l'appel à Claude. L'ancien couperet fixe (30 min) tombait en
# pleine rédaction d'un cours long — et ne tuait que claude.cmd, pas node.
INACTIVITE_MAX = 20 * 60   # plus aucune ligne reçue depuis ce délai : on coupe
DUREE_MAX = 120 * 60       # plafond absolu, quoi qu'il arrive
JOURNAL_SYNTHESE = "synthese.log"      # dans le dossier du cours
PARTIEL_SYNTHESE = "synthese_partielle.md"

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


class _CliObsolete(RuntimeError):
    """Claude Code refuse le modèle : une version plus récente est exigée."""


# « API Error: 400 Claude Code 2.1.234 does not support this model; version
# 2.1.251 or newer is required. Run 'claude update' … »
_RE_CLI_OBSOLETE = re.compile(
    r"does not support this model|version [\d.]+ or newer is required|"
    r"run 'claude update'|please update claude code", re.I)


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


def _lancer_utilitaire(commande: list[str], delai: int) -> tuple[int, str]:
    """Exécute une commande silencieuse (pas de fenêtre) et renvoie
    (code, sortie stdout+stderr)."""
    try:
        proc = suivre(subprocess.Popen(
            commande, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
            text=True, encoding="utf-8", errors="replace"))
        try:
            sortie, _ = proc.communicate(timeout=delai)
        except subprocess.TimeoutExpired:
            tuer(proc, "délai dépassé")
            sortie, _ = proc.communicate()
            return -1, (sortie or "") + "\n[délai dépassé]"
        return proc.returncode, sortie or ""
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def version_claude(claude: str) -> str:
    code, sortie = _lancer_utilitaire([claude, "--version"], 60)
    m = re.search(r"\d+\.\d+\.\d+", sortie)
    return m.group(0) if (code == 0 and m) else ""


def mettre_a_jour_claude(claude: str, signaler=None) -> str:
    """Met Claude Code à jour et renvoie la version obtenue ('' si inconnue).

    Le CLI ne se met à jour tout seul qu'en session interactive ; ici il
    n'est jamais lancé qu'en headless par l'application, donc il vieillit
    jusqu'à ce que l'API refuse le modèle. `claude update` couvre les
    installations natives et npm ; en cas d'échec, npm est tenté directement.
    """
    avant = version_claude(claude)
    noter(f"claude : mise à jour (version actuelle {avant or '?'})")
    if signaler:
        signaler(f"Mise à jour de Claude Code ({avant or '?'})…")

    code, sortie = _lancer_utilitaire([claude, "update"], 600)
    noter(f"claude update : code {code} — {sortie.strip()[-300:]}")
    apres = version_claude(claude)
    if apres and apres != avant:
        noter(f"claude : {avant or '?'} -> {apres}")
        return apres

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm:
        code, sortie = _lancer_utilitaire(
            [npm, "install", "-g", "@anthropic-ai/claude-code@latest"], 900)
        noter(f"npm install claude-code : code {code} — {sortie.strip()[-300:]}")
        apres = version_claude(claude)
        if apres and apres != avant:
            noter(f"claude : {avant or '?'} -> {apres} (npm)")
            return apres
    return apres if apres != avant else ""


_MAJ_CLAUDE_FINIE = threading.Event()
_MAJ_CLAUDE_FINIE.set()   # levé par défaut : seul main() l'abaisse, le temps de la vérification
# État affiché dans l'interface : {"etat": "maj" | "ok" | "echec" | "absent", "texte": …}
_ETAT_CLAUDE: dict[str, str] = {"etat": "inconnu", "texte": ""}


def maj_claude_en_arriere_plan(signaler=None) -> None:
    """Au lancement, sans bloquer : le CLI est à jour avant que la
    transcription (une dizaine de minutes) ne laisse la main à la synthèse.
    synthetiser() attend la fin de cette étape avant de lancer claude, pour
    ne pas exécuter un binaire en cours de remplacement."""

    def poser(etat: str, texte: str) -> None:
        _ETAT_CLAUDE.update(etat=etat, texte=texte)
        noter(f"claude (démarrage) : {texte}")
        if signaler:
            try:
                signaler(dict(_ETAT_CLAUDE))
            except Exception:
                pass

    try:
        claude = chemin_claude()
        if not claude:
            poser("absent", "Claude Code introuvable — relance Installer.bat.")
            return
        avant = version_claude(claude)
        poser("maj", f"Claude Code {avant or ''} : recherche de mise à jour…".replace("  ", " "))
        code, sortie = _lancer_utilitaire([claude, "update"], 600)
        noter(f"claude update (démarrage) : code {code} — {sortie.strip()[-200:]}")
        apres = version_claude(claude)
        if apres and avant and apres != avant:
            poser("ok", f"Claude Code mis à jour : {avant} → {apres}.")
        elif code == 0 and apres:
            poser("ok", f"Claude Code {apres} · à jour.")
        else:
            poser("echec", f"Claude Code {apres or avant or ''} : mise à jour impossible "
                           "pour l'instant (hors ligne ?) — la version installée "
                           "sera utilisée.".replace("  ", " "))
    except Exception as exc:  # noqa: BLE001
        poser("echec", f"Claude Code : vérification impossible ({str(exc)[:80]}).")
    finally:
        _MAJ_CLAUDE_FINIE.set()


def _texte_resultat(ev: dict[str, Any]) -> str:
    """Le texte porté par un événement `result`, quelle que soit sa forme."""
    r = ev.get("result")
    if isinstance(r, str):
        return r
    if isinstance(r, list):  # certaines versions renvoient des blocs
        return "".join(b.get("text") or "" for b in r
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _expliquer(message: str, code: Optional[int]) -> str:
    """Traduit les erreurs connues de Claude Code en consigne actionnable."""
    bas = message.lower()
    if re.search(r"usage limit|rate limit|limit (will )?reset|hit your limit|"
                 r"limite d.utilisation|too many requests|429", bas):
        return ("Limite d'utilisation de ton abonnement Claude atteinte. "
                "Attends la réinitialisation puis « Relancer la synthèse ». "
                f"({message[:160]})")
    if re.search(r"not logged in|log ?in|authenticat|api key|unauthori|401|"
                 r"invalid.*token|oauth", bas):
        return ("Claude Code n'est pas connecté. Ouvre une invite de commandes, "
                "tape 'claude' et suis la procédure de connexion.")
    if "max_turns" in bas or "maximum number of turns" in bas or "max turns" in bas:
        return ("Claude a dépassé le nombre de tours autorisés avant de terminer. "
                "Relance la synthèse ; si ça se reproduit, la transcription est "
                "peut-être trop longue pour une seule passe.")
    if re.search(r"prompt is too long|context (window|length)|too many tokens|"
                 r"exceeds.*(context|maximum)", bas):
        return ("Transcription trop longue pour une seule passe de Claude. "
                "Coupe l'audio en deux et relance chaque moitié.")
    if re.search(r"overloaded|529|503|502|econnreset|enotfound|etimedout|"
                 r"network|fetch failed|socket", bas):
        return ("Claude est injoignable ou surchargé pour le moment. "
                f"Réessaie dans quelques minutes. ({message[:160]})")
    if not message.strip():
        return f"Claude Code s'est arrêté sans message (code {code})."
    return message


def _appeler_claude(t: Traitement, commande: list[str], transcription: Path,
                    jalons: Jalons) -> tuple[str, dict[str, Any]]:
    debut = time.time()
    journal = t.dossier / JOURNAL_SYNTHESE
    inactivite = INACTIVITE_MAX if "stream-json" in commande else None

    def tracer(*morceaux: str) -> None:
        try:
            with journal.open("a", encoding="utf-8") as fh:
                for m in morceaux:
                    fh.write(m.rstrip("\n") + "\n")
        except Exception:
            pass

    tracer("", f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} === "
              + subprocess.list2cmdline(commande[:2] + ["<consigne>"] + commande[3:]),
           f"stdin : {transcription} ({transcription.stat().st_size} octets)")

    with transcription.open("rb") as stdin:
        proc = suivre(subprocess.Popen(
            commande, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=t.dossier, creationflags=NO_WINDOW, shell=False,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        ))

    # stderr est vidé à part : s'il se remplissait sans lecteur, Claude se
    # bloquerait en écriture et on attendrait un résultat qui ne vient jamais.
    erreurs_brutes: list[str] = []

    def _drainer() -> None:
        try:
            for l in proc.stderr:  # type: ignore[union-attr]
                erreurs_brutes.append(l)
        except Exception:
            pass

    drain = threading.Thread(target=_drainer, daemon=True)
    drain.start()

    # Garde-fou : par inactivité (rien reçu depuis INACTIVITE_MAX) ou par durée
    # absolue, et en tuant tout l'arbre — sinon node survit à claude.cmd.
    garde = {"dernier": time.time(), "motif": ""}

    def _veiller() -> None:
        while True:
            time.sleep(5)
            if proc.poll() is not None:   # fini entre-temps : rien à faire
                return
            maintenant = time.time()
            motif = ""
            if maintenant - debut > DUREE_MAX:
                motif = f"durée maximale de {DUREE_MAX // 60} min dépassée"
            elif inactivite and maintenant - garde["dernier"] > inactivite:
                motif = f"plus aucune sortie de Claude depuis {inactivite // 60} min"
            if motif:
                garde["motif"] = motif
                tracer(f"--- interruption : {motif}")
                tuer(proc, motif)
                return

    threading.Thread(target=_veiller, daemon=True).start()

    lignes: list[str] = []          # lignes hors flux de deltas
    resultat: Optional[dict[str, Any]] = None
    texte_assistant = ""            # blocs texte des messages complets
    texte_deltas = ""               # reconstruit depuis les deltas
    nb_deltas = 0
    partiel = ""
    section = 0

    try:
        for ligne in proc.stdout:  # type: ignore[union-attr]
            garde["dernier"] = time.time()
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                ev = json.loads(ligne)
            except json.JSONDecodeError:
                lignes.append(ligne)
                tracer(ligne)
                continue
            if not isinstance(ev, dict):
                lignes.append(ligne)
                tracer(ligne)
                continue

            genre = ev.get("type")
            if genre == "stream_event":
                nb_deltas += 1
            else:
                lignes.append(ligne)
                tracer(ligne)
            if genre == "result":
                resultat = ev

            morceau = _fragment(ev)
            if not morceau:
                continue
            if genre == "assistant":
                texte_assistant += morceau
            else:
                texte_deltas += morceau
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
        proc.wait()
        drain.join(5)
        erreurs = "".join(erreurs_brutes).strip()
        tracer(f"--- fin : code {proc.returncode}, {int(time.time() - debut)} s, "
               f"{nb_deltas} deltas non journalisés",
               *(["--- stderr :", erreurs] if erreurs else []))

    if resultat is None and lignes:  # sortie JSON d'un seul bloc, non streamée
        try:
            charge = json.loads("\n".join(lignes))
            resultat = charge if isinstance(charge, dict) else None
        except json.JSONDecodeError:
            resultat = None

    markdown = _texte_resultat(resultat) if resultat else ""
    if not markdown.strip():
        markdown = texte_assistant or texte_deltas
    en_erreur = bool(resultat and resultat.get("is_error"))

    # Un vrai résultat prime sur le code de sortie : si seul le wrapper
    # claude.cmd est mort, node a quand même livré la synthèse.
    if markdown.strip() and not en_erreur and not garde["motif"]:
        if proc.returncode != 0:
            noter(f"claude : code {proc.returncode} mais résultat complet, on le garde")
        u = (resultat or {}).get("usage") or {}
        usage = {"entree": u.get("input_tokens"), "sortie": u.get("output_tokens"),
                 "duree": (resultat or {}).get("duration_ms")}
        return markdown, usage

    # --- échec : reconstituer le message le plus parlant -------------------
    if en_erreur:
        message = (_texte_resultat(resultat)
                   or " ".join(str(e) for e in (resultat.get("errors") or []))
                   or str(resultat.get("subtype") or "")).strip()
    elif erreurs:
        message = erreurs
    else:
        # Les lignes JSON d'amorce (system/init…) n'apprennent rien : on
        # montre ce qui vient après, ou rien.
        utiles = [l for l in lignes
                  if not l.startswith('{"type":"system"')
                  and not l.startswith('{"type":"assistant"')]
        message = "\n".join(utiles[-3:])

    if re.search(r"unknown (option|argument)|unrecognized|--\w[\w-]* is not",
                 message + " " + erreurs, re.I):
        raise _OptionInconnue(message or erreurs)
    if _RE_CLI_OBSOLETE.search(message + " " + erreurs):
        noter(f"claude : CLI obsolète — {message[:200]}")
        raise _CliObsolete(message or erreurs)

    if garde["motif"]:
        message = f"Synthèse interrompue : {garde['motif']}."
    else:
        message = _expliquer(message, proc.returncode)

    brouillon = texte_assistant or texte_deltas
    if len(brouillon.strip()) > 200:
        try:
            (t.dossier / PARTIEL_SYNTHESE).write_text(brouillon, encoding="utf-8")
            message += f" Le texte déjà rédigé est dans {PARTIEL_SYNTHESE}."
        except Exception:
            pass
    message += f" Détail : {JOURNAL_SYNTHESE} dans le dossier du cours."
    noter(f"claude : échec (code {proc.returncode}) — {message[:300]}")
    raise RuntimeError(message[:400])


def synthetiser(t: Traitement, transcription: Path) -> Path:
    claude = chemin_claude()
    if not claude:
        raise RuntimeError(
            "Claude Code est introuvable. Relance Installer.bat, ou installe-le "
            "avec : npm install -g @anthropic-ai/claude-code")

    # Une mise à jour lancée au démarrage est peut-être encore en train de
    # remplacer le binaire : on ne l'exécute pas avant qu'elle ait fini.
    if not _MAJ_CLAUDE_FINIE.is_set():
        t.maj("synthese", statut="en_cours", detail="Mise à jour de Claude Code…")
        _MAJ_CLAUDE_FINIE.wait(600)

    t.maj("synthese", statut="en_cours", detail="Claude lit la transcription")

    # La copie posée dans le dossier du cours fait foi ; le fichier de
    # l'application sert de secours.
    jalons = Jalons.depuis(t.dossier / "CLAUDE.md", CLAUDE_MD)

    # 12 tours : Claude n'a besoin que d'un seul, mais s'il décide de relire
    # CLAUDE.md ou la transcription par morceaux, 6 ne suffisaient plus.
    base = [claude, "-p", CONSIGNE, "--max-turns", "12"]
    # Du plus bavard au plus sobre : chaque repli perd un peu d'avancement
    # mais reste fonctionnel sur une version plus ancienne du CLI.
    tentatives = [
        base + ["--output-format", "stream-json", "--verbose",
                "--include-partial-messages"],
        base + ["--output-format", "stream-json", "--verbose"],
        base + ["--output-format", "json"],
    ]

    def essayer() -> tuple[str, dict[str, Any]]:
        derniere: Optional[Exception] = None
        for commande in tentatives:
            try:
                return _appeler_claude(t, commande, transcription, jalons)
            except _OptionInconnue as exc:
                derniere = exc
        raise RuntimeError(str(derniere)[:400] if derniere else
                           "Claude Code n'a accepté aucun format de sortie.")

    try:
        markdown, usage = essayer()
    except _CliObsolete as exc:
        # L'API refuse le modèle à cette version du CLI : on met à jour et
        # on refait un essai, une seule fois.
        nouvelle = mettre_a_jour_claude(
            claude, lambda d: t.maj("synthese", statut="en_cours", detail=d))
        if not nouvelle:
            raise RuntimeError(
                "Claude Code est trop ancien pour ce modèle et sa mise à jour "
                "automatique a échoué. Dans une invite de commandes : "
                "claude update  (ou : npm install -g @anthropic-ai/claude-code@latest), "
                f"puis « Relancer la synthèse ». ({str(exc)[:120]})") from exc
        t.maj("synthese", statut="en_cours",
              detail=f"Claude Code {nouvelle} — nouvel essai")
        try:
            markdown, usage = essayer()
        except _CliObsolete as exc2:
            raise RuntimeError(
                f"Claude Code {nouvelle} refuse toujours ce modèle. "
                f"({str(exc2)[:200]})") from exc2

    if not markdown.strip():
        raise RuntimeError("Claude a renvoyé une réponse vide. "
                           f"Détail : {JOURNAL_SYNTHESE} dans le dossier du cours.")

    t.usage = usage
    sortie = t.dossier / f"{t.audio.stem}.md"
    sortie.write_text(markdown.strip() + "\n", encoding="utf-8")
    t.fichiers["md"] = str(sortie)
    t.maj("synthese", statut="fait", avancement=1.0,
          detail=f"{len(markdown.split())} mots")
    noter(f"synthèse : {sortie.name}, {len(markdown.split())} mots")
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
    """Objet exposé au JavaScript.

    Attention : pywebview parcourt récursivement les attributs publics de cet
    objet pour construire l'API JavaScript (util.py, get_functions). Un
    attribut public tenant la fenêtre l'entraînait jusque dans les propriétés
    COM de CoreWebView2, qu'il interrogeait depuis un thread autre que celui
    de l'interface — ce que WebView2 interdit formellement. D'où les
    plantages au démarrage. Tout ce qui n'est pas une méthode destinée au
    JavaScript doit donc commencer par un souligné : le parcours l'ignore.
    """

    def __init__(self):
        self._fenetre = None
        self._courant: Optional[Traitement] = None

    # -- appelée depuis le JavaScript ---------------------------------
    def choisir_fichier(self) -> Optional[dict[str, Any]]:
        noter("appel choisir_fichier")
        if os.name == "nt":
            try:
                chemin = choisir_fichier_natif()
                noter(f"boîte native : {chemin or 'annulée'}")
                return self._decrire(chemin) if chemin else None
            except Exception as exc:  # noqa: BLE001
                noter(f"boîte native indisponible ({exc}), repli sur pywebview")

        import webview

        motifs = ("Fichiers audio (" + ";".join(f"*{e}" for e in AUDIO_EXT) + ")",
                  "Tous les fichiers (*.*)")
        resultat = self._fenetre.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=motifs)
        if not resultat:
            return None
        return self._decrire(resultat[0])

    def etat_claude(self) -> dict[str, str]:
        """Où en est la vérification de Claude Code lancée au démarrage."""
        return dict(_ETAT_CLAUDE)

    def fichier_initial(self) -> Optional[dict[str, Any]]:
        """Audio déposé sur le raccourci du bureau ou passé en argument."""
        noter(f"appel fichier_initial : {sys.argv[1:]!r}")
        if len(sys.argv) > 1:
            return self._decrire(sys.argv[1])
        return None

    def _decrire(self, chemin: str) -> Optional[dict[str, Any]]:
        p = Path(chemin)
        if not p.is_file():
            return {"erreur": "Fichier introuvable."}
        if p.suffix.lower() == ".txt":
            # Une transcription déjà produite : on ne refera que la synthèse.
            entete = _entete_transcription(p)
            if not entete.get("mots"):
                return {"erreur": "Ce fichier texte est vide."}
            return {"chemin": str(p), "nom": p.name, "reprise": True,
                    "taille": round(p.stat().st_size / 1024),
                    "mots": entete["mots"], "matiere": entete.get("matiere", "")}
        # FFmpeg lit bien plus de formats que la liste ci-dessus : on laisse
        # passer, c'est le decodage qui tranchera avec un message clair.
        if p.suffix.lower() not in AUDIO_EXT and p.stat().st_size < 4096:
            return {"erreur": f"Fichier {p.suffix or 'sans extension'} trop petit "
                              "pour un enregistrement audio."}
        return {"chemin": str(p), "nom": p.name,
                "taille": round(p.stat().st_size / 1048576)}

    def lancer(self, options: dict[str, Any]) -> dict[str, Any]:
        noter(f"appel lancer : {options.get('chemin')!r}, "
              f"modèle {options.get('modele')!r}")
        source = Path(options.get("chemin", ""))
        if not source.exists():
            return {"erreur": "Fichier introuvable."}

        if source.suffix.lower() == ".txt":
            return self._reprendre(source, options)

        dossier = self._nouveau_dossier(source.stem)
        self._courant = Traitement(source, dossier, options, self._pousser)
        threading.Thread(target=self._pipeline, args=(self._courant,), daemon=True).start()
        return self._courant.instantane()

    def relancer_synthese(self) -> dict[str, Any]:
        """Après un échec de la synthèse : on repart de la transcription déjà
        faite, dans le même dossier, sans refaire passer l'audio au GPU."""
        t = self._courant
        noter("appel relancer_synthese")
        if not t or t.etat == "en_cours":
            return {"erreur": "Rien à relancer."}
        transcription = Path(t.fichiers.get("txt") or t.dossier / "transcription.txt")
        if not transcription.exists():
            return {"erreur": "transcription.txt est introuvable : relance le cours "
                              "depuis l'audio."}
        t.etat, t.erreur, t.usage = "en_cours", None, {}
        t.fichiers = {"txt": str(transcription)}
        for nom in ("synthese", "pdf"):
            t.etapes[nom] = {"statut": "attente", "avancement": 0.0, "detail": ""}
        threading.Thread(target=self._pipeline, args=(t, transcription),
                         daemon=True).start()
        return t.instantane()

    # -- interne -------------------------------------------------------
    def _nouveau_dossier(self, nom: str) -> Path:
        dossier = SORTIE / f"{time.strftime('%Y-%m-%d')}_{nom[:60]}"
        n = 2
        while dossier.exists():
            dossier = dossier.with_name(f"{dossier.name.rstrip('0123456789_')}_{n}")
            n += 1
        dossier.mkdir(parents=True, exist_ok=True)
        if CLAUDE_MD.exists():
            shutil.copy(CLAUDE_MD, dossier / "CLAUDE.md")
        return dossier

    def _reprendre(self, texte: Path, options: dict[str, Any]) -> dict[str, Any]:
        """Un transcription.txt choisi à la place de l'audio : la synthèse se
        refait dans son dossier de cours s'il en vient, sinon dans un nouveau."""
        entete = _entete_transcription(texte)
        if not entete.get("mots"):
            return {"erreur": "Ce fichier texte est vide."}

        dans_un_cours = (texte.name == "transcription.txt"
                         and texte.parent != SORTIE
                         and texte.parent.parent == SORTIE)
        if dans_un_cours:
            dossier = texte.parent
            transcription = texte
            if CLAUDE_MD.exists() and not (dossier / "CLAUDE.md").exists():
                shutil.copy(CLAUDE_MD, dossier / "CLAUDE.md")
        else:
            dossier = self._nouveau_dossier(texte.stem)
            transcription = dossier / "transcription.txt"
            shutil.copy(texte, transcription)

        # Le nom de l'audio d'origine est dans l'en-tête ; à défaut, celui du
        # dossier sans sa date sert de nom au support.
        nom_audio = entete.get("fichier") or re.sub(r"^\d{4}-\d{2}-\d{2}_", "", dossier.name)
        audio = dossier / nom_audio
        if audio.suffix.lower() in (".txt", ".md"):
            audio = audio.with_suffix("")
        options = dict(options, matiere=options.get("matiere") or entete.get("matiere", ""))

        t = Traitement(audio, dossier, options, self._pousser)
        t.fichiers["txt"] = str(transcription)
        t.etapes["transcription"] = {"statut": "fait", "avancement": 1.0,
                                     "detail": f"reprise · {entete['mots']} mots"}
        self._courant = t
        threading.Thread(target=self._pipeline, args=(t, transcription),
                         daemon=True).start()
        return t.instantane()

    def lire(self, genre: str) -> str:
        if not self._courant:
            return ""
        chemin = self._courant.fichiers.get(genre)
        if not chemin or not Path(chemin).exists():
            return ""
        return Path(chemin).read_text(encoding="utf-8")

    def ouvrir(self, cible: str) -> None:
        if not self._courant:
            return
        chemin = self._courant.fichiers.get(cible) or str(self._courant.dossier)
        try:
            os.startfile(chemin)  # noqa: S606
        except Exception:
            subprocess.Popen(["explorer", chemin], creationflags=NO_WINDOW)

    def _pousser(self, instantane: dict[str, Any]) -> None:
        if not self._fenetre:
            return
        charge = json.dumps(instantane, ensure_ascii=False)
        try:
            self._fenetre.evaluate_js(f"window.majEtat({charge})")
        except Exception:
            pass

    def _pousser_claude(self, etat: dict[str, str]) -> None:
        """Avant que la page soit chargée, evaluate_js échoue : l'interface
        redemande alors l'état par etat_claude() dès qu'elle est prête."""
        if not self._fenetre:
            return
        charge = json.dumps(etat, ensure_ascii=False)
        try:
            self._fenetre.evaluate_js(f"window.majClaude && window.majClaude({charge})")
        except Exception:
            pass

    def _pipeline(self, t: Traitement, transcription: Optional[Path] = None) -> None:
        noter(f"pipeline : {t.audio.name} -> {t.dossier}"
              + (" (reprise depuis la transcription)" if transcription else ""))
        if transcription is None:
            try:
                transcription = transcrire(t)
            except Exception as exc:  # noqa: BLE001
                t.echec("transcription", _lisible(exc))
                return
        try:
            markdown = synthetiser(t, transcription)
        except Exception as exc:  # noqa: BLE001
            noter("synthèse : échec — " + str(exc)[:300])
            t.echec("synthese", _lisible(exc))
            return
        try:
            mettre_en_page(t, markdown)
        except Exception as exc:  # noqa: BLE001
            t.maj("pdf", statut="ignore", detail=_lisible(exc)[:150])

        t.etat = "fini"
        t.notifier(t.instantane())


def _entete_transcription(chemin: Path) -> dict[str, Any]:
    """Relit l'en-tête écrit par _transcrire (« Cours : », « Fichier : »)
    et compte les mots. Tolère un texte quelconque sans en-tête."""
    infos: dict[str, Any] = {}
    try:
        texte = chemin.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return infos
    for ligne in texte.splitlines()[:5]:
        m = re.match(r"^(Cours|Fichier|Durée)\s*:\s*(.+?)\s*$", ligne)
        if m:
            cle = {"Cours": "matiere", "Fichier": "fichier", "Durée": "duree"}[m.group(1)]
            infos[cle] = m.group(2)
    if infos.get("matiere") in ("à préciser", ""):
        infos["matiere"] = ""
    infos["mots"] = len(texte.split())
    return infos


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
    brancher_journal()
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
    passerelle._fenetre = fenetre

    # Filet de sécurité : si webview.start() ne rend jamais la main — ça
    # arrive — la fermeture de la fenêtre coupe quand même le processus.
    def _tracer(nom: str):
        def _reagir(*_a: Any, **_k: Any) -> None:
            noter(f"événement {nom}")
            if nom == "closed":
                arreter(0)
        return _reagir

    for nom in ("shown", "loaded", "closing", "closed"):
        try:
            getattr(fenetre.events, nom).__iadd__(_tracer(nom))
        except Exception as exc:  # noqa: BLE001
            noter(f"événement {nom} indisponible : {exc}")

    # La fenetre n'existe pas encore : un guetteur la reconnaitra a son titre.
    threading.Thread(target=poser_icone, daemon=True).start()

    # Claude Code ne se met à jour qu'en session interactive — et ici il n'en
    # a jamais : on s'en charge à chaque lancement, en arrière-plan.
    _MAJ_CLAUDE_FINIE.clear()
    threading.Thread(target=maj_claude_en_arriere_plan,
                     args=(passerelle._pousser_claude,), daemon=True).start()

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
