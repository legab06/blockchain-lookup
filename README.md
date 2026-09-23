# 🔎 Blockchain Lookup

**Blockchain Lookup** est une application Streamlit permettant de retrouver rapidement une opération sur **Solana**, **Ethereum** ou **Bitcoin** à partir d'informations partielles :

- une **date UTC** ;
- une **heure approximative UTC** ;
- une **tolérance temporelle** en secondes ;
- éventuellement un **montant** et l'actif concerné.

L'objectif est de pouvoir exploiter une information du type _« opération d'environ 0,5472056 ETH vers 11:19:57 UTC »_ sans connaître au préalable le hash de transaction, l'adresse ou le bloc.

## Réseaux pris en charge

| Réseau | Actifs disponibles dans l'interface | Source principale | Analyse |
| --- | --- | --- | --- |
| **Solana** | SOL, USDC, USDT | JSON-RPC Solana Mainnet | transactions, transferts natifs/tokens, variations de solde, swaps probables |
| **Ethereum** | ETH, USDC, USDT | JSON-RPC Ethereum | transactions, reçus, transferts ERC-20, mouvements ETH/tokens, swaps probables |
| **Bitcoin** | BTC | API mempool.space | blocs confirmés, transactions brutes, sorties BTC (vout) |

Les liens vers les explorateurs sont intégrés directement dans les résultats :

- Solana Explorer / Solscan ;
- Etherscan / Blockscout ;
- mempool.space / Blockstream.

---

## Fonctionnalités

### Recherche par fenêtre temporelle

La recherche part d'une date et d'une heure UTC, puis construit une fenêtre :

```text
heure recherchée ± tolérance
```

Exemple :

```text
22/09/2026 11:19:57 UTC
Tolérance : ± 30 secondes

=> fenêtre analysée :
11:19:27 → 11:20:27 UTC
```

Le moteur localise ensuite les blocs ou slots correspondant à cette période et n'analyse que la zone utile.
Si la fenêtre déborde l'historique disponible ou le dernier bloc confirmé, la
recherche est signalée comme **partielle**. Une fenêtre entièrement hors
historique provoque une erreur explicite ; une absence de correspondance dans
une recherche partielle n'est pas concluante pour la portion non couverte.

### Montant facultatif

Le montant peut être laissé vide. Dans ce cas, Blockchain Lookup affiche toutes les transactions et opérations observées dans la fenêtre temporelle.

Si un montant est renseigné, il est utilisé pour produire une vue **Résultats** contenant les correspondances détectées.

Les séparateurs décimaux `.` et `,` sont acceptés.

### Correspondance exacte ou approchée

Le moteur conserve la **précision réellement saisie par l'utilisateur**.

Par exemple :

```text
Montant saisi : 0.5472056 ETH
Valeur on-chain : 0.547205695045168545 ETH
```

La valeur peut être classée comme **approchée**, car l'utilisateur n'a fourni que 7 décimales.

La tolérance est dérivée du nombre de décimales saisies et reste plafonnée à **0,000001 unité** afin d'éviter les faux positifs trop larges.

La comparaison est stricte :

```text
écart < tolérance
```

Précisions maximales :

| Actif | Décimales |
| --- | ---: |
| SOL | 9 |
| ETH | 18 |
| BTC | 8 |
| USDC / USDT | 6 |

---

## Spécificités par blockchain

### Solana

Le moteur Solana :

- localise les slots proches de l'horodatage demandé ;
- analyse les transactions de la fenêtre ;
- détecte les transferts SOL ;
- détecte les transferts de tokens connus ;
- exploite les soldes avant/après transaction ;
- inspecte les instructions internes lorsque cela apporte une preuve supplémentaire ;
- détecte heuristiquement certains **swaps probables** ;
- recherche également le montant brut encodé dans certaines instructions.

Les transactions Solana échouées restent visibles dans **Transactions**, sans
être traitées comme des transferts ou swaps exécutés. Les montants seulement
présents dans des données brutes sont signalés comme des **indices techniques**.

Les appels sont volontairement limités et temporisés pour mieux supporter les restrictions du RPC public.

### Ethereum

Le moteur Ethereum :

- localise les blocs correspondant à la fenêtre UTC ;
- récupère les transactions complètes ;
- récupère les reçus par lots ;
- détecte les transferts ETH natifs ;
- décode les événements ERC-20 `Transfer` ;
- reconstitue des mouvements par actif ;
- détecte certains **swaps probables** à partir des mouvements visibles ;
- peut retrouver un montant encodé dans le calldata lorsqu'il complète une détection de swap.

Le moteur détecte également les RPC dont l'historique a été **pruné**. Si la période demandée est antérieure au premier bloc conservé par le fournisseur, l'application indique qu'un RPC avec historique plus ancien ou de type archive est nécessaire.

Un reçu Ethereum indisponible donne le statut **Statut inconnu** et rend la
recherche partielle ; il ne prouve pas une exécution échouée. Un montant
présent uniquement dans le calldata d'un swap est un indice technique. WETH
et WSOL peuvent compter économiquement comme ETH et SOL dans la détection de
swaps, mais l'actif observé reste identifié dans les résultats.

> Les transferts ETH internes exécutés exclusivement à l'intérieur de contrats ne sont pas tous observables avec le JSON-RPC standard. Une API de traces serait nécessaire pour une couverture exhaustive de ce cas.

### Bitcoin

Le moteur Bitcoin fonctionne différemment des deux autres réseaux.

Bitcoin n'associe pas un timestamp on-chain individuel à chaque transaction confirmée. Blockchain Lookup utilise donc **l'horodatage du bloc de confirmation**.

Le moteur :

- localise les blocs proches de la fenêtre UTC ;
- si aucun bloc n'est horodaté dans une fenêtre courte, analyse le **bloc le plus proche** et affiche explicitement l'écart temporel ;
- télécharge le **bloc brut** ;
- parse localement ses transactions pour limiter le nombre d'appels réseau ;
- prend en charge les transactions legacy et SegWit ;
- calcule les TXID localement ;
- décode les sorties standards :
  - P2PKH ;
  - P2SH ;
  - P2WPKH ;
  - P2WSH ;
  - Taproot / P2TR ;
- recherche les montants directement en **satoshis** ;
- affiche les sorties correspondantes avec leur adresse lorsque le script permet de la reconstruire.

La recherche BTC actuelle concerne les **transactions confirmées** et se concentre sur les sorties `vout`. Elle ne tente pas de reconstruire systématiquement la valeur et l'adresse de chaque entrée à partir des transactions précédentes.

---

## Interface

Après une recherche, plusieurs vues sont disponibles :

- **🎯 Résultats** — correspondances exactes ou approchées avec le montant demandé ;
- **🧾 Transactions** — transactions observées dans la fenêtre ;
- **💸 Opérations** — transferts et opérations détectées ;
- **📊 Variations / Mouvements / Sorties BTC** — vue plus détaillée des mouvements selon le réseau ;
- **ℹ️ Résumé de la recherche** — bornes et plage réellement interrogée.

Les tableaux proposent également :

- recherche textuelle globale ;
- filtrage des transactions réussies lorsque pertinent ;
- affichage antéchronologique ;
- pagination de **100 / 250 / 500 lignes** ;
- export CSV de la page affichée ;
- génération du CSV complet uniquement à la demande.

Cette organisation évite de rendre simultanément plusieurs gros tableaux dans Streamlit et limite la consommation mémoire côté serveur et navigateur.

---

## Installation locale

### Prérequis

- Python 3.11+ recommandé ;
- accès Internet aux RPC/API publics utilisés.

### Installation

```bash
git clone https://github.com/legab06/blockchain-lookup.git
cd blockchain-lookup

python -m venv .venv
```

Sous Linux/macOS :

```bash
source .venv/bin/activate
```

Sous Windows :

```powershell
.venv\Scripts\Activate.ps1
```

Puis :

```bash
pip install -r requirements.txt
streamlit run app.py
```

L'application est ensuite accessible sur l'adresse affichée par Streamlit, généralement :

```text
http://localhost:8501
```

---

## Déploiement Streamlit Community Cloud

Le dépôt peut être déployé directement sur Streamlit Community Cloud.

Paramètres principaux :

```text
Repository : legab06/blockchain-lookup
Branch     : main
Main file  : app.py
```

