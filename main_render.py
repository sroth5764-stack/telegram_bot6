import os
import io
import base64
import logging
import asyncio
import sqlite3
import threading
from flask import Flask
import requests
from dotenv import load_dotenv
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, 
    KeyboardButton,
    ReplyKeyboardRemove,
    BotCommand
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)

# Load environment variables
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0")) # Telegram Group / Channel ID សម្រាប់ Noti
KHPAY_API_KEY = os.getenv("KHPAY_API_KEY")
KHPAY_BASE_URL = os.getenv("KHPAY_BASE_URL", "https://khpay.site/api/v1")

# Link Group Telegram សម្រាប់មើលរបៀបប្រើប្រាស់
TUTORIAL_GROUP_URL = os.getenv("TUTORIAL_GROUP_URL", "https://t.me/+LFJigLE3GfJhMTc9")

# បញ្ជី Telegram User ID របស់ Admin
ADMIN_IDS = [int(admin_id.strip()) for admin_id in os.getenv("ADMIN_IDS", "0").split(",") if admin_id.strip().isdigit()]

# File Path សម្រាប់វីដេអូស្វាគមន៍
WELCOME_VIDEO_PATH = "welcome.mp4"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- DATABASE SETUP (SQLite) ---

def init_db():
    conn = sqlite3.connect("store.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            product_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price REAL NOT NULL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stock_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            secret_key TEXT NOT NULL UNIQUE,
            is_sold INTEGER DEFAULT 0,
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            transaction_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            key_given TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    default_products = [
        ("vpn_1h", "VPN AIM HACK (1 ម៉ោង)", 0.50),
        ("vpn_3h", "VPN AIM HACK (3 ម៉ោង)", 0.75),
        ("vpn_6h", "VPN AIM HACK (6 ម៉ោង)", 1.20),
        ("vpn_1d", "VPN AIM HACK (1 ថ្ងៃ)", 2.00),
        ("vpn_7d", "VPN AIM HACK (7 ថ្ងៃ)", 5.00),
        ("vpn_30d", "VPN AIM HACK (30 ថ្ងៃ)", 10.00),
        ("nova_1d", "INNOVA  (1 ថ្ងៃ)", 1.50),
        ("nova_7d", "INNOVA  (7 ថ្ងៃ)", 5.00),
        ("nova_30d", "INNOVA  (30 ថ្ងៃ)", 8.00),
    ]
    
    for pid, pname, pprice in default_products:
        cursor.execute("INSERT OR IGNORE INTO products (product_id, name, price) VALUES (?, ?, ?)", (pid, pname, pprice))
    
    conn.commit()
    conn.close()

init_db()

# --- DATABASE HELPER FUNCTIONS ---

def get_db():
    conn = sqlite3.connect("store.db")
    conn.row_factory = sqlite3.Row
    return conn

def get_products():
    conn = get_db()
    # កែប្រែ Syntax ត្រង់នេះដើម្បីការពារ Compatibility issue លើ SQLite គ្រប់កំណែ
    products = conn.execute("""
        SELECT p.product_id, p.name, p.price,
               COALESCE(SUM(CASE WHEN k.is_sold = 0 THEN 1 ELSE 0 END), 0) as stock_count
        FROM products p
        LEFT JOIN stock_keys k ON p.product_id = k.product_id
        GROUP BY p.product_id
    """).fetchall()
    conn.close()
    return products

def add_stock_key(product_id: str, key_str: str) -> bool:
    try:
        conn = get_db()
        conn.execute("INSERT INTO stock_keys (product_id, secret_key) VALUES (?, ?)", (product_id, key_str.strip()))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False

def get_and_assign_key(product_id: str, transaction_id: str, user_id: int) -> str:
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, secret_key FROM stock_keys WHERE product_id = ? AND is_sold = 0 LIMIT 1", (product_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return None
    
    key_id, secret_key = row[0], row[1]
    cursor.execute("UPDATE stock_keys SET is_sold = 1 WHERE id = ?", (key_id,))
    cursor.execute("""
        UPDATE orders 
        SET status = 'paid', key_given = ?
        WHERE transaction_id = ?
    """, (secret_key, transaction_id))
    
    conn.commit()
    conn.close()
    return secret_key

# --- KHPAY API HELPERS ---

def generate_khpay_qr(amount: float, note: str, metadata: dict):
    headers = {
        "Authorization": f"Bearer {KHPAY_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "amount": f"{amount:.2f}",
        "currency": "USD",
        "note": note,
        "metadata": metadata
    }
    try:
        res = requests.post(f"{KHPAY_BASE_URL}/qr/generate", json=payload, headers=headers, timeout=10)
        data = res.json()
        if data.get("success"):
            return data.get("data")
        logger.error(f"KHPAY API Error: {data}")
        return None
    except Exception as e:
        logger.error(f"Generate QR Exception: {e}")
        return None

def check_khpay_status(transaction_id: str):
    headers = {"Authorization": f"Bearer {KHPAY_API_KEY}"}
    try:
        res = requests.get(f"{KHPAY_BASE_URL}/qr/check/{transaction_id}", headers=headers, timeout=10)
        data = res.json()
        if data.get("success"):
            return data.get("data")
        return None
    except Exception as e:
        logger.error(f"Check Status Exception: {e}")
        return None

# --- TELEGRAM BOT HANDLERS ---

def get_user_keyboard(user_id: int):
    """ បង្កើត Main Reply Keyboard ដើម """
    if user_id in ADMIN_IDS:
        return ReplyKeyboardMarkup([
            [KeyboardButton("BUY NOW", icon_custom_emoji_id="5792174177918134715"), KeyboardButton("MY KEYS", icon_custom_emoji_id="6181593283483933319")],
            [KeyboardButton("ADMIN PHANEL", icon_custom_emoji_id="6091656427787525125")]
        ], resize_keyboard=True)
    else:
        return ReplyKeyboardMarkup([
            [KeyboardButton("BUY NOW"), KeyboardButton("MY KEYS")]
        ], resize_keyboard=True)

def get_exit_add_key_keyboard():
    """ បង្កើត Keyboard ខាងក្រោមសម្រាប់ចាកចេញពីកន្លែង Add Key """
    return ReplyKeyboardMarkup([
        [KeyboardButton("ចាកចេញពីកន្លែង Add Key", icon_custom_emoji_id="6332130371085277395")]
    ], resize_keyboard=True)

async def set_bot_commands(application: Application):
    """ បង្កើត Menu Command ដូចបតគេ """
    commands = [
        BotCommand("start", "ចាប់ផ្តើមទិញទំនិញ / Start Store"),
        BotCommand("buy", "មើលមុខទំនិញ / View Store"),
    ]
    await application.bot.set_my_commands(commands)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_name = user.first_name if user.first_name else "អតិថិជន"
    
    welcome_text = (
        f"ជម្រាបសួរ {user_name}! 🙏\n"
        f"សូមស្វាគមន៍មកកាន់ROMA BOT\n\n"
        f"សូមចុចប៊ូតុង **🛒 BUY NOW** នៅខាងក្រោមដើម្បីជ្រើសរើសទិញ!"
    )
    
    reply_markup = get_user_keyboard(user.id)

    if os.path.exists(WELCOME_VIDEO_PATH):
        with open(WELCOME_VIDEO_PATH, 'rb') as video_file:
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=video_file,
                caption=welcome_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
    else:
        await update.message.reply_text(
            welcome_text, 
            parse_mode="Markdown", 
            reply_markup=reply_markup
        )

async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_categories(update.effective_chat.id, context)

async def show_main_categories(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("VIP AIM HACK", callback_data="cat_vpn", icon_custom_emoji_id="6158782244023443718")],
        [InlineKeyboardButton("INNOVA(ESIGN)", callback_data="cat_nova", icon_custom_emoji_id="6201740876285222838")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = "🛒 **សូមជ្រើសរើសប្រភេទMOD**"
    await context.bot.send_message(chat_id=chat_id, text=msg_text, parse_mode="Markdown", reply_markup=reply_markup)

async def exit_add_key_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ អនុគមន៍សម្រាប់ចាកចេញពី Add Key និងលុបសារទាំងអស់ """
    admin_state = context.user_data.get("admin_state", {})
    chat_id = update.effective_chat.id

    msg_ids_to_delete = admin_state.get("msg_ids_to_delete", [])
    
    if update.message:
        msg_ids_to_delete.append(update.message.message_id)

    for msg_id in msg_ids_to_delete:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

    context.user_data.pop("admin_state", None)

    await context.bot.send_message(
        chat_id=chat_id,
        text="✅ **បានចាកចេញពីកន្លែង Add Key **",
        parse_mode="Markdown",
        reply_markup=get_user_keyboard(update.effective_user.id)
    )
    
    await open_admin_panel(update, context)

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # 1. ពិនិត្យមើលថាតើ Admin កំពុងចុច "ចាកចេញពីកន្លែង Add Key" ឬ "❌ ចាកចេញពីកន្លែង Add Key" ឬទេ
    if ("ចាកចេញពីកន្លែង Add Key" in text) and user.id in ADMIN_IDS:
        await exit_add_key_mode(update, context)
        return

    # 2. ពិនិត្យមើលថាតើ Admin កំពុងស្ថិតក្នុង Mode បញ្ចូល Key (Add Key State) ដែរឬទេ
    admin_state = context.user_data.get("admin_state")
    if user.id in ADMIN_IDS and admin_state and admin_state.get("action") == "adding_key":
        product_id = admin_state.get("product_id")
        
        if "msg_ids_to_delete" not in admin_state:
            admin_state["msg_ids_to_delete"] = []
        admin_state["msg_ids_to_delete"].append(update.message.message_id)

        # កែសម្រួល៖ គាំទ្រការ Paste ច្រើន Key ក្នុងពេលតែមួយ (បំបែកតាមបន្ទាត់)
        keys_list = [k.strip() for k in text.split("\n") if k.strip()]
        added_count = 0
        failed_count = 0

        for single_key in keys_list:
            if add_stock_key(product_id, single_key):
                added_count += 1
            else:
                failed_count += 1
        
        conn = get_db()
        prod = conn.execute("SELECT name FROM products WHERE product_id = ?", (product_id,)).fetchone()
        p_name = prod['name'] if prod else product_id
        conn.close()

        if added_count > 0:
            msg_content = f"✅ បានបញ្ចូល `{added_count}` Key ចូល `{p_name}` ជោគជ័យ!"
            if failed_count > 0:
                msg_content += f"\n⚠️ (`{failed_count}` Key បរាជ័យដោយសារស្ទួន)"
            msg_content += "\n👉 ទម្លាក់ Key បន្ទាប់ទៀតបាន ឬចុចប៊ូតុង **ចាកចេញពីកន្លែង Add Key** នៅខាងក្រោម។"
            
            reply_msg = await update.message.reply_text(msg_content, parse_mode="Markdown")
        else:
            reply_msg = await update.message.reply_text(
                f"❌ បរាជ័យ! Key ទាំងនេះមានរួចហើយក្នុងប្រព័ន្ធ។\n", 
                parse_mode="Markdown"
            )
            
        admin_state["msg_ids_to_delete"].append(reply_msg.message_id)
        return

    # 3. Reply Keyboard Actions ធម្មតា
    if text == "BUY NOW":
        await show_main_categories(chat_id, context)
    elif text == "MY KEYS":
        await show_my_keys(user.id, chat_id, context)
    elif text == "ADMIN PHANEL":
        if user.id in ADMIN_IDS:
            await open_admin_panel(update, context)

async def open_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    context.user_data.pop("admin_state", None)
    
    keyboard = [
        [InlineKeyboardButton("ADD KEYS", callback_data="adm_add_keys_menu", icon_custom_emoji_id="6287387643768477442")],
        [InlineKeyboardButton("KEYS IN STOCK", callback_data="adm_check_stock", icon_custom_emoji_id="5976712859449563800")],
        [InlineKeyboardButton("KEYS SOLD", callback_data="adm_check_sold", icon_custom_emoji_id="5444856076954520455")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg_text = "🛡️ **ផ្ទាំងគ្រប់គ្រងរដ្ឋបាល (Admin Control Panel)**\nសូមជ្រើសរើសមុខងារខាងក្រោម៖"
    
    if update.callback_query:
        await update.callback_query.message.edit_text(msg_text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg_text, parse_mode="Markdown", reply_markup=reply_markup)

async def show_my_keys(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    orders = conn.execute("""
        SELECT o.transaction_id, p.name, o.amount, o.key_given, o.created_at 
        FROM orders o
        JOIN products p ON o.product_id = p.product_id
        WHERE o.user_id = ? AND o.status = 'paid'
        ORDER BY o.created_at DESC
    """, (user_id,)).fetchall()
    conn.close()
    
    if not orders:
        await context.bot.send_message(chat_id=chat_id, text="🔑 អ្នកមិនទាន់មាន Key ដែលបានទិញនៅក្នុងប្រព័ន្ធនៅឡើយទេ។")
        return
        
    msg = "🔑 **បញ្ជី Key ដែលអ្នកបានទិញរក្សាទុក៖**\n\n"
    for ord in orders:
        msg += (
            f"📦 **{ord['name']}**\n"
            f"🔑 Key: `{ord['key_given']}`\n"
            f"💵 តម្លៃ: ${ord['amount']:.2f}\n"
            f"📅 កាលបរិច្ឆេទ: {ord['created_at']}\n"
            f"-----------------------------\n"
        )
    
    keyboard = [[InlineKeyboardButton("មើលរបៀបប្រើប្រាស់", url=TUTORIAL_GROUP_URL)]]
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user = query.from_user
    chat_id = query.message.chat_id
    
    if data.startswith("adm_") and user.id not in ADMIN_IDS:
        return

    # --- ADMIN PANEL CALLBACKS ---
    if data == "adm_panel":
        await open_admin_panel(update, context)
        return

    elif data == "adm_check_stock":
        products = get_products()
        msg = "📊 **របាយការណ៍ស្តុក Key បច្ចុប្បន្ន៖**\n\n"
        for p in products:
            msg += f"• **{p['name']}** (`{p['product_id']}`): នៅសល់ `{p['stock_count']}` Key\n"
        
        keyboard = [[InlineKeyboardButton("BACK TO ADMIN", icon_custom_emoji_id="6091571112557157542", callback_data="adm_panel")]]
        await query.message.edit_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data == "adm_check_sold":
        conn = get_db()
        sold_orders = conn.execute("""
            SELECT o.transaction_id, p.name, o.amount, o.key_given, o.user_id, o.created_at
            FROM orders o
            JOIN products p ON o.product_id = p.product_id
            WHERE o.status = 'paid'
            ORDER BY o.created_at DESC LIMIT 15
        """).fetchall()
        conn.close()

        if not sold_orders:
            msg = "💰 មិនទាន់មាន Key ណាត្រូវបានលក់ចេញនៅឡើយទេ។"
        else:
            msg = "💰 **បញ្ជី Key ដែលបានលក់ចេញចុងក្រោយ (១៥ កុម្ម៉ង់ចុងក្រោយ)៖**\n\n"
            for ord in sold_orders:
                msg += (
                    f"📦 {ord['name']}\n"
                    f"👤 User ID: `{ord['user_id']}`\n"
                    f"🔑 Key: `{ord['key_given']}`\n"
                    f"📅 {ord['created_at']}\n"
                    f"----------------------\n"
                )
        keyboard = [[InlineKeyboardButton("BACK TO ADMIN", callback_data="adm_panel", icon_custom_emoji_id="6091571112557157542")]]
        await query.message.edit_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data == "adm_add_keys_menu":
        context.user_data.pop("admin_state", None)
        keyboard = [
            [InlineKeyboardButton("VIP AIM HACK", callback_data="adm_add_vpn", icon_custom_emoji_id="6158782244023443718")],
            [InlineKeyboardButton("INNOVA(ESIGN)", callback_data="adm_add_nova", icon_custom_emoji_id="6201740876285222838")],
            [InlineKeyboardButton("BACK TO ADMIN", callback_data="adm_panel", icon_custom_emoji_id="6091571112557157542")]
        ]
        await query.message.edit_text("➕ **សូមជ្រើសរើសប្រភេទសេវាកម្មដែលចង់ Add Key៖**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data == "adm_add_nova":
        keyboard = [
            [InlineKeyboardButton("INNOVA  (1 ថ្ងៃ)", callback_data="adm_add_prod_nova_1d", icon_custom_emoji_id="6201740876285222838")],
            [InlineKeyboardButton("INNOVA  (7 ថ្ងៃ)", callback_data="adm_add_prod_nova_7d", icon_custom_emoji_id="6201740876285222838")],
            [InlineKeyboardButton("INNOVA  (30 ថ្ងៃ)", callback_data="adm_add_prod_nova_30d", icon_custom_emoji_id="6201740876285222838")],
            [InlineKeyboardButton("BACK", callback_data="adm_add_keys_menu", icon_custom_emoji_id="6091571112557157542")]
        ]
        await query.message.edit_text(" **ជ្រើសរើសរយៈពេល INNOVA ៖**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data == "adm_add_vpn":
        keyboard = [
            [InlineKeyboardButton("VIP AIM HACK (1 ម៉ោង)", callback_data="adm_add_prod_vpn_1h", icon_custom_emoji_id="6158782244023443718")],
            [InlineKeyboardButton("VIP AIM HACK (3 ម៉ោង)", callback_data="adm_add_prod_vpn_3h", icon_custom_emoji_id="6158782244023443718")],
            [InlineKeyboardButton("VIP AIM HACK (6 ម៉ោង)", callback_data="adm_add_prod_vpn_6h", icon_custom_emoji_id="6158782244023443718")],
            [InlineKeyboardButton("VIP AIM HACK (1 ថ្ងៃ)", callback_data="adm_add_prod_vpn_1d", icon_custom_emoji_id="6158782244023443718")],
            [InlineKeyboardButton("VIP AIM HACK (7 ថ្ងៃ)", callback_data="adm_add_prod_vpn_7d", icon_custom_emoji_id="6158782244023443718")],
            [InlineKeyboardButton("VIP AIM HACK (30 ថ្ងៃ)", callback_data="adm_add_prod_vpn_30d", icon_custom_emoji_id="6158782244023443718")],
            [InlineKeyboardButton("BACK", callback_data="adm_add_keys_menu", icon_custom_emoji_id="6091571112557157542")]
        ]
        await query.message.edit_text(" **ជ្រើសរើសរយៈពេល VPN AIM HACK៖**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data.startswith("adm_add_prod_"):
        product_id = data.replace("adm_add_prod_", "")
        
        conn = get_db()
        prod = conn.execute("SELECT name FROM products WHERE product_id = ?", (product_id,)).fetchone()
        p_name = prod['name'] if prod else product_id
        conn.close()

        add_key_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"📥 **កំពុងស្ថិតក្នុង Add Key សម្រាប់៖** `{p_name}`\n\n"
                f"👉 ឥឡូវនេះ អ្នកអាច **ទម្លាក់ (Paste) Key ចូលក្នុង chat នេះបានភ្លាមៗ**។ ប្រព័ន្ធនឹងកត់ទុកអូតូ!\n"
            ),
            parse_mode="Markdown",
            reply_markup=get_exit_add_key_keyboard()
        )

        context.user_data["admin_state"] = {
            "action": "adding_key",
            "product_id": product_id,
            "msg_ids_to_delete": [add_key_msg.message_id]
        }
        return

    # --- USER NAVIGATION & SHOPPING MENU LOGIC ---
    if data == "main_menu":
        keyboard = [
            [InlineKeyboardButton("VIP AIM HACK", callback_data="cat_vpn", icon_custom_emoji_id="6158782244023443718")],
            [InlineKeyboardButton("INNOVA(ESIGN)", callback_data="cat_nova", icon_custom_emoji_id="6201740876285222838")]
        ]
        await query.message.edit_text("🛒 ** សូមជ្រើសរើសប្រភេទMOD៖**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data == "cat_vpn":
        products = get_products()
        keyboard = []
        for p in products:
            if p['product_id'].startswith("vpn_"):
                btn_text = f" {p['name']} | ${p['price']:.2f} (ស្តុក: {p['stock_count']})"
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"select_{p['product_id']}")])
        keyboard.append([InlineKeyboardButton("BACK", callback_data="main_menu", icon_custom_emoji_id="6091571112557157542")])
        await query.message.edit_text(" **សូមជ្រើសរើសកញ្ចប់ VPN៖**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data == "cat_nova":
        products = get_products()
        keyboard = []
        for p in products:
            # កែប្រែ៖ គាំទ្រទាំង Prefix nova_ និង capcut_
            if p['product_id'].startswith("nova_") or p['product_id'].startswith("capcut_"):
                btn_text = f" {p['name']} | ${p['price']:.2f} (ស្តុក: {p['stock_count']})"
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"select_{p['product_id']}")])
        keyboard.append([InlineKeyboardButton("BACK", callback_data="main_menu", icon_custom_emoji_id="6091571112557157542")])
        await query.message.edit_text("**សូមជ្រើសរើសកញ្ចប់ INNOVA (NO ESIGN)៖**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data.startswith("select_"):
        product_id = data.replace("select_", "")
        conn = get_db()
        product = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
        
        stock_count = conn.execute(
            "SELECT COUNT(*) FROM stock_keys WHERE product_id = ? AND is_sold = 0", (product_id,)
        ).fetchone()[0]
        conn.close()
        
        if stock_count == 0:
            await query.message.reply_text("❌ សុំទោស! ទំនិញនេះអស់ស្តុកបណ្តោះអាសន្នហើយ។")
            return

        back_target = "cat_vpn" if product_id.startswith("vpn_") else "cat_nova"
        keyboard = [
            [InlineKeyboardButton("💳 KHQR ABA", callback_data=f"pay_khqr_{product_id}")],
            [InlineKeyboardButton("BACK", callback_data=back_target, icon_custom_emoji_id="6091571112557157542")]
        ]
        text = (
            f"📦 **ទំនិញ:** {product['name']}\n"
            f"💵 **តម្លៃ:** `${product['price']:.2f}` USD\n\n"
            f"សូមជ្រើសរើសវិធីសាស្ត្រទូទាត់ប្រាក់៖"
        )
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    elif data.startswith("pay_khqr_"):
        product_id = data.replace("pay_khqr_", "")

        try:
            await query.message.delete()
        except Exception as e:
            logger.error(f"Error deleting pay button message: {e}")

        conn = get_db()
        product = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
        conn.close()

        status_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ កំពុងបង្កើត KHQR Code ទូទាត់ប្រាក់...")

        txn = generate_khpay_qr(
            amount=product['price'],
            note=f"Order {product['name']}",
            metadata={"user_id": str(user.id), "product_id": product_id}
        )

        if not txn:
            await status_msg.edit_text("❌ មានបញ្ហាក្នុងការបង្កើត QR Code! សូមព្យាយាមម្តងទៀត។")
            return

        txn_id = txn["transaction_id"]
        qr_image_base64 = txn.get("qr_image", "")
        payment_url = txn.get("payment_url", "")

        conn = get_db()
        conn.execute("""
            INSERT INTO orders (transaction_id, user_id, product_id, amount, status)
            VALUES (?, ?, ?, ?, 'pending')
        """, (txn_id, user.id, product_id, product['price']))
        conn.commit()
        conn.close()

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        except Exception as e:
            logger.error(f"Error deleting temp status msg: {e}")

        photo_bytes = None
        if qr_image_base64 and qr_image_base64.startswith("data:image"):
            base64_data = qr_image_base64.split(",")[1]
            photo_bytes = base64.b64decode(base64_data)

        caption = (
            f"📲 **ទូទាត់ប្រាក់តាម KHQR ABA**\n\n"
            f"📦 **ទំនិញ:** {product['name']}\n"
            f"💵 **តម្លៃ:** `${product['price']:.2f}` USD\n"
            f"⏳ **សុពលភាព QR:** 5 នាទី\n\n"
            f"👉 [ចុចទីនេះដើម្បីបង់តាម App ធនាគារ]({payment_url})\n\n"
            f"លោកអ្នកអាចស្កេន QR Code ឬចុច Link ខាងលើ។ បន្ទាប់ពីទូទាត់រួច ប្រព័ន្ធនឹងបោះ Key ជូនដោយស្វ័យប្រវត្តិ!"
        )

        qr_message = None
        if photo_bytes:
            qr_message = await context.bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(photo_bytes),
                caption=caption,
                parse_mode="Markdown"
            )
        else:
            qr_message = await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="Markdown")

        asyncio.create_task(
            monitor_payment(context, user.id, chat_id, txn_id, product_id, qr_message.message_id if qr_message else None)
        )

async def monitor_payment(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, txn_id: str, product_id: str, qr_msg_id: int):
    max_checks = 225
    for _ in range(max_checks):
        await asyncio.sleep(4)
        status_data = check_khpay_status(txn_id)
        
        if status_data:
            paid = status_data.get("paid", False)
            action = status_data.get("action", "")
            
            if paid or action == "approved":
                if qr_msg_id:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=qr_msg_id)
                    except Exception as e:
                        logger.error(f"Failed to delete QR msg: {e}")

                key = get_and_assign_key(product_id, txn_id, user_id)
                
                conn = get_db()
                product = conn.execute("SELECT name FROM products WHERE product_id = ?", (product_id,)).fetchone()
                p_name = product['name'] if product else product_id
                conn.close()
                
                if key:
                    success_msg = (
                        f"🎉 **ការទូទាត់ទទួលបានជោគជ័យ!**\n\n"
                        f"🆔 **Txn ID:** `{txn_id}`\n"
                        f"📦 **ទំនិញ:** {p_name}\n"
                        f"🔑 **License Key របស់អ្នក:**\n`{key}`\n\n"
                        f"សូមអរគុណសម្រាប់ការគាំទ្រ🙏"
                    )
                    
                    group_keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("មើលរបៀបប្រើប្រាស់", url=TUTORIAL_GROUP_URL)]
                    ])
                    
                    await context.bot.send_message(
                        chat_id=chat_id, 
                        text=success_msg, 
                        parse_mode="Markdown",
                        reply_markup=group_keyboard
                    )
                    
                    if ADMIN_CHAT_ID != 0:
                        admin_alert = (
                            f"🔔 **មានការបញ្ជាទិញថ្មីជោគជ័យ!**\n\n"
                            f"👤 **User ID:** `{user_id}`\n"
                            f"📦 **ទំនិញ:** {p_name}\n"
                            f"🔑 **Key លក់បាន:** `{key}`\n"
                            f"🆔 **Txn ID:** `{txn_id}`"
                        )
                        try:
                            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_alert, parse_mode="Markdown")
                        except Exception as e:
                            logger.error(f"Failed to send alert to group: {e}")
                else:
                    await context.bot.send_message(
                        chat_id=chat_id, 
                        text="⚠️ ការបង់ប្រាក់ទទួលបានជោគជ័យ ប៉ុន្តែប្រព័ន្ធជួបបញ្ហាស្តុក! សូមទាក់ទង Admin ជាបន្ទាន់।"
                    )
                return

            elif status_data.get("status") in ["expired", "failed"]:
                if qr_msg_id:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=qr_msg_id)
                    except Exception:
                        pass
                await context.bot.send_message(chat_id=chat_id, text="❌ ប្រតិបត្តិការទូទាត់ប្រាក់ត្រូវបានលុបចោល ឬ ផុតកំណត់!")
                return

    if qr_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=qr_msg_id)
            await context.bot.send_message(chat_id=chat_id, text="⏰ KHQR Code បានផុតកំណត់ (4នាទី) ហើយត្រូវបានលុបចោលដោយស្វ័យប្រវត្តិ។")
        except Exception as e:
            logger.error(f"Failed to delete expired QR msg: {e}")

# --- RENDER WEB SERVER / HEALTH CHECK ---
# Render Web Service needs an HTTP port to stay healthy.
web_app = Flask(__name__)

@web_app.get("/")
def health_check():
    return "Telegram Bot is running", 200

def start_web_server():
    port = int(os.getenv("PORT", "10000"))
    web_app.run(host="0.0.0.0", port=port, use_reloader=False)


# --- MAIN RUNNER ---

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN missing in .env file!")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(set_bot_commands).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("buy", buy_command))
    app.add_handler(CommandHandler("admin", open_admin_panel))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    # Start Render's HTTP health-check server in the background.
    threading.Thread(target=start_web_server, daemon=True).start()

    print("🤖 Telegram Reseller Bot is running smoothly...")
    app.run_polling()

if __name__ == "__main__":
    main()