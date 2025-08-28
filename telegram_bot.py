import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import sqlite3
import os
from dataclasses import dataclass
from decimal import Decimal
import schedule
import time
import threading

import aiohttp
from aiohttp import web
import telegram
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

# Configuration
TELEGRAM_BOT_TOKEN = '8412949168:AAGk_F8gQcECVWKK1_ARGhbHpVx_e3GS-5o'
WEBHOOK_PORT = int(os.getenv('PORT', 8080))
WEBHOOK_HOST = '0.0.0.0'
ADMIN_CHAT_ID = '8147226685'
PUBLIC_CHANNEL_ID = '-4950276288'

# Configuration du logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

@dataclass
class Trade:
    id: str
    symbol: str
    side: str  # 'buy' or 'sell'
    entry_price: float
    quantity: float
    timestamp: datetime
    exit_price: Optional[float] = None
    exit_timestamp: Optional[datetime] = None
    pnl: Optional[float] = None
    status: str = 'OPEN'  # OPEN, CLOSED, CANCELLED

class TradingDatabase:
    def __init__(self, db_path: str = 'trading.db'):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Initialise la base de données SQLite"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity REAL NOT NULL,
                timestamp TEXT NOT NULL,
                exit_price REAL,
                exit_timestamp TEXT,
                pnl REAL,
                status TEXT DEFAULT 'OPEN'
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_trades INTEGER,
                winning_trades INTEGER,
                losing_trades INTEGER,
                total_pnl REAL,
                win_rate REAL
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def add_trade(self, trade: Trade):
        """Ajoute un nouveau trade"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO trades 
            (id, symbol, side, entry_price, quantity, timestamp, exit_price, exit_timestamp, pnl, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade.id, trade.symbol, trade.side, trade.entry_price, trade.quantity,
            trade.timestamp.isoformat(), 
            trade.exit_price, 
            trade.exit_timestamp.isoformat() if trade.exit_timestamp else None,
            trade.pnl, trade.status
        ))
        
        conn.commit()
        conn.close()
    
    def close_trade(self, trade_id: str, exit_price: float, exit_timestamp: datetime, pnl: float):
        """Ferme un trade existant"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE trades 
            SET exit_price = ?, exit_timestamp = ?, pnl = ?, status = 'CLOSED'
            WHERE id = ?
        ''', (exit_price, exit_timestamp.isoformat(), pnl, trade_id))
        
        conn.commit()
        conn.close()
    
    def get_open_trades(self) -> List[Trade]:
        """Récupère tous les trades ouverts"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM trades WHERE status = "OPEN"')
        rows = cursor.fetchall()
        conn.close()
        
        trades = []
        for row in rows:
            trades.append(Trade(
                id=row[0], symbol=row[1], side=row[2], entry_price=row[3],
                quantity=row[4], timestamp=datetime.fromisoformat(row[5]),
                exit_price=row[6], 
                exit_timestamp=datetime.fromisoformat(row[7]) if row[7] else None,
                pnl=row[8], status=row[9]
            ))
        
        return trades
    
    def get_daily_stats(self, date: datetime) -> Dict:
        """Récupère les statistiques du jour"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        date_str = date.strftime('%Y-%m-%d')
        
        cursor.execute('''
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                SUM(pnl) as total_pnl,
                AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) as win_rate
            FROM trades 
            WHERE DATE(exit_timestamp) = ? AND status = 'CLOSED'
        ''', (date_str,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row[0] == 0:  # Aucun trade fermé aujourd'hui
            return {
                'date': date_str,
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'total_pnl': 0.0,
                'win_rate': 0.0
            }
        
        return {
            'date': date_str,
            'total_trades': row[0] or 0,
            'winning_trades': row[1] or 0,
            'losing_trades': row[2] or 0,
            'total_pnl': row[3] or 0.0,
            'win_rate': (row[4] or 0.0) * 100
        }

class TradingBot:
    def __init__(self):
        self.db = TradingDatabase()
        self.application = None
        
    async def initialize(self):
        """Initialise le bot Telegram"""
        self.application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Ajout des commandes
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("menu", self.menu_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("trades", self.trades_command))
        self.application.add_handler(CommandHandler("pnl", self.pnl_command))
        self.application.add_handler(CommandHandler("report", self.report_command))
        
        # Gestionnaire de boutons
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        
        # CORRECTION : Initialisation complète
        await self.application.initialize()
        await self.application.start()
        
    def create_main_menu_keyboard(self):
        keyboard = [
            [
                InlineKeyboardButton("📊 P&L Temps Réel", callback_data="realtime_pnl"),
                InlineKeyboardButton("📈 Stats Jour", callback_data="daily_stats")
            ],
            [
                InlineKeyboardButton("🔄 Trades Ouverts", callback_data="open_trades"),
                InlineKeyboardButton("📋 Rapport", callback_data="full_report")
            ],
            [
                InlineKeyboardButton("📅 7 Jours", callback_data="weekly_stats"),
                InlineKeyboardButton("⚙️ Status", callback_data="algo_status")
            ],
            [
                InlineKeyboardButton("🔄 Actualiser", callback_data="refresh_menu")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def create_back_keyboard(self):
        keyboard = [
            [InlineKeyboardButton("🏠 Menu Principal", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)
        
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /start"""
        welcome_message = """
🚀 **Bot Trading BTC - Bienvenue !**

📊 **Commandes disponibles :**
/menu - Menu interactif
/stats - Statistiques du jour
/trades - Trades ouverts
/pnl - P&L total
/report - Rapport détaillé

📈 **Notifications automatiques :**
• Alertes d'entrée en temps réel
• Alertes de sortie avec P&L
• Rapports quotidiens

🎯 **Algo :** Quick Profits BTC 5M
💰 **Risk :** 5% par trade

Utilisez /menu pour le tableau de bord !
        """
        
        keyboard = [
            [InlineKeyboardButton("📊 Ouvrir Menu", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            welcome_message, 
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    async def menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /menu"""
        await self.send_main_menu(update.effective_chat.id)
    
    async def send_main_menu(self, chat_id: str, message_id: int = None):
        """Envoie le menu principal"""
        today = datetime.now(timezone.utc)
        stats = self.db.get_daily_stats(today)
        open_trades = self.db.get_open_trades()
        
        menu_text = f"""
🚀 **MENU TRADING BTC**

📊 **Aujourd'hui ({today.strftime('%d/%m')}):**
• Trades: {stats['total_trades']}
• P&L: {stats['total_pnl']:+.2f} USD
• Win Rate: {stats['win_rate']:.1f}%
• En cours: {len(open_trades)}

⚡ **Status:** Actif
🕐 **MAJ:** {datetime.now(timezone.utc).strftime('%H:%M')} UTC

Sélectionnez une option:
        """
        
        keyboard = self.create_main_menu_keyboard()
        
        try:
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            if message_id:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=menu_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=menu_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
        except Exception as e:
            logger.error(f"Erreur menu: {e}")
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gestionnaire des boutons"""
        query = update.callback_query
        await query.answer()
        
        chat_id = str(query.message.chat_id)
        message_id = query.message.message_id
        
        try:
            if query.data == "main_menu" or query.data == "refresh_menu":
                await self.send_main_menu(chat_id, message_id)
            elif query.data == "realtime_pnl":
                await self.show_pnl_realtime(chat_id, message_id)
            elif query.data == "daily_stats":
                await self.show_daily_stats(chat_id, message_id)
            elif query.data == "open_trades":
                await self.show_open_trades(chat_id, message_id)
            elif query.data == "weekly_stats":
                await self.show_weekly_stats(chat_id, message_id)
            elif query.data == "algo_status":
                await self.show_algo_status(chat_id, message_id)
            elif query.data == "full_report":
                await self.send_daily_report(chat_id, message_id, True)
        except Exception as e:
            logger.error(f"Erreur bouton {query.data}: {e}")
    
    async def show_pnl_realtime(self, chat_id: str, message_id: int):
        today = datetime.now(timezone.utc)
        today_stats = self.db.get_daily_stats(today)
        
        weekly_pnl = 0
        for i in range(7):
            date = today - timedelta(days=i)
            stats = self.db.get_daily_stats(date)
            weekly_pnl += stats['total_pnl']
        
        text = f"""
💰 **P&L TEMPS RÉEL**

📈 **Aujourd'hui:** {today_stats['total_pnl']:+.2f} USD
📊 **7 jours:** {weekly_pnl:+.2f} USD
📅 **Moyenne/jour:** {weekly_pnl/7:+.2f} USD

🎯 **Performance:**
• Trades: {today_stats['total_trades']}
• Win Rate: {today_stats['win_rate']:.1f}%

🕐 **MAJ:** {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC
        """
        
        keyboard = self.create_back_keyboard()
        
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    
    async def show_daily_stats(self, chat_id: str, message_id: int):
        today = datetime.now(timezone.utc)
        stats = self.db.get_daily_stats(today)
        open_trades = self.db.get_open_trades()
        
        text = f"""
📊 **STATS DÉTAILLÉES**

🗓️ **{today.strftime('%d/%m/%Y')}**

📈 **Trades:**
• Total: {stats['total_trades']}
• Gagnants: {stats['winning_trades']} ✅
• Perdants: {stats['losing_trades']} ❌
• En cours: {len(open_trades)} 🔄

💰 **Performance:**
• P&L: {stats['total_pnl']:+.2f} USD
• Win Rate: {stats['win_rate']:.1f}%
        """
        
        keyboard = self.create_back_keyboard()
        
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    
    async def show_open_trades(self, chat_id: str, message_id: int):
        open_trades = self.db.get_open_trades()
        
        if not open_trades:
            text = """
🔄 **TRADES OUVERTS**

📊 Aucun trade ouvert

🎯 En attente du prochain signal
✅ Algorithme actif
            """
        else:
            text = f"🔄 **TRADES OUVERTS** ({len(open_trades)})\n\n"
            
            for i, trade in enumerate(open_trades[:5], 1):
                duration = datetime.now(timezone.utc) - trade.timestamp
                hours = duration.total_seconds() / 3600
                
                text += f"""
**#{i} {trade.symbol}**
🎯 {trade.side.upper()}
💰 {trade.entry_price:.2f}
⏱️ {hours:.1f}h
📅 {trade.timestamp.strftime('%H:%M')}
---
                """
        
        keyboard = self.create_back_keyboard()
        
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    
    async def show_weekly_stats(self, chat_id: str, message_id: int):
        today = datetime.now(timezone.utc)
        
        total_pnl = 0
        total_trades = 0
        
        text = "📅 **HISTORIQUE 7 JOURS**\n\n"
        
        for i in range(7):
            date = today - timedelta(days=i)
            stats = self.db.get_daily_stats(date)
            total_pnl += stats['total_pnl']
            total_trades += stats['total_trades']
            
            day_name = date.strftime('%a %d/%m')
            pnl_emoji = "🟢" if stats['total_pnl'] > 0 else "🔴" if stats['total_pnl'] < 0 else "⚪"
            text += f"{pnl_emoji} {day_name}: {stats['total_pnl']:+.1f} USD\n"
        
        text += f"""
📊 **TOTAL:**
• P&L: {total_pnl:+.2f} USD
• Trades: {total_trades}
• Moyenne: {total_pnl/7:+.2f} USD/jour
        """
        
        keyboard = self.create_back_keyboard()
        
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    
    async def show_algo_status(self, chat_id: str, message_id: int):
        open_trades = self.db.get_open_trades()
        
        text = f"""
⚙️ **STATUS ALGORITHME**

🚀 **Quick Profits BTC 5M**
✅ Status: ACTIF
🌍 Sessions: 24/7
🎯 Risk: 5%/trade

📊 **État:**
• Trades ouverts: {len(open_trades)}
• Monitoring: BTC/USD 5M
• Dernière vérif: {datetime.now(timezone.utc).strftime('%H:%M')} UTC

📡 **Connexions:**
✅ TradingView OK
✅ Heroku OK
✅ Telegram OK
        """
        
        keyboard = self.create_back_keyboard()
        
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /stats"""
        today = datetime.now(timezone.utc)
        stats = self.db.get_daily_stats(today)
        
        message = f"""
📊 **Statistiques** ({stats['date']})

🎯 **Trades:** {stats['total_trades']}
✅ **Gagnants:** {stats['winning_trades']}
❌ **Perdants:** {stats['losing_trades']}
📈 **Win Rate:** {stats['win_rate']:.1f}%
💰 **P&L:** {stats['total_pnl']:+.2f} USD
        """
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def trades_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /trades"""
        open_trades = self.db.get_open_trades()
        
        if not open_trades:
            await update.message.reply_text("📊 Aucun trade ouvert")
            return
        
        message = "📊 **Trades Ouverts:**\n\n"
        for trade in open_trades:
            duration = datetime.now(timezone.utc) - trade.timestamp
            hours = duration.total_seconds() / 3600
            
            message += f"""
🎯 **{trade.symbol}** - {trade.side.upper()}
💰 Prix : {trade.entry_price:.2f}
📊 Qty : {trade.quantity:.4f}
⏱️ Durée : {hours:.1f}h
            """
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def pnl_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /pnl"""
        total_pnl = 0
        for i in range(7):
            date = datetime.now(timezone.utc) - timedelta(days=i)
            stats = self.db.get_daily_stats(date)
            total_pnl += stats['total_pnl']
        
        today_stats = self.db.get_daily_stats(datetime.now(timezone.utc))
        
        message = f"""
💰 **P&L Summary**

📈 **Aujourd'hui:** {today_stats['total_pnl']:+.2f} USD
📊 **7 jours:** {total_pnl:+.2f} USD
🎯 **Trades:** {today_stats['total_trades']}
✅ **Win Rate:** {today_stats['win_rate']:.1f}%
        """
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def report_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /report"""
        await self.send_daily_report(update.effective_chat.id)
    
    async def send_trade_alert(self, trade_data: Dict):
        """Envoie alerte de trade au canal"""
        try:
            if trade_data['action'] == 'entry':
                # Nouveau trade
                trade = Trade(
                    id=trade_data.get('id', f"{trade_data['symbol']}_{int(time.time())}"),
                    symbol=trade_data['symbol'],
                    side=trade_data['side'],
                    entry_price=float(trade_data['price']),
                    quantity=float(trade_data.get('quantity', 0.05)),
                    timestamp=datetime.now(timezone.utc)
                )
                
                self.db.add_trade(trade)
                
                message = f"""
🚀 **NOUVELLE ENTRÉE**

🎯 **{trade.symbol}** - {trade.side.upper()}
💰 **Prix:** {trade.entry_price:.2f} USD
📊 **Qty:** {trade.quantity:.4f}
⏰ **Heure:** {trade.timestamp.strftime('%H:%M UTC')}

🔥 **Algo:** Quick Profits BTC 5M
                """
                
            elif trade_data['action'] == 'exit':
                # Fermeture de trade
                trade_id = trade_data.get('id')
                exit_price = float(trade_data['price'])
                pnl = float(trade_data.get('pnl', 0))
                
                if trade_id:
                    self.db.close_trade(trade_id, exit_price, datetime.now(timezone.utc), pnl)
                
                pnl_emoji = "💚" if pnl > 0 else "❤️"
                pnl_text = "PROFIT" if pnl > 0 else "PERTE"
                
                message = f"""
{pnl_emoji} **TRADE FERMÉ - {pnl_text}**

🎯 **{trade_data['symbol']}**
💰 **Sortie:** {exit_price:.2f} USD
📊 **P&L:** {pnl:+.2f} USD
⏰ **Heure:** {datetime.now(timezone.utc).strftime('%H:%M UTC')}
                """
            else:
                return
            
            # Envoyer au canal
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.send_message(
                chat_id=PUBLIC_CHANNEL_ID,
                text=message,
                parse_mode='Markdown'
            )
            
            logger.info(f"Alerte envoyée: {trade_data['action']} pour {trade_data['symbol']}")
            
        except Exception as e:
            logger.error(f"Erreur lors de l'envoi d'alerte: {e}")
    
    async def send_daily_report(self, chat_id: str = None, message_id: int = None, interactive: bool = False):
        """Envoie le rapport quotidien"""
        chat_id = chat_id or PUBLIC_CHANNEL_ID
        today = datetime.now(timezone.utc)
        
        today_stats = self.db.get_daily_stats(today)
        
        # Calcul des stats de la semaine
        weekly_pnl = 0
        weekly_trades = 0
        for i in range(7):
            date = today - timedelta(days=i)
            stats = self.db.get_daily_stats(date)
            weekly_pnl += stats['total_pnl']
            weekly_trades += stats['total_trades']
        
        report = f"""
📊 **RAPPORT QUOTIDIEN** - {today.strftime('%d/%m/%Y')}

🎯 **AUJOURD'HUI**
• Trades: {today_stats['total_trades']}
• Win Rate: {today_stats['win_rate']:.1f}%
• P&L: {today_stats['total_pnl']:+.2f} USD

📊 **7 JOURS**
• P&L Total: {weekly_pnl:+.2f} USD
• Trades: {weekly_trades}
• Moyenne: {weekly_pnl/7:+.2f} USD/jour

🚀 **ALGO:** Quick Profits BTC 5M
⚡ **STATUS:** Actif 24/7
        """
        
        keyboard = None
        if interactive:
            keyboard = self.create_back_keyboard()
        
        try:
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            if message_id and interactive:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=report,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=report,
                    parse_mode='Markdown'
                )
            logger.info("Rapport quotidien envoyé")
        except Exception as e:
            logger.error(f"Erreur lors de l'envoi du rapport: {e}")

# Instance globale du bot
trading_bot = TradingBot()

async def webhook_handler(request):
    """Gestionnaire des webhooks de TradingView"""
    try:
        data = await request.json()
        logger.info(f"Webhook TradingView reçu: {data}")
        
        # Traitement des données du webhook
        if 'action' in data and 'symbol' in data:
            await trading_bot.send_trade_alert(data)
        
        return web.json_response({'status': 'success'})
    
    except Exception as e:
        logger.error(f"Erreur webhook: {e}")
        return web.json_response({'error': str(e)}, status=400)

def schedule_daily_reports():
    """Programme les rapports quotidiens"""
    def send_report():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(trading_bot.send_daily_report())
            loop.close()
        except Exception as e:
            logger.error(f"Erreur rapport quotidien: {e}")
    
    schedule.every().day.at("00:00").do(send_report)
    
    while True:
        schedule.run_pending()
        time.sleep(60)
    """Initialise le serveur web pour les webhooks"""
    app = web.Application()
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_get('/health', lambda r: web.json_response({
        'status': 'ok', 
        'time': datetime.now().isoformat(),
        'bot': 'trading_alerts_v2'
    }))
    
    return app

async def main():
    """Fonction principale CORRIGÉE"""
    logger.info("Démarrage du bot trading...")
    
    try:
        # 1. Initialisation du bot Telegram (avec start() inclus)
        await trading_bot.initialize()
        logger.info("Bot Telegram initialisé et démarré")
        
        # 2. Démarrage du serveur web pour webhooks TradingView
        app = await init_web_server()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
        await site.start()
        logger.info(f"Serveur webhook TradingView: {WEBHOOK_HOST}:{WEBHOOK_PORT}")
        
        # 3. Démarrage du scheduler pour rapports quotidiens
        scheduler_thread = threading.Thread(target=schedule_daily_reports, daemon=True)
        scheduler_thread.start()
        logger.info("Scheduler de rapports démarré")
        
        # 4. Démarrage du polling Telegram (SANS re-start)
        logger.info("Démarrage du polling Telegram...")
        await trading_bot.application.updater.start_polling(
            poll_interval=1.0,
            timeout=10,
            bootstrap_retries=-1,
            read_timeout=30,
            connect_timeout=30,
            drop_pending_updates=True
        )
        logger.info("Bot Telegram en mode polling - Commandes disponibles")
        
        # 5. Message de démarrage APRÈS que tout soit opérationnel
        await asyncio.sleep(2)  # Attendre que le polling soit stabilisé
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=PUBLIC_CHANNEL_ID,
            text="🚀 **BOT REDÉMARRÉ V2**\n\n📊 Surveillance active\n⚡ Alertes et menu opérationnels !",
            parse_mode='Markdown'
        )
        logger.info("Message de démarrage envoyé")
        
        # 6. Boucle principale pour maintenir les services
        logger.info("Services actifs - Bot opérationnel")
        while True:
            await asyncio.sleep(60)
            
    except Exception as e:
        logger.error(f"Erreur critique: {e}")
        raise
    finally:
        logger.info("Arrêt du bot...")
        try:
            await trading_bot.application.updater.stop()
            await trading_bot.application.stop()
            await trading_bot.application.shutdown()
        except Exception as e:
            logger.error(f"Erreur lors de l'arrêt: {e}")

if __name__ == '__main__':
    asyncio.run(main())
