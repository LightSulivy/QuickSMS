import asyncio
import os
import glob
import json
import sqlite3
import shutil
from datetime import datetime
from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv

# Charger la config
load_dotenv()

# Dossiers
SESSIONS_DIR = "sessions"
PROCESSED_DIR = "sessions/processed"
DB_PATH = "database.db"

# Créer le dossier processed si inexistant
if not os.path.exists(PROCESSED_DIR):
    os.makedirs(PROCESSED_DIR)

# Connexion DB
def add_to_db(phone, session_string, password, cost):
    conn = sqlite3.connect(DB_PATH)
    # Vérif doublon
    exists = conn.execute("SELECT 1 FROM telegram_accounts WHERE phone=?", (phone,)).fetchone()
    if exists:
        print(f"⚠️ {phone} déjà en base. Skipped.")
        conn.close()
        return False
    
    conn.execute(
        "INSERT INTO telegram_accounts (phone, session_string, password_2fa, price_cost, origin, added_at, status) VALUES (?, ?, ?, ?, ?, ?, 'AVAILABLE')",
        (phone, session_string, password, cost, "IMPORT_SCRIPT", str(datetime.now())),
    )
    conn.commit()
    conn.close()
    return True

async def process_file(file_path, cost):
    filename = os.path.basename(file_path)
    phone_raw = os.path.splitext(filename)[0] # +12345
    
    json_path = file_path.replace(".session", ".json")
    
    # Lecture JSON pour infos
    api_id = 2040 # Default backup
    api_hash = "b18441a1ff607e10a989891a5462e627"
    password = None
    
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get("app_id"): api_id = data["app_id"]
                if data.get("app_hash"): api_hash = data["app_hash"]
                password = data.get("twoFA") # Peut être null
        except Exception as e:
            print(f"⚠️ Erreur lecture JSON pour {phone_raw}: {e}")

    # Connexion Telethon en utilisant le fichier .session
    # Note: On doit passer le chemin SANS l'extension .session à TelegramClient
    session_path_root = os.path.join(SESSIONS_DIR, phone_raw)
    
    print(f"🔄 Traitement de {phone_raw}...")
    
    client = TelegramClient(session_path_root, api_id, api_hash)
    
    try:
        await client.connect()
        
        if not await client.is_user_authorized():
            print(f"❌ {phone_raw} : Session invalide ou déconnectée.")
            await client.disconnect()
            return False
            
        # Conversion en StringSession
        string_session = StringSession.save(client.session)
        
        # Ajout DB
        added = add_to_db(phone_raw, string_session, password, cost)
        
        if added:
            print(f"✅ {phone_raw} importé avec succès !")
        
        await client.disconnect()
        
        # Déplacement vers processed (pour ne pas réimporter)
        shutil.move(file_path, os.path.join(PROCESSED_DIR, filename))
        if os.path.exists(json_path):
            shutil.move(json_path, os.path.join(PROCESSED_DIR, os.path.basename(json_path)))
            
        return True
        
    except Exception as e:
        print(f"❌ Erreur critique sur {phone_raw}: {e}")
        try: await client.disconnect()
        except: pass
        return False

async def main():
    print("🚀 Démarrage de l'import des sessions...")
    
    # Liste tous les .session
    files = glob.glob(os.path.join(SESSIONS_DIR, "*.session"))
    print(f"📂 {len(files)} fichiers trouvés.")
    
    if not files:
        print("Fin du script (rien à faire).")
        return

    try:
        cost_input = input("💰 Entrez le coût d'achat par compte (en €) [ex: 1.5] : ")
        cost = float(cost_input)
    except ValueError:
        print("❌ Prix invalide. Utilisation de la valeur par défaut : 1.5€")
        cost = 1.5
    
    count = 0
    for f in files:
        # On ignore ceux qui sont déjà dans processed (meme si glob ne devrait pas les voir si pas recursif)
        if "processed" in f: continue
            
        success = await process_file(f, cost)
        if success: count += 1
        
        # Petite pause pour éviter de flood Telegram
        await asyncio.sleep(1)
        
    print(f"\n✨ Terminé ! {count} comptes importés dans la Base de Données.")

if __name__ == "__main__":
    asyncio.run(main())
