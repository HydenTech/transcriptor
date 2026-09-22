# Transcriptor

Transforme l'enregistrement d'un cours — et, si tu les as, ses diapos et les
photos du tableau — en support de révision : transcription sur ta carte
graphique, synthèse par Claude selon un **modèle de consignes** que tu choisis
et modifies librement, Markdown et PDF mis en page.

Application Windows. Ni WSL, ni navigateur, ni terminal.

## Installation

Double-clic sur **`Installer.bat`**. Compter une dizaine de minutes et 4 Go.

À la fin, une session Claude Code s'ouvre pour connecter ton abonnement — suis
la procédure affichée, puis Ctrl+C. C'est la seule étape manuelle.

Un raccourci **Transcriptor** apparaît sur le bureau.

## Utilisation

Double-clic sur le raccourci. Ou dépose directement un fichier audio dessus :
il est chargé d'emblée.

Quelques champs, puis un bouton :

- **Supports du cours** *(facultatif)* — diapos PowerPoint (`.pptx`), PDF, photos
  du tableau (JPG, PNG, HEIC), notes (`.txt`, `.md`). Plusieurs fichiers à la
  fois. Voir [Diapos et photos du tableau](#diapos-et-photos-du-tableau).
- **Matière** — pré-remplie depuis le nom du fichier, corrige si besoin.
- **Consignes de synthèse** — le modèle qui dit à Claude quoi rédiger et au PDF
  comment se présenter. Voir [Régler la sortie](#régler-la-sortie).
- **Modèle Claude** et **effort** — qui rédige la synthèse. Voir
  [Choisir le modèle Claude](#choisir-le-modèle-claude).
- **Vocabulaire du chapitre** — le levier le plus rentable. Whisper recopie la
  casse qu'il lit ici : écrire `ArrayList<String>` évite « array liste ». Une
  trentaine de termes maximum. Les titres des diapos jointes s'y ajoutent
  automatiquement. Ce vocabulaire est redonné à Whisper à chaque tranche de
  30 secondes, sur tout l'enregistrement (auparavant, avec
  `condition_on_previous_text=False`, seule la première tranche en profitait).
- **Modèle** — `large-v3` par défaut. Environ 8 minutes pour 2 h de cours sur
  une RTX 3080.

## Où sortent les fichiers

Un dossier par cours dans `Documents\Transcriptor\` :

```
2026-08-18_algo_seance4\
├── transcription.txt      transcription horodatée à la minute
├── consignes.md           copie du modèle de consignes utilisé ce jour-là
├── supports\              diapos, photos et notes préparées (si fournies)
├── synthese.log           ce que Claude Code a renvoyé (diagnostic)
├── algo_seance4.md        le support de révision
└── algo_seance4.pdf       le même, mis en page
```

Le bouton **Ouvrir le dossier** t'y emmène directement.

## Refaire seulement la synthèse

La transcription (GPU, ~10 min) et la synthèse (Claude) sont indépendantes.
Si la synthèse échoue, la transcription est conservée :

- sur le moment, le bouton **Relancer la synthèse** repart de `transcription.txt`
  dans le même dossier ;
- plus tard, choisis le `transcription.txt` du dossier à la place de l'audio :
  l'app saute la transcription et ne refait que synthèse et PDF, avec le modèle
  de consignes choisi — et les supports déjà préparés pour ce cours. Ça sert
  aussi à essayer un autre modèle sur un cours déjà transcrit.
- pour ne refaire **que le PDF**, choisis le `.md` du support (ou clique
  **Refaire le PDF** sur l'écran de résultat) : utile pour ajuster la mise en page.

## Régler la sortie

Ce que Claude rédige et l'allure du PDF sont décrits par un **modèle de
consignes** : un fichier Markdown de `Documents\Transcriptor\Consignes\`. Le
modèle *Cours complet* (les 10 sections habituelles) y est installé au premier
lancement. Dans l'app, la liste **Consignes de synthèse** propose tous les
fichiers `.md` du dossier :

- **Modifier** ouvre le modèle dans ton éditeur (celui associé aux `.md`, sinon
  le Bloc-notes). La modification vaut pour le cours suivant.
- **Dupliquer** crée un nouveau modèle à partir de celui-ci — le bon réflexe pour
  une variante (Java avec reconstruction du code, quiz seul, version courte…).
- **Dossier** ouvre le dossier des modèles : tu peux aussi y déposer, renommer
  ou supprimer des fichiers.

Un modèle a deux parties :

```markdown
---
description: Ce qui s'affiche sous la liste.
pdf:
  couleur: "#1e4e8c"
  cartes: FICHE
  reponses: fin
---
# Les consignes pour Claude (tout ce qui suit le second ---)
```

**Le corps** est envoyé tel quel à Claude, avec la transcription : structure
des sections, règles de style, gabarits. Numérote les sections
(`### 1. En-tête`, `### 2. …`) : l'app s'en sert pour afficher l'avancement.

**L'en-tête** (facultatif) n'est lu que par l'app. Réglages du PDF, tous
facultatifs :

| Réglage | Défaut | Effet |
|---|---|---|
| `couverture` | `oui` | Bloc titre en haut de la première page |
| `surtitre` | `Support de révision` | Petite ligne au-dessus du titre |
| `titre` | `"{Thème de la séance}"` | `{Champ}` = valeur d'un tableau `\| Champ \| Valeur \|` du support ; à défaut, le titre `#` unique, puis le nom du fichier |
| `sous_titre` | `"{Matière} · {Durée}"` | Sous le titre |
| `pied_de_page` | `"{Matière}"` | À gauche du numéro de page |
| `couleur` | `"#1e4e8c"` | Bandeaux, cartes, tableaux (entre guillemets) |
| `niveau_sections` | `auto` | Niveau des grandes sections : `2` pour `##`, `3` pour `###` ; `auto` prend le plus haut niveau répété |
| `nouvelle_page` | `oui` | Chaque grande section commence une page |
| `cartes` | `FICHE` | Titres contenant ce mot (mot entier) encadrés en cartes insécables ; plusieurs mots séparés par des virgules ; `non` pour aucune |
| `reponses` | `visibles` | Réponses des `<details>` : `visibles`, `masquees` (quiz à remplir) ou `fin` (corrigé en fin de document) |
| `sommaire` | `non` | Liste des sections après la couverture |
| `format` / `orientation` | `A4` / `portrait` | `A3`, `A4`, `A5`, `Letter`, `Legal` / `portrait`, `paysage` |
| `marges` | `14mm 14mm 16mm 14mm` | Haut, droite, bas, gauche (1 à 4 valeurs) |
| `taille_texte` | `10.5pt` | Taille du texte courant ; les titres suivent |
| `police` | *(Arial)* | Nom d'une police installée |
| `css` | — | CSS libre, appliqué en dernier (`css: \|` puis lignes indentées) |

Le PDF s'adapte à n'importe quelle structure : un modèle sans fiches ni quiz
donne un PDF sans fiches ni quiz. Un réglage mal écrit est signalé sous la
liste des consignes et remplacé par sa valeur par défaut.

Pour **ajuster la mise en page** sans relancer Claude : modifie l'en-tête `pdf:`,
puis **Refaire le PDF** sur l'écran de résultat.

## Choisir le modèle Claude

Deux listes sous les consignes règlent la synthèse (et seulement elle) :

| Modèle | Pour quoi |
|---|---|
| *Par défaut du compte* | le modèle réglé pour ton abonnement dans Claude Code |
| **Le meilleur disponible** (`best`) | le plus capable auquel ton compte a accès |
| **Fable** | le plus capable, taillé pour les longues tâches : cours de 2 h avec beaucoup de supports |
| **Opus** | raisonnement soigné ; bon choix pour des fiches exigeantes |
| **Sonnet** | équilibré, nettement plus rapide |
| **Haiku** | rapide et économe : résumé express, quiz |
| *Autre identifiant…* | un nom complet (`claude-opus-5-5`) ou une variante (`opus[1m]`, `sonnet[1m]` pour un contexte d'un million de tokens) |

Les noms courts sont des alias de Claude Code : ils suivent d'eux-mêmes les
nouvelles versions. L'**effort** (`low` → `max`) règle le temps de réflexion :
plus haut, plus fin, plus lent. Tous les niveaux n'existent pas pour tous
les modèles.

- Le choix est mémorisé d'un lancement à l'autre.
- Un modèle de consignes peut proposer son modèle Claude (bloc `claude:` de
  l'en-tête, voir l'exemple commenté dans *Cours complet*) : le choisir dans la
  liste présélectionne ce modèle, que tu peux encore changer.
- Le modèle réellement utilisé s'affiche sous le support (« Synthèse par
  claude-… ») et dans `synthese.log`.
- **Limite d'utilisation atteinte** ou **modèle indisponible** : l'écran d'erreur
  propose de relancer la synthèse avec un autre modèle, sans retranscrire.

```yaml
claude:
  modele: sonnet
  effort: medium
```

## Diapos et photos du tableau

Ajoute-les avec **Ajouter des diapos ou photos** : l'étape **Supports** les
prépare en local avant la transcription.

| Support | Préparation | Ce que Claude en fait |
|---|---|---|
| `.pptx` | texte de chaque diapo, **notes de l'orateur**, images (sans les logos répétés) | lit le texte, regarde les images |
| `.pdf` | copié tel quel | le lit en entier, texte et visuel |
| Photos JPG, PNG, HEIC | redressées (EXIF), réduites à 1600 px, rangées par heure de prise de vue | les regarde, peut les insérer dans le support |
| `.txt`, `.md` | tels quels | tes notes, lues avec la transcription |

La corrélation se fait à deux moments :

- **à la transcription** : les titres des diapos rejoignent le vocabulaire donné
  à Whisper, qui orthographie mieux les termes techniques ;
- **à la synthèse** : Claude reçoit consignes, supports et transcription, et
  croise tout — termes et formules corrigés d'après les diapos, schémas du
  tableau repris, renvois `[00:47 · diapo 12]`, photos du tableau insérées dans
  les fiches (elles apparaissent dans le PDF). L'heure de prise de vue d'une
  photo, comparée à l'heure de début de l'enregistrement quand le fichier audio
  la porte, aide à la situer dans le cours. Les règles de croisement sont dans
  le modèle de consignes (partie *Supports du cours*) : modifiables comme le reste.

Un `.ppt` (ancien format) n'est pas lisible : dans PowerPoint, **Enregistrer
sous** `.pptx`, ou **Exporter** en PDF — le PDF garde la mise en page exacte des
diapos. Au premier support, l'app installe si besoin ce qu'il lui faut
(python-pptx, Pillow, pillow-heif) : il faut être connecté.

## Dépannage

**« Claude Code n'est pas connecté »** — ouvre une invite de commandes, tape
`claude`, suis la procédure. Une seule fois.

**« Claude Code x.y.z does not support this model »** — le CLI est trop
ancien pour le modèle de ton compte. En mode headless il ne se met jamais à
jour seul ; l'app le fait donc à chaque lancement (point d'état sous le bouton)
et, si l'erreur survient quand même, met à jour puis réessaie. À la main, dans
une **invite de commandes Windows** (pas WSL) : `claude update`, ou
`npm install -g @anthropic-ai/claude-code@latest`.

**La synthèse échoue** — le message affiché est celui de Claude Code (limite
d'utilisation de l'abonnement, connexion, tours dépassés…). Le détail complet
est dans `synthese.log`, dans le dossier du cours ; si Claude avait déjà rédigé
une partie, elle est dans `synthese_partielle.md`. Clique **Relancer la
synthèse** : rien n'est à retranscrire.

**La synthèse est interrompue « aucune sortie depuis 20 min »** — Claude n'a
plus rien envoyé pendant 20 minutes (réseau, service surchargé). Relance-la.
Le plafond absolu est de deux heures.

**Un support est « ignoré »** — la raison s'affiche sous les étapes (`.ppt`
ancien format, fichier vide, photo HEIC sans pillow-heif installable…). Le cours
continue sans lui.

