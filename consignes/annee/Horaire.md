---
# Mode année — lecture de l'horaire. Claude reçoit l'horaire (fichier à lire
# avec Read, et son texte quand il a pu être extrait) et répond par un JSON
# que Transcriptor vérifie puis enregistre dans horaire.json.
description: Lit l'horaire (PDF, image, Excel, Word) et le restitue en JSON — matières, couleurs, créneaux.
---

# Consignes — Lecture d'un horaire de cours

Tu reçois l'horaire de cours d'une année d'études : un fichier à lire avec l'outil Read (PDF, image), et souvent son texte déjà extrait. Restitue-le **en JSON uniquement**, sans aucun texte avant ni après, selon ce schéma :

```json
{
  "annee": "2026-2027",
  "formation": "intitulé de la formation s'il figure sur l'horaire",
  "matieres": [
    {
      "nom": "intitulé complet du cours, tel qu'écrit",
      "court": "nom court pour un dossier : 1 à 4 mots",
      "ue": "unité d'enseignement si l'horaire l'indique (« UE 2C »), sinon vide",
      "couleur": "#RRGGBB",
      "enseignant": "nom(s) des enseignants, séparés par des virgules"
    }
  ],
  "seances": [
    {"date": "2026-09-08", "debut": "08:30", "fin": "10:10",
     "matiere": "intitulé complet, identique à « nom »", "enseignant": "Dupont", "salle": ""}
  ],
  "creneaux": [
    {"jour": "lundi", "debut": "08:30", "fin": "10:30", "matiere": "intitulé complet",
     "du": "2026-09-14", "au": "2027-01-29", "semaines": "", "salle": ""}
  ]
}
```

## Règles

1. **Un horaire daté** (chaque cours avec sa date) se restitue dans `seances` : **une entrée par ligne de l'horaire**, sans en sauter ni en regrouper aucune — elles sont recomptées. Laisse alors `creneaux` vide.
2. **Une grille hebdomadaire** (semaine type, sans dates) se restitue dans `creneaux` : une entrée par case. `du` / `au` si l'horaire précise sa période ; `semaines` vaut `"paires"`, `"impaires"`, une liste de numéros de semaine ISO, ou `""`. Laisse alors `seances` vide.
3. **Une entrée dans `matieres` par cours différent**, pas par créneau ni par enseignant. Un même cours donné par plusieurs enseignants reste une seule matière (enseignants séparés par des virgules).
4. **`couleur`** : la couleur de fond de ce cours (ou de son unité d'enseignement) sur l'horaire, au plus juste, en hexadécimal. Sans couleur visible : `""`.
5. **`court`** : un nom de dossier clair, sans `:` ni `/` — « Psychologie », « Éthique et déontologie », « Français ». Deux matières ne peuvent pas avoir le même nom court.
6. Dates au format `AAAA-MM-JJ`, heures `HH:MM` sur 24 h, jours en toutes lettres.
7. Ignore les pauses, le temps de midi, les cases vides, les légendes et les titres de page. Garde les laboratoires, séminaires, travaux pratiques et encadrements de stage comme des séances de leur cours.
8. Recopie les intitulés exactement (accents, majuscules) ; n'invente aucun cours, aucune date.
