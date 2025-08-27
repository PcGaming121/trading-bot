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
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

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
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("trades", self.trades_command))
        self.application.add_handler(CommandHandler("pnl", self.pnl_command))
        self.application.add_handler(CommandHandler("report", self.report_command))
        
        await self.application.initialize()
        await self.application.start()
        
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    async def send_daily_report(self, chat_id: str = None):
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
        
        try:
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
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