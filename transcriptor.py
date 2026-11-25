#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Transcriptor — application Windows.

Audio d'un cours (+ diapos, photos du tableau, notes) -> transcription GPU
-> support de révision rédigé par Claude selon un fichier de consignes -> PDF.
Fenêtre native, aucun terminal, aucun WSL.

Les consignes sont des fichiers Markdown de Documents\\Transcriptor\\Consignes :
le corps est envoyé à Claude, l'en-tête `pdf:` règle la mise en page.
"""
from __future__ import annotations

import datetime
import errno
import importlib
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
SCRIPTS = APP_DIR / "scripts"
PDF_SCRIPT = SCRIPTS / "generate_pdf.py"
DOCX_SCRIPT = SCRIPTS / "generate_docx.py"
ICONE = APP_DIR / "static" / "transcriptor.ico"
MODELES_APP = APP_DIR / "consignes"      # modèles fournis avec l'application
MODELES_ANNEE_APP = MODELES_APP / "annee"

sys.path.insert(0, str(SCRIPTS))
import annee as an  # noqa: E402  (mode année : horaire, classement des audios)
import entete  # noqa: E402  (en-tête des fichiers de consignes)
import supports as sup  # noqa: E402  (diapos, photos, notes joints à l'audio)
import suivi  # noqa: E402  (mode année : ce qui est déjà dans les fiches)

DOCUMENTS = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents"
SORTIE = Path(os.environ.get("TRANSCRIPTOR_SORTIE", DOCUMENTS / "Transcriptor"))
CONSIGNES = SORTIE / "Consignes"          # modèles de l'utilisateur, modifiables
CONSIGNES_ANNEE = CONSIGNES / "Année"     # ceux du mode année (horaire, notes, fiches, infos)

AUDIO_EXT = an.EXT_AUDIO
STEPS = ("supports", "transcription", "synthese", "pdf")

# Masque la fenêtre noire des sous-processus.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

_model_cache: dict[str, Any] = {}


# ----------------------------------------------------------------------
# Cycle de vie du processus
# ----------------------------------------------------------------------
BASE_LOCALE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Transcriptor"
JOURNAL = BASE_LOCALE / "journal.log"
REGLAGES = BASE_LOCALE / "reglages.json"
# Répertoires de travail de Claude, un par synthèse : vides de tout CLAUDE.md
# ou AGENTS.md, ils ne contiennent que les supports du cours. Seules les
# consignes choisies comptent donc, quel que soit le dossier d'où l'on
# reprend — et deux fenêtres ouvertes ne se marchent pas dessus.
ATELIER = BASE_LOCALE / "atelier"
VENDOR = BASE_LOCALE / "vendor"             # mermaid.min.js, téléchargé une fois

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
def _filtres_principal() -> list[tuple[str, str]]:
    return [("Fichiers audio", ";".join(f"*{e}" for e in AUDIO_EXT)),
            ("Transcription déjà produite — synthèse seule", "*.txt"),
            ("Résumé déjà produit — PDF seul", "*.md"),
            ("Tous les fichiers", "*.*")]


def _filtres_supports() -> list[tuple[str, str]]:
    return [("Diapos, photos du tableau, notes",
             ";".join(f"*{e}" for e in sorted(sup.EXT_SUPPORTS))),
            ("Tous les fichiers", "*.*")]


def choisir_fichier_natif(titre: str, filtres: list[tuple[str, str]],
                          multiple: bool = False) -> Optional[list[str]]:
    """Boîte « Ouvrir » de Windows, affichée sur un thread STA dédié.

    pywebview exécute les appels venus du JavaScript sur un thread Python
    ordinaire — donc MTA côté .NET — puis y ouvre WinForms.OpenFileDialog en
    lui passant un formulaire appartenant à un autre thread. Double faute :
    violation d'apartment COM et accès inter-thread à un contrôle. C'est ce
    qui fait tomber l'application au clic sur « choisir l'enregistrement ».
    On ouvre donc la boîte nous-mêmes, sur un thread correctement initialisé.

    Renvoie les chemins choisis, None si annulé, et lève RuntimeError si la
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

    filtre = "".join(f"{nom}\0{masque}\0" for nom, masque in filtres) + "\0"
    tampon = ctypes.create_unicode_buffer(65536 if multiple else 8192)
    issue: dict[str, Any] = {}

    OFN = 0x0008_180C  # EXPLORER | FILEMUSTEXIST | PATHMUSTEXIST | NOCHANGEDIR | HIDEREADONLY
    if multiple:
        OFN |= 0x0200  # ALLOWMULTISELECT
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
                # Sélection multiple : « dossier\0fichier1\0fichier2\0\0 » ;
                # un seul fichier : son chemin complet.
                brut = ctypes.wstring_at(ctypes.addressof(tampon), len(tampon))
                parties = [x for x in brut.split("\0\0", 1)[0].split("\0") if x]
                if len(parties) > 1:
                    issue["chemins"] = [os.path.join(parties[0], f) for f in parties[1:]]
                elif parties:
                    issue["chemins"] = parties
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
    return issue.get("chemins")


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


