import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
import sqlite3
import os
import time

import aiohttp
from aiohttp import web
import telegram
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

# Configuration - VOS DONNÉES
TELEGRAM_BOT_TOKEN = '8427601866:AAF-D_BiODOunTel5Xs-WwxDn2V14XsxvQ0'
WEBHOOK_PORT = int(os.getenv('PORT', 8080))
WEBHOOK_HOST = '0.0.0.0'
PUBLIC_CHANNEL_ID = '-1003034510195'
ADMIN_CHAT_ID = '8147226685'

# Timezone UTC+2
TIMEZONE = timezone(timedelta(hours=2))

# Configuration du logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Base de données simple
def init_database():
    conn = sqlite3.connect('trading.db')
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
    
    conn.commit()
    conn.close()
    logger.info("Base de données initialisée")

def add_trade(trade_id, symbol, side, price, quantity):
    conn = sqlite3.connect('trading.db')
    cursor = conn.cursor()
    
    timestamp = datetime.now(TIMEZONE).isoformat()
    
    cursor.execute('''
        INSERT OR REPLACE INTO trades (id, symbol, side, entry_price, quantity, timestamp, status)
        VALUES (?, ?, ?, ?, ?, ?, 'OPEN')
    ''', (trade_id, symbol, side, price, quantity, timestamp))
    
    conn.commit()
    conn.close()

def close_trade(trade_id, exit_price, pnl):
    conn = sqlite3.connect('trading.db')
    cursor = conn.cursor()
    
    exit_timestamp = datetime.now(TIMEZONE).isoformat()
    
    cursor.execute('''
        UPDATE trades 
        SET exit_price = ?, exit_timestamp = ?, pnl = ?, status = 'CLOSED'
        WHERE id = ?
    ''', (exit_price, exit_timestamp, pnl, trade_id))
    
    conn.commit()
    conn.close()

def get_daily_stats(date):
    conn = sqlite3.connect('trading.db')
    cursor = conn.cursor()
    
    date_str = date.strftime('%Y-%m-%d')
    
    cursor.execute('''
        SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
            SUM(pnl) as total_pnl,
            COUNT(CASE WHEN status = 'OPEN' THEN 1 END) as open_trades
        FROM trades 
        WHERE DATE(exit_timestamp) = ? AND status = 'CLOSED'
    ''', (date_str,))
    
    row = cursor.fetchone()
    
    # Trades ouverts
    cursor.execute('SELECT COUNT(*) FROM trades WHERE status = "OPEN"')
    open_trades = cursor.fetchone()[0]
    
    conn.close()
    
    if not row or row[0] == 0:
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_pnl': 0.0,
            'win_rate': 0.0,
            'open_trades': open_trades
        }
    
    win_rate = (row[1] / row[0]) * 100 if row[0] > 0 else 0.0
    
    return {
        'total_trades': row[0],
        'winning_trades': row[1] or 0,
        'losing_trades': row[2] or 0,
        'total_pnl': row[3] or 0.0,
        'win_rate': win_rate,
        'open_trades': open_trades
    }

