import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import json
import aiohttp
import asyncio
from datetime import datetime, timedelta, time
from discord.ext import tasks
import os
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
API_KEY = os.getenv("SMS_ACTIVATE_API_KEY")
BASE_URL = "https://api.sms-activate.org/stubs/handler_api.php"

# --- ADMINS ---
ADMIN_IDS = [
    227390137892339722,
    1300246463951011981,
    1279003722172862465,
]

# --- MAPPING (Pour simplifier la vie de tes clients) ---
# SMS-Activate utilise des IDs pour les pays et des codes pour les services.
# Tu devras compléter cette liste selon ce que tu veux vendre.
SERVICES = {
    "whatsapp": "wa",
    "telegram": "tg",
    "google": "go",
    "amazon": "am",
    "tinder": "oi",
    "microsoft": "mm",
    "facebook": "fb",
    "instagram": "ig",
    "tiktok": "lf",
    "uber": "ub",
}

COUNTRIES = {"france": "78", "canada": "36", "united_kingdom": "16"}

# --- HOODPAY CONFIG ---
HOODPAY_BS_ID = os.getenv("HOODPAY_BUSINESS_ID")
HOODPAY_API_KEY = os.getenv("HOODPAY_API_KEY")
HOODPAY_WEBHOOK_SECRET = os.getenv("HOODPAY_WEBHOOK_SECRET")
WEBHOOK_PORT = 5000  # Port pour écouter les paiements


# --- GESTION BASE DE DONNÉES (SQLite) ---
def init_db():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    # Table Utilisateurs
    c.execute(
        """CREATE TABLE IF NOT EXISTS users
                 (discord_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0)"""
    )
    # Table Commandes
    # Table Commandes
    c.execute(
        """CREATE TABLE IF NOT EXISTS orders
                 (order_id TEXT PRIMARY KEY, discord_id INTEGER, 
                  phone TEXT, price REAL, status TEXT, created_at TEXT, service TEXT)"""
    )
    # Table Numéros Bloqués
    c.execute(
        """CREATE TABLE IF NOT EXISTS blocked_numbers
                 (phone TEXT PRIMARY KEY, service TEXT, reported_at TEXT)"""
    )

    # Migration : Ajout colonne service si elle existe pas (pour les anciennes DB)
    try:
        c.execute("ALTER TABLE orders ADD COLUMN service TEXT")
    except sqlite3.OperationalError:
        pass  # La colonne existe déjà

    # Migration : Ajout colonne cost pour le calcul de bénéfice
    try:
        c.execute("ALTER TABLE orders ADD COLUMN cost REAL")
    except sqlite3.OperationalError:
        pass  # La colonne existe déjà

    # Table Paramètres (Settings)
    c.execute(
        """CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)"""
    )
    # Initialisation de la marge par défaut si inexistante
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('margin', '1.20')")

    # Table Admins
    c.execute(
        """CREATE TABLE IF NOT EXISTS admins
                 (discord_id INTEGER PRIMARY KEY, added_at TEXT)"""
    )

    # Table Dépôts (Historique rechargements)
    c.execute(
        """CREATE TABLE IF NOT EXISTS deposits
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, discord_id INTEGER, 
                  amount REAL, source TEXT, created_at TEXT)"""
    )

    # Migration : On ajoute les admins hardcodés dans la DB pour qu'ils y soient par défaut
    for admin_id in ADMIN_IDS:
        c.execute(
            "INSERT OR IGNORE INTO admins (discord_id, added_at) VALUES (?, ?)",
            (admin_id, str(datetime.now())),
        )

    # Table Comptes Telegram (Stock)
    c.execute(
        """CREATE TABLE IF NOT EXISTS telegram_accounts
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  phone TEXT, 
                  session_string TEXT, 
                  password_2fa TEXT, 
                  origin TEXT DEFAULT 'MANUAL', 
                  price_cost REAL DEFAULT 0.0,
                  added_at TEXT,
                  status TEXT DEFAULT 'AVAILABLE', 
                  sold_to INTEGER DEFAULT NULL,
                  sold_at TEXT DEFAULT NULL)"""
    )

    conn.commit()
    conn.close()


# --- GESTION STOCK TELEGRAM ---
def add_telegram_account_db(
    phone, session_string, password_2fa, price_cost, origin="MANUAL"
):
    conn = sqlite3.connect("database.db")
    conn.execute(
        "INSERT INTO telegram_accounts (phone, session_string, password_2fa, price_cost, origin, added_at, status) VALUES (?, ?, ?, ?, ?, ?, 'AVAILABLE')",
        (phone, session_string, password_2fa, price_cost, origin, str(datetime.now())),
    )
    conn.commit()
    conn.close()
    print(f"✅ Compte Telegram ajouté au stock : {phone}")


def get_available_telegram_account():
    conn = sqlite3.connect("database.db")
    # On prend le premier disponible (FIFO)
    row = conn.execute(
        "SELECT id, phone, session_string, password_2fa, price_cost FROM telegram_accounts WHERE status='AVAILABLE' LIMIT 1"
    ).fetchone()
    conn.close()

    if row:
        return {
            "id": row[0],
            "phone": row[1],
            "session_string": row[2],
            "password_2fa": row[3],
            "cost": row[4],
        }
    return None


def mark_telegram_account_sold(account_id, user_id):
    conn = sqlite3.connect("database.db")
    conn.execute(
        "UPDATE telegram_accounts SET status='SOLD', sold_to=?, sold_at=? WHERE id=?",
        (user_id, str(datetime.now()), account_id),
    )
    conn.commit()
    conn.close()


def count_telegram_stock():
    conn = sqlite3.connect("database.db")
    count = conn.execute(
        "SELECT COUNT(*) FROM telegram_accounts WHERE status='AVAILABLE'"
    ).fetchone()[0]
    conn.close()
    return count


def get_margin():
    conn = sqlite3.connect("database.db")
    res = conn.execute("SELECT value FROM settings WHERE key='margin'").fetchone()
    conn.close()
    return float(res[0]) if res else 1.20


def update_margin_db(new_margin):
    conn = sqlite3.connect("database.db")
    conn.execute("UPDATE settings SET value=? WHERE key='margin'", (str(new_margin),))
    conn.commit()
    conn.close()


def calculate_selling_price(api_price):
    if api_price is None:
        return None

    margin = get_margin()
    # Formule : ((API * 1.3) * margin) * 0.9
    return round(((api_price * 1.3) * margin) * 0.9, 2)