def taille_fenetre() -> tuple[int, int, bool]:
    """(largeur, hauteur, agrandie) de la fenêtre au lancement, en pixels
    logiques, d'après la zone utile de l'écran (sans la barre des tâches).

    L'écran de choix passe sur deux colonnes dès 960 px de large et tient
    alors en ~760 px de haut : on vise cette largeur, toute la hauteur utile
    jusqu'à 940 px, et la fenêtre agrandie sur les petits écrans."""
    defaut = (1240, 900, False)
    if os.name != "nt":
        return defaut
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        zone = wintypes.RECT()
        if not u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(zone), 0):  # SPI_GETWORKAREA
            return defaut
        largeur = zone.right - zone.left
        hauteur = zone.bottom - zone.top
        # Un processus qui gère le DPI reçoit des pixels physiques ;
        # pywebview attend des pixels logiques (96 ppp).
        try:
            if u32.IsProcessDPIAware():
                echelle = u32.GetDpiForSystem() / 96 or 1
                largeur, hauteur = largeur / echelle, hauteur / echelle
        except Exception:  # noqa: BLE001  (Windows antérieur à 10)
            pass
        noter(f"zone utile de l'écran : {int(largeur)}×{int(hauteur)}")
        if hauteur < 800 or largeur < 1040:
            return int(min(largeur, 1240)), int(hauteur), True
        return int(min(largeur - 48, 1240)), int(min(hauteur - 24, 940)), False
    except Exception as exc:  # noqa: BLE001
        noter(f"taille de fenêtre par défaut ({exc})")
        return defaut


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
# Réglages mémorisés, modules installés à la demande, ouverture de fichiers
# ----------------------------------------------------------------------
def lire_reglages() -> dict[str, Any]:
    try:
        return json.loads(REGLAGES.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def ecrire_reglages(**maj: Any) -> None:
    try:
        r = lire_reglages()
        r.update(maj)
        REGLAGES.parent.mkdir(parents=True, exist_ok=True)
        REGLAGES.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        noter(f"réglages non enregistrés : {exc}")


_VERROU_PIP = threading.Lock()
_ECHECS_PIP: set[str] = set()     # une installation ratée n'est pas retentée par photo


def module_requis(nom: str, paquet: str, signaler=None) -> Any:
    """Importe `nom`, en installant `paquet` dans le venv s'il manque.
    Une installation faite avant l'arrivée des supports (PPTX, photos) n'a
    ni python-pptx ni Pillow : inutile de relancer Installer.bat pour ça."""
    try:
        return importlib.import_module(nom)
    except ImportError:
        pass
    with _VERROU_PIP:
        try:
            return importlib.import_module(nom)
        except ImportError:
            pass
        if paquet in _ECHECS_PIP:
            raise ImportError(f"{paquet} indisponible (installation déjà tentée)")
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe" and python.with_name("python.exe").exists():
            python = python.with_name("python.exe")
        if signaler:
            signaler(f"Installation de {paquet}…")
        noter(f"module {nom} absent : pip install {paquet}")
        code, sortie = _lancer_utilitaire(
            [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
             paquet], 900)
        noter(f"pip install {paquet} : code {code} — {sortie.strip()[-300:]}")
        importlib.invalidate_caches()
        try:
            return importlib.import_module(nom)
        except ImportError as exc:
            _ECHECS_PIP.add(paquet)
            raise ImportError(f"{paquet} n'a pas pu s'installer (connexion ?) — "
                              "relance Installer.bat") from exc


def ouvrir_fichier(chemin: Path, *, modifier: bool = False) -> None:
    """Ouvre un fichier ou un dossier avec l'application de Windows. Pour
    modifier un .md sans application associée, le Bloc-notes prend le relais."""
    if os.name != "nt":
        subprocess.Popen(["xdg-open", str(chemin)])
        return
    for verbe in (("edit", "open") if modifier else ("open",)):
        try:
            os.startfile(str(chemin), verbe)  # noqa: S606
            return
        except OSError:
            continue
    if modifier:
        subprocess.Popen(["notepad.exe", str(chemin)])
    else:
        subprocess.Popen(["explorer", str(chemin)], creationflags=NO_WINDOW)


# ----------------------------------------------------------------------
# Consignes de synthèse : un fichier .md par modèle
# ----------------------------------------------------------------------
def installer_modeles() -> None:
    """Copie dans Documents\\Transcriptor\\Consignes les modèles livrés avec
    l'application qui n'y ont jamais été proposés. Un modèle que l'utilisateur
    a supprimé ne revient pas, sauf si le dossier est vide : les noms déjà
    livrés sont notés dans .modeles_livres."""
    CONSIGNES.mkdir(parents=True, exist_ok=True)
    registre = CONSIGNES / ".modeles_livres"
    try:
        livres = set(registre.read_text(encoding="utf-8").split("\n")) - {""}
    except OSError:
        livres = set()
    vide = not any(_modele_visible(p) for p in CONSIGNES.glob("*.md"))
    modeles = sorted(MODELES_APP.glob("*.md")) if MODELES_APP.is_dir() else []
    change = False
    for src in modeles:
        cible = CONSIGNES / src.name
        if not cible.exists() and (vide or src.name not in livres):
            shutil.copyfile(src, cible)
            noter(f"consignes : modèle « {src.stem} » installé")
            change = True
        if src.name not in livres:
            livres.add(src.name)
            change = True
    if change:
        registre.write_text("\n".join(sorted(livres)) + "\n", encoding="utf-8")


def _modele_visible(p: Path) -> bool:
    return p.suffix.lower() == ".md" and not p.name.startswith((".", "~", "_"))


def liste_consignes() -> list[dict[str, Any]]:
    try:
        installer_modeles()
    except Exception as exc:  # noqa: BLE001
        noter(f"consignes : installation des modèles impossible ({exc})")
    modeles = []
    for p in sorted((x for x in CONSIGNES.glob("*.md") if _modele_visible(x)),
                    key=lambda x: x.stem.lower()):
        info: dict[str, Any] = {"nom": p.stem, "description": "", "avertissement": "",
                                "sections": 0, "claude": {}}
        try:
            c = entete.lire(p)
            info["description"] = c.description
            info["claude"] = c.claude
            info["sections"] = len(Jalons.depuis(c.corps).noms)
            if not c.corps.strip():
                info["avertissement"] = "Fichier vide : Claude n'aurait aucune consigne."
            elif c.avertissements:
                info["avertissement"] = " ; ".join(c.avertissements)
        except Exception as exc:  # noqa: BLE001
            info["avertissement"] = f"Illisible : {exc}"
        modeles.append(info)
    return modeles


def chemin_consignes(nom: str) -> Path:
    """Le fichier du modèle `nom`, sans jamais sortir du dossier Consignes."""
    p = (CONSIGNES / f"{nom}.md").resolve()
    if not nom or p.parent != CONSIGNES.resolve() or not p.is_file():
        raise FileNotFoundError(f"Modèle de consignes introuvable : « {nom} ».")
    return p


def debut_enregistrement(audio: Path) -> Optional[datetime.datetime]:
    """Heure de début de l'enregistrement quand le fichier la porte
    (métadonnée creation_time), en heure locale. Sert à situer les photos
    du tableau dans le cours."""
    try:
        import av

        with av.open(str(audio)) as conteneur:
            brut = conteneur.metadata.get("creation_time") or next(
                (f.metadata.get("creation_time") for f in conteneur.streams
                 if f.metadata.get("creation_time")), None)
        if not brut:
            return None
        d = datetime.datetime.fromisoformat(brut.strip().replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone().replace(tzinfo=None)
        return d if d.year >= 2000 else None
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------
# État du traitement
# ----------------------------------------------------------------------
_NUMEROS = iter(range(1, 1_000_000))


class Traitement:
    def __init__(self, audio: Path, dossier: Path, options: dict[str, Any], notifier):
        self.id = next(_NUMEROS)
        self.audio = audio
        self.dossier = dossier
        self.options = options
        self.notifier = notifier
        self.etat = "en_cours"
        self.erreur: Optional[str] = None
        self.connexion = False          # échec parce que Claude Code n'est pas connecté
        self.usage: dict[str, Any] = {}
        self.fichiers: dict[str, str] = {}
        self.etapes = {n: {"statut": "attente", "avancement": 0.0, "detail": ""} for n in STEPS}
        self.etapes["supports"]["statut"] = "ignore"
        self._dernier_envoi = 0.0
        self.consignes: Optional[Path] = None          # modèle choisi (fichier vivant)
        self.sources_supports = [Path(p) for p in options.get("supports") or []]
        self.supports: list[sup.Support] = []
        self.avertissements: list[str] = []
        self.debut: Optional[datetime.datetime] = None  # début de l'enregistrement
        self.duree = 0.0                                # durée de l'audio, en s

    def instantane(self) -> dict[str, Any]:
        return {
            "etat": self.etat, "erreur": self.erreur, "usage": self.usage,
            "connexion": self.connexion,
            "etapes": self.etapes, "fichiers": self.fichiers,
            "nom": self.audio.stem, "dossier": str(self.dossier),
            "consignes": self.consignes.stem if self.consignes else "",
            "avertissements": self.avertissements, "id": self.id,
            "claude": {"modele": self.options.get("claude_modele") or "",
                       "effort": self.options.get("claude_effort") or ""},
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
# Étape 0 — supports du cours (diapos, photos du tableau, notes)
# ----------------------------------------------------------------------
def preparer_supports(t: Traitement) -> None:
    """Prépare les supports joints dans <cours>/supports/. Ceux d'un passage
    précédent (relance, reprise) sont repris sans être refaits."""
    dossier = t.dossier / "supports"
    deja = sup.lire_manifeste(dossier) if dossier.is_dir() else []
    if not t.sources_supports and not deja:
        t.maj("supports", statut="ignore", detail="aucun")
        return

    nouveaux = [p for p in t.sources_supports
                if str(p.resolve()) not in {s.source for s in deja}]
    total = max(1, len(nouveaux))
    compte = [0]
    t.maj("supports", statut="en_cours", avancement=0.0,
          detail=f"Préparation de {total} fichier" + ("s" if total > 1 else "") if nouveaux
          else "Reprise des supports déjà préparés")

    def fichier(nom: str) -> None:
        compte[0] += 1
        t.maj("supports", avancement=(compte[0] - 1) / total,
              detail=f"{compte[0]}/{total} · {nom}"[:90])

    def importer(nom: str, paquet: str) -> Any:
        return module_requis(nom, paquet, lambda m: t.maj("supports", detail=m))

    t.supports, avert = sup.preparer(t.sources_supports, dossier,
                                     importer=importer, signaler=fichier)
    for a in avert:
        noter("supports : " + a)
        if a not in t.avertissements:
            t.avertissements.append(a)
    if not t.supports:
        t.maj("supports", statut="ignore", detail="aucun support utilisable")
        return
    detail = sup.resume(t.supports)
    if avert:
        detail += f" · {len(avert)} ignoré" + ("s" if len(avert) > 1 else "")
    t.maj("supports", statut="fait", avancement=1.0, detail=detail)
    noter(f"supports : {detail}")


# ----------------------------------------------------------------------
# Étape 1 — transcription
# ----------------------------------------------------------------------
def amorce(matiere: str, termes: str) -> str:
    """Amorce de Whisper (initial_prompt). Avec condition_on_previous_text
    à False, faster-whisper ne la donne qu'à la première fenêtre de 30 s :
    le vocabulaire passe donc aussi par mots_cles()."""
    matiere = matiere.strip() or "cours magistral"
    texte = f"Cours magistral de {matiere}."
    termes = termes.strip().strip(",")
    if termes:
        texte += f" Vocabulaire : {termes}."
    return texte[:900]  # Whisper tronque au-delà d'environ 224 tokens.


def mots_cles(termes: str, diapos: str = "") -> str:
    """Vocabulaire redonné à Whisper à chaque fenêtre (hotwords) : il y
    reprend l'orthographe et la casse. Les termes saisis d'abord — faster-
    whisper coupe par la fin au-delà de ~220 tokens —, puis les titres des
    diapositives jointes."""
    morceaux = [x.strip().strip(",") for x in (termes, diapos) if x and x.strip().strip(",")]
    return ", ".join(morceaux)[:900]


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


class Interrompu(RuntimeError):
    """Arrêt demandé par l'utilisateur."""


def transcrire_fichier(audio: Path, sortie: Path, *, modele: str, matiere: str, termes: str,
                       vocabulaire_supports: str = "", debut: Optional[datetime.datetime] = None,
                       signaler=None, arret: Optional[threading.Event] = None,
                       cpu: bool = False) -> dict[str, Any]:
    """Transcrit `audio` dans `sortie`, écrite d'un bloc à la fin : un fichier
    de transcription présent est toujours complet. signaler(avancement, detail)
    suit la progression ; `arret` levé interrompt (Interrompu)."""
    def dire(avancement: Optional[float], detail: str) -> None:
        if signaler:
            signaler(avancement, detail)

    dire(None, "Chargement du modèle" + (" (processeur)" if cpu else ""))
    m = charger_modele(modele, cpu=cpu)
    dire(None, "Analyse de l'audio")
    reglages = dict(
        language="fr",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=amorce(matiere, termes),
    )
    vocabulaire = mots_cles(termes, vocabulaire_supports)
    try:
        segments, info = m.transcribe(str(audio), hotwords=vocabulaire or None, **reglages)
    except TypeError:            # faster-whisper < 1.0.2 : pas de hotwords
        segments, info = m.transcribe(str(audio), **reglages)

    total = max(float(info.duration or 0.0), 1.0)
    debut = debut or debut_enregistrement(audio)
    sortie.parent.mkdir(parents=True, exist_ok=True)
    provisoire = sortie.with_name(sortie.name + ".partiel")
    depart = time.time()
    minute_courante = -1
    tampon: list[str] = []
    try:
        with provisoire.open("w", encoding="utf-8") as fh:
            fh.write(f"Cours : {matiere or 'à préciser'}\n")
            fh.write(f"Fichier : {audio.name}\nDurée : {hhmm(total)}\n")
            if debut:
                fh.write(f"Début : {debut:%Y-%m-%d %H:%M:%S}\n")
            fh.write("\n")

            for seg in segments:
                if arret is not None and arret.is_set():
                    raise Interrompu("arrêt demandé")
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
                dire(part, detail)

            if tampon:
                fh.write(" ".join(tampon).strip() + "\n")
        os.replace(provisoire, sortie)
    finally:
        if provisoire.exists():
            try:
                provisoire.unlink()
            except OSError:
                pass

    mots = len(sortie.read_text(encoding="utf-8").split())
    return {"duree": total, "debut": debut, "mots": mots}


def transcrire_robuste(audio: Path, sortie: Path, **options: Any) -> dict[str, Any]:
    """transcrire_fichier sur la carte graphique, sinon sur le processeur."""
    try:
        return transcrire_fichier(audio, sortie, cpu=False, **options)
    except Interrompu:
        raise
    except Exception as exc:  # noqa: BLE001
        if not _panne_cuda(exc):
            raise
    if options.get("signaler"):
        options["signaler"](0.0, "CUDA indisponible — reprise sur le processeur")
    return transcrire_fichier(audio, sortie, cpu=True, **options)


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
    sortie = t.dossier / "transcription.txt"

    def signaler(avancement: Optional[float], detail: str) -> None:
        t.maj("transcription", avancement=avancement, detail=detail)

    r = transcrire_fichier(
        t.audio, sortie, modele=t.options["modele"], matiere=t.options["matiere"],
        termes=t.options["termes"],
        vocabulaire_supports=sup.vocabulaire(t.supports, t.dossier / "supports"),
        signaler=signaler, cpu=cpu)
    t.duree, t.debut = r["duree"], r["debut"]
    t.fichiers["txt"] = str(sortie)
    t.maj("transcription", statut="fait", avancement=1.0,
          detail=f"{r['mots']} mots".replace(",", " "))
    return sortie


# ----------------------------------------------------------------------
# Étape 2 — synthèse par Claude Code
# ----------------------------------------------------------------------
def consigne(avec_supports: bool) -> str:
    """Le prompt passé à `claude -p`. Tout le reste (consignes choisies,
    supports, transcription) arrive sur l'entrée standard.

    Pas de chevrons ni de guillemets droits ici : claude.cmd passe par
    cmd.exe, qui les interpréterait hors d'une chaîne protégée."""
    texte = ("L'entrée standard contient, entre balises : consignes, les règles de "
             "rédaction à appliquer ; transcription, la transcription brute d'un cours")
    if avec_supports:
        texte += (" ; supports, les supports de ce cours (diapositives, photos du tableau, "
                  "notes). Avant de rédiger, lis avec l'outil Read chaque image et chaque PDF "
                  "listés, puis croise-les avec la transcription comme le demandent les "
                  "consignes — à défaut : corrige grâce à eux les termes, noms, chiffres et "
                  "formules mal transcrits, complète schémas et formules, et cite la source "
                  "(diapo n, photo n). Pour insérer une image dans le document, écris "
                  "![légende](supports/nom_du_fichier.jpg) avec un nom exact de la liste")
    texte += (". Produis le document décrit par les consignes, en suivant strictement leur "
              "structure, leur ordre et leur style. Traite tout en une seule réponse : "
              "personne ne pourra te dire de continuer, ignore donc ce que les consignes "
              "prévoient pour un échange (commandes rapides, traitement en plusieurs fois). "
              "Réponds uniquement par le Markdown final, sans préambule ni commentaire.")
    return texte


# Modèles proposés dans l'interface. Les alias de Claude Code suivent les
# nouvelles versions d'eux-mêmes (« opus » = le dernier Opus disponible) ; un
# identifiant complet (claude-opus-5-5, opus[1m]…) reste possible via « Autre ».
MODELES_CLAUDE = (
    ("", "Par défaut du compte"),
    ("best", "Le meilleur disponible"),
    ("fable", "Fable — le plus capable, cours longs"),
    ("opus", "Opus — raisonnement soigné"),
    ("sonnet", "Sonnet — équilibré, plus rapide"),
    ("haiku", "Haiku — rapide et économe"),
)
EFFORTS_CLAUDE = (
    ("", "Par défaut"), ("low", "Faible"), ("medium", "Moyen"), ("high", "Élevé"),
    ("xhigh", "Très élevé"), ("max", "Maximum"),
)


def reglages_claude(options: dict[str, Any]) -> tuple[str, str]:
    """(modèle, effort) demandés par l'interface, vérifiés ; ValueError sinon."""
    try:
        return (entete.modele_valide(options.get("claude_modele") or ""),
                entete.effort_valide(options.get("claude_effort") or ""))
    except ValueError as exc:
        raise ValueError(f"Réglage Claude : {exc}.") from exc


# Garde-fous de l'appel à Claude. L'ancien couperet fixe (30 min) tombait en
# pleine rédaction d'un cours long — et ne tuait que claude.cmd, pas node.
INACTIVITE_MAX = 20 * 60   # plus aucune ligne reçue depuis ce délai : on coupe
DUREE_MAX = 120 * 60       # plafond absolu, quoi qu'il arrive
JOURNAL_SYNTHESE = "synthese.log"      # dans le dossier du cours
PARTIEL_SYNTHESE = "synthese_partielle.md"

# Les sections attendues ne sont pas figées ici : elles sont relues dans le
# fichier de consignes choisi (titres « ### 1. Nom »), pour que modifier les
# consignes suffise à changer la sortie — et le suivi d'avancement.

_RE_TITRE = re.compile(r"^#{1,4}[ \t]*(.+?)[ \t]*$", re.M)
_RE_NUMERO = re.compile(r"^(\d{1,2})[.)]")
_RE_SECTION_CONSIGNE = re.compile(r"^#{2,4}[ \t]*(\d{1,2})[.)][ \t]*(.+?)[ \t]*$", re.M)


def _sans_accent(texte: str) -> str:
    plat = unicodedata.normalize("NFD", texte.lower())
    return "".join(c for c in plat if unicodedata.category(c) != "Mn")


class Jalons:
    """Les titres de sections numérotées des consignes (« ### 3. Plan »),
    servant de repères d'avancement pendant que Claude rédige. Des consignes
    sans sections numérotées n'en ont pas : on affiche alors le nombre de
    mots écrits."""

    def __init__(self, sections: dict[int, str]):
        self.noms = sections
        self.total = max(sections) if sections else 0
        self._plats = {n: _sans_accent(v) for n, v in self.noms.items()}

    @classmethod
    def depuis(cls, texte: str) -> "Jalons":
        trouve: dict[int, str] = {}
        for m in _RE_SECTION_CONSIGNE.finditer(texte or ""):
            n = int(m.group(1))
            if 1 <= n <= 40:
                trouve.setdefault(n, m.group(2))
        return cls(trouve if len(trouve) >= 3 else {})

    def reperer(self, texte: str) -> int:
        """Numéro de la section la plus avancée présente dans un fragment."""
        if not self.total:
            return 0
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

# Claude Code sans connexion valide au compte Claude. En mode -p, il écrit
# « Not logged in · Please run /login », « Failed to authenticate… API Error:
# 401 OAuth token has expired », « Invalid API key… ». Les nombres sont pris
# en mots entiers : un identifiant de requête peut contenir « 401 ».
_RE_CONNEXION = re.compile(
    r"not logged in|run /login|failed to authenticate|authentication_failed|"
    r"authentication_error|invalid api key|invalid authentication|login expired|"
    r"oauth (token|session)|could not resolve authentication|"
    r"invalid (auth |bearer )?token|\b401\b|unauthori[sz]ed", re.I)

# Les mêmes, sans ambiguïté : pour juger une « réponse » de quelques mots
# (« TERMINÉ, 401 mots écrits » n'est pas une erreur de connexion).
_RE_CONNEXION_FORTE = re.compile(
    r"not logged in|run /login|failed to authenticate|invalid api key|login expired|"
    r"oauth (token|session) (has )?(expired|revoked)", re.I)

# Variables qui, en mode -p, passent avant la connexion au compte Claude.
_VARIABLES_CLE = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")


class ConnexionClaude(RuntimeError):
    """Claude Code n'est pas (ou plus) connecté au compte Claude : l'interface
    propose alors « Se connecter à Claude »."""


class ToursEpuises(RuntimeError):
    """Claude a utilisé tous ses tours (--max-turns) sans livrer de réponse,
    le plus souvent à ouvrir des documents un à un."""


def variables_de_cle() -> list[str]:
    return [v for v in _VARIABLES_CLE if os.environ.get(v, "").strip()]


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
# État affiché dans l'interface : {"etat": "maj" | "ok" | "echec" | "absent" |
# "connexion", "texte": …, "connexion": "oui" | "non" | ""}
_ETAT_CLAUDE: dict[str, str] = {"etat": "inconnu", "texte": "", "connexion": ""}

# Comment Claude Code s'authentifie (champ authMethod de « claude auth status »).
_METHODES_CONNEXION = {
    "claude.ai": "connecté à ton abonnement Claude",
    "oauth_token": "connecté par un jeton (CLAUDE_CODE_OAUTH_TOKEN)",
    "api_key": "connecté par une clé API, facturée à part de l'abonnement",
    "api_key_helper": "connecté par une clé API (apiKeyHelper)",
    "third_party": "connecté par un fournisseur tiers",
}


def connexion_claude(claude: str) -> dict[str, Any]:
    """État de connexion de Claude Code d'après « claude auth status » (lecture
    locale, sans appel à l'API). {"connu": bool, "connecte": bool, "methode": str}.
    Un CLI trop ancien pour cette commande donne connu = False."""
    code, sortie = _lancer_utilitaire([claude, "auth", "status"], 45)
    m = re.search(r"\{.*\}", sortie, re.S)
    try:
        donnees = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        donnees = None
    if not isinstance(donnees, dict):
        return {"connu": False, "connecte": False, "methode": ""}
    methode = str(donnees.get("authMethod") or "")
    connecte = code == 0 and methode != "none"
    if isinstance(donnees.get("loggedIn"), bool):
        connecte = donnees["loggedIn"]
    return {"connu": True, "connecte": connecte, "methode": methode}


def texte_connexion(cx: dict[str, Any]) -> str:
    if not cx.get("connu"):
        return ""
    if not cx.get("connecte"):
        return "non connecté à ton compte Claude"
    texte = _METHODES_CONNEXION.get(cx.get("methode") or "", "connecté")
    if "ANTHROPIC_API_KEY" in variables_de_cle() and cx.get("methode") != "api_key":
        texte += (" — mais la variable ANTHROPIC_API_KEY est définie : en mode "
                  "automatique, Claude Code l'utilise à sa place")
    return texte


def poser_connexion(cx: dict[str, Any]) -> None:
    """Ajoute l'état de connexion à celui de la mise à jour (« base ») dans
    _ETAT_CLAUDE : texte affiché et pastille."""
    if not cx.get("connu"):
        _ETAT_CLAUDE["connexion"] = ""
        return
    base = _ETAT_CLAUDE.get("base", "")
    texte = texte_connexion(cx)
    _ETAT_CLAUDE["connexion"] = "oui" if cx.get("connecte") else "non"
    _ETAT_CLAUDE["texte"] = f"{base} · {texte}." if base else f"Claude Code {texte}."
    _ETAT_CLAUDE["etat"] = ((_ETAT_CLAUDE.get("etat_base") or "ok") if cx.get("connecte")
                            else "connexion")


def script_connexion(claude: str) -> str:
    """Le .cmd ouvert par « Se connecter à Claude » : claude auth login (le
    navigateur s'ouvre), session interactive avec /login si cette commande
    n'existe pas encore, puis l'état obtenu et une pause pour le lire.
    Pas de parenthèses dans les echo (blocs cmd) ; chcp 65001 pour les accents."""
    c = f'"{claude}"'
    lignes = [
        "@echo off",
        "chcp 65001 >nul",
        "title Connexion de Claude Code",
        "echo.",
        "echo   Connexion de Claude Code à ton compte Claude",
        "echo   ---------------------------------------------",
        "echo   Ton navigateur va s'ouvrir : connecte-toi avec le compte de ton abonnement",
        "echo   Claude et accepte. Si rien ne s'ouvre, copie dans le navigateur l'adresse",
        "echo   affichée ci-dessous.",
        "echo.",
        f"call {c} auth login",
        "if not errorlevel 1 goto etat",
        "echo.",
        "echo   La connexion n'a pas abouti par cette voie. Claude Code va s'ouvrir :",
        "echo   tape /login, suis la procédure, puis tape /exit.",
        "echo.",
        "pause",
        f"call {c}",
        ":etat",
        "echo.",
        f"call {c} auth status --text",
        "echo.",
        "if not defined ANTHROPIC_API_KEY goto fin",
        "echo   Attention : la variable ANTHROPIC_API_KEY est définie sur ce PC. En mode",
        "echo   automatique, Claude Code l'utilise à la place de ton abonnement : supprime-la",
        "echo   dans les variables d'environnement de Windows, puis relance Transcriptor.",
        "echo.",
        ":fin",
        "echo   Tu peux fermer cette fenêtre et relancer dans Transcriptor.",
        "echo.",
        "pause",
    ]
    return "\r\n".join(lignes) + "\r\n"


def ouvrir_connexion_claude(apres=None) -> dict[str, Any]:
    """Ouvre une console qui connecte Claude Code au compte Claude. `apres`
    est appelé quand la console se ferme (pour revérifier la connexion)."""
    claude = chemin_claude()
    if not claude:
        return {"erreur": "Claude Code est introuvable : relance Installer.bat."}
    if os.name != "nt":
        return {"erreur": "Dans un terminal : claude auth login"}
    BASE_LOCALE.mkdir(parents=True, exist_ok=True)
    script = BASE_LOCALE / "connexion_claude.cmd"
    script.write_bytes(script_connexion(claude).encode("utf-8"))
    proc = subprocess.Popen(["cmd.exe", "/c", str(script)], cwd=str(Path.home()),
                            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    noter("claude : fenêtre de connexion ouverte")
    if apres:
        def guetter() -> None:
            try:
                proc.wait()
            finally:
                apres()
        threading.Thread(target=guetter, daemon=True).start()
    return {"ok": True}


def verifier_connexion(signaler=None) -> dict[str, str]:
    """Relit l'état de connexion (après la fenêtre de connexion) et le pousse."""
    claude = chemin_claude()
    if claude:
        cx = connexion_claude(claude)
        noter(f"claude : connexion — {cx}")
        poser_connexion(cx)
    # Compteur de relectures : l'interface sait ainsi que la fenêtre de
    # connexion s'est refermée et que l'état affiché est frais.
    _ETAT_CLAUDE["verifie"] = str(int(_ETAT_CLAUDE.get("verifie") or 0) + 1)
    if signaler:
        try:
            signaler(dict(_ETAT_CLAUDE))
        except Exception:
            pass
    return dict(_ETAT_CLAUDE)


def maj_claude_en_arriere_plan(signaler=None) -> None:
    """Au lancement, sans bloquer : le CLI est à jour avant que la
    transcription (une dizaine de minutes) ne laisse la main à la synthèse.
    synthetiser() attend la fin de cette étape avant de lancer claude, pour
    ne pas exécuter un binaire en cours de remplacement."""

    def poser(etat: str, texte: str) -> None:
        _ETAT_CLAUDE.update(etat=etat, texte=texte, base=texte.rstrip(". "), etat_base=etat)
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
        # Connexion au compte : signalée dès l'ouverture, avant tout traitement.
        _MAJ_CLAUDE_FINIE.set()
        cx = connexion_claude(claude)
        noter(f"claude (démarrage) : connexion — {cx}")
        if cx.get("connu"):
            poser_connexion(cx)
            if signaler:
                signaler(dict(_ETAT_CLAUDE))
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


def _expliquer(message: str, code: Optional[int], modele: str = "") -> str:
    """Traduit les erreurs connues de Claude Code en consigne actionnable."""
    bas = message.lower()
    nom = f"« {modele} »" if modele else "par défaut"
    if re.search(r"issue with the selected model|not_found_error|invalid model|unknown model|"
                 r"model[^.]{0,60}(not exist|not found|not available|isn.t available|"
                 r"unavailable|not supported|no access|not have access)", bas):
        return (f"Le modèle Claude {nom} n'existe pas ou n'est pas disponible avec ton "
                "abonnement. Choisis-en un autre et « Relancer la synthèse ». "
                f"({message[:160]})")
    if "effort" in bas and re.search(r"not supported|invalid|unsupported|not available", bas):
        return (f"Ce niveau d'effort n'est pas disponible pour le modèle {nom}. Choisis "
                f"« Par défaut » ou un autre niveau, puis relance. ({message[:160]})")
    if _RE_CONNEXION.search(message):
        texte = ("Claude Code n'est pas connecté à ton compte Claude (connexion absente "
                 "ou expirée). Clique « Se connecter à Claude », connecte-toi dans le "
                 "navigateur, puis relance.")
        cles = variables_de_cle()
        if cles:
            texte += (f" Attention : la variable {cles[0]} est définie sur ce PC ; en mode "
                      "automatique, Claude Code l'utilise à la place de ta connexion. "
                      "Supprime-la (Windows : « Modifier les variables d'environnement "
                      "pour votre compte »), puis relance Transcriptor.")
        return texte + f" (Claude Code : {message.strip()[:160]})"
    if re.search(r"usage limit|rate limit|limit (will )?reset|hit your limit|"
                 r"limite d.utilisation|too many requests|\b429\b", bas):
        return ("Limite d'utilisation de ton abonnement Claude atteinte"
                + (f" pour le modèle {nom}" if modele else "") + ". Attends la "
                "réinitialisation, ou relance la synthèse avec un autre modèle "
                f"(Sonnet, Haiku). ({message[:160]})")
    if "max_turns" in bas or "maximum number of turns" in bas or "max turns" in bas:
        return ("Claude a dépassé le nombre de tours autorisés avant de terminer. "
                "Relance la synthèse ; si ça se reproduit, la transcription est "
                "peut-être trop longue pour une seule passe.")
    if re.search(r"prompt is too long|context (window|length)|too many tokens|"
                 r"exceeds.*(context|maximum)", bas):
        return ("Transcription trop longue pour une seule passe de Claude. "
                "Coupe l'audio en deux et relance chaque moitié.")
    if re.search(r"overloaded|\b(529|503|502)\b|econnreset|enotfound|etimedout|"
                 r"network|fetch failed|socket", bas):
        return ("Claude est injoignable ou surchargé pour le moment. "
                f"Réessaie dans quelques minutes. ({message[:160]})")
    if not message.strip():
        return f"Claude Code s'est arrêté sans message (code {code})."
    return message


def _appeler_claude(t: Traitement, commande: list[str], entree: Path,
                    jalons: Jalons) -> tuple[str, dict[str, Any]]:
    """Un appel à Claude Code. `t` est un Traitement (mode cours) ou une
    EtapeAnnee : seuls maj(), dossier, options, consignes, supports et les
    attributs facultatifs etape_claude / journal_claude / lieu_journal sont lus."""
    debut = time.time()
    etape = getattr(t, "etape_claude", "synthese")
    journal = getattr(t, "journal_claude", None) or t.dossier / JOURNAL_SYNTHESE
    lieu = getattr(t, "lieu_journal", "le dossier du cours")
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
           f"consignes : {t.consignes} · supports : {len(t.supports)} fichier(s)",
           f"stdin : {entree} ({entree.stat().st_size} octets)")

    with entree.open("rb") as stdin:
        proc = suivre(subprocess.Popen(
            commande, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=entree.parent, creationflags=NO_WINDOW, shell=False,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        ))
    t.processus = proc            # pour pouvoir l'arrêter (mode année)

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
    mots = 0
    modele_reel = ""
    demande = commande[commande.index("--model") + 1] if "--model" in commande else ""
    ouverts: list[str] = []         # fichiers que Claude a ouverts ou parcourus

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
            if genre == "system" and ev.get("subtype") == "init" and ev.get("model"):
                modele_reel = str(ev.get("model"))
                t.maj(etape, detail=f"Claude lit le contenu · {modele_reel}"
                      if etape != "synthese" else f"Claude lit la transcription · {modele_reel}")
            if genre == "assistant":
                # Claude lit les supports avant de rédiger, ou écrit ses
                # fichiers (fiches longues) : on le montre.
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        entrees = b.get("input") or {}
                        cible = Path(str(entrees.get("file_path") or "")).name
                        if b.get("name") in ("Write", "Edit"):
                            ecrit = str(entrees.get("content") or entrees.get("new_string") or "")
                            mots += len(ecrit.split())
                            t.maj(etape, avancement=mots / (mots + 6000),
                                  detail=f"Rédaction · {cible} · {mots} mots")
                        else:
                            if b.get("name") in ("Read", "Grep", "Glob"):
                                ouverts.append(cible or str(entrees.get("pattern") or ""))
                            t.maj(etape, detail="Lecture des supports · "
                                  + (cible or str(b.get("name") or "")))

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
                if genre != "assistant" or not nb_deltas:   # sans double compte
                    mots += len(complet.split())
            ecoule = int(time.time() - debut)
            if jalons.total:
                t.maj(etape,
                      avancement=section / jalons.total,
                      detail=(jalons.etiquette(section) if section
                              else f"Rédaction… {ecoule} s"))
            else:
                t.maj(etape,
                      avancement=mots / (mots + 4000) if mots else 0.04,
                      detail=(f"Rédaction… {mots} mots · {ecoule} s" if mots
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
    # Une « réponse » de quelques mots qui dit de se connecter n'en est pas une.
    if len(markdown.strip()) < 300 and _RE_CONNEXION_FORTE.search(markdown):
        en_erreur = True

    # Un vrai résultat prime sur le code de sortie : si seul le wrapper
    # claude.cmd est mort, node a quand même livré la synthèse.
    if markdown.strip() and not en_erreur and not garde["motif"]:
        if proc.returncode != 0:
            noter(f"claude : code {proc.returncode} mais résultat complet, on le garde")
        u = (resultat or {}).get("usage") or {}
        par_modele = (resultat or {}).get("modelUsage")
        modeles = list(par_modele) if isinstance(par_modele, dict) else []
        usage = {"entree": u.get("input_tokens"), "sortie": u.get("output_tokens"),
                 "duree": (resultat or {}).get("duration_ms"),
                 "modele": modele_reel or (modeles[0] if modeles else demande),
                 "modeles": modeles}
        noter(f"claude : synthèse par {usage['modele'] or 'le modèle par défaut'}")
        return markdown, usage

    # --- échec : reconstituer le message le plus parlant -------------------
    if en_erreur:
        message = ((_texte_resultat(resultat) if resultat else "")
                   or " ".join(str(e) for e in ((resultat or {}).get("errors") or []))
                   or str((resultat or {}).get("subtype") or "")
                   or markdown).strip()
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

    connexion = not garde["motif"] and bool(_RE_CONNEXION.search(message))
    tours = (not garde["motif"] and not connexion
             and (str((resultat or {}).get("subtype")) == "error_max_turns"
                  or bool(re.search(r"maximum number of turns|max_turns", message, re.I))))
    if garde["motif"]:
        message = f"Synthèse interrompue : {garde['motif']}."
    elif tours:
        maxi = commande[commande.index("--max-turns") + 1] if "--max-turns" in commande else "?"
        lus = sorted({o for o in ouverts if o})
        message = (f"Claude a utilisé ses {maxi} tours sans finir"
                   + (f" : il a ouvert ou parcouru {len(lus)} fichier(s) ({', '.join(lus[:4])}"
                      + ("…" if len(lus) > 4 else "") + ")" if lus else "")
                   + ". " + ("Relance « Tout traiter »." if etape != "synthese" else
                             "Relance la synthèse ; si ça se reproduit, la transcription est "
                             "peut-être trop longue pour une seule passe."))
    else:
        message = _expliquer(message, proc.returncode, demande)

    brouillon = texte_assistant or texte_deltas
    if len(brouillon.strip()) > 200:
        try:
            (t.dossier / PARTIEL_SYNTHESE).write_text(brouillon, encoding="utf-8")
            message += f" Le texte déjà rédigé est dans {PARTIEL_SYNTHESE}."
        except Exception:
            pass
    message += f" Détail : {journal.name} dans {lieu}."
    noter(f"claude : échec (code {proc.returncode}) — {message[:300]}")
    raise (ConnexionClaude if connexion else ToursEpuises if tours else RuntimeError)(message[:800])


def preparer_atelier(t: Traitement, corps: str, transcription: Path) -> Path:
    """Crée le répertoire de travail de cette synthèse (supports du cours) et
    y écrit ce que Claude recevra sur l'entrée standard : consignes,
    supports, transcription. Renvoie ce fichier ; son dossier est le cwd.
    Les ateliers de plus d'un jour sont effacés au passage : le dernier reste
    disponible pour diagnostic."""
    ATELIER.mkdir(parents=True, exist_ok=True)
    for vieux in ATELIER.glob("cours-*"):
        try:
            if time.time() - vieux.stat().st_mtime > 24 * 3600:
                shutil.rmtree(vieux, ignore_errors=True)
        except OSError:
            pass
    atelier = Path(tempfile.mkdtemp(prefix=f"cours-{time.strftime('%Y%m%d-%H%M%S')}-",
                                    dir=ATELIER))
    copie = atelier / "supports"
    if t.supports:
        copie.mkdir(parents=True, exist_ok=True)
        for s in t.supports:
            try:
                shutil.copyfile(t.dossier / "supports" / s.fichier, copie / s.fichier)
            except OSError as exc:
                noter(f"support {s.fichier} non copié : {exc}")

    # Syllabus et diapositives repérés à leur nom : Claude sait ce qui fait foi.
    bloc = sup.bloc_pour_claude(t.supports, t.dossier / "supports",
                                debut=t.debut, duree=t.duree, racine=copie,
                                roles={s.origine: sup.role_document(s.origine) for s in t.supports})
    texte = transcription.read_text(encoding="utf-8", errors="replace").strip()
    entree = atelier / "entree_claude.txt"
    entree.write_text(
        "<consignes>\n" + corps.strip() + "\n</consignes>\n\n"
        + (bloc + "\n\n" if bloc else "")
        + "<transcription>\n" + texte + "\n</transcription>\n",
        encoding="utf-8")
    return entree


def executer_claude(t: Any, prompt: str, entree: Path, jalons: Jalons, *,
                    outils: tuple[str, ...] = (), tours: int = 12) -> tuple[str, dict[str, Any]]:
    """Lance Claude Code sur `entree` (stdin) avec le modèle et l'effort de
    t.options, en se repliant sur des options plus anciennes si le CLI ne les
    connaît pas, et en le mettant à jour s'il refuse le modèle."""
    etape = getattr(t, "etape_claude", "synthese")
    claude = chemin_claude()
    if not claude:
        raise RuntimeError(
            "Claude Code est introuvable. Relance Installer.bat, ou installe-le "
            "avec : npm install -g @anthropic-ai/claude-code")

    # Une mise à jour lancée au démarrage est peut-être encore en train de
    # remplacer le binaire : on ne l'exécute pas avant qu'elle ait fini.
    if not _MAJ_CLAUDE_FINIE.is_set():
        t.maj(etape, statut="en_cours", detail="Mise à jour de Claude Code…")
        _MAJ_CLAUDE_FINIE.wait(600)

    base = [claude, "-p", prompt, "--max-turns", str(tours)]
    if outils:
        base += ["--allowedTools", ",".join(outils)]
    modele, effort = reglages_claude(t.options)
    if modele:
        base += ["--model", modele]
    if effort:
        base += ["--effort", effort]
    # Du plus bavard au plus sobre : chaque repli perd un peu d'avancement
    # mais reste fonctionnel sur une version plus ancienne du CLI.
    tentatives = [
        base + ["--output-format", "stream-json", "--verbose",
                "--include-partial-messages"],
        base + ["--output-format", "stream-json", "--verbose"],
        base + ["--output-format", "json"],
    ]
    # Options dont on peut se passer si le CLI ne les connaît pas encore.
    facultatives = ["--effort"] if effort else []

    def sans(commande: list[str], drapeau: str) -> list[str]:
        i = commande.index(drapeau)
        return commande[:i] + commande[i + 2:]

    def essayer() -> tuple[str, dict[str, Any]]:
        nonlocal tentatives
        derniere: Optional[Exception] = None
        i = 0
        while i < len(tentatives):
            try:
                return _appeler_claude(t, tentatives[i], entree, jalons)
            except _OptionInconnue as exc:
                derniere = exc
                gene = next((d for d in facultatives if d.lstrip("-") in str(exc)), None)
                if gene:
                    facultatives.remove(gene)
                    tentatives = [sans(c, gene) for c in tentatives]
                    remarque = (f"Claude Code ne connaît pas encore {gene} : "
                                "synthèse lancée sans ce réglage.")
                    noter(remarque)
                    t.avertissements.append(remarque)
                    continue
                i += 1
        raise RuntimeError(str(derniere)[:400] if derniere else
                           "Claude Code n'a accepté aucun format de sortie.")

    try:
        return essayer()
    except _CliObsolete as exc:
        # L'API refuse le modèle à cette version du CLI : on met à jour et
        # on refait un essai, une seule fois.
        nouvelle = mettre_a_jour_claude(
            claude, lambda d: t.maj(etape, statut="en_cours", detail=d))
        if not nouvelle:
            raise RuntimeError(
                "Claude Code est trop ancien pour ce modèle et sa mise à jour "
                "automatique a échoué. Dans une invite de commandes : "
                "claude update  (ou : npm install -g @anthropic-ai/claude-code@latest), "
                f"puis relance. ({str(exc)[:120]})") from exc
        t.maj(etape, statut="en_cours", detail=f"Claude Code {nouvelle} — nouvel essai")
        try:
            return essayer()
        except _CliObsolete as exc2:
            raise RuntimeError(
                f"Claude Code {nouvelle} refuse toujours ce modèle. "
                f"({str(exc2)[:200]})") from exc2


def synthetiser(t: Traitement, transcription: Path) -> Path:
    t.maj("synthese", statut="en_cours", detail="Claude lit la transcription")

    # Les consignes sont relues à chaque synthèse : une modification du
    # fichier s'applique au cours suivant, ou à « Relancer la synthèse ».
    if not t.consignes or not t.consignes.is_file():
        raise RuntimeError("Le fichier de consignes a disparu. Choisis un modèle dans la "
                           "liste « Consignes de synthèse » et relance.")
    regles = entete.lire(t.consignes)
    if not regles.corps.strip():
        raise RuntimeError(f"Le fichier de consignes « {t.consignes.stem} » est vide.")
    try:  # trace : quelles consignes ont produit ce support
        shutil.copyfile(t.consignes, t.dossier / "consignes.md")
    except OSError as exc:
        noter(f"copie des consignes impossible : {exc}")
    jalons = Jalons.depuis(regles.corps)
    entree = preparer_atelier(t, regles.corps, transcription)

    # 12 tours suffisent à rédiger ; chaque image ou tranche de PDF à lire
    # en demande un de plus.
    lectures = 0
    for s in sup.a_lire(t.supports):
        pages = re.search(r"\d+", s.detail) if s.genre == "pdf" else None
        lectures += 1 + (int(pages.group(0)) // 15 if pages else 0)
    modele, effort = reglages_claude(t.options)
    t.maj("synthese", detail="Claude lit la transcription"
          + (f" · {modele}" if modele else "") + (f" · effort {effort}" if effort else ""))
    markdown, usage = executer_claude(t, consigne(bool(t.supports)), entree, jalons,
                                      outils=("Read",) if lectures else (),
                                      tours=min(80, 12 + lectures))

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

    t.maj("pdf", statut="en_cours", avancement=0.0, detail="Rendu en cours")
    commande = [sys.executable, str(PDF_SCRIPT), str(markdown)]
    if t.consignes and t.consignes.is_file():
        commande += ["--consignes", str(t.consignes)]   # l'en-tête `pdf:` règle la page
    proc = suivre(subprocess.Popen(
        commande,
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
    remarques = [l.strip() for l in (err or b"").decode("utf-8", "replace").splitlines()
                 if l.strip().startswith("Consignes")]
    for r in remarques:
        if r not in t.avertissements:
            t.avertissements.append(r)
    t.maj("pdf", statut="fait", avancement=1.0, detail=f"{pdf.stat().st_size / 1024:.0f} Ko"
          + (" · réglages à vérifier" if remarques else ""))


# ----------------------------------------------------------------------
# Mode année : un dossier par année d'études, l'horaire classe les
# enregistrements par matière, une matière = un seul cours suivi
# ----------------------------------------------------------------------
NOTES_SEANCE = "Notes de séance"
FICHES_MATIERE = "Fiches matière"
INFOS_MATIERE = "Infos matière"
LECTURE_HORAIRE = "Horaire"
TRAVAIL = an.DOSSIER_TECHNIQUE          # « _Transcriptor » dans chaque matière
BUDGET_SUPPORTS_FICHES = 170_000        # caractères de supports reproduits pour les fiches
BUDGET_NOTES_LOT = 150_000              # caractères de notes de séance par passage de Claude
BUDGET_SUPPORTS_INFOS = 60_000


def installer_modeles_annee() -> None:
    """Comme installer_modeles(), pour les consignes du mode année
    (Documents\\Transcriptor\\Consignes\\Année)."""
    CONSIGNES_ANNEE.mkdir(parents=True, exist_ok=True)
    registre = CONSIGNES_ANNEE / ".modeles_livres"
    try:
        livres = set(registre.read_text(encoding="utf-8").split("\n")) - {""}
    except OSError:
        livres = set()
    change = False
    for src in sorted(MODELES_ANNEE_APP.glob("*.md")) if MODELES_ANNEE_APP.is_dir() else []:
        cible = CONSIGNES_ANNEE / src.name
        if not cible.exists() and src.name not in livres:
            shutil.copyfile(src, cible)
            noter(f"consignes année : modèle « {src.stem} » installé")
            change = True
        if src.name not in livres:
            livres.add(src.name)
            change = True
    if change:
        registre.write_text("\n".join(sorted(livres)) + "\n", encoding="utf-8")


def consignes_annee(nom: str) -> Path:
    """Le modèle `nom` du mode année : celui de l'utilisateur (modifiable),
    sinon celui livré avec l'application."""
    try:
        installer_modeles_annee()
    except Exception as exc:  # noqa: BLE001
        noter(f"consignes année : installation impossible ({exc})")
    for base in (CONSIGNES_ANNEE, MODELES_ANNEE_APP):
        p = base / f"{nom}.md"
        if p.is_file():
            return p
    raise FileNotFoundError(f"Consignes « {nom} » introuvables dans {CONSIGNES_ANNEE}.")


# La fiche « Infos » se rédige d'après le texte reproduit : ouvrir chaque
# article pour en lire le titre épuisait les tours de Claude.
OUVRIR_INFOS = (" N'ouvre un document avec Read que pour y chercher une information pratique "
                "absente du texte reproduit, comme une date d'examen ou une consigne de travail ; "
                "pour citer une ressource, le début reproduit suffit.")
SANS_LECTURE = " N'ouvre aucun fichier : tout ce qui est utile est reproduit dans l'entrée."


def consigne_annee(genre: str) -> str:
    """Le prompt de `claude -p` pour une étape du mode année. Pas de chevrons
    ni de guillemets droits : claude.cmd passe par cmd.exe."""
    debut = "L'entrée standard contient, entre balises : consignes, les règles à appliquer ; "
    fin = (" Réponds uniquement par le Markdown final, sans préambule ni commentaire. "
           "Personne ne pourra te répondre : ne pose aucune question.")
    if genre == "horaire":
        return (debut + "horaire, le fichier d'horaire et, quand il a pu être extrait, son texte. "
                "Si un fichier est indiqué, lis-le avec l'outil Read pour voir sa mise en page et "
                "ses couleurs. Réponds uniquement par le JSON demandé, sans aucun autre texte.")
    if genre == "notes":
        return (debut + "seance, la description de la séance ; vocabulaire, les termes tirés des "
                "supports de la matière ; transcription, la transcription automatique de la "
                "séance. Produis les notes décrites par les consignes." + fin)
    if genre == "infos":
        return (debut + "matiere ; infos, les informations pratiques relevées séance par séance ; "
                "supports, les documents de la matière. Rédige la fiche décrite par les consignes."
                + OUVRIR_INFOS + fin)
    if genre == "infos_ajout":
        return (debut + "mission, ce que tu dois faire ; infos_actuelles, la fiche telle qu'elle "
                "est ; matiere ; infos, les informations des nouvelles séances seulement ; "
                "supports, les nouveaux documents seulement. Réponds par la fiche complète mise "
                "à jour, en suivant la mission et les consignes." + OUVRIR_INFOS + fin)
    if genre == "fiches_ajout":
        return (debut + "mission, ce que tu dois faire ; matiere ; notes, les nouvelles séances "
                "seulement ; supports, les nouveaux documents seulement. Les fiches existantes "
                "sont dans le répertoire courant, un fichier par catégorie, décrits dans la "
                "mission. Intègre le nouveau en suivant la mission, avec les outils Read, Edit et "
                "Write. Quand tout est intégré, réponds seulement TERMINÉ, ou RIEN À AJOUTER si "
                "rien n'était nouveau. Personne ne pourra te répondre : ne pose aucune question.")
    return (debut + "matiere ; notes, les notes de chaque séance ; supports, les documents de la "
            "matière, à lire avec l'outil Read quand ils sont listés comme fichiers et utiles. "
            "Rédige le document décrit par les consignes et écris-le avec l'outil Write dans le "
            "répertoire courant : un fichier par catégorie, nommés 010.md, 020.md, 030.md et "
            "ainsi de suite dans l'ordre, chaque fichier commençant par son titre de catégorie, "
            "et le lexique dans 990.md. N'écris aucun autre fichier. Quand tout est écrit, "
            "réponds seulement TERMINÉ. Personne ne pourra te répondre : ne pose aucune question.")


MISSION_AJOUT_FICHES = """Les fiches de cette matière existent déjà. Elles sont dans le répertoire courant ({atelier}), un fichier par catégorie :

{plan}

Tu reçois seulement ce qui est nouveau : {quoi}. Intègre-le aux fiches existantes, comme si les fiches avaient été rédigées d'emblée avec :

1. Chaque notion nouvelle va à sa place logique : complète une sous-catégorie existante, ajoute une sous-catégorie (###) dans la bonne catégorie, ou, si elle ouvre un nouveau thème, crée une catégorie : un nouveau fichier dont le numéro la place au bon endroit (025.md entre 020.md et 030.md), qui commence par son titre ##.
2. Une notion déjà traitée est enrichie (précision, exemple, nuance, insistance de l'enseignant), jamais répétée ailleurs.
3. Lis avec l'outil Read seulement les fichiers que tu vas modifier, puis modifie-les avec l'outil Edit. Ne réécris pas un fichier entier pour y ajouter quelques lignes. Ne supprime rien de juste : corrige seulement ce que le nouveau contredit ou précise.
4. Complète le lexique ({lexique}) avec les nouveaux termes, en gardant l'ordre alphabétique.
5. Respecte toutes les conventions des consignes : niveaux de titres, mots-clés en gras, encadrés, exemples concrets, tableaux, schémas.{revues}

Quand tout est intégré, réponds seulement TERMINÉ — ou RIEN À AJOUTER si le nouveau n'apporte rien que les fiches ne disent déjà."""

MISSION_AJOUT_INFOS = """La fiche « Infos importantes » de cette matière existe déjà (infos_actuelles). Tu reçois seulement ce qui est nouveau : {quoi}. Réponds par la fiche complète mise à jour :

1. Garde tout ce qui reste vrai, avec ses sources entre parenthèses.
2. Ajoute les informations nouvelles à leur section ; quand une information nouvelle remplace une ancienne (date d'examen déplacée, consigne modifiée…), garde la plus récente et signale le changement.
3. Respecte les sections et les conventions des consignes."""


def nom_fichier_matiere(m: an.Matiere) -> str:
    return an.nom_dossier(m.titre, 50)


def ecrire_atomique(chemin: Path, texte: str) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_name(chemin.name + ".tmp")
    tmp.write_text(texte, encoding="utf-8")
    os.replace(tmp, chemin)


def texte_de_transcription(chemin: Path) -> str:
    """Le corps d'une transcription, sans son en-tête (Cours, Fichier…)."""
    texte = chemin.read_text(encoding="utf-8", errors="replace")
    entete_, sep, corps = texte.partition("\n\n")
    if sep and re.match(r"^(Cours|Fichier)\s*:", entete_):
        return corps.strip()
    return texte.strip()


def section_markdown(md: str, titre: str) -> str:
    """Le contenu d'une section « ## titre » (jusqu'à la section suivante)."""
    cle = an.plat(titre)
    sortie: list[str] = []
    dedans = False
    for ligne in md.splitlines():
        m = re.match(r"^(#{1,2})\s+(.*)$", ligne)
        if m:
            if dedans:
                break
            dedans = an.plat(m.group(2)).startswith(cle)
            continue
        if dedans:
            sortie.append(ligne)
    return "\n".join(sortie).strip()


def sauvegarder_version(fichier: Path, dossier: Path, garder: int = 8) -> None:
    """Avant de remplacer un document Word, sa version actuelle (peut-être
    annotée à la main) part dans _Transcriptor\\versions."""
    if not fichier.is_file():
        return
    try:
        dossier.mkdir(parents=True, exist_ok=True)
        date = datetime.datetime.fromtimestamp(fichier.stat().st_mtime)
        cible = dossier / f"{fichier.stem} ({date:%Y-%m-%d %Hh%M}){fichier.suffix}"
        if not cible.exists():
            shutil.copy2(fichier, cible)
        anciennes = sorted(dossier.glob(f"{an.glob_echappe(fichier.stem)} (*){fichier.suffix}"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        for vieux in anciennes[garder:]:
            try:
                vieux.unlink()
            except OSError:
                pass
    except OSError as exc:
        noter(f"version précédente de {fichier.name} non gardée : {exc}")


def produire_word(md: Path, sortie: Path, consignes: Path, m: an.Matiere, *,
                  sous_titre: str, travail: Path) -> tuple[Path, list[str]]:
    """Markdown -> Word (generate_docx.py, dans un processus à part comme le PDF)."""
    module_requis("docx", "python-docx")
    sauvegarder_version(sortie, travail / "versions")
    commande = [sys.executable, str(DOCX_SCRIPT), str(md), "--sortie", str(sortie),
                "--consignes", str(consignes), "--titre", m.nom, "--sous-titre", sous_titre,
                "--couleur", m.couleur, "--etiquette", m.ue, "--cache", str(VENDOR),
                "--images", str(travail / "schemas")]
    proc = suivre(subprocess.Popen(commande, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=NO_WINDOW))
    try:
        sortie_std, erreurs = proc.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        tuer(proc, "mise en page Word trop longue")
        sortie_std, erreurs = proc.communicate()
    lignes = (sortie_std or b"").decode("utf-8", "replace").strip().splitlines()
    if proc.returncode != 0 or not lignes:
        dernieres = (erreurs or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("Mise en page Word impossible : "
                           + (dernieres[-1][:200] if dernieres else f"code {proc.returncode}"))
    info = json.loads(lignes[-1])
    avert = list(info.get("avertissements") or [])
    if info.get("schemas_rates"):
        avert.append(f"{info['schemas_rates']} schéma(s) remplacé(s) par du texte")
    return Path(info["sortie"]), avert


class EtapeAnnee:
    """Ce qu'executer_claude() attend d'un traitement, pour une tâche du mode
    année : la progression va à la tâche correspondante du chantier."""
    etape_claude = "claude"

    def __init__(self, chantier: "Chantier", cle: str, travail: Path,
                 options: dict[str, Any], consignes: Optional[Path] = None):
        self.chantier, self.cle = chantier, cle
        self.dossier = travail
        self.options = options
        self.consignes = consignes
        self.journal_claude = travail / "claude.log"
        self.lieu_journal = f"« {travail} »"
        self.supports: list[sup.Support] = []
        self.avertissements = chantier.remarques
        self.processus: Optional[subprocess.Popen] = None
        chantier.etapes_claude.append(self)

    def maj(self, etape: str, *, statut: Optional[str] = None,
            avancement: Optional[float] = None, detail: Optional[str] = None) -> None:
        self.chantier.maj_tache(self.cle, statut=statut, avancement=avancement, detail=detail)


class _PlanSeance:
    def __init__(self, seance: an.Seance, notes: Path, audios: list[tuple[an.Classement, Path]]):
        self.seance, self.notes, self.audios = seance, notes, audios
        self.echec = False


class _PlanMatiere:
    def __init__(self, m: an.Matiere, dossier: Path, sources: list[Path]):
        self.m, self.dossier, self.sources = m, dossier, sources
        self.travail = dossier / TRAVAIL
        self.seances: list[_PlanSeance] = []
        self.supports: Optional[list[sup.Support]] = None
        self.vocabulaire = ""
        self.echec = False
        self.reecrire = False            # « Tout réécrire » : repartir de zéro

    def documents(self) -> dict[str, Path]:
        """Documents du dossier de la matière, par chemin relatif."""
        sortie = {}
        for p in self.sources:
            try:
                sortie[p.relative_to(self.dossier).as_posix()] = p
            except ValueError:
                sortie[p.name] = p
        return sortie

    def notes(self) -> dict[str, Path]:
        return {ps.seance.cle: ps.notes for ps in self.seances}

    def etat(self) -> suivi.EtatMatiere:
        return etat_matiere(self.travail, self.notes(), self.documents())


def trier_documents(sources: list[Path]) -> tuple[list[Path], list[Path]]:
    """Les documents d'une matière que Transcriptor sait lire, et ceux trouvés
    mais à convertir d'abord (.ppt, .doc, .odt…) : ces derniers sont montrés
    dans l'interface, jamais comptés comme intégrés aux fiches."""
    lisibles, a_convertir = [], []
    for p in sources:
        genre = sup.genre_de(p)
        (a_convertir if genre in ("a_convertir", "") else lisibles).append(p)
    return lisibles, a_convertir


ORDRE_ROLES = {"syllabus": 0, "diapositives": 1, "": 2}


def etat_matiere(travail: Path, notes: dict[str, Path], documents: dict[str, Path]) -> suivi.EtatMatiere:
    """Ce que contiennent déjà les fiches et les infos d'une matière (un
    etat.json d'avant est traduit au passage)."""
    return suivi.EtatMatiere.charger(
        travail, notes, documents,
        signature_v1=lambda: signature_matiere(list(notes.values()), list(documents.values())))


def signature_matiere(notes: list[Path], sources: list[Path]) -> str:
    return an.signature([*notes, *sources])


class Chantier:
    """Un passage sur une année : lecture de l'horaire, ou traitement des
    matières (transcriptions sur la carte graphique et rédactions par Claude
    menées en parallèle), ou remise en page des documents Word."""

    def __init__(self, annee_: an.Annee, options: dict[str, Any], notifier):
        self.id = next(_NUMEROS)
        self.annee = annee_
        self.options = options
        self.notifier = notifier
        self.genre = options.get("genre") or "tout"      # tout | horaire | word
        self.arret = threading.Event()
        self.etat = "en_cours"
        self.erreur: Optional[str] = None
        self.remarques: list[str] = []
        self.taches: dict[str, dict[str, Any]] = {}
        self.plan: list[_PlanMatiere] = []
        self.horaire: Optional[an.Horaire] = None
        self.verrou = threading.RLock()
        self.cond = threading.Condition(self.verrou)
        self._verrou_supports = threading.Lock()
        self.gpu_fini = False
        self.claude_bloque = ""
        self.connexion = False          # Claude Code pas connecté : bouton « Se connecter »
        self.etapes_claude: list[EtapeAnnee] = []
        self._dernier_envoi = 0.0
        self.debut = time.time()

    # -- suivi --------------------------------------------------------------
    def tache(self, cle: str, genre: str, matiere: str, libelle: str) -> None:
        with self.verrou:
            self.taches[cle] = {"cle": cle, "genre": genre, "matiere": matiere, "libelle": libelle,
                                "statut": "attente", "avancement": 0.0, "detail": ""}

    def maj_tache(self, cle: str, *, statut: Optional[str] = None,
                  avancement: Optional[float] = None, detail: Optional[str] = None) -> None:
        with self.verrou:
            t = self.taches.get(cle)
            if not t:
                return
            if statut is not None:
                t["statut"] = statut
            if avancement is not None:
                t["avancement"] = max(0.0, min(1.0, float(avancement)))
            if detail is not None:
                t["detail"] = str(detail)[:160]
        self.pousser(force=statut is not None)

    def remarque(self, texte: str) -> None:
        noter("année : " + texte)
        with self.verrou:
            if texte not in self.remarques:
                self.remarques.append(texte)

    def pousser(self, force: bool = False) -> None:
        maintenant = time.time()
        if not force and maintenant - self._dernier_envoi < 0.5:
            return
        self._dernier_envoi = maintenant
        try:
            self.notifier(self.instantane())
        except Exception:  # noqa: BLE001
            pass

    def instantane(self) -> dict[str, Any]:
        with self.verrou:
            return {"id": self.id, "genre": self.genre, "etat": self.etat, "erreur": self.erreur,
                    "connexion": self.connexion,
                    "remarques": list(self.remarques)[-30:],
                    "taches": [dict(t) for t in self.taches.values()],
                    "duree": int(time.time() - self.debut)}

    def en_cours(self) -> bool:
        return self.etat == "en_cours"

    # -- déroulement ----------------------------------------------------------
    def lancer(self) -> None:
        threading.Thread(target=self._executer, daemon=True).start()

    def arreter(self) -> None:
        self.arret.set()
        for e in list(self.etapes_claude):
            if e.processus is not None:
                tuer(e.processus, "arrêt demandé")
        with self.cond:
            self.cond.notify_all()

    def _executer(self) -> None:
        noter(f"année : chantier {self.genre} sur {self.annee.racine}")
        try:
            if self.genre == "horaire":
                self._lire_horaire()
            else:
                self._planifier()
                if self.genre == "word":
                    self._refaire_word()
                else:
                    gpu = threading.Thread(target=self._fil_gpu, daemon=True)
                    gpu.start()
                    self._fil_claude()
                    gpu.join()
                    if not self.arret.is_set() and self.annee.ranger_traites():
                        self._ranger()
            with self.verrou:
                for t in self.taches.values():
                    if t["statut"] == "en_cours":
                        t["statut"], t["detail"] = "attente", "interrompue"
                self.etat = "arrete" if self.arret.is_set() else "fini"
        except Exception as exc:  # noqa: BLE001
            noter("année : échec — " + traceback.format_exc()[-800:])
            self.connexion = self.connexion or isinstance(exc, ConnexionClaude)
            self.etat, self.erreur = "erreur", _lisible(exc)
        finally:
            noter(f"année : chantier terminé ({self.etat})")
            self.pousser(force=True)

    def _ranger(self) -> None:
        """Range les enregistrements déjà dans les fiches et les infos de leur
        matière dans Traités\\AAAA-MM, les « pas un cours » et les doublons
        dans Écartés : à la racine du dossier des audios ne restent que les
        nouveaux. Le registre suit chaque audio : rien n'est refait."""
        a = self.annee
        try:
            analyse = a.analyser()
            transcriptions = a.transcriptions(analyse.infos)
        except Exception as exc:  # noqa: BLE001
            self.remarque(f"Rangement des audios impossible : {_lisible(exc)[:150]}")
            return
        etats: dict[str, Optional[suivi.EtatMatiere]] = {}
        ranges: dict[str, int] = {}
        for c in analyse.classements:
            info = c.audio
            if a.est_range(info.relatif):
                continue
            if c.ignore:
                sous = an.DOSSIER_ECARTES
            elif c.classe and analyse.horaire and (m := analyse.horaire.matiere(c.matiere)):
                if c.matiere not in etats:
                    dossier = a.dossier_matiere(m)
                    seances = analyse.seances.get(m.nom, [])
                    travail = dossier / TRAVAIL
                    notes = {x.cle: travail / "seances" / f"{x.cle}.md" for x in seances}
                    documents = {}
                    lisibles = trier_documents(a.supports_matiere(m))[0] if dossier.is_dir() else []
                    for p in lisibles:
                        documents[p.relative_to(dossier).as_posix()] = p
                    etats[c.matiere] = etat_matiere(travail, notes, documents)
                etat = etats[c.matiere]
                fichier_notes = a.dossier_matiere(m) / TRAVAIL / "seances" / f"{c.seance}.md"
                if not (etat and transcriptions.get(info.relatif, Path()).is_file()
                        and etat.fiches.seance_integree(c.seance, fichier_notes)
                        and etat.infos.seance_integree(c.seance, fichier_notes)):
                    continue
                sous = f"{an.DOSSIER_TRAITES}/{info.debut:%Y-%m}" if info.debut \
                    else f"{an.DOSSIER_TRAITES}/sans date"
            else:
                continue
            try:
                a.deplacer_audio(info, sous)
                ranges[sous.split("/")[0]] = ranges.get(sous.split("/")[0], 0) + 1
            except an.CheminTropLong as exc:
                noter(f"rangement : {info.nom} laissé en place — {exc}")
            except OSError as exc:
                self.remarque(f"{info.nom} : pas rangé ({exc})")
        if ranges:
            self.remarque("Rangés dans « " + a.dossier_audios().name + " » : " + ", ".join(
                f"{n} dans {d}" for d, n in ranges.items()) + ". Ne restent à la racine que les nouveaux.")

    def _options_claude(self, regles: entete.Consignes) -> dict[str, Any]:
        """Le modèle de l'interface, sauf si les consignes de l'étape en
        imposent un (bloc claude: de leur en-tête)."""
        o = dict(self.options)
        if regles.claude.get("modele"):
            o["claude_modele"] = regles.claude["modele"]
        if regles.claude.get("effort"):
            o["claude_effort"] = regles.claude["effort"]
        return o

    def _atelier(self, nom: str) -> Path:
        ATELIER.mkdir(parents=True, exist_ok=True)
        for vieux in ATELIER.glob("annee-*"):
            try:
                if time.time() - vieux.stat().st_mtime > 24 * 3600:
                    shutil.rmtree(vieux, ignore_errors=True)
            except OSError:
                pass
        return Path(tempfile.mkdtemp(prefix=f"annee-{nom}-{time.strftime('%Y%m%d-%H%M%S')}-",
                                     dir=ATELIER))

    def _echec_claude(self, cle: str, exc: Exception) -> None:
        if self.arret.is_set():
            self.maj_tache(cle, statut="attente", detail="interrompue")
            return
        message = _lisible(exc)
        libelle = self.taches.get(cle, {}).get("libelle", cle)
        matiere = self.taches.get(cle, {}).get("matiere", "")
        self.maj_tache(cle, statut="erreur", detail=message[:160])
        self.remarque(f"{matiere} · {libelle} : {message[:300]}".lstrip(" ·"))
        bloque = ""
        if isinstance(exc, ConnexionClaude):
            self.connexion = True
            bloque = "Claude Code non connecté — connecte-le puis relance"
        elif re.search(r"limite d.utilisation|usage limit|rate limit|\b429\b", message, re.I):
            bloque = "limite Claude atteinte — relance plus tard"
        if bloque:
            self.claude_bloque = message
            with self.verrou:
                for t in self.taches.values():
                    if t["genre"] in ("notes", "fiches", "infos") and t["statut"] == "attente":
                        t["statut"], t["detail"] = "ignore", bloque

    # -- horaire -------------------------------------------------------------
    def _lire_horaire(self) -> None:
        a = self.annee
        src = a.source_horaire()
        self.tache("h", "horaire", "", "Lecture de l'horaire")
        if not src:
            raise RuntimeError("Aucun horaire trouvé : dépose le fichier d'horaire (PDF, image, "
                               f"Excel…) dans « {a.dossier_audios().name} », puis relance.")
        self.maj_tache("h", statut="en_cours", detail=src.name)
        ancien = a.charger_horaire()
        ext = src.suffix.lower()
        texte_extrait = ""
        brut: Any = {}
        if ext == ".json":
            brut = json.loads(src.read_text(encoding="utf-8-sig"))
            h = an.Horaire.depuis_dict(brut)
        elif ext == ".ics":
            h = an.horaire_depuis_ics(src.read_text(encoding="utf-8-sig", errors="replace"))
        else:
            def importer(nom: str, paquet: str) -> Any:
                return module_requis(nom, paquet, lambda d: self.maj_tache("h", detail=d))
            if ext == ".pdf":
                try:
                    pypdf = importer("pypdf", "pypdf")
                    texte_extrait = "\n".join((pg.extract_text() or "")
                                              for pg in pypdf.PdfReader(str(src)).pages)
                except Exception as exc:  # noqa: BLE001
                    noter(f"horaire : texte du PDF non extrait ({exc})")
            else:
                texte_extrait = an.horaire_en_texte(src, importer) or ""
            consignes = consignes_annee(LECTURE_HORAIRE)
            regles = entete.lire(consignes)
            atelier = self._atelier("horaire")
            lisible = ext in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif",
                              ".tiff", ".heic", ".heif")
            if lisible:
                (atelier / "horaire").mkdir()
                shutil.copyfile(src, atelier / "horaire" / f"horaire{ext}")
            bloc = [f"Fichier d'origine : {src.name}"]
            if lisible:
                bloc.append(f"Fichier à lire avec Read : horaire/horaire{ext} (chemin complet : "
                            f"{atelier / 'horaire' / ('horaire' + ext)})")
            if texte_extrait.strip():
                bloc += ["", "Texte extrait du fichier :", texte_extrait.strip()[:150_000]]
            entree = atelier / "entree_claude.txt"
            entree.write_text("<consignes>\n" + regles.corps.strip() + "\n</consignes>\n\n"
                              "<horaire>\n" + "\n".join(bloc) + "\n</horaire>\n", encoding="utf-8")
            etape = EtapeAnnee(self, "h", atelier, self._options_claude(regles), consignes)
            etape.journal_claude = a.technique / "claude_horaire.log"
            a.technique.mkdir(parents=True, exist_ok=True)
            reponse, _ = executer_claude(etape, consigne_annee("horaire"), entree, Jalons({}),
                                         outils=("Read",) if lisible else (), tours=8)
            brut = an.json_dans(reponse)
            h = an.Horaire.depuis_dict(brut)
        if not h.matieres:
            raise RuntimeError("Aucune matière lue dans l'horaire. "
                               + ("; ".join(h.avertissements[:3]) if h.avertissements else ""))
        h.source = src.name

        # Contrôle d'un horaire daté : une séance lue par plage horaire du texte.
        lues = len(brut.get("seances") or []) if isinstance(brut, dict) else 0
        if texte_extrait and lues:
            plages = len(re.findall(r"\b\d{1,2}[:hH]\d{2}\s*[-–]\s*\d{1,2}[:hH]\d{2}\b", texte_extrait))
            if plages >= 10 and abs(plages - lues) > max(2, plages // 50):
                self.remarque(f"Horaire : {lues} séances lues pour {plages} plages horaires dans "
                              "le fichier — vérifie horaire.json (bouton « Modifier »).")
        # Couleurs exactes : lues dans l'horaire lui-même (le fond de la case
        # de chaque cours dans un PDF ; l'aplat le plus proche dans une image).
        try:
            if ext == ".pdf":
                n = an.couleurs_depuis_pdf(src, h, module_requis(
                    "pdfplumber", "pdfplumber", lambda d: self.maj_tache("h", detail=d)))
                noter(f"horaire : {n} couleur(s) lue(s) dans le PDF")
            elif ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"):
                module_requis("PIL.Image", "pillow")
                from PIL import Image
                with Image.open(src) as image:
                    n = an.affiner_couleurs(h, image)
                noter(f"horaire : {n} couleur(s) recalée(s) sur l'image")
        except Exception as exc:  # noqa: BLE001
            noter(f"horaire : couleurs non recalées ({exc})")
        # Les dossiers déjà choisis pour les matières sont gardés.
        if ancien:
            for m in h.matieres:
                vieux = ancien.matiere(m.nom)
                if vieux and vieux.dossier:
                    m.dossier = vieux.dossier
                if vieux and vieux.vocabulaire and not m.vocabulaire:
                    m.vocabulaire = vieux.vocabulaire
            try:
                shutil.copyfile(a.horaire_json(), a.horaire_json().with_suffix(".json.bak"))
            except OSError:
                pass
        a.dossier_audios().mkdir(parents=True, exist_ok=True)
        a.retenir_dossier_audios(a.dossier_audios())
        a.enregistrer_horaire(h)
        for x in h.avertissements[:10]:
            self.remarque(f"Horaire : {x}")
        # Un dossier par matière, tout de suite (les dossiers existants au nom
        # proche, comme « Psycho », sont repris).
        try:
            crees = a.creer_structure(a.charger_horaire())
        except OSError as exc:
            crees = []
            self.remarque(f"Dossiers des matières non créés : {exc}")
        seances = sum(len(c.dates) for c in h.creneaux)
        self.maj_tache("h", statut="fait", avancement=1.0,
                       detail=f"{len(h.matieres)} matières"
                       + (f", {seances} séances datées" if seances else f", {len(h.creneaux)} créneaux")
                       + (f" · {len(crees)} dossier(s) créé(s)" if crees else ""))

    # -- planification ------------------------------------------------------
    def _planifier(self) -> None:
        a = self.annee
        analyse = a.analyser()
        h = analyse.horaire
        if not h or not h.matieres:
            raise RuntimeError("Pas encore d'horaire lu : clique d'abord « Lire l'horaire ».")
        self.horaire = h
        cibles = set(self.options.get("matieres") or [])
        reecrire = bool(self.options.get("reecrire")) and bool(cibles)
        if not cibles:
            a_classer = [c for c in analyse.classements if not c.classe and not c.ignore]
            if a_classer:
                self.remarque(f"{len(a_classer)} enregistrement(s) à classer : choisis leur matière "
                              "dans la liste des enregistrements, puis relance.")
        transcriptions = a.transcriptions(analyse.infos, chercher=self.genre == "tout")
        for m in h.matieres:
            if cibles and m.nom not in cibles:
                continue
            seances = analyse.seances.get(m.nom, [])
            dossier = a.dossier_matiere(m)
            sources, illisibles = trier_documents(a.supports_matiere(m))
            if illisibles and self.genre == "tout":
                self.remarque(f"{m.titre} : " + " ; ".join(
                    f"« {x.name} » — {sup.conversion(x)}" for x in illisibles[:4])
                    + (f" (et {len(illisibles) - 4} autre(s))" if len(illisibles) > 4 else ""))
            if self.genre == "word":
                if (dossier / TRAVAIL / "fiches.md").is_file() or (dossier / TRAVAIL / "infos.md").is_file():
                    pm = _PlanMatiere(m, dossier, sources)
                    self.plan.append(pm)
                    self.tache(f"w:{m.nom}", "word", m.nom, "Remise en page Word")
                continue
            fiches = dossier / f"{nom_fichier_matiere(m)} - {an.FICHES}.docx"
            infos = dossier / f"{nom_fichier_matiere(m)} - {an.INFOS}.docx"
            # Sans enregistrement, une matière n'est rédigée qu'à la demande (bouton
            # « Rédiger ») ; ensuite, « Tout traiter » suit ses nouveaux supports.
            if not seances and not (sources and (cibles or fiches.is_file())):
                continue
            pm = _PlanMatiere(m, dossier, sources)
            pm.reecrire = reecrire
            self.plan.append(pm)
            a_noter = False
            for s in seances:
                audios = [(c, transcriptions.get(c.audio.relatif) or a.transcription_de(c.audio.chemin))
                          for c in s.audios]
                ps = _PlanSeance(s, pm.travail / "seances" / f"{s.cle}.md", audios)
                pm.seances.append(ps)
                manquantes = [c for c, txt in audios if not txt.is_file()]
                for c in manquantes:
                    self.tache(f"t:{c.audio.relatif}", "transcription", m.nom,
                               f"Transcription · {s.libelle} · {c.audio.nom}")
                notes_perimees = ps.notes.is_file() and any(
                    txt.is_file() and txt.stat().st_mtime > ps.notes.stat().st_mtime
                    for _, txt in audios)
                if manquantes or not ps.notes.is_file() or notes_perimees:
                    a_noter = True
                    self.tache(f"n:{m.nom}:{s.cle}", "notes", m.nom, f"Notes · {s.libelle}")
            # Fiches et infos : seulement s'il y a du nouveau à y ajouter (une
            # séance, un document), ou à refaire de zéro.
            etat = pm.etat()
            documents = pm.documents()
            nouveau = any(not doc.seance_integree(ps.seance.cle, ps.notes)
                          for ps in pm.seances for doc in (etat.fiches, etat.infos)) \
                or any(not doc.support_integre(rel, chemin)
                       for rel, chemin in documents.items() for doc in (etat.fiches, etat.infos))
            if reecrire or a_noter or nouveau or not fiches.is_file() or not infos.is_file():
                self.tache(f"f:{m.nom}", "fiches", m.nom,
                           "Fiches résumés · tout réécrire" if reecrire else "Fiches résumés")
                self.tache(f"i:{m.nom}", "infos", m.nom, "Infos importantes")
            partis = sorted(set(etat.fiches.seances) - {ps.seance.cle for ps in pm.seances})
            if partis and not reecrire:
                self.remarque(f"{m.titre} : {len(partis)} séance(s) déjà dans les fiches n'ont plus "
                              "d'enregistrement ici (reclassées ?). « Tout réécrire » les en retire.")
        if not self.taches:
            self.remarque("Tout est à jour : aucune nouvelle séance ni aucun nouveau support "
                          "depuis le dernier passage.")
        self.pousser(force=True)

    # -- supports d'une matière ---------------------------------------------
    def _preparer_supports(self, pm: _PlanMatiere, cle: str = "") -> None:
        with self._verrou_supports:
            if pm.supports is not None:
                return
            pm.supports = []
            if not pm.sources:
                return

            def importer(nom: str, paquet: str) -> Any:
                return module_requis(nom, paquet,
                                     (lambda d: self.maj_tache(cle, detail=d)) if cle else None)
            try:
                supports, avert = sup.preparer(pm.sources, pm.travail / "supports", importer=importer,
                                               extraire_texte=True, oublier_absents=True)
                pm.supports = supports
                pm.vocabulaire = sup.vocabulaire(supports, pm.travail / "supports")
                for x in avert:
                    self.remarque(f"{pm.m.titre} : {x}")
            except Exception as exc:  # noqa: BLE001
                self.remarque(f"{pm.m.titre} : supports non préparés ({_lisible(exc)[:150]})")

    def _bloc_supports(self, pm: _PlanMatiere, atelier: Path, budget: int,
                       seulement: Optional[list[str]] = None,
                       lecture: bool = True) -> tuple[str, list[sup.Support]]:
        """Le bloc <supports> d'une matière, et les supports qu'il donne ; les
        fichiers que Claude peut lire sont copiés dans l'atelier (son
        répertoire de travail). `seulement` : les documents à donner (chemins
        relatifs au dossier de la matière), par défaut tous. `lecture=False` :
        Claude n'ouvrira rien, seul leur texte (ou son début) est donné."""
        supports_ = pm.supports or []
        if seulement is not None:
            voulus = set()
            for rel in seulement:
                try:
                    voulus.add(str((pm.dossier / rel).resolve()))
                except OSError:
                    pass
            supports_ = [x for x in supports_ if x.source in voulus]
        if not supports_:
            return "", []
        copie = atelier / "supports"
        if lecture:
            copie.mkdir(parents=True, exist_ok=True)
        for s in (supports_ if lecture else []):
            for nom in {s.fichier, s.texte} - {""}:
                try:
                    shutil.copyfile(pm.travail / "supports" / nom, copie / nom)
                except OSError as exc:
                    noter(f"support {nom} non copié : {exc}")
        # Les documents sont désignés par leur place dans le dossier de la
        # matière (un sous-dossier « Travail de groupe » dit ce qu'il contient).
        chemins = {}
        for p in pm.sources:
            try:
                chemins[p.name] = p.relative_to(pm.dossier).as_posix()
            except ValueError:
                chemins[p.name] = p.name
        # Le syllabus d'abord (plan et modalités du cours), puis les
        # diapositives, puis le reste ; à rang égal, la racine du dossier avant
        # les sous-dossiers. Le budget de texte va ainsi d'abord à l'essentiel.
        roles = {p.name: sup.role_document(p.name) for p in pm.sources}
        ordre = {p.name: (ORDRE_ROLES.get(roles[p.name], 2),
                          len(p.relative_to(pm.dossier).parts) if p.is_relative_to(pm.dossier) else 9)
                 for p in pm.sources}
        supports = sorted(supports_, key=lambda s: ordre.get(s.origine, (2, 9)))
        return sup.bloc_pour_claude(supports, pm.travail / "supports", racine=copie if lecture else None,
                                    budget=budget, chemins=chemins, roles=roles, lecture=lecture,
                                    intro="Documents rangés dans le dossier de la matière"), supports

    def _bloc_matiere(self, pm: _PlanMatiere) -> str:
        m, h = pm.m, self.horaire
        dates = [ps.seance.date for ps in pm.seances if ps.seance.date]
        def periode(jours: list) -> str:
            if not jours:
                return ""
            a, b = min(jours), max(jours)
            return (f", le {an.date_fr(a, jour=False)}" if a == b else
                    f", du {an.date_fr(a, jour=False)} au {an.date_fr(b, jour=False)}")
        lignes = [f"Matière : {m.nom}" + (f" ({m.ue})" if m.ue else ""),
                  f"Formation : {h.formation}" if h and h.formation else "",
                  f"Enseignant(s) : {m.enseignant or 'non précisé'}",
                  f"Séances enregistrées : {len(pm.seances)}" + periode(dates)]
        if h:
            prevues = sorted({x for c in h.creneaux_de(m.nom) for x in c.dates})
            if prevues:
                lignes.append(f"Séances prévues à l'horaire : {len(prevues)}" + periode(prevues))
            elif h.creneaux_de(m.nom):
                lignes.append("Créneaux : " + ", ".join(
                    f"{an.JOURS[c.jour]} {c.plage}" for c in h.creneaux_de(m.nom)))
        return "\n".join(x for x in lignes if x)

    # -- les deux fils ---------------------------------------------------------
    def _fil_gpu(self) -> None:
        try:
            for pm in self.plan:
                for ps in pm.seances:
                    for c, txt in ps.audios:
                        cle = f"t:{c.audio.relatif}"
                        if cle not in self.taches or self.taches[cle]["statut"] != "attente":
                            continue
                        if self.arret.is_set():
                            return
                        self.maj_tache(cle, statut="en_cours", detail="Préparation")
                        self._preparer_supports(pm, cle)
                        try:
                            if not c.audio.chemin.is_file():
                                raise FileNotFoundError(errno.ENOENT, "No such file or directory",
                                                        str(c.audio.chemin))
                            r = transcrire_robuste(
                                c.audio.chemin, txt, modele=self.options.get("modele") or "large-v3",
                                matiere=pm.m.nom, termes=pm.m.vocabulaire,
                                vocabulaire_supports=pm.vocabulaire,
                                debut=c.audio.debut if c.audio.precision == "heure" else None,
                                signaler=lambda a, d, cle=cle: self.maj_tache(cle, avancement=a, detail=d),
                                arret=self.arret)
                            self.maj_tache(cle, statut="fait", avancement=1.0,
                                           detail=f"{r['mots']} mots · {an.duree_lisible(r['duree'])}")
                            try:
                                self.annee.noter_transcription(c.audio, txt)
                            except OSError as exc:
                                noter(f"registre : {exc}")
                        except Interrompu:
                            self.maj_tache(cle, statut="attente", detail="interrompue")
                            return
                        except Exception as exc:  # noqa: BLE001
                            noter("année : transcription — " + traceback.format_exc()[-600:])
                            self.maj_tache(cle, statut="erreur", detail=_lisible(exc)[:160])
                            self.remarque(f"{c.audio.nom} : {_lisible(exc)[:200]}")
                            ps.echec = True
                        finally:
                            with self.cond:
                                self.cond.notify_all()
        finally:
            with self.cond:
                self.gpu_fini = True
                self.cond.notify_all()

    def _prochain(self) -> Optional[tuple[str, _PlanMatiere, Optional[_PlanSeance]]]:
        """La prochaine rédaction possible : les notes d'une séance dont tous
        les audios sont transcrits, puis les fiches d'une matière dont toutes
        les séances ont leurs notes."""
        if self.claude_bloque:
            return None
        for pm in self.plan:
            if pm.echec:
                continue
            for ps in pm.seances:
                t = self.taches.get(f"n:{pm.m.nom}:{ps.seance.cle}")
                if not t or t["statut"] != "attente":
                    continue
                if not ps.echec and all(txt.is_file() for _, txt in ps.audios):
                    return "notes", pm, ps
            t = self.taches.get(f"f:{pm.m.nom}")
            if t and t["statut"] == "attente":
                bloquees = [ps for ps in pm.seances if ps.echec or self.taches.get(
                    f"n:{pm.m.nom}:{ps.seance.cle}", {}).get("statut") in ("erreur", "ignore")]
                if bloquees:
                    pm.echec = True
                    for cle in (f"f:{pm.m.nom}", f"i:{pm.m.nom}"):
                        self.maj_tache(cle, statut="ignore",
                                       detail=f"séance du {bloquees[0].seance.libelle} en échec")
                    continue
                pretes = all(all(txt.is_file() for _, txt in ps.audios) and ps.notes.is_file()
                             and self.taches.get(f"n:{pm.m.nom}:{ps.seance.cle}", {}).get(
                                 "statut", "fait") == "fait" for ps in pm.seances)
                if pretes:
                    return "fiches", pm, None
        return None

    def _fil_claude(self) -> None:
        while not self.arret.is_set():
            with self.cond:
                travail = self._prochain()
                if travail is None:
                    if self.gpu_fini:
                        # Plus rien ne peut se débloquer : ce qui attend encore
                        # dépend d'une étape en échec.
                        for t in self.taches.values():
                            if t["genre"] in ("notes", "fiches", "infos") and t["statut"] == "attente":
                                t["statut"] = "ignore"
                                t["detail"] = t["detail"] or "dépend d'une étape en échec"
                        return
                    self.cond.wait(2)
                    continue
            genre, pm, ps = travail
            if genre == "notes":
                self._notes(pm, ps)
            else:
                self._fiches(pm)

    # -- rédactions ------------------------------------------------------------
    def _notes(self, pm: _PlanMatiere, ps: _PlanSeance) -> None:
        cle = f"n:{pm.m.nom}:{ps.seance.cle}"
        self.maj_tache(cle, statut="en_cours", detail="Préparation")
        try:
            self._preparer_supports(pm, cle)
            consignes = consignes_annee(NOTES_SEANCE)
            regles = entete.lire(consignes)
            atelier = self._atelier("notes")
            morceaux = []
            for i, (c, txt) in enumerate(ps.audios, 1):
                quand = (f" · début {c.audio.debut:%H:%M}" if c.audio.debut and c.audio.precision == "heure"
                         else "")
                morceaux.append(f"=== Enregistrement {i}/{len(ps.audios)} — {c.audio.nom}{quand}"
                                f" · {an.duree_lisible(c.audio.duree) or 'durée inconnue'} ===\n"
                                + texte_de_transcription(txt))
            titres = ps.seance.titres
            seance = "\n".join(x for x in (
                self._bloc_matiere(pm).split("\n")[0],
                f"Formation : {self.horaire.formation}" if self.horaire and self.horaire.formation else "",
                f"Enseignant(s) : {pm.m.enseignant or 'non précisé'}",
                f"Date : {ps.seance.libelle}",
                f"Enregistrements : {len(ps.audios)}",
                ("Titres donnés par l'enregistreur : " + " ; ".join(titres)) if titres else "",
            ) if x)
            entree = atelier / "entree_claude.txt"
            entree.write_text(
                "<consignes>\n" + regles.corps.strip() + "\n</consignes>\n\n"
                "<seance>\n" + seance + "\n</seance>\n\n"
                "<vocabulaire>\n" + (", ".join(x for x in (pm.m.vocabulaire, pm.vocabulaire) if x) or "—")
                + "\n</vocabulaire>\n\n"
                "<transcription>\n" + "\n\n".join(morceaux) + "\n</transcription>\n", encoding="utf-8")
            etape = EtapeAnnee(self, cle, pm.travail, self._options_claude(regles), consignes)
            md, usage = executer_claude(etape, consigne_annee("notes"), entree, Jalons({}), tours=6)
            if len(md.split()) < 40:
                raise RuntimeError(f"Notes presque vides ({len(md.split())} mots). Détail : claude.log "
                                   f"dans « {pm.travail} ».")
            ecrire_atomique(ps.notes, md.strip() + "\n")
            self.maj_tache(cle, statut="fait", avancement=1.0,
                           detail=f"{len(md.split())} mots" + (f" · {usage['modele']}" if usage.get("modele") else ""))
        except Exception as exc:  # noqa: BLE001
            noter("année : notes — " + traceback.format_exc()[-600:])
            ps.echec = not self.arret.is_set()
            self._echec_claude(cle, exc)
        finally:
            with self.cond:
                self.cond.notify_all()

    def _sous_titre(self, pm: _PlanMatiere) -> str:
        """« Dupont, Martin · 12 séances enregistrées · 8 septembre – 16 décembre 2026 »."""
        dates = [ps.seance.date for ps in pm.seances if ps.seance.date]
        n = len(pm.seances)
        morceaux = [pm.m.enseignant,
                    f"{n} séance{'s' if n > 1 else ''} enregistrée{'s' if n > 1 else ''}" if n else ""]
        if dates:
            a, b = min(dates), max(dates)
            morceaux.append(an.date_fr(a, jour=False) if a == b else
                            f"{an.date_fr(a, jour=False, annee=a.year != b.year)} – "
                            f"{an.date_fr(b, jour=False)}")
        return " · ".join(x for x in morceaux if x)

    def _sous_titre_infos(self, pm: _PlanMatiere) -> str:
        """« Dupont · Bachelier en éducation spécialisée »."""
        return " · ".join(x for x in (pm.m.enseignant, self.horaire.formation if self.horaire else "") if x)

    def _fiches(self, pm: _PlanMatiere) -> None:
        """Fiches résumés d'une matière : les séances et documents qui n'y sont
        pas encore y sont ajoutés (Claude complète les catégories concernées) ;
        sans fiches, ou pour « Tout réécrire », elles sont rédigées de zéro,
        par lots de séances si la matière est longue. Puis les infos."""
        cf, ci = f"f:{pm.m.nom}", f"i:{pm.m.nom}"
        self.maj_tache(cf, statut="en_cours", detail="Préparation")
        etat = pm.etat()
        notes: dict[str, tuple[_PlanSeance, str]] = {}
        try:
            self._preparer_supports(pm, cf)
            for ps in pm.seances:
                if ps.notes.is_file():
                    notes[ps.seance.cle] = (ps, ps.notes.read_text(encoding="utf-8", errors="replace"))
            if not notes and not pm.supports:
                raise RuntimeError("ni séance enregistrée ni support dans le dossier de la matière")
            md_fiches = pm.travail / "fiches.md"
            if pm.reecrire and md_fiches.is_file():
                sauvegarder_version(md_fiches, pm.travail / "versions")
            existant = "" if pm.reecrire else (
                md_fiches.read_text(encoding="utf-8", errors="replace") if md_fiches.is_file() else "")
            if not existant.strip():
                etat.fiches.vider()
            documents = pm.documents()
            nouvelles = [cle for cle, (ps, _) in notes.items()
                         if not etat.fiches.seance_integree(cle, ps.notes)]
            docs = [rel for rel, chemin in documents.items()
                    if not etat.fiches.support_integre(rel, chemin)]
            sortie = pm.dossier / f"{nom_fichier_matiere(pm.m)} - {an.FICHES}.docx"
            if not nouvelles and not docs and existant.strip():
                detail = "rien de nouveau"
            else:
                tailles = [(cle, len(notes[cle][1])) for cle in nouvelles]
                paquets = suivi.lots(tailles, BUDGET_NOTES_LOT) or [[]]
                for i, paquet in enumerate(paquets):
                    if self.arret.is_set():
                        return
                    if len(paquets) > 1:
                        self.maj_tache(cf, avancement=i / len(paquets),
                                       detail=f"Lot {i + 1}/{len(paquets)} · {len(paquet)} séance(s)")
                    docs_lot = docs if i == 0 else []
                    complet = not existant.strip()
                    md = self._rediger_fiches(pm, cf, existant, [notes[c] for c in paquet], docs_lot,
                                              revues=[c for c in paquet if c in etat.fiches.seances])
                    ecrire_atomique(md_fiches, md)
                    existant = md
                    le = suivi.maintenant()
                    if complet:
                        etat.fiches.vider()
                        etat.fiches.complet = le
                    else:
                        etat.fiches.ajouts += 1
                    for cle in paquet:
                        etat.fiches.noter_seance(cle, notes[cle][0].notes, notes[cle][1], le)
                    for rel in docs_lot:
                        etat.fiches.noter_support(rel, documents[rel], le)
                    etat.sauver()
                detail = (f"{len(nouvelles)} séance(s)" if nouvelles else "") + \
                    (" et " if nouvelles and docs else "") + (f"{len(docs)} document(s)" if docs else "")
                detail = ("réécrites : " if pm.reecrire else "ajout : ") + (detail or "rédaction")
            if detail != "rien de nouveau" or not sortie.is_file():
                self.maj_tache(cf, avancement=0.95, detail="Mise en page Word")
                ecrit, avert = produire_word(md_fiches, sortie, consignes_annee(FICHES_MATIERE), pm.m,
                                             sous_titre=self._sous_titre(pm), travail=pm.travail)
                for x in avert:
                    self.remarque(f"{pm.m.titre} : {x}")
            self.maj_tache(cf, statut="fait", avancement=1.0,
                           detail=f"{detail} · {suivi.mots(existant)} mots")
        except Exception as exc:  # noqa: BLE001
            noter("année : fiches — " + traceback.format_exc()[-600:])
            self._echec_claude(cf, exc)
            if not self.arret.is_set():
                self.maj_tache(ci, statut="ignore", detail="fiches en échec")
            return
        finally:
            with self.cond:
                self.cond.notify_all()
        if self.arret.is_set():
            return
        self._infos(pm, list(notes.values()), etat)

    def _rediger_fiches(self, pm: _PlanMatiere, cle: str, existant: str,
                        seances: list[tuple[_PlanSeance, str]], docs: list[str],
                        revues: list[str]) -> str:
        """Un passage de Claude sur les fiches : rédaction complète (pas de
        fiches existantes) ou ajout des `seances` et `docs` aux fiches
        existantes, découpées en une partie par catégorie dans l'atelier."""
        consignes = consignes_annee(FICHES_MATIERE)
        regles = entete.lire(consignes)
        atelier = self._atelier("fiches")
        ajout = bool(existant.strip())
        avant = suivi.mots(existant)
        if ajout:
            for nom, texte in suivi.nommer(suivi.decouper(existant)).items():
                (atelier / nom).write_text(texte, encoding="utf-8")
        blocs_notes = "\n\n".join(f'<seance date="{ps.seance.date or "inconnue"}">\n'
                                  f"{texte.strip()}\n</seance>" for ps, texte in seances)
        bloc_sup, remis = self._bloc_supports(pm, atelier, BUDGET_SUPPORTS_FICHES, seulement=docs)
        if ajout:
            quoi = " et ".join(x for x in (
                f"{len(seances)} séance(s)" if seances else "",
                f"{len(docs)} document(s) du dossier de la matière" if docs else "") if x)
            libelles = {ps.seance.cle: ps.seance.libelle for ps, _ in seances}
            mission = MISSION_AJOUT_FICHES.format(
                atelier=atelier, plan=suivi.plan(suivi.parties_dans(atelier)), quoi=quoi or "rien",
                lexique=suivi.LEXIQUE,
                revues=("\n6. Déjà intégrée(s) auparavant, avec des notes depuis revues : "
                        + ", ".join(libelles.get(c, c) for c in revues)
                        + ". Corrige ce qui la concerne, sans rien dupliquer.") if revues else "")
            tete = "<mission>\n" + mission + "\n</mission>\n\n"
        else:
            tete = ("<sortie>\nÉcris les fichiers 010.md, 020.md… et 990.md (lexique) dans ce "
                    f"dossier (le répertoire courant) : {atelier}\n</sortie>\n\n")
        entree = atelier / "entree_claude.txt"
        entree.write_text(
            "<consignes>\n" + regles.corps.strip() + "\n</consignes>\n\n" + tete
            + "<matiere>\n" + self._bloc_matiere(pm) + "\n</matiere>\n\n"
            + "<notes>\n" + (blocs_notes or ("Aucune nouvelle séance." if ajout else
                                             "Aucune séance enregistrée : rédige à partir des supports."))
            + "\n</notes>\n\n" + (bloc_sup + "\n" if bloc_sup else ""), encoding="utf-8")
        etape = EtapeAnnee(self, cle, pm.travail, self._options_claude(regles), consignes)
        etape.supports = remis
        # Un tour par fichier écrit ou complété, et de quoi ouvrir chaque
        # document remis (un long PDF se lit en plusieurs fois).
        reponse, usage = executer_claude(etape, consigne_annee("fiches_ajout" if ajout else "fiches"),
                                         entree, Jalons({}), outils=("Read", "Write", "Edit"),
                                         tours=min(150, 60 + 3 * len(remis)))
        parties = suivi.parties_dans(atelier)
        md = suivi.assembler(parties) if parties else reponse.strip() + "\n"
        lieu = f"Détail : claude.log dans « {pm.travail} »."
        if not ajout:
            if suivi.mots(md) < 150:
                raise RuntimeError(f"Claude n'a pas écrit les fiches ({suivi.mots(md)} mots). {lieu}")
            return md
        if suivi.mots(md) < 0.9 * avant:
            raise RuntimeError(f"Les fiches complétées sont plus courtes que les précédentes "
                               f"({suivi.mots(md)} mots contre {avant}) : elles n'ont pas été "
                               f"remplacées. {lieu}")
        inchange = re.sub(r"\s+", " ", md).strip() == re.sub(r"\s+", " ", existant).strip()
        if inchange and "RIEN" not in reponse.upper():
            raise RuntimeError(f"Claude n'a rien ajouté aux fiches. {lieu}")
        return md

    def _infos(self, pm: _PlanMatiere, notes: list[tuple[_PlanSeance, str]],
               etat: suivi.EtatMatiere) -> bool:
        """La fiche « Infos importantes » : complétée avec les informations des
        nouvelles séances et des nouveaux documents, ou rédigée de zéro."""
        ci = f"i:{pm.m.nom}"
        self.maj_tache(ci, statut="en_cours", detail="Préparation")
        try:
            consignes = consignes_annee(INFOS_MATIERE)
            regles = entete.lire(consignes)
            md_infos = pm.travail / "infos.md"
            existant = "" if pm.reecrire else (
                md_infos.read_text(encoding="utf-8", errors="replace") if md_infos.is_file() else "")
            if not existant.strip():
                etat.infos.vider()
            documents = pm.documents()
            nouvelles = [(ps, texte) for ps, texte in notes
                         if not etat.infos.seance_integree(ps.seance.cle, ps.notes)]
            docs = [rel for rel, chemin in documents.items()
                    if not etat.infos.support_integre(rel, chemin)]
            sortie = pm.dossier / f"{nom_fichier_matiere(pm.m)} - {an.INFOS}.docx"
            if not nouvelles and not docs and existant.strip():
                if not sortie.is_file():
                    produire_word(md_infos, sortie, consignes, pm.m,
                                  sous_titre=self._sous_titre_infos(pm), travail=pm.travail)
                self.maj_tache(ci, statut="fait", avancement=1.0, detail="rien de nouveau")
                return True
            atelier = self._atelier("infos")
            releves = []
            for ps, texte in nouvelles:
                infos = section_markdown(texte, "Infos pratiques et examen") or "Néant."
                signaux = [l.strip() for l in texte.splitlines() if "🎯" in l]
                releves.append(f"### Séance du {ps.seance.libelle}\n{infos}"
                               + ("\n\nPoints sur lesquels l'enseignant a insisté :\n" + "\n".join(signaux)
                                  if signaux else ""))
            ajout = bool(existant.strip())
            tete = ""
            if ajout:
                quoi = " et ".join(x for x in (
                    f"{len(nouvelles)} séance(s)" if nouvelles else "",
                    f"{len(docs)} document(s)" if docs else "") if x)
                tete = ("<mission>\n" + MISSION_AJOUT_INFOS.format(quoi=quoi) + "\n</mission>\n\n"
                        "<infos_actuelles>\n" + existant.strip() + "\n</infos_actuelles>\n\n")

            def composer(atelier: Path, bloc_sup: str) -> Path:
                entree = atelier / "entree_claude.txt"
                entree.write_text(
                    "<consignes>\n" + regles.corps.strip() + "\n</consignes>\n\n" + tete
                    + "<matiere>\n" + self._bloc_matiere(pm) + "\n</matiere>\n\n"
                    "<infos>\n" + ("\n\n".join(releves) or ("Aucune nouvelle séance." if ajout
                                                           else "Aucune séance enregistrée."))
                    + "\n</infos>\n\n" + (bloc_sup + "\n" if bloc_sup else ""), encoding="utf-8")
                return entree

            genre = "infos_ajout" if ajout else "infos"
            bloc_sup, remis = self._bloc_supports(pm, atelier, BUDGET_SUPPORTS_INFOS, seulement=docs)
            etape = EtapeAnnee(self, ci, pm.travail, self._options_claude(regles), consignes)
            etape.supports = remis
            try:
                # La fiche est une réponse en un message, mais Claude peut ouvrir
                # quelques documents (fin d'un syllabus tronqué, PDF scanné) :
                # deux tours par document remis.
                md, usage = executer_claude(etape, consigne_annee(genre), composer(atelier, bloc_sup),
                                            Jalons({}), outils=("Read",) if remis else (),
                                            tours=min(40, 8 + 2 * len(remis)) if remis else 6)
            except ToursEpuises as exc:
                # Il a passé ses tours à ouvrir des documents : la fiche est
                # refaite d'après leur texte reproduit, sans rien ouvrir.
                noter(f"année : infos {pm.m.nom} — {exc} — reprise sans ouvrir de fichier")
                self.maj_tache(ci, detail="Reprise d'après le texte des documents")
                atelier = self._atelier("infos")
                bloc_sup, _ = self._bloc_supports(pm, atelier, BUDGET_SUPPORTS_INFOS,
                                                  seulement=docs, lecture=False)
                md, usage = executer_claude(etape, consigne_annee(genre) + SANS_LECTURE,
                                            composer(atelier, bloc_sup), Jalons({}), tours=6)
                self.remarque(f"{pm.m.titre} : infos rédigées d'après le texte des documents, "
                              "sans les ouvrir (Claude manquait de tours pour les parcourir).")
            if suivi.mots(md) < 30:
                raise RuntimeError(f"Fiche presque vide ({suivi.mots(md)} mots).")
            if ajout and suivi.mots(md) < 0.6 * suivi.mots(existant):
                raise RuntimeError(f"La fiche mise à jour a perdu une grande partie de son contenu "
                                   f"({suivi.mots(md)} mots contre {suivi.mots(existant)}) : elle n'a "
                                   "pas été remplacée.")
            if pm.reecrire and md_infos.is_file():
                sauvegarder_version(md_infos, pm.travail / "versions")
            ecrire_atomique(md_infos, md.strip() + "\n")
            le = suivi.maintenant()
            if ajout:
                etat.infos.ajouts += 1
            else:
                etat.infos.vider()
                etat.infos.complet = le
            for ps, texte in nouvelles:
                etat.infos.noter_seance(ps.seance.cle, ps.notes, texte, le)
            for rel in docs:
                etat.infos.noter_support(rel, documents[rel], le)
            etat.sauver()
            self.maj_tache(ci, avancement=0.9, detail="Mise en page Word")
            ecrit, avert = produire_word(md_infos, sortie, consignes, pm.m,
                                         sous_titre=self._sous_titre_infos(pm), travail=pm.travail)
            for x in avert:
                self.remarque(f"{pm.m.titre} : {x}")
            self.maj_tache(ci, statut="fait", avancement=1.0,
                           detail=("mise à jour · " if ajout else "") + f"{suivi.mots(md)} mots · {ecrit.name}")
            return True
        except Exception as exc:  # noqa: BLE001
            noter("année : infos — " + traceback.format_exc()[-600:])
            self._echec_claude(ci, exc)
            return False

    def _refaire_word(self) -> None:
        """Remise en page des documents Word depuis les .md déjà rédigés, avec
        l'en-tête docx: actuel des consignes : sans relancer Claude."""
        analyse = self.annee.analyser()
        for pm in self.plan:
            if self.arret.is_set():
                return
            cle = f"w:{pm.m.nom}"
            self.maj_tache(cle, statut="en_cours", detail="Mise en page")
            seances = analyse.seances.get(pm.m.nom, [])
            pm.seances = [_PlanSeance(s, pm.travail / "seances" / f"{s.cle}.md", []) for s in seances]
            faits = []
            try:
                for nom_md, modele, suffixe, sous_titre in (
                        ("fiches.md", FICHES_MATIERE, an.FICHES, self._sous_titre(pm)),
                        ("infos.md", INFOS_MATIERE, an.INFOS, self._sous_titre_infos(pm))):
                    md = pm.travail / nom_md
                    if not md.is_file():
                        continue
                    ecrit, avert = produire_word(md, pm.dossier / f"{nom_fichier_matiere(pm.m)} - {suffixe}.docx",
                                                 consignes_annee(modele), pm.m, sous_titre=sous_titre,
                                                 travail=pm.travail)
                    faits.append(ecrit.name)
                    for x in avert:
                        self.remarque(f"{pm.m.titre} : {x}")
                self.maj_tache(cle, statut="fait", avancement=1.0, detail=" · ".join(faits) or "rien à refaire")
            except Exception as exc:  # noqa: BLE001
                self.maj_tache(cle, statut="erreur", detail=_lisible(exc)[:160])
                self.remarque(f"{pm.m.titre} : {_lisible(exc)[:200]}")


def _date_courte(iso: str) -> str:
    """« 2026-11-23T09:12:00 » -> « 23/11/2026 »."""
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%d/%m/%Y") if iso else ""
    except ValueError:
        return iso[:10]


def _quand(chemin: Path) -> Optional[str]:
    try:
        return datetime.datetime.fromtimestamp(chemin.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
    except OSError:
        return None


def instantane_annee(a: an.Annee, chantier: Optional[Chantier]) -> dict[str, Any]:
    """Tout ce que l'écran « année » affiche : dossiers, horaire, matières,
    enregistrements et leur classement, traitement en cours."""
    analyse = a.analyser()
    h = analyse.horaire
    src = a.source_horaire()
    hj = a.horaire_json()
    dossier_audios = a.dossier_audios()
    sortie: dict[str, Any] = {
        "chemin": str(a.racine), "nom": a.racine.name, "existe": a.racine.is_dir(),
        "audios": {"dossier": dossier_audios.name, "existe": dossier_audios.is_dir(),
                   "chemin": str(dossier_audios)},
        "a_ranger": [p.name for p in a.a_ranger()],
        "horaire": {
            "source": src.name if src else "", "lu": hj.is_file(), "chemin_json": str(hj),
            "perime": bool(src and hj.is_file() and src.stat().st_mtime > hj.stat().st_mtime + 1),
            "formation": h.formation if h else "", "annee": h.annee if h else "",
            "avertissements": (h.avertissements if h else [])[:8]},
        "remarques": analyse.remarques,
        "consignes": str(CONSIGNES_ANNEE),
        "ranger": a.ranger_traites(),
        "chantier": chantier.instantane() if chantier else None,
        "matieres": [], "enregistrements": [],
    }
    if h and h.periode():
        p0, p1 = h.periode()
        sortie["horaire"]["periode"] = f"du {an.date_fr(p0, jour=False)} au {an.date_fr(p1, jour=False)}"

    chemins_t = a.transcriptions(analyse.infos)
    transcrits = {rel: t.is_file() for rel, t in chemins_t.items()}
    # Séance -> date à laquelle elle est entrée dans les fiches et les infos
    # (les deux), ou absente.
    integrees: dict[tuple[str, str], str] = {}
    notes_faites: set[tuple[str, str]] = set()
    if h:
        for m in h.matieres:
            dossier = a.dossier_matiere(m)
            travail = dossier / TRAVAIL
            seances = analyse.seances.get(m.nom, [])
            audios = [c for s in seances for c in s.audios]
            tous = a.supports_matiere(m) if dossier.is_dir() else []
            sources, illisibles = trier_documents(tous)
            notes = {s.cle: travail / "seances" / f"{s.cle}.md" for s in seances}
            documents = {}
            for p in sources:
                documents[p.relative_to(dossier).as_posix()] = p
            fiches = dossier / f"{nom_fichier_matiere(m)} - {an.FICHES}.docx"
            infos = dossier / f"{nom_fichier_matiere(m)} - {an.INFOS}.docx"
            etat = etat_matiere(travail, notes, documents)
            dans_fiches = 0
            detail_seances = []
            for s in seances:
                n = notes[s.cle]
                integre = ""
                if n.is_file():
                    notes_faites.add((m.nom, s.cle))
                    if etat.fiches.seance_integree(s.cle, n) and etat.infos.seance_integree(s.cle, n):
                        integre = integrees[(m.nom, s.cle)] = etat.fiches.date_seance(s.cle)
                        dans_fiches += 1
                tous_transcrits = all(transcrits.get(c.audio.relatif) for c in s.audios)
                detail_seances.append({
                    "cle": s.cle, "date": f"{an.JOURS[s.date.weekday()][:3]} {s.date:%d/%m}" if s.date else "",
                    "heure": f"{s.audios[0].audio.debut:%H:%M}" if s.audios and s.audios[0].audio.debut
                    and s.audios[0].audio.precision == "heure" else "",
                    "audios": [{"nom": c.audio.nom, "titre": c.audio.titre or c.audio.nom,
                                "duree": an.duree_lisible(c.audio.duree)} for c in s.audios],
                    "etat": ("fiches" if integre and tous_transcrits else
                             "notes" if n.is_file() and tous_transcrits else
                             "transcrit" if tous_transcrits else "nouveau"),
                    "integre": _date_courte(integre)})
            # Les documents trouvés dans le dossier de la matière, tels que
            # Transcriptor les voit : rôle (syllabus, diapositives), format,
            # dans les fiches ou pas encore, ou à convertir.
            detail_docs = []
            for q in tous:
                rel = q.relative_to(dossier).as_posix()
                lisible = rel in documents
                dans = lisible and etat.fiches.support_integre(rel, q) and etat.infos.support_integre(rel, q)
                try:
                    taille = q.stat().st_size
                except OSError:
                    taille = 0
                detail_docs.append({
                    "nom": q.name, "relatif": rel, "chemin": str(q),
                    "sous_dossier": rel.rsplit("/", 1)[0] if "/" in rel else "",
                    "role": sup.role_document(q.name), "format": sup.libelle_format(q),
                    "taille": (f"{taille / 1048576:.1f} Mo" if taille >= 1048576
                               else f"{max(1, round(taille / 1024))} Ko") if taille else "",
                    "etat": "fiches" if dans else ("nouveau" if lisible else "a_convertir"),
                    "integre": _date_courte(str((etat.fiches.supports.get(rel) or {}).get("le") or ""))
                    if dans else "",
                    "conseil": "" if lisible else sup.conversion(q)})
            detail_docs.sort(key=lambda d: (ORDRE_ROLES.get(d["role"], 2), d["etat"] == "a_convertir",
                                            d["relatif"].count("/"), d["relatif"].lower()))
            docs_a_jour = all(etat.fiches.support_integre(r, q) and etat.infos.support_integre(r, q)
                              for r, q in documents.items())
            a_jour = (fiches.is_file() and infos.is_file() and dans_fiches == len(seances)
                      and all(transcrits.get(c.audio.relatif) for c in audios) and docs_a_jour)
            sortie["matieres"].append({
                "nom": m.nom, "court": m.titre, "ue": m.ue, "couleur": m.couleur,
                "encre": an.texte_sur(m.couleur), "fonce": an.foncee(m.couleur, 0.2),
                "enseignant": m.enseignant, "dossier": dossier.name, "dossier_existe": dossier.is_dir(),
                "chemin": str(dossier), "prevues": h.seances_prevues(m.nom), "seances": len(seances),
                "audios": len(audios), "transcrits": sum(1 for c in audios if transcrits.get(c.audio.relatif)),
                "notes": sum(1 for n in notes.values() if n.is_file()), "supports": len(sources),
                "a_convertir": len(illisibles), "documents": detail_docs, "detail_seances": detail_seances,
                "dans_fiches": dans_fiches, "nouvelles": len(seances) - dans_fiches,
                "docs_nouveaux": 0 if docs_a_jour else sum(
                    1 for r, q in documents.items()
                    if not (etat.fiches.support_integre(r, q) and etat.infos.support_integre(r, q))),
                "ajouts": etat.fiches.ajouts, "complet": etat.fiches.complet[:10],
                "fiches": {"chemin": str(fiches), "date": _quand(fiches)} if fiches.is_file() else None,
                "infos": {"chemin": str(infos), "date": _quand(infos)} if infos.is_file() else None,
                "a_jour": bool(a_jour and (seances or sources))})

    def cle_tri(c: an.Classement) -> tuple:
        return (c.audio.debut is None, c.audio.debut or datetime.datetime.max, c.audio.nom)
    for c in sorted(analyse.classements, key=cle_tri):
        d = c.audio.debut
        transcrit = transcrits.get(c.audio.relatif, False)
        integre = integrees.get((c.matiere, c.seance), "") if c.classe else ""
        if c.doublon:
            etape = "doublon"
        elif c.ignore:
            etape = "ecarte"
        elif not c.classe:
            etape = "a_classer"
        elif integre and transcrit:
            etape = "fiches"
        elif transcrit and (c.matiere, c.seance) in notes_faites:
            etape = "notes"
        elif transcrit:
            etape = "transcrit"
        else:
            etape = "nouveau"
        rangement = c.audio.relatif.rsplit("/", 1)[0] if a.est_range(c.audio.relatif) else ""
        sortie["enregistrements"].append({
            "relatif": c.audio.relatif, "nom": c.audio.nom, "titre": c.audio.titre,
            "date": f"{d:%d/%m/%Y}" if d else "", "jour": an.JOURS[d.weekday()] if d else "",
            "heure": f"{d:%H:%M}" if d and c.audio.precision == "heure" else "",
            "mois": f"{d:%Y-%m}" if d else "", "mois_libelle": f"{an.MOIS[d.month - 1]} {d.year}" if d else "",
            "source": c.audio.source, "duree": an.duree_lisible(c.audio.duree),
            "matiere": c.matiere, "confiance": c.confiance, "raison": c.raison,
            "ignore": c.ignore, "doublon": c.doublon, "transcrit": transcrit,
            "seance": c.seance, "etape": etape,
            "integre": _date_courte(integre),
            "range": rangement})
    return sortie


def choisir_dossier_natif(titre: str) -> Optional[str]:
    """Boîte « Rechercher un dossier » de Windows, sur un thread STA comme la
    boîte « Ouvrir » (voir choisir_fichier_natif). Lecteurs réseau compris."""
    import ctypes
    from ctypes import wintypes

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND), ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", wintypes.LPWSTR), ("lpszTitle", wintypes.LPCWSTR),
                    ("ulFlags", wintypes.UINT), ("lpfn", ctypes.c_void_p),
                    ("lParam", wintypes.LPARAM), ("iImage", ctypes.c_int)]

    issue: dict[str, Any] = {}

    def _montrer() -> None:
        ole32 = ctypes.windll.ole32
        ole32.OleInitialize(None)
        try:
            nom = ctypes.create_unicode_buffer(260)
            bi = BROWSEINFOW()
            bi.hwndOwner = _HANDLE_FENETRE.get("hwnd") or None
            bi.pszDisplayName = ctypes.cast(nom, wintypes.LPWSTR)
            bi.lpszTitle = titre
            bi.ulFlags = 0x0001 | 0x0010 | 0x0040      # RETURNONLYFSDIRS | EDITBOX | NEWDIALOGSTYLE
            shell32 = ctypes.windll.shell32
            shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
            shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
            pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
            if pidl:
                chemin = ctypes.create_unicode_buffer(32768)
                if shell32.SHGetPathFromIDListW(ctypes.c_void_p(pidl), chemin):
                    issue["chemin"] = chemin.value
                ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
        except Exception as exc:  # noqa: BLE001
            issue["erreur"] = str(exc)
        finally:
            ole32.OleUninitialize()

    fil = threading.Thread(target=_montrer, daemon=True)
    fil.start()
    fil.join(600)
    if fil.is_alive():
        raise RuntimeError("la boîte de dialogue ne répond pas")
    if "erreur" in issue:
        raise RuntimeError(issue["erreur"])
    return issue.get("chemin")


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
        self._annee: Optional[an.Annee] = None          # mode année : dossier ouvert
        self._chantier: Optional[Chantier] = None       # et son traitement

    # -- appelée depuis le JavaScript ---------------------------------
    def choisir_fichier(self) -> Optional[dict[str, Any]]:
        noter("appel choisir_fichier")
        chemins = self._dialogue("Choisir l'enregistrement", _filtres_principal(), False)
        return self._decrire(chemins[0]) if chemins else None

    def choisir_supports(self) -> list[dict[str, Any]]:
        """Diapos, photos du tableau, notes : plusieurs fichiers à la fois."""
        noter("appel choisir_supports")
        chemins = self._dialogue("Ajouter des supports du cours", _filtres_supports(), True)
        return [self._decrire_support(c) for c in chemins or []]

    def _dialogue(self, titre: str, filtres: list[tuple[str, str]],
                  multiple: bool) -> Optional[list[str]]:
        if os.name == "nt":
            try:
                chemins = choisir_fichier_natif(titre, filtres, multiple)
                noter(f"boîte native : {len(chemins) if chemins else 'annulée'}")
                return chemins
            except Exception as exc:  # noqa: BLE001
                noter(f"boîte native indisponible ({exc}), repli sur pywebview")

        import webview

        # pywebview n'accepte que lettres et espaces dans le nom d'un filtre.
        motifs = tuple(" ".join(re.sub(r"[^\w ]+", " ", nom).split()) + f" ({masque})"
                       for nom, masque in filtres)
        resultat = self._fenetre.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=multiple, file_types=motifs)
        return list(resultat) if resultat else None

    def _decrire_support(self, chemin: str) -> dict[str, Any]:
        p = Path(chemin)
        genre = sup.genre_de(p)
        info: dict[str, Any] = {"chemin": str(p), "nom": p.name, "genre": genre}
        if not p.is_file():
            info["erreur"] = "introuvable"
        elif not genre:
            info["erreur"] = f"format {p.suffix or 'inconnu'} non pris en charge"
        elif genre == "a_convertir":
            info["erreur"] = sup.conversion(p)
        else:
            taille = p.stat().st_size
            info["taille"] = (f"{taille / 1048576:.1f} Mo" if taille >= 1048576
                              else f"{max(1, round(taille / 1024))} Ko")
            info["role"] = sup.role_document(p.name, genre)
        return info

    # -- modèle Claude --------------------------------------------------
    def options_claude(self) -> dict[str, Any]:
        """Les choix proposés et le dernier réglage utilisé."""
        r = lire_reglages()
        return {"modeles": [{"valeur": v, "libelle": l} for v, l in MODELES_CLAUDE],
                "efforts": [{"valeur": v, "libelle": l} for v, l in EFFORTS_CLAUDE],
                "choisi": {"modele": r.get("claude_modele", ""),
                           "effort": r.get("claude_effort", "")}}

    # -- consignes -----------------------------------------------------
    def lister_consignes(self) -> dict[str, Any]:
        modeles = liste_consignes()
        noms = [m["nom"] for m in modeles]
        choisi = lire_reglages().get("consignes")
        if choisi not in noms:
            choisi = "Cours complet" if "Cours complet" in noms else (noms[0] if noms else "")
        return {"modeles": modeles, "choisi": choisi, "dossier": str(CONSIGNES)}

    def retenir_consignes(self, nom: str) -> bool:
        ecrire_reglages(consignes=nom)
        return True

    def modifier_consignes(self, nom: str) -> dict[str, Any]:
        try:
            ouvrir_fichier(chemin_consignes(nom), modifier=True)
            return {}
        except Exception as exc:  # noqa: BLE001
            return {"erreur": str(exc)}

    def dupliquer_consignes(self, nom: str, nouveau: str) -> dict[str, Any]:
        """Copie un modèle sous un autre nom et l'ouvre pour modification."""
        try:
            source = chemin_consignes(nom)
        except FileNotFoundError as exc:
            return {"erreur": str(exc)}
        propre = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", nouveau or "").strip().strip(".")[:80]
        if not propre:
            return {"erreur": "Donne un nom au nouveau modèle."}
        cible = CONSIGNES / f"{propre}.md"
        if cible.exists():
            return {"erreur": f"Un modèle « {propre} » existe déjà."}
        try:
            shutil.copyfile(source, cible)
        except OSError as exc:
            return {"erreur": f"Copie impossible : {exc}"}
        noter(f"consignes : « {nom} » dupliqué en « {propre} »")
        ecrire_reglages(consignes=propre)
        ouvrir_fichier(cible, modifier=True)
        return {"nom": propre}

    def ouvrir_dossier_consignes(self) -> None:
        try:
            installer_modeles()
        except Exception:  # noqa: BLE001
            CONSIGNES.mkdir(parents=True, exist_ok=True)
        ouvrir_fichier(CONSIGNES)

    def modifier_consignes_du_cours(self) -> dict[str, Any]:
        """Depuis l'écran de résultat : le modèle utilisé pour ce cours."""
        t = self._courant
        if not t or not t.consignes:
            return {"erreur": "Aucun modèle associé à ce cours."}
        ouvrir_fichier(t.consignes, modifier=True)
        return {}

    def connecter_claude(self) -> dict[str, Any]:
        """« Se connecter à Claude » : une console lance claude auth login ;
        à sa fermeture, l'état de connexion est relu et affiché."""
        try:
            return ouvrir_connexion_claude(
                apres=lambda: verifier_connexion(self._pousser_claude))
        except Exception as exc:  # noqa: BLE001
            noter("claude : connexion impossible à ouvrir — " + str(exc))
            return {"erreur": f"Impossible d'ouvrir la connexion : {exc}"}

    def verifier_connexion_claude(self) -> dict[str, str]:
        return verifier_connexion()

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
        if p.suffix.lower() == ".md":
            # Un support déjà rédigé : on ne refera que la mise en page, selon
            # l'en-tête `pdf:` des consignes choisies.
            try:
                mots = len(p.read_text(encoding="utf-8", errors="replace").split())
            except OSError:
                mots = 0
            if not mots:
                return {"erreur": "Ce fichier Markdown est vide."}
            return {"chemin": str(p), "nom": p.name, "pdf_seul": True, "mots": mots}
        if p.suffix.lower() in sup.EXT_SUPPORTS and p.suffix.lower() not in (".txt",):
            return {"erreur": "C'est un support (diapos, photo) : ajoute-le avec « Ajouter des "
                              "supports ». Ici, choisis l'enregistrement audio."}
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
        noter(f"appel lancer : {options.get('chemin')!r}, modèle {options.get('modele')!r}, "
              f"consignes {options.get('consignes')!r}, "
              f"{len(options.get('supports') or [])} support(s)")
        source = Path(options.get("chemin", ""))
        if not source.exists():
            return {"erreur": "Fichier introuvable."}
        if self._chantier and self._chantier.en_cours():
            return {"erreur": "Un traitement d'année est en cours (mode année) : attends sa fin "
                              "ou arrête-le avant de lancer un cours seul."}
        try:
            consignes = chemin_consignes(options.get("consignes") or "")
        except FileNotFoundError as exc:
            return {"erreur": f"{exc} Choisis un modèle dans « Consignes de synthèse »."}
        try:
            modele, effort = reglages_claude(options)
        except ValueError as exc:
            return {"erreur": str(exc)}
        options = dict(options, claude_modele=modele, claude_effort=effort)
        ecrire_reglages(consignes=consignes.stem, claude_modele=modele, claude_effort=effort)

        if source.suffix.lower() == ".md":
            return self._pdf_seul(source, options, consignes)
        if source.suffix.lower() == ".txt":
            return self._reprendre(source, options, consignes)

        dossier = self._nouveau_dossier(source.stem)
        t = Traitement(source, dossier, options, self._pousser)
        t.consignes = consignes
        self._courant = t
        threading.Thread(target=self._pipeline, args=(t,), daemon=True).start()
        return t.instantane()

    def relancer_synthese(self, claude: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Après un échec de la synthèse : on repart de la transcription déjà
        faite, dans le même dossier, sans refaire passer l'audio au GPU. Le
        modèle Claude peut changer (limite atteinte, modèle indisponible)."""
        t = self._courant
        noter(f"appel relancer_synthese {claude or ''}")
        if not t or t.etat == "en_cours":
            return {"erreur": "Rien à relancer."}
        if claude is not None:
            try:
                modele, effort = reglages_claude({"claude_modele": claude.get("modele"),
                                                  "claude_effort": claude.get("effort")})
            except ValueError as exc:
                return {"erreur": str(exc)}
            t.options = dict(t.options, claude_modele=modele, claude_effort=effort)
            ecrire_reglages(claude_modele=modele, claude_effort=effort)
        transcription = Path(t.fichiers.get("txt") or t.dossier / "transcription.txt")
        if not transcription.exists():
            return {"erreur": "transcription.txt est introuvable : relance le cours "
                              "depuis l'audio."}
        t.etat, t.erreur, t.usage, t.connexion = "en_cours", None, {}, False
        t.fichiers = {"txt": str(transcription)}
        t.avertissements = []
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
        return dossier

    def _pdf_seul(self, md: Path, options: dict[str, Any], consignes: Path) -> dict[str, Any]:
        """Un .md déjà produit : mise en page seule, selon les consignes choisies."""
        t = Traitement(md, md.parent, dict(options, supports=[]), self._pousser)
        t.consignes = consignes
        t.fichiers["md"] = str(md)
        if (md.parent / "transcription.txt").exists():
            t.fichiers["txt"] = str(md.parent / "transcription.txt")
        for nom in ("transcription", "synthese"):
            t.etapes[nom] = {"statut": "fait", "avancement": 1.0, "detail": "déjà faite"}
        self._courant = t
        threading.Thread(target=self._mise_en_page_seule, args=(t,), daemon=True).start()
        return t.instantane()

    def _mise_en_page_seule(self, t: Traitement) -> None:
        try:
            mettre_en_page(t, Path(t.fichiers["md"]))
        except Exception as exc:  # noqa: BLE001
            t.maj("pdf", statut="ignore", detail=_lisible(exc)[:150])
        t.etat = "fini"
        t.notifier(t.instantane())

    def refaire_pdf(self) -> dict[str, Any]:
        """Remet en page le support affiché, avec l'en-tête `pdf:` actuel du
        modèle de consignes : pour ajuster la mise en page sans relancer Claude."""
        t = self._courant
        noter("appel refaire_pdf")
        if not t or t.etat == "en_cours" or not t.fichiers.get("md"):
            return {"erreur": "Aucun support à remettre en page."}
        t.avertissements = [a for a in t.avertissements if not a.startswith("Consignes")]
        try:
            mettre_en_page(t, Path(t.fichiers["md"]))
        except Exception as exc:  # noqa: BLE001
            t.maj("pdf", statut="ignore", detail=_lisible(exc)[:150])
        return t.instantane()

    def _reprendre(self, texte: Path, options: dict[str, Any],
                   consignes: Path) -> dict[str, Any]:
        """Un transcription.txt choisi à la place de l'audio : la synthèse se
        refait dans son dossier de cours s'il en vient, sinon dans un nouveau."""
        entete = _entete_transcription(texte)
        if not entete.get("mots"):
            return {"erreur": "Ce fichier texte est vide."}

        dans_un_cours = (texte.name == "transcription.txt"
                         and texte.parent != SORTIE
                         and texte.parent.parent == SORTIE)
        if dans_un_cours:
            dossier = texte.parent          # ses supports éventuels sont repris
            transcription = texte
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
        t.consignes = consignes
        t.fichiers["txt"] = str(transcription)
        t.duree = float(entete.get("secondes") or 0)
        t.debut = entete.get("debut")
        t.etapes["transcription"] = {"statut": "fait", "avancement": 1.0,
                                     "detail": f"reprise · {entete['mots']} mots"}
        self._courant = t
        threading.Thread(target=self._pipeline, args=(t, transcription),
                         daemon=True).start()
        return t.instantane()

    # -- mode année ------------------------------------------------------
    def annee_initiale(self) -> dict[str, Any]:
        """La dernière année ouverte et le dernier modèle de transcription."""
        r = lire_reglages()
        return {"chemin": r.get("annee_chemin", ""), "modele": r.get("annee_modele", "large-v3"),
                "mode": r.get("mode", "cours")}

    def retenir_mode(self, mode: str) -> bool:
        """Le mode affiché (un cours / une année) est retrouvé au lancement suivant."""
        ecrire_reglages(mode="annee" if mode == "annee" else "cours")
        return True

    def choisir_dossier_annee(self) -> Optional[str]:
        noter("appel choisir_dossier_annee")
        if os.name == "nt":
            try:
                return choisir_dossier_natif("Choisis le dossier de l'année (par exemple « Année 1 »)")
            except Exception as exc:  # noqa: BLE001
                noter(f"boîte « dossier » native indisponible ({exc}), repli sur pywebview")
        import webview
        r = self._fenetre.create_file_dialog(webview.FOLDER_DIALOG)
        if not r:
            return None
        return r[0] if isinstance(r, (list, tuple)) else str(r)

    def ouvrir_annee(self, chemin: str) -> dict[str, Any]:
        chemin = (chemin or "").strip().strip('"').strip()
        noter(f"appel ouvrir_annee : {chemin!r}")
        if not chemin:
            return {"erreur": "Indique le dossier de l'année."}
        p = Path(chemin)
        if not p.is_dir():
            return {"erreur": f"Dossier introuvable : {chemin}"}
        if self._chantier and self._chantier.en_cours() and self._chantier.annee.racine != p:
            return {"erreur": "Un traitement est en cours sur une autre année : arrête-le d'abord."}
        self._annee = an.Annee(p)
        ecrire_reglages(annee_chemin=str(p))
        return self.etat_annee()

    def etat_annee(self) -> dict[str, Any]:
        if not self._annee:
            return {"erreur": "Aucune année ouverte."}
        chantier = self._chantier if (self._chantier and
                                      self._chantier.annee.racine == self._annee.racine) else None
        try:
            return instantane_annee(self._annee, chantier)
        except Exception as exc:  # noqa: BLE001
            noter("année : état illisible — " + traceback.format_exc()[-800:])
            return {"erreur": f"Lecture du dossier impossible : {_lisible(exc)}"}

    def preparer_dossiers_annee(self) -> dict[str, Any]:
        """Range dans le dossier des audios les enregistrements et l'horaire
        posés à la racine de l'année, puis crée un dossier par matière."""
        a = self._annee
        if not a:
            return {"erreur": "Aucune année ouverte."}
        if self._occupe():
            return {"erreur": "Attends la fin du traitement en cours."}
        try:
            ranges = a.ranger()
            crees = a.creer_structure(a.charger_horaire())
        except Exception as exc:  # noqa: BLE001
            noter("année : dossiers — " + traceback.format_exc()[-600:])
            return {"erreur": f"Préparation des dossiers impossible : {_lisible(exc)}"}
        noter(f"année : {len(ranges)} fichier(s) rangé(s), dossiers créés : {crees}")
        etat = self.etat_annee()
        morceaux = []
        deplaces = [m for m in ranges if "→" in m]
        if deplaces:
            morceaux.append(f"{len(deplaces)} fichier(s) rangé(s) dans « {a.dossier_audios().name} »")
        if crees:
            morceaux.append(f"{len(crees)} dossier(s) créé(s) : " + ", ".join(crees))
        morceaux += [m for m in ranges if "→" not in m]
        etat["message"] = " · ".join(morceaux) or "Les dossiers étaient déjà prêts."
        return etat

    def lire_horaire_annee(self, claude: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._lancer_chantier(dict(claude or {}, genre="horaire"))

    def modifier_horaire_annee(self) -> dict[str, Any]:
        a = self._annee
        if not a:
            return {"erreur": "Aucune année ouverte."}
        if not a.horaire_json().is_file():
            return {"erreur": "L'horaire n'a pas encore été lu : clique « Lire l'horaire »."}
        ouvrir_fichier(a.horaire_json(), modifier=True)
        return {}

    def classer_audio_annee(self, relatif: str, matiere: str) -> dict[str, Any]:
        a = self._annee
        if not a:
            return {"erreur": "Aucune année ouverte."}
        try:
            a.classer_manuellement(relatif, matiere)
        except OSError as exc:
            return {"erreur": f"Classement non enregistré : {exc}"}
        return self.etat_annee()

    def lancer_annee(self, options: dict[str, Any]) -> dict[str, Any]:
        """« Tout traiter », ou une matière (`matieres`) — `reecrire` : ses
        fiches et ses infos sont refaites de zéro."""
        return self._lancer_chantier(dict(options, genre="tout"))

    def regler_annee(self, reglages: dict[str, Any]) -> dict[str, Any]:
        a = self._annee
        if not a:
            return {"erreur": "Aucune année ouverte."}
        try:
            if "ranger" in reglages:
                a.regler(ranger=bool(reglages["ranger"]))
        except OSError as exc:
            return {"erreur": f"Réglage non enregistré : {exc}"}
        return self.etat_annee()

    def refaire_word_annee(self, matiere: str = "") -> dict[str, Any]:
        return self._lancer_chantier({"genre": "word", "matieres": [matiere] if matiere else []})

    def arreter_annee(self) -> dict[str, Any]:
        if self._chantier and self._chantier.en_cours():
            noter("année : arrêt demandé")
            self._chantier.arreter()
        return {}

    def ouvrir_annee_chemin(self, chemin: str) -> dict[str, Any]:
        """Ouvre un fichier ou un dossier de l'année (fiches Word, dossier
        d'une matière…), et rien en dehors."""
        p = Path(chemin or "")
        try:
            racine = self._annee.racine.resolve() if self._annee else None
            cible = p.resolve()
            dedans = racine is not None and (cible == racine or racine in cible.parents)
        except OSError:
            dedans = False
        if not dedans:
            return {"erreur": "Chemin hors du dossier de l'année."}
        if not p.exists():
            return {"erreur": f"Introuvable : {p.name}"}
        ouvrir_fichier(p)
        return {}

    def ouvrir_consignes_annee(self) -> None:
        try:
            installer_modeles_annee()
        except Exception:  # noqa: BLE001
            CONSIGNES_ANNEE.mkdir(parents=True, exist_ok=True)
        ouvrir_fichier(CONSIGNES_ANNEE)

    def _lancer_chantier(self, options: dict[str, Any]) -> dict[str, Any]:
        a = self._annee
        noter(f"appel chantier {options.get('genre')} : {options.get('matieres') or 'toutes'}")
        if not a:
            return {"erreur": "Aucune année ouverte."}
        if self._occupe():
            return {"erreur": "Un traitement est déjà en cours."}
        try:
            modele, effort = reglages_claude(options)
        except ValueError as exc:
            return {"erreur": str(exc)}
        options = dict(options, claude_modele=modele, claude_effort=effort)
        if options.get("genre") == "tout":
            ecrire_reglages(annee_modele=options.get("modele") or "large-v3",
                            claude_modele=modele, claude_effort=effort)
        self._chantier = Chantier(a, options, self._pousser_annee)
        self._chantier.lancer()
        return self.etat_annee()

    def _occupe(self) -> bool:
        return bool((self._chantier and self._chantier.en_cours())
                    or (self._courant and self._courant.etat == "en_cours"))

    def _pousser_annee(self, instantane: dict[str, Any]) -> None:
        if not self._fenetre or not self._chantier or instantane.get("id") != self._chantier.id:
            return
        charge = json.dumps(instantane, ensure_ascii=False)
        try:
            self._fenetre.evaluate_js(f"window.majChantier && window.majChantier({charge})")
        except Exception:  # noqa: BLE001
            pass

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
        # Un traitement remplacé entre-temps (« Un autre cours ») se tait.
        if not self._fenetre or not self._courant or instantane.get("id") != self._courant.id:
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
        try:
            preparer_supports(t)
        except Exception as exc:  # noqa: BLE001  (un support raté n'arrête pas le cours)
            noter("supports : échec — " + traceback.format_exc()[-400:])
            t.maj("supports", statut="ignore", detail=_lisible(exc)[:150])
        if transcription is None:
            try:
                transcription = transcrire(t)
            except Exception as exc:  # noqa: BLE001
                t.echec("transcription", _lisible(exc))
                return
        t.connexion = False
        try:
            markdown = synthetiser(t, transcription)
        except Exception as exc:  # noqa: BLE001
            noter("synthèse : échec — " + str(exc)[:300])
            t.connexion = isinstance(exc, ConnexionClaude)
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
    for ligne in texte.splitlines()[:6]:
        m = re.match(r"^(Cours|Fichier|Durée|Début)\s*:\s*(.+?)\s*$", ligne)
        if m:
            cle = {"Cours": "matiere", "Fichier": "fichier", "Durée": "duree",
                   "Début": "debut"}[m.group(1)]
            infos[cle] = m.group(2)
    if infos.get("matiere") in ("à préciser", ""):
        infos["matiere"] = ""
    m = re.fullmatch(r"(\d+):(\d{2})", infos.get("duree", ""))
    if m:
        infos["secondes"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60
    try:
        infos["debut"] = (datetime.datetime.strptime(infos["debut"], "%Y-%m-%d %H:%M:%S")
                          if infos.get("debut") else None)
    except ValueError:
        infos["debut"] = None
    infos["mots"] = len(texte.split())
    return infos


def _lisible(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    bas = message.lower()
    if isinstance(exc, FileNotFoundError) or "no such file" in bas:
        # Un fichier absent n'est pas un fichier illisible : soit il a bougé,
        # soit son chemin dépasse la limite de Windows (260 caractères).
        chemin = str(getattr(exc, "filename", None) or "")
        if not chemin:
            m = re.search(r"No such file or directory: '(.+)'", message)
            chemin = m.group(1).replace("\\\\", "\\") if m else ""
        nom = re.split(r"[\\/]", chemin)[-1] if chemin else ""
        if len(chemin) > an.LIMITE_CHEMIN:
            message = (f"Chemin trop long pour Windows ({len(chemin)} caractères, "
                       f"{an.LIMITE_CHEMIN} au plus) : « {nom[:70]}… ». Raccourcis le nom "
                       "du fichier, puis relance.")
        elif nom:
            message = (f"Fichier introuvable : « {nom[:120]} » — déplacé, renommé ou supprimé "
                       "pendant le traitement ? Relance.")
    elif "invalid data" in bas or "moov atom" in bas or "does not contain" in bas \
            or "av." in bas.split(":")[0]:
        message = ("Ce fichier n'a pas pu être décodé. Convertis-le en MP3 ou WAV, "
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
    try:
        installer_modeles()
    except Exception as exc:  # noqa: BLE001
        noter(f"consignes : installation des modèles impossible ({exc})")
    preparer_webview2()

    import webview

    noter(f"pywebview {getattr(webview, '__version__', '?')}")
    passerelle = Passerelle()
    # Toute l'application visible dès l'ouverture : la fenêtre prend la hauteur
    # utile de l'écran (agrandie sur un petit écran), et la page passe sur deux
    # colonnes dès qu'elle est assez large.
    largeur, hauteur, agrandie = taille_fenetre()
    fenetre = webview.create_window(
        "Transcriptor",
        str(APP_DIR / "static" / "index.html"),
        js_api=passerelle,
        width=largeur, height=hauteur, min_size=(560, 620),
        maximized=agrandie,
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
