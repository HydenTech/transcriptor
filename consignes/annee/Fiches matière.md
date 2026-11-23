---
# Mode année — étape 2 : toutes les séances d'une matière (et ses supports)
# deviennent des fiches résumés suivies, mises en page en Word.
description: Fiches résumés suivies d'une matière — catégories en couleurs, mots-clés en gras, exemples, schémas, tableaux.

# claude:
#   modele: opus
#   effort: high

# Mise en page du document Word. Après une modification, « Refaire les Word »
# applique les réglages sans relancer Claude.
docx:
  couleurs: niveaux               # niveaux : catégories en rose, sous-catégories en bleu, puis violet, vert
                                  # categories : une couleur par catégorie (rose, puis bleu, puis violet…)
  palette: rose, bleu, violet, vert, orange, turquoise
  surtitre: Fiches résumés
  nouvelle_page: oui              # chaque catégorie commence une page
  sommaire: oui
  schemas: oui                    # blocs mermaid dessinés en images
  police: Calibri
  taille_texte: 11
  marges: 2cm
---

# Consignes — Fiches résumés d'une matière

Tu reçois, entre balises :
- `<matiere>` : la matière, sa formation, ses enseignants et les séances enregistrées ;
- `<notes>` : les notes de chaque séance, dans l'ordre chronologique, déjà nettoyées des digressions ;
- `<supports>` : les documents de la matière (PDF de Moodle, diapositives, notes, travaux) — texte reproduit, ou fichiers à lire avec l'outil Read.

Toutes ces séances forment **un seul et même cours**. Rédige des **fiches résumés suivies** qui couvrent toute la matière : une progression logique unique, organisée par catégories et sous-catégories — jamais un résumé séance par séance.

## Fond

1. **Fusionne.** Une notion vue en plusieurs séances est traitée une seule fois, à sa place logique, en réunissant tout ce qui en a été dit.
2. **Croise avec les supports.** L'oral fait foi pour ce sur quoi l'enseignant a insisté ; les supports font foi pour l'orthographe des termes et des auteurs, les définitions écrites, les schémas et les chiffres. Ce qui n'apparaît que dans les supports est intégré, résumé, avec la mention *(support)*. Ne recopie pas les supports.
3. **Des exemples concrets partout.** Chaque notion importante est illustrée par au moins un exemple concret : en priorité ceux donnés en cours ou dans les supports (« Exemple »). À défaut, rédige un exemple court et réaliste tiré du terrain professionnel de la formation (situation avec une personne accompagnée, une équipe, une institution), anonymisé, signalé comme « Exemple (illustration) ».
4. **Rien d'inutile.** Ni anecdote, ni digression, ni information administrative (elles vont dans la fiche « Infos importantes »), ni horodatage, ni référence aux séances (« comme vu le 12/10 »).
5. **N'invente pas de contenu de cours.** Pas de théorie, d'auteur, de chiffre ou de définition absents des notes et des supports. Les ⚠️ restés incertains sont reportés tels quels.
6. Les points marqués 🎯 dans les notes sont des priorités : développe-les et termine leur sous-catégorie par un « À retenir ».

## Forme — le document est converti en Word : respecte exactement ces conventions

- **Pas de titre `#`** : le nom de la matière est ajouté automatiquement, dans sa couleur de l'horaire.
- `##` = **catégorie** (grande partie de la matière), `###` = **sous-catégorie**, `####` = point précis. Aucun niveau plus bas. Vise 4 à 10 catégories ; titres courts et parlants, sans numérotation. Le code couleur suit ces niveaux : respecte la hiérarchie.
- Sous chaque titre : quelques lignes ou puces denses, phrases courtes. **Mots-clés en gras** : chaque notion, auteur, terme technique ou idée maîtresse — 1 à 4 par paragraphe ou par puce, jamais une phrase entière.
- **Encadrés** : un paragraphe qui commence par `> **Définition** :`, `> **Exemple** :`, `> **Exemple (illustration)** :`, `> **À retenir** :`, `> **Attention** :` ou `> **Conseil** :` devient un encadré coloré. Une définition clé par notion au plus ; un « Attention » pour une confusion fréquente ou un piège signalé ; termine chaque sous-catégorie importante par un « À retenir » d'une ou deux phrases. Laisse une ligne vide entre deux encadrés.
- **Tableaux comparatifs** en Markdown (`| … |`) dès qu'on compare au moins deux notions sur plusieurs critères (auteurs, théories, stades, approches, types de troubles, dispositifs…). 2 à 5 colonnes, cellules brèves, mots-clés en gras.
- **Schémas** : quand un enchaînement, une classification, un cycle ou des relations se comprennent mieux en image, insère un bloc de code `mermaid` — `flowchart TD` (de haut en bas : processus, étapes, causes et effets, classifications) ou `mindmap` (une notion et ses composantes). Étiquettes courtes (6 mots au plus), 3 à 10 éléments, sans couleurs ni styles (la mise en page s'en charge), guillemets autour d'une étiquette qui contient des parenthèses ou des deux-points : `A["Stade 1 : sensori-moteur"]`. Fais suivre chaque bloc d'une ligne en italique qui sert de légende : `*Schéma — …*`. Environ un schéma par catégorie quand il apporte quelque chose, jamais décoratif.
- Pas d'emoji, pas de HTML, pas de LaTeX.

## Pour finir

Termine par une catégorie `## Lexique` : tableau `| Terme | Définition courte |` des termes techniques de la matière, par ordre alphabétique.
