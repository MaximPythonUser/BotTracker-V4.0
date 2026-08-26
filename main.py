import os
import asyncio
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv
import aiosqlite
from aiogram.fsm.state import State, StatesGroup
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from sqlalchemy import create_engine
from pytz import timezone as pytz_timezone
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from aiogram.utils.keyboard import InlineKeyboardBuilder

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


file_handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s:%(name)s:%(levelname)s: %(message)s'))

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s:%(name)s:%(levelname)s: %(message)s'))


logger.handlers.clear()
logger.addHandler(file_handler)
logger.addHandler(console_handler)


apscheduler_logger = logging.getLogger('apscheduler')
apscheduler_logger.setLevel(logging.DEBUG)
apscheduler_logger.addHandler(file_handler)
apscheduler_logger.addHandler(console_handler)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logger.error("BOT_TOKEN не найден в файле .env")
    raise ValueError("BOT_TOKEN не найден в .env")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB_PATH = "training.db"
moscow_tz = pytz_timezone("Europe/Moscow")


engine = create_engine(f"sqlite:///{DB_PATH}")
jobstore = {"default": SQLAlchemyJobStore(engine=engine)}


scheduler = AsyncIOScheduler(
    jobstores=jobstore,
    timezone=moscow_tz,
    misfire_grace_time=1800)

class EditState(StatesGroup):
    waiting_for_date = State()
    waiting_for_name = State()
    waiting_for_time = State()

class AddTrainingState(StatesGroup):
    waiting_for_date = State()
    waiting_for_name = State()
    waiting_for_time = State()

def get_main_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить тренировку", callback_data="start_add_flow")
    builder.button(text="🗓 Мои тренировки", callback_data="show_my_trainings")
    builder.adjust(1)
    return builder.as_markup()

def get_post_trainings_keyboard(training_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Прошла", callback_data=f"train_done_{training_id}")
    builder.button(text="🔄 Перенести", callback_data=f"train_reschedule_{training_id}")
    builder.button(text="🗑 Удалить", callback_data=f"train_delete_{training_id}")
    builder.adjust(1, 2)
    return builder.as_markup()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         CREATE TABLE IF NOT EXISTS one_off_trainings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                training_name TEXT NOT NULL,
                start_time TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
                         )
                         """)

        cursor = await db.execute("PRAGMA table_info(one_off_trainings)")
        columns = [col[1] for col in await cursor.fetchall()]

        if 'status' not in columns:
            await db.execute("ALTER TABLE one_off_trainings ADD COLUMN status TEXT")
            logger.info("🆕 Колонка 'status' успешно добавлена в базу данных.")
        else:
            logger.debug("📋 Колонка 'status' уже существует.")
        await db.execute("UPDATE one_off_trainings SET status = 'pending' WHERE status IS NULL")

        await db.commit()

    logger.info("✅ База данных готова (статусы проверены и нормализованы).")

async def add_one_off_training(user_id: int, date: str, name: str, time: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO one_off_trainings (user_id, date, training_name, start_time) VALUES (?, ?, ?, ?)""", (user_id, date, name, time)
        )

        await db.commit()  #commit - Сохранить изменения. Без этого запись не появится в базе.