**Le PDF ne suit pas mes réglages** — un réglage mal écrit est signalé sous la
liste des consignes et sous les étapes, puis remplacé par sa valeur par défaut.
Mets entre guillemets les valeurs qui contiennent `{…}` ou commencent par `#`.

**L'étape PDF est ignorée** — Chromium ne s'est pas installé. Dans une invite
de commandes :
`%LOCALAPPDATA%\Transcriptor\venv\Scripts\python -m playwright install chromium`
Le Markdown reste produit dans tous les cas.

**Transcription très lente** — CUDA n'a pas été trouvé, l'app est passée sur le
processeur. Mets à jour le pilote NVIDIA (`nvidia-smi` doit répondre dans une
invite de commandes).

**Le premier cours est long à démarrer** — le modèle `large-v3` se télécharge,
environ 3 Go. Il reste ensuite en mémoire d'un cours à l'autre.

**La fenêtre ne s'ouvre pas** — lance `Transcriptor.bat` : il affiche les
erreurs. Un rapport est aussi écrit dans `Documents\Transcriptor\erreur.txt`.

## Ce qui tourne où

| Étape | Où | Ce qui sort de la machine |
|---|---|---|
| Transcription | ta carte graphique | rien |
| Préparation des supports | ton PC | rien |
| Synthèse | Claude Code | la transcription, les consignes et les supports joints |
| Mise en page | Chromium local | rien |

L'audio ne quitte jamais le PC. Seuls le texte transcrit, les consignes et les
supports que tu joins sont envoyés, à l'étape de synthèse, via ton abonnement
Claude.