# NOUVELLES FONCTIONS POUR L'API (ajoutées après get_daily_stats)
def get_recent_trades(limit=10):
    """Récupère les trades récents pour l'API"""
    conn = sqlite3.connect('trading.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT id, symbol, side, entry_price, quantity, timestamp, 
               exit_price, exit_timestamp, pnl, status
        FROM trades 
        ORDER BY timestamp DESC 
        LIMIT ?
    ''', (limit,))
    
    trades = []
    for row in cursor.fetchall():
        trade = {
            'id': row[0],
            'symbol': row[1],
            'side': row[2],
            'entry_price': row[3],
            'quantity': row[4],
            'timestamp': row[5],
            'exit_price': row[6],
            'exit_timestamp': row[7],
            'pnl': row[8],
            'status': row[9]
        }
        trades.append(trade)
    
    conn.close()
    return trades

def get_all_stats():
    """Récupère toutes les statistiques pour l'API"""
    conn = sqlite3.connect('trading.db')
    cursor = conn.cursor()
    
    # Stats globales
    cursor.execute('''
        SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
            SUM(pnl) as total_pnl
        FROM trades 
        WHERE status = 'CLOSED'
    ''')
    
    row = cursor.fetchone()
    
    # Trades ouverts
    cursor.execute('SELECT COUNT(*) FROM trades WHERE status = "OPEN"')
    open_trades = cursor.fetchone()[0]
    
    conn.close()
    
    if not row or row[0] == 0:
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_pnl': 0.0,
            'win_rate': 0.0,
            'open_trades': open_trades
        }
    
    win_rate = (row[1] / row[0]) * 100 if row[0] > 0 else 0.0
    
    return {
        'total_trades': row[0],
        'winning_trades': row[1] or 0,
        'losing_trades': row[2] or 0,
        'total_pnl': row[3] or 0.0,
        'win_rate': win_rate,
        'open_trades': open_trades
    }

def get_pnl_chart_data():
    """Récupère les données pour le graphique P&L"""
    conn = sqlite3.connect('trading.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT exit_timestamp, pnl 
        FROM trades 
        WHERE status = 'CLOSED' AND exit_timestamp IS NOT NULL
        ORDER BY exit_timestamp ASC
    ''')
    
    trades = cursor.fetchall()
    conn.close()
    
    if not trades:
        return []
    
    chart_data = []
    cumulative_pnl = 0
    
    for timestamp, pnl in trades:
        cumulative_pnl += pnl
        # Formater la date pour l'affichage
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            time_label = dt.strftime('%H:%M')
        except:
            time_label = timestamp[:5]  # Fallback
        
        chart_data.append({
            'time': time_label,
            'pnl': cumulative_pnl
        })
    
    return chart_data

# Instance globale de l'application
application = None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /start"""
    message = """
🚀 Bot Trading BTC - Alertes Automatiques

📊 Commandes disponibles :
/start - Ce message
/stats - Statistiques du jour
/rapport - Rapport détaillé

📈 Fonctionnalités automatiques :
• Alertes d'entrée/sortie en temps réel
• Rapport quotidien à 22:00 UTC+2

🎯 Algo : Quick Profits BTC 5M
⚡ Status : Actif
    """
    await update.message.reply_text(message)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /stats"""
    try:
        today = datetime.now(TIMEZONE).date()
        stats = get_daily_stats(today)
        
        message = f"""
📊 Statistiques du jour ({today.strftime('%d/%m/%Y')})

🎯 Trades fermés : {stats['total_trades']}
✅ Gagnants : {stats['winning_trades']}
❌ Perdants : {stats['losing_trades']}
📈 Win Rate : {stats['win_rate']:.1f}%
💰 P&L : {stats['total_pnl']:+.2f} USD
🔄 Trades ouverts : {stats['open_trades']}
        """
        
        await update.message.reply_text(message)
    except Exception as e:
        logger.error(f"Erreur commande stats: {e}")
        await update.message.reply_text("Erreur lors du calcul des statistiques")

async def rapport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /rapport"""
    try:
        await send_daily_report(update.effective_chat.id)
    except Exception as e:
        logger.error(f"Erreur commande rapport: {e}")
        await update.message.reply_text("Erreur lors de la génération du rapport")

async def send_trade_alert(trade_data):
    """Envoie une alerte de trade"""
    try:
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        
        if trade_data['action'] == 'entry':
            # Nouvelle entrée
            symbol = trade_data.get('symbol', 'BTCUSD')
            side = trade_data.get('side', 'buy')
            price = float(trade_data.get('price', 0))
            quantity = float(trade_data.get('quantity', 0.05))
            trade_id = trade_data.get('id', f"{symbol}_{int(time.time())}")
            
            # Sauvegarder en base
            add_trade(trade_id, symbol, side, price, quantity)
            
            # Message d'alerte
            time_str = datetime.now(TIMEZONE).strftime('%H:%M:%S')
            message = f"""
🚀 NOUVELLE ENTRÉE