async def send_reminder(user_id: int, training_name: str, date: str, time: str, training_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT status FROM one_off_trainings WHERE id = ?", (training_id,))
        row = await cursor.fetchone()

    job_id = f"reminder_{training_id}"


    if not row:
        logger.warning(f"⚠️ Тренировка {training_id} не найдена в БД — напоминание пропущено.")
        try:
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id, jobstore="default")
                logger.info(f"🗑 Удалена мёртвая задача из планировщика и БД: {job_id}")
        except Exception as e:
            logger.error(f"Ошибка при удалении задачи {job_id}: {e}")
        return

    status = row["status"]

    if status in ("done", "missed"):
        logger.debug(f"📋 Статус тренировки {training_id}: '{status}'. Напоминание не нужно.")
        try:
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id, jobstore="default")
                logger.info(f"🗑 Удалена неактуальная задача: {job_id}")
        except Exception as e:
            logger.error(f"Ошибка при удалении задачи {job_id}: {e}")
        return

    logger.info(f"🔔 ОТПРАВЛЕНО НАПОМИНАНИЕ: Тренировка '{training_name}' ({date} {time}) для пользователя {user_id}")

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⏰ <b>Напоминание!</b>\n\n"
                f"У тебя сегодня тренировка: <b>{training_name}</b>\n"
                f"📅 Дата: {date}\n"
                f"🕒 Время: {time}\n\n"
                f"Готовься к рекордам! 💪"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Не удалось отправить напоминание пользователю {user_id}: {e}")


async def schedule_existing_reminders():
    """Восстанавливает задачи при старте бота"""
    logger.info("🔄 Восстановление расписания из базы данных...")
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM one_off_trainings")
            rows = await cursor.fetchall()

        now_dt = datetime.now(moscow_tz).replace(tzinfo=None)
        count = 0

        for row in rows:
            train_datetime_str = f"{row['date']} {row['start_time']}"
            try:
                train_datetime = datetime.strptime(train_datetime_str, "%Y-%m-%d %H:%M")
            except ValueError:
                logger.warning(f"⚠️ Пропущена тренировка с некорректной датой/временем: ID {row['id']}")
                continue

            if train_datetime + timedelta(hours=1) <= now_dt:
                continue

            training_id = row['id']
            count += 1

            reminder_time = train_datetime - timedelta(minutes=20)
            check_time = train_datetime + timedelta(hours=1)


            if reminder_time > now_dt:
                job_id = f"reminder_{training_id}"
                if not scheduler.get_job(job_id):
                    scheduler.add_job(
                        send_reminder,
                        trigger="date",
                        run_date=reminder_time,
                        args=[row["user_id"], row["training_name"], row["date"], row["start_time"], row["id"]],
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(f"✅ Восстановлено напоминание для ID {training_id} на {reminder_time}")

            # Планируем проверку статуса (если ещё не прошло и задачи нет)
            if check_time > now_dt:
                job_id = f"check_{training_id}"
                if not scheduler.get_job(job_id):
                    scheduler.add_job(
                        check_training_status,
                        trigger="date",
                        run_date=check_time,
                        args=[training_id, row["user_id"], row["training_name"], row["date"], row["start_time"]],
                        id=job_id,
                        replace_existing=True
                    )
                    logger.info(f"✅ Восстановлена проверка статуса для ID {training_id} на {check_time}")

        logger.info(f"✅ Восстановлено расписание для {count} активных тренировок (напоминания и проверки).")
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка при восстановлении расписания: {e}")

async def check_training_status(training_id: int, user_id: int, name: str, date: str, time: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT status FROM one_off_trainings WHERE id = ?", (training_id,))
        row = await cursor.fetchone()

        if row and row["status"] in ("done", "missed"):
            logger.debug(f"Тренировка ID={training_id} уже завершена, запрос о статусе не нужен.")
            try:
                job_id = f"check_{training_id}"
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id, jobstore="default")
                    logger.info(f"🗑 Автоматически удалена проверка статуса для завершённой тренировки {job_id}")
            except Exception as e:
                logger.error(f"Ошибка удаления check-задачи: {e}")
            return

    logger.debug(f"⏰ Время проверки статуса для тренировки ID {training_id}")

    try:
        await bot.send_message(
            chat_id=user_id,
            text=f"🕒 Час после тренировки прошел. Как прошла тренировка «{name}» ({date} {time})?",
reply_markup=get_post_trainings_keyboard(training_id)
        )
    except Exception as e:
        logger.error(f"❌ Не удалось отправить запрос о статусе пользователю {user_id}: {e}")

@dp.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(AddTrainingState.waiting_for_date)
    await state.update_data(user_id=message.from_user.id)

    calendar = SimpleCalendar(locale="ru")
    await message.answer(
        "📅 Выбери дату тренировки:",
        reply_markup=await calendar.start_calendar()
    )


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
       f"👋 Привет, {message.from_user.first_name}! Я твой трекер тренировок.\n\n"
        "Вот что я умею:\n\n"
        "📅 /my_trainings — посмотреть список своих тренировок.\n"
        "➕ /add ГГГГ-ММ-ДД Название Время — добавить новую тренировку.\n"
        "🗑️ /delete <номер> — удалить тренировку по номеру из списка.\n"
        "✏️ /edit — выбрать тренировку и отредактировать её через кнопки (дата, название, время).\n\n"  
        "💡 Подсказка: формат даты — 2026-08-25, время — 19:00 (24 часа)."
    )
    await message.answer(welcome_text)


