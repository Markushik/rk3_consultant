import asyncio
import json
import html
import logging
from collections import defaultdict
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, CallbackQuery, Message

from aiogram_dialog import Dialog, DialogManager, StartMode, Window, setup_dialogs, ShowMode
from aiogram_dialog.widgets.kbd import Button, Row, Select
from aiogram_dialog.widgets.text import Const, Format
from aiogram_dialog.widgets.input import TextInput


API_TOKEN = "8770032694:AAEoKjxrTKwciqUew8Qf0HmGLCDPnLS11dQ"

REG_PATH = "reg.json"
QUEUE_PATH = "queue.json"
MARKED_PATH = "marked.json"
GROUPS_PATH = "groups.json"

DEFAULT_MINUTES = 15

SLOT_1 = "CONSULT_1"
SLOT_2 = "CONSULT_2"

DAYS = ("TUESDAY", "WEDNESDAY", "FRIDAY")

TUE_GENERAL_CAP_MIN = 6 * 60
WED_CAP_MIN = 4 * 60 + 45
FRI_CAP_MIN = 100 * DEFAULT_MINUTES


class RegMenu(StatesGroup):
    SURNAME = State()
    GROUP = State()
    MILITARY = State()
    REDUCTOR = State()


class UserMenu(StatesGroup):
    MENU = State()
    PICK_DAY = State()
    GROUP_OFFER = State()


class DeleteMenu(StatesGroup):
    SLOT = State()


class MarkMenu(StatesGroup):
    LIST = State()
    CONFIRM = State()


async def read_json(path, default):
    def _io():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    return await asyncio.to_thread(_io)


async def write_json(path, data):
    def _io():
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    await asyncio.to_thread(_io)


def esc(s: str) -> str:
    return html.escape((s or "").strip())


def day_title(day):
    return {"TUESDAY": "Вторник", "WEDNESDAY": "Среда", "FRIDAY": "Пятница"}.get(day, day)


def day_title_lc(day):
    return {"TUESDAY": "вторник", "WEDNESDAY": "среда", "FRIDAY": "пятница"}.get(day, (day or "").lower())


def slot_short(slot):
    return {SLOT_1: "1", SLOT_2: "2"}.get(slot, "?")


def cap_minutes(day_key: str) -> int:
    if day_key == "TUESDAY":
        return TUE_GENERAL_CAP_MIN
    if day_key == "WEDNESDAY":
        return WED_CAP_MIN
    return FRI_CAP_MIN


def cap_slots(day_key: str) -> int:
    return cap_minutes(day_key) // DEFAULT_MINUTES


async def get_user(uid: int):
    db = await read_json(REG_PATH, {})
    return db.get(str(uid))


def is_military_user(u: dict) -> bool:
    return (u or {}).get("MILITARY") == "YES"


async def ensure_user(uid: int):
    return bool(await get_user(uid))


async def guard_registered(message: Message) -> bool:
    if await ensure_user(message.from_user.id):
        return True
    await message.answer("<b>⚠️ Предупреждение</b> • Сначала выполните /start и завершите регистрацию")
    return False


async def remove_slot_from_queue(user_id: int, slot: str):
    q = await read_json(QUEUE_PATH, [])
    q = [x for x in q if not (x.get("USER_ID") == user_id and x.get("SLOT") == slot)]
    await write_json(QUEUE_PATH, q)


def queue_is_military_entry(x: dict) -> bool:
    return x.get("MILITARY") == "YES"


async def day_load_minutes(day_key: str, *, include_military: bool = True, only_unmarked: bool = True) -> int:
    q = await read_json(QUEUE_PATH, [])
    total = 0
    for x in q:
        if x.get("DAY") != day_key:
            continue
        if only_unmarked and x.get("MARKED"):
            continue
        if not include_military and queue_is_military_entry(x):
            continue
        total += int(x.get("MINUTES") or 0)
    return total


