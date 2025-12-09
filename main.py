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
    1279003722172862465
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
    "tiktok": "lf"
}

COUNTRIES = {
    "france": "78",
    "canada": "36"
}

# --- GESTION BASE DE DONNÉES (SQLite) ---
def init_db():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()
    # Table Utilisateurs
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (discord_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0)''')
    # Table Commandes
    # Table Commandes
    c.execute('''CREATE TABLE IF NOT EXISTS orders
                 (order_id TEXT PRIMARY KEY, discord_id INTEGER, 
                  phone TEXT, price REAL, status TEXT, created_at TEXT, service TEXT)''')
    
    # Migration : Ajout colonne service si elle existe pas (pour les anciennes DB)
    try:
        c.execute("ALTER TABLE orders ADD COLUMN service TEXT")
    except sqlite3.OperationalError:
        pass # La colonne existe déjà

    # Migration : Ajout colonne cost pour le calcul de bénéfice
    try:
        c.execute("ALTER TABLE orders ADD COLUMN cost REAL")
    except sqlite3.OperationalError:
        pass # La colonne existe déjà

    conn.commit()
    conn.close()

def get_balance(user_id):
    conn = sqlite3.connect('database.db')
    res = conn.execute("SELECT balance FROM users WHERE discord_id=?", (user_id,)).fetchone()
    conn.close()
    return res[0] if res else 0.0

def update_balance(user_id, amount):
    conn = sqlite3.connect('database.db')
    conn.execute("INSERT OR IGNORE INTO users (discord_id, balance) VALUES (?, 0)", (user_id,))
    conn.execute("UPDATE users SET balance = balance + ? WHERE discord_id=?", (amount, user_id))
    conn.commit()
    conn.close()

def is_number_used(phone, service):
    conn = sqlite3.connect('database.db')
    # On regarde si ce numéro a déjà une commande complétée ou en attente pour ce service
    res = conn.execute("SELECT 1 FROM orders WHERE phone=? AND service=?", (phone, service)).fetchone()
    conn.close()
    return res is not None

# --- CLIENT API SMS-ACTIVATE ---
class SMSClient:
    async def request(self, action, params={}):
        params['api_key'] = API_KEY
        params['action'] = action
        async with aiohttp.ClientSession() as session:
            async with session.get(BASE_URL, params=params) as resp:
                return await resp.text()

    async def buy_number(self, service, country):
        # Réponse attendue : ACCESS_NUMBER:$ID:$NUMBER
        text = await self.request('getNumber', {'service': service, 'country': country, 'freePrice': 0})
        if "ACCESS_NUMBER" in text:
            parts = text.split(':')
            return {"success": True, "id": parts[1], "phone": parts[2]}
        elif "NO_NUMBERS" in text:
            return {"success": False, "error": "Plus de stock pour ce pays."}
        elif "NO_BALANCE" in text:
            return {"success": False, "error": "Erreur interne (Fonds insuffisants chez le bot)."}
        else:
            return {"success": False, "error": text}

    async def get_price(self, service, country):
        try:
            # On retourne sur getPrices pour avoir le prix Réel (et pas moyen/stats)
            response = await self.request('getPrices', {'service': service, 'country': country, 'freePrice': 0})
            
            # Pas de print global pour éviter le spam, on affiche juste le résultat trouvé
            data = json.loads(response)
            
            country_str = str(country)
            
            if country_str in data and service in data[country_str]:
                cost = float(data[country_str][service]['cost'])
                count = data[country_str][service]['count']
                return cost
            
            print(f"DEBUG: Pas de prix trouvé pour {service} en pays {country}")
            return None
        except Exception as e:
            print(f"Erreur get_price: {e}")
            return None

    async def get_status(self, activation_id):
        # Réponse attendue : STATUS_OK:CODE ou STATUS_WAIT_CODE
        text = await self.request('getStatus', {'id': activation_id})
        return text

    async def cancel_order(self, activation_id):
        # On envoie le statut 8 (Annulation)
        # On attend la réponse pour savoir si ça a marché
        response = await self.request('setStatus', {'id': activation_id, 'status': '8'})
        print(f"DEBUG ANNULATION - ID {activation_id} : {response}") # Pour voir ce qui se passe dans ta console
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
    print(f"Bot connecté en tant que {bot.user}")

@tasks.loop(time=time(hour=10, minute=0))
async def daily_stats_task():
    limit_date = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT order_id, price, cost FROM orders WHERE status='COMPLETED' AND created_at >= ?", (limit_date,))
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return # Pas de commande, pas de stats

    total_sales = 0.0
    total_cost = 0.0
    
    for _, price, cost in rows:
        total_sales += price
        if cost:
            total_cost += (cost * 0.9)
            
    profit = total_sales - total_cost
    
    embed = discord.Embed(title="📅 Rapport Quotidien (24h)", color=0x3498db)
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
async def deposit(interaction: discord.Interaction, amount: float, user: discord.Member):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ Vous n'avez pas la permission d'utiliser cette commande.", ephemeral=True)

    update_balance(user.id, amount)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ADMIN LOG: {interaction.user} (ID: {interaction.user.id}) credited {amount}€ to {user} (ID: {user.id})")
    await interaction.response.send_message(f"✅ Compte de {user.mention} crédité de {amount}€. Nouveau solde : {get_balance(user.id):.2f}€", ephemeral=True)

@bot.tree.command(name="stats", description="Voir les bénéfices du jour (Admin uniquement)")
async def stats(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)

    today = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    
    # On récupère les commandes COMPLETED du jour pour le calcul précis
    # On filtre sur 'created_at' qui contient la date. LIKE '2023-12-08%'
    cursor.execute("SELECT price, cost FROM orders WHERE status='COMPLETED' AND created_at LIKE ?", (f"{today}%",))
    rows = cursor.fetchall()
    conn.close()
    
    total_sales = 0.0
    total_cost = 0.0
    
    for price, cost in rows:
        total_sales += price
        # Si le coût n'a pas été enregistré (vieilles commandes), on estime grossièrement ou on ignore
        if cost:
            # Le cost stocké est le prix brut API. Il faut le convertir en EUR pour la comparaison (x0.9 approx)
            total_cost += (cost * 0.9) 
            
    profit = total_sales - total_cost
    
    embed = discord.Embed(title=f"📊 Statistiques du {today}", color=0xffd700)
    embed.add_field(name="Ventes Totales", value=f"{total_sales:.2f}€", inline=True)
    embed.add_field(name="Coût Estimé (API)", value=f"{total_cost:.2f}€", inline=True)
    embed.add_field(name="Bénéfice Net", value=f"{profit:.2f}€", inline=False)
    embed.set_footer(text=f"{len(rows)} commandes terminées aujourd'hui.")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="balance", description="Voir mon solde")
async def balance(interaction: discord.Interaction):
    await interaction.response.send_message(f"💰 Votre solde : {get_balance(interaction.user.id):.2f}€", ephemeral=True)

@bot.tree.command(name="services", description="Voir les services et prix disponibles")
@app_commands.describe(country="Le pays pour lequel afficher les prix (défaut : France)")
@app_commands.choices(country=[
    app_commands.Choice(name="France (+33)", value="france"),
    app_commands.Choice(name="Canada (+1)", value="canada")
])
async def services(interaction: discord.Interaction, country: app_commands.Choice[str] = None):
    await interaction.response.defer(ephemeral=True)
    
    # Pays par défaut : France
    selected_country_code = "france"
    selected_country_name = "France (+33)"
    
    if country:
        selected_country_code = country.value
        selected_country_name = country.name

    embed = discord.Embed(title="📱 Services Disponibles", color=0x00ff00)
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
    
    for i, price_api in enumerate(results):
        name = service_list[i]
        
        if price_api:
            # Calcul du prix client
            # Ajustement API (+50%) puis Marge (+30%) puis Conversion USD->EUR (x0.9)
            adjusted_cost = price_api * 1.5
            margin_price = adjusted_cost * 1.30
            final_price = round(margin_price * 0.9, 2)
            description += f"**{name.capitalize()}** : ~{final_price}€\n"
        else:
            description += f"**{name.capitalize()}** : *Indisponible*\n"
            
    embed.description = description
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="buy", description="Acheter un numéro")
@app_commands.choices(service=[
    app_commands.Choice(name="WhatsApp", value="whatsapp"),
    app_commands.Choice(name="Telegram", value="telegram"),
    app_commands.Choice(name="Google", value="google"),
    app_commands.Choice(name="Amazon", value="amazon"),
    app_commands.Choice(name="Tinder", value="tinder"),
    app_commands.Choice(name="Microsoft", value="microsoft"),
    app_commands.Choice(name="Facebook", value="facebook"),
    app_commands.Choice(name="Instagram", value="instagram"),
    app_commands.Choice(name="TikTok", value="tiktok")
])
@app_commands.choices(pays=[
    app_commands.Choice(name="France (+33)", value="france"),
    app_commands.Choice(name="Canada (+1)", value="canada")
])
async def buy(interaction: discord.Interaction, service: app_commands.Choice[str], pays: app_commands.Choice[str]):
    user_id = interaction.user.id
    
    await interaction.response.defer(ephemeral=True)
    
    # 1. DÉFINITION DU PRIX (Récupération via API)
    srv_code = SERVICES[service.value]
    ctry_id = COUNTRIES[pays.value]
    
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BUY REQUEST: {interaction.user} (ID: {interaction.user.id}) requested {service.name} in {pays.name}")
    
    # On récupère le prix réel du service
    real_price = await sms_api.get_price(srv_code, ctry_id)
    
    if real_price is None:
         return await interaction.followup.send("❌ Impossible de récupérer le prix ou pas de stock.", ephemeral=True)
         
    # Calcul du prix de vente
    # Ajustement API (+50%) puis Marge (+30%) puis Conversion USD->EUR (x0.9)
    adjusted_cost = real_price * 1.5
    margin_price = adjusted_cost * 1.30
    prive_vente = round(margin_price * 0.9, 2)
    
    # 4. AFFICHAGE DE LA CONFIRMATION
    # On passe real_price à la vue pour qu'elle puisse l'enregistrer dans la DB
    view = ConfirmBuyView(srv_code, ctry_id, prive_vente, user_id, service.name, pays.name, real_price)
    
    await interaction.followup.send(
        f"🔎 **Proposition d'achat**\n"
        f"Service : {service.name} | Pays : {pays.name}\n"
        f"Prix : **{prive_vente}€**\n\n"
        f"Voulez-vous confirmer l'achat ?",
        view=view,
        ephemeral=True
    )

class ConfirmBuyView(discord.ui.View):
    def __init__(self, service_code, country_id, price, user_id, service_name, country_name, cost_price):
        super().__init__(timeout=60)
        self.service_code = service_code
        self.country_id = country_id
        self.price = price
        self.user_id = user_id
        self.service_name = service_name
        self.country_name = country_name
        self.cost_price = cost_price

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Ce n'est pas votre commande !", ephemeral=True)
            
        # Vérification Solde
        if get_balance(self.user_id) < self.price:
             return await interaction.response.send_message("❌ Solde insuffisant pour confirmer.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        # Désactiver les boutons
        self.clear_items()
        await interaction.edit_original_response(view=self)

        # 2. APPEL API (Achat réel) avec vérification de doublon
        max_retries = 5
        order = None
        
        for i in range(max_retries):
            # Achat du numéro
            temp_order = await sms_api.buy_number(self.service_code, self.country_id)
            
            if not temp_order['success']:
                return await interaction.followup.send(f"❌ Échec de l'achat : {temp_order['error']}", ephemeral=True)
            
            # Vérification si déjà utilisé
            if is_number_used(temp_order['phone'], self.service_name):
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] REJET DOUBLON: Numéro {temp_order['phone']} déjà utilisé pour {self.service_name}. Nouvel essai...")
                # On annule immédiatement ce mauvais numéro
                await sms_api.cancel_order(temp_order['id'])
                await asyncio.sleep(1) # Petite pause pour pas spam l'API
                continue # On réessaye
            else:
                # C'est un bon numéro !
                order = temp_order
                break
        
        if order is None:
             return await interaction.followup.send(f"❌ Impossible de trouver un numéro vierge après {max_retries} essais. Réessayez plus tard.", ephemeral=True)


        if not order['success']:
            return await interaction.followup.send(f"❌ Échec de l'achat : {order['error']}", ephemeral=True)

        # 3. DÉBIT ET SAUVEGARDE
        update_balance(self.user_id, -self.price)
        
        conn = sqlite3.connect('database.db')
        # On sauvegarde aussi le 'cost' (coût API brut) pour les stats
        conn.execute("INSERT INTO orders (order_id, discord_id, phone, price, status, created_at, service, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                     (order['id'], self.user_id, order['phone'], self.price, "PENDING", str(datetime.now()), self.service_name, self.cost_price))
        conn.commit()
        conn.close()

        # 4. ENVOI EN DM
        try:
            # On crée le DM si pas existant
            dm_channel = await interaction.user.create_dm()
            
            # 5. AFFICHAGE ET SUIVI (EN DM)
            view = OrderView(order['id'], self.price, self.user_id, dm_channel)
            new_balance = get_balance(self.user_id)
            
            await dm_channel.send(
                f"✅ **Commande confirmée pour {interaction.user.mention}**\n"
                f"**Numéro réservé :** `{order['phone']}`\n"
                f"Service : {self.service_name} | Pays : {self.country_name}\n"
                f"💰 Débité : {self.price}€\n"
                f"💳 **Solde restant : {new_balance:.2f}€**\n\n"
                f"📡 **En attente du SMS...**",
                view=view
            )
            
            await interaction.followup.send(f"✅ Commande validée ! Je vous ai envoyé les détails en MP.", ephemeral=True)

            # 6. DÉMARRAGE DU POLLING
            asyncio.create_task(check_sms_loop(order['id'], dm_channel, view, interaction))
            
        except discord.Forbidden:
             # Si l'utilisateur a bloqué les MPs
             return await interaction.followup.send(f"❌ Je ne peux pas vous envoyer de MP. Veuillez activer vos messages privés et réessayer.", ephemeral=True)
        except Exception as e:
             return await interaction.followup.send(f"❌ Erreur lors de l'envoi du MP : {e}", ephemeral=True)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Ce n'est pas votre commande !", ephemeral=True)
            
        self.clear_items()
        await interaction.response.edit_message(content="❌ Achat annulé.", view=self)


# --- TÂCHE DE FOND : VÉRIFICATION DU SMS ---
async def check_sms_loop(order_id, channel, view, original_interaction):
    attempts = 0
    received_codes = set() # Pour éviter de renvoyer le même code en boucle
     
    while attempts < 300: # 300 * 5s = 25 minutes max
        if view.is_cancelled or view.is_finished:
            break
            
        status_text = await sms_api.get_status(order_id)
        
        # CAS 1 : CODE REÇU
        if "STATUS_OK" in status_text:
            code = status_text.split(':')[1].strip()
            
            if code not in received_codes:
                received_codes.add(code)
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CODE RECU: {code}")
                
                # Mise à jour de la vue pour activer les boutons suite au code
                view.code_received = True
                
                # On active les boutons Finish et Retry, on désactive Cancel
                for child in view.children:
                    if child.custom_id == "btn_finish" or child.custom_id == "btn_retry":
                        child.disabled = False
                    if child.custom_id == "btn_cancel":
                        child.disabled = True
                
                # On envoie le code
                await channel.send(f"📩 **CODE REÇU :** `{code}`\n*Si ce code ne fonctionne pas, cliquez sur 'Demander un autre code'.*")
                
                # On met à jour le message original avec les boutons activés
                try:
                    await original_interaction.edit_original_response(view=view)
                except:
                    pass
                
                # On réinitialise le compteur pour laisser du temps si on veut un autre code
                attempts = 0 
                
            # On ne sort PAS de la boucle, on attend que l'utilisateur choisisse "Terminer" ou "Autre code"


        # CAS 2 : ANNULÉ PAR LE FOURNISSEUR
        elif "STATUS_CANCEL" in status_text:
            await refund_user_channel(view.user_id, view.price, order_id, channel)
            return

        attempts += 1
        await asyncio.sleep(5) # Pause de 5 secondes

    # Si on sort de la boucle sans code (Timeout) ou si fini
    if view.is_finished:
        # Commande validée par l'utilisateur
        conn = sqlite3.connect('database.db')
        conn.execute("UPDATE orders SET status='COMPLETED' WHERE order_id=?", (order_id,))
        conn.commit()
        conn.close()
        
    elif not view.is_cancelled and not view.is_finished:
        if not received_codes:
            # Timeout sans AUCUN code reçu -> On annule et rembourse
            await sms_api.cancel_order(order_id)
            await refund_user_channel(view.user_id, view.price, order_id, channel, reason="Temps écoulé")
        else:
            # Timeout MAIS on a eu des codes -> On considère "Terminé" (le client a oublié de valider)
            await sms_api.request('setStatus', {'id': order_id, 'status': '6'})
            conn = sqlite3.connect('database.db')
            conn.execute("UPDATE orders SET status='COMPLETED' WHERE order_id=?", (order_id,))
            conn.commit()
            conn.close()
            await channel.send("ℹ️ Temps écoulé. Commande validée automatiquement.")
        
async def refund_user_channel(user_id, amount, order_id, channel, reason="Annulation"):
    update_balance(user_id, amount)
    conn = sqlite3.connect('database.db')
    conn.execute("UPDATE orders SET status='REFUNDED' WHERE order_id=?", (order_id,))
    conn.commit()
    conn.close()
    await channel.send(f"info : Commande annulée ({reason}). Vous avez été remboursé de {amount}€.")

async def refund_user(user_id, amount, order_id, interaction, reason="Annulation"):
    update_balance(user_id, amount)
    conn = sqlite3.connect('database.db')
    conn.execute("UPDATE orders SET status='REFUNDED' WHERE order_id=?", (order_id,))
    conn.commit()
    conn.close()
    await interaction.followup.send(f"info : Commande annulée ({reason}). Vous avez été remboursé de {amount}€.", ephemeral=True)


# --- INTERFACE BOUTONS ---
class OrderView(discord.ui.View):
    def __init__(self, order_id, price, user_id, original_interaction):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.price = price
        self.user_id = user_id
        self.original_interaction = original_interaction
        self.is_cancelled = False
        self.is_finished = False
        self.code_received = False

    @discord.ui.button(label="Terminer (Validé)", style=discord.ButtonStyle.success, disabled=True, custom_id="btn_finish")
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Ce n'est pas votre commande !", ephemeral=True)
            
        await interaction.response.defer()
        # On valide la commande chez SMS-Activate (Status 6)
        await sms_api.request('setStatus', {'id': self.order_id, 'status': '6'})
        self.is_finished = True
        self.stop() # On arrête la vue
        await interaction.followup.send("✅ Commande terminée avec succès.", ephemeral=True)

    @discord.ui.button(label="Demander un autre code", style=discord.ButtonStyle.primary, disabled=True, custom_id="btn_retry")
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Ce n'est pas votre commande !", ephemeral=True)
        
        await interaction.response.defer()
        # On demande un autre code (Status 3)
        await sms_api.request('setStatus', {'id': self.order_id, 'status': '3'})
        await interaction.followup.send("🔄 Demande de nouveau code envoyée... Attendez le prochain SMS.", ephemeral=True)
        
    @discord.ui.button(label="Annuler & Rembourser", style=discord.ButtonStyle.danger, custom_id="btn_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Ce n'est pas votre commande !", ephemeral=True)
        
        if self.code_received:
             return await interaction.response.send_message("❌ Impossible d'annuler car un code a déjà été reçu. Utilisez 'Terminer' ou demandez un autre code.", ephemeral=True)

        # On désactive le bouton pour éviter le double-clic
        button.disabled = True
        await interaction.response.edit_message(view=self)
        
        # 1. On tente d'annuler chez SMS-Activate D'ABORD
        api_response = await sms_api.cancel_order(self.order_id)
        
        # 2. On vérifie la réponse du fournisseur
        if "ACCESS_CANCEL" in api_response or "ACCESS_ACTIVATION_CANCELED" in api_response:
            self.is_cancelled = True
            self.stop()
            # 3. C'est confirmé, on rembourse le client
            await refund_user(self.user_id, self.price, self.order_id, interaction, reason="Annulation utilisateur")
            
        else:
            # 4. ÉCHEC
            button.disabled = False
            self.is_cancelled = False 
            await interaction.followup.send(
                f"❌ Impossible d'annuler pour le moment (Réponse API: `{api_response}`). Réessayez.", 
                ephemeral=True
            )
            # On remet le bouton actif
            await interaction.edit_original_response(view=self)
            
bot.run(TOKEN)