# -*- coding: utf-8 -*-
"""
Supports de cours joints à l'enregistrement : diapositives (PPTX, PDF),
photos du tableau, notes (TXT, MD).

Ils sont préparés en local dans <dossier du cours>/supports/, puis transmis
à Claude avec la transcription pour qu'il croise les deux :

- photos : orientation EXIF appliquée, réduites à 1600 px de côté (l'API les
  réduirait de toute façon), heure de prise de vue relevée pour situer la
  photo dans l'enregistrement ; HEIC accepté grâce à pillow-heif ;
- PPTX : texte de chaque diapositive et notes de l'orateur dans un .md,
  images des diapositives extraites (logos répétés et vignettes écartés) ;
- PDF : copié tel quel, Claude le lit lui-même (texte et visuel) ;
- TXT/MD : notes personnelles, transmises telles quelles.

Le manifeste (supports/manifeste.json) permet de relancer la synthèse sans
rien refaire, et d'ajouter des supports lors d'une reprise.
"""
from __future__ import annotations

import datetime
import hashlib
import importlib
import io
import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

EXT_IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
             ".heic", ".heif"}
EXT_DIAPOS = {".pptx", ".pdf"}
EXT_NOTES = {".txt", ".md"}
EXT_SUPPORTS = EXT_IMAGE | EXT_DIAPOS | EXT_NOTES | {".ppt"}

COTE_MAX = 1600            # px, plus grand côté des images envoyées
IMAGES_PAR_PPTX = 40       # au-delà, on garde les plus grandes
TEXTE_MAX = 60_000         # caractères transmis en ligne par document
MANIFESTE = "manifeste.json"
LISIBLES_PAR_CLAUDE = ("photo", "image_diapo", "pdf")   # à lire avec l'outil Read


@dataclass
class Support:
    fichier: str                 # nom dans supports/
    genre: str                   # photo | pdf | diapos | image_diapo | notes
    origine: str                 # nom du fichier fourni
    detail: str = ""             # « 1600×1200 », « ≈ 34 pages », « 28 diapositives »
    prise: str = ""              # photo : « AAAA-MM-JJ HH:MM:SS » (EXIF)
    diapo: int = 0               # image_diapo : numéro de la diapositive
    source: str = ""             # chemin d'origine, pour ne pas préparer deux fois

    @classmethod
    def depuis(cls, d: dict) -> "Support":
        champs = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**champs)


Importeur = Callable[[str, str], Any]


def _importer_simple(nom: str, _paquet: str) -> Any:
    return importlib.import_module(nom)


def genre_de(chemin: Path) -> str:
    ext = chemin.suffix.lower()
    if ext in EXT_IMAGE:
        return "photo"
    if ext == ".pptx":
        return "diapos"
    if ext == ".ppt":
        return "ppt"
    if ext == ".pdf":
        return "pdf"
    if ext in EXT_NOTES:
        return "notes"
    return ""


def _nom_sur(texte: str, limite: int = 40) -> str:
    """Nom de fichier sans surprise pour Claude comme pour Windows."""
    s = re.sub(r"[^\w.-]+", "_", texte, flags=re.UNICODE).strip("._")
    return (s or "support")[:limite]


# ----------------------------------------------------------------------
# Manifeste
# ----------------------------------------------------------------------
def lire_manifeste(dossier: Path) -> list[Support]:
    try:
        brut = json.loads((dossier / MANIFESTE).read_text(encoding="utf-8"))
        return [Support.depuis(d) for d in brut.get("supports", [])
                if (dossier / d.get("fichier", "")).is_file()]
    except Exception:  # noqa: BLE001
        return []


def ecrire_manifeste(dossier: Path, supports: list[Support]) -> None:
    (dossier / MANIFESTE).write_text(
        json.dumps({"supports": [asdict(s) for s in supports]}, ensure_ascii=False, indent=1),
        encoding="utf-8")