🎯 {symbol} - {side.upper()}
💰 Prix : {price:.2f} USD
📊 Quantité : {quantity:.4f}
⏰ Heure : {time_str} (UTC+2)

🔥 Algo : Quick Profits BTC 5M
            """
            
            await bot.send_message(chat_id=PUBLIC_CHANNEL_ID, text=message)
            logger.info(f"Alerte entrée envoyée: {symbol} {side} {price}")
            
        elif trade_data['action'] == 'exit':
            # Fermeture de trade
            symbol = trade_data.get('symbol', 'BTCUSD')
            exit_price = float(trade_data.get('price', 0))
            pnl = float(trade_data.get('pnl', 0))
            trade_id = trade_data.get('id', '')
            
            # Mettre à jour en base
            if trade_id:
                close_trade(trade_id, exit_price, pnl)
            
            # Message d'alerte
            time_str = datetime.now(TIMEZONE).strftime('%H:%M:%S')
            pnl_emoji = "💚" if pnl > 0 else "❤️"
            pnl_text = "PROFIT" if pnl > 0 else "PERTE"
            
            message = f"""
{pnl_emoji} TRADE FERMÉ - {pnl_text}

🎯 {symbol}
💰 Prix sortie : {exit_price:.2f} USD
📊 P&L : {pnl:+.2f} USD
⏰ Heure : {time_str} (UTC+2)
            """
            
            await bot.send_message(chat_id=PUBLIC_CHANNEL_ID, text=message)
            logger.info(f"Alerte sortie envoyée: {symbol} P&L={pnl}")
            
    except Exception as e:
        logger.error(f"Erreur alerte trade: {e}")

async def send_daily_report(chat_id=None):
    """Envoie le rapport quotidien"""
    try:
        chat_id = chat_id or PUBLIC_CHANNEL_ID
        today = datetime.now(TIMEZONE).date()
        yesterday = today - timedelta(days=1)
        
        # Stats d'aujourd'hui et hier
        today_stats = get_daily_stats(today)
        yesterday_stats = get_daily_stats(yesterday)
        
        # Stats de la semaine
        weekly_pnl = 0
        weekly_trades = 0
        for i in range(7):
            date = today - timedelta(days=i)
            stats = get_daily_stats(date)
            weekly_pnl += stats['total_pnl']
            weekly_trades += stats['total_trades']
        
        # Génération du rapport
        report = f"""
📊 RAPPORT QUOTIDIEN - {today.strftime('%d/%m/%Y')}

🎯 AUJOURD'HUI
• Trades fermés : {today_stats['total_trades']}
• Win Rate : {today_stats['win_rate']:.1f}%
• P&L : {today_stats['total_pnl']:+.2f} USD
• Trades ouverts : {today_stats['open_trades']}

📈 HIER
• P&L : {yesterday_stats['total_pnl']:+.2f} USD
• Trades : {yesterday_stats['total_trades']}

📊 7 DERNIERS JOURS
• P&L Total : {weekly_pnl:+.2f} USD
• Trades Total : {weekly_trades}
• P&L Moyen/jour : {weekly_pnl/7:+.2f} USD

🚀 ALGORITHME : Quick Profits BTC 5M
⚡ STATUS : Actif 24/7
🎯 RISK : 5% par trade

