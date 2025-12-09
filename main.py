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
    1186309476626747422,
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
}

COUNTRIES = {"france": "78", "canada": "36"}


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

    conn.commit()
    conn.close()


def calculate_selling_price(api_price):
    if api_price is None:
        return None
    # Formule : ((API * 1.5) * 1.3) * 0.9
    return round(((api_price * 1.5) * 1.30) * 0.9, 2)


def calculate_selling_price(api_price):
    if api_price is None:
        return None
    # Formule : ((API * 1.5) * 1.3) * 0.9
    return round(((api_price * 1.5) * 1.30) * 0.9, 2)


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


# --- CLIENT API SMS-ACTIVATE ---
class SMSClient:
    async def request(self, action, params={}):
        params["api_key"] = API_KEY
        params["action"] = action
        async with aiohttp.ClientSession() as session:
            async with session.get(BASE_URL, params=params) as resp:
                return await resp.text()

    async def buy_number(self, service, country):
        # Réponse attendue : ACCESS_NUMBER:$ID:$NUMBER
        text = await self.request("getNumber", {"service": service, "country": country})
        print(f"DEBUG buy_number response: {text}")
        if "ACCESS_NUMBER" in text:
            parts = text.split(":")
            return {"success": True, "id": parts[1], "phone": parts[2]}
        elif "NO_NUMBERS" in text:
            return {"success": False, "error": "Plus de stock pour ce pays."}
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
                "getPrices", {"service": service, "country": country, "freePrice": 0}
            )

            # Pas de print global pour éviter le spam, on affiche juste le résultat trouvé
            data = json.loads(response)

            country_str = str(country)

            if country_str in data and service in data[country_str]:
                cost = float(data[country_str][service]["cost"])
                count = data[country_str][service]["count"]
                return cost

            print(f"DEBUG: Pas de prix trouvé pour {service} en pays {country}")
            return None
        except Exception as e:
            print(f"Erreur get_price: {e}")
            return None

    async def get_status(self, activation_id):
        # Réponse attendue : STATUS_OK:CODE ou STATUS_WAIT_CODE
        text = await self.request("getStatus", {"id": activation_id})
        return text

    async def cancel_order(self, activation_id):
        # On envoie le statut 8 (Annulation)
        # On attend la réponse pour savoir si ça a marché
        response = await self.request("setStatus", {"id": activation_id, "status": "8"})
        print(
            f"DEBUG ANNULATION - ID {activation_id} : {response}"
        )  # Pour voir ce qui se passe dans ta console
        return response


# --- LOGIQUE DU BOT ---
init_db()
bot = commands.Bot(command_prefix="!", intents=discord.Intents.all())
sms_api = SMSClient()


@bot.event
async def on_ready():
    await bot.tree.sync()
    if not daily_stats_task.is_running():
        daily_stats_task.start()

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
            total_cost += cost * 0.9

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
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message(
            "❌ Vous n'avez pas la permission d'utiliser cette commande.",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)

    update_balance(user.id, amount)
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ADMIN LOG: {interaction.user} (ID: {interaction.user.id}) credited {amount}€ to {user} (ID: {user.id})"
    )
    await interaction.followup.send(
        f"✅ Compte de {user.mention} crédité de {amount}€. Nouveau solde : {get_balance(user.id):.2f}€",
        ephemeral=True,
    )


