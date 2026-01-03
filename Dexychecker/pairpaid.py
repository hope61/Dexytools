import os
import logging
import asyncio
import aiohttp
import aiomysql
import dotenv
from datetime import datetime, timedelta
from collections import defaultdict
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler
)

# Load environment variables from a .env file if present
dotenv.load_dotenv()

# Replace with your bot token (set as an environment variable)
BOT_TOKEN = os.environ.get('DEXYCHECKER_BOT_API')

if not BOT_TOKEN:
    raise ValueError("DEXYCHECKER_BOT_API is not set. Please set it as an environment variable.")
# Base URL of the API
BASE_URL = 'https://api.dexscreener.com/orders/v1/solana/'

# Base URL for clickable link
LINK_BASE_URL = 'https://t.me/odysseus_trojanbot?start=r-gorbachovqddbwu-'

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,  # Set to DEBUG for more detailed logs
    format='%(asctime)s - %(levelname)s - %(name)s - [%(funcName)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.StreamHandler()                 # Also log to console
    ]
)
logger = logging.getLogger(__name__)

# Store user-specific data
user_pair_statuses = defaultdict(dict)
user_monitoring_tasks = defaultdict(dict)
user_monitoring_preferences = defaultdict(lambda: True)  # Default to auto-monitoring ON
last_change_time = defaultdict(dict)
original_pair_addresses = defaultdict(dict)  # Store original pair addresses
user_request_counts = defaultdict(lambda: defaultdict(int))  # Store request counts per user per day