# ----------------------------------------------------------------------
# Préparation
# ----------------------------------------------------------------------
def preparer(sources: list[Path], dossier: Path, *,
             importer: Importeur = _importer_simple,
             signaler: Optional[Callable[[str], None]] = None) -> tuple[list[Support], list[str]]:
    """Prépare les fichiers `sources` dans `dossier` (le sous-dossier
    supports/ du cours) et renvoie (tous les supports, avertissements).
    Les supports déjà préparés lors d'un passage précédent sont conservés."""
    dossier.mkdir(parents=True, exist_ok=True)
    supports = lire_manifeste(dossier)
    deja = {s.source for s in supports if s.source}
    avert: list[str] = []
    rang = 1 + max((int(m.group(1)) for s in supports
                    if (m := re.match(r"(\d+)_", s.fichier))), default=0)

    # Diapos et notes d'abord, dans l'ordre donné ; puis les photos dans l'ordre
    # chronologique de prise de vue quand on la connaît : c'est l'ordre du cours.
    a_faire = [Path(p) for p in sources if str(Path(p).resolve()) not in deja]
    a_faire.sort(key=lambda p: (genre_de(p) == "photo", _prise_rapide(p, importer) or "9999"))

    for src in a_faire:
        genre = genre_de(src)
        prefixe = f"{rang:02d}_{_nom_sur(src.stem)}"
        if signaler:
            signaler(f"{src.name}")
        try:
            if not src.is_file():
                raise ValueError("fichier introuvable")
            if genre == "photo":
                nouveaux = [_photo(src, dossier, prefixe, importer)]
            elif genre == "diapos":
                nouveaux = _pptx(src, dossier, prefixe, importer, avert)
            elif genre == "pdf":
                nouveaux = [_pdf(src, dossier, prefixe)]
            elif genre == "notes":
                nouveaux = [_notes(src, dossier, prefixe)]
            elif genre == "ppt":
                raise ValueError("ancien format PowerPoint illisible — dans PowerPoint, "
                                 "« Enregistrer sous » .pptx ou « Exporter » en PDF, puis rajoute-le")
            else:
                raise ValueError(f"format {src.suffix or 'inconnu'} non pris en charge")
        except Exception as exc:  # noqa: BLE001
            avert.append(f"{src.name} ignoré : {exc}")
            continue
        for s in nouveaux:
            s.source = str(src.resolve())
        supports.extend(nouveaux)
        rang += 1

    ecrire_manifeste(dossier, supports)
    return supports, avert


def _pillow(importer: Importeur, heic: bool = False):
    Image = importer("PIL.Image", "pillow")
    ImageOps = importer("PIL.ImageOps", "pillow")
    if heic:
        pillow_heif = importer("pillow_heif", "pillow-heif")
        pillow_heif.register_heif_opener()
    return Image, ImageOps


