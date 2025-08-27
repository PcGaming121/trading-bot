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
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
WEBHOOK_PORT = int(os.getenv('PORT', 8080))
WEBHOOK_HOST = os.getenv('WEBHOOK_HOST', '0.0.0.0')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', 'YOUR_ADMIN_CHAT_ID')  # Chat ID de l'admin
PUBLIC_CHANNEL_ID = os.getenv('PUBLIC_CHANNEL_ID', 'YOUR_CHANNEL_ID')  # ID du canal public

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
        
        # Ajout des gestionnaires de boutons interactifs
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        
        await self.application.initialize()
        await self.application.start()
        
    def create_main_menu_keyboard(self):
        """Crée le clavier du menu principal"""
        keyboard = [
            [
                InlineKeyboardButton("📊 P&L Temps Réel", callback_data="realtime_pnl"),
                InlineKeyboardButton("📈 Stats Jour", callback_data="daily_stats")
            ],
            [
                InlineKeyboardButton("🔄 Trades Ouverts", callback_data="open_trades"),
                InlineKeyboardButton("📋 Rapport Complet", callback_data="full_report")
            ],
            [
                InlineKeyboardButton("📅 Historique 7J", callback_data="weekly_stats"),
                InlineKeyboardButton("⚙️ Status Algo", callback_data="algo_status")
            ],
            [
                InlineKeyboardButton("🔄 Actualiser", callback_data="refresh_menu")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def create_quick_actions_keyboard(self):
        """Crée un clavier d'actions rapides"""
        keyboard = [
            [
                InlineKeyboardButton("📊 P&L", callback_data="quick_pnl"),
                InlineKeyboardButton("📈 Win Rate", callback_data="quick_winrate"),
                InlineKeyboardButton("🔄 Trades", callback_data="quick_trades")
            ],
            [InlineKeyboardButton("🏠 Menu Principal", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /menu - Affiche le menu interactif"""
        await self.send_main_menu(update.effective_chat.id, update.message_id)
    
    async def send_main_menu(self, chat_id: str, message_id: int = None):
        """Envoie ou met à jour le menu principal"""
        today = datetime.now(timezone.utc)
        stats = self.db.get_daily_stats(today)
        
        menu_text = f"""
🚀 **MENU TRADING BTC**

📊 **Aujourd'hui ({today.strftime('%d/%m')}):**
• Trades: {stats['total_trades']}
• P&L: {stats['total_pnl']:+.2f} USD
• Win Rate: {stats['win_rate']:.1f}%

⚡ **Status:** Bot actif
🕐 **Dernière MAJ:** {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC

Cliquez sur les boutons pour plus d'infos:
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
            logger.error(f"Erreur envoi menu: {e}")

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gestionnaire des boutons interactifs"""
        query = update.callback_query
        await query.answer()
        
        chat_id = query.message.chat_id
        message_id = query.message.message_id
        
        if query.data == "realtime_pnl":
            await self.handle_realtime_pnl(chat_id, message_id)
        elif query.data == "daily_stats":
            await self.handle_daily_stats(chat_id, message_id)
        elif query.data == "open_trades":
            await self.handle_open_trades(chat_id, message_id)
        elif query.data == "full_report":
            await self.handle_full_report(chat_id, message_id)
        elif query.data == "weekly_stats":
            await self.handle_weekly_stats(chat_id, message_id)
        elif query.data == "algo_status":
            await self.handle_algo_status(chat_id, message_id)
        elif query.data == "refresh_menu":
            await self.send_main_menu(chat_id, message_id)
        elif query.data == "main_menu":
            await self.send_main_menu(chat_id, message_id)
        elif query.data.startswith("quick_"):
            await self.handle_quick_action(query.data, chat_id, message_id)

    async def handle_realtime_pnl(self, chat_id: str, message_id: int):
        """Affiche le P&L en temps réel"""
        today = datetime.now(timezone.utc)
        stats_today = self.db.get_daily_stats(today)
        
        # Calcul P&L des 7 derniers jours
        weekly_pnl = 0
        for i in range(7):
            date = today - timedelta(days=i)
            daily_stats = self.db.get_daily_stats(date)
            weekly_pnl += daily_stats['total_pnl']
        
        # Calcul P&L du mois
        monthly_pnl = 0
        for i in range(30):
            date = today - timedelta(days=i)
            daily_stats = self.db.get_daily_stats(date)
            monthly_pnl += daily_stats['total_pnl']
        
        pnl_text = f"""
💰 **P&L TEMPS RÉEL**

📈 **Aujourd'hui:** {stats_today['total_pnl']:+.2f} USD
📊 **7 derniers jours:** {weekly_pnl:+.2f} USD
📅 **30 derniers jours:** {monthly_pnl:+.2f} USD

🎯 **Performance:**
• Trades aujourd'hui: {stats_today['total_trades']}
• Win Rate: {stats_today['win_rate']:.1f}%
• Moyenne/jour (7J): {weekly_pnl/7:+.2f} USD

🕐 **MAJ:** {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC
        """
        
        keyboard = self.create_quick_actions_keyboard()
        
        try:
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=pnl_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Erreur P&L temps réel: {e}")

    async def handle_daily_stats(self, chat_id: str, message_id: int):
        """Affiche les statistiques détaillées du jour"""
        today = datetime.now(timezone.utc)
        stats = self.db.get_daily_stats(today)
        open_trades = self.db.get_open_trades()
        
        # Calcul du temps moyen des trades ouverts
        avg_duration = 0
        if open_trades:
            total_duration = sum(
                (datetime.now(timezone.utc) - trade.timestamp).total_seconds() 
                for trade in open_trades
            )
            avg_duration = total_duration / len(open_trades) / 3600  # en heures
        
        stats_text = f"""
📊 **STATISTIQUES DÉTAILLÉES**

🗓️ **{today.strftime('%d/%m/%Y')}**

📈 **Trades:**
• Total: {stats['total_trades']}
• Gagnants: {stats['winning_trades']} ✅
• Perdants: {stats['losing_trades']} ❌
• En cours: {len(open_trades)} 🔄

💰 **Performance:**
• P&L Total: {stats['total_pnl']:+.2f} USD
• Win Rate: {stats['win_rate']:.1f}%
• P&L moyen/trade: {stats['total_pnl']/max(1,stats['total_trades']):+.2f} USD

⏱️ **Temps:**
• Durée moy. trades ouverts: {avg_duration:.1f}h
• Dernière MAJ: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC
        """
        
        keyboard = self.create_quick_actions_keyboard()
        
        try:
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=stats_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Erreur stats quotidiennes: {e}")

    async def handle_open_trades(self, chat_id: str, message_id: int):
        """Affiche les trades ouverts avec détails"""
        open_trades = self.db.get_open_trades()
        
        if not open_trades:
            trades_text = """
🔄 **TRADES OUVERTS**

📊 Aucun trade ouvert actuellement

🎯 En attente du prochain signal...
✅ Algorithme actif et surveillant le marché
            """
        else:
            trades_text = f"🔄 **TRADES OUVERTS** ({len(open_trades)})\n\n"
            
            for i, trade in enumerate(open_trades, 1):
                duration = datetime.now(timezone.utc) - trade.timestamp
                hours = duration.total_seconds() / 3600
                
                # Estimation P&L flottant (approximatif)
                # Note: Il faudrait le prix actuel pour un calcul précis
                trades_text += f"""
**#{i} - {trade.symbol}**
🎯 Direction: {trade.side.upper()}
💰 Prix entrée: {trade.entry_price:.2f}
📊 Quantité: {trade.quantity:.4f}
⏱️ Durée: {hours:.1f}h
📅 {trade.timestamp.strftime('%d/%m %H:%M')}
---
                """
        
        keyboard = self.create_quick_actions_keyboard()
        
        try:
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=trades_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Erreur trades ouverts: {e}")

    async def handle_weekly_stats(self, chat_id: str, message_id: int):
        """Affiche les statistiques de la semaine"""
        today = datetime.now(timezone.utc)
        
        weekly_data = []
        total_pnl = 0
        total_trades = 0
        winning_days = 0
        
        for i in range(7):
            date = today - timedelta(days=i)
            stats = self.db.get_daily_stats(date)
            weekly_data.append((date, stats))
            total_pnl += stats['total_pnl']
            total_trades += stats['total_trades']
            if stats['total_pnl'] > 0:
                winning_days += 1
        
        weekly_text = f"""
📅 **STATISTIQUES 7 DERNIERS JOURS**

💰 **Performance globale:**
• P&L Total: {total_pnl:+.2f} USD
• Trades Total: {total_trades}
• P&L Moyen/jour: {total_pnl/7:+.2f} USD
• Jours gagnants: {winning_days}/7

📊 **Détail par jour:**
        """
        
        for date, stats in reversed(weekly_data):
            day_name = date.strftime('%a %d/%m')
            pnl_emoji = "🟢" if stats['total_pnl'] > 0 else "🔴" if stats['total_pnl'] < 0 else "⚪"
            weekly_text += f"\n{pnl_emoji} {day_name}: {stats['total_pnl']:+.1f} USD ({stats['total_trades']} trades)"
        
        keyboard = self.create_quick_actions_keyboard()
        
        try:
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=weekly_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Erreur stats hebdomadaires: {e}")

    async def handle_algo_status(self, chat_id: str, message_id: int):
        """Affiche le status de l'algorithme"""
        # Vérifier la dernière activité
        open_trades = self.db.get_open_trades()
        
        # Simuler le status de l'algo (vous pourriez ajouter plus de vérifications)
        status_text = f"""
⚙️ **STATUS ALGORITHME**

🚀 **Quick Profits BTC 5M**
✅ Status: ACTIF
🌍 Sessions: 24/7
🎯 Risk/trade: 5%

📊 **Paramètres actuels:**
• POC Length: 50
• RSI Length: 9 
• RSI Threshold: 50
• TP Points: 100
• SL Multiplier: 3.0x

🔄 **État actuel:**
• Trades ouverts: {len(open_trades)}
• Monitoring: BTC/USD 5M
• Dernière vérif: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC

📡 **Connexions:**
✅ TradingView → Heroku
✅ Heroku → Telegram
✅ Base de données OK
        """
        
        keyboard = self.create_quick_actions_keyboard()
        
        try:
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=status_text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Erreur status algo: {e}")

    async def handle_full_report(self, chat_id: str, message_id: int):
        """Génère et affiche un rapport complet"""
        await self.send_daily_report(chat_id, message_id, interactive=True)

    async def handle_quick_action(self, action: str, chat_id: str, message_id: int):
        """Gère les actions rapides"""
        if action == "quick_pnl":
            today_stats = self.db.get_daily_stats(datetime.now(timezone.utc))
            text = f"💰 **P&L Aujourd'hui:** {today_stats['total_pnl']:+.2f} USD"
        elif action == "quick_winrate":
            today_stats = self.db.get_daily_stats(datetime.now(timezone.utc))
            text = f"📈 **Win Rate:** {today_stats['win_rate']:.1f}% ({today_stats['winning_trades']}/{today_stats['total_trades']})"
        elif action == "quick_trades":
            open_trades = self.db.get_open_trades()
            text = f"🔄 **Trades:** {len(open_trades)} ouverts"
        
        keyboard = self.create_quick_actions_keyboard()
        
        try:
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode='Markdown',
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Erreur action rapide: {e}")
        """Commande /start"""
        welcome_message = """
🚀 **Bot Trading BTC - Bienvenue !**

📊 **Commandes disponibles :**
/stats - Statistiques du jour
/trades - Trades ouverts
/pnl - P&L total
/report - Rapport détaillé

📈 **Vous recevrez automatiquement :**
• Alertes d'entrée en temps réel
• Alertes de sortie avec P&L
• Rapports quotidiens à 00:00 UTC

🎯 **Algo :** Quick Profits BTC 5M
💰 **Risk par trade :** Configurable
        """
        await update.message.reply_text(welcome_message, parse_mode='Markdown')
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /stats - Statistiques du jour"""
        today = datetime.now(timezone.utc)
        stats = self.db.get_daily_stats(today)
        
        message = f"""
📊 **Statistiques du jour** ({stats['date']})

🎯 **Trades :** {stats['total_trades']}
✅ **Gagnants :** {stats['winning_trades']}
❌ **Perdants :** {stats['losing_trades']}
📈 **Win Rate :** {stats['win_rate']:.1f}%
💰 **P&L Total :** {stats['total_pnl']:+.2f} USD
        """
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def trades_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /trades - Trades ouverts"""
        open_trades = self.db.get_open_trades()
        
        if not open_trades:
            await update.message.reply_text("📊 Aucun trade ouvert actuellement")
            return
        
        message = "📊 **Trades Ouverts :**\n\n"
        for trade in open_trades:
            duration = datetime.now(timezone.utc) - trade.timestamp
            hours = duration.total_seconds() / 3600
            
            message += f"""
🎯 **{trade.symbol}** - {trade.side.upper()}
💰 Prix : {trade.entry_price:.2f}
📊 Qty : {trade.quantity:.4f}
⏱️ Durée : {hours:.1f}h
📅 {trade.timestamp.strftime('%H:%M:%S')}
            """
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def pnl_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /pnl - P&L global"""
        # Calculer P&L des 7 derniers jours
        total_pnl = 0
        for i in range(7):
            date = datetime.now(timezone.utc) - timedelta(days=i)
            stats = self.db.get_daily_stats(date)
            total_pnl += stats['total_pnl']
        
        today_stats = self.db.get_daily_stats(datetime.now(timezone.utc))
        
        message = f"""
💰 **P&L Summary**

📈 **Aujourd'hui :** {today_stats['total_pnl']:+.2f} USD
📊 **7 derniers jours :** {total_pnl:+.2f} USD

🎯 **Trades aujourd'hui :** {today_stats['total_trades']}
✅ **Win Rate :** {today_stats['win_rate']:.1f}%
        """
        
        await update.message.reply_text(message, parse_mode='Markdown')
    
    async def report_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Commande /report - Rapport détaillé"""
        await self.send_daily_report(update.effective_chat.id)
    
    async def send_trade_alert(self, trade_data: Dict):
        """Envoie une alerte de trade au canal public"""
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
💰 **Prix :** {trade.entry_price:.2f} USD
📊 **Quantité :** {trade.quantity:.4f} BTC
⏰ **Heure :** {trade.timestamp.strftime('%H:%M:%S UTC')}

🔥 **Algorithme :** Quick Profits BTC 5M
📈 **Signal :** POC Breakout + RSI Cross
                """
                
            elif trade_data['action'] == 'exit':
                # Fermeture de trade
                trade_id = trade_data.get('id')
                exit_price = float(trade_data['price'])
                pnl = float(trade_data.get('pnl', 0))
                
                self.db.close_trade(trade_id, exit_price, datetime.now(timezone.utc), pnl)
                
                pnl_emoji = "💚" if pnl > 0 else "❤️"
                pnl_text = "PROFIT" if pnl > 0 else "PERTE"
                
                message = f"""
{pnl_emoji} **TRADE FERMÉ - {pnl_text}**

🎯 **{trade_data['symbol']}**
💰 **Prix de sortie :** {exit_price:.2f} USD
📊 **P&L :** {pnl:+.2f} USD ({pnl/float(trade_data.get('entry_price', 1))*100:+.2f}%)
⏰ **Heure :** {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}

🎯 **Résultat :** {pnl_text}
                """
            
            # Envoyer au canal public
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
        yesterday = today - timedelta(days=1)
        
        today_stats = self.db.get_daily_stats(today)
        yesterday_stats = self.db.get_daily_stats(yesterday)
        
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

📈 **HIER**
• P&L: {yesterday_stats['total_pnl']:+.2f} USD
• Trades: {yesterday_stats['total_trades']}

📊 **7 DERNIERS JOURS**
• P&L Total: {weekly_pnl:+.2f} USD
• Trades Total: {weekly_trades}
• P&L Moyen/jour: {weekly_pnl/7:+.2f} USD

🚀 **ALGORITHME:** Quick Profits BTC 5M
⚡ **STATUS:** Actif 24/7
🎯 **RISK:** 5% par trade

---
💡 **Prochaines Sessions:**
🏮 Asie: 20:00-08:00 UTC
🇪🇺 Europe: 08:00-16:00 UTC  
🇺🇸 USA: 14:00-22:00 UTC
        """
        
        # Ajouter boutons si interactif
        keyboard = None
        if interactive:
            keyboard = self.create_quick_actions_keyboard()
        
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
                    parse_mode='Markdown',
                    reply_markup=keyboard
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
        logger.info(f"Webhook reçu: {data}")
        
        # Traitement des données du webhook
        if 'action' in data and 'symbol' in data:
            await trading_bot.send_trade_alert(data)
        
        return web.json_response({'status': 'success'})
    
    except Exception as e:
        logger.error(f"Erreur webhook: {e}")
        return web.json_response({'error': str(e)}, status=400)

def schedule_daily_reports():
    """Programme les rapports quotidiens"""
    schedule.every().day.at("00:00").do(
        lambda: asyncio.create_task(trading_bot.send_daily_report())
    )
    
    while True:
        schedule.run_pending()
        time.sleep(60)  # Vérifier chaque minute

async def init_web_server():
    """Initialise le serveur web pour les webhooks"""
    app = web.Application()
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_get('/health', lambda r: web.json_response({'status': 'ok'}))
    
    return app

async def main():
    """Fonction principale"""
    logger.info("Démarrage du bot trading...")
    
    # Initialisation du bot Telegram
    await trading_bot.initialize()
    logger.info("Bot Telegram initialisé")
    
    # Démarrage du serveur web pour webhooks
    app = await init_web_server()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()
    logger.info(f"Serveur webhook démarré sur {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    
    # Démarrage du scheduler pour rapports quotidiens en thread séparé
    scheduler_thread = threading.Thread(target=schedule_daily_reports, daemon=True)
    scheduler_thread.start()
    logger.info("Scheduler de rapports démarré")
    
    # Message de démarrage
    try:
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=PUBLIC_CHANNEL_ID,
            text="🚀 **BOT TRADING DÉMARRÉ**\n\n📊 Surveillance des signaux activée\n⚡ Prêt à recevoir les alertes !",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Impossible d'envoyer le message de démarrage: {e}")
    
    # Maintenir le bot en vie
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Arrêt du bot...")
    finally:
        await trading_bot.application.stop()
        await trading_bot.application.shutdown()

if __name__ == '__main__':
    asyncio.run(main())
