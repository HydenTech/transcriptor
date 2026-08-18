# Transcriptor

Transforme l'enregistrement d'un cours en support de révision : transcription
sur ta carte graphique, synthèse par Claude selon les règles de `CLAUDE.md`,
Markdown et PDF mis en page.

Application Windows. Ni WSL, ni navigateur, ni terminal.

## Installation

Double-clic sur **`Installer.bat`**. Compter une dizaine de minutes et 4 Go.

À la fin, une session Claude Code s'ouvre pour connecter ton abonnement — suis
la procédure affichée, puis Ctrl+C. C'est la seule étape manuelle.

Un raccourci **Transcriptor** apparaît sur le bureau.

## Utilisation

Double-clic sur le raccourci. Ou dépose directement un fichier audio dessus :
il est chargé d'emblée.

Trois champs, puis un bouton :

- **Matière** — pré-remplie depuis le nom du fichier, corrige si besoin.
- **Vocabulaire du chapitre** — le levier le plus rentable. Whisper recopie la
  casse qu'il lit ici : écrire `ArrayList<String>` évite « array liste ». Une
  trentaine de termes maximum, au-delà l'amorce est tronquée.
- **Modèle** — `large-v3` par défaut. Environ 8 minutes pour 2 h de cours sur
  une RTX 3080.

## Où sortent les fichiers

Un dossier par cours dans `Documents\Transcriptor\` :

```
2026-08-18_algo_seance4\
├── transcription.txt      transcription horodatée à la minute
├── CLAUDE.md              copie des règles utilisées ce jour-là
├── algo_seance4.md        le support de révision
└── algo_seance4.pdf       le même, mis en page
```

Le bouton **Ouvrir le dossier** t'y emmène directement.

## Régler la sortie

Tout le format du support est piloté par `CLAUDE.md`, à côté de l'application.
Modifie-le, relance un cours, la sortie suit. Une copie part dans chaque dossier
de cours : tu sais toujours quelles règles ont produit quel document.

Pour la mise en page du PDF (couleurs, marges, sauts de page), voir la variable
`CSS` dans `scripts\generate_pdf.py`.

## Dépannage

**« Claude Code n'est pas connecté »** — ouvre une invite de commandes, tape
`claude`, suis la procédure. Une seule fois.

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
| Synthèse | Claude Code | la transcription seule |
| Mise en page | Chromium local | rien |

L'audio ne quitte jamais le PC. Seul le texte transcrit est envoyé, à l'étape de
synthèse, via ton abonnement Claude.