@dp.message(Command("my_trainings"))
async def cmd_my_trainings(message: types.Message):
    user_id = message.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row  # Теперь можно обращаться к колонкам по имени, а не по индексу
        cursor = await db.execute(
            "SELECT id, date, training_name, start_time FROM one_off_trainings WHERE user_id = ? ORDER BY id",
            (user_id,)
        )
        rows = await cursor.fetchall()

    if not rows:
        await message.answer("У тебя пока нет запланированных тренировок")
        return

    text = "🗓 Твои тренировки:\n\n"
    for i, row in enumerate(rows, start=1):
        # enumerate(rows, start=1) - нумерация списка. берет каждую строку из базы данных и дает ей номер: 1, 2, 3
        # i - непосредственно номер.

        text += f"№{i}. 📅 {row['date']} | 🏋️ {row['training_name']} | ⏰ {row['start_time']}\n"
    await message.answer(text)


@dp.message(Command("edit"))
async def cmd_edit_start(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id,date, training_name, start_time FROM one_off_trainings WHERE user_id = ? ORDER BY id",
            (user_id,))
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("У тебя нет тренировок для редактирования.")
        return
    text = "🗓 Выбери тренировку для редактирования:\n\n"

    builder = InlineKeyboardBuilder()

    for i, row in enumerate(rows, 1):
        text += (
            f"🏋️ <b>{row['training_name']}</b>\n"
            f"📅 {row['date']} | ⏰ {row['start_time']}\n"
            f"--------------------------\n")
        prefix = f"edit_{row['id']}_"

        builder.button(text="📅 Изменить дату", callback_data=prefix + "date")
        builder.button(text="📝 Переименовать", callback_data=prefix + "name")
        builder.button(text="⏰ Сменить время", callback_data=prefix + "time")

        builder.adjust(3)

    for i, row in enumerate(rows, 1):
        text += (
            f"{i}. <b>{row['training_name']}</b>\n"
            f"   📅 {row['date']} • ⏰ {row['start_time']}\n\n"
        )

        prefix = f"edit_{row['id']}_"

        builder.button(text="📅 Дата", callback_data=prefix + "date")
        builder.button(text="📝 Название", callback_data=prefix + "name")
        builder.button(text="⏰ Время", callback_data=prefix + "time")

        builder.adjust(3)

    await message.answer(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Используй: /delete <номер>\n Номер смотри в списке /my_trainings")
        return

    try:
        idx = int(args[1])
    except ValueError:
        await message.answer("❌ Номер должен быть числом, например: /delete 1")
        return
    user_id = message.from_user.id
    target_id = None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM one_off_trainings WHERE user_id = ? ORDER BY id", (user_id,)
        )
        rows = await cursor.fetchall()

        if not rows:
            await message.answer("У тебя нет тренировок, нечего удалять.")
            return
        if idx < 1 or idx > len(rows):
            await message.answer(f"❌ Такого номера нет. Найдено тренировок: {len(rows)}")
            return

        target_id = rows[idx - 1]["id"]

        await db.execute("DELETE FROM one_off_trainings WHERE id = ?", (target_id,))
        await db.commit()

        if target_id:
            job_ids = [f"reminder_{target_id}",
            f"check_{target_id}"
                       ]
            for job_id in job_ids:
                try:
                    if scheduler.get_job(job_id):
                        scheduler.remove_job(job_id, jobstore="default")
                        logger.info(f"✅ Задача {job_id} удалена из планировщика.")
                    else:
                        logger.debug(f"⚠️ Задача {job_id} не найдена в планировщике (это нормально, если тренировка уже прошла).")

                except Exception as e:
                    logger.error(f"❌ Ошибка при удалении задачи {job_id}: {e}")

        await message.answer(f"✅ Тренировка №{idx} успешно удалена!")

