# 🚀 QuickSMS v2.5

Bot Discord professionnel automatisé pour la vente et la réception de SMS de validation (OTP).
Système complet "Set & Forget" avec gestion de solde, dashboard interactif, paiements automatisés et panel d'administration avancé.

---

## 🛠️ Commandes Serveur (Maintenance)

Commandes essentielles pour gérer le processus du bot sur votre VPS/Serveur (si vous utilisez PM2) :

```bash
# Voir les logs (transactions, erreurs) en temps réel
pm2 logs QuickSMS

# Redémarrer le bot (après une mise à jour ou un bug)
pm2 restart QuickSMS

# Arrêter le bot
pm2 stop QuickSMS
```

---

## 🤖 Commandes Discord

Le bot fonctionne principalement via des **Slash Commands** (`/`) et un **Dashboard Interactif** persistant.

### 👑 Commandes Administrateur

_Ces commandes sont réservées aux administrateurs (définis dans la base de données)._

| Commande                       | Description                                                                                  | Exemple                    |
| :----------------------------- | :------------------------------------------------------------------------------------------- | :------------------------- |
| **`/deposit <user> <amount>`** | Ajoute manuellement du crédit à un utilisateur.                                              | `/deposit @Client 10`      |
| **`/setmargin <margin>`**      | Définit le multiplicateur de marge global.                                                   | `/setmargin 1.5` (50%)     |
| **`/stats`**                   | Affiche un rapport complet des ventes et bénéfices du jour.                                  | `/stats`                   |
| **`/history <user> [filter]`** | Voir l'historique détaillé d'un membre. Filtres dispos : **Tout**, **Validées**, **Dépôts**. | `/history @Client`         |
| **`/addadmin <user>`**         | Ajoute un nouvel administrateur au bot.                                                      | `/addadmin @Modo`          |
| **`/removeadmin <user>`**      | Retire les droits d'administrateur à un membre.                                              | `/removeadmin @AncienModo` |
| **`/listadmins`**              | Affiche la liste de tous les administrateurs actuels.                                        | `/listadmins`              |

### 👤 Commandes Utilisateur

_Accessibles à tous les membres. Le Dashboard est généralement suffisant._

| Commande                 | Description                                                                    |
| :----------------------- | :----------------------------------------------------------------------------- |
| **`/balance`**           | Affiche le solde actuel de votre compte.                                       |
| **`/recharge <amount>`** | Génère un lien de paiement (Carte/Crypto) via Hoodpay pour créditer le compte. |
| **`/services [pays]`**   | Affiche la liste des services disponibles et leurs prix en temps réel.         |

---

## 📱 Dashboard Client

Le bot déploie automatiquement un **Dashboard Interactif** dans les salons configurés :

1.  **🛒 Acheter un numéro** :

    - Sélection intuitive du Pays (ex: 🇫🇷 France, 🇨🇦 Canada).
    - Choix du Service (Whatsapp, Telegram, Uber, etc.).
    - Le bot envoie le numéro en **Message Privé (DM)**.
    - L'utilisateur attend le code directement dans ses DMs avec mise à jour en temps réel.

2.  **💰 Mon Solde** : Vérification immédiate des crédits.

3.  **💳 Recharger** : Raccourci vers la commande de paiement.

4.  **📦 Packs Spéciaux** : (Optionnel) Permet l'achat groupé de plusieurs services (ex: Pack "Double WA").

---

## ⚙️ Détails Techniques

- **Base de Données** : SQLite (Stockage local rapide et fiable).
  - `users` : Soldes clients.
  - `orders` : Historique des commandes.
  - `deposits` : Historique des rechargements (Admin + Hoodpay).
  - `admins` : Liste dynamique des admins.
  - `blocked_numbers` : Blacklist des numéros défectueux.
- **API** : Intégration SMS-Activate (Achat numéros) & Hoodpay (Paiements).
- **Prix** : Calcul dynamique basé sur le coût API + Marge.
  - Formule : `((Prix API * 1.3) * Marge) * 0.9` (Ajustable dans le code).

**Note:** Les administrateurs peuvent être gérés directement via Discord sans toucher au code ou à la base de données.
