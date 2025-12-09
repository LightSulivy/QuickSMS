# 🚀 QuickSMS v2.1

Bot Discord automatisé pour l'achat et la réception de SMS de validation (OTP) via l'API SMS-Activate.
Système complet avec gestion de solde, dashboard interactif et panel administration.

## 🛠️ Commandes Serveur (Maintenance)

Commandes essentielles pour gérer le processus du bot sur le VPS/Serveur :

```bash
# Voir les logs (erreurs, activités) en temps réel
pm2 logs QuickSMS

# Redémarrer le bot (après une mise à jour ou un bug)
pm2 restart QuickSMS
```

## 🤖 Commandes Discord

Le bot fonctionne principalement via des **Slash Commands** (`/`) et un **Dashboard Interactif**.

### 👑 Commandes Administrateur
*Ces commandes sont réservées aux IDs définis dans la configuration.*

| Commande | Description | Exemple |
| :--- | :--- | :--- |
| `/deposit user amount` | Ajoute du crédit sur le solde d'un utilisateur. | `/deposit @Client 10` |
| `/setmargin margin` | Définit le coefficient de marge appliqué sur les prix. | `/setmargin 1.30` (30%) |
| `/stats` | Affiche un rapport des ventes, coûts et bénéfices du jour. | `/stats` |

### 👤 Commandes Utilisateur
*Accessibles à tous, mais le Dashboard est recommandé.*

- **/balance** : Affiche le solde actuel de l'utilisateur.
- **/services [pays]** : Liste les services et les prix pour un pays donné.

---

## 📱 Fonctionnement du Dashboard

Le bot crée automatiquement un salon `commander-num` avec un panneau de contrôle :

1. **🛒 Acheter un numéro** : 
   - L'utilisateur choisit le pays (ex: France).
   - Il sélectionne le service (ex: Whatsapp).
   - Une **confirmation** est envoyée en MP avec le prix final.
   - Après validation, le numéro est fourni et le bot attend le code SMS.
   
2. **📱 Services & Prix** : Permet de consulter les tarifs actuels (qui évoluent selon l'offre/demande de l'API).

3. **💰 Mon Solde** : Affiche les crédits disponibles.

4. **📦 Pack** : Bouton spécial pour des achats groupés (ex: Whatsapp FR + Telegram CA).

---

### ⚙️ Logique de Prix
Le prix de vente est calculé dynamiquement :
`Prix Vente = ((Prix API * 1.5) * Marge) * 0.9`
*La marge est modifiable via `/setmargin`.*