@dp.callback_query(SimpleCalendarCallback.filter())
async def process_calendar_selection(
    callback: types.CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext
):
    calendar = SimpleCalendar(locale="ru")
    selected, date = await calendar.process_selection(callback, callback_data)

    if not selected:
        await callback.answer()
        return

    date_str = date.strftime("%Y-%m-%d")
    current_state = await state.get_state()

    if current_state == AddTrainingState.waiting_for_date:
        # ===== ВЕТКА ДОБАВЛЕНИЯ =====
        await state.update_data(selected_date=date_str)
        await state.set_state(AddTrainingState.waiting_for_name)
        await callback.message.edit_text(
            f"✅ Дата выбрана: {date_str}\n\n📝 Теперь напиши название тренировки:"
        )

    elif current_state == EditState.waiting_for_date:
        # ===== ВЕТКА РЕДАКТИРОВАНИЯ =====
        data = await state.get_data()
        target_id = data["target_id"]

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE one_off_trainings SET date = ? WHERE id = ?",
                (date_str, target_id)
            )
            await db.commit()

        await state.clear()
        await callback.message.edit_text(f"✅ Дата обновлена на {date_str}.")

    await callback.answer()

@dp.callback_query(F.data.startswith("edit_"))
async def process_edit_choice(callback: types.CallbackQuery,state: FSMContext):

    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.answer("Ошибка обработки кнопки", show_alert=True)
        return

    target_id = int(parts[1])
    action = parts[2]
    logger.info(f"🔧 Пользователь редактирует: id={target_id}, поле={action}")
    await state.update_data(target_id=target_id, field=action)

    prompts = {
        "date": "📅 Напиши новую дату в формате ГГГГ-ММ-ДД (например, 2026-09-01):",
        "name": "📝 Напиши новое название тренировки (можно с пробелами):",
        "time": "⏰ Напиши новое время в формате ЧЧ:ММ (24 часа, например, 21:30):"
    }

    # Меняем сообщение на запросе ввода

    if action == "date":
        calendar = SimpleCalendar(locale="ru")
        await callback.message.edit_text(
            "📅 Выбери новую дату:",
            reply_markup=await calendar.start_calendar()
        )
        await state.set_state(EditState.waiting_for_date)
        await callback.answer()
        return
    elif action == "name":
        await state.set_state(EditState.waiting_for_name)
    elif action == "time":
        await state.set_state(EditState.waiting_for_time)


    await callback.message.edit_text(prompts[action])
    await callback.answer()

@dp.message(AddTrainingState.waiting_for_name)
async def process_add_name(message: types.Message, state: FSMContext):
    name = message.text.strip()

    if not name:
        await message.answer("❌ Название не может быть пустым. Напиши название тренировки:")
        return

    await state.update_data(training_name=name)
    await state.set_state(AddTrainingState.waiting_for_time)

    await message.answer(f"✅ Название: {name}\n\n⏰ Теперь укажи время в формате ЧЧ:ММ (например, 19:00):")