async def can_place(day_key: str, *, user_is_military: bool) -> bool:
    if day_key == "TUESDAY":
        if user_is_military:
            return True
        used = await day_load_minutes("TUESDAY", include_military=False, only_unmarked=True)
        return used + DEFAULT_MINUTES <= TUE_GENERAL_CAP_MIN
    if day_key == "WEDNESDAY":
        used = await day_load_minutes("WEDNESDAY", include_military=True, only_unmarked=True)
        return used + DEFAULT_MINUTES <= WED_CAP_MIN
    used = await day_load_minutes("FRIDAY", include_military=True, only_unmarked=True)
    return used + DEFAULT_MINUTES <= FRI_CAP_MIN


def military_ok_after_change(u: dict, slot: str, new_day: str | None) -> bool:
    if not is_military_user(u):
        return True
    other_slot = SLOT_2 if slot == SLOT_1 else SLOT_1
    other_day = u.get(other_slot)
    if new_day == "TUESDAY" or other_day == "TUESDAY":
        return True
    if new_day in ("WEDNESDAY", "FRIDAY") and other_day in ("WEDNESDAY", "FRIDAY"):
        return False
    return True


async def ensure_groups_store():
    g = await read_json(GROUPS_PATH, None)
    if not isinstance(g, dict) or "next_id" not in g or "groups" not in g:
        g = {"next_id": 1, "groups": {}}
        await write_json(GROUPS_PATH, g)
    return g


async def find_group_candidate(day_key: str, reductor: str, current_uid: int):
    q = await read_json(QUEUE_PATH, [])
    for x in q:
        if x.get("DAY") != day_key:
            continue
        if x.get("MARKED"):
            continue
        if (x.get("REDUCTOR") or "") != (reductor or ""):
            continue
        if int(x.get("USER_ID") or 0) == int(current_uid):
            continue
        return x
    return None


async def flatten_unmarked_queue():
    q = await read_json(QUEUE_PATH, [])
    return [x for x in q if x.get("DAY") in DAYS and not x.get("MARKED")]


async def mark_entry_self(user_id: int, slot: str, *, marked_by: int):
    q = await read_json(QUEUE_PATH, [])
    now = datetime.now(timezone.utc).isoformat()
    changed = False
    marked_batch = []

    for x in q:
        if x.get("MARKED"):
            continue
        if int(x.get("USER_ID") or 0) != int(user_id):
            continue
        if (x.get("SLOT") or "") != (slot or ""):
            continue
        x["MARKED"] = True
        x["MARKED_BY"] = marked_by
        x["MARKED_AT"] = now
        changed = True
        marked_batch.append(x)
        break

    if changed:
        await write_json(QUEUE_PATH, q)
        m = await read_json(MARKED_PATH, [])
        m.extend(marked_batch)
        await write_json(MARKED_PATH, m)

    return changed


async def queues_html():
    q = await read_json(QUEUE_PATH, [])
    if not q:
        return "<b>📌 Текущая очередь:</b>\n\n—"

    store = await ensure_groups_store()
    groups = store.get("groups", {}) or {}

    by_day_group_ids = defaultdict(list)
    for gid, g in groups.items():
        d = g.get("DAY")
        if d in DAYS:
            by_day_group_ids[d].append((int(gid), gid, g))
    for d in by_day_group_ids:
        by_day_group_ids[d].sort(key=lambda t: t[0])

    out = []
    out.append("<b>📌 Текущая очередь:</b>\n")

    for d in DAYS:
        out.append(f"• <u><b>{day_title(d)}</b></u>\n")

        used = set()
        group_blocks = []
        for _, gid, g in by_day_group_ids.get(d, []):
            members = set(int(x) for x in (g.get("MEMBERS") or []))
            lines = []
            for x in q:
                if x.get("DAY") != d:
                    continue
                if int(x.get("USER_ID") or 0) not in members:
                    continue
                if x.get("GROUP_ID") != gid:
                    continue
                line = f"{esc(x.get('GROUP'))} — {esc(x.get('SURNAME'))}"
                if x.get("MARKED"):
                    line = f"<s>{line}</s>"
                lines.append(line)
                used.add((int(x.get("USER_ID") or 0), x.get("SLOT")))
            if lines:
                group_blocks.append((gid, g.get("REDUCTOR") or "—", lines))

        idx = 1
        group_idx = 1

        rest = []
        for x in q:
            if x.get("DAY") != d:
                continue
            if (int(x.get("USER_ID") or 0), x.get("SLOT")) in used:
                continue
            if x.get("GROUP_ID"):
                continue

            line = f"{esc(x.get('GROUP'))} — {esc(x.get('SURNAME'))} — <i>{esc(x.get('REDUCTOR') or '—')}</i>"
            if x.get("MARKED"):
                line = f"<s>{line}</s>"
            rest.append(line)

        if rest:
            for line in rest:
                out.append(f"{idx}. {line}")
                idx += 1

        for _, red, lines in group_blocks:
            out.append(f"{idx}. <b>Группа {group_idx}</b> (<i>{esc(red)}</i>):")
            for ln in lines:
                out.append(ln)
            group_idx += 1
            idx += 1

        if not rest and not group_blocks:
            out.append("—")

        out.append("")

    return "\n".join(out).strip()