def debug_log(func):
    """
    Decorator to log function entry, exit, and exceptions.
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        func_name = func.__name__
        logger.debug(f"Entering function: {func_name} | args: {args} | kwargs: {kwargs}")
        try:
            result = await func(*args, **kwargs)
            logger.debug(f"Exiting function: {func_name} | Return: {result}")
            return result
        except Exception as e:
            logger.exception(f"Exception in function: {func_name} | Exception: {e}")
            raise e
    return wrapper

def is_valid_pair_address(pair_address):
    is_valid = 35 <= len(pair_address) <= 50
    logger.debug(f"Validating pair address '{pair_address}': {'Valid' if is_valid else 'Invalid'}")
    return is_valid

# Function to get the pair status via API
@debug_log
async def get_pair_status(session, pair_address):
    logger.info(f"Fetching status for Contract address: {pair_address}")
    try:
        async with session.get(f"{BASE_URL}{pair_address}") as response:
            logger.debug(f"API response status for {pair_address}: {response.status}")
            if response.status == 429:
                logger.error("API rate limit exceeded.")
                return "API RATE LIMIT EXCEEDED"
            if response.status != 200:
                logger.warning(f"API request for {pair_address} returned status {response.status}")
                return "NOT PAID"
            data = await response.json()
            logger.debug(f"API response data for {pair_address}: {data}")
            if not data or 'orders' not in data or not data['orders']:
                logger.info(f"No data or orders returned for Contract address {pair_address}")
                return "NOT PAID"
            
            # Extract status from the first order (assuming that's the relevant one)
            status_info = data['orders'][0]
            status = status_info.get("status", "NOT PAID").upper()
            
            # Change "APPROVED" to "PAID" as before
            if status.lower() == "approved":
                status = "PAID"
            
            logger.info(f"Status for Contract address {pair_address}: {status}")
            return status
    except Exception as e:
        logger.error(f"Error fetching status for Contract address {pair_address}: {e}", exc_info=True)
        return "FETCH FAILED"

# Function to get the statuses for multiple pair addresses via API
@debug_log
async def get_multiple_pair_statuses(session, pair_addresses):
    statuses = {}
    for pair_address in pair_addresses:
        status = await get_pair_status(session, pair_address)
        statuses[pair_address] = status
        logger.debug(f"Status for {pair_address}: {status}")
    return statuses

# Function to get the status emoji
def get_status_emoji(status):
    status_emojis = {
        "PENDING": "🟡",
        "PROCESSING": "🟡",
        "PAID": "🟢",
        "CANCELED": "🔴",
        "ON-HOLD": "🟠",
        "REJECTED": "⚫",
        "NOT PAID": "❌",
        "FETCH FAILED": "🚫",
        "API RATE LIMIT EXCEEDED": "🚫"
    }
    emoji = status_emojis.get(status, "❌")
    logger.debug(f"Emoji for status '{status}': {emoji}")
    return emoji

# Function to create an inline keyboard button
def create_inline_button(original_pair_address):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🐎 Trojan", url=f"{LINK_BASE_URL}{original_pair_address}"),
            InlineKeyboardButton("🐂 Bullx", url=f"https://bullx.io/terminal?chainId=1399811149&address={original_pair_address}&r=SBUZGK2REY9")
        ],
        [
            InlineKeyboardButton("⚡ Photon", url=f"https://photon-sol.tinyastro.io/en/r/@dexyfun/{original_pair_address}"),
            InlineKeyboardButton("🐶 Bonk", url=f"https://t.me/bonkbot_bot?start=ref_glnhq_ca_{original_pair_address}")
        ]
    ])
    logger.debug(f"Created inline keyboard for {original_pair_address}.")
    return keyboard

# Function to send messages with retry mechanism
@debug_log
async def send_message_with_retry(bot, chat_id, text, keyboard=None, retries=3, timeout=5, parse_mode='Markdown'):
    for attempt in range(1, retries + 1):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=parse_mode
            )
            logger.info(f"Message sent to chat {chat_id}: {text}")
            return
        except Exception as e:
            logger.error(f"Attempt {attempt} - Error sending message to chat {chat_id}: {e}", exc_info=True)
            if attempt < retries:
                logger.info(f"Retrying to send message to chat {chat_id} in {timeout} seconds...")
                await asyncio.sleep(timeout)
            else:
                logger.critical(f"Failed to send message to chat {chat_id} after {retries} attempts.")
                try:
                    await bot.send_message(chat_id=chat_id, text="⚠️ *Failed to send your message after multiple attempts.*", parse_mode='Markdown')
                except Exception as inner_e:
                    logger.exception(f"Failed to send fallback message to chat {chat_id}: {inner_e}")
    return

# Command handler for /start
@debug_log
async def start(update: Update, context: CallbackContext):
    user_id = update.effective_chat.id
    current_preference = "ON" if user_monitoring_preferences[user_id] else "OFF"
    logger.info(f"User {user_id} initiated /start command.")

    welcome_text = (
        "👋 *Welcome to Dexycheck Bot!*\n\n"
        "Monitor the status of your Contract addresses in real-time.\n\n"
        "📋 *Available Commands:*\n"
        "/start - Display this welcome message\n"
        "/list - Show your monitored Contract addresses\n"
        "/clear - Stop monitoring all Contract addresses\n"
        "/help - Get help on how to use the bot\n\n"
        "🔄 *Auto-Monitoring is currently:* *{}*\n"
        "You can toggle this setting using the buttons below."
    ).format(current_preference)

    keyboard = [
        [InlineKeyboardButton(f"Toggle Auto-Monitoring (Currently {current_preference})", callback_data='toggle_monitoring')],
        [InlineKeyboardButton("📋 List Contract Addresses", callback_data='list_pairs')],
        [InlineKeyboardButton("🛑 Clear Contract Addresses", callback_data='clear_pairs')],
        [InlineKeyboardButton("ℹ️ Help", callback_data='help')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=user_id,
        text=welcome_text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    logger.debug(f"Sent welcome message to user {user_id}.")

# Command handler for /help
@debug_log
async def help_command(update: Update, context: CallbackContext):
    help_text = (
        "ℹ️ *How to Use DEXY CHECK BOT:*\n\n"
        "*1️⃣ Monitor a Contract Address:*\n"
        "Send me a contract address to start monitoring its status.\n\n"
        "*2️⃣ Check Monitored Contract Addresses:*\n"
        "/list - View the contract addresses you're currently monitoring.\n\n"
        "*3️⃣ Stop Monitoring:*\n"
        "/clear - Stop monitoring all contract addresses.\n"
        "/stop `<contract_address>` - Stop monitoring a specific contract address.\n\n"
        "📞 *Support:*\n"
        "If you have any questions or need assistance, feel free to contact [@Dicki69](https://t.me/Dicki69)."
    )

    if update.callback_query:
        # If the function is called from a button press
        logger.debug("Help command invoked via callback query.")
        await update.callback_query.answer()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=help_text,
            parse_mode='MarkdownV2',
            disable_web_page_preview=True
        )
    else:
        # If the function is called from the /help command
        logger.debug("Help command invoked via /help command.")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=help_text,
            parse_mode='MarkdownV2',
            disable_web_page_preview=True
        )
    logger.info(f"Sent help message to user {update.effective_chat.id}.")

# Function to toggle auto-monitoring
@debug_log
async def toggle_monitoring(update: Update, context: CallbackContext):
    user_id = update.effective_chat.id
    current_preference = user_monitoring_preferences[user_id]
    user_monitoring_preferences[user_id] = not current_preference
    new_status = "ON" if user_monitoring_preferences[user_id] else "OFF"
    logger.info(f"User {user_id} toggled auto-monitoring to {new_status}.")

    welcome_text = (
        "👋 *Welcome to Dexycheck Bot!*\n\n"
        "Monitor the status of your Contract addresses in real-time.\n\n"
        "📋 *Available Commands:*\n"
        "/start - Display this welcome message\n"
        "/list - Show your monitored Contract addresses\n"
        "/clear - Stop monitoring all Contract addresses\n"
        "/help - Get help on how to use the bot\n\n"
        "🔄 *Auto-Monitoring is currently:* *{}*\n"
        "You can toggle this setting using the buttons below."
    ).format(new_status)

    keyboard = [
        [InlineKeyboardButton(f"Toggle Auto-Monitoring (Currently {new_status})", callback_data='toggle_monitoring')],
        [InlineKeyboardButton("📋 List Contract Addresses", callback_data='list_pairs')],
        [InlineKeyboardButton("🛑 Clear Contract Addresses", callback_data='clear_pairs')],
        [InlineKeyboardButton("ℹ️ Help", callback_data='help')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        text=welcome_text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    logger.debug(f"Updated welcome message for user {user_id} with new auto-monitoring status.")

# Command handler for /clear
@debug_log
async def clear(update: Update, context: CallbackContext):
    user_id = update.effective_chat.id
    logger.info(f"User {user_id} initiated /clear command.")

    user_pair_statuses[user_id].clear()
    original_pair_addresses[user_id].clear()
    logger.debug(f"Cleared pair statuses and original addresses for user {user_id}.")

    # Cancel all monitoring tasks for the user
    for pair_address, task in user_monitoring_tasks[user_id].items():
        task.cancel()
        logger.debug(f"Cancelled monitoring task for pair {pair_address} for user {user_id}.")
    user_monitoring_tasks[user_id].clear()

    await context.bot.send_message(chat_id=user_id, text="🛑 Stopped monitoring all Contract addresses.")
    logger.info(f"User {user_id} has stopped monitoring all Contract addresses.")

# Command handler for /list
@debug_log
async def list_pairs(update: Update, context: CallbackContext):
    user_id = update.effective_chat.id
    logger.info(f"User {user_id} requested list of monitored Contract addresses.")

    active_pairs = user_pair_statuses[user_id]
    if not active_pairs:
        await context.bot.send_message(chat_id=user_id, text="📭 You are not monitoring any Contract addresses currently.")
        logger.debug(f"No active pairs found for user {user_id}.")
    else:
        message_text = "📋 *Contract Addresses You Are Monitoring:*\n\n"
        for idx, pair_address in enumerate(active_pairs, 1):
            status = active_pairs[pair_address]
            status_emoji = get_status_emoji(status)
            original_pa = original_pair_addresses[user_id][pair_address]
            message_text += f"{status_emoji} **PA #{idx}**: `{original_pa}`\n"
        await context.bot.send_message(chat_id=user_id, text=message_text, parse_mode='Markdown')
        logger.debug(f"Sent list of monitored pairs to user {user_id}.")

# Command handler for /stop
@debug_log
async def stop_monitoring(update: Update, context: CallbackContext):
    user_id = update.effective_chat.id
    args = context.args
    logger.info(f"User {user_id} initiated /stop command with args: {args}")

    if not args:
        await context.bot.send_message(chat_id=user_id, text="❗ Please provide the Contract address to stop monitoring.\nUsage: /stop `<contract_address>`", parse_mode='Markdown')
        logger.warning(f"User {user_id} did not provide a Contract address for /stop command.")
        return
    pair_to_stop = args[0].lower()
    logger.debug(f"User {user_id} requested to stop monitoring pair: {pair_to_stop}")

    if pair_to_stop in user_monitoring_tasks[user_id]:
        task = user_monitoring_tasks[user_id][pair_to_stop]
        task.cancel()
        logger.debug(f"Cancelled monitoring task for pair {pair_to_stop} for user {user_id}.")

        original_pair = original_pair_addresses[user_id].get(pair_to_stop, pair_to_stop)
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🛑 Stopped monitoring Contract address `{original_pair}`.",
            parse_mode='Markdown'
        )
        logger.info(f"User {user_id} has stopped monitoring Contract address {original_pair}.")
    else:
        await context.bot.send_message(
            chat_id=user_id,
            text="⚠️ You're not monitoring this Contract address.",
            parse_mode='Markdown'
        )
        logger.warning(f"User {user_id} attempted to stop monitoring a non-monitored pair: {pair_to_stop}.")

# Callback query handler for inline buttons
@debug_log
async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    user_id = update.effective_chat.id
    logger.info(f"User {user_id} pressed button with data: {data}")

    if data == 'list_pairs':
        await list_pairs(update, context)
    elif data == 'clear_pairs':
        await clear(update, context)
    elif data == 'help':
        await help_command(update, context)
    elif data == 'toggle_monitoring':
        await toggle_monitoring(update, context)
    elif data.startswith('monitor_'):
        await manual_monitor(update, context)
    else:
        logger.warning(f"Received unknown callback data: {data} from user {user_id}")
        await query.answer(text="⚠️ Unknown action.", show_alert=True)

# Function to handle manual monitoring via button
@debug_log
async def manual_monitor(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    data = query.data
    pair_address = data.split('_', 1)[1].lower()
    user_id = update.effective_chat.id
    original_pair_address = original_pair_addresses[user_id].get(pair_address, pair_address)
    logger.info(f"User {user_id} initiated manual monitoring for pair {pair_address}.")

    if pair_address in user_pair_statuses[user_id]:
        logger.debug(f"Pair {pair_address} is already being monitored by user {user_id}.")
        await send_message_with_retry(
            context.bot, user_id, "⚠️ Contract address is already being monitored."
        )
        return

    try:
        async with aiohttp.ClientSession() as session:
            logger.debug(f"Fetching status for pair {pair_address} during manual monitoring.")
            status = await get_pair_status(session, pair_address)

        logger.debug(f"Status for pair {pair_address}: {status}")
        await monitor_pair(user_id, pair_address, original_pair_address, status, context)
    except Exception as e:
        logger.error(f"An error occurred in manual_monitor: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=user_id,
            text="😞 An unexpected error occurred. Please try again later.",
            parse_mode='Markdown'
        )

# Function to start monitoring a pair
@debug_log
async def monitor_pair(user_id, pair_address, original_pair_address, status, context):
    status_with_emoji = f"{status} {get_status_emoji(status)}"
    keyboard = create_inline_button(original_pair_address)
    logger.info(f"Starting to monitor pair {pair_address} for user {user_id} with initial status {status}.")

    if status == "PAID":
        message_text = (
            f"✅ *Contract Address Paid!*\n\n"
            f"**Contract Address:** `{original_pair_address}`"
        )
        await send_message_with_retry(context.bot, user_id, message_text, keyboard)
        logger.debug(f"Contract address {original_pair_address} is already PAID. Not adding to monitoring.")
        # Do not add to monitoring
        return

    # Now add to monitoring
    # Store the original pair address
    original_pair_addresses[user_id][pair_address] = original_pair_address
    user_pair_statuses[user_id][pair_address] = status
    last_change_time[user_id][pair_address] = datetime.now()
    num_of_pairs = len(user_pair_statuses[user_id])

    message_text = (
        f"🔄 *Monitoring Contract Address #{num_of_pairs}*\n\n"
        f"**Status:** {status_with_emoji}\n"
        f"**Contract Address:** `{original_pair_address}`\n\n"
        "🔔 We'll notify you of any status changes."
    )
    await send_message_with_retry(context.bot, user_id, message_text, keyboard)
    logger.debug(f"Sent monitoring confirmation to user {user_id} for pair {original_pair_address}.")

    if pair_address not in user_monitoring_tasks[user_id]:
        task = asyncio.create_task(monitor_status_changes(user_id, pair_address, context))
        user_monitoring_tasks[user_id][pair_address] = task
        logger.info(f"Created monitoring task for pair {pair_address} for user {user_id}.")

# Monitor status changes for a pair address
@debug_log
async def monitor_status_changes(user_id, pair_address, context):
    last_status = user_pair_statuses[user_id][pair_address]
    original_pair_address = original_pair_addresses[user_id][pair_address]
    logger.info(f"Started monitoring status changes for pair {pair_address} for user {user_id}.")

    async with aiohttp.ClientSession() as session:
        while pair_address in user_monitoring_tasks[user_id]:
            try:
                status = await get_pair_status(session, pair_address)
                current_status = status
                now = datetime.now()
                logger.debug(f"Checked status for {pair_address}: {current_status}")

                if current_status in ["FETCH FAILED", "API RATE LIMIT EXCEEDED"]:
                    await send_message_with_retry(
                        context.bot,
                        user_id,
                        "⚠️ *Error fetching Contract address status. Monitoring stopped.*"
                    )
                    logger.warning(f"Monitoring stopped for {pair_address} due to API error.")
                    break

                if current_status != last_status:
                    last_change_time[user_id][pair_address] = now
                    status_with_emoji = f"{current_status} {get_status_emoji(current_status)}"
                    keyboard = create_inline_button(original_pair_address)

                    if current_status == "PAID":
                        message_text = (
                            f"✅ *Contract Address Paid!*\n\n"
                            f"**Contract Address:** `{original_pair_address}`"
                        )
                        await send_message_with_retry(context.bot, user_id, message_text, keyboard)
                        logger.info(f"Contract address {original_pair_address} has been PAID. Stopping monitoring.")
                        # Stop monitoring
                        break

                    message_text = (
                        f"🔄 *Status Update*\n\n"
                        f"**Status:** {status_with_emoji}\n"
                        f"**Contract Address:** `{original_pair_address}`"
                    )
                    await send_message_with_retry(context.bot, user_id, message_text, keyboard)
                    logger.info(f"Status updated for pair {original_pair_address} to {current_status} for user {user_id}.")

                    last_status = current_status
                    user_pair_statuses[user_id][pair_address] = current_status

                # Stop monitoring if no status change for 30 minutes
                elapsed_time = now - last_change_time[user_id][pair_address]
                if elapsed_time > timedelta(minutes=60):
                    status_with_emoji = f"{current_status} {get_status_emoji(current_status)}"
                    message_text = (
                        f"🕒 *Monitoring Stopped*\n\n"
                        f"**Contract Address:** `{original_pair_address}`\n"
                        f"**Last Status:** {status_with_emoji}\n\n"
                        "🛑 *No status change detected for over 60 minutes.*"
                    )
                    await send_message_with_retry(context.bot, user_id, message_text)
                    logger.info(f"Stopped monitoring pair {original_pair_address} for user {user_id} due to inactivity.")
                    break

                await asyncio.sleep(60)  # Check for status changes every 1 minutes (60 seconds)
                logger.debug(f"Sleeping for 5 minutes before next status check for pair {pair_address}.")

            except asyncio.CancelledError:
                logger.info(f"Monitoring task for pair {pair_address} was cancelled by user {user_id}.")
                break
            except Exception as e:
                logger.error(f"Error in monitoring status changes for pair {pair_address}: {e}", exc_info=True)
                break

    # Clean up after monitoring stops
    user_monitoring_tasks[user_id].pop(pair_address, None)
    user_pair_statuses[user_id].pop(pair_address, None)
    original_pair_addresses[user_id].pop(pair_address, None)
    last_change_time[user_id].pop(pair_address, None)
    logger.debug(f"Cleaned up monitoring data for pair {pair_address} for user {user_id}.")

# Message handler for receiving pair addresses
@debug_log
async def check_status(update: Update, context: CallbackContext):
    user_id = update.effective_chat.id
    user_username = update.effective_user.username or "Unknown"
    original_pair_address = update.message.text.strip()
    pair_address = original_pair_address.lower()  # Use lower case for processing
    logger.info(f"Received status check from user {user_id} (Username: {user_username}) for Contract address: {original_pair_address}")

    # Increment the user's request count for the day
    today = datetime.now().date()
    user_request_counts[user_id][today] += 1
    logger.debug(f"User {user_id} has made {user_request_counts[user_id][today]} requests today.")

    if not is_valid_pair_address(original_pair_address):
        message_text = (
            "❌ *Invalid Contract Address*\n\n"
            "Please ensure you've entered a valid Contract address between 35 and 50 characters.\n"
            "Example: `3MAtbFBf6TbhgGZC8KmeUrN8dxLY5wZszknVBCsX1CyG`"
        )
        logger.warning(f"User {user_id} provided invalid pair address: {original_pair_address}")
        await send_message_with_retry(
            context.bot, user_id, message_text, parse_mode='Markdown'
        )
        return

    # Check if pair is already being monitored
    if pair_address in user_pair_statuses[user_id]:
        logger.info(f"User {user_id} is already monitoring pair {pair_address}.")
        await send_message_with_retry(
            context.bot, user_id, "⚠️ Contract address is already being monitored."
        )
        return

    try:
        async with aiohttp.ClientSession() as session:
            logger.debug(f"Fetching status for pair {pair_address}.")
            status = await get_pair_status(session, pair_address)

        status_with_emoji = f"{status} {get_status_emoji(status)}"
        logger.debug(f"Status fetched for pair {pair_address}: {status_with_emoji}")

        if status in ["FETCH FAILED", "API RATE LIMIT EXCEEDED"]:
            message_text = (
                "⚠️ *Error Fetching Contract Address Status*\n\n"
                "Please try again later or contact support if the issue persists."
            )
            logger.error(f"Failed to fetch status for pair {pair_address}: {status}")
            await send_message_with_retry(
                context.bot, user_id, message_text
            )
            return

        if user_monitoring_preferences[user_id]:
            # Automatic monitoring is ON
            logger.debug(f"Auto-monitoring is ON for user {user_id}. Starting monitoring for pair {pair_address}.")
            await monitor_pair(user_id, pair_address, original_pair_address, status, context)
        else:
            # Automatic monitoring is OFF
            message_text = (
                f"🔍 *Contract Address Status*\n\n"
                f"**Status:** {status_with_emoji}\n"
                f"**Contract Address:** `{original_pair_address}`\n\n"
                "🔔 *Would you like to monitor this Contract address?*"
            )
            # Add the wallet buttons and the monitor button
            keyboard_markup = create_inline_button(original_pair_address)
            # Convert inline_keyboard to a list of lists
            keyboard_buttons = [list(row) for row in keyboard_markup.inline_keyboard]
            # Append the new button
            keyboard_buttons.append(
                [InlineKeyboardButton("➕ Add for Monitoring", callback_data=f"monitor_{pair_address}")]
            )
            full_keyboard = InlineKeyboardMarkup(keyboard_buttons)
            logger.debug(f"Sending status message with monitoring option to user {user_id}.")
            await send_message_with_retry(
                context.bot, user_id, message_text, full_keyboard, parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"An unexpected error occurred in check_status: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=user_id,
            text="😞 An unexpected error occurred. Please try again later.",
            parse_mode='Markdown'
        )

# Modify your main function
def main():
    # Create the application with the builder pattern
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Add handlers
    start_handler = CommandHandler('start', start)
    application.add_handler(start_handler)

    help_handler = CommandHandler('help', help_command)
    application.add_handler(help_handler)

    clear_handler = CommandHandler('clear', clear)
    application.add_handler(clear_handler)

    list_handler = CommandHandler('list', list_pairs)
    application.add_handler(list_handler)

    stop_handler = CommandHandler('stop', stop_monitoring)
    application.add_handler(stop_handler)

    # Add callback query handlers with specific patterns
    button_query_handler = CallbackQueryHandler(button_handler)
    application.add_handler(button_query_handler)

    manual_monitor_handler = CallbackQueryHandler(manual_monitor, pattern='^monitor_')
    application.add_handler(manual_monitor_handler)

    message_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), check_status)
    application.add_handler(message_handler)

    logger.info("Starting DexyCheck Bot...")

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()