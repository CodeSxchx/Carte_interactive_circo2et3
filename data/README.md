# Données d'entrée

## Sélection des communes

Créer `communes_selection.csv` si tu veux n’afficher que certaines communes.

Exemple :

```csv
insee
21054
21231
```

Sans ce fichier, toutes les communes du département seront cliquables.

## Résultats par élection

Mettre **1 CSV par candidat** dans chaque dossier :

- `legislatives2024/`
- `municipales2026/`

Le script va regrouper ces CSV par commune et afficher une table par commune et par élection.

## Municipales 2026 (mode 1 fichier par commune)

Tu peux aussi utiliser (recommandé pour les municipales) :

- `municipales2026/21231_T1.csv`
- `municipales2026/21231_T2.csv` (optionnel si élu au 1er tour)

Chaque fichier contient une ligne par candidat/liste (et éventuellement par bureau via `code_bv`).

Alternative : tu peux aussi mettre **un seul CSV** pour tout le `T1` (et un autre pour `T2`) dans `municipales2026/` si le nom contient `T1` / `T2` et qu'il y a une colonne `insee`.

Si tes CSV sont au **niveau bureaux de vote** (colonne `code_bv`), le rapport affiche aussi :

- une section **“Par bureau de vote”** avec le top 1 / top 2 par bureau
- un tableau **“Total commune (somme des bureaux)”** par candidat

Colonnes minimales recommandées dans chaque CSV :

- `insee` (code INSEE commune)
- `commune` (nom)
- `code_bv` (optionnel, si résultats par bureau)
- `voix` (nombre de voix)
- `pct` (optionnel)

Si tu as d’autres colonnes (inscrits, exprimés, etc.), on pourra les afficher ensuite.