def get_balance(user_id):
    conn = sqlite3.connect("database.db")
    res = conn.execute(
        "SELECT balance FROM users WHERE discord_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return res[0] if res else 0.0


def update_balance(user_id, amount):
    conn = sqlite3.connect("database.db")
    conn.execute(
        "INSERT OR IGNORE INTO users (discord_id, balance) VALUES (?, 0)", (user_id,)
    )
    conn.execute(
        "UPDATE users SET balance = balance + ? WHERE discord_id=?", (amount, user_id)
    )
    conn.commit()
    conn.close()


def add_deposit_log(user_id, amount, source):
    conn = sqlite3.connect("database.db")
    conn.execute(
        "INSERT INTO deposits (discord_id, amount, source, created_at) VALUES (?, ?, ?, ?)",
        (user_id, amount, source, str(datetime.now())),
    )
    conn.commit()
    conn.close()


def is_number_used(phone, service):
    conn = sqlite3.connect("database.db")
    # On regarde si ce numéro a déjà une commande complétée ou en attente pour ce service
    res = conn.execute(
        "SELECT 1 FROM orders WHERE phone=? AND service=?", (phone, service)
    ).fetchone()

    if res:
        conn.close()
        return True

    # Vérification dans la table des numéros bloqués
    res_blocked = conn.execute(
        "SELECT 1 FROM blocked_numbers WHERE phone=?", (phone,)
    ).fetchone()
    conn.close()
    return res_blocked is not None


def block_number_db(phone, service):
    conn = sqlite3.connect("database.db")
    conn.execute(
        "INSERT OR IGNORE INTO blocked_numbers (phone, service, reported_at) VALUES (?, ?, ?)",
        (phone, service, str(datetime.now())),
    )
    conn.commit()
    conn.close()


def is_user_admin(user_id):
    # On vérifie dans la liste hardcodée (backup) OU dans la DB
    if user_id in ADMIN_IDS:
        return True

    conn = sqlite3.connect("database.db")
    res = conn.execute("SELECT 1 FROM admins WHERE discord_id=?", (user_id,)).fetchone()
    conn.close()
    return res is not None


def add_new_admin_db(user_id):
    conn = sqlite3.connect("database.db")
    conn.execute(
        "INSERT OR IGNORE INTO admins (discord_id, added_at) VALUES (?, ?)",
        (user_id, str(datetime.now())),
    )
    conn.commit()
    conn.close()


def remove_admin_db(user_id):
    conn = sqlite3.connect("database.db")
    conn.execute("DELETE FROM admins WHERE discord_id=?", (user_id,))
    conn.commit()
    conn.close()


# --- CLIENT API SMS-ACTIVATE ---
class SMSClient:
    async def request(self, action, params={}):
        params["api_key"] = API_KEY
        params["action"] = action
        async with aiohttp.ClientSession() as session:
            async with session.get(BASE_URL, params=params) as resp:
                return await resp.text()

    async def buy_number(self, service, country, user_info=None):
        # Réponse attendue : ACCESS_NUMBER:$ID:$NUMBER
        text = await self.request(
            "getNumber", {"service": service, "country": country, "freePrice": 1}
        )
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        initiator = user_info if user_info else "Unknown"
        print(
            f"[{timestamp}] [Initiator: {initiator}] DEBUG buy_number response: {text}"
        )
        if "ACCESS_NUMBER" in text:
            parts = text.split(":")
            return {"success": True, "id": parts[1], "phone": parts[2]}
        elif "NO_NUMBERS" in text:
            return {
                "success": False,
                "error": "Plus de stock pour ce pays.",
                "code": "NO_NUMBERS",
            }
        elif "NO_BALANCE" in text:
            return {
                "success": False,
                "error": "Erreur interne (Fonds insuffisants chez le bot).",
            }
        else:
            return {"success": False, "error": text}

    async def get_price(self, service, country):
        try:
            # On retourne sur getPrices pour avoir le prix Réel (et pas moyen/stats)
            response = await self.request(
                "getPrices", {"service": service, "country": country, "freePrice": 1}
            )

            # DEBUG TEMPORAIRE POUR VOIR L'ERREUR
            print(f"DEBUG RAW RESPONSE ({service}/{country}): {response}")

            # Pas de print global pour éviter le spam, on affiche juste le résultat trouvé
            data = json.loads(response)

            country_str = str(country)

            if country_str in data and service in data[country_str]:
                cost = float(data[country_str][service]["cost"])
                count = int(data[country_str][service]["count"])
                if count > 0:
                    return cost
                else:
                    return None

            print(f"DEBUG: Pas de prix trouvé pour {service} en pays {country}")
            return None
        except Exception as e:
            print(f"Erreur get_price: {e}")
            return None

    async def get_status(self, activation_id):
        # Réponse attendue : STATUS_OK:CODE ou STATUS_WAIT_CODE
        text = await self.request("getStatus", {"id": activation_id})
        return text

    async def cancel_order(self, activation_id, user_info=None):
        # On envoie le statut 8 (Annulation)
        # On attend la réponse pour savoir si ça a marché
        response = await self.request("setStatus", {"id": activation_id, "status": "8"})
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        initiator = user_info if user_info else "Unknown"
        print(
            f"[{timestamp}] [Initiator: {initiator}] DEBUG ANNULATION - ID {activation_id} : {response}"
        )  # Pour voir ce qui se passe dans ta console
        return response


# --- CLIENT HOODPAY ---
class HoodpayClient:
    def __init__(self):
        self.base_url = "https://api.hoodpay.io/v1"
        self.headers = {
            "Authorization": f"Bearer {HOODPAY_API_KEY}",
            "Content-Type": "application/json",
        }

    async def create_payment(self, amount, user_id, guild_id):
        url = f"{self.base_url}/payments"
        payload = {
            "amount": float(amount),
            "currency": "EUR",
            "description": f"Recharge QuickSMS - {amount}€",
            "businessId": HOODPAY_BS_ID,
            "metadata": {"user_id": str(user_id), "guild_id": str(guild_id)},
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self.headers) as resp:
                if resp.status == 200 or resp.status == 201:
                    data = await resp.json()
                    return data.get("data", {}).get("checkoutUrl")
                else:
                    text = await resp.text()
                    print(f"Erreur Hoodpay ({resp.status}): {text}")
                    return None


# --- HANDLER TELETHON ---
from telethon import TelegramClient
from telethon.sessions import StringSession
import re

# --- CONFIG TELETHON ---
TG_API_ID = os.getenv("TELEGRAM_API_ID")
TG_API_HASH = os.getenv("TELEGRAM_API_HASH")


# --- HANDLER TELETHON (Gestionnaire de Sessions) ---
class TelethonHandler:
    @staticmethod
    async def get_login_code(session_string):
        """
        Se connecte au compte via session_string, récupère le dernier code Telegram,
        et se déconnecte.
        """
        if not TG_API_ID or not TG_API_HASH:
            return {
                "success": False,
                "error": "Config Bot Telegram manquante (API_ID/HASH).",
            }

        client = TelegramClient(
            StringSession(session_string), int(TG_API_ID), TG_API_HASH
        )

        try:
            print(f"📡 [Telethon] Tentative de connexion pour la session du compte...")
            await client.connect()

            if await client.is_user_authorized():
                print(f"✅ [Telethon] Connexion CLIENT réussie. Session active.")

                # On cherche le message de Telegram (777000)
                # On prend l'historique récent
                print(f"🔍 [Telethon] Recherche du code dans les messages de 777000...")

                try:
                    system_user = await client.get_entity(777000)
                except Exception as e:
                    print(
                        f"⚠️ [Telethon] Impossible de résoudre l'entité 777000 directement : {e}"
                    )
                    # Fallback éventuel ou abandon

                # On lit les 5 derniers messages
                messages = await client.get_messages(777000, limit=5)

                code = None
                print(f"📨 [Telethon] {len(messages)} messages trouvés de Telegram.")

                for msg in messages:
                    if msg.message:
                        print(
                            f"   -> Message reçu ({msg.date}): {msg.message[:50]}..."
                        )  # Log tronqué pour debug

                        # Regex pour trouver un code à 5 chiffres
                        # Ex: "Login code: 12345" ou "code de connexion: 12345"
                        # On cherche 5 chiffres isolés ou précédés de "code"
                        match = re.search(r":\s*(\d{5})", msg.message)
                        if not match:
                            match = re.search(r"\b(\d{5})\b", msg.message)

                        if match:
                            # On vérifie si le message est récent (moins de 2 min)
                            # msg.date est en UTC timezoné
                            now = datetime.now(msg.date.tzinfo)
                            diff = now - msg.date
                            print(
                                f"      Code potentiel trouvé: {match.group(1)} (il y a {int(diff.total_seconds())}s)"
                            )

                            if diff.total_seconds() < 300:  # 5 minutes max
                                code = match.group(1)
                                print(f"✅ [Telethon] CODE VALIDE EXTRAT : {code}")
                                break
                            else:
                                print(f"      ❌ Code expiré (>300s).")

                await client.disconnect()

                if code:
                    return {"success": True, "code": code}
                else:
                    return {
                        "success": False,
                        "error": "Aucun code récent trouvé. Connectez-vous sur l'app Telegram pour recevoir le code, puis réessayez ici.",
                    }
            else:
                print(
                    f"❌ [Telethon] Connexion ECHOUÉE : Session non autorisée (invalide ou déconnectée)."
                )
                await client.disconnect()
                return {
                    "success": False,
                    "error": "Session du bot invalide. Contactez le support.",
                }

        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            print(f"❌Erreur Telethon CRITIQUE: {e}")
            return {"success": False, "error": f"Erreur connexion : {str(e)}"}


# --- WEBHOOK SERVER ---
from aiohttp import web


async def handle_webhook(request):
    # Sécurité : Vérifier le secret si possible (Hoodpay envoie souvent une signature)
    # Pour l'instant on fait simple
    try:
        data = await request.json()
        print(f"WEBHOOK REÇU : {data}")

        event_type = data.get("type")
        if event_type == "payment.succeeded":
            payload = data.get("data", {})
            metadata = payload.get("metadata", {})
            user_id = int(metadata.get("user_id", 0))
            amount = float(payload.get("amount", 0))

            if user_id > 0:
                print(f"✅ Paiement validé pour {user_id} : +{amount}€")
                update_balance(user_id, amount)
                add_deposit_log(user_id, amount, "Hoodpay")

                # Notification utilisateur (Optionnel, requiert d'avoir le bot accessible)
                # On ne peut pas facilement await bot.fetch_user ici sans contexte,
                # mais le solde est mis à jour.

        return web.Response(text="OK")
    except Exception as e:
        print(f"Erreur Webhook: {e}")
        return web.Response(status=500, text="Error")


async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"🌍 Serveur Webhook démarré sur le port {WEBHOOK_PORT}")


# --- LOGIQUE DU BOT ---
init_db()
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
sms_api = SMSClient()
hoodpay_api = HoodpayClient()


@bot.event
async def on_ready():
    await bot.tree.sync()
    bot.add_view(DashboardView())
    if not daily_stats_task.is_running():
        daily_stats_task.start()

    # Démarrage du serveur Webhook
    if HOODPAY_API_KEY:
        await start_webhook_server()

    # Setup Dashboard sur tous les serveurs
    for guild in bot.guilds:
        await setup_dashboard(guild)

    # REPRISE DES COMMANDES EN COURS (Après redémarrage)
    print("🔄 Recherche des commandes en attente...")
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT order_id, discord_id, price, service, status FROM orders WHERE status='PENDING'"
    )
    pending_orders = cursor.fetchall()
    conn.close()

    count = 0
    for order_id, user_id, price, service_name, status in pending_orders:
        try:
            # DEBUG: Identifier la ligne qui plante
            # print(f"DEBUG REPRISE {order_id} - Step 1: Create View")
            view = OrderView(order_id, price, user_id, original_interaction=None)

            # print(f"DEBUG REPRISE {order_id} - Step 2: Add View")
            bot.add_view(view)

            # print(f"DEBUG REPRISE {order_id} - Step 3: Fetch User")
            user = await bot.fetch_user(user_id)

            if user:
                # print(f"DEBUG REPRISE {order_id} - Step 4: Create DM")
                dm_channel = await user.create_dm()

                # print(f"DEBUG REPRISE {order_id} - Step 5: Start Task")
                asyncio.create_task(
                    check_sms_loop(order_id, dm_channel, view, dm_message=None)
                )
                count += 1
        except Exception as e:
            # J'affiche la traceback complète pour comprendre
            import traceback

            traceback.print_exc()
            print(f"Erreur reprise commande {order_id}: {e}")

    print(
        f"✅ {count} commandes en attente reprises. Bot connecté en tant que {bot.user}"
    )


