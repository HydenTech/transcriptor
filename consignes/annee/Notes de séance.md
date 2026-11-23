---
# Mode année — étape 1 : une séance enregistrée devient des notes propres.
# Ces notes ne sont pas le document final : elles sont fusionnées ensuite, avec
# celles des autres séances, pour rédiger les fiches de la matière.
description: Nettoie et structure la transcription d'une séance (digressions retirées, tout le contenu du cours gardé).
# Modèle Claude proposé pour cette étape (moins exigeante que les fiches) :
# claude:
#   modele: sonnet
#   effort: medium
---

# Consignes — Notes d'une séance de cours

Tu reçois, entre balises, la **transcription automatique d'une séance de cours** enregistrée en auditoire (parfois en plusieurs fichiers), la matière, la date, l'enseignant, et le vocabulaire tiré des supports de la matière.

Ces notes seront fusionnées plus tard avec celles des autres séances pour rédiger des fiches de révision. Ton travail : **garder tout ce qui relève du cours, et rien d'autre**, sous une forme propre et structurée.

## Nettoyage

1. **Retire** ce qui n'a pas sa place dans des fiches : digressions et anecdotes personnelles de l'enseignant sans lien avec la matière, blagues, bavardages, échanges logistiques (appel, pauses, micro, salle, retards), répétitions, hésitations, questions d'étudiants inaudibles.
2. Une anecdote, un témoignage ou un cas vécu **qui illustre une notion** n'est pas une digression : garde-le comme exemple, reformulé en quelques lignes et **anonymisé** (prénom d'emprunt ou initiale pour les personnes accompagnées, pas de nom d'institution identifiable).
3. Les informations pratiques (examen, travaux, dates, documents, conseils pour réussir) ne sont pas des digressions : rassemble-les dans « Infos pratiques et examen ».

## Fidélité

4. **Corrige silencieusement** les erreurs évidentes de transcription (termes, auteurs, sigles) quand le mot juste est certain d'après le contexte ou le vocabulaire fourni.
5. **Signale par ⚠️** tout terme, nom, chiffre ou date incertain, avec la forme entendue : `Bowlby ⚠️ (transcrit : "boule bit")`.
6. **N'invente rien** : aucune définition, aucun auteur, aucun exemple absent de la transcription. Un passage inaudible ou incohérent devient `[passage inaudible]`.
7. Garde les **définitions** au plus près de la formulation de l'enseignant, surtout quand il les dicte ou les répète.
8. Marque par 🎯 chaque point sur lequel l'enseignant insiste (« retenez bien », « c'est important », « ça tombera à l'examen », répétition, passage dicté), avec le signal entre parenthèses.

## Structure de sortie (Markdown, rien d'autre)

```
# {Matière} — séance du {date}

## Contenu
### Thème…
#### Sous-thème…

## Infos pratiques et examen

## Zones d'ombre
```

- **Contenu** : organisé par thèmes (`###`) et sous-thèmes (`####`), dans l'ordre logique du cours. Définitions, explications, auteurs et théories, classifications, étapes, comparaisons, et les **exemples donnés en cours** (introduits par « Exemple : »). Phrases courtes, puces autorisées, **termes clés en gras**. Ajoute `[hh:mm]` à la fin de chaque thème.
- **Infos pratiques et examen** : forme de l'évaluation, matière à étudier, type de questions, pondération, documents autorisés, travaux et échéances, dates, documents Moodle ou lectures cités, conseils de l'enseignant pour réussir. Une puce par information, avec `[hh:mm]`. Si rien : *Néant.*
- **Zones d'ombre** : les ⚠️ et passages inaudibles qui comptent, sous forme de questions précises. Si rien : *Aucune.*
