import logging
import requests
import base64
import io
import asyncio
import time
import datetime
import json
import os
import aiohttp
from collections import defaultdict
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ApplicationBuilder
)
from telegram.constants import ParseMode, ChatAction

TELEGRAM_BOT_TOKEN = "7639285272:AAH-vhuRyoVDMNjqyvkDgfsZw7_d5GEc77Q" # <<< ВАШ ОПУБЛИКОВАННЫЙ TELEGRAM TOKEN
LANGDOCK_API_KEY = "sk-OP8Ybcki6KOxtIcWZCFmrdNGizFUSiMLIu7sncfB0Pzqi1mfSFVhlz1x-GwBRZ1aPCWwglAY2V5bjNsA4c_Zfw" # <<< ВАШ ОПУБЛИКОВАННЫЙ LANGDOCK KEY

LANGDOCK_API_URL = "https://api.langdock.com/anthropic/eu/v1/messages"
CLAUDE_MODEL = "claude-3-7-sonnet-20250219"
MAX_MESSAGE_LENGTH = 4096
API_TIMEOUT = 180 

# Настройки ограничения запросов и администратора
ADMIN_ID = 8199808170
WEEKLY_REQUEST_LIMIT = 10
USER_DATA = {}  # Словарь для хранения данных пользователей
USER_DATA_FILE = "user_data.json"  # Файл для хранения данных пользователей

# Семафоры для ограничения параллельных запросов к API
API_SEMAPHORE = asyncio.Semaphore(5)  # Максимум 5 одновременных запросов к API
USER_LOCKS = defaultdict(asyncio.Lock)  # Блокировки по пользователям для атомарного доступа к данным

# Добавляем в начало файла после импортов
PROCESSING_USERS = set()  # Множество пользователей, запросы которых в обработке

# Добавляем константы для отслеживания расходов API
INITIAL_API_BALANCE = 100.0  # Начальный баланс в евро
COST_PER_QUERY = 0.015  # Примерная стоимость одного запроса в евро (может быть скорректирована)
API_USAGE = {
    'total_tokens': 0,
    'total_cost': 0.0,
    'queries_count': 0,
    'last_update': time.time()
}
API_USAGE_FILE = "api_usage.json"  # Файл для хранения данных об использовании API

# Добавляем новые константы для LangDock API учета использования
LANGDOCK_BILLING_API_URL = "https://api.langdock.com/billing/v1/usage"  # Предполагаемый URL для запроса данных биллинга

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s: %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("ТелеграмБот")

def save_user_data():
    """Сохраняет данные пользователей в файл."""
    try:
        with open(USER_DATA_FILE, 'w', encoding='utf-8') as file:
            # Преобразуем числовые ключи в строки для json
            data_to_save = {str(k): v for k, v in USER_DATA.items()}
            json.dump(data_to_save, file, ensure_ascii=False, indent=2)
        logger.info(f"Данные пользователей сохранены в {USER_DATA_FILE}")
    except Exception as e:
        logger.error(f"Ошибка при сохранении данных пользователей: {e}")

def load_user_data():
    """Загружает данные пользователей из файла."""
    global USER_DATA
    try:
        if os.path.exists(USER_DATA_FILE):
            with open(USER_DATA_FILE, 'r', encoding='utf-8') as file:
                # Загружаем данные и преобразуем строковые ключи обратно в числа
                loaded_data = json.load(file)
                USER_DATA = {int(k): v for k, v in loaded_data.items()}
            logger.info(f"Данные пользователей загружены из {USER_DATA_FILE}")
        else:
            logger.info(f"Файл {USER_DATA_FILE} не найден, будет создан новый")
    except Exception as e:
        logger.error(f"Ошибка при загрузке данных пользователей: {e}")
        USER_DATA = {}

def save_api_usage():
    """Сохраняет данные об использовании API в файл."""
    try:
        with open(API_USAGE_FILE, 'w', encoding='utf-8') as file:
            json.dump(API_USAGE, file, ensure_ascii=False, indent=2)
        logger.info(f"Данные использования API сохранены в {API_USAGE_FILE}")
    except Exception as e:
        logger.error(f"Ошибка при сохранении данных использования API: {e}")

def load_api_usage():
    """Загружает данные об использовании API из файла."""
    global API_USAGE
    try:
        if os.path.exists(API_USAGE_FILE):
            with open(API_USAGE_FILE, 'r', encoding='utf-8') as file:
                loaded_data = json.load(file)
                
                # Убедимся, что все необходимые поля присутствуют
                API_USAGE = {
                    'total_tokens': loaded_data.get('total_tokens', 0),
                    'total_cost': loaded_data.get('total_cost', 0.0),
                    'queries_count': loaded_data.get('queries_count', 0),
                    'last_update': loaded_data.get('last_update', time.time())
                }
            logger.info(f"Данные использования API загружены из {API_USAGE_FILE}: {API_USAGE}")
        else:
            logger.info(f"Файл {API_USAGE_FILE} не найден, будут использованы значения по умолчанию")
            API_USAGE = {
                'total_tokens': 0,
                'total_cost': 0.0,
                'queries_count': 0,
                'last_update': time.time()
            }
            save_api_usage()  # Создаем файл с начальными значениями
    except Exception as e:
        logger.error(f"Ошибка при загрузке данных использования API: {e}")
        API_USAGE = {
            'total_tokens': 0,
            'total_cost': 0.0,
            'queries_count': 0,
            'last_update': time.time()
        }
        save_api_usage()  # Пытаемся сохранить дефолтные значения

async def send_long_message(update: Update, text: str):
    """Отправляет длинные сообщения частями."""
    if not text: return
    user_id = update.effective_user.id
    for i in range(0, len(text), MAX_MESSAGE_LENGTH):
        chunk = text[i:i + MAX_MESSAGE_LENGTH]
        is_last_chunk = i + MAX_MESSAGE_LENGTH >= len(text)
        try:
            # Добавляем клавиатуру только к последнему фрагменту
            if is_last_chunk:
                await update.message.reply_text(chunk, parse_mode=ParseMode.HTML, reply_markup=get_profile_keyboard(user_id))
            else:
                await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
                
            if len(text) > MAX_MESSAGE_LENGTH: await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
            try:
                error_msg = (
                    "<b>⚠️ Ошибка при отправке части ответа</b>\n\n"
                    "Не удалось отправить полный ответ. Возможно, в тексте содержится неподдерживаемое форматирование."
                )
                await update.message.reply_html(error_msg, reply_markup=get_profile_keyboard(user_id))
            except Exception as inner_e:
                logger.error(f"Невозможно отправить сообщение об ошибке: {inner_e}")
            break

