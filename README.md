# PDF interactif (carte cliquable) — Côte-d'Or

Ce dossier contient un petit générateur **sans dépendances** qui produit un **HTML imprimable en PDF** :

- Page 1 : une **carte des communes de Côte-d'Or** (département `21`)
- Chaque commune sélectionnée est **cliquable** et renvoie vers sa **fiche** (pages suivantes)
- Chaque fiche contient les résultats **Législatives 2024** + **Municipales 2026** (à partir de tes CSV)

Le PDF final se fait en pratique via **Imprimer → Enregistrer en PDF** depuis Chrome/Edge (les liens internes restent cliquables).

## Pré-requis

- Windows + Python 3.12+ (déjà ok si `python --version` fonctionne)

## Démarrage rapide

1) Mets tes fichiers dans :

- `data/legislatives2024/` : 1 CSV par candidat
- `data/municipales2026/` : soit 1 CSV par candidat, soit 1 CSV par commune (voir ci-dessous)

Tu peux aussi organiser les Législatives par circonscription en sous-dossiers (le script lit récursivement) :

- `data/legislatives2024/circ2/*.csv`
- `data/legislatives2024/circ3/*.csv`

Exemple (avec tours) :

- `data/legislatives2024/circ2/Dupont_T1.csv`
- `data/legislatives2024/circ2/Dupont_T2.csv`
- `data/legislatives2024/circ3/Martin_T1.csv`
- `data/legislatives2024/circ3/Martin_T2.csv`

### Cas particulier : une commune dans 2 circonscriptions (ex: Dijon 21231)

Si une commune est partagée entre plusieurs circonscriptions, mets les lignes **par bureau de vote** dans le bon sous-dossier (`circ2/`, `circ3/`, etc.).  
Le rapport affichera alors, dans la fiche de la commune, une section séparée **par circonscription** (ex: “Circonscription 2” et “Circonscription 3”) avec les bons totaux.

Pour Dijon (`21231`) en particulier, le rapport crée maintenant 2 fiches :

- `Dijon (21231) 2` : municipales (tout Dijon) + législatives (partie circo 2)
- `Dijon (21231) 3` : municipales (tout Dijon) + législatives (partie circo 3)

2) Optionnel : sélection de communes à afficher (recommandé)

- `data/communes_selection.csv` avec une colonne `insee` (ex: `21054`)

3) Génère le HTML :

```powershell
python .\scripts\generate_cote_dor_report.py
```

Sortie :

- `dist/cote-dor-resultats.html`

4) Fais le PDF :

- Ouvre `dist/cote-dor-resultats.html` dans Chrome/Edge
- `Ctrl+P` → Destination : **Enregistrer en PDF**

## Déploiement Netlify (petit site)

Le site peut être déployé comme un site statique Netlify. La config est prête via `netlify.toml`.

- Dossier publié : `dist/`
- Commande de build : `python scripts/generate_cote_dor_report.py --municipales-layout per_commune --out dist/index.html`

Étapes :
1) Pousse le projet sur GitHub
2) Netlify → “Add new site” → “Import from Git”
3) Vérifie que `Publish directory` = `dist`
4) Lance un deploy

## Notes sur la carte (GeoJSON)

Par défaut, le script tente de télécharger le GeoJSON des communes du 21 depuis `geo.api.gouv.fr` et le met en cache dans `cache/communes-21.geojson`.

Si tu es hors-ligne, tu peux mettre ton propre GeoJSON ici :

- `data/communes-21.geojson`

## Dépannage colonnes CSV

Pour voir ce que le script détecte par fichier :

```powershell
python .\scripts\generate_cote_dor_report.py --verbose
```

Si besoin, tu peux forcer des noms de colonnes (même nom dans tous tes CSV) :

```powershell
python .\scripts\generate_cote_dor_report.py --force-insee-col "Code commune" --force-voix-col "Voix" --force-pct-col "Pourcentage"
```

## Format CSV attendu (flexible)

Comme tes CSV sont “1 fichier par candidat”, le script essaye de détecter automatiquement :

- le code commune (INSEE) : colonne contenant `insee`, `codgeo`, `code_commune`, etc.
- le nom commune : colonne contenant `commune`, `libelle`, `nom`, etc.
- les voix : colonne contenant `voix`, `votes`, `suffrages`, etc.
- le % (optionnel) : `pourcentage`, `pct`, `%`, etc.
- si c’est par bureaux : une colonne `code_bv` (le rapport affichera un top 1 / top 2 par bureau)

## Municipales 2026 — 1 CSV par commune (T1/T2)

Pour démarrer la partie municipales comme tu veux :

- Mets `T1` ici : `data/municipales2026/<INSEE>_T1.csv`
- Mets `T2` ici : `data/municipales2026/<INSEE>_T2.csv` (si 2e tour)

Exemple : `data/municipales2026/21231_T1.csv` et `data/municipales2026/21231_T2.csv`.

Colonnes recommandées : `insee`, `commune`, `candidat` (ou `liste`), `voix`, `pct` (optionnel), `code_bv` (optionnel).

Tu peux aussi avoir **1 seul CSV pour tout le Tour 1** (et un autre pour le Tour 2) tant que le nom du fichier contient `T1` / `T2` et qu'il y a une colonne `insee`.

Par défaut, les municipales sont filtrées aux communes d’intérêt (celles présentes dans tes législatives, ou `communes_selection.csv` si tu l’utilises). Si besoin, tu peux changer ça avec `--municipales-scope all`.

Quand `T1` et `T2` existent pour une même commune, le rapport affiche par défaut **uniquement le T2** (et garde le T1 seulement pour les communes absentes du fichier T2).  
Si tu veux forcer l’affichage des deux tours : `--municipales-tour-policy both`.

## Couleurs circo sur la carte

Pour distinguer visuellement **circo 2** et **circo 3** sur la carte, le script lit :

- `data/circo2_communes.csv` (liste INSEE des communes en circo 2)

Les communes sélectionnées qui ne sont pas dans cette liste sont colorées comme **circo 3**.  
Si une commune est partagée (ex: Dijon `21231`), elle peut apparaître en **violet**.

Le nom du candidat est, par défaut, le **nom du fichier** (sans extension).

Si tes colonnes ne matchent pas, dis-moi le nom exact des colonnes et je t’ajuste le détecteur.

## Astuce si tu n'as pas `communes_selection.csv`

Sans fichier de sélection, le script rend cliquables uniquement les communes qui apparaissent dans tes CSV (union des INSEE trouvés dans `data/legislatives2024/` et `data/municipales2026/`).

## Pourquoi parfois un candidat apparaît 2 fois ?

Souvent, c’est parce que tes CSV mélangent **Tour 1 / Tour 2** (Législatives) ou que tu as 2 exports par candidat.

Le script essaie de détecter le tour :
- via une colonne `tour` (si elle existe)
- sinon via le **nom du fichier** (ex: `Dupont_T1.csv`, `Dupont_T2.csv`)

Quand il détecte les deux tours, il affiche deux sous-tableaux `T2` puis `T1` dans la fiche commune.