@bot.event
async def on_guild_join(guild):
    await setup_dashboard(guild)


@tasks.loop(time=time(hour=10, minute=0))
async def daily_stats_task():
    limit_date = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT order_id, price, cost FROM orders WHERE status='COMPLETED' AND created_at >= ?",
        (limit_date,),
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return  # Pas de commande, pas de stats

    total_sales = 0.0
    total_cost = 0.0

    for _, price, cost in rows:
        total_sales += price
        if cost:
            total_cost += cost * 1.3 * 0.9

    profit = total_sales - total_cost

    embed = discord.Embed(title="📅 Rapport Quotidien (24h)", color=0x3498DB)
    embed.add_field(name="Ventes", value=f"{total_sales:.2f}€", inline=True)
    embed.add_field(name="Bénéfice Net", value=f"{profit:.2f}€", inline=True)
    embed.set_footer(text=f"{len(rows)} commandes traitées.")

    for admin_id in ADMIN_IDS:
        try:
            user = await bot.fetch_user(admin_id)
            if user:
                await user.send(embed=embed)
        except Exception as e:
            print(f"Erreur envoi stats à {admin_id}: {e}")


@bot.tree.command(name="deposit", description="Ajouter des crédits (Admin uniquement)")
async def deposit(
    interaction: discord.Interaction, amount: float, user: discord.Member
):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ Vous n'avez pas la permission d'utiliser cette commande.",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)

    update_balance(user.id, amount)
    add_deposit_log(user.id, amount, f"Admin Deposit by {interaction.user.id}")
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ADMIN LOG: {interaction.user} (ID: {interaction.user.id}) credited {amount}€ to {user} (ID: {user.id})"
    )
    await interaction.followup.send(
        f"✅ Compte de {user.mention} crédité de {amount}€. Nouveau solde : {get_balance(user.id):.2f}€",
        ephemeral=True,
    )


@bot.tree.command(
    name="recharge", description="Recharger votre solde (Carte/Crypto via Hoodpay)"
)
async def recharge(interaction: discord.Interaction, amount: float):
    # --- DESACTIVATION TEMPORAIRE ---
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message(
            "⚠️ Cette commande est désactivée pour le moment. Veuillez contacter un administrateur pour recharger.",
            ephemeral=True,
        )

    if amount < 1:
        return await interaction.response.send_message("❌ Minimum 1€.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    if not HOODPAY_API_KEY:
        return await interaction.followup.send(
            "❌ Les paiements sont désactivés pour le moment (Config manquante).",
            ephemeral=True,
        )

    url = await hoodpay_api.create_payment(
        amount, interaction.user.id, interaction.guild_id
    )

    if url:
        embed = discord.Embed(
            title="💳 Recharger mon compte",
            description=f"Cliquez sur le lien ci-dessous pour payer **{amount}€** via Hoodpay (CB / Crypto).",
            color=0x5865F2,
        )
        embed.add_field(
            name="Lien de paiement", value=f"[👉 Payer maintenant]({url})", inline=False
        )
        embed.set_footer(
            text="Votre solde sera crédité automatiquement après validation."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send(
            "❌ Erreur lors de la création du paiement. Réessayez plus tard.",
            ephemeral=True,
        )


@bot.tree.command(name="setmargin", description="Changer la marge (Admin uniquement)")
async def setmargin(interaction: discord.Interaction, margin: float):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ Accès refusé.", ephemeral=True
        )

    if margin < 1.0:
        return await interaction.response.send_message(
            "⚠️ La marge doit être au moins de 1.0 (100%).", ephemeral=True
        )

    update_margin_db(margin)
    await interaction.response.send_message(
        f"✅ Marge mise à jour : **x{margin}** ({int((margin-1)*100)}% de bénéfice)",
        ephemeral=True,
    )


@bot.tree.command(
    name="stats", description="Voir les bénéfices du jour (Admin uniquement)"
)
async def stats(interaction: discord.Interaction):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ Accès refusé.", ephemeral=True
        )

    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    # On récupère les commandes COMPLETED du jour pour le calcul précis
    # On filtre sur 'created_at' qui contient la date. LIKE '2023-12-08%'
    cursor.execute(
        "SELECT price, cost, service FROM orders WHERE status='COMPLETED' AND created_at LIKE ?",
        (f"{today}%",),
    )
    rows = cursor.fetchall()
    conn.close()

    total_sales = 0.0
    total_cost = 0.0

    for price, cost, service in rows:
        total_sales += price

        if cost:
            # Si c'est un compte Telegram stocké, le coût est déjà net en Euro
            if service == "Telegram Account":
                total_cost += cost
            else:
                # Sinon c'est un SMS (Prix brut API), on applique la formule de conversion
                total_cost += cost * 1.3 * 0.9

    profit = total_sales - total_cost

    profit = total_sales - total_cost

    embed = discord.Embed(title=f"📊 Statistiques du {today}", color=0xFFD700)
    embed.add_field(name="Ventes Totales", value=f"{total_sales:.2f}€", inline=True)
    embed.add_field(name="Coût Estimé (API)", value=f"{total_cost:.2f}€", inline=True)
    embed.add_field(name="Bénéfice Net", value=f"{profit:.2f}€", inline=False)
    embed.set_footer(text=f"{len(rows)} commandes terminées aujourd'hui.")

    view = StatsView(today)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class StatsView(discord.ui.View):
    def __init__(self, date_str):
        super().__init__(timeout=60)
        self.date_str = date_str

    @discord.ui.button(
        label="📜 Voir détails", style=discord.ButtonStyle.secondary, emoji="🔎"
    )
    async def details_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)

        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT order_id, discord_id, service, price, status, created_at FROM orders WHERE created_at LIKE ? ORDER BY created_at DESC",
            (f"{self.date_str}%",),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return await interaction.followup.send(
                "❌ Aucune commande trouvée pour cette date.", ephemeral=True
            )

        # Construction du rapport
        lines = [f"**Commandes du {self.date_str}**"]
        for row in rows:
            oid, uid, svc, price, status, created = row
            # Format heure
            try:
                dt = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
                time_str = dt.strftime("%H:%M")
            except:
                time_str = "??"

            icon = "✅" if status == "COMPLETED" else "⏳" if status == "PENDING" else "❌"
            username = f"<@{uid}>"
            
            lines.append(
                f"`{time_str}` {icon} **{svc}** | `{oid}` | {price}€ | {username}"
            )

        # Gestion de la longueur du message
        full_text = "\n".join(lines)
        if len(full_text) > 4000:
            import io
            file_data = io.BytesIO(full_text.encode("utf-8"))
            await interaction.followup.send(
                content="📄 Rapport trop long, voir fichier :",
                file=discord.File(file_data, filename=f"stats_{self.date_str}.txt"),
                ephemeral=True,
            )
        else:
            embed = discord.Embed(description=full_text, color=0x3498DB)
            await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="history", description="Voir l'historique d'un membre (Admin uniquement)"
)
@app_commands.choices(
    filter=[
        app_commands.Choice(name="Commandes (Tout)", value="all"),
        app_commands.Choice(name="Commandes (Validées)", value="completed"),
        app_commands.Choice(name="Dépôts", value="deposits"),
    ]
)
async def history(
    interaction: discord.Interaction,
    user: discord.User,
    filter: app_commands.Choice[str] = None,
):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ Accès refusé.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    filter_value = filter.value if filter else "all"

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    rows = []
    is_deposit = False

    if filter_value == "deposits":
        is_deposit = True
        cursor.execute(
            "SELECT amount, source, created_at FROM deposits WHERE discord_id=? ORDER BY created_at DESC LIMIT 20",
            (user.id,),
        )
        rows = cursor.fetchall()
    elif filter_value == "completed":
        cursor.execute(
            "SELECT order_id, phone, price, status, service, created_at FROM orders WHERE discord_id=? AND status='COMPLETED' ORDER BY created_at DESC LIMIT 20",
            (user.id,),
        )
        rows = cursor.fetchall()
    else:  # all
        cursor.execute(
            "SELECT order_id, phone, price, status, service, created_at FROM orders WHERE discord_id=? ORDER BY created_at DESC LIMIT 20",
            (user.id,),
        )
        rows = cursor.fetchall()

    conn.close()

    user_balance = get_balance(user.id)

    if not rows:
        return await interaction.followup.send(
            f"ℹ️ Aucun historique trouvé ({filter_value}) pour {user.mention}. (Solde: {user_balance:.2f}€)",
            ephemeral=True,
        )

    embed = discord.Embed(
        title=f"📜 Historique de {user.name} ({filter_value.capitalize()})",
        description=f"**ID:** {user.id}\n**Solde:** {user_balance:.2f}€\n**Derniers éléments (max 20):**",
        color=0x9B59B6,
    )

    if is_deposit:
        for amount, source, created_at in rows:
            embed.add_field(
                name=f"💰 +{amount}€",
                value=f"**Source:** {source}\n**Date:** {created_at}",
                inline=False,
            )
    else:
        for order_id, phone, price, status, service, created_at in rows:
            # On essaie de parser la date pour faire joli
            date_str = created_at
            try:
                dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S.%f")
                date_str = dt.strftime("%d/%m %H:%M")
            except:
                try:
                    dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                    date_str = dt.strftime("%d/%m %H:%M")
                except:
                    pass  # On garde le format brut si échec

            status_emoji = (
                "✅"
                if status == "COMPLETED"
                else "❌" if status in ["REFUNDED", "CANCELLED"] else "⏳"
            )

            embed.add_field(
                name=f"{status_emoji} {date_str} - {service}",
                value=f"**Prix:** {price}€ | **Tel:** `{phone}`\n**Status:** {status}",
                inline=False,
            )

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="addadmin", description="Ajouter un administrateur (Admin uniquement)"
)
async def addadmin(interaction: discord.Interaction, user: discord.User):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ Accès refusé.", ephemeral=True
        )

    add_new_admin_db(user.id)
    # On met à jour la liste en mémoire pour éviter d'attendre le redémarrage
    if user.id not in ADMIN_IDS:
        ADMIN_IDS.append(user.id)

    await interaction.response.send_message(
        f"✅ **{user.name}** a été ajouté en tant qu'administrateur.", ephemeral=True
    )