@bot.tree.command(
    name="stats", description="Voir les bénéfices du jour (Admin uniquement)"
)
async def stats(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message(
            "❌ Accès refusé.", ephemeral=True
        )

    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()

    # On récupère les commandes COMPLETED du jour pour le calcul précis
    # On filtre sur 'created_at' qui contient la date. LIKE '2023-12-08%'
    cursor.execute(
        "SELECT price, cost FROM orders WHERE status='COMPLETED' AND created_at LIKE ?",
        (f"{today}%",),
    )
    rows = cursor.fetchall()
    conn.close()

    total_sales = 0.0
    total_cost = 0.0

    for price, cost in rows:
        total_sales += price
        # Si le coût n'a pas été enregistré (vieilles commandes), on estime grossièrement ou on ignore
        if cost:
            # Le cost stocké est le prix brut API. Il faut le convertir en EUR pour la comparaison (x0.9 approx)
            total_cost += cost * 0.9

    profit = total_sales - total_cost

    embed = discord.Embed(title=f"📊 Statistiques du {today}", color=0xFFD700)
    embed.add_field(name="Ventes Totales", value=f"{total_sales:.2f}€", inline=True)
    embed.add_field(name="Coût Estimé (API)", value=f"{total_cost:.2f}€", inline=True)
    embed.add_field(name="Bénéfice Net", value=f"{profit:.2f}€", inline=False)
    embed.set_footer(text=f"{len(rows)} commandes terminées aujourd'hui.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="balance", description="Voir mon solde")
async def balance(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"💰 Votre solde : {get_balance(interaction.user.id):.2f}€", ephemeral=True
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

            description += f"{emoji} **{svc_name}** : {final_price:.2f}€\n"
        else:
            description += f"🔴 **{svc_name}** : *Indisponible*\n"

    embed.description = description
    embed.set_footer(text="Prix sujets à variation (Offre/Demande)")
    await interaction.followup.send(embed=embed, ephemeral=True)


async def execute_pack_logic(interaction: discord.Interaction):
    # Pack WA (France) + TG (Canada)
    steps = [("telegram", "canada")]

    # 1. Calcul des prix pour le pack
    svc1_code = SERVICES["whatsapp"]
    ctry1_id = COUNTRIES["france"]

    svc2_code = SERVICES["telegram"]
    ctry2_id = COUNTRIES["canada"]

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
            description="Détail du Pack **Whatsapp (FR) + Telegram (CA)**",
            color=0xE91E63,
        )
        embed.add_field(
            name="1. Whatsapp (France)", value=f"{price1:.2f}€", inline=True
        )
        embed.add_field(
            name="2. Telegram (Canada)", value=f"{price2:.2f}€", inline=True
        )
        embed.add_field(
            name="💰 PRIX TOTAL", value=f"**{total_price:.2f}€**", inline=False
        )
        embed.set_footer(text="Le débit se fera étape par étape.")

        view = ConfirmPackView(interaction.user.id, price1, steps)
        await dm_channel.send(embed=embed, view=view)
        await interaction.followup.send(
            "📩 Confirmation envoyée en MP. Vérifiez vos messages !", ephemeral=True
        )

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
        await interaction.response.edit_message(content="❌ Pack annulé.", view=self)


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
        temp_order = await sms_api.buy_number(service_code, country_id)

        if temp_order["success"]:
            # Vérif doublons
            if is_number_used(temp_order["phone"], service_name):
                print(f"DOUBLON REJETÉ: {temp_order['phone']}")
                await sms_api.cancel_order(temp_order["id"])
                await asyncio.sleep(1)
                continue
            else:
                order = temp_order
                break
        else:
            # Si erreur NO_NUMBERS, on peut arrêter
            if "NO_NUMBERS" in str(temp_order.get("error", "")):
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

        msg = f"✅ **Commande validée !**\nService : **{service_name}** | Pays : **{country_name}**\nNuméro : `{order['phone']}`\n\nAttendez le code ci-dessous..."
        if next_steps:
            msg += f"\n\n🎁 **PACK EN COURS** : Prochaine étape -> {next_steps[0][0].capitalize()}"

        dm_message = await dm_channel.send(msg, view=view)
        # Message éphémère de confirmation
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
                    if (
                        child.custom_id == "btn_finish"
                        or child.custom_id == "btn_retry"
                    ):
                        child.disabled = False
                    if child.custom_id == "btn_cancel":
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
            await sms_api.cancel_order(order_id)
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

        # Gestion de la suite du Pack (si applicable)
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
        await interaction.edit_original_response(view=self)

        # 1. On tente d'annuler chez SMS-Activate D'ABORD
        api_response = await sms_api.cancel_order(self.order_id)

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
            await interaction.followup.send(error_message, ephemeral=True)
            # On remet le bouton actif
            await interaction.edit_original_response(view=self)

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
        await interaction.edit_original_response(view=self)

        # 1. Annulation chez SMS-Activate
        api_response = await sms_api.cancel_order(self.order_id)

        # 2. Vérification
        if (
            "ACCESS_CANCEL" in api_response
            or "ACCESS_ACTIVATION_CANCELED" in api_response
        ):
            self.is_cancelled = True
            self.stop()

            # 3. Remboursement
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
                f"🚫 **Numéro {self.phone} banni et signalé.** Vous avez été remboursé.\n🔄 **Recherche d'un nouveau numéro en cours...**",
                ephemeral=True,
            )

            # 5. Retry Logic (Relance immédiate)
            if self.service_key and self.country_key:
                await execute_buy_logic(
                    interaction,
                    self.service_key,
                    self.country_key,
                    next_steps=self.next_steps,
                )
        else:
            button.disabled = False
            self.is_cancelled = False
            error_message = f"❌ Impossible d'annuler pour le moment (Réponse API: `{api_response}`)."
            if "EARLY_CANCEL_DENIED" in api_response:
                error_message = "⏳ **Trop tôt !** Attendez 2 minutes."
            await interaction.followup.send(error_message, ephemeral=True)
            await interaction.edit_original_response(view=self)


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

    # Vérification du dernier message
    # On cherche si on a déjà posté le dashboard
    last_message = None
    async for message in channel.history(limit=5):
        if (
            message.author == bot.user
            and message.embeds
            and message.embeds[0].title == "🚀 QuickSMS Dashboard"
        ):
            last_message = message
            break

    view = DashboardView()
    embed = discord.Embed(
        title="🚀 QuickSMS Dashboard",
        description="Bienvenue ! Utilisez les boutons ci-dessous pour commander.",
        color=0x3498DB,
    )

    if last_message:
        # On ne fait rien si le dashboard est déjà là (ou on l'update si besoin)
        pass
    else:
        # On nettoie et on poste
        await channel.purge(limit=10)
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
            f"💰 **Votre solde : {balance:.2f}€**", ephemeral=True
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
            await interaction.response.send_message(
                f"📱 **Pays : {country_key.capitalize()}**. Choisissez le service :",
                view=ServiceSelectView(country_key),
                ephemeral=True,
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

                    description += f"{emoji} **{svc_name}** : {final_price:.2f}€\n"
                else:
                    description += f"🔴 **{svc_name}** : *Indisponible*\n"

            embed.description = description
            embed.set_footer(text="Prix sujets à variation (Offre/Demande)")
            await interaction.followup.send(embed=embed, ephemeral=True)


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
        # Déclenche l'achat
        await execute_buy_logic(interaction, service_key, self.country_key)


bot.run(TOKEN)