@dp.message(AddTrainingState.waiting_for_time)
async def process_add_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()

    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await message.answer("❌ Неверное время. Используй формат ЧЧ:ММ, например 19:00. Попробуй ещё раз:")
        return
    data = await state.get_data()
    user_id = data["user_id"]
    date_str = data["selected_date"]
    name = data["training_name"]

    train_datetime_str = f"{date_str} {time_str}"
    train_datetime = datetime.strptime(train_datetime_str, "%Y-%m-%d %H:%M")

    if train_datetime <= datetime.now():
        await message.answer("❌ Нельзя добавить тренировку, которая уже прошла.")
        await state.clear()
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO one_off_trainings (user_id, training_name, date, start_time) VALUES (?, ?, ?, ?)",
            (user_id, name, date_str, time_str)
        )
        await db.commit()
        training_id = cursor.lastrowid

    reminder_time = train_datetime - timedelta(minutes=20)
    scheduler.add_job(
        send_reminder,
        trigger="date",
        run_date=reminder_time,
        args=[user_id, name, date_str, time_str, training_id],
        id=f"reminder_{training_id}",
        replace_existing=True
    )

    check_time = train_datetime + timedelta(hours=1)
    scheduler.add_job(
        check_training_status,
        trigger="date",
        run_date=check_time,
        args=[training_id, user_id, name, date_str, time_str],
        id=f"check_{training_id}",
        replace_existing=True
    )

    await message.answer(
        f"✅ Тренировка сохранена: <b>{name}</b> на {date_str} в {time_str}.\n"
        f"Я напомню за 20 минут и спрошу о результате через час.",
        parse_mode="HTML"
    )
    await state.clear()

@dp.callback_query(F.data.startswith('train_'))
async def process_post_training_callback(callback_query: types.CallbackQuery):
    data = callback_query.data
    parts = data.split("_")
    if len(parts) < 3:
        await callback_query.answer("❌ Ошибка обработки кнопки.", show_alert=True)
        return

    try:
        training_id = int(parts[-1])
        action = parts[1]
    except (ValueError, IndexError):
        await callback_query.answer("❌ Ошибка обработки кнопки.", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM one_off_trainings WHERE id = ?", (training_id,)
        )
        row = await cursor.fetchone()

        if not row:
            await callback_query.answer("❌ Эта тренировка уже удалена!", show_alert=True)
            return
        training_name = row["training_name"]

    try:
        scheduler.remove_job(f"reminder_{training_id}", jobstore="default")
        scheduler.remove_job(f"check_{training_id}", jobstore="default")
    except Exception as e:
        # Если задачи уже нет — это нормально, просто логируем
        logger.info(f"⚠️ Задача для ID {training_id} уже отсутствовала: {e}")

    if action == "done":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM one_off_trainings WHERE id = ?", (training_id,))
            await db.commit()

        await callback_query.message.edit_text(f"✅ Отлично! Тренировка «{training_name}» отмечена как завершённая и удалена.")
        await callback_query.answer()
        return

    elif action == "delete":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM one_off_trainings WHERE id = ?", (training_id,))
            await db.commit()

        await callback_query.message.edit_text(f"🗑 Тренировка «{training_name}» удалена.")
        await callback_query.answer()
        return

    elif action == "reschedule":
        await callback_query.message.edit_text(
            f"🔄 Ты хочешь перенести тренировку «{training_name}».\n\n"
            f"К сожалению, автоматический перенос пока недоступен.\n"
            f"Пожалуйста, создай новую тренировку с новым временем через /add."
        )
        await callback_query.answer()
        return

    await callback_query.answer("❌ Неизвестное действие.", show_alert=True)