@bot.tree.command(
    name="listadmins", description="Liste tous les administrateurs (Admin uniquement)"
)
async def listadmins(interaction: discord.Interaction):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ Accès refusé.", ephemeral=True
        )

    conn = sqlite3.connect("database.db")
    rows = conn.execute("SELECT discord_id, added_at FROM admins").fetchall()
    conn.close()

    description = ""
    for discord_id, added_at in rows:
        description += f"• <@{discord_id}> (ID: {discord_id}) - Ajouté le: {added_at}\n"

    # Ajout des admins hardcodés s'ils ne sont pas dans la DB (cas rare avec la migration, mais on sait jamais)
    for admin_id in ADMIN_IDS:
        found = False
        for row in rows:
            if row[0] == admin_id:
                found = True
                break
        if not found:
            description += (
                f"• <@{admin_id}> (ID: {admin_id}) - *Admin Config (Hardcodé)*\n"
            )

    embed = discord.Embed(
        title="🛡️ Liste des Administrateurs",
        description=description if description else "Aucun admin en DB.",
        color=0xE67E22,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="removeadmin", description="Supprimer un administrateur (Admin uniquement)"
)
async def removeadmin(interaction: discord.Interaction, user: discord.User):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ Accès refusé.", ephemeral=True
        )

    if user.id == interaction.user.id:
        return await interaction.response.send_message(
            "❌ Vous ne pouvez pas vous supprimer vous-même.", ephemeral=True
        )

    remove_admin_db(user.id)
    if user.id in ADMIN_IDS:
        ADMIN_IDS.remove(user.id)

    await interaction.response.send_message(
        f"✅ **{user.name}** a été retiré des administrateurs.", ephemeral=True
    )


# --- SYSTEME DE REPARATION DE SESSION (MIGRATION IP) ---