def get_profile_keyboard(user_id=None):
    """Создает клавиатуру с кнопкой профиля и админ-панелью для администратора."""
    if user_id == ADMIN_ID:
        keyboard = [
            [KeyboardButton("Мой профиль")],
            [KeyboardButton("🛡️ Админ-панель")]
        ]
    else:
        keyboard = [[KeyboardButton("Мой профиль")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def init_user_data(user_id, user):
    """Инициализирует данные пользователя если их нет."""
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {
            'first_name': user.first_name,
            'last_name': user.last_name,
            'username': user.username,
            'joined_at': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'requests_left': WEEKLY_REQUEST_LIMIT,
            'reset_time': get_next_reset_time(),
            'total_requests': 0,
        }
        save_user_data()  # Сохраняем данные после создания нового пользователя
    return USER_DATA[user_id]

def get_next_reset_time():
    """Получает время следующего сброса счетчика запросов (начало следующей недели)."""
    today = datetime.datetime.now()
    days_until_monday = 7 - today.weekday() if today.weekday() > 0 else 7
    next_monday = today + datetime.timedelta(days=days_until_monday)
    next_monday = next_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return next_monday.timestamp()

def check_and_update_requests(user_id, context):
    """Проверяет и обновляет количество оставшихся запросов пользователя."""
    user_data = USER_DATA[user_id]
    
    # Проверка необходимости сброса счетчика
    current_time = time.time()
    if current_time >= user_data['reset_time']:
        user_data['requests_left'] = WEEKLY_REQUEST_LIMIT
        user_data['reset_time'] = get_next_reset_time()
        # Сохраняем данные после обновления счетчика
        save_user_data()
    
    # Если у пользователя закончились запросы
    if user_data['requests_left'] <= 0 and user_id != ADMIN_ID:
        reset_date = datetime.datetime.fromtimestamp(user_data['reset_time']).strftime("%d.%m.%Y")
        return False, f"У вас закончились запросы на этой неделе. Лимит будет обновлен {reset_date}."
    
    return True, None

async def call_claude_api(user_id: int, context: ContextTypes.DEFAULT_TYPE, new_user_content: list | str) -> str | None:
    """Вызывает API Claude, возвращает ответ или сообщение об ошибке."""
    if 'history' not in context.user_data: context.user_data['history'] = []
    history = context.user_data['history']
    history.append({"role": "user", "content": new_user_content})

    max_history_messages = 10
    if len(history) > max_history_messages:
        history = history[-max_history_messages:]
        context.user_data['history'] = history

    headers = {"Authorization": f"Bearer {LANGDOCK_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": CLAUDE_MODEL, "messages": history, "max_tokens": 4000}
    logger.info(f"Отправка запроса к API для пользователя {user_id}. История: {len(history)} сообщений")

    # Используем семафор для ограничения параллельных запросов к API
    async with API_SEMAPHORE:
        try:
            logger.info(f"Получен доступ к API для пользователя {user_id}")
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(LANGDOCK_API_URL, headers=headers, json=payload, timeout=API_TIMEOUT) as response:
                        response.raise_for_status()
                        response_data = await response.json()
                        
                        # Обновляем статистику использования API
                        API_USAGE['queries_count'] += 1
                        
                        # Если в ответе есть информация о токенах, учитываем ее
                        if response_data.get('usage') and 'total_tokens' in response_data['usage']:
                            tokens_used = response_data['usage']['total_tokens']
                            API_USAGE['total_tokens'] += tokens_used
                            # Оценочная стоимость запроса (можно скорректировать формулу)
                            query_cost = tokens_used * 0.000005  # примерно 0.5 евроцента за 1000 токенов
                            API_USAGE['total_cost'] += query_cost
                            logger.info(f"Запрос использовал {tokens_used} токенов, стоимость: {query_cost:.4f} €")
                        else:
                            # Если информации о токенах нет, используем фиксированную стоимость
                            API_USAGE['total_cost'] += COST_PER_QUERY
                            logger.info(f"Информация о токенах не получена, использую фиксированную стоимость: {COST_PER_QUERY} €")
                            
                        API_USAGE['last_update'] = time.time()
                        save_api_usage()  # Сохраняем обновленные данные
                
                except asyncio.TimeoutError:
                    logger.error(f"Таймаут API ({API_TIMEOUT} сек) для пользователя {user_id}")
                    return f"<b>⏳ Превышено время ожидания</b>\n\nНейросеть отвечает слишком долго (более {API_TIMEOUT} сек). Пожалуйста, попробуйте позже или задайте более короткий вопрос."
                except aiohttp.ClientResponseError as e:
                    logger.error(f"Ошибка HTTP {e.status}: {e.message}")
                    
                    # Понятное сообщение для пользователя
                    if e.status == 401:
                        return "<b>❌ Ошибка авторизации</b>\n\nПроблема с доступом к API нейросети. Пожалуйста, сообщите администратору."
                    elif e.status == 429:
                        return "<b>⚠️ Превышен лимит запросов</b>\n\nСлишком много запросов к нейросети. Пожалуйста, подождите несколько минут и попробуйте снова."
                    elif e.status >= 500:
                        return "<b>🛠️ Технические работы</b>\n\nСервис нейросети временно недоступен. Пожалуйста, попробуйте позже."
                    else:
                        return f"<b>❌ Ошибка {e.status}</b>\n\nПроизошла проблема при обработке запроса."

            if response_data.get("content") and isinstance(response_data["content"], list) and len(response_data["content"]) > 0:
                assistant_response_block = response_data["content"][0]
                if assistant_response_block.get("type") == "text":
                    assistant_text = assistant_response_block.get("text", "").strip()
                    if assistant_text:
                        history.append({"role": "assistant", "content": assistant_text})
                        context.user_data['history'] = history
                        logger.info(f"Получен успешный ответ от API для пользователя {user_id}. Длина: {len(assistant_text)} символов")
                        return assistant_text
                    else:
                        logger.error(f"API вернуло пустой текстовый блок: {response_data}")
                        return "<b>⚠️ Ошибка:</b> Получен пустой ответ от нейросети. Попробуйте повторить запрос."
                else:
                     logger.error(f"API вернуло нетекстовый блок: {response_data}")
                     return "<b>⚠️ Ошибка:</b> Получен ответ в неподдерживаемом формате."
            else:
                logger.error(f"Неожиданная структура ответа API: {response_data}")
                stop_reason = response_data.get("stop_reason")
                if stop_reason == "max_tokens":
                     return "<b>⚠️ Внимание:</b> Ответ получился слишком длинным и был обрезан. Попробуйте переформулировать вопрос или используйте команду /clear."
                return f"<b>⚠️ Ошибка:</b> Проблема с ответом нейросети. (Причина: {stop_reason or 'Неизвестная ошибка'})"

        except aiohttp.ClientError as e:
            logger.error(f"Ошибка сети при запросе к API: {e}")
            return "<b>🔌 Ошибка подключения</b>\n\nНе удалось подключиться к нейросети. Пожалуйста, проверьте подключение к интернету."
        except Exception as e:
            logger.exception(f"Неожиданная ошибка API для пользователя {user_id}: {e}")
            return f"<b>💥 Непредвиденная ошибка</b>\n\nПроизошла неизвестная ошибка при обработке запроса. Пожалуйста, попробуйте позже."

# --- Обработчики Telegram ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start с улучшенным дизайном."""
    user = update.effective_user
    user_id = user.id
    context.user_data['history'] = [] # Очищаем историю
    logger.info(f"Пользователь {user.id} ({user.username or 'без имени'}) запустил бота")

    # Инициализируем данные пользователя
    init_user_data(user_id, user)

    # Формируем приветственное сообщение с HTML-разметкой
    welcome_message = (
        f"<b>✨ Приветствую, {user.first_name}! ✨</b>\n\n"
        f"<b>🤖 Меня зовут Claude 3.7 Sonnet</b> — ваш интеллектуальный ассистент нового поколения!\n\n"
        f"<b>🔮 Что я могу:</b>\n"
        f"  • 💬 Отвечу на любые вопросы\n"
        f"  • 🖼️ Проанализирую изображения\n"
        f"  • 📝 Создам и отредактирую тексты\n"
        f"  • 📊 Помогу с анализом данных\n"
        f"  • 🧠 Запомню всю нашу беседу\n\n"
        f"<b>⚙️ Доступные команды:</b>\n"
        f"  • /start — новый диалог\n"
        f"  • /clear — очистка истории\n"
        f"  • /profile — ваш профиль\n"
    )
    
    # Добавляем информацию о команде админ-панели только для администратора
    if user_id == ADMIN_ID:
        welcome_message += f"  • /admin_panel — панель администратора\n"
        
    welcome_message += (
        f"\n<i>Просто напишите или отправьте фото, и я помогу вам!</i>\n\n"
        f"<b>💫 Давайте начнем увлекательное общение! 💫</b>"
    )

    # Отправляем сообщение с клавиатурой
    await update.message.reply_html(welcome_message, reply_markup=get_profile_keyboard(user_id))
    logger.info(f"Приветственное сообщение отправлено пользователю {user.id}")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /clear."""
    user_id = update.effective_user.id
    context.user_data['history'] = []
    logger.info(f"История очищена для пользователя {user_id}")
    
    clear_message = (
        "<b>🧹 История диалога очищена!</b>\n\n"
        "Все предыдущие сообщения забыты. Можем начать общение заново."
    )
    
    await update.message.reply_html(clear_message, reply_markup=get_profile_keyboard(user_id))

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отображает профиль пользователя и информацию о лимитах."""
    user = update.effective_user
    user_id = user.id
    
    # Используем блокировку для безопасного доступа к данным пользователя
    async with USER_LOCKS[user_id]:
        # Инициализируем данные пользователя, если они еще не созданы
        user_data = init_user_data(user_id, user)
        
        # Проверяем и обновляем счетчик запросов на случай, если истек срок сброса
        check_and_update_requests(user_id, context)
        
        # Форматируем дату сброса
        reset_date = datetime.datetime.fromtimestamp(user_data['reset_time']).strftime("%d.%m.%Y")
        
        # Определяем статус пользователя
        status = "🔑 Администратор" if user_id == ADMIN_ID else "👤 Пользователь"
        
        # Рассчитываем процент оставшихся запросов для прогресс-бара
        if user_id == ADMIN_ID:
            progress_bar = "▰▰▰▰▰▰▰▰▰▰"  # Полная полоса для админа
            progress_percent = 100
        else:
            progress_percent = int((user_data['requests_left'] / WEEKLY_REQUEST_LIMIT) * 100)
            filled_blocks = int(progress_percent / 10)
            progress_bar = "▰" * filled_blocks + "▱" * (10 - filled_blocks)
        
        # Выбираем эмодзи для индикатора запросов
        if user_id == ADMIN_ID:
            requests_emoji = "♾️"  # Бесконечность для админа
        elif progress_percent >= 70:
            requests_emoji = "🟢"  # Много запросов
        elif progress_percent >= 30:
            requests_emoji = "🟡"  # Среднее количество
        else:
            requests_emoji = "🔴"  # Мало запросов
        
        # Рассчитываем время использования бота
        joined_date = datetime.datetime.strptime(user_data['joined_at'], "%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.now()
        days_since_joined = (now - joined_date).days
        
        # Составляем информацию о профиле с улучшенным форматированием
        profile_info = (
            f"<b>📱 ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ</b>\n\n"
            f"<b>┌─────────────────────────</b>\n"
            f"<b>│ 👤 Имя:</b> {user_data['first_name']}"
        )
        
        if user_data['last_name']:
            profile_info += f"\n<b>│ 👤 Фамилия:</b> {user_data['last_name']}"
        
        if user_data['username']:
            profile_info += f"\n<b>│ 🔖 Username:</b> @{user_data['username']}"
        
        profile_info += (
            f"\n<b>│ 🆔 ID:</b> <code>{user_id}</code>"
            f"\n<b>│ 🏅 Статус:</b> {status}"
            f"\n<b>│ 📅 Дата регистрации:</b> {user_data['joined_at']}"
            f"\n<b>│ ⏱️ Дней с нами:</b> {days_since_joined}"
            f"\n<b>└─────────────────────────</b>\n\n"
            
            f"<b>📊 СТАТИСТИКА ИСПОЛЬЗОВАНИЯ</b>\n\n"
            f"<b>┌─────────────────────────</b>\n"
            f"<b>│ 📈 Всего запросов:</b> {user_data['total_requests']}"
        )
        
        # Для обычных пользователей показываем лимиты
        if user_id != ADMIN_ID:
            profile_info += (
                f"\n<b>│ {requests_emoji} Оставшиеся запросы:</b> {user_data['requests_left']} из {WEEKLY_REQUEST_LIMIT}"
                f"\n<b>│ 📊 Прогресс:</b> {progress_bar} ({progress_percent}%)"
                f"\n<b>│ 🔄 Сброс лимита:</b> {reset_date}"
            )
        else:
            profile_info += f"\n<b>│ {requests_emoji} Лимит запросов:</b> Не ограничен"
        
        profile_info += f"\n<b>└─────────────────────────</b>\n\n"
        
        # Добавляем подсказку для пользователя
        profile_info += (
            f"<i>💡 Чтобы продолжить общение, просто отправьте сообщение или фотографию.</i>"
        )
    
    # Отправляем информацию о профиле
    await update.message.reply_html(profile_info, reply_markup=get_profile_keyboard(user_id))
    logger.info(f"Профиль пользователя {user_id} отправлен")

async def reset_limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /reset_limits для администратора."""
    user_id = update.effective_user.id
    
    # Проверяем, является ли пользователь администратором
    if user_id != ADMIN_ID:
        await update.message.reply_html(
            "<b>⛔ Доступ запрещен</b>\n\nЭта команда доступна только администратору.",
            reply_markup=get_profile_keyboard(user_id)
        )
        return
    
    # Проверяем наличие аргумента - ID пользователя
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_html(
            "<b>❌ Ошибка</b>\n\nИспользование: /reset_limits [ID пользователя]\nПример: /reset_limits 123456789",
            reply_markup=get_profile_keyboard(user_id)
        )
        return
    
    target_user_id = int(args[0])
    
    # Используем блокировку для безопасного доступа к данным пользователя
    async with USER_LOCKS[target_user_id]:
        # Проверяем существование пользователя в базе
        if target_user_id not in USER_DATA:
            await update.message.reply_html(
                f"<b>❌ Ошибка</b>\n\nПользователь с ID {target_user_id} не найден.",
                reply_markup=get_profile_keyboard(user_id)
            )
            return
        
        # Сбрасываем лимит запросов пользователя
        USER_DATA[target_user_id]['requests_left'] = WEEKLY_REQUEST_LIMIT
        
        # Сохраняем изменения
        save_user_data()
    
    await update.message.reply_html(
        f"<b>✅ Успешно</b>\n\nЛимит запросов для пользователя {target_user_id} сброшен.\n"
        f"Новое количество запросов: {WEEKLY_REQUEST_LIMIT}",
        reply_markup=get_profile_keyboard(user_id)
    )
    logger.info(f"Администратор {user_id} сбросил лимит запросов для пользователя {target_user_id}")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /stats для администратора."""
    user_id = update.effective_user.id
    
    # Проверяем, является ли пользователь администратором
    if user_id != ADMIN_ID:
        await update.message.reply_html(
            "<b>⛔ Доступ запрещен</b>\n\nЭта команда доступна только администратору.",
            reply_markup=get_profile_keyboard(user_id)
        )
        return
    
    # Получаем копию данных, чтобы не блокировать слишком долго
    user_data_copy = {}
    for uid in USER_DATA:
        async with USER_LOCKS[uid]:
            user_data_copy[uid] = USER_DATA[uid].copy()
    
    # Если нет пользователей
    if not user_data_copy:
        await update.message.reply_html(
            "<b>📊 Статистика</b>\n\nНет данных о пользователях.",
            reply_markup=get_profile_keyboard(user_id)
        )
        return
    
    # Собираем статистику
    total_users = len(user_data_copy)
    total_requests = sum(user_data['total_requests'] for user_data in user_data_copy.values())
    active_users = sum(1 for user_data in user_data_copy.values() if user_data['total_requests'] > 0)
    
    # Находим топ-5 пользователей по количеству запросов
    top_users = sorted(user_data_copy.items(), key=lambda x: x[1]['total_requests'], reverse=True)[:5]
    
    # Формируем сообщение
    stats_message = (
        f"<b>📊 Общая статистика</b>\n\n"
        f"<b>Всего пользователей:</b> {total_users}\n"
        f"<b>Активных пользователей:</b> {active_users}\n"
        f"<b>Всего запросов:</b> {total_requests}\n\n"
        f"<b>Топ-5 пользователей по запросам:</b>\n"
    )
    
    for idx, (uid, user_data) in enumerate(top_users, 1):
        username = f"@{user_data['username']}" if user_data['username'] else "Без имени"
        stats_message += f"{idx}. {user_data['first_name']} {user_data['last_name'] or ''} ({username}): {user_data['total_requests']} запросов\n"
    
    await update.message.reply_html(stats_message, reply_markup=get_profile_keyboard(user_id))
    logger.info(f"Администратор {user_id} запросил статистику")

async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /admin_panel."""
    user_id = update.effective_user.id
    
    # Проверяем, является ли пользователь администратором
    if user_id != ADMIN_ID:
        await update.message.reply_html(
            "<b>⛔ Доступ запрещен</b>\n\nЭта команда доступна только администратору.",
            reply_markup=get_profile_keyboard(user_id)
        )
        return
    
    # Создаем админ-клавиатуру с дополнительной кнопкой для проверки баланса API
    admin_keyboard = [
        [KeyboardButton("📊 Статистика"), KeyboardButton("👥 Список пользователей")],
        [KeyboardButton("➕ Начислить запросы"), KeyboardButton("➖ Снять запросы")],
        [KeyboardButton("📥 Выгрузить историю"), KeyboardButton("💰 Баланс API")],
        [KeyboardButton("🔙 Вернуться")]
    ]
    
    reply_markup = ReplyKeyboardMarkup(admin_keyboard, resize_keyboard=True)
    
    admin_message = (
        f"<b>🛡️ Панель администратора</b>\n\n"
        f"Добро пожаловать в панель управления, {update.effective_user.first_name}!\n\n"
        f"<b>Доступные функции:</b>\n"
        f"• Просмотр статистики бота\n"
        f"• Экспорт списка пользователей\n"
        f"• Управление запросами пользователей\n"
        f"• Экспорт истории переписки\n"
        f"• Проверка баланса API"
    )
    
    await update.message.reply_html(admin_message, reply_markup=reply_markup)
    logger.info(f"Админ-панель отображена для администратора {user_id}")

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик текстовых сообщений."""
    user_id = update.effective_user.id
    user_text = update.message.text
    if not user_text: return

    # Обработка кнопки "Мой профиль"
    if user_text == "Мой профиль":
        await profile_command(update, context)
        return
    
    # Обработка админ-кнопок (только для администратора)
    if user_id == ADMIN_ID:
        # Кнопка вызова админ-панели
        if user_text == "🛡️ Админ-панель":
            await admin_panel_command(update, context)
            return
            
        # Кнопка статистики
        elif user_text == "📊 Статистика":
            await stats_command(update, context)
            return
            
        # Кнопка списка пользователей
        elif user_text == "👥 Список пользователей":
            await export_users_list(update, context)
            return
            
        # Кнопка баланса API
        elif user_text == "💰 Баланс API":
            await api_balance_command(update, context)
            return
            
        # Кнопка возврата из админ-панели
        elif user_text == "🔙 Вернуться":
            return_message = "<b>✅ Обычный режим</b>\n\nВы вернулись в обычный режим работы."
            await update.message.reply_html(return_message, reply_markup=get_profile_keyboard(user_id))
            return
            
        # Кнопка начисления запросов
        elif user_text == "➕ Начислить запросы":
            context.user_data['admin_action'] = 'add_requests'
            await update.message.reply_html(
                "<b>➕ Начисление запросов</b>\n\n"
                "Введите ID пользователя и количество запросов в формате:\n"
                "<code>ID количество</code>\n\n"
                "Например: <code>123456789 5</code> - добавит 5 запросов пользователю с ID 123456789"
            )
            return
            
        # Кнопка снятия запросов
        elif user_text == "➖ Снять запросы":
            context.user_data['admin_action'] = 'remove_requests'
            await update.message.reply_html(
                "<b>➖ Снятие запросов</b>\n\n"
                "Введите ID пользователя и количество запросов для снятия в формате:\n"
                "<code>ID количество</code>\n\n"
                "Например: <code>123456789 3</code> - снимет 3 запроса у пользователя с ID 123456789"
            )
            return
            
        # Кнопка выгрузки истории
        elif user_text == "📥 Выгрузить историю":
            context.user_data['admin_action'] = 'export_history'
            await update.message.reply_html(
                "<b>📥 Выгрузка истории</b>\n\n"
                "Введите ID пользователя для выгрузки истории переписки:\n"
                "<code>ID</code>\n\n"
                "Например: <code>123456789</code>"
            )
            return
            
        # Обработка действий администратора (после нажатия на кнопки)
        elif 'admin_action' in context.user_data:
            admin_action = context.user_data['admin_action']
            
            # Обработка добавления запросов
            if admin_action == 'add_requests':
                parts = user_text.split()
                if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                    await update.message.reply_html(
                        "<b>❌ Ошибка формата</b>\n\n"
                        "Введите ID пользователя и количество запросов в формате:\n"
                        "<code>ID количество</code>"
                    )
                    return
                
                target_id = int(parts[0])
                amount = int(parts[1])
                
                async with USER_LOCKS[target_id]:
                    if target_id not in USER_DATA:
                        await update.message.reply_html(
                            f"<b>❌ Пользователь не найден</b>\n\n"
                            f"Пользователь с ID {target_id} не найден в базе.",
                            reply_markup=get_profile_keyboard(user_id)
                        )
                        del context.user_data['admin_action']
                        return
                    
                    USER_DATA[target_id]['requests_left'] += amount
                    save_user_data()
                    
                    await update.message.reply_html(
                        f"<b>✅ Запросы начислены</b>\n\n"
                        f"Добавлено {amount} запросов пользователю {target_id}.\n"
                        f"Текущий баланс: {USER_DATA[target_id]['requests_left']} запросов.",
                        reply_markup=get_profile_keyboard(user_id)
                    )
                    logger.info(f"Админ {user_id} начислил {amount} запросов пользователю {target_id}")
                    del context.user_data['admin_action']
                    return
            
            # Обработка снятия запросов
            elif admin_action == 'remove_requests':
                parts = user_text.split()
                if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                    await update.message.reply_html(
                        "<b>❌ Ошибка формата</b>\n\n"
                        "Введите ID пользователя и количество запросов для снятия в формате:\n"
                        "<code>ID количество</code>"
                    )
                    return
                
                target_id = int(parts[0])
                amount = int(parts[1])
                
                async with USER_LOCKS[target_id]:
                    if target_id not in USER_DATA:
                        await update.message.reply_html(
                            f"<b>❌ Пользователь не найден</b>\n\n"
                            f"Пользователь с ID {target_id} не найден в базе.",
                            reply_markup=get_profile_keyboard(user_id)
                        )
                        del context.user_data['admin_action']
                        return
                    
                    USER_DATA[target_id]['requests_left'] = max(0, USER_DATA[target_id]['requests_left'] - amount)
                    save_user_data()
                    
                    await update.message.reply_html(
                        f"<b>✅ Запросы сняты</b>\n\n"
                        f"Снято {amount} запросов у пользователя {target_id}.\n"
                        f"Текущий баланс: {USER_DATA[target_id]['requests_left']} запросов.",
                        reply_markup=get_profile_keyboard(user_id)
                    )
                    logger.info(f"Админ {user_id} снял {amount} запросов у пользователя {target_id}")
                    del context.user_data['admin_action']
                    return
            
            # Обработка выгрузки истории
            elif admin_action == 'export_history':
                if not user_text.isdigit():
                    await update.message.reply_html(
                        "<b>❌ Ошибка формата</b>\n\n"
                        "Введите только ID пользователя.",
                        reply_markup=get_profile_keyboard(user_id)
                    )
                    return
                
                target_id = int(user_text)
                await export_chat_history(update, context, target_id)
                del context.user_data['admin_action']
                return

    # Проверяем, не обрабатывается ли уже запрос от этого пользователя
    if user_id in PROCESSING_USERS:
        await update.message.reply_html(
            "<b>⏳ Пожалуйста, подождите</b>\n\n"
            "Я еще обрабатываю ваш предыдущий запрос. Как только закончу, сразу займусь новым!",
            reply_markup=get_profile_keyboard(user_id)
        )
        return

    logger.info(f"Получено сообщение от пользователя {user_id}. Длина: {len(user_text)} символов")
    
    # Используем блокировку для безопасного доступа к данным пользователя
    async with USER_LOCKS[user_id]:
        # Инициализируем данные пользователя, если они еще не созданы
        user_data = init_user_data(user_id, update.effective_user)
        
        # Проверяем лимит запросов
        has_requests, error_message = check_and_update_requests(user_id, context)
        if not has_requests:
            await update.message.reply_html(error_message, reply_markup=get_profile_keyboard(user_id))
            return
        
        # Уменьшаем количество запросов (кроме администратора)
        if user_id != ADMIN_ID:
            user_data['requests_left'] -= 1
        
        # Увеличиваем счетчик всего запросов
        user_data['total_requests'] += 1
        
        # Сохраняем изменения данных пользователя
        save_user_data()
    
    # Добавляем пользователя в множество обрабатываемых запросов
    PROCESSING_USERS.add(user_id)
    
    try:
        # Показываем "печатает..." для лучшего UX
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        response_text = await call_claude_api(user_id, context, user_text)
        if response_text: 
            logger.info(f"Отправка ответа пользователю {user_id}. Длина ответа: {len(response_text)} символов")
            await send_long_message(update, response_text)
    finally:
        # Удаляем пользователя из множества обрабатываемых запросов
        PROCESSING_USERS.discard(user_id)

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик сообщений с фото."""
    user_id = update.effective_user.id
    logger.info(f"Получено изображение от пользователя {user_id}")
    
    # Проверяем, не обрабатывается ли уже запрос от этого пользователя
    if user_id in PROCESSING_USERS:
        await update.message.reply_html(
            "<b>⏳ Пожалуйста, подождите</b>\n\n"
            "Я еще обрабатываю ваш предыдущий запрос. Как только закончу, сразу займусь новым!",
            reply_markup=get_profile_keyboard(user_id)
        )
        return
    
    # Используем блокировку для безопасного доступа к данным пользователя
    async with USER_LOCKS[user_id]:
        # Инициализируем данные пользователя, если они еще не созданы
        user_data = init_user_data(user_id, update.effective_user)
        
        # Проверяем лимит запросов
        has_requests, error_message = check_and_update_requests(user_id, context)
        if not has_requests:
            await update.message.reply_html(error_message, reply_markup=get_profile_keyboard(user_id))
            return
        
        # Уменьшаем количество запросов (кроме администратора)
        if user_id != ADMIN_ID:
            user_data['requests_left'] -= 1
        
        # Увеличиваем счетчик всего запросов
        user_data['total_requests'] += 1
        
        # Сохраняем изменения данных пользователя
        save_user_data()
    
    # Добавляем пользователя в множество обрабатываемых запросов
    PROCESSING_USERS.add(user_id)
    
    try:
        # Показываем "загрузка фото..." (или "печатает...")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)

        photo_file = await update.message.photo[-1].get_file()
        with io.BytesIO() as buf:
            await photo_file.download_to_memory(buf)
            buf.seek(0)
            image_bytes = buf.read()
            base64_image = base64.b64encode(image_bytes).decode('utf-8')

        media_type = "image/jpeg" # По умолчанию
        if photo_file.file_path:
            ext = photo_file.file_path.split('.')[-1].lower()
            if ext == 'png': media_type = "image/png"
            elif ext == 'gif': media_type = "image/gif"
            elif ext == 'webp': media_type = "image/webp"
        logger.info(f"Тип изображения: {media_type}")

        caption = update.message.caption if update.message.caption else "Опиши это изображение."
        logger.info(f"Подпись к изображению: '{caption}'")

        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64_image}},
            {"type": "text", "text": caption}
        ]
        # Показываем "печатает..." перед долгим запросом к API
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        
        # Отправляем уведомление о начале обработки изображения
        processing_msg = await update.message.reply_html("<i>🔍 Анализирую изображение, пожалуйста, подождите...</i>")
        
        response_text = await call_claude_api(user_id, context, user_content)
        
        # Удаляем сообщение об обработке
        await processing_msg.delete()
        
        if response_text: 
            logger.info(f"Отправка ответа по изображению пользователю {user_id}")
            await send_long_message(update, response_text)

    except Exception as e:
        logger.error(f"Ошибка обработки изображения: {e}", exc_info=True)
        error_msg = (
            "<b>⚠️ Ошибка обработки изображения</b>\n\n"
            "К сожалению, не удалось обработать ваше изображение.\n"
            "Пожалуйста, попробуйте отправить другое изображение или в другом формате."
        )
        await update.message.reply_html(error_msg, reply_markup=get_profile_keyboard(user_id))
    finally:
        # Удаляем пользователя из множества обрабатываемых запросов
        PROCESSING_USERS.discard(user_id)

async def export_chat_history(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int) -> None:
    """Экспорт истории переписки пользователя в HTML формате."""
    admin_id = update.effective_user.id
    
    if target_id not in USER_DATA:
        await update.message.reply_html(
            f"<b>❌ Ошибка</b>\n\nПользователь с ID {target_id} не найден в базе.",
            reply_markup=get_profile_keyboard(admin_id)
        )
        return
    
    # Получаем данные пользователя
    user_data = USER_DATA[target_id]
    first_name = user_data['first_name'] or "Неизвестно"
    last_name = user_data['last_name'] or ""
    username = user_data['username'] or "Нет"
    is_admin = target_id == ADMIN_ID
    
    # Получаем текущую дату и время для файла
    current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    file_timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # Создаем красивый HTML файл
    html_content = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>История чата пользователя {target_id}</title>
        <style>
            :root {{
                --primary-color: #4b6cb7;
                --primary-light: #e4ecff;
                --secondary-color: #182848;
                --success-color: #4CAF50;
                --danger-color: #f44336;
                --warning-color: #ff9800;
                --light-gray: #f5f5f5;
                --dark-gray: #333;
                --border-radius: 8px;
                --box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
                --user-message-color: #e3f2fd;
                --assistant-message-color: #f1f8e9;
                --user-message-border: #2196F3;
                --assistant-message-border: #8BC34A;
                
                /* Светлая тема по умолчанию */
                --bg-color: linear-gradient(135deg, #f5f7fa 0%, #e4ecff 100%);
                --container-bg: white;
                --text-color: #333;
                --text-muted: #666;
                --border-color: #eee;
                --scrollbar-thumb: #c1c1c1;
                --scrollbar-track: #f1f1f1;
                
                --transition-speed: 0.3s;
            }}
            
            /* Темная тема */
            @media (prefers-color-scheme: dark) {{
                :root {{
                    --primary-color: #5d7dcf;
                    --primary-light: #1a2744;
                    --secondary-color: #0f1729;
                    --bg-color: linear-gradient(135deg, #111827 0%, #1e293b 100%);
                    --container-bg: #1f2937;
                    --text-color: #e2e8f0;
                    --text-muted: #9ca3af;
                    --border-color: #374151;
                    --user-message-color: #172032;
                    --assistant-message-color: #1a2e1a;
                    --user-message-border: #3b82f6;
                    --assistant-message-border: #4ade80;
                    --scrollbar-thumb: #4b5563;
                    --scrollbar-track: #1f2937;
                }}
            }}
            
            /* Переключатель темы (скрыт пока не реализуем JS) */
            #theme-toggle {{
                position: fixed;
                top: 20px;
                right: 20px;
                z-index: 1000;
                background: var(--primary-color);
                color: white;
                border: none;
                padding: 8px 15px;
                border-radius: 20px;
                cursor: pointer;
                box-shadow: var(--box-shadow);
                transition: all var(--transition-speed) ease;
            }}
            
            #theme-toggle:hover {{
                transform: translateY(-2px);
            }}
            
            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
                transition: background-color var(--transition-speed) ease, 
                           color var(--transition-speed) ease,
                           border-color var(--transition-speed) ease,
                           box-shadow var(--transition-speed) ease;
            }}
            
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                line-height: 1.6;
                color: var(--text-color);
                background: var(--bg-color);
                padding: 20px;
                min-height: 100vh;
            }}
            
            /* Стилизация скроллбара */
            ::-webkit-scrollbar {{
                width: 10px;
            }}
            
            ::-webkit-scrollbar-track {{
                background: var(--scrollbar-track);
                border-radius: 10px;
            }}
            
            ::-webkit-scrollbar-thumb {{
                background: var(--scrollbar-thumb);
                border-radius: 10px;
            }}
            
            ::-webkit-scrollbar-thumb:hover {{
                background: var(--primary-color);
            }}
            
            .container {{
                max-width: 900px;
                margin: 0 auto;
                background-color: var(--container-bg);
                border-radius: var(--border-radius);
                box-shadow: var(--box-shadow);
                overflow: hidden;
                opacity: 0;
                transform: translateY(20px);
                animation: fadeIn 0.5s ease forwards;
            }}
            
            @keyframes fadeIn {{
                to {{
                    opacity: 1;
                    transform: translateY(0);
                }}
            }}
            
            .header {{
                background: linear-gradient(to right, var(--primary-color), var(--secondary-color));
                color: white;
                padding: 30px;
                text-align: center;
            }}
            
            .header h1 {{
                margin-bottom: 10px;
                font-size: 2rem;
                opacity: 0;
                transform: translateY(-10px);
                animation: slideDown 0.5s ease 0.2s forwards;
            }}
            
            @keyframes slideDown {{
                to {{
                    opacity: 1;
                    transform: translateY(0);
                }}
            }}
            
            .header p {{
                font-size: 1rem;
                opacity: 0;
                animation: fadeIn 0.5s ease 0.4s forwards;
            }}
            
            .user-info {{
                padding: 20px;
                margin: 20px;
                background-color: var(--primary-light);
                border-radius: var(--border-radius);
                display: flex;
                flex-wrap: wrap;
                justify-content: space-between;
                align-items: center;
                box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
                opacity: 0;
                animation: fadeIn 0.5s ease 0.6s forwards;
            }}
            
            .user-profile {{
                display: flex;
                align-items: center;
                margin-bottom: 15px;
                flex: 1;
                min-width: 300px;
            }}
            
            .user-avatar {{
                width: 70px;
                height: 70px;
                background: linear-gradient(135deg, var(--primary-color), var(--secondary-color));
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                margin-right: 20px;
                font-size: 1.8rem;
                font-weight: bold;
                color: white;
                box-shadow: 0 4px 10px rgba(0, 0, 0, 0.15);
                border: 3px solid rgba(255, 255, 255, 0.2);
                transition: transform 0.3s ease, box-shadow 0.3s ease;
            }}
            
            .user-avatar:hover {{
                transform: scale(1.05);
                box-shadow: 0 6px 15px rgba(0, 0, 0, 0.2);
            }}
            
            .user-details {{
                flex-grow: 1;
            }}
            
            .user-name {{
                font-size: 1.6rem;
                font-weight: bold;
                display: flex;
                align-items: center;
                margin-bottom: 5px;
            }}
            
            .admin-badge {{
                background-color: var(--warning-color);
                color: white;
                font-size: 0.7rem;
                padding: 4px 10px;
                border-radius: 15px;
                margin-left: 10px;
                box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1);
                animation: pulse 2s infinite;
            }}
            
            @keyframes pulse {{
                0% {{ transform: scale(1); }}
                50% {{ transform: scale(1.05); }}
                100% {{ transform: scale(1); }}
            }}
            
            .user-id {{
                font-size: 0.95rem;
                color: var(--text-muted);
            }}
            
            .user-stats {{
                display: flex;
                flex-wrap: wrap;
                gap: 20px;
                flex: 1;
                min-width: 300px;
            }}
            
            .stat-item {{
                background: var(--container-bg);
                padding: 15px;
                border-radius: var(--border-radius);
                box-shadow: 0 4px 10px rgba(0, 0, 0, 0.1);
                flex: 1;
                min-width: 120px;
                text-align: center;
                transition: transform 0.3s ease, box-shadow 0.3s ease;
            }}
            
            .stat-item:hover {{
                transform: translateY(-5px);
                box-shadow: 0 8px 15px rgba(0, 0, 0, 0.15);
            }}
            
            .stat-value {{
                font-size: 1.6rem;
                font-weight: bold;
                color: var(--primary-color);
                margin-bottom: 5px;
            }}
            
            .stat-label {{
                font-size: 0.85rem;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }}
            
            .messages-container {{
                padding: 20px;
                margin: 20px;
                opacity: 0;
                animation: fadeIn 0.5s ease 0.8s forwards;
            }}
            
            .messages-header {{
                margin-bottom: 20px;
                border-bottom: 2px solid var(--primary-color);
                padding-bottom: 10px;
                position: relative;
            }}
            
            .messages-header:after {{
                content: '';
                position: absolute;
                bottom: -2px;
                left: 0;
                width: 100px;
                height: 2px;
                background-color: var(--secondary-color);
            }}
            
            .message {{
                margin-bottom: 25px;
                padding: 15px;
                border-radius: var(--border-radius);
                box-shadow: 0 3px 8px rgba(0, 0, 0, 0.1);
                position: relative;
                max-width: 80%;
                opacity: 0;
                animation: slideIn 0.5s ease forwards;
                animation-delay: calc(var(--i) * 0.1s + 1s);
            }}
            
            @keyframes slideIn {{
                from {{
                    opacity: 0;
                    transform: translateX(-20px);
                }}
                to {{
                    opacity: 1;
                    transform: translateX(0);
                }}
            }}
            
            .user {{
                background-color: var(--user-message-color);
                border-left: 4px solid var(--user-message-border);
                margin-left: auto;
                transform: translateX(20px);
                animation-name: slideInRight;
            }}
            
            @keyframes slideInRight {{
                from {{
                    opacity: 0;
                    transform: translateX(20px);
                }}
                to {{
                    opacity: 1;
                    transform: translateX(0);
                }}
            }}
            
            .assistant {{
                background-color: var(--assistant-message-color);
                border-left: 4px solid var(--assistant-message-border);
                margin-right: auto;
                transform: translateX(-20px);
                animation-name: slideIn;
            }}
            
            .message-header {{
                display: flex;
                justify-content: space-between;
                margin-bottom: 10px;
                font-size: 0.85rem;
                color: var(--text-muted);
                border-bottom: 1px solid var(--border-color);
                padding-bottom: 8px;
            }}
            
            .message-role {{
                font-weight: bold;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }}
            
            .message-time {{
                font-size: 0.8rem;
                color: var(--text-muted);
            }}
            
            .message-content {{
                white-space: pre-wrap;
                overflow-wrap: break-word;
                line-height: 1.5;
                font-size: 1rem;
            }}
            
            .no-messages {{
                text-align: center;
                padding: 40px;
                background-color: rgba(255, 248, 225, 0.2);
                border-radius: var(--border-radius);
                color: var(--warning-color);
                border: 1px dashed var(--warning-color);
                margin: 20px 0;
            }}
            
            .footer {{
                text-align: center;
                padding: 25px;
                background-color: var(--secondary-color);
                color: white;
                font-size: 0.9rem;
                position: relative;
                overflow: hidden;
            }}
            
            /* Аннимированные фигуры в футере */
            .footer:before, .footer:after {{
                content: '';
                position: absolute;
                width: 200px;
                height: 200px;
                border-radius: 50%;
                background: rgba(255, 255, 255, 0.05);
                z-index: 0;
            }}
            
            .footer:before {{
                left: -100px;
                top: -100px;
                animation: float 10s infinite ease-in-out;
            }}
            
            .footer:after {{
                right: -100px;
                bottom: -100px;
                animation: float 13s infinite ease-in-out reverse;
            }}
            
            @keyframes float {{
                0%, 100% {{ transform: translate(0, 0); }}
                25% {{ transform: translate(10px, 10px); }}
                50% {{ transform: translate(5px, -5px); }}
                75% {{ transform: translate(-10px, 5px); }}
            }}
            
            .footer p {{
                position: relative;
                z-index: 1;
            }}
        </style>
    </head>
    <body>
        <!--<button id="theme-toggle">Сменить тему</button>-->
        <div class="container">
            <div class="header">
                <h1>История чата пользователя</h1>
                <p>Экспортировано: {current_time}</p>
            </div>
            
            <div class="user-info">
                <div class="user-profile">
                    <div class="user-avatar">{(first_name[0] if first_name and first_name != "Неизвестно" else "?") + (last_name[0] if last_name else "")}</div>
                    <div class="user-details">
                        <div class="user-name">
                            {first_name} {last_name}
                            {f'<span class="admin-badge">АДМИН</span>' if is_admin else ''}
                        </div>
                        <div class="user-id">ID: {target_id} | @{username if username != "Нет" else "—"}</div>
                    </div>
                </div>
                
                <div class="user-stats">
                    <div class="stat-item">
                        <div class="stat-value">{user_data['total_requests']}</div>
                        <div class="stat-label">Всего запросов</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">{user_data['requests_left'] if not is_admin else "∞"}</div>
                        <div class="stat-label">Осталось запросов</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">{user_data['joined_at'].split(' ')[0]}</div>
                        <div class="stat-label">Дата регистрации</div>
                    </div>
                </div>
            </div>
            
            <div class="messages-container">
                <div class="messages-header">
                    <h2>История сообщений</h2>
                </div>
    """
    
    # Получаем и сохраняем историю переписки
    application = context.application
    app_user_data = application.user_data.get(target_id)
    
    if not app_user_data or 'history' not in app_user_data or not app_user_data['history']:
        html_content += """
                <div class="no-messages">
                    <h3>История сообщений пуста</h3>
                    <p>У этого пользователя нет истории сообщений или она недоступна.</p>
                </div>
        """
    else:
        # Добавляем каждое сообщение в HTML
        for i, message in enumerate(app_user_data['history']):
            role = message.get('role', 'unknown')
            content = message.get('content', '')
            
            # Генерируем фиктивное время сообщения, так как в реальных данных его может не быть
            message_time = datetime.datetime.now() - datetime.timedelta(minutes=(len(app_user_data['history'])-i)*10)
            time_str = message_time.strftime("%d.%m.%Y %H:%M:%S")
            
            if isinstance(content, list):  # Если это список (как с изображениями)
                text = "Изображение с подписью: "
                for item in content:
                    if item.get('type') == 'text':
                        text += item.get('text', '')
                content = text
            
            # Преобразуем текст для отображения в HTML (заменяем переносы строк и т.д.)
            if isinstance(content, str):
                # Экранируем HTML-символы для безопасного отображения
                content = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                # Заменяем переносы строк на HTML-теги <br>
                content = content.replace("\n", "<br>")
            
            html_content += f"""
                <div class="message {role}" style="--i: {i};">
                    <div class="message-header">
                        <span class="message-role">{role.upper()}</span>
                        <span class="message-time">{time_str}</span>
                    </div>
                    <div class="message-content">{content}</div>
                </div>
            """
    
    # Завершаем HTML-документ
    html_content += """
            </div>
            
            <div class="footer">
                <p>© Claude 3.7 Sonnet Telegram Bot</p>
            </div>
        </div>
        
        <!-- <script>
            // JavaScript для переключения темы (если понадобится)
            document.getElementById('theme-toggle').addEventListener('click', function() {
                document.body.classList.toggle('dark-theme');
                if (document.body.classList.contains('dark-theme')) {
                    this.textContent = '☀️ Светлая тема';
                } else {
                    this.textContent = '🌙 Темная тема';
                }
            });
        </script> -->
    </body>
    </html>
    """
    
    # Создаем файл с историей переписки
    filename = f"chat_history_{target_id}_{file_timestamp}.html"
    with open(filename, 'w', encoding='utf-8') as file:
        file.write(html_content)
    
    # Отправляем файл администратору
    with open(filename, 'rb') as file:
        await update.message.reply_document(
            document=file, 
            filename=filename,
            caption=f"История переписки пользователя {target_id} ({first_name})"
        )
    
    # Удаляем временный файл
    os.remove(filename)
    
    await update.message.reply_html(
        f"<b>✅ История переписки экспортирована</b>\n\n"
        f"История чата пользователя {target_id} ({first_name}) успешно выгружена.",
        reply_markup=get_profile_keyboard(admin_id)
    )
    logger.info(f"Администратор {admin_id} выгрузил историю переписки пользователя {target_id}")

async def export_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Экспорт списка всех пользователей в HTML формате."""
    admin_id = update.effective_user.id
    
    # Проверяем наличие пользователей
    if not USER_DATA:
        await update.message.reply_html(
            "<b>❌ Ошибка</b>\n\nВ базе нет пользователей.",
            reply_markup=get_profile_keyboard(admin_id)
        )
        return
    
    # Получаем текущую дату и время для файла
    current_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    file_timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # Создаем красивый HTML файл
    html_content = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Список пользователей бота Claude 3.7 Sonnet</title>
        <style>
            :root {{
                --primary-color: #4b6cb7;
                --primary-light: #e4ecff;
                --secondary-color: #182848;
                --success-color: #4CAF50;
                --danger-color: #f44336;
                --warning-color: #ff9800;
                --light-gray: #f5f5f5;
                --dark-gray: #333;
                --border-radius: 10px;
                --box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
                
                /* Светлая тема по умолчанию */
                --bg-color: linear-gradient(135deg, #f5f7fa 0%, #e4ecff 100%);
                --container-bg: white;
                --text-color: #333;
                --text-muted: #666;
                --border-color: #eee;
                --card-bg: white;
                --scrollbar-thumb: #c1c1c1;
                --scrollbar-track: #f1f1f1;
                
                --transition-speed: 0.3s;
            }}
            
            /* Темная тема */
            @media (prefers-color-scheme: dark) {{
                :root {{
                    --primary-color: #5d7dcf;
                    --primary-light: #1a2744;
                    --secondary-color: #0f1729;
                    --bg-color: linear-gradient(135deg, #111827 0%, #1e293b 100%);
                    --container-bg: #1f2937;
                    --text-color: #e2e8f0;
                    --text-muted: #9ca3af;
                    --border-color: #374151;
                    --card-bg: #1f2937;
                    --scrollbar-thumb: #4b5563;
                    --scrollbar-track: #1f2937;
                }}
            }}
            
            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
                transition: background-color var(--transition-speed) ease, 
                           color var(--transition-speed) ease,
                           border-color var(--transition-speed) ease,
                           box-shadow var(--transition-speed) ease;
            }}
            
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                line-height: 1.6;
                color: var(--text-color);
                background: var(--bg-color);
                padding: 20px;
                min-height: 100vh;
            }}
            
            /* Стилизация скроллбара */
            ::-webkit-scrollbar {{
                width: 10px;
            }}
            
            ::-webkit-scrollbar-track {{
                background: var(--scrollbar-track);
                border-radius: 10px;
            }}
            
            ::-webkit-scrollbar-thumb {{
                background: var(--scrollbar-thumb);
                border-radius: 10px;
            }}
            
            ::-webkit-scrollbar-thumb:hover {{
                background: var(--primary-color);
            }}
            
            .container {{
                max-width: 1200px;
                margin: 0 auto;
                background-color: var(--container-bg);
                border-radius: var(--border-radius);
                box-shadow: var(--box-shadow);
                overflow: hidden;
                opacity: 0;
                transform: translateY(20px);
                animation: fadeIn 0.5s ease forwards;
            }}
            
            @keyframes fadeIn {{
                to {{
                    opacity: 1;
                    transform: translateY(0);
                }}
            }}
            
            .header {{
                background: linear-gradient(to right, var(--primary-color), var(--secondary-color));
                color: white;
                padding: 35px;
                text-align: center;
                position: relative;
                overflow: hidden;
            }}
            
            .header::before, .header::after {{
                content: '';
                position: absolute;
                width: 300px;
                height: 300px;
                border-radius: 50%;
                background: rgba(255, 255, 255, 0.05);
                z-index: 0;
            }}
            
            .header::before {{
                top: -150px;
                left: -100px;
            }}
            
            .header::after {{
                bottom: -150px;
                right: -100px;
            }}
            
            .header h1 {{
                margin-bottom: 15px;
                font-size: 2.4rem;
                position: relative;
                z-index: 1;
                opacity: 0;
                transform: translateY(-10px);
                animation: slideDown 0.5s ease 0.2s forwards;
            }}
            
            @keyframes slideDown {{
                to {{
                    opacity: 1;
                    transform: translateY(0);
                }}
            }}
            
            .header p {{
                font-size: 1.2rem;
                position: relative;
                z-index: 1;
                opacity: 0;
                animation: fadeIn 0.5s ease 0.4s forwards;
            }}
            
            .stats-container {{
                background-color: var(--primary-light);
                padding: 25px;
                margin: 25px;
                border-radius: var(--border-radius);
                display: flex;
                justify-content: space-around;
                flex-wrap: wrap;
                gap: 20px;
                box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
                opacity: 0;
                animation: fadeIn 0.5s ease 0.6s forwards;
            }}
            
            .stat-card {{
                background-color: var(--card-bg);
                padding: 25px;
                border-radius: var(--border-radius);
                box-shadow: 0 4px 10px rgba(0, 0, 0, 0.1);
                flex: 1;
                min-width: 220px;
                text-align: center;
                transition: transform 0.3s ease, box-shadow 0.3s ease;
                position: relative;
                overflow: hidden;
            }}
            
            .stat-card::before {{
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 5px;
                background: linear-gradient(to right, var(--primary-color), var(--secondary-color));
            }}
            
            .stat-card:hover {{
                transform: translateY(-7px);
                box-shadow: 0 10px 20px rgba(0, 0, 0, 0.15);
            }}
            
            .stat-card h3 {{
                color: var(--text-color);
                margin-bottom: 15px;
                font-size: 1.4rem;
                position: relative;
            }}
            
            .stat-card .stat-value {{
                font-size: 2.5rem;
                font-weight: bold;
                color: var(--primary-color);
                margin-bottom: 10px;
                position: relative;
            }}
            
            .users-container {{
                padding: 25px;
                margin: 25px;
                opacity: 0;
                animation: fadeIn 0.5s ease 0.8s forwards;
            }}
            
            .users-header {{
                margin-bottom: 30px;
                border-bottom: 2px solid var(--primary-color);
                padding-bottom: 15px;
                position: relative;
            }}
            
            .users-header::after {{
                content: '';
                position: absolute;
                bottom: -2px;
                left: 0;
                width: 100px;
                height: 2px;
                background-color: var(--secondary-color);
            }}
            
            .user-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
                gap: 25px;
            }}
            
            .user-card {{
                background-color: var(--container-bg);
                border-radius: var(--border-radius);
                box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
                padding: 25px;
                transition: all 0.3s ease;
                border-left: 5px solid var(--primary-color);
                position: relative;
                overflow: hidden;
                opacity: 0;
                animation: fadeUp 0.5s ease forwards;
            }}
            
            @keyframes fadeUp {{
                from {{
                    opacity: 0;
                    transform: translateY(20px);
                }}
                to {{
                    opacity: 1;
                    transform: translateY(0);
                }}
            }}
            
            .user-card:hover {{
                transform: translateY(-7px) scale(1.02);
                box-shadow: 0 15px 30px rgba(0, 0, 0, 0.2);
            }}
            
            .user-card.admin {{
                border-left: 5px solid var(--warning-color);
            }}
            
            .user-card::before {{
                content: '';
                position: absolute;
                width: 200px;
                height: 200px;
                background: radial-gradient(circle, var(--primary-light) 0%, transparent 70%);
                opacity: 0.3;
                bottom: -100px;
                right: -100px;
                border-radius: 50%;
                z-index: 0;
                transition: all 0.5s ease;
            }}
            
            .user-card:hover::before {{
                transform: scale(1.2);
            }}
            
            .user-card.admin::before {{
                background: radial-gradient(circle, rgba(255, 152, 0, 0.2) 0%, transparent 70%);
            }}
            
            .user-header {{
                display: flex;
                align-items: center;
                margin-bottom: 20px;
                position: relative;
                z-index: 1;
            }}
            
            .user-avatar {{
                width: 60px;
                height: 60px;
                background: linear-gradient(135deg, var(--primary-color), var(--secondary-color));
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                margin-right: 20px;
                font-size: 1.6rem;
                font-weight: bold;
                color: white;
                box-shadow: 0 4px 10px rgba(0, 0, 0, 0.15);
                border: 3px solid rgba(255, 255, 255, 0.2);
                transition: transform 0.3s ease, box-shadow 0.3s ease;
            }}
            
            .user-card:hover .user-avatar {{
                transform: rotate(10deg) scale(1.1);
            }}
            
            .admin-badge {{
                background-color: var(--warning-color);
                color: white;
                font-size: 0.7rem;
                padding: 4px 10px;
                border-radius: 15px;
                margin-left: 10px;
                box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1);
                animation: pulse 2s infinite;
            }}
            
            @keyframes pulse {{
                0% {{ transform: scale(1); }}
                50% {{ transform: scale(1.05); }}
                100% {{ transform: scale(1); }}
            }}
            
            .user-info {{
                flex-grow: 1;
            }}
            
            .user-name {{
                font-weight: bold;
                font-size: 1.3rem;
                color: var(--text-color);
                display: flex;
                align-items: center;
                margin-bottom: 5px;
            }}
            
            .user-username {{
                color: var(--text-muted);
                font-size: 0.95rem;
            }}
            
            .user-details {{
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 15px;
                margin-top: 20px;
                position: relative;
                z-index: 1;
            }}
            
            .detail-item {{
                display: flex;
                flex-direction: column;
                background-color: rgba(0, 0, 0, 0.03);
                padding: 12px;
                border-radius: var(--border-radius);
                transition: all 0.3s ease;
            }}
            
            .user-card:hover .detail-item {{
                background-color: rgba(0, 0, 0, 0.05);
            }}
            
            .detail-label {{
                font-size: 0.75rem;
                color: var(--text-muted);
                margin-bottom: 5px;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }}
            
            .detail-value {{
                font-size: 1.1rem;
                font-weight: 500;
            }}
            
            .requests-left {{
                color: var(--success-color);
                font-weight: bold;
            }}
            
            .requests-low {{
                color: var(--danger-color);
                font-weight: bold;
            }}
            
            .footer {{
                text-align: center;
                padding: 30px;
                background-color: var(--secondary-color);
                color: white;
                position: relative;
                overflow: hidden;
            }}
            
            .footer::before, .footer::after {{
                content: '';
                position: absolute;
                width: 200px;
                height: 200px;
                border-radius: 50%;
                background: rgba(255, 255, 255, 0.05);
                z-index: 0;
            }}
            
            .footer::before {{
                left: -100px;
                top: -100px;
                animation: float 10s infinite ease-in-out;
            }}
            
            .footer::after {{
                right: -100px;
                bottom: -100px;
                animation: float 13s infinite ease-in-out reverse;
            }}
            
            @keyframes float {{
                0%, 100% {{ transform: translate(0, 0); }}
                25% {{ transform: translate(10px, 10px); }}
                50% {{ transform: translate(5px, -5px); }}
                75% {{ transform: translate(-10px, 5px); }}
            }}
            
            .footer p {{
                position: relative;
                z-index: 1;
                font-size: 1rem;
            }}
            
            /* Адаптивность для мобильных устройств */
            @media (max-width: 768px) {{
                .user-grid {{
                    grid-template-columns: 1fr;
                }}
                
                .stats-container {{
                    flex-direction: column;
                }}
                
                .header h1 {{
                    font-size: 1.8rem;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Список пользователей бота</h1>
                <p>Экспортировано: {current_time}</p>
            </div>
            
            <div class="stats-container">
                <div class="stat-card">
                    <h3>Всего пользователей</h3>
                    <div class="stat-value">{len(USER_DATA)}</div>
                </div>
                <div class="stat-card">
                    <h3>Активных пользователей</h3>
                    <div class="stat-value">{sum(1 for user_data in USER_DATA.values() if user_data['total_requests'] > 0)}</div>
                </div>
                <div class="stat-card">
                    <h3>Всего запросов</h3>
                    <div class="stat-value">{sum(user_data['total_requests'] for user_data in USER_DATA.values())}</div>
                </div>
            </div>
            
            <div class="users-container">
                <div class="users-header">
                    <h2>Детальная информация о пользователях</h2>
                </div>
                
                <div class="user-grid">
    """
    
    # Добавляем карточки пользователей, сначала администратора, затем остальных, отсортированных по количеству запросов
    sorted_users = sorted(USER_DATA.items(), key=lambda x: (x[0] != ADMIN_ID, -x[1]['total_requests']))
    
    for i, (user_id, user_data) in enumerate(sorted_users):
        first_name = user_data['first_name'] or "Неизвестно"
        last_name = user_data['last_name'] or ""
        username = user_data['username'] or "Нет"
        is_admin = user_id == ADMIN_ID
        
        # Создаем инициалы для аватара
        initials = (first_name[0] if first_name and first_name != "Неизвестно" else "?") + (last_name[0] if last_name else "")
        
        # Определяем класс для оставшихся запросов (низкий/нормальный)
        requests_class = "requests-low" if user_data['requests_left'] < 3 and not is_admin else "requests-left"
        
        # Форматируем дату сброса
        reset_date = datetime.datetime.fromtimestamp(user_data['reset_time']).strftime("%d.%m.%Y")
        
        # Анимация появления карточки пользователя с задержкой
        animation_delay = min(i * 0.1, 2) # Максимальная задержка 2 секунды
        
        html_content += f"""
                    <div class="user-card{' admin' if is_admin else ''}" style="animation-delay: {animation_delay}s">
                        <div class="user-header">
                            <div class="user-avatar">{initials.upper()}</div>
                            <div class="user-info">
                                <div class="user-name">
                                    {first_name} {last_name}
                                    {f'<span class="admin-badge">АДМИН</span>' if is_admin else ''}
                                </div>
                                <div class="user-username">@{username if username != "Нет" else "—"}</div>
                            </div>
                        </div>
                        <div class="user-details">
                            <div class="detail-item">
                                <div class="detail-label">ID</div>
                                <div class="detail-value">{user_id}</div>
                            </div>
                            <div class="detail-item">
                                <div class="detail-label">Дата регистрации</div>
                                <div class="detail-value">{user_data['joined_at'].split(' ')[0]}</div>
                            </div>
                            <div class="detail-item">
                                <div class="detail-label">Всего запросов</div>
                                <div class="detail-value">{user_data['total_requests']}</div>
                            </div>
                            <div class="detail-item">
                                <div class="detail-label">Осталось запросов</div>
                                <div class="detail-value {requests_class}">
                                    {user_data['requests_left'] if not is_admin else "∞"}
                                </div>
                            </div>
                            <div class="detail-item" style="grid-column: span 2;">
                                <div class="detail-label">Сброс лимита</div>
                                <div class="detail-value">{reset_date if not is_admin else "Не ограничен"}</div>
                            </div>
                        </div>
                    </div>
        """
    
    # Завершаем HTML-документ
    html_content += """
                </div>
            </div>
            
            <div class="footer">
                <p>© Claude 3.7 Sonnet Telegram Bot</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    # Создаем файл
    filename = f"users_list_{file_timestamp}.html"
    with open(filename, 'w', encoding='utf-8') as file:
        file.write(html_content)
    
    # Отправляем файл администратору
    with open(filename, 'rb') as file:
        await update.message.reply_document(
            document=file, 
            filename=filename,
            caption=f"Список пользователей бота (всего: {len(USER_DATA)})"
        )
    
    # Удаляем временный файл
    os.remove(filename)
    
    await update.message.reply_html(
        f"<b>✅ Список пользователей экспортирован</b>\n\n"
        f"Всего пользователей: {len(USER_DATA)}\n"
        f"Активных пользователей: {sum(1 for user_data in USER_DATA.values() if user_data['total_requests'] > 0)}",
        reply_markup=get_profile_keyboard(admin_id)
    )
    
    logger.info(f"Администратор {admin_id} выгрузил список всех пользователей")

# --- Основная функция запуска ---

def main() -> None:
    """Запуск бота."""
    print("\n" + "★" * 60)
    print("★    Запуск Телеграм бота на базе Claude 3.7 Sonnet    ★")
    print("★" * 60 + "\n")
    
    logger.info("Инициализация приложения...")
    
    try:
        # Загружаем данные пользователей
        load_user_data()
        
        # Загружаем данные использования API
        load_api_usage()
        
        # Явно отключаем JobQueue для обхода ошибки 'weak reference'
        # Настраиваем параметры для параллельной обработки запросов
        builder = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN) \
            .job_queue(None) \
            .concurrent_updates(True) \
            .connection_pool_size(8) \
            .get_updates_connection_pool_size(16) \
            .pool_timeout(API_TIMEOUT)
            
        logger.info("Создание приложения с настройками параллельной обработки")
        application = builder.build()

        # Регистрация обработчиков
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("clear", clear_command))
        application.add_handler(CommandHandler("profile", profile_command))
        application.add_handler(CommandHandler("reset_limits", reset_limits_command))
        application.add_handler(CommandHandler("stats", stats_command))
        application.add_handler(CommandHandler("admin_panel", admin_panel_command))
        application.add_handler(CommandHandler("api_balance", api_balance_command))
        application.add_handler(MessageHandler(filters.PHOTO & (~filters.COMMAND), handle_photo_message))
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text_message))

        print("┌" + "─" * 50 + "┐")
        print("│" + " " * 15 + "СТАТУС СИСТЕМЫ" + " " * 16 + "│")
        print("├" + "─" * 50 + "┤")
        print("│ ✅ Бот успешно инициализирован                  │")
        print("│ ✅ Обработчики команд зарегистрированы          │")
        print("│ ✅ Поддержка изображений активна                │")
        print("│ ✅ Защита от одновременных запросов активна     │")
        print("│ ✅ Параллельная обработка запросов активна      │")
        print("│ ⚙️  Запуск бота в режиме опроса...              │")
        print("└" + "─" * 50 + "┘\n")
        
        logger.info("Бот запущен в режиме опроса с параллельной обработкой запросов")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

    except Exception as e:
        print("\n" + "⚠️ " * 10)
        print("❌ КРИТИЧЕСКАЯ ОШИБКА ПРИ ЗАПУСКЕ БОТА:")
        print(f"❌ {e}")
        print("⚠️ " * 10 + "\n")
        logger.critical(f"Критическая ошибка: {e}", exc_info=True)

# Добавляем новую команду для проверки баланса API
async def api_balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверка баланса и использования API (только для администратора)."""
    user_id = update.effective_user.id
    
    # Проверяем, является ли пользователь администратором
    if user_id != ADMIN_ID:
        await update.message.reply_html(
            "<b>⛔ Доступ запрещен</b>\n\nЭта команда доступна только администратору.",
            reply_markup=get_profile_keyboard(user_id)
        )
        return
    
    # Отправляем сообщение о начале проверки баланса
    processing_msg = await update.message.reply_html(
        "<i>🔄 Получение данных о балансе API, пожалуйста, подождите...</i>"
    )
    
    # Пытаемся получить реальные данные от LangDock API
    langdock_data = await get_langdock_usage()
    
    # Загружаем также локальные данные для сравнения или использования как резервный вариант
    load_api_usage()
    
    # Определяем, какие данные использовать
    if langdock_data:
        # Если удалось получить данные от API, используем их
        try:
            # Обработка данных в зависимости от структуры ответа API
            # Это примерная структура, нужно адаптировать под реальный ответ API
            if 'usage' in langdock_data:
                tokens_used = langdock_data.get('usage', {}).get('total_tokens', API_USAGE['total_tokens'])
                cost = langdock_data.get('usage', {}).get('cost', API_USAGE['total_cost'])
                requests_count = langdock_data.get('usage', {}).get('requests', API_USAGE['queries_count'])
                
                # Обновляем локальные данные для синхронизации
                API_USAGE['total_tokens'] = tokens_used
                API_USAGE['total_cost'] = cost
                API_USAGE['queries_count'] = requests_count
                API_USAGE['last_update'] = time.time()
                save_api_usage()
                
                # Логируем полученные данные
                logger.info(f"Данные API с LangDock: запросы={requests_count}, токены={tokens_used}, стоимость={cost}")
                using_real_data = True
            else:
                # Если структура не соответствует ожидаемой, используем локальные данные
                logger.warning(f"Неожиданная структура данных от LangDock API: {langdock_data}")
                using_real_data = False
        except Exception as e:
            logger.error(f"Ошибка обработки данных от LangDock API: {e}")
            using_real_data = False
    else:
        # Если данные от API получить не удалось, используем локальные данные
        logger.warning("Не удалось получить данные от LangDock API, используются локальные данные")
        using_real_data = False
    
    # Удаляем сообщение о загрузке
    await processing_msg.delete()
        
    # Форматируем данные о расходах API
    remaining_balance = INITIAL_API_BALANCE - API_USAGE['total_cost']
    percentage_used = (API_USAGE['total_cost'] / INITIAL_API_BALANCE) * 100 if INITIAL_API_BALANCE > 0 else 0
    
    # Оформляем красивый вывод
    data_source = "🌐 Данные получены с сервера LangDock" if using_real_data else "📊 Данные рассчитаны локально"
    
    balance_message = (
        f"<b>💰 БАЛАНС API LANGDOCK</b>\n\n"
        f"{data_source}\n\n"
        f"<b>┌───────────────────────────</b>\n"
        f"<b>│ 💳 Начальный баланс:</b> {INITIAL_API_BALANCE:.2f} €\n"
        f"<b>│ 📉 Использовано:</b> {API_USAGE['total_cost']:.2f} € ({percentage_used:.1f}%)\n"
        f"<b>│ 📈 Осталось:</b> {remaining_balance:.2f} €\n"
        f"<b>└───────────────────────────</b>\n\n"
        
        f"<b>📊 СТАТИСТИКА ИСПОЛЬЗОВАНИЯ</b>\n\n"
        f"<b>┌───────────────────────────</b>\n"
        f"<b>│ 🔢 Всего запросов:</b> {API_USAGE['queries_count']}\n"
        f"<b>│ 🔠 Всего токенов:</b> {API_USAGE['total_tokens']}\n"
    )
    
    # Добавляем среднюю стоимость запроса (избегаем деления на ноль)
    if API_USAGE['queries_count'] > 0:
        avg_cost = API_USAGE['total_cost'] / API_USAGE['queries_count']
        balance_message += f"<b>│ 💱 Средняя стоимость запроса:</b> {avg_cost:.4f} €\n"
    else:
        balance_message += f"<b>│ 💱 Средняя стоимость запроса:</b> 0.0000 €\n"
    
    # Добавляем индикатор использования баланса
    filled_blocks = int(percentage_used / 10) if percentage_used > 0 else 0
    balance_bar = "▰" * filled_blocks + "▱" * (10 - filled_blocks)
    
    # Выбираем эмодзи индикатора баланса
    if percentage_used < 50:
        balance_emoji = "🟢"  # Более половины баланса
    elif percentage_used < 80:
        balance_emoji = "🟡"  # Менее половины баланса
    else:
        balance_emoji = "🔴"  # Осталось мало средств
        
    # Последнее обновление статистики
    last_update = datetime.datetime.fromtimestamp(API_USAGE['last_update']).strftime("%d.%m.%Y %H:%M:%S")
    
    balance_message += (
        f"<b>│ 📅 Последнее обновление:</b> {last_update}\n"
        f"<b>└───────────────────────────</b>\n\n"
        
        f"<b>📋 ИСПОЛЬЗОВАНИЕ БАЛАНСА</b>\n\n"
        f"{balance_emoji} {balance_bar} {percentage_used:.1f}%\n\n"
    )
    
    # Добавляем предупреждение, если баланс подходит к концу
    if percentage_used > 80:
        balance_message += (
            f"<b>⚠️ ВНИМАНИЕ!</b>\n"
            f"Баланс API почти исчерпан. Рекомендуется пополнить счет или ограничить использование бота."
        )
    
    # Если данные были рассчитаны локально, добавляем примечание
    if not using_real_data:
        balance_message += (
            f"\n\n<i>⚠️ Примечание: Не удалось получить данные напрямую с сервера LangDock. "
            f"Отображаемая информация основана на локальном учете использования API.</i>"
        )
    
    await update.message.reply_html(balance_message, reply_markup=get_profile_keyboard(user_id))
    logger.info(f"Администратор {user_id} запросил информацию о балансе API ({data_source})")

# Обновленная функция для запроса информации об использовании API напрямую с LangDock
async def get_langdock_usage():
    """Запрашивает данные об использовании API напрямую с сервера LangDock."""
    try:
        headers = {"Authorization": f"Bearer {LANGDOCK_API_KEY}", "Content-Type": "application/json"}
        
        # Определяем период для запроса (начало месяца до текущей даты)
        now = datetime.datetime.now()
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # Форматируем даты для API запроса
        start_date = start_of_month.strftime("%Y-%m-%d")
        end_date = now.strftime("%Y-%m-%d")
        
        params = {
            "start_date": start_date,
            "end_date": end_date
        }
        
        logger.info(f"Запрос данных использования API LangDock за период: {start_date} - {end_date}")
        
        async with aiohttp.ClientSession() as session:
            try:
                # Пробуем сначала запросить через предполагаемый API биллинга
                async with session.get(LANGDOCK_BILLING_API_URL, headers=headers, params=params, timeout=API_TIMEOUT) as response:
                    if response.status == 200:
                        usage_data = await response.json()
                        logger.info(f"Успешно получены данные использования API LangDock: {usage_data}")
                        return usage_data
                    elif response.status == 404:
                        # Если API биллинга не найден, пробуем получить данные через альтернативный метод
                        logger.warning("API биллинга LangDock не найден, пробуем альтернативный метод")
                        return await get_langdock_usage_alternative()
                    else:
                        error_text = await response.text()
                        logger.error(f"Ошибка LangDock API: {response.status} - {error_text}")
                        return None
            except aiohttp.ClientError as e:
                logger.error(f"Ошибка сети при запросе данных использования API: {e}")
                return await get_langdock_usage_alternative()
                
    except Exception as e:
        logger.exception(f"Ошибка при запросе данных использования API: {e}")
        return None

async def get_langdock_usage_alternative():
    """Альтернативный метод получения данных об использовании API через другие доступные эндпоинты."""
    try:
        headers = {"Authorization": f"Bearer {LANGDOCK_API_KEY}", "Content-Type": "application/json"}
        
        # Попытка получить информацию через другие потенциальные эндпоинты
        potential_endpoints = [
            "https://api.langdock.com/account/v1/usage",
            "https://api.langdock.com/v1/dashboard/usage",
            "https://api.langdock.com/eu/v1/usage"
        ]
        
        for endpoint in potential_endpoints:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(endpoint, headers=headers, timeout=API_TIMEOUT) as response:
                        if response.status == 200:
                            usage_data = await response.json()
                            logger.info(f"Успешно получены данные через альтернативный эндпоинт {endpoint}: {usage_data}")
                            return usage_data
            except Exception:
                continue
                
        logger.warning("Не удалось получить данные через альтернативные эндпоинты, возвращаем локальные данные")
        return None
        
    except Exception as e:
        logger.exception(f"Ошибка при использовании альтернативного метода: {e}")
        return None

if __name__ == "__main__":
    main()