REDUCTORS = {
    "R1": "Цилиндрическией",
    "R2": "Червячный",
    "R3": "Конический",
    "R4": "Планетарный",
    "R5": "Волновой",
}


async def cmd_start(message: Message, dialog_manager: DialogManager):
    if await ensure_user(message.from_user.id):
        await dialog_manager.start(UserMenu.MENU, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)
    else:
        await dialog_manager.start(RegMenu.SURNAME, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


async def cmd_queues(message: Message):
    if not await guard_registered(message):
        return
    await message.answer(await queues_html())


async def cmd_delete(message: Message, dialog_manager: DialogManager):
    if not await guard_registered(message):
        return
    await dialog_manager.start(DeleteMenu.SLOT, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


async def cmd_mark(message: Message, dialog_manager: DialogManager):
    if not await guard_registered(message):
        return
    await dialog_manager.start(MarkMenu.LIST, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


async def on_surname(message: Message, widget: TextInput, manager: DialogManager, text: str):
    manager.dialog_data["SURNAME"] = text.strip().capitalize()
    await manager.switch_to(RegMenu.GROUP)


async def on_group(message: Message, widget: TextInput, manager: DialogManager, text: str):
    manager.dialog_data["GROUP"] = text.strip().upper()
    await manager.switch_to(RegMenu.MILITARY)


async def finish_reg(callback: CallbackQuery, manager: DialogManager):
    uid = str(callback.from_user.id)
    db = await read_json(REG_PATH, {})
    db[uid] = {
        "USER_ID": callback.from_user.id,
        "SURNAME": manager.dialog_data.get("SURNAME"),
        "GROUP": manager.dialog_data.get("GROUP"),
        "MILITARY": manager.dialog_data.get("MILITARY"),
        "REDUCTOR": manager.dialog_data.get("REDUCTOR"),
        "LOCKED": False,
        SLOT_1: None,
        SLOT_2: None,
    }
    await write_json(REG_PATH, db)
    await callback.message.answer("<b>✅ Успех</b> • Регистрация завершена")
    await manager.start(UserMenu.MENU, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


async def mil_yes(callback: CallbackQuery, button: Button, manager: DialogManager):
    manager.dialog_data["MILITARY"] = "YES"
    await manager.switch_to(RegMenu.REDUCTOR)


async def mil_no(callback: CallbackQuery, button: Button, manager: DialogManager):
    manager.dialog_data["MILITARY"] = "NO"
    await manager.switch_to(RegMenu.REDUCTOR)


async def on_reductor(callback: CallbackQuery, button: Button, manager: DialogManager):
    manager.dialog_data["REDUCTOR"] = REDUCTORS.get(button.widget_id)
    await finish_reg(callback, manager)


async def on_refresh(callback: CallbackQuery, button: Button, manager: DialogManager):
    await manager.start(UserMenu.MENU, mode=StartMode.NORMAL, show_mode=ShowMode.EDIT)


async def menu_getter(dialog_manager: DialogManager, **kwargs):
    u = await get_user(dialog_manager.event.from_user.id) or {}

    c1 = u.get(SLOT_1)
    c2 = u.get(SLOT_2)

    used_tue = (await day_load_minutes("TUESDAY", include_military=False, only_unmarked=True)) // DEFAULT_MINUTES
    used_wed = (await day_load_minutes("WEDNESDAY", include_military=True, only_unmarked=True)) // DEFAULT_MINUTES
    used_fri = (await day_load_minutes("FRIDAY", include_military=True, only_unmarked=True)) // DEFAULT_MINUTES

    tot_tue = cap_slots("TUESDAY")
    tot_wed = cap_slots("WEDNESDAY")
    tot_fri = cap_slots("FRIDAY")

    pre = "\n".join(
        [
            f"Вторник : {used_tue:>2} / {tot_tue:<3}",
            f"Среда   : {used_wed:>2} / {tot_wed:<3}",
            f"Пятница : {used_fri:>2} / {tot_fri:<3}",
        ]
    )

    locked = bool(u.get("LOCKED"))

    text = (
        "<b>Ваши консультации:</b>\n\n"
        f"1. {day_title(c1) if c1 else '—'}\n"
        f"2. {day_title(c2) if c2 else '—'}\n\n"
        f"<i>❕ Консультация длится {DEFAULT_MINUTES} мин.</i>\n\n"
        "<b>🗓️ Заполненность слотов:</b>\n"
        f"<pre>{pre}</pre>\n\n"
        "👇 <b>Выберите</b> консультацию:"
    )
    return {"MENU_TEXT": text, "LOCKED": locked}


async def group_offer_getter(dialog_manager: DialogManager, **kwargs):
    day_key = dialog_manager.dialog_data.get("OFFER_DAY")
    red = dialog_manager.dialog_data.get("OFFER_RED")
    cand_group = dialog_manager.dialog_data.get("OFFER_CAND_GROUP")
    cand_surname = dialog_manager.dialog_data.get("OFFER_CAND_SURNAME")
    cand_slot = dialog_manager.dialog_data.get("OFFER_CAND_SLOT")

    text = (
        "<b>⚠️ Предупреждение</b> • Найдено совпадение по редуктору\n\n"
        f"День: <code>{day_title_lc(day_key)}</code>\n"
        f"Редуктор: <i>{esc(red)}</i>\n\n"
        f"В очереди уже есть: <b>{esc(cand_group)} — {esc(cand_surname)}</b> "
        f"(консультация <b>{slot_short(cand_slot)}</b>).\n\n"
        "Объединиться в группу?"
    )
    return {"OFFER_TEXT": text}


async def pick_c1(callback: CallbackQuery, button: Button, manager: DialogManager):
    u = await get_user(callback.from_user.id) or {}
    if u.get("LOCKED"):
        await callback.message.answer("<b>⚠️ Предупреждение</b> • Вы уже закрыли консультацию — перезапись запрещена")
        await manager.start(UserMenu.MENU, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)
        return
    manager.dialog_data["SLOT"] = SLOT_1
    await manager.switch_to(UserMenu.PICK_DAY)


async def pick_c2(callback: CallbackQuery, button: Button, manager: DialogManager):
    u = await get_user(callback.from_user.id) or {}
    if u.get("LOCKED"):
        await callback.message.answer("<b>⚠️ Предупреждение</b> • Вы уже закрыли консультацию — перезапись запрещена")
        await manager.start(UserMenu.MENU, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)
        return
    manager.dialog_data["SLOT"] = SLOT_2
    await manager.switch_to(UserMenu.PICK_DAY)


async def set_slot_day(user_id: int, slot: str, day_key: str):
    u = await get_user(user_id)
    if not u:
        return False, "<b>❌ Ошибка</b> • Сначала выполните /start", None

    if u.get("LOCKED"):
        return False, "<b>⚠️ Предупреждение</b> • Вы уже закрыли консультацию — перезапись запрещена", None

    if slot not in (SLOT_1, SLOT_2) or day_key not in DAYS:
        return False, "<b>❌ Ошибка</b> • Неверный выбор", None

    if not military_ok_after_change(u, slot, day_key):
        return (
            False,
            "<b>⚠️ Предупреждение</b> • Студентам военной кафедры доступны комбинации:\n"
            "Вторник + Среда\n"
            "Вторник + Пятница",
            None,
        )

    await remove_slot_from_queue(user_id, slot)

    if not await can_place(day_key, user_is_military=is_military_user(u)):
        return (
            False,
            f"<b>⚠️ Предупреждение</b> • На <code>{day_title_lc(day_key)}</code> больше нет свободных слотов",
            None,
        )

    u[slot] = day_key
    db = await read_json(REG_PATH, {})
    db[str(user_id)] = u
    await write_json(REG_PATH, db)

    q = await read_json(QUEUE_PATH, [])
    q.append(
        {
            "USER_ID": user_id,
            "SLOT": slot,
            "DAY": day_key,
            "MINUTES": DEFAULT_MINUTES,
            "GROUP": u.get("GROUP", ""),
            "SURNAME": u.get("SURNAME", ""),
            "MILITARY": u.get("MILITARY"),
            "REDUCTOR": u.get("REDUCTOR"),
            "MARKED": False,
        }
    )
    await write_json(QUEUE_PATH, q)

    red = u.get("REDUCTOR")
    offer = None
    if red:
        cand = await find_group_candidate(day_key, red, user_id)
        if cand:
            offer = {
                "DAY": day_key,
                "RED": red,
                "CAND_UID": int(cand.get("USER_ID") or 0),
                "CAND_SLOT": cand.get("SLOT"),
                "CAND_GROUP": cand.get("GROUP", ""),
                "CAND_SURNAME": cand.get("SURNAME", ""),
            }

    return True, f"<b>✅ Успех</b> • Записано: <code>{day_title_lc(day_key)}</code>", offer


async def set_day(callback: CallbackQuery, day_key: str, manager: DialogManager):
    slot = manager.dialog_data.get("SLOT")
    ok, msg, offer = await set_slot_day(callback.from_user.id, slot, day_key)
    await callback.message.answer(msg)

    if ok and offer:
        manager.dialog_data["OFFER_DAY"] = offer["DAY"]
        manager.dialog_data["OFFER_RED"] = offer["RED"]
        manager.dialog_data["OFFER_CAND_UID"] = offer["CAND_UID"]
        manager.dialog_data["OFFER_CAND_SLOT"] = offer["CAND_SLOT"]
        manager.dialog_data["OFFER_CAND_GROUP"] = offer["CAND_GROUP"]
        manager.dialog_data["OFFER_CAND_SURNAME"] = offer["CAND_SURNAME"]
        manager.dialog_data["OFFER_MY_SLOT"] = slot
        await manager.switch_to(UserMenu.GROUP_OFFER)
        return

    await manager.start(UserMenu.MENU, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


async def day_tue(callback: CallbackQuery, button: Button, manager: DialogManager):
    await set_day(callback, "TUESDAY", manager)


async def day_wed(callback: CallbackQuery, button: Button, manager: DialogManager):
    await set_day(callback, "WEDNESDAY", manager)


async def day_fri(callback: CallbackQuery, button: Button, manager: DialogManager):
    await set_day(callback, "FRIDAY", manager)


async def clear_slot(user_id: int, slot: str):
    u = await get_user(user_id)
    if not u:
        return False, "<b>❌ Ошибка</b> • Сначала выполните /start"

    if u.get("LOCKED"):
        return False, "<b>⚠️ Предупреждение</b> • После закрытия консультации удаление/перезапись запрещены"

    await remove_slot_from_queue(user_id, slot)

    u[slot] = None
    db = await read_json(REG_PATH, {})
    db[str(user_id)] = u
    await write_json(REG_PATH, db)
    return True, "<b>✅ Успех</b> • Запись удалена"


async def del_c1(callback: CallbackQuery, button: Button, manager: DialogManager):
    ok, msg = await clear_slot(callback.from_user.id, SLOT_1)
    await callback.message.answer(msg)
    await manager.start(UserMenu.MENU, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


async def del_c2(callback: CallbackQuery, button: Button, manager: DialogManager):
    ok, msg = await clear_slot(callback.from_user.id, SLOT_2)
    await callback.message.answer(msg)
    await manager.start(UserMenu.MENU, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


async def offer_yes(callback: CallbackQuery, button: Button, manager: DialogManager):
    await callback.message.answer("<b>✅ Успех</b> • Группа сформирована")
    await callback.message.answer(await queues_html())
    await manager.start(UserMenu.MENU, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


async def offer_no(callback: CallbackQuery, button: Button, manager: DialogManager):
    await callback.message.answer("<b>✅ Успех</b> • Ок, без группы")
    await callback.message.answer(await queues_html())
    await manager.start(UserMenu.MENU, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


async def mark_list_getter(dialog_manager: DialogManager, **kwargs):
    uid = dialog_manager.event.from_user.id
    q = await flatten_unmarked_queue()
    mine = [x for x in q if int(x.get("USER_ID") or 0) == int(uid)]

    day_counts = defaultdict(int)
    for x in mine:
        day_counts[x.get("DAY")] += 1

    items = []
    for x in mine:
        slot = x.get("SLOT") or ""
        key = f"{uid}:{slot}"
        d = x.get("DAY")
        title = day_title(d)
        if day_counts.get(d, 0) > 1:
            title = f"{title} ({slot_short(slot)})"
        items.append({"id": key, "title": title})

    text = "<b>✅ Закрыть консультацию</b>\n\n" + ("Выберите день:" if items else "У вас нет активных записей.")
    return {"MARK_TEXT": text, "MARK_ITEMS": items}


async def on_mark_pick(callback: CallbackQuery, widget: Select, manager: DialogManager, item_id: str):
    manager.dialog_data["MARK_PICK"] = item_id
    await manager.switch_to(MarkMenu.CONFIRM)


async def mark_yes(callback: CallbackQuery, button: Button, manager: DialogManager):
    pick = (manager.dialog_data.get("MARK_PICK") or "").strip()
    try:
        uid_s, slot = pick.split(":", 1)
        uid = int(uid_s)
    except Exception:
        await callback.message.answer("<b>❌ Ошибка</b> • Некорректная запись")
        await manager.start(MarkMenu.LIST, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)
        return

    if uid != callback.from_user.id:
        await callback.message.answer("<b>❌ Ошибка</b> • Можно закрыть только свою консультацию")
        await manager.start(MarkMenu.LIST, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)
        return

    ok = await mark_entry_self(uid, slot, marked_by=callback.from_user.id)
    if ok:
        db = await read_json(REG_PATH, {})
        u = db.get(str(uid), {})
        u["LOCKED"] = True
        db[str(uid)] = u
        await write_json(REG_PATH, db)

        await callback.message.answer("<b>✅ Успех</b> • Консультация закрыта")
    else:
        await callback.message.answer("<b>⚠️ Предупреждение</b> • Не удалось закрыть (уже закрыто)")

    await callback.message.answer(await queues_html())
    await manager.start(MarkMenu.LIST, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


async def mark_no(callback: CallbackQuery, button: Button, manager: DialogManager):
    await callback.message.answer("<b>✅ Успех</b> • Ок")
    await manager.start(MarkMenu.LIST, mode=StartMode.RESET_STACK, show_mode=ShowMode.DELETE_AND_SEND)


REDUCTORS_TEXT = (
    "⚙️ <b>Выберите редуктор</b>\n\n"
    "1 — Цилиндрическией\n"
    "2 — Червячный\n"
    "3 — Конический\n"
    "4 — Планетарный\n"
    "5 — Волновой"
)

reg_menu = Dialog(
    Window(
        Const("✍️ Введите <b>фамилию</b>"),
        TextInput(id="SURNAME", on_success=on_surname),
        state=RegMenu.SURNAME,
    ),
    Window(
        Const("✍️ Введите <b>группу</b>"),
        TextInput(id="GROUP", on_success=on_group),
        state=RegMenu.GROUP,
    ),
    Window(
        Const("🎖️ Военная кафедра?"),
        Row(
            Button(Const("Да"), id="YES", on_click=mil_yes),
            Button(Const("Нет"), id="NO", on_click=mil_no),
        ),
        state=RegMenu.MILITARY,
    ),
    Window(
        Const(REDUCTORS_TEXT),
        Row(
            Button(Const("1"), id="R1", on_click=on_reductor),
            Button(Const("2"), id="R2", on_click=on_reductor),
            Button(Const("3"), id="R3", on_click=on_reductor),
            Button(Const("4"), id="R4", on_click=on_reductor),
            Button(Const("5"), id="R5", on_click=on_reductor),
        ),
        state=RegMenu.REDUCTOR,
    ),
)

user_menu = Dialog(
    Window(
        Format("{MENU_TEXT}"),
        Row(
            Button(Const("Консультация 1"), id="C1", on_click=pick_c1),
            Button(Const("Консультация 2"), id="C2", on_click=pick_c2),
        ),
        Row(
            Button(Const("🔄 Обновить"), id="REFRESH", on_click=on_refresh),
        ),
        state=UserMenu.MENU,
        getter=menu_getter,
    ),
    Window(
        Const("📅 Выберите <b>день</b>:"),
        Row(
            Button(Const("Вторник"), id="TUE", on_click=day_tue),
            Button(Const("Среда"), id="WED", on_click=day_wed),
            Button(Const("Пятница"), id="FRI", on_click=day_fri),
        ),
        state=UserMenu.PICK_DAY,
    ),
    Window(
        Format("{OFFER_TEXT}"),
        Row(
            Button(Const("Да"), id="OY", on_click=offer_yes),
            Button(Const("Нет"), id="ON", on_click=offer_no),
        ),
        getter=group_offer_getter,
        state=UserMenu.GROUP_OFFER,
    ),
)

delete_menu = Dialog(
    Window(
        Const("🗑️ Удалить запись:"),
        Row(
            Button(Const("Удалить 1"), id="D1", on_click=del_c1),
            Button(Const("Удалить 2"), id="D2", on_click=del_c2),
        ),
        state=DeleteMenu.SLOT,
    ),
)

mark_menu = Dialog(
    Window(
        Format("{MARK_TEXT}"),
        Select(
            Format("{item[title]}"),
            id="MARK_SEL",
            item_id_getter=lambda x: x["id"],
            items="MARK_ITEMS",
            on_click=on_mark_pick,
        ),
        getter=mark_list_getter,
        state=MarkMenu.LIST,
    ),
    Window(
        Const("Консультация закончена?"),
        Row(
            Button(Const("Да"), id="MY", on_click=mark_yes),
            Button(Const("Нет"), id="MN", on_click=mark_no),
        ),
        state=MarkMenu.CONFIRM,
    ),
)


async def main():
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="— запуск"),
            BotCommand(command="queues", description="— очередь / группы"),
            BotCommand(command="delete", description="— удалить свою запись"),
            BotCommand(command="mark", description="— закрыть консультацию"),
        ]
    )

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage, events_isolation=SimpleEventIsolation())

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_queues, Command("queues"))
    dp.message.register(cmd_delete, Command("delete"))
    dp.message.register(cmd_mark, Command("mark"))

    dp.include_router(reg_menu)
    dp.include_router(user_menu)
    dp.include_router(delete_menu)
    dp.include_router(mark_menu)

    setup_dialogs(dp)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())