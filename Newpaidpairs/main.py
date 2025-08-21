import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
import aiohttp
import asyncio
from datetime import datetime, timedelta

# Configuration Constants
# Ensure the correct environment variable is used
BOT_TOKEN = os.environ.get("NEWPAIDPAIRS")
CHANNEL_ID = '@dexylastpaid'
API_URL = 'https://api.dexscreener.com/token-profiles/latest/v1'  # API endpoint
ADMIN_USER_ID = 6004549750  # Replace with your actual Telegram user ID
FETCH_INTERVAL = 60  # Interval in seconds for fetching new coin data

# Initialize logging
logging.basicConfig(
    level=logging.INFO,  # Set to DEBUG for detailed logs
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global variable to store the last processed coin ID
last_coin_id = None


async def get_latest_coin(session: aiohttp.ClientSession):
    global last_coin_id
    logger.debug("Fetching the latest coin data...")
    try:
        async with session.get(API_URL, timeout=10) as response:  # Added timeout
            if response.status != 200:
                logger.error(f"Failed to fetch data, status code: {response.status}")
                raise aiohttp.ClientError(f"Status code: {response.status}")
            data = await response.json()
            logger.debug(f"Received data: {data}")

            tokens = []
            if isinstance(data, dict):
                tokens = data.get('tokens', [])
            elif isinstance(data, list):
                tokens = data
            else:
                logger.error("Unexpected data format received from API.")

            if not tokens:
                logger.info("No tokens data received")
                return None

            # Filter tokens that are on Solana
            solana_tokens = [
                token for token in tokens if token.get('chainId') == 'solana']

            if not solana_tokens:
                logger.info("No Solana tokens found in the latest data.")
                return None

            # Assuming the tokens are ordered by latest first
            # Get the first Solana token as the latest
            latest_coin = solana_tokens[0]

            current_coin_id = latest_coin.get('tokenAddress')
            if current_coin_id == last_coin_id:
                logger.debug("No new Solana coin detected.")
                return None

            last_coin_id = current_coin_id
            return latest_coin
    except asyncio.TimeoutError:
        logger.exception("API request timed out.")
        raise
    except Exception as e:
        logger.exception(f"Error fetching the latest coin data: {e}")
        raise

# newpaidpairs_bot.py


async def fetch_token_details(session: aiohttp.ClientSession, token_address: str) -> dict:
    url = f'https://api.dexscreener.com/latest/dex/tokens/{token_address}'
    try:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                pairs = data.get("pairs", [{}])[0]
                created_at = pairs.get('pairCreatedAt', 0)
                logger.debug(f"Fetched created_at: {created_at} for token {token_address}")

                # Ensure created_at is a number
                try:
                    created_at = int(created_at)
                except (TypeError, ValueError):
                    logger.error(f"Invalid created_at value: {created_at}")
                    created_at = 0  # Set to 0 or handle appropriately

                # Adjust the timestamp if necessary
                # Let's assume that created_at is always in milliseconds
                if created_at > 0:
                    created_at = created_at / 1000  # Convert to seconds
                else:
                    logger.warning(f"created_at is zero or negative for token {token_address}")

                # Extract volume data
                volume_data = pairs.get('volume', {})
                volume1h = volume_data.get('h1', 0)  # 1-hour volume

                # Format and remove trailing .00 if not needed
                if isinstance(volume1h, (int, float)):
                    volume1h_str = "{:,.2f}".format(volume1h).rstrip('0').rstrip('.') if '.' in "{:,.2f}".format(volume1h) else "{:,.2f}".format(volume1h)
                else:
                    volume1h_str = "0"

                return {
                    "url": pairs.get("url", ""),
                    "base_name": pairs.get("baseToken", {}).get("name", "Unknown"),
                    "token_address": pairs.get('baseToken', {}).get('address', 'Unknown'),
                    "base_symbol": pairs.get("baseToken", {}).get("symbol", "Unknown"),
                    "volume1h": volume1h_str,  # Corrected and formatted
                    "marketCap": pairs.get("fdv", 0),
                    "created_at": created_at,
                    "dexId": pairs.get('dexId', '')
                }
            else:
                logger.warning(f"Failed to fetch details for token address: {token_address}")
                return {}
    except Exception as e:
        logger.error(f"Error fetching token details for {token_address}: {e}")
        return {}


def get_time_ago(timestamp):
    """
    Converts a Unix timestamp to a human-readable 'time ago' string.
    """
    if not timestamp:
        return 'Unknown'
    from datetime import datetime
    try:
        logger.debug(f"Converting timestamp: {timestamp}")
        timestamp = float(timestamp)
        if timestamp > datetime.now().timestamp() + 10000:
            # Timestamp is unrealistically in the future, adjust or skip
            logger.warning(f"Timestamp {timestamp} is in the future.")
            return 'Just now'
        timestamp_dt = datetime.fromtimestamp(timestamp)
        time_difference = datetime.now() - timestamp_dt
        seconds = time_difference.total_seconds()
        if seconds < 0:
            # Future date, return 'Just now'
            return 'Just now'
        minutes = seconds / 60
        hours = minutes / 60
        days = hours / 24

        if days >= 1:
            return f"{int(days)} day(s) ago"
        elif hours >= 1:
            return f"{int(hours)} hour(s) ago"
        elif minutes >= 1:
            return f"{int(minutes)} minute(s) ago"
        else:
            return f"{int(seconds)} second(s) ago"
    except Exception as e:
        logger.error(f"Error in get_time_ago with timestamp {timestamp}: {e}")
        return 'Unknown'


async def send_latest_coin(bot, coin, token_details):
    logger.debug(f"Preparing to send coin data: {coin}")
    url = coin.get('url', 'No URL Provided')
    token_address = coin.get('tokenAddress', 'Unknown')

    # Extract additional token details
    token_name = token_details.get('base_name', 'Unknown')
    market_cap = token_details.get('marketCap', 0)
    market_cap_str = f"{market_cap:,}" if isinstance(market_cap, (int, float)) else market_cap
    volume1h = token_details.get('volume1h', '0')  # 1-hour volume
    boosts_active = token_details.get('boosts_active', 0)
    created_timestamp = token_details.get('created_at', 0)
    is_older_than_3_days = False
    if created_timestamp:
        created_dt = datetime.fromtimestamp(created_timestamp)
        is_older_than_3_days = datetime.now() - created_dt > timedelta(days=3)

    # Define the buttons without social links
    keyboard_buttons = [
        [
            InlineKeyboardButton("🐎 Trojan", url=f"https://t.me/odysseus_trojanbot?start=r-___p88arl-{token_address}"),
            InlineKeyboardButton("🐂 Bullx", url=f"https://bullx.io/terminal?chainId=1399811149&address={token_address}&r=SBUZGK2REY9")
        ],
        [
            InlineKeyboardButton("⚡ Photon", url=f"https://photon-sol.tinyastro.io/en/r/@dexyfun/{token_address}"),
            InlineKeyboardButton("🐶 Bonk", url=f"https://t.me/bonkbot_bot?start=ref_glnhq_ca_{token_address}")
        ]
    ]

    # Prepare social links as hyperlinks in the message text
    links = coin.get('links', [])  # Added line to define 'links'
    social_links_text = ''
    for link in links:
        link_type = link.get('type', '').lower()
        link_url = link.get('url')
        if link_type and link_url:
            if link_type == 'website':
                social_links_text += f"🌐 **Website:** [Visit]({link_url})\n"
            elif link_type == 'twitter':
                social_links_text += f"🐦 **Twitter:** [Follow]({link_url})\n"
            elif link_type == 'telegram':
                social_links_text += f"💬 **Telegram:** [Join]({link_url})\n"
            else:
                link_type_cap = link_type.capitalize()
                social_links_text += f"🔗 **{link_type_cap}:** [Link]({link_url})\n"

    # Format the message using Markdown for better readability
    message_text = (
        f"🔗 **Dexscreener:** [Visit Here]({url})\n"
        f"⏳ **Created:** {get_time_ago(created_timestamp)}\n"
        f"📈 **Market Cap:** ${market_cap_str}\n"
        f"🪣 **1 Hour Volume:** ${volume1h}\n"
        f"🔖 **Token Name:** {token_name}\n"
        f"🔥 **Active Boosts:** {boosts_active}\n"
        f"📌 **Token Address:** `{token_address}`\n\n"
        f"{social_links_text}"  # Appended social_links_text to the message
    )
    logger.info(f"Sending message to channel {CHANNEL_ID}: {message_text}")

    if is_older_than_3_days:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard_buttons),
            parse_mode='Markdown'
        )
    else:
        logger.info("Token did not meet criteria: Paid and >3 days old.")


async def fetch_and_send_latest_coin(context: ContextTypes.DEFAULT_TYPE):
    logger.debug("Scheduled job: Fetching latest coin data...")
    try:
        async with aiohttp.ClientSession() as session:
            latest_coin = await get_latest_coin(session)
            if latest_coin:
                logger.info(f"New Solana coin detected: {latest_coin}")
                # Fetch additional token details
                token_address = latest_coin.get('tokenAddress', 'Unknown')
                token_details = await fetch_token_details(session, token_address)
                await send_latest_coin(context.bot, latest_coin, token_details)
            else:
                logger.debug("No new Solana coin to send.")
    except asyncio.TimeoutError:
        logger.error("API request timed out. Notifying admin.")
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text="⚠️ **Alert:** The API request timed out. Please check the API status."
        )
    except Exception as e:
        logger.error(f"An error occurred: {e}. Notifying admin.")
        await context.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"⚠️ **Alert:** An error occurred while fetching the latest coin data: {e}"
        )


async def dexy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.info(f"/dexy command received from user_id: {user_id}")
    keyboard = [
        [InlineKeyboardButton("Open DEXY BOT!", url="https://t.me/dexydexpaid_bot")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Check, monitor, and get notified when DEX is paid!",
        reply_markup=reply_markup
    )


async def startup(application: Application):
    """Function to run on startup of the bot."""
    logger.info("Bot is starting up and scheduling the monitoring job.")
    # Schedule the fetch_and_send_latest_coin job
    application.job_queue.run_repeating(
        fetch_and_send_latest_coin,
        interval=FETCH_INTERVAL,
        first=0,
        name="fetch_and_send_latest_coin"
    )
    logger.info("Monitoring job scheduled successfully.")


def main():
    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).post_init(startup).build()

    # Register command handlers
    application.add_handler(CommandHandler('dexy', dexy))

    logger.info("Handlers added. Starting the bot...")

    # Run the bot until the user presses Ctrl-C
    application.run_polling()

if __name__ == '__main__':
    main()