---
Prochain rapport : Demain 22:00 UTC+2
        """
        
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=chat_id, text=report)
        logger.info("Rapport quotidien envoyé")
        
    except Exception as e:
        logger.error(f"Erreur rapport quotidien: {e}")

async def scheduler_daily_reports():
    """Scheduler pour les rapports quotidiens à 22:00 UTC+2"""
    while True:
        try:
            now = datetime.now(TIMEZONE)
            
            # Vérifier si c'est 22:00
            if now.hour == 22 and now.minute == 0:
                await send_daily_report()
                # Attendre 61 secondes pour éviter les doublons
                await asyncio.sleep(61)
            else:
                # Vérifier chaque minute
                await asyncio.sleep(60)
                
        except Exception as e:
            logger.error(f"Erreur scheduler: {e}")
            await asyncio.sleep(60)

# NOUVEAUX HANDLERS API (ajoutés après scheduler_daily_reports)
async def api_stats_handler(request):
    """API endpoint pour les statistiques"""
    try:
        response = web.json_response(get_all_stats())
        # Ajouter CORS seulement pour cette route
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        logger.error(f"Erreur API stats: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def api_trades_handler(request):
    """API endpoint pour les trades récents"""
    try:
        limit = int(request.query.get('limit', 10))
        trades = get_recent_trades(limit)
        response = web.json_response({'trades': trades})
        # Ajouter CORS seulement pour cette route
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        logger.error(f"Erreur API trades: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def api_chart_handler(request):
    """API endpoint pour les données du graphique"""
    try:
        chart_data = get_pnl_chart_data()
        response = web.json_response({'chart_data': chart_data})
        # Ajouter CORS seulement pour cette route
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        logger.error(f"Erreur API chart: {e}")
        return web.json_response({'error': str(e)}, status=500)

# Webhook pour TradingView
async def webhook_handler(request):
    """Gestionnaire webhook TradingView"""
    try:
        data = await request.json()
        logger.info(f"Webhook TradingView reçu: {data}")
        
        # Vérifier que c'est un signal valide
        if 'action' in data and 'symbol' in data:
            await send_trade_alert(data)
        
        return web.json_response({'status': 'success'})
        
    except Exception as e:
        logger.error(f"Erreur webhook: {e}")
        return web.json_response({'error': str(e)}, status=400)

async def init_web_server():
    """Initialise le serveur web - VERSION SÉCURISÉE"""
    app = web.Application()
    
    # Routes existantes - INCHANGÉES
    app.router.add_post('/webhook', webhook_handler)
    app.router.add_get('/health', lambda r: web.json_response({
        'status': 'ok',
        'time': datetime.now(TIMEZONE).isoformat(),
        'version': 'simple_bot_v1.0'
    }))
    
    # NOUVELLES ROUTES API - ajoutées sans middleware global
    app.router.add_get('/api/stats', api_stats_handler)
    app.router.add_get('/api/trades', api_trades_handler)
    app.router.add_get('/api/chart', api_chart_handler)
    
    return app

async def main():
    """Fonction principale"""
    global application
    
    logger.info("Démarrage du bot trading simple...")
    
    try:
        # 1. Initialisation base de données
        init_database()
        
        # 2. Initialisation bot Telegram
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Ajout des commandes
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("stats", stats_command))
        application.add_handler(CommandHandler("rapport", rapport_command))
        
        await application.initialize()
        await application.start()
        
        # 3. Démarrage serveur webhook
        app = await init_web_server()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
        await site.start()
        
        logger.info(f"Serveur webhook démarré sur {WEBHOOK_HOST}:{WEBHOOK_PORT}")
        
        # 4. Démarrage polling Telegram
        await application.updater.start_polling(
            drop_pending_updates=True,
            poll_interval=2.0
        )
        logger.info("Polling Telegram démarré")
        
        # 5. Démarrage scheduler rapports quotidiens
        asyncio.create_task(scheduler_daily_reports())
        logger.info("Scheduler rapports quotidiens démarré (22:00 UTC+2)")
        
        # 6. Message de démarrage
        bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=PUBLIC_CHANNEL_ID,
            text=f"""
🚀 BOT TRADING REDÉMARRÉ

📊 Surveillance active
⚡ Alertes opérationnelles  
📋 Rapport quotidien à 22:00 UTC+2

Tapez /start pour les commandes
            """
        )
        logger.info("Message de démarrage envoyé")
        
        # 7. Boucle principale
        logger.info("Bot entièrement opérationnel")
        while True:
            await asyncio.sleep(60)
            
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        raise
        
    finally:
        if application:
            try:
                await application.updater.stop()
                await application.stop()
                await application.shutdown()
            except:
                pass

if __name__ == '__main__':
    asyncio.run(main())