Aucun secret n'est requis avec les endpoints publics configurés par défaut.

Le serveur limite par défaut les scans simultanés à trois. La variable
`BLOCKCHAIN_LOOKUP_MAX_CONCURRENT_SEARCHES` permet de fixer une autre limite
positive ; une valeur absente, invalide ou non positive revient à trois. Les
recherches supplémentaires attendent qu'une place se libère, au plus 30 secondes
par défaut (`BLOCKCHAIN_LOOKUP_SEARCH_QUEUE_TIMEOUT_SECONDS`). Cette limite
s'applique à chaque processus Streamlit. Une recherche s'interrompt aussi avant
de générer plus de 50 000 lignes détaillées par défaut
(`BLOCKCHAIN_LOOKUP_MAX_RESULT_ROWS`) ; aucun résultat n'est tronqué silencieusement.

Les services publics peuvent cependant appliquer des limites de débit, réduire leur historique ou modifier leurs conditions d'accès. Pour un usage intensif, il est préférable de prévoir ses propres endpoints RPC/API.

---

## Architecture du dépôt

```text
blockchain-lookup/
├── app.py
├── blockchain_lookup/
│   ├── version.py
│   ├── domain/       # contrats, montants, ordre et manifest
│   ├── engines/      # recherches Solana, Ethereum et Bitcoin
│   ├── runtime/      # limite de concurrence des recherches
│   └── ui/           # application Streamlit et présentation
├── pyproject.toml
├── requirements.txt
├── .streamlit/config.toml
└── tests/
```

### Fichiers principaux

| Fichier | Rôle |
| --- | --- |
| `app.py` | point d'entrée `streamlit run app.py` |
| `blockchain_lookup/engines/` | moteurs et protocoles réseau propres à chaque blockchain |
| `blockchain_lookup/domain/` | contrat `SearchResult`, matching, ordre et manifest |
| `blockchain_lookup/ui/` | vues, thèmes, tableaux, pagination, exports et messages |
| `blockchain_lookup/version.py` | version unique de l'application |
| `tests/` | tests unitaires hors ligne des moteurs, du domaine et de l'UI |

---

## Tests

Les tests peuvent être lancés avec :

```bash
python -m unittest discover -s tests -v
ruff check .
python -m compileall .
```

Ils couvrent notamment :

- le matching exact et approché ;
- la prise en compte de la précision saisie ;
- la limite des 8 décimales BTC ;
- le parsing de transactions Bitcoin legacy ;
- le parsing SegWit et le calcul du TXID sans witness ;
- le décodage des sorties Bitcoin standards testées ;
- les moteurs Solana et Ethereum, la complétude et l'ordre des résultats ;
- le manifest JSON et les messages de l'interface.

---

## Performances et garde-fous

Blockchain Lookup est pensé pour pouvoir fonctionner sur un hébergement Streamlit léger.

Quelques choix importants :

- une seule vue de données rendue à la fois ;
- pagination côté application ;
- rendu `st.dataframe` différé/lazy ;
- CSV complet généré uniquement sur demande ;
- recherche limitée à une fenêtre temporelle ;
- récupération par lots de certains objets Ethereum ;
- parsing local du bloc brut Bitcoin plutôt qu'un appel API par transaction ;
- plafonds sur les fenêtres trop importantes afin de protéger le service et les endpoints publics.

---

## Limites

Blockchain Lookup est un outil de **recherche et d'aide à l'analyse**, pas un indexeur blockchain complet.

Les résultats dépendent notamment :

- de la disponibilité et de la rétention historique du RPC/API utilisé ;
- des limites de débit du fournisseur public ;
- de la façon dont une opération est encodée par un smart contract ou un programme ;
- des heuristiques utilisées pour identifier certains swaps ;
- de la granularité temporelle propre à chaque blockchain.

Une absence de correspondance ne signifie donc pas nécessairement qu'une opération n'existe pas : elle peut nécessiter un endpoint archive, une API de traces, l'analyse d'un token supplémentaire ou une indexation spécialisée.

---

## Stack

- **Python**
- **Streamlit**
- **pandas**
- JSON-RPC Solana
- JSON-RPC Ethereum
- API mempool.space / parsing Bitcoin local
