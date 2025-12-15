# 🚀 QuickSMS v3.0

Bot Discord professionnel "All-in-One" pour la vente automatisée de **Numéros SMS (OTP)** et de **Comptes Telegram**.
Système complet "Set & Forget" avec gestion de solde, dashboard interactif, paiements Hoodpay/Cryptomus et panel d'administration avancé.

---

## 🔥 Nouveautés v3.0 (Telegram Accounts)

- **Vente de Comptes Telegram** : Le bot peut désormais vendre des sessions Telegram pré-enregistrées.
- **Connexion "Magique"** : Plus besoin de manipuler des fichiers `.session` pour le client. Le bot intercepte le code de connexion Telegram et l'envoie au client.
- **Import Automatique** : Script pour charger des centaines de comptes depuis un dossier `sessions/` (Format TData/Json).

---

## 🛠️ Maintenance Serveur

Commandes pour gérer le bot sur votre VPS (PM2) :

```bash
# Voir les logs
pm2 logs QuickSMS

# Redémarrer (Mise à jour)
pm2 restart QuickSMS
```

---

## 🤖 Commandes Administrateur

_Réservé aux admins définis._

### 📦 Gestion des Comptes Telegram (Stock)

| Commande          | Description                                                            |
| :---------------- | :--------------------------------------------------------------------- |
| **`/addstock`**   | Ajouter un compte manuellement (Phone + Session String).               |
| **`/stock`**      | Voir l'état de l'inventaire (Total, Disponibles, Vendus).              |
| **`/clearstock`** | Supprimer des comptes de la base de données (Invendus ou Reset total). |

### 💰 Gestion Finance & Users

| Commande                       | Description                                              |
| :----------------------------- | :------------------------------------------------------- |
| **`/stats`**                   | Rapport des ventes et bénéfices du jour (SMS + Comptes). |
| **`/deposit <user> <amount>`** | Ajouter du crédit manuellement.                          |
| **`/setmargin <margin>`**      | Changer la marge globale (SMS uniquement).               |
| **`/history <user>`**          | Voir l'historique des achats et dépôts.                  |
| **`/listadmins`**              | Gérer les admins.                                        |

### 🔧 Outils

- **Script d'import de masse** :
  1.  Placez vos fichiers (`.session` + `.json`) dans le dossier `sessions/`.
  2.  Lancez : `./venv/bin/python import_sessions.py`
  3.  Vos comptes sont prêts à être vendus !

---

## 👤 Commandes & Features Utilisateur

### Dashboard Interactif

Le bot déploie un panel complet :

1.  **🛒 Acheter SMS** : Whatsapp, Uber, Telegram, etc. (Automatique via SMS-Activate).
2.  **🔥 Compte Telegram** : Achat immédiat d'un compte (vieux/vérifié) depuis votre stock.
    - _Fonction "Recevoir le Code"_ : Le bot donne le code de connexion en temps réel.
3.  **💳 Recharger** : Paiement Auto (Carte/Crypto) via Hoodpay.
4.  **💰 Mon Solde** : Solde en temps réel.

### Commandes Utiles

| Commande          | Description                                                                                   |
| :---------------- | :-------------------------------------------------------------------------------------------- |
| **`/myaccounts`** | Si le bot redémarre, permet de retrouver ses comptes achetés et le bouton "Recevoir le code". |
| **`/balance`**    | Voir son solde.                                                                               |

---

## ⚙️ Détails Techniques v3.0

- **Base de Données** : SQLite (Locale).
  - `telegram_accounts` : Stockage des sessions (encryptées format StringSession).
  - `orders` : Historique unifié (SMS et Comptes).
- **APIs** :
  - **SMS-Activate** : Pour les numéros temporaires à la demande.
  - **Telethon** : Pour la connexion "Client" invisible aux comptes Telegram vendus.
  - **Hoodpay** : Gateway de paiement.
- **Prix** :
  - SMS : `Prix API * Marge`.
  - Comptes Telegram : Fixé à `2.00€` minimum (ou `Coût * 2` si supérieur).