def _date_exif(im) -> str:
    try:
        exif = im.getexif()
        brut = exif.get_ifd(0x8769).get(36867) or exif.get(306)   # DateTimeOriginal, DateTime
        if brut:
            d = datetime.datetime.strptime(str(brut).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
            return d.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _prise_rapide(p: Path, importer: Importeur) -> str:
    if genre_de(p) != "photo":
        return ""
    try:
        Image, _ = _pillow(importer, heic=p.suffix.lower() in (".heic", ".heif"))
        with Image.open(p) as im:
            return _date_exif(im)
    except Exception:  # noqa: BLE001
        return ""


def _enregistrer_image(im, dossier: Path, prefixe: str, Image, ImageOps,
                       garder_png: bool) -> tuple[str, str]:
    """Redresse (EXIF), réduit et enregistre ; renvoie (nom, « L×H »)."""
    im = ImageOps.exif_transpose(im)
    if garder_png:
        if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            im = im.convert("RGBA")
    elif im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        fond = Image.new("RGB", rgba.size, "white")
        fond.paste(rgba, mask=rgba.split()[-1])
        im = fond
    elif im.mode != "RGB":
        im = im.convert("RGB")
    im.thumbnail((COTE_MAX, COTE_MAX))
    nom = prefixe + (".png" if garder_png else ".jpg")
    if garder_png:
        im.save(dossier / nom, "PNG", optimize=True)
    else:
        im.save(dossier / nom, "JPEG", quality=88, optimize=True)
    return nom, f"{im.width}×{im.height}"


def _photo(src: Path, dossier: Path, prefixe: str, importer: Importeur) -> Support:
    heic = src.suffix.lower() in (".heic", ".heif")
    try:
        Image, ImageOps = _pillow(importer, heic=heic)
    except Exception as exc:  # noqa: BLE001
        if heic:
            raise ValueError("HEIC illisible sans pillow-heif — convertis la photo en JPG") from exc
        if src.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif") \
                and src.stat().st_size < 5_000_000:
            nom = prefixe + src.suffix.lower()
            shutil.copyfile(src, dossier / nom)       # sans Pillow : copie telle quelle
            return Support(nom, "photo", src.name, "taille d'origine")
        raise ValueError(f"Pillow indisponible ({exc})") from exc

    with Image.open(src) as im:
        prise = _date_exif(im)
        garder_png = src.suffix.lower() in (".png", ".gif", ".bmp")   # captures, schémas : net
        im.load()
        nom, taille = _enregistrer_image(im, dossier, prefixe, Image, ImageOps, garder_png)
    return Support(nom, "photo", src.name, taille, prise=prise)


def _pdf(src: Path, dossier: Path, prefixe: str) -> Support:
    nom = prefixe + ".pdf"
    shutil.copyfile(src, dossier / nom)       # sans les attributs (lecture seule…)
    try:
        pages = len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", src.read_bytes()))
    except Exception:  # noqa: BLE001
        pages = 0
    return Support(nom, "pdf", src.name, f"≈ {pages} pages" if pages else "")


def _notes(src: Path, dossier: Path, prefixe: str) -> Support:
    texte = src.read_text(encoding="utf-8-sig", errors="replace")
    if not texte.strip():
        raise ValueError("fichier vide")
    nom = prefixe + ".notes.txt"
    (dossier / nom).write_text(texte, encoding="utf-8")
    return Support(nom, "notes", src.name, f"{len(texte.split())} mots")


def _formes(conteneur):
    """Toutes les formes, y compris celles des groupes."""
    for forme in conteneur:
        yield forme
        try:
            groupe = forme.shape_type == 6          # MSO_SHAPE_TYPE.GROUP
        except Exception:  # noqa: BLE001  (python-pptx : forme de type inconnu)
            groupe = False
        if groupe and hasattr(forme, "shapes"):
            yield from _formes(forme.shapes)


def _pptx(src: Path, dossier: Path, prefixe: str, importer: Importeur,
          avert: list[str]) -> list[Support]:
    pptx = importer("pptx", "python-pptx")
    try:
        prs = pptx.Presentation(str(src))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"présentation illisible ({exc})") from exc

    # Premier passage : les images et leur fréquence. Une image présente sur
    # trois diapositives ou plus est un logo ou un fond de modèle.
    frequence: dict[str, int] = {}
    images: list[tuple[int, str, bytes]] = []
    for n, diapo in enumerate(prs.slides, 1):
        vues: set[str] = set()
        for forme in _formes(diapo.shapes):
            try:
                blob = forme.image.blob          # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            empreinte = hashlib.sha1(blob).hexdigest()
            if empreinte not in vues:
                vues.add(empreinte)
                frequence[empreinte] = frequence.get(empreinte, 0) + 1
                images.append((n, empreinte, blob))

    retenues: list[tuple[int, str, bytes]] = []
    deja: set[str] = set()
    for n, empreinte, blob in images:
        if frequence[empreinte] >= 3 or empreinte in deja or len(blob) < 6_000:
            continue
        deja.add(empreinte)
        retenues.append((n, empreinte, blob))
    if len(retenues) > IMAGES_PAR_PPTX:
        avert.append(f"{src.name} : {len(retenues)} images, seules les "
                     f"{IMAGES_PAR_PPTX} plus grandes sont transmises")
        garde = {id(x) for x in sorted(retenues, key=lambda x: -len(x[2]))[:IMAGES_PAR_PPTX]}
        retenues = [x for x in retenues if id(x) in garde]

    supports: list[Support] = []
    par_diapo: dict[int, list[str]] = {}
    Image = ImageOps = None
    if retenues:
        try:
            Image, ImageOps = _pillow(importer)
        except Exception as exc:  # noqa: BLE001
            avert.append(f"{src.name} : images des diapositives non extraites ({exc})")
            retenues = []
    compteur: dict[int, int] = {}
    for n, _empreinte, blob in retenues:
        try:
            with Image.open(io.BytesIO(blob)) as im:          # type: ignore[union-attr]
                if min(im.size) < 120:
                    continue
                im.load()
                compteur[n] = compteur.get(n, 0) + 1
                nom, taille = _enregistrer_image(
                    im, dossier, f"{prefixe}_d{n:02d}_{compteur[n]}", Image, ImageOps,
                    garder_png=(im.format or "").upper() in ("PNG", "GIF", "BMP"))
        except Exception:  # noqa: BLE001  (EMF/WMF, formats exotiques)
            continue
        par_diapo.setdefault(n, []).append(nom)
        supports.append(Support(nom, "image_diapo", src.name, taille, diapo=n))

    # Texte et notes, diapositive par diapositive.
    lignes = [f"# Diapositives — {src.name}", ""]
    nb = 0
    for n, diapo in enumerate(prs.slides, 1):
        nb = n
        titre_forme = diapo.shapes.title
        titre = " ".join((titre_forme.text_frame.text if titre_forme is not None
                          and titre_forme.has_text_frame else "").split())
        lignes.append(f"## Diapositive {n}" + (f" — {titre}" if titre else ""))
        for forme in _formes(diapo.shapes):
            if titre_forme is not None and forme.shape_id == titre_forme.shape_id:
                continue
            if getattr(forme, "has_text_frame", False) and forme.has_text_frame:
                for p in forme.text_frame.paragraphs:
                    t = " ".join("".join(r.text for r in p.runs).split()) or " ".join(p.text.split())
                    if t:
                        lignes.append("  " * min(p.level, 4) + f"- {t}")
            if getattr(forme, "has_table", False) and forme.has_table:
                for rangee in forme.table.rows:
                    cellules = [" ".join(c.text.split()) for c in rangee.cells]
                    lignes.append("| " + " | ".join(cellules) + " |")
        if diapo.has_notes_slide:
            notes = diapo.notes_slide.notes_text_frame.text.strip() \
                if diapo.notes_slide.notes_text_frame is not None else ""
            if notes:
                lignes.append("> Notes de l'orateur : " + " ".join(notes.split()))
        if par_diapo.get(n):
            lignes.append("Images : " + ", ".join(f"supports/{x}" for x in par_diapo[n]))
        lignes.append("")

    nom_md = prefixe + ".diapos.md"
    (dossier / nom_md).write_text("\n".join(lignes), encoding="utf-8")
    texte_md = Support(nom_md, "diapos", src.name, f"{nb} diapositives")
    return [texte_md] + supports


