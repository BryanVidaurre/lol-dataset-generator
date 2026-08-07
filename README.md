# 🎮 LoL Dataset Generator

Un collecteur de données automatisé pour League of Legends utilisant l'API officielle Riot Games.
Conçu pour générer un dataset ML propre à partir des matchs Ranked Solo/Duo des joueurs Challenger, Grandmaster et Master.

---

## 📁 Structure du projet

```
lol_dataset/
│
├── collector/
│   ├── 1_collect_players.py     # Phase 1 : Collecte des joueurs (PUUID)
│   ├── 2_collect_matches.py     # Phase 2 : Collecte des Match IDs
│   └── 3_collect_details.py     # Phase 3 : Extraction des détails (→ CSV)
│
├── config/
│   └── settings.py              # Régions, routing, chemins
│
├── database/
│   ├── db_manager.py            # Abstraction SQLite
│   └── schema.sql               # Schéma de la base
│
├── utils/
│   ├── riot_api.py              # Client API Riot
│   ├── rate_limiter.py          # Limiteur de débit (thread-safe)
│   ├── logger.py                # Logging
│   └── verify_dataset.py        # Vérification du dataset final
│
├── data/                        # Généré automatiquement (ignoré par git)
│   ├── riot_dataset.db          # Base SQLite
│   └── dataset.csv              # Dataset ML final
│
├── .env.example                 # Template de configuration
├── .gitignore
└── README.md
```

---

## ⚙️ Installation

```bash
# 1. Cloner le projet
git clone https://github.com/votre-username/lol-dataset-generator.git
cd lol-dataset-generator/lol_dataset

# 2. Créer l'environnement virtuel
python -m venv venv

# 3. Activer l'environnement
# Windows :
venv\Scripts\activate
# Linux/Mac :
source venv/bin/activate

# 4. Installer les dépendances
pip install requests pandas tqdm python-dotenv

# 5. Configurer la clé API
cp .env.example .env
# Editer .env et remplacer par votre clé Riot Developer
```

---

## 🔑 Obtenir une clé API Riot

1. Rendez-vous sur [developer.riotgames.com](https://developer.riotgames.com)
2. Connectez-vous avec votre compte Riot
3. Générez une clé de développement (valable 24h)
4. Copiez-la dans votre fichier `.env`

> ⚠️ **La clé développeur expire toutes les 24h.** Renouvelez-la chaque jour avant de lancer la collecte.

---

## 🚀 Utilisation

### Phase 1 — Collecter les joueurs
```bash
python collector\1_collect_players.py
```
Collecte ~80 000 joueurs (Challenger, Grandmaster, Master) dans toutes les régions.

### Phase 2 — Collecter les Match IDs
```bash
python collector\2_collect_matches.py
```
Récupère tous les match IDs Ranked Solo/Duo (saison 2026) pour chaque joueur.

### Phase 3 — Extraire les détails
```bash
python collector\3_collect_details.py
```
Télécharge les détails de chaque match et génère `data/dataset.csv`.

> ✅ Chaque phase est **reprénable** : vous pouvez l'arrêter et la relancer, elle reprend exactement où elle s'était arrêtée.

---

## 📊 Format du Dataset

Le fichier `dataset.csv` contient les colonnes suivantes :

| Colonne | Description |
|---|---|
| `match_id` | Identifiant unique du match |
| `patch` | Version du jeu |
| `game_duration` | Durée en secondes |
| `game_creation` | Timestamp de création |
| `t1_top` … `t1_support` | Champions de l'équipe 1 par rôle |
| `t1_avg_tier` | Tier moyen équipe 1 (1=Master, 2=GM, 3=Challenger) |
| `t2_top` … `t2_support` | Champions de l'équipe 2 par rôle |
| `t2_avg_tier` | Tier moyen équipe 2 |
| `win_team1` | True si l'équipe 1 a gagné |

---

## ⚖️ Conformité Riot Games

Ce projet respecte les [Conditions d'utilisation de l'API Riot](https://developer.riotgames.com/terms) :
- ✅ Utilisation uniquement des endpoints officiels documentés
- ✅ Clé API sécurisée (jamais committée, stockée dans `.env`)
- ✅ Projet à usage personnel / éducatif / recherche ML
- ✅ Aucune publication communautaire avec clé développeur
- ✅ Respect des rate limits

---

## 📄 Licence

MIT License — Libre d'utilisation dans le respect des CGU Riot Games.