@dp.message(EditState.waiting_for_name)
async def process_edit_name(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data["target_id"]
    new_name = message.text.strip()

    if not new_name:
        await message.answer("❌ Название не может быть пустым.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE one_off_trainings SET training_name = ? WHERE id = ?",(new_name, target_id)
        )
        await db.commit()

    await state.clear()
    await message.answer(f"✅ Название обновлено: {new_name}")

@dp.message(EditState.waiting_for_time)
async def process_edit_time(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("target_id")
    new_time = message.text.strip()


    try:
        datetime.strptime(new_time, "%H:%M")
    except ValueError:
        await message.answer("❌ Неверное время. Используй формат ЧЧ:ММ, например: 21:30")
        await state.clear()
        return

    if not target_id:
        await message.answer("❌ Ошибка: не найден ID тренировки. Начни редактирование заново.")
        await state.clear()
        return

    logger.info(f"🔄 Редактирую время тренировки ID={target_id} на {new_time}")

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT date, training_name, user_id FROM one_off_trainings WHERE id = ?",
                (target_id,)
            )
            row = await cursor.fetchone()

            if not row:
                await message.answer("❌ Тренировка не найдена.")
                await state.clear()
                return

            old_date, name, user_id = row


            old_reminder_job_id = f"reminder_{target_id}"
            old_check_job_id = f"check_{target_id}"

            try:
                if scheduler.get_job(old_reminder_job_id):
                    scheduler.remove_job(old_reminder_job_id, jobstore="default")
                    logger.info(f"🗑️ Удалено старое напоминание для ID {target_id}")
                if scheduler.get_job(old_check_job_id):
                    scheduler.remove_job(old_check_job_id, jobstore="default")
                    logger.info(f"🗑️ Удалена старая проверка статуса для ID {target_id}")
            except Exception as e:
                logger.error(f"⚠️ Ошибка удаления старых задач: {e}")


            await db.execute(
                "UPDATE one_off_trainings SET start_time = ? WHERE id = ?",
                (new_time, target_id)
            )
            await db.commit()
            logger.info(f"💾 Время тренировки ID={target_id} обновлено в БД на {new_time}")


            train_datetime_str = f"{old_date} {new_time}"
            train_datetime = datetime.strptime(train_datetime_str, "%Y-%m-%d %H:%M")
            now_dt = datetime.now(moscow_tz).replace(tzinfo=None)

            if train_datetime > now_dt:
                reminder_time = train_datetime - timedelta(minutes=20)
                new_reminder_job_id = f"reminder_{target_id}"
                scheduler.add_job(
                    send_reminder,
                    trigger="date",
                    run_date=reminder_time,
                    args=[user_id, name, old_date, new_time, target_id],
                    id=new_reminder_job_id,
                    replace_existing=True
                )
                logger.info(f"✅ Запланировано новое напоминание на {reminder_time}")

                check_time = train_datetime + timedelta(hours=1)
                new_check_job_id = f"check_{target_id}"

                scheduler.add_job(
                    check_training_status,
                    trigger="date",
                    run_date=check_time,
                    args=[target_id, user_id, name, old_date, new_time],
                    id=new_check_job_id,
                    replace_existing=True
                )
                logger.info(f"✅ Запланирована новая проверка статуса на {check_time}")

                await message.answer(f"✅ Время обновлено на {new_time}!\n"
                                     f"Напоминание и проверка статуса пересчитаны.")
            else:
                await message.answer(f"✅ Время обновлено на {new_time}, но тренировка уже прошла или в прошлом.\n"
                                     f"Автоматические уведомления для неё не будут отправлены.")
    except Exception as e:
        logger.exception(f"❌ Критическая ошибка при редактировании тренировки ID={target_id}: {e}")
        await message.answer("❌ Произошла ошибка при обновлении времени. Попробуй позже или напиши админу.")
    finally:
        await state.clear()
        logger.info("🧹 Состояние FSM очищено.")


async def main():
    await init_db()

    # Сначала запускаем планировщик
    scheduler.start()
    logger.info("🕒 Планировщик APScheduler запущен")

    # Сразу восстанавливаем задачи из БД
    await schedule_existing_reminders()

    # Запускаем поллинг бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")