# ----------------------------------------------------------------------
# Ce qui part vers Whisper et vers Claude
# ----------------------------------------------------------------------
def vocabulaire(supports: list[Support], dossier: Path, limite: int = 600) -> str:
    """Titres des diapositives, pour l'amorce de Whisper : il recopie
    l'orthographe qu'il y lit (termes techniques, noms propres)."""
    vus: list[str] = []
    for s in supports:
        if s.genre != "diapos":
            continue
        try:
            texte = (dossier / s.fichier).read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        for m in re.finditer(r"^## Diapositive \d+ — (.+)$", texte, re.M):
            t = m.group(1).strip(" .:")
            if 2 < len(t) <= 80 and t.lower() not in (v.lower() for v in vus):
                vus.append(t)
    texte = ""
    for t in vus:
        if len(texte) + len(t) + 2 > limite:
            break
        texte = f"{texte}, {t}" if texte else t
    return texte


def a_lire(supports: list[Support]) -> list[Support]:
    return [s for s in supports if s.genre in LISIBLES_PAR_CLAUDE]


def _situer(prise: str, debut: Optional[datetime.datetime], duree: float) -> str:
    """« ≈ [00:47] » si la photo a été prise pendant l'enregistrement."""
    if not prise or not debut:
        return ""
    try:
        d = datetime.datetime.strptime(prise, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    ecart = (d - debut).total_seconds()
    if not -120 <= ecart <= duree + 900:
        return ""
    s = max(0, int(ecart))
    return f" · prise vers [{s // 3600:02d}:{(s % 3600) // 60:02d}] de l'enregistrement (estimation)"


def bloc_pour_claude(supports: list[Support], dossier: Path, *,
                     debut: Optional[datetime.datetime] = None, duree: float = 0,
                     racine: Optional[Path] = None) -> str:
    """Le bloc <supports> placé dans l'entrée de Claude : la liste des
    fichiers à lire, et le texte des diapositives et notes en clair."""
    if not supports:
        return ""
    lignes = ["<supports>",
              "Supports fournis avec l'enregistrement, dans le dossier supports/ du "
              "répertoire courant" + (f" (chemin complet : {racine})" if racine else "")
              + ". Les photos, images et PDF sont à lire avec l'outil Read ; "
              "le texte des diapositives et des notes est reproduit plus bas.", ""]
    photos = 0
    for s in supports:
        chemin = f"supports/{s.fichier}"
        if s.genre == "photo":
            photos += 1
            quand = ""
            if s.prise:
                d = datetime.datetime.strptime(s.prise, "%Y-%m-%d %H:%M:%S")
                quand = f", prise le {d:%d/%m/%Y à %H:%M}"
            lignes.append(f"- {chemin} — photo {photos} (« {s.origine} », {s.detail}{quand})"
                          + _situer(s.prise, debut, duree))
        elif s.genre == "pdf":
            lignes.append(f"- {chemin} — PDF « {s.origine} »" + (f", {s.detail}" if s.detail else ""))
        elif s.genre == "diapos":
            lignes.append(f"- {chemin} — texte et notes des {s.detail} de « {s.origine} » "
                          "(reproduit ci-dessous)")
        elif s.genre == "image_diapo":
            lignes.append(f"- {chemin} — image de la diapositive {s.diapo} de « {s.origine} »")
        elif s.genre == "notes":
            lignes.append(f"- {chemin} — notes « {s.origine} » (reproduites ci-dessous)")

    for s in supports:
        if s.genre not in ("diapos", "notes"):
            continue
        try:
            texte = (dossier / s.fichier).read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if len(texte) > TEXTE_MAX:
            texte = texte[:TEXTE_MAX] + "\n[… tronqué : voir le fichier complet avec Read]"
        genre = "diapositives" if s.genre == "diapos" else "notes"
        lignes += ["", f'<document type="{genre}" fichier="{s.origine}">', texte.strip(), "</document>"]
    lignes.append("</supports>")
    return "\n".join(lignes)


def resume(supports: list[Support]) -> str:
    """« 3 photos, 1 PDF, 1 présentation (28 diapositives) »."""
    n = {g: sum(1 for s in supports if s.genre == g)
         for g in ("photo", "pdf", "diapos", "image_diapo", "notes")}
    morceaux = []
    if n["photo"]:
        morceaux.append(f"{n['photo']} photo" + ("s" if n["photo"] > 1 else ""))
    if n["diapos"]:
        diapos = sum(int(re.match(r"\d+", s.detail).group(0)) for s in supports
                     if s.genre == "diapos" and re.match(r"\d+", s.detail))
        morceaux.append(f"{n['diapos']} présentation" + ("s" if n["diapos"] > 1 else "")
                        + (f" ({diapos} diapos" + (f", {n['image_diapo']} image"
                                                    + ("s" if n["image_diapo"] > 1 else "") + ")"
                                                    if n["image_diapo"] else ")")
                           if diapos else ""))
    if n["pdf"]:
        morceaux.append(f"{n['pdf']} PDF")
    if n["notes"]:
        morceaux.append(f"{n['notes']} fichier" + ("s" if n["notes"] > 1 else "") + " de notes")
    return ", ".join(morceaux) or "aucun"