class FixSessionView(discord.ui.View):
    def __init__(self, client, phone, account_id):
        super().__init__(timeout=300)
        self.client = client
        self.phone = phone
        self.account_id = account_id
        self.code = None
        self.password = None

    @discord.ui.button(label="Entrer Code", style=discord.ButtonStyle.green)
    async def enter_code(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(CodeModal(self))

    @discord.ui.button(label="Entrer 2FA (Mdp)", style=discord.ButtonStyle.blurple)
    async def enter_password(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_modal(PasswordModal(self))

    @discord.ui.button(label="Supprimer du Stock", style=discord.ButtonStyle.red)
    async def delete_account(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        conn = sqlite3.connect("database.db")
        conn.execute("DELETE FROM telegram_accounts WHERE id=?", (self.account_id,))
        conn.commit()
        conn.close()
        await interaction.response.send_message(
            f"🗑️ Compte {self.phone} supprimé de la base.", ephemeral=True
        )
        self.stop()


class CodeModal(discord.ui.Modal, title="Code de Connexion"):
    code_input = discord.ui.TextInput(
        label="Code reçu (Mail/SMS/App)", placeholder="12345"
    )

    def __init__(self, view):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        code = self.code_input.value
        await interaction.response.defer(ephemeral=True)

        try:
            # On tente le sign-in avec le code
            await self.view_ref.client.sign_in(self.view_ref.phone, code)

            # Si ça passe, on sauvegarde
            new_session = StringSession.save(self.view_ref.client.session)

            conn = sqlite3.connect("database.db")
            conn.execute(
                "UPDATE telegram_accounts SET session_string=? WHERE id=?",
                (new_session, self.view_ref.account_id),
            )
            conn.commit()
            conn.close()

            await interaction.followup.send(
                f"✅ Session réparée et sauvegardée pour {self.view_ref.phone} !"
            )
            self.view_ref.stop()

        except Exception as e:
            if "password" in str(e).lower():
                await interaction.followup.send(
                    f"⚠️ Besoin du mot de passe 2FA (Cliquez sur le bouton bleu). Erreur: {e}"
                )
            else:
                await interaction.followup.send(f"❌ Erreur code: {e}")


class PasswordModal(discord.ui.Modal, title="Mot de Passe 2FA"):
    password_input = discord.ui.TextInput(label="Mot de passe", placeholder="S3cr3t")

    def __init__(self, view):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        pwd = self.password_input.value
        await interaction.response.defer(ephemeral=True)

        try:
            # On tente le sign-in avec le mot de passe
            await self.view_ref.client.sign_in(password=pwd)

            # Si ça passe
            new_session = StringSession.save(self.view_ref.client.session)

            conn = sqlite3.connect("database.db")
            conn.execute(
                "UPDATE telegram_accounts SET session_string=? WHERE id=?",
                (new_session, self.view_ref.account_id),
            )
            conn.commit()
            conn.close()

            await interaction.followup.send(
                f"✅ Session réparée (2FA) et sauvegardée pour {self.view_ref.phone} !"
            )
            self.view_ref.stop()

        except Exception as e:
            await interaction.followup.send(f"❌ Erreur mot de passe: {e}")


@bot.tree.command(
    name="fix_stock", description="Réparer les sessions invalides sur le VPS"
)
async def fix_stock(interaction: discord.Interaction):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ Accès refusé.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    conn = sqlite3.connect("database.db")
    accounts = conn.execute(
        "SELECT id, phone, session_string FROM telegram_accounts WHERE status='AVAILABLE'"
    ).fetchall()
    conn.close()

    await interaction.followup.send(
        f"🔄 Analyse de {len(accounts)} comptes en cours... Cela peut prendre du temps."
    )

    issues_found = 0

    for acc_id, phone, session_str in accounts:
        if not TG_API_ID or not TG_API_HASH:
            await interaction.followup.send("❌ Config TELEGRAM_API_ID manquante.")
            return

        client = TelegramClient(StringSession(session_str), int(TG_API_ID), TG_API_HASH)

        try:
            await client.connect()
            if not await client.is_user_authorized():
                # Session invalide -> Besoin de réparation
                issues_found += 1

                # On déclenche l'envoi du code
                try:
                    await client.send_code_request(phone)
                    msg = f"⚠️ **Compte {phone}** : Session invalide. Code envoyé (SMS/Mail)."
                    view = FixSessionView(client, phone, acc_id)
                    await interaction.followup.send(msg, view=view, ephemeral=True)

                    # On attend que l'admin interagisse avant de passer au suivant (pour éviter de tout spammer)
                    # Hack : on attend que la vue soit stoppée
                    await view.wait()

                except Exception as e:
                    await interaction.followup.send(
                        f"❌ Erreur critique sur {phone} (impossible d'envoyer le code) : {e}",
                        ephemeral=True,
                    )
                    await client.disconnect()
            else:
                # Session valide, on déconnecte juste
                await client.disconnect()

        except Exception as e:
            print(f"Erreur check {phone}: {e}")
            try:
                await client.disconnect()
            except:
                pass

    if issues_found == 0:
        await interaction.followup.send(
            "✅ Tous les comptes du stock semblent actifs et valides !", ephemeral=True
        )
    else:
        await interaction.followup.send("🏁 Vérification terminée.", ephemeral=True)


@bot.tree.command(
    name="balance", description="Voir mon solde ou celui d'un utilisateur (Admin)"
)
async def balance(interaction: discord.Interaction, user: discord.User = None):
    target_user = user if user else interaction.user

    # Si on demande le solde d'un autre et qu'on n'est PAS admin -> Refusé
    if target_user.id != interaction.user.id and not is_user_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ Vous ne pouvez voir que votre propre solde.", ephemeral=True
        )

    user_balance = get_balance(target_user.id)

    msg_prefix = (
        "💰 **Votre solde"
        if target_user.id == interaction.user.id
        else f"💰 **Solde de {target_user.name}"
    )

    await interaction.response.send_message(
        f"{msg_prefix} : {user_balance:.2f}€**", ephemeral=True
    )


@bot.tree.command(name="services", description="Voir les services et prix disponibles")
@app_commands.describe(
    country="Le pays pour lequel afficher les prix (défaut : France)"
)
@app_commands.choices(
    country=[
        app_commands.Choice(name="France (+33)", value="france"),
        app_commands.Choice(name="Canada (+1)", value="canada"),
    ]
)
async def services(
    interaction: discord.Interaction, country: app_commands.Choice[str] = None
):
    await interaction.response.defer(ephemeral=True)

    # Pays par défaut : France
    selected_country_code = "france"
    selected_country_name = "France (+33)"

    if country:
        selected_country_code = country.value
        selected_country_name = country.name

    embed = discord.Embed(title="📱 Services Disponibles", color=0x00FF00)
    user_balance = get_balance(interaction.user.id)
    description = f"**Votre solde : {user_balance:.2f}€**\n\n**Pays : {selected_country_name}**\n\n"

    country_id = COUNTRIES[selected_country_code]

    # Optimisation : On lance toutes les requêtes en parallèle et non une par une
    # Cela évite le timeout de Discord si l'API est lente
    tasks = []
    service_list = []
    for name, code in SERVICES.items():
        service_list.append(name)
        tasks.append(sms_api.get_price(code, country_id))

    results = await asyncio.gather(*tasks)
    for i, res in enumerate(results):
        svc_name = service_list[i].capitalize()
        if res is not None:
            final_price = calculate_selling_price(res)
            # Petit embellissement
            emoji = "📱"
            if "whatsapp" in svc_name.lower():
                emoji = "🟢"
            elif "telegram" in svc_name.lower():
                emoji = "🔵"
            elif "uber" in svc_name.lower():
                emoji = "🚗"

            description += f"{emoji} **{svc_name}** : {final_price:.2f}€\n"
        else:
            description += f"🔴 **{svc_name}** : *Indisponible*\n"

    embed.description = description
    embed.set_footer(text="Prix sujets à variation (Offre/Demande)")
    await interaction.followup.send(embed=embed, ephemeral=True)


async def execute_pack_logic(interaction: discord.Interaction):
    # Pack WA (France) + TG (Canada)
    steps = [("telegram", "united_kingdom")]

    # 1. Calcul des prix pour le pack
    svc1_code = SERVICES["whatsapp"]
    ctry1_id = COUNTRIES["france"]

    svc2_code = SERVICES["telegram"]
    ctry2_id = COUNTRIES["united_kingdom"]

    # On lance les requêtes de prix en parallèle
    price1_brut, price2_brut = await asyncio.gather(
        sms_api.get_price(svc1_code, ctry1_id), sms_api.get_price(svc2_code, ctry2_id)
    )

    if price1_brut is None or price2_brut is None:
        return await interaction.followup.send(
            "⚠️ Un des services du pack est indisponible pour le moment.", ephemeral=True
        )

    price1 = calculate_selling_price(price1_brut)
    price2 = calculate_selling_price(price2_brut)
    total_price = price1 + price2

    # On envoie un DM de confirmation
    try:
        dm_channel = await interaction.user.create_dm()

        embed = discord.Embed(
            title="📦 Confirmation Pack",
            description="Détail du Pack **Whatsapp (FR) + Telegram (UK)**",
            color=0xE91E63,
        )
        embed.add_field(
            name="1. Whatsapp (France)", value=f"{price1:.2f}€", inline=True
        )
        embed.add_field(name="2. Telegram (UK)", value=f"{price2:.2f}€", inline=True)
        embed.add_field(
            name="💰 PRIX TOTAL", value=f"**{total_price:.2f}€**", inline=False
        )
        embed.set_footer(text="Le débit se fera étape par étape.")

        view = ConfirmPackView(interaction.user.id, price1, steps)
        await dm_channel.send(embed=embed, view=view)
        if interaction.guild:
            await interaction.followup.send(
                "📩 Confirmation envoyée en MP. Vérifiez vos messages !", ephemeral=True
            )
        else:
            await interaction.followup.send("📩 Confirmation envoyée !", ephemeral=True)

    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Impossible de vous envoyer un MP. Ouvrez vos messages privés.",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur : {e}", ephemeral=True)


class ConfirmPackView(discord.ui.View):
    def __init__(self, user_id, price, steps):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.price = price
        self.steps = steps

    @discord.ui.button(label="✅ Valider et Acheter", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            return

        await interaction.response.defer()
        # On désactive les boutons
        self.clear_items()
        await interaction.edit_original_response(
            content="✅ **Lancement du pack...**", view=self
        )

        # On lance la logique d'achat pour le premier item
        # Appel à execute_buy_logic avec les paramètres de la Step 1 (WhatsApp France)
        # Note: execute_buy_logic attend une interaction pour répondre. On lui passe l'interaction du bouton.
        await execute_buy_logic(
            interaction, "whatsapp", "france", next_steps=self.steps
        )

    @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return

        self.clear_items()
        try:
            await interaction.response.edit_message(
                content="❌ Pack annulé.", view=self
            )
        except discord.NotFound:
            await interaction.followup.send("❌ Pack annulé.", ephemeral=True)


class ConfirmBuyView(discord.ui.View):
    def __init__(self, user_id, price, service_key, country_key):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.price = price
        self.service_key = service_key
        self.country_key = country_key

    @discord.ui.button(label="✅ Valider et Acheter", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            return

        await interaction.response.defer()
        # On désactive les boutons
        self.clear_items()
        await interaction.edit_original_response(
            content="✅ **Lancement de l'achat...**", view=self
        )

        await execute_buy_logic(interaction, self.service_key, self.country_key)

    @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return

        self.clear_items()
        self.clear_items()
        try:
            await interaction.response.edit_message(
                content="❌ Achat annulé.", view=self
            )
        except discord.NotFound:
            await interaction.followup.send("❌ Achat annulé.", ephemeral=True)


async def execute_buy_logic(
    interaction: discord.Interaction,
    service_key: str,
    country_key: str,
    next_steps=None,
):
    user_id = interaction.user.id

    if service_key not in SERVICES or country_key not in COUNTRIES:
        return await interaction.followup.send(
            "❌ Service ou pays invalide (Configuration).", ephemeral=True
        )

    service_code = SERVICES[service_key]
    country_id = COUNTRIES[country_key]
    service_name = service_key.capitalize()
    country_name = country_key.capitalize()

    # 1. Vérif Prix et Stock
    cost_price = await sms_api.get_price(service_code, country_id)
    if cost_price is None:
        return await interaction.followup.send(
            f"⚠️ Stock épuisé ou erreur prix pour **{service_name}** ({country_name}). Réessayez plus tard.",
            ephemeral=True,
        )

    price = calculate_selling_price(cost_price)

    # 2. Vérif Solde
    balance = get_balance(user_id)
    if balance < price:
        return await interaction.followup.send(
            f"❌ **Solde insuffisant.**\nPrix : {price:.2f}€\nVotre solde : {balance:.2f}€",
            ephemeral=True,
        )

    # 3. Achat (Boucle de tentative)
    max_retries = 5
    order = None

    for i in range(max_retries):
        temp_order = await sms_api.buy_number(
            service_code, country_id, user_info=f"User {user_id}"
        )

        if temp_order["success"]:
            # Vérif doublons
            if is_number_used(temp_order["phone"], service_name):
                print(f"DOUBLON REJETÉ: {temp_order['phone']}")
                await sms_api.cancel_order(
                    temp_order["id"], user_info=f"User {user_id}"
                )
                await asyncio.sleep(1)
                continue
            else:
                order = temp_order
                break
        else:
            # Si erreur NO_NUMBERS, on peut arrêter
            if temp_order.get("code") == "NO_NUMBERS":
                break
            await asyncio.sleep(0.5)

    if not order or not order["success"]:
        error_msg = order["error"] if order and "error" in order else "Erreur inconnue"
        return await interaction.followup.send(
            f"❌ Impossible d'obtenir un numéro pour {service_name}. \nRaison : {error_msg}",
            ephemeral=True,
        )

    # 4. Débit et Enregistrement
    update_balance(user_id, -price)

    conn = sqlite3.connect("database.db")
    conn.execute(
        "INSERT INTO orders (order_id, discord_id, phone, price, status, created_at, service, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            order["id"],
            user_id,
            order["phone"],
            price,
            "PENDING",
            str(datetime.now()),
            service_name,
            cost_price,
        ),
    )
    conn.commit()
    conn.close()

    # 5. Envoi DM
    try:
        dm_channel = await interaction.user.create_dm()
        view = OrderView(
            order["id"],
            price,
            user_id,
            interaction,
            phone=order["phone"],
            service_name=service_name,
            service_key=service_key,
            country_key=country_key,
            next_steps=next_steps,
        )
        new_balance = get_balance(user_id)

        # Formatage du numéro pour le Canada (Retirer le 1 au début)
        display_phone = order["phone"]
        if (
            country_key == "canada"
            and str(display_phone).startswith("1")
            and len(str(display_phone)) > 10
        ):
            display_phone = str(display_phone)[1:]

        msg = f"✅ **Commande validée !**\nService : **{service_name}** | Pays : **{country_name}**\nNuméro : `{display_phone}`\n\nAttendez le code ci-dessous..."
        if next_steps:
            msg += f"\n\n🎁 **PACK EN COURS** : Prochaine étape -> {next_steps[0][0].capitalize()}"

        dm_message = await dm_channel.send(msg, view=view)
        # Message éphémère de confirmation (Seulement si sur un serveur)
        if interaction.guild:
            await interaction.followup.send(
                f"✅ Numéro acheté ! Regardez vos MP.", ephemeral=True
            )

        # Lancement du check
        asyncio.create_task(check_sms_loop(order["id"], dm_channel, view, dm_message))

    except Exception as e:
        await interaction.followup.send(
            f"❌ Erreur lors de l'envoi du MP (Ouvrez vos MP !) : {e}", ephemeral=True
        )


# --- TÂCHE DE FOND : VÉRIFICATION DU SMS ---
async def check_sms_loop(order_id, channel, view, dm_message):
    attempts = 0
    received_codes = set()  # Pour éviter de renvoyer le même code en boucle

    while attempts < 300:  # 300 * 5s = 25 minutes max
        if view.is_cancelled or view.process_finished:
            break

        status_text = await sms_api.get_status(order_id)

        # CAS 1 : CODE REÇU
        if "STATUS_OK" in status_text:
            code = status_text.split(":")[1].strip()

            if code not in received_codes:
                received_codes.add(code)
                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CODE RECU: {code}"
                )

                # Mise à jour de la vue pour activer les boutons suite au code
                view.code_received = True

                # On active les boutons Finish et Retry, on désactive Cancel
                for child in view.children:
                    if child.custom_id:
                        if child.custom_id.startswith(
                            "btn_finish"
                        ) or child.custom_id.startswith("btn_retry"):
                            child.disabled = False
                        if child.custom_id.startswith("btn_cancel"):
                            child.disabled = True

                # On envoie le code
                await channel.send(
                    f"📩 **CODE REÇU :** `{code}`\n*Si ce code ne fonctionne pas, cliquez sur 'Demander un autre code'.*"
                )

                # On met à jour le message original (DM) avec les boutons activés
                if dm_message:
                    try:
                        await dm_message.edit(view=view)
                    except Exception as e:
                        print(f"Erreur update view DM: {e}")

                # On réinitialise le compteur pour laisser du temps si on veut un autre code
                attempts = 0

            # On ne sort PAS de la boucle, on attend que l'utilisateur choisisse "Terminer" ou "Autre code"

        # CAS 2 : ANNULÉ PAR LE FOURNISSEUR
        elif "STATUS_CANCEL" in status_text:
            await refund_user_channel(view.user_id, view.price, order_id, channel)
            return

        attempts += 1
        await asyncio.sleep(5)  # Pause de 5 secondes

    # Si on sort de la boucle sans code (Timeout) ou si fini
    if view.process_finished:
        # Commande validée par l'utilisateur
        conn = sqlite3.connect("database.db")
        conn.execute(
            "UPDATE orders SET status='COMPLETED' WHERE order_id=?", (order_id,)
        )
        conn.commit()
        conn.close()

    elif not view.is_cancelled and not view.process_finished:
        if not received_codes:
            # Timeout sans AUCUN code reçu -> On annule et rembourse
            await sms_api.cancel_order(order_id, user_info=f"User {view.user_id}")
            await refund_user_channel(
                view.user_id, view.price, order_id, channel, reason="Temps écoulé"
            )
        else:
            # Timeout MAIS on a eu des codes -> On considère "Terminé" (le client a oublié de valider)
            await sms_api.request("setStatus", {"id": order_id, "status": "6"})
            conn = sqlite3.connect("database.db")
            conn.execute(
                "UPDATE orders SET status='COMPLETED' WHERE order_id=?", (order_id,)
            )
            conn.commit()
            conn.close()
            await channel.send("ℹ️ Temps écoulé. Commande validée automatiquement.")


async def refund_user_channel(user_id, amount, order_id, channel, reason="Annulation"):
    update_balance(user_id, amount)
    conn = sqlite3.connect("database.db")
    conn.execute("UPDATE orders SET status='REFUNDED' WHERE order_id=?", (order_id,))
    conn.commit()
    conn.close()
    await channel.send(
        f"info : Commande annulée ({reason}). Vous avez été remboursé de {amount}€."
    )


async def refund_user(user_id, amount, order_id, interaction, reason="Annulation"):
    update_balance(user_id, amount)
    conn = sqlite3.connect("database.db")
    conn.execute("UPDATE orders SET status='REFUNDED' WHERE order_id=?", (order_id,))
    conn.commit()
    conn.close()
    await interaction.followup.send(
        f"info : Commande annulée ({reason}). Vous avez été remboursé de {amount}€.",
        ephemeral=True,
    )


# --- INTERFACE BOUTONS ---
class OrderView(discord.ui.View):
    def __init__(
        self,
        order_id,
        price,
        user_id,
        original_interaction,
        phone=None,
        service_name=None,
        service_key=None,
        country_key=None,
        next_steps=None,
    ):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.price = price
        self.user_id = user_id
        self.original_interaction = original_interaction
        self.phone = phone
        self.service_name = service_name
        self.service_key = service_key
        self.country_key = country_key
        self.is_cancelled = False
        self.process_finished = False
        self.code_received = False
        self.next_steps = next_steps

        # --- FIX: IDs UNIQUES POUR LA PERSISTANCE ---
        # Pour que bot.add_view() fonctionne avec plusieurs commandes,
        # il faut que chaque bouton ait un custom_id unique.
        for item in self.children:
            if item.custom_id:
                # On ajoute l'ID de la commande au custom_id de base (ex: btn_finish_12345)
                item.custom_id = f"{item.custom_id}_{self.order_id}"

    @discord.ui.button(
        label="Terminer (Validé)",
        style=discord.ButtonStyle.success,
        disabled=True,
        custom_id="btn_finish",
    )
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "Ce n'est pas votre commande !", ephemeral=True
            )

        await interaction.response.defer()
        # On valide la commande chez SMS-Activate (Status 6)
        await sms_api.request("setStatus", {"id": self.order_id, "status": "6"})
        self.process_finished = True
        self.stop()  # On arrête la vue
        await interaction.followup.send(
            "✅ Commande terminée avec succès.", ephemeral=True
        )

        if self.next_steps:
            # On prend le premier élément
            next_svc, next_ctry = self.next_steps[0]
            remaining_steps = self.next_steps[1:]

            await interaction.followup.send(
                f"🔄 **Lancement automatique de la suite du pack : {next_svc.capitalize()} ({next_ctry.capitalize()})...**",
                ephemeral=True,
            )

            # On appelle execute_buy_logic avec le reste des étapes
            # Note: execute_buy_logic prend 'interaction', on peut réutiliser l'actuelle (followup)
            await execute_buy_logic(
                interaction, next_svc, next_ctry, next_steps=remaining_steps
            )

    @discord.ui.button(
        label="Demander un autre code",
        style=discord.ButtonStyle.primary,
        disabled=True,
        custom_id="btn_retry",
    )
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "Ce n'est pas votre commande !", ephemeral=True
            )

        await interaction.response.defer()
        # On demande un autre code (Status 3)
        await sms_api.request("setStatus", {"id": self.order_id, "status": "3"})
        await interaction.followup.send(
            "🔄 Demande de nouveau code envoyée... Attendez le prochain SMS.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Annuler & Rembourser",
        style=discord.ButtonStyle.danger,
        custom_id="btn_cancel",
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "Ce n'est pas votre commande !", ephemeral=True
            )

        if self.code_received:
            return await interaction.response.send_message(
                "❌ Impossible d'annuler car un code a déjà été reçu. Utilisez 'Terminer' ou demandez un autre code.",
                ephemeral=True,
            )

        # On signale à Discord qu'on traite la demande (évite 'Echec interaction')
        await interaction.response.defer()

        # On désactive le bouton pour éviter le double-clic
        button.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.NotFound:
            # Le message a peut-être été supprimé, mais on continue l'annulation
            pass

        # 1. On tente d'annuler chez SMS-Activate D'ABORD
        api_response = await sms_api.cancel_order(
            self.order_id, user_info=f"User {self.user_id}"
        )

        # 2. On vérifie la réponse du fournisseur
        if (
            "ACCESS_CANCEL" in api_response
            or "ACCESS_ACTIVATION_CANCELED" in api_response
        ):
            self.is_cancelled = True
            self.stop()
            # 3. C'est confirmé, on rembourse le client
            # Note: refund_user utilise followup.send donc c'est compatible avec defer()
            await refund_user(
                self.user_id,
                self.price,
                self.order_id,
                interaction,
                reason="Annulation utilisateur",
            )

        else:
            # 4. ÉCHEC
            button.disabled = False
            self.is_cancelled = False

            error_message = f"❌ Impossible d'annuler pour le moment (Réponse API: `{api_response}`). Réessayez."

            if "EARLY_CANCEL_DENIED" in api_response:
                error_message = "⏳ **Trop tôt pour annuler !**\nVeuillez attendre **2 minutes** après l'achat avant de pouvoir annuler.\nRéessayez dans quelques instants."

            await interaction.followup.send(error_message, ephemeral=True)
            try:
                await interaction.edit_original_response(view=self)
            except discord.NotFound:
                # Si le message d'origine est introuvable (trop vieux), on envoie une nouvelle vue
                await interaction.followup.send(
                    "⚠️ La vue a expiré, voici les nouveaux contrôles :",
                    view=self,
                    ephemeral=True,
                )

    @discord.ui.button(
        label="⛔ Compte Banni",
        style=discord.ButtonStyle.danger,
        custom_id="btn_block_number",
        row=1,
    )
    async def block_number(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "Ce n'est pas votre commande !", ephemeral=True
            )

        if self.code_received:
            return await interaction.response.send_message(
                "❌ Impossible de signaler un compte banni après avoir reçu un code.",
                ephemeral=True,
            )

        await interaction.response.defer()
        button.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.NotFound:
            pass

        # 1. Tentative d'annulation chez SMS-Activate (Best effort)
        api_response = await sms_api.cancel_order(
            self.order_id, user_info=f"User {self.user_id}"
        )
        # On log mais on ignore l'erreur si ça rate (le remboursement auto se fera plus tard côté API)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[{timestamp}] [Initiator: User {self.user_id}] Cancel Blocked Number response: {api_response}"
        )

        # 2. On procède DIRECTEMENT au remboursement interne et au changement de numéro
        self.is_cancelled = True
        self.stop()

        # 3. Remboursement interne immédiat
        await refund_user(
            self.user_id,
            self.price,
            self.order_id,
            interaction,
            reason="Compte Banni - Signalé par utilisateur",
        )

        # 4. Blacklist du numéro
        if self.phone and self.service_name:
            block_number_db(self.phone, self.service_name)

        await interaction.followup.send(
            f"🚫 **Numéro {self.phone} géré (Banni).** Remplacement automatique en cours...",
            ephemeral=True,
        )

        # 5. Retry Logic (Relance immédiate avec nouveau numéro)
        # On relance exactement la même demande (même service, même pays, même suite de pack)
        if self.service_key and self.country_key:
            await execute_buy_logic(
                interaction,
                self.service_key,
                self.country_key,
                next_steps=self.next_steps,
            )

    @discord.ui.button(
        label="🔄 Prendre un autre",
        style=discord.ButtonStyle.primary,
        emoji="🛒",
        row=1,
    )
    async def rebuy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "Ce n'est pas votre commande !", ephemeral=True
            )

        if not self.service_key or not self.country_key:
            return await interaction.response.send_message(
                "❌ Impossible de recommander (Info manquante).", ephemeral=True
            )

        await interaction.response.defer()
        # On relance direct le processus d'achat
        await execute_buy_logic(interaction, self.service_key, self.country_key)


