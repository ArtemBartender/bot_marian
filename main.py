import asyncio
import json
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.contrib.middlewares.i18n import I18nMiddleware
from aiogram.dispatcher import FSMContext
from dotenv import load_dotenv
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           ReplyKeyboardRemove)
from groq import Groq

# --- Конфигурация ---
try:
    from config import ADMIN_ID, BOT_TOKEN, GROQ_API_KEY
except ImportError:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_ID = os.getenv("ADMIN_ID")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not all([BOT_TOKEN, ADMIN_ID, GROQ_API_KEY]):
    raise ValueError(
        "One or more required configurations (BOT_TOKEN, ADMIN_ID, GROQ_API_KEY) are missing."
    )

# --- Настройка ---
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# --- i18n (Интернационализация) ---
BASE_DIR = Path(__file__).resolve().parent
locales_path = os.path.join(BASE_DIR, 'locales')
i18n = I18nMiddleware('messages', locales_path, default='ru')
_ = i18n.gettext
dp.middleware.setup(i18n)

# --- FSM Состояния ---
class Conversation(StatesGroup):
    waiting_for_language = State()
    active = State()
    confirmation = State()

# --- Клиент Groq и AI ---
client = Groq(api_key=GROQ_API_KEY)

def get_system_prompt(lang_code: str) -> str:
    lang_map = {'ru': 'русском', 'en': 'английском', 'pl': 'польском'}
    language_name = lang_map.get(lang_code, 'русском')
    
    return f"""
Ты — "Smoky", дружелюбный и профессиональный AI-ассистент сервиса кальянного кейтеринга.
Твоя главная задача — в естественном диалоге помочь пользователю оформить заказ.

Тебе НЕОБХОДИМО собрать следующую информацию:
1. arrival_time: Время и дата прибытия.
2. duration_hours: Продолжительность мероприятия в часах.
3. hookah_masters_count: Количество кальянных мастеров.
4. hookahs_count: Количество кальянов.
5. location: Полный адрес мероприятия.
6. phone_number: Контактный номер телефона пользователя.

Веди диалог естественно. Не задавай все вопросы сразу.

КРАЙНЕ ВАЖНО: Как только ты соберешь ВСЮ необходимую информацию, ты ДОЛЖЕН вызвать функцию `create_hookah_order`.

САМОЕ ГЛАВНОЕ ПРАВИЛО: Ты ОБЯЗАН общаться с пользователем СТРОГО на {language_name} языке.
Не используй ни одного слова из других языков. Это твое самое важное правило.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_hookah_order",
            "description": "Создает заказ на кальянный кейтеринг после сбора всей необходимой информации от пользователя.",
            "parameters": {
                "type": "object",
                "properties": {
                    "arrival_time": {"type": "string", "description": "Время и дата прибытия, например, 'завтра в 20:00'."},
                    "duration_hours": {"type": "number", "description": "Продолжительность мероприятия в часах, например, 4."},
                    "hookah_masters_count": {"type": "integer", "description": "Количество необходимых кальянных мастеров."},
                    "hookahs_count": {"type": "integer", "description": "Количество необходимых кальянов."},
                    "location": {"type": "string", "description": "Полный адрес проведения мероприятия."},
                    "phone_number": {"type": "string", "description": "Контактный номер телефона пользователя."}
                },
                "required": ["arrival_time", "duration_hours", "hookah_masters_count", "hookahs_count", "location", "phone_number"]
            }
        }
    }
]

# --- Обработчики команд ---
@dp.message_handler(commands=['start'], state='*')
async def cmd_start(message: types.Message, state: FSMContext):
    await state.finish()
    keyboard = InlineKeyboardMarkup(row_width=3)
    keyboard.add(
        InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
        InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        InlineKeyboardButton("🇵🇱 Polski", callback_data="lang_pl")
    )
    await message.answer(
        "Please choose your language / Пожалуйста, выберите язык / Proszę wybrać język:",
        reply_markup=keyboard
    )
    await Conversation.waiting_for_language.set()

@dp.callback_query_handler(lambda c: c.data.startswith('lang_'), state=Conversation.waiting_for_language)
async def process_language_callback(callback: types.CallbackQuery, state: FSMContext):
    """
    ИСПРАВЛЕНО: Обрабатывает нажатие на кнопку и УДАЛЯЕТ сообщение с выбором языка.
    """
    lang_code = callback.data.split('_')[1]
    
    # Удаляем сообщение с кнопками выбора языка
    await callback.message.delete()
    await callback.answer()

    i18n.ctx_locale.set(lang_code)
    
    system_prompt = get_system_prompt(lang_code)
    await state.update_data(
        lang=lang_code,
        history=[{"role": "system", "content": system_prompt}]
    )
    
    await Conversation.active.set()
    # Отправляем приветственное сообщение как новое, т.к. старое удалили
    await bot.send_message(
        chat_id=callback.from_user.id,
        text=_("welcome_message"),
        reply_markup=ReplyKeyboardRemove()
    )

# --- Основной обработчик диалога ---
@dp.message_handler(state=Conversation.active)
async def handle_conversation(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    history = user_data.get("history", [])
    lang = user_data.get("lang", "ru")

    lang_reminders = {
        'ru': '(Напоминание: отвечай только на русском языке)',
        'en': '(Reminder: reply in English only)',
        'pl': '(Przypomnienie: odpowiadaj tylko po polsku)'
    }
    user_message_with_reminder = f"{message.text} {lang_reminders.get(lang, '')}"
    history.append({"role": "user", "content": user_message_with_reminder})

    thinking_message = await message.answer(_("thinking_message"))

    try:
        response = client.chat.completions.create(
            model="llama3-70b-8192",
            messages=history,
            tools=TOOLS,
            tool_choice="auto"
        )
        response_message = response.choices[0].message

        tool_calls = response_message.tool_calls
        if tool_calls:
            await handle_tool_call(message, state, tool_calls[0])
            await bot.delete_message(chat_id=message.chat.id, message_id=thinking_message.message_id)
        else:
            ai_response_text = response_message.content
            history.append({"role": "assistant", "content": ai_response_text})
            await state.update_data(history=history)
            await bot.edit_message_text(ai_response_text, chat_id=message.chat.id, message_id=thinking_message.message_id)

    except asyncio.TimeoutError:
        logging.warning("Groq API request timed out.")
        await bot.edit_message_text(_("timeout_error_message"), chat_id=message.chat.id, message_id=thinking_message.message_id)
        history.pop()
        await state.update_data(history=history)
    except Exception as e:
        logging.error(f"Error during AI conversation: {e}")
        await bot.edit_message_text(_("error_message"), chat_id=message.chat.id, message_id=thinking_message.message_id)
        history.pop()
        await state.update_data(history=history)


async def handle_tool_call(message: types.Message, state: FSMContext, tool_call):
    if tool_call.function.name == "create_hookah_order":
        arguments = json.loads(tool_call.function.arguments)
        await state.update_data(order_details=arguments)

        user_link = f"@{message.from_user.username}" if message.from_user.username else f"tg://user?id={message.from_user.id}"
        
        summary = (
            f"🔍 **{_('summary_title')}**\n\n"
            f"**{_('summary_when')}:** {arguments.get('arrival_time')}\n"
            f"**{_('summary_duration')}:** {arguments.get('duration_hours')} ч.\n"
            f"**{_('summary_hookahs')}:** {arguments.get('hookahs_count')} шт.\n"
            f"**{_('summary_masters')}:** {arguments.get('hookah_masters_count')} чел.\n"
            f"**{_('summary_where')}:** {arguments.get('location')}\n"
            f"**{_('summary_phone')}:** {arguments.get('phone_number')}\n\n"
            f"**{_('summary_client')}:** {user_link}"
        )

        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton(f"✅ {_('button_confirm')}", callback_data="confirm_order"),
            InlineKeyboardButton(f"✏️ {_('button_edit')}", callback_data="edit_order")
        )

        await message.answer(summary, reply_markup=keyboard, parse_mode="Markdown")
        await Conversation.confirmation.set()

# --- Обработчики кнопок подтверждения ---
@dp.callback_query_handler(lambda c: c.data == 'confirm_order', state=Conversation.confirmation)
async def process_confirm_order(callback: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    order_details = user_data.get("order_details")

    user = callback.from_user
    user_link = f"@{user.username}" if user.username else f"tg://user?id={user.id}"
    admin_summary = (
        f"📩 **Новый заказ!**\n\n"
        f"Когда: {order_details.get('arrival_time')}\n"
        f"На сколько: {order_details.get('duration_hours')} ч.\n"
        f"Кальяны: {order_details.get('hookahs_count')} шт.\n"
        f"Мастера: {order_details.get('hookah_masters_count')} чел.\n"
        f"Куда: {order_details.get('location')}\n"
        f"Телефон: {order_details.get('phone_number')}\n\n"
        f"Клиент: {user_link}"
    )

    await bot.send_message(ADMIN_ID, admin_summary, parse_mode="Markdown")
    
    await callback.message.edit_text(_("confirmation_thanks_message"), parse_mode="Markdown")
    await callback.answer()
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'edit_order', state=Conversation.confirmation)
async def process_edit_order(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup()
    await callback.answer(_('edit_callback_answer'))
    
    user_data = await state.get_data()
    history = user_data.get("history", [])
    history.append({
        "role": "assistant",
        "content": "Пользователь хочет внести изменения в заказ. Уточни, что именно нужно поменять."
    })
    await state.update_data(history=history)
    
    await callback.message.answer(_('edit_prompt_message'))
    await Conversation.active.set()

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