async def setup_dashboard(guild):
    channel_name = "commander-num"
    channel = discord.utils.get(guild.text_channels, name=channel_name)

    if not channel:
        try:
            # Création du salon
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(send_messages=False),
                guild.me: discord.PermissionOverwrite(send_messages=True),
            }
            channel = await guild.create_text_channel(
                channel_name, overwrites=overwrites
            )
        except Exception as e:
            print(f"Erreur création salon sur {guild.name}: {e}")
            return

    # Nettoyage complet du salon (pour ne garder qu'un seul message)
    try:
        await channel.purge(limit=100)
    except Exception as e:
        print(f"Erreur purge channel: {e}")

    view = DashboardView()
    embed = discord.Embed(
        title="🚀 QuickSMS Dashboard",
        description="Bienvenue ! Utilisez les boutons ci-dessous pour commander.",
        color=0x3498DB,
    )

    await channel.send(embed=embed, view=view)


class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🛒 Acheter un numéro",
        style=discord.ButtonStyle.primary,
        custom_id="dash_buy",
    )
    async def buy_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "🌍 **Étape 1 : Choisissez le pays**",
            view=CountrySelectView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="📱 Services & Prix",
        style=discord.ButtonStyle.secondary,
        custom_id="dash_services",
    )
    async def services_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # Réutilisation de la logique /services
        # On peut appeler la fonction services() mais c'est une commande...
        # On va refaire une vue simple ou appeler le select Country pour afficher les prix
        await interaction.response.send_message(
            "🌍 **Voir les prix pour quel pays ?**",
            view=CountrySelectView(mode="prices"),
            ephemeral=True,
        )

    @discord.ui.button(
        label="💰 Mon Solde",
        style=discord.ButtonStyle.success,
        custom_id="dash_balance",
    )
    async def balance_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        balance = get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"💰 **Votre solde : {balance:.2f}€**",
            ephemeral=True,
        )

    @discord.ui.button(
        label="📦 Pack: WA(FR) + TG(CA)",
        style=discord.ButtonStyle.danger,
        custom_id="dash_pack_wa_tg_v2",
    )
    async def pack_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer(ephemeral=True)
        await execute_pack_logic(interaction)

    @discord.ui.button(
        label="🔥 Compte Telegram (Session)",
        style=discord.ButtonStyle.danger,
        emoji="✈️",
        custom_id="dash_buy_account",
        row=1,
    )
    async def buy_account_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # 1. Vérif Stock
        stock_count = count_telegram_stock()
        if stock_count == 0:
            return await interaction.response.send_message(
                "❌ **Rupture de Stock !**\nRevenez plus tard ou ouvrez un ticket pour pré-commander.",
                ephemeral=True,
            )

        # 2. Prix (Fixe pour l'instant ou récupéré du prochain item)
        # On définit un prix de vente standard ou basé sur le coût du premier item
        next_account = get_available_telegram_account()
        if not next_account:
            return await interaction.response.send_message(
                "❌ Erreur Stock (Ghost).", ephemeral=True
            )

        cost = next_account["cost"]
        # Prix de vente : 2.00€ minimum OU le double du coût d'achat si supérieur
        selling_price = max(2.00, round(cost * 2, 2))

        # 3. Confirmation
        embed = discord.Embed(
            title="✈️ Acheter un Compte Telegram",
            description=f"**Produit :** Compte Telegram (Session)\n**Stock dispo :** {stock_count}\n**Prix :** {selling_price}€\n\n✅ **Connexion Facile** (Code via le Bot)",
            color=0x0088CC,
        )

        view = ConfirmAccountBuyView(
            interaction.user.id, selling_price, next_account["id"]
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ConfirmAccountBuyView(discord.ui.View):
    def __init__(self, user_id, price, account_id):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.price = price
        self.account_id = account_id

    @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return
        await interaction.response.edit_message(
            content="❌ Achat annulé.", embed=None, view=None
        )

    @discord.ui.button(label="✅ Confirmer l'achat", style=discord.ButtonStyle.success)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            return

        await interaction.response.defer()

        # Double check stock (Race condition)
        target_account = get_available_telegram_account()
        if not target_account or target_account["id"] != self.account_id:
            # Si l'ID a changé mais qu'il y en a un autre, on prend l'autre
            if target_account:
                self.account_id = target_account["id"]
            else:
                return await interaction.followup.send(
                    "❌ Trop tard ! Le dernier compte vient de partir.", ephemeral=True
                )

        # Check Solde
        user_balance = get_balance(self.user_id)
        if user_balance < self.price:
            return await interaction.followup.send(
                f"❌ Solde insuffisant ({user_balance:.2f}€).", ephemeral=True
            )

        # Achat !
        update_balance(self.user_id, -self.price)
        mark_telegram_account_sold(self.account_id, self.user_id)

        # Historique
        conn = sqlite3.connect("database.db")
        conn.execute(
            "INSERT INTO orders (order_id, discord_id, phone, price, status, created_at, service, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"ACC-{self.account_id}",
                self.user_id,
                target_account["phone"],
                self.price,
                "COMPLETED",
                str(datetime.now()),
                "Telegram Account",
                target_account["cost"],
            ),
        )
        conn.commit()
        conn.close()

        # DM
        try:
            dm_channel = await interaction.user.create_dm()

            embed_dm = discord.Embed(
                title="✈️ Votre Compte Telegram",
                description=f"**Numéro :** `{target_account['phone']}`",
                color=0x0088CC,
            )
            embed_dm.add_field(
                name="1. Connexion",
                value="Entrez le numéro sur votre application Telegram.",
                inline=False,
            )
            embed_dm.add_field(
                name="2. Code",
                value="Cliquez sur le bouton ci-dessous pour recevoir le code envoyé par Telegram.",
                inline=False,
            )

            if target_account["password_2fa"]:
                embed_dm.add_field(
                    name="🔐 2FA (Mot de passe)",
                    value=f"`{target_account['password_2fa']}`",
                    inline=False,
                )

            view_dm = TelegramAccountView(target_account["session_string"])
            await dm_channel.send(embed=embed_dm, view=view_dm)

            await interaction.followup.send(
                "✅ Compte acheté ! Vérifiez vos MP.\n💡 *Retrouvez vos comptes via la commande* `/myaccounts`",
                ephemeral=True,
            )
            self.stop()
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Impossible d'envoyer le MP ! (Remboursement auto à faire par admin)",
                ephemeral=True,
            )


class TelegramAccountView(discord.ui.View):
    def __init__(self, session_string):
        super().__init__(timeout=None)
        self.session_string = session_string

    @discord.ui.button(
        label="📩 Recevoir le Code", style=discord.ButtonStyle.primary, emoji="📬"
    )
    async def get_code_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        await interaction.followup.send(
            "⏳ Connexion au compte et recherche du code... (Patientez 5-10s)",
            ephemeral=True,
        )

        result = await TelethonHandler.get_login_code(self.session_string)

        if result["success"]:
            await interaction.followup.send(
                f"✅ **CODE REÇU :** `{result['code']}`", ephemeral=True
            )
        else:
            await interaction.followup.send(f"⚠️ {result['error']}", ephemeral=True)


@bot.tree.command(
    name="addstock", description="Ajouter un compte Telegram au stock (Admin)"
)
async def addstock(
    interaction: discord.Interaction,
    phone: str,
    session: str,
    cost: float,
    password: str = None,
):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message("❌", ephemeral=True)

    add_telegram_account_db(phone, session, password, cost)
    await interaction.response.send_message(
        f"✅ Compte {phone} ajouté au stock (Coût: {cost}€)", ephemeral=True
    )


@bot.tree.command(
    name="stock", description="Voir l'état du stock de comptes Telegram (Admin)"
)
async def stock(interaction: discord.Interaction):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message("❌", ephemeral=True)

    conn = sqlite3.connect("database.db")
    total = conn.execute("SELECT COUNT(*) FROM telegram_accounts").fetchone()[0]
    available = conn.execute(
        "SELECT COUNT(*) FROM telegram_accounts WHERE status='AVAILABLE'"
    ).fetchone()[0]
    sold = conn.execute(
        "SELECT COUNT(*) FROM telegram_accounts WHERE status='SOLD'"
    ).fetchone()[0]
    conn.close()

    embed = discord.Embed(title="📦 Inventaire Telegram", color=0xF1C40F)
    embed.add_field(name="Total", value=str(total), inline=True)
    embed.add_field(name="🟢 Disponibles", value=str(available), inline=True)
    embed.add_field(name="🔴 Vendus", value=str(sold), inline=True)

    # Estimation valeur stock
    estimated_value = available * 1.5  # Prix arbitraire ou moyen
    embed.set_footer(text=f"Valeur estimée du stock dispo : ~{estimated_value}€")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="myaccounts",
    description="Voir mes comptes Telegram achetés (et recevoir le code)",
)
async def myaccounts(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    conn = sqlite3.connect("database.db")
    # On récupère les 5 derniers comptes achetés par l'utilisateur
    rows = conn.execute(
        "SELECT id, phone, session_string, password_2fa, sold_at FROM telegram_accounts WHERE sold_to=? ORDER BY sold_at DESC LIMIT 5",
        (interaction.user.id,),
    ).fetchall()
    conn.close()

    if not rows:
        return await interaction.followup.send(
            "❌ Vous n'avez acheté aucun compte Telegram.", ephemeral=True
        )

    for row in rows:
        acc_id, phone, session, pwd, date = row

        embed = discord.Embed(title=f"📱 Compte {phone}", color=0x0088CC)
        embed.add_field(name="Date Achat", value=str(date), inline=True)
        if pwd:
            embed.add_field(name="🔐 2FA", value=f"`{pwd}`", inline=True)

        view = TelegramAccountView(session)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # Message de fin
    await interaction.followup.send("✅ Voici vos comptes récents.", ephemeral=True)


class ClearStockView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(
        label="🗑️ Supprimer le Stock (Invendus)", style=discord.ButtonStyle.danger
    )
    async def clear_available(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        conn = sqlite3.connect("database.db")
        count = conn.execute(
            "DELETE FROM telegram_accounts WHERE status='AVAILABLE'"
        ).rowcount
        conn.commit()
        conn.close()
        await interaction.edit_original_response(
            content=f"✅ **{count} comptes disponibles** ont été supprimés du stock.",
            view=None,
        )

    @discord.ui.button(
        label="🔥 TOUT Supprimer (Reset Total)", style=discord.ButtonStyle.danger
    )
    async def clear_all(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.defer()
        conn = sqlite3.connect("database.db")
        count = conn.execute("DELETE FROM telegram_accounts").rowcount
        conn.commit()
        conn.close()
        await interaction.edit_original_response(
            content=f"⚠️ **Reset Complet** : La table Telegram a été vidée ({count} comptes supprimés).",
            view=None,
        )

    @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Opération annulée.", view=None)


@bot.tree.command(
    name="clearstock", description="Supprimer des comptes Telegram de la DB (Admin)"
)
async def clearstock(interaction: discord.Interaction):
    if not is_user_admin(interaction.user.id):
        return await interaction.response.send_message("❌", ephemeral=True)

    view = ClearStockView()
    await interaction.response.send_message(
        "⚠️ **Zone de Danger**\nQue voulez-vous supprimer ?\n\n*Note : Supprimer 'Tout' effacera aussi l'historique des comptes vendus (les clients perdront l'accès au code).*",
        view=view,
        ephemeral=True,
    )


class CountrySelectView(discord.ui.View):
    def __init__(self, mode="buy"):
        super().__init__(timeout=60)
        self.mode = mode

        # Construction des options depuis COUNTRIES
        options = []
        for name, code in COUNTRIES.items():
            options.append(discord.SelectOption(label=name.capitalize(), value=name))

        self.add_item(CountrySelect(options, mode))


class CountrySelect(discord.ui.Select):
    def __init__(self, options, mode):
        self.mode = mode
        super().__init__(
            placeholder="Sélectionnez un pays...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        country_key = self.values[0]

        if self.mode == "buy":
            await interaction.response.edit_message(
                content=f"📱 **Pays : {country_key.capitalize()}**. Choisissez le service :",
                view=ServiceSelectView(country_key),
            )
        else:
            # Mode Prix
            await interaction.response.defer(ephemeral=True)

            country_id = COUNTRIES[country_key]
            tasks = []
            service_list = []
            for name, code in SERVICES.items():
                service_list.append(name)
                tasks.append(sms_api.get_price(code, country_id))

            results = await asyncio.gather(*tasks)

            embed = discord.Embed(
                title=f"📱 Services & Prix ({country_key.capitalize()})", color=0x00FF00
            )
            description = ""

            for i, res in enumerate(results):
                svc_name = service_list[i].capitalize()
                if res is not None:
                    final_price = calculate_selling_price(res)
                    emoji = "📱"
                    if "whatsapp" in svc_name.lower():
                        emoji = "🟢"
                    elif "telegram" in svc_name.lower():
                        emoji = "🔵"
                    elif "uber" in svc_name.lower():
                        emoji = "🚗"

                    description += f"{emoji} **{svc_name}** : {final_price:.2f}€\n"
                else:
                    description += f"🔴 **{svc_name}** : *Indisponible*\n"

            embed.description = description
            embed.set_footer(text="Prix sujets à variation (Offre/Demande)")
            await interaction.edit_original_response(
                content=None, embed=embed, view=None
            )


class ServiceSelectView(discord.ui.View):
    def __init__(self, country_key):
        super().__init__(timeout=60)

        # Options pour le Select
        options = []
        for name in SERVICES.keys():
            options.append(discord.SelectOption(label=name.capitalize(), value=name))

        self.add_item(ServiceSelect(options, country_key))


class ServiceSelect(discord.ui.Select):
    def __init__(self, options, country_key):
        self.country_key = country_key
        super().__init__(
            placeholder="Sélectionnez un service...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        service_key = self.values[0]
        await interaction.response.defer(ephemeral=True)

        # 1. Récupération du prix pour confirmation
        service_code = SERVICES[service_key]
        country_id = COUNTRIES[self.country_key]

        cost_price = await sms_api.get_price(service_code, country_id)

        if cost_price is None:
            return await interaction.followup.send(
                f"⚠️ Stock épuisé ou erreur prix pour **{service_key.capitalize()}** ({self.country_key.capitalize()}). Réessayez plus tard.",
                ephemeral=True,
            )

        final_price = calculate_selling_price(cost_price)

        # 2. Affichage de la confirmation
        embed = discord.Embed(
            title="🛒 Confirmation d'Achat",
            description="Détail de la commande :",
            color=0x3498DB,
        )
        embed.add_field(name="📱 Service", value=service_key.capitalize(), inline=True)
        embed.add_field(
            name="🌍 Pays", value=self.country_key.capitalize(), inline=True
        )
        embed.add_field(name="💰 Prix", value=f"**{final_price:.2f}€**", inline=False)
        embed.set_footer(text="Le débit sera effectué après confirmation.")

        view = ConfirmBuyView(
            interaction.user.id, final_price, service_key, self.country_key
        )

        # Envoi en DM
        try:
            dm_channel = await interaction.user.create_dm()
            await dm_channel.send(embed=embed, view=view)

            # On met à jour le message éphémère pour dire que c'est envoyé
            msg_confirm = "📩 Confirmation envoyée en MP. Vérifiez vos messages !"
            if not interaction.guild:
                msg_confirm = "📩 Confirmation générée :"

            await interaction.edit_original_response(
                content=msg_confirm,
                embed=None,
                view=None,
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="❌ Impossible de vous envoyer un MP. Ouvrez vos messages privés.",
                embed=None,
                view=None,
            )
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ Erreur : {e}", embed=None, view=None
            )


bot.run(TOKEN)
