"""
Telegram-бот-конструктор еды.

Возможности:
- выбор приема пищи: завтрак, обед, ужин;
- сборные блюда из ингредиентов с произвольным количеством;
- расчет КБЖУ и цены;
- персональные нормы КБЖУ пользователя;
- сохранение заказов в SQLite или PostgreSQL;
- личные шаблоны пользователя;
- админ-панель и команды для редактирования ингредиентов, КБЖУ, цен и меню;
- экспорт итогового состава в PDF или Google Sheets.

Быстрый запуск:
1) python -m venv .venv
2) source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
3) pip install -r requirements.txt
4) export BOT_TOKEN="токен_от_BotFather"
5) python meal_builder_bot.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine


# =========================
# 1. НАСТРОЙКИ
# =========================

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///meal_builder_bot.db")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(";", ",").split(",") if x.strip().isdigit()}
PRICE_CURRENCY = os.getenv("PRICE_CURRENCY", "RUB").upper()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_WORKSHEET = os.getenv("GOOGLE_SHEET_WORKSHEET", "Orders")
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", tempfile.gettempdir()))
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

CURRENCY_SYMBOLS = {
    "RUB": "₽",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "KZT": "₸",
}


# =========================
# 2. БАЗОВОЕ МЕНЮ
# =========================
# Меню можно расширять через админ-команду /add_item.
# КБЖУ и цена базовых ингредиентов хранятся в DEFAULT_ITEM_VALUES.
# Админские изменения сохраняются в БД и накладываются поверх этих значений.

MEALS: dict[str, str] = {
    "breakfast": "🍳 Завтрак",
    "lunch": "🥗 Обед",
    "dinner": "🍽 Ужин",
}

MENU: dict[str, list[dict[str, Any]]] = {
    "breakfast": [
        {
            "id": "oat_bowl",
            "name": "Овсяный боул",
            "description": "Основа + фрукты/ягоды + белок + топпинги.",
            "categories": [
                {
                    "id": "base",
                    "name": "Основа",
                    "items": [
                        {"id": "oats", "name": "Овсяные хлопья", "unit": "г", "step": 20},
                        {"id": "milk", "name": "Молоко", "unit": "мл", "step": 50},
                        {"id": "yogurt", "name": "Греческий йогурт", "unit": "г", "step": 50},
                    ],
                },
                {
                    "id": "fruit",
                    "name": "Фрукты и ягоды",
                    "items": [
                        {"id": "banana", "name": "Банан", "unit": "г", "step": 30},
                        {"id": "berries", "name": "Ягоды", "unit": "г", "step": 30},
                        {"id": "apple", "name": "Яблоко", "unit": "г", "step": 30},
                    ],
                },
                {
                    "id": "protein",
                    "name": "Белок",
                    "items": [
                        {"id": "protein_powder", "name": "Протеин", "unit": "г", "step": 10},
                        {"id": "cottage", "name": "Творог", "unit": "г", "step": 50},
                    ],
                },
                {
                    "id": "toppings",
                    "name": "Топпинги",
                    "items": [
                        {"id": "nuts", "name": "Орехи", "unit": "г", "step": 10},
                        {"id": "honey", "name": "Мёд", "unit": "г", "step": 10},
                        {"id": "chia", "name": "Семена чиа", "unit": "г", "step": 5},
                    ],
                },
            ],
        },
        {
            "id": "omelet",
            "name": "Омлет-конструктор",
            "description": "Яйца + овощи + сыр/мясо + зелень.",
            "categories": [
                {
                    "id": "base",
                    "name": "Основа",
                    "items": [
                        {"id": "eggs", "name": "Яйца", "unit": "шт", "step": 1},
                        {"id": "egg_whites", "name": "Белки", "unit": "шт", "step": 1},
                    ],
                },
                {
                    "id": "veg",
                    "name": "Овощи",
                    "items": [
                        {"id": "tomatoes", "name": "Томаты", "unit": "г", "step": 30},
                        {"id": "spinach", "name": "Шпинат", "unit": "г", "step": 20},
                        {"id": "mushrooms", "name": "Грибы", "unit": "г", "step": 30},
                    ],
                },
                {
                    "id": "extra",
                    "name": "Дополнительно",
                    "items": [
                        {"id": "cheese", "name": "Сыр", "unit": "г", "step": 20},
                        {"id": "turkey", "name": "Индейка", "unit": "г", "step": 30},
                        {"id": "avocado", "name": "Авокадо", "unit": "г", "step": 30},
                    ],
                },
            ],
        },
    ],
    "lunch": [
        {
            "id": "lunch_bowl",
            "name": "Боул-конструктор",
            "description": "Крупа/основа + белок + овощи + соус + топпинги.",
            "categories": [
                {
                    "id": "base",
                    "name": "Основа",
                    "items": [
                        {"id": "rice", "name": "Рис", "unit": "г", "step": 50},
                        {"id": "buckwheat", "name": "Гречка", "unit": "г", "step": 50},
                        {"id": "quinoa", "name": "Киноа", "unit": "г", "step": 50},
                    ],
                },
                {
                    "id": "protein",
                    "name": "Белок",
                    "items": [
                        {"id": "chicken", "name": "Курица", "unit": "г", "step": 50},
                        {"id": "salmon", "name": "Лосось", "unit": "г", "step": 50},
                        {"id": "tofu", "name": "Тофу", "unit": "г", "step": 50},
                    ],
                },
                {
                    "id": "veg",
                    "name": "Овощи",
                    "items": [
                        {"id": "cucumber", "name": "Огурец", "unit": "г", "step": 30},
                        {"id": "tomatoes", "name": "Томаты", "unit": "г", "step": 30},
                        {"id": "corn", "name": "Кукуруза", "unit": "г", "step": 30},
                        {"id": "greens", "name": "Зелень", "unit": "г", "step": 10},
                    ],
                },
                {
                    "id": "sauce",
                    "name": "Соус",
                    "items": [
                        {"id": "soy", "name": "Соевый соус", "unit": "мл", "step": 10},
                        {"id": "yogurt_sauce", "name": "Йогуртовый соус", "unit": "г", "step": 20},
                        {"id": "olive_oil", "name": "Оливковое масло", "unit": "мл", "step": 5},
                    ],
                },
            ],
        },
        {
            "id": "pasta",
            "name": "Паста-конструктор",
            "description": "Паста + белок + овощи + соус.",
            "categories": [
                {
                    "id": "base",
                    "name": "Паста",
                    "items": [
                        {"id": "spaghetti", "name": "Спагетти", "unit": "г", "step": 50},
                        {"id": "penne", "name": "Пенне", "unit": "г", "step": 50},
                    ],
                },
                {
                    "id": "protein",
                    "name": "Белок",
                    "items": [
                        {"id": "chicken", "name": "Курица", "unit": "г", "step": 50},
                        {"id": "shrimp", "name": "Креветки", "unit": "г", "step": 50},
                    ],
                },
                {
                    "id": "sauce",
                    "name": "Соус",
                    "items": [
                        {"id": "tomato_sauce", "name": "Томатный соус", "unit": "г", "step": 30},
                        {"id": "cream_sauce", "name": "Сливочный соус", "unit": "г", "step": 30},
                        {"id": "pesto", "name": "Песто", "unit": "г", "step": 10},
                    ],
                },
                {
                    "id": "veg",
                    "name": "Овощи",
                    "items": [
                        {"id": "mushrooms", "name": "Грибы", "unit": "г", "step": 30},
                        {"id": "spinach", "name": "Шпинат", "unit": "г", "step": 20},
                        {"id": "pepper", "name": "Перец", "unit": "г", "step": 30},
                    ],
                },
            ],
        },
    ],
    "dinner": [
        {
            "id": "salad",
            "name": "Салат-конструктор",
            "description": "Листья + белок + овощи + заправка + топпинги.",
            "categories": [
                {
                    "id": "greens",
                    "name": "Листья",
                    "items": [
                        {"id": "romaine", "name": "Ромэн", "unit": "г", "step": 30},
                        {"id": "spinach", "name": "Шпинат", "unit": "г", "step": 20},
                        {"id": "arugula", "name": "Руккола", "unit": "г", "step": 20},
                    ],
                },
                {
                    "id": "protein",
                    "name": "Белок",
                    "items": [
                        {"id": "tuna", "name": "Тунец", "unit": "г", "step": 50},
                        {"id": "chicken", "name": "Курица", "unit": "г", "step": 50},
                        {"id": "egg", "name": "Яйцо", "unit": "шт", "step": 1},
                    ],
                },
                {
                    "id": "veg",
                    "name": "Овощи",
                    "items": [
                        {"id": "tomatoes", "name": "Томаты", "unit": "г", "step": 30},
                        {"id": "avocado", "name": "Авокадо", "unit": "г", "step": 30},
                    ],
                },
                {
                    "id": "dressing",
                    "name": "Заправка",
                    "items": [
                        {"id": "olive_oil", "name": "Оливковое масло", "unit": "мл", "step": 5},
                        {"id": "lemon", "name": "Лимонный сок", "unit": "мл", "step": 5},
                        {"id": "balsamic", "name": "Бальзамик", "unit": "мл", "step": 5},
                    ],
                },
            ],
        },
        {
            "id": "hot_plate",
            "name": "Тёплая тарелка",
            "description": "Овощи + белок + гарнир + соус.",
            "categories": [
                {
                    "id": "veg",
                    "name": "Овощи",
                    "items": [
                        {"id": "broccoli", "name": "Брокколи", "unit": "г", "step": 50},
                        {"id": "zucchini", "name": "Кабачок", "unit": "г", "step": 50},
                        {"id": "pepper", "name": "Перец", "unit": "г", "step": 30},
                    ],
                },
                {
                    "id": "protein",
                    "name": "Белок",
                    "items": [
                        {"id": "beef", "name": "Говядина", "unit": "г", "step": 50},
                        {"id": "turkey", "name": "Индейка", "unit": "г", "step": 50},
                        {"id": "tofu", "name": "Тофу", "unit": "г", "step": 50},
                    ],
                },
                {
                    "id": "side",
                    "name": "Гарнир",
                    "items": [
                        {"id": "potato", "name": "Картофель", "unit": "г", "step": 50},
                        {"id": "rice", "name": "Рис", "unit": "г", "step": 50},
                        {"id": "beans", "name": "Фасоль", "unit": "г", "step": 50},
                    ],
                },
                {
                    "id": "sauce",
                    "name": "Соус",
                    "items": [
                        {"id": "teriyaki", "name": "Терияки", "unit": "мл", "step": 10},
                        {"id": "garlic_yogurt", "name": "Чесночный йогуртовый", "unit": "г", "step": 20},
                    ],
                },
            ],
        },
    ],
}

# КБЖУ и цена указываются на 100 г/мл, а для unit="шт" - на 1 штуку.
DEFAULT_ITEM_VALUES: dict[str, dict[str, float]] = {
    "oats": {"kcal": 389, "protein": 16.9, "fat": 6.9, "carbs": 66.3, "price": 18},
    "milk": {"kcal": 52, "protein": 3.0, "fat": 2.5, "carbs": 4.8, "price": 9},
    "yogurt": {"kcal": 59, "protein": 10.0, "fat": 0.4, "carbs": 3.6, "price": 38},
    "banana": {"kcal": 89, "protein": 1.1, "fat": 0.3, "carbs": 22.8, "price": 16},
    "berries": {"kcal": 50, "protein": 0.7, "fat": 0.3, "carbs": 12.0, "price": 65},
    "apple": {"kcal": 52, "protein": 0.3, "fat": 0.2, "carbs": 13.8, "price": 20},
    "protein_powder": {"kcal": 380, "protein": 75.0, "fat": 6.0, "carbs": 8.0, "price": 150},
    "cottage": {"kcal": 120, "protein": 17.0, "fat": 5.0, "carbs": 3.0, "price": 36},
    "nuts": {"kcal": 607, "protein": 20.0, "fat": 54.0, "carbs": 20.0, "price": 120},
    "honey": {"kcal": 304, "protein": 0.3, "fat": 0.0, "carbs": 82.4, "price": 45},
    "chia": {"kcal": 486, "protein": 16.5, "fat": 30.7, "carbs": 42.1, "price": 95},
    "eggs": {"kcal": 70, "protein": 6.3, "fat": 5.0, "carbs": 0.4, "price": 18},
    "egg": {"kcal": 70, "protein": 6.3, "fat": 5.0, "carbs": 0.4, "price": 18},
    "egg_whites": {"kcal": 17, "protein": 3.6, "fat": 0.1, "carbs": 0.2, "price": 10},
    "tomatoes": {"kcal": 18, "protein": 0.9, "fat": 0.2, "carbs": 3.9, "price": 28},
    "spinach": {"kcal": 23, "protein": 2.9, "fat": 0.4, "carbs": 3.6, "price": 70},
    "mushrooms": {"kcal": 22, "protein": 3.1, "fat": 0.3, "carbs": 3.3, "price": 42},
    "cheese": {"kcal": 356, "protein": 25.0, "fat": 27.0, "carbs": 2.2, "price": 95},
    "turkey": {"kcal": 135, "protein": 29.0, "fat": 1.5, "carbs": 0.0, "price": 85},
    "avocado": {"kcal": 160, "protein": 2.0, "fat": 14.7, "carbs": 8.5, "price": 95},
    "rice": {"kcal": 130, "protein": 2.7, "fat": 0.3, "carbs": 28.0, "price": 16},
    "buckwheat": {"kcal": 110, "protein": 3.6, "fat": 1.1, "carbs": 21.3, "price": 18},
    "quinoa": {"kcal": 120, "protein": 4.4, "fat": 1.9, "carbs": 21.3, "price": 55},
    "chicken": {"kcal": 165, "protein": 31.0, "fat": 3.6, "carbs": 0.0, "price": 70},
    "salmon": {"kcal": 208, "protein": 20.0, "fat": 13.0, "carbs": 0.0, "price": 210},
    "tofu": {"kcal": 76, "protein": 8.0, "fat": 4.8, "carbs": 1.9, "price": 50},
    "cucumber": {"kcal": 15, "protein": 0.7, "fat": 0.1, "carbs": 3.6, "price": 22},
    "corn": {"kcal": 96, "protein": 3.4, "fat": 1.5, "carbs": 21.0, "price": 28},
    "greens": {"kcal": 20, "protein": 2.0, "fat": 0.3, "carbs": 3.0, "price": 60},
    "soy": {"kcal": 53, "protein": 8.1, "fat": 0.6, "carbs": 4.9, "price": 20},
    "yogurt_sauce": {"kcal": 78, "protein": 4.5, "fat": 3.0, "carbs": 7.0, "price": 35},
    "olive_oil": {"kcal": 884, "protein": 0.0, "fat": 100.0, "carbs": 0.0, "price": 80},
    "spaghetti": {"kcal": 158, "protein": 5.8, "fat": 0.9, "carbs": 30.9, "price": 18},
    "penne": {"kcal": 157, "protein": 5.7, "fat": 0.9, "carbs": 30.5, "price": 18},
    "shrimp": {"kcal": 99, "protein": 24.0, "fat": 0.3, "carbs": 0.2, "price": 170},
    "tomato_sauce": {"kcal": 45, "protein": 1.5, "fat": 1.0, "carbs": 7.0, "price": 24},
    "cream_sauce": {"kcal": 220, "protein": 3.0, "fat": 20.0, "carbs": 7.0, "price": 55},
    "pesto": {"kcal": 430, "protein": 5.0, "fat": 42.0, "carbs": 8.0, "price": 115},
    "pepper": {"kcal": 31, "protein": 1.0, "fat": 0.3, "carbs": 6.0, "price": 35},
    "romaine": {"kcal": 17, "protein": 1.2, "fat": 0.3, "carbs": 3.3, "price": 55},
    "arugula": {"kcal": 25, "protein": 2.6, "fat": 0.7, "carbs": 3.7, "price": 80},
    "tuna": {"kcal": 132, "protein": 29.0, "fat": 1.0, "carbs": 0.0, "price": 95},
    "lemon": {"kcal": 22, "protein": 0.4, "fat": 0.2, "carbs": 6.9, "price": 30},
    "balsamic": {"kcal": 88, "protein": 0.5, "fat": 0.0, "carbs": 17.0, "price": 40},
    "broccoli": {"kcal": 34, "protein": 2.8, "fat": 0.4, "carbs": 6.6, "price": 40},
    "zucchini": {"kcal": 17, "protein": 1.2, "fat": 0.3, "carbs": 3.1, "price": 30},
    "beef": {"kcal": 250, "protein": 26.0, "fat": 15.0, "carbs": 0.0, "price": 150},
    "potato": {"kcal": 77, "protein": 2.0, "fat": 0.1, "carbs": 17.0, "price": 12},
    "beans": {"kcal": 127, "protein": 8.7, "fat": 0.5, "carbs": 22.8, "price": 25},
    "teriyaki": {"kcal": 89, "protein": 5.9, "fat": 0.0, "carbs": 15.5, "price": 35},
    "garlic_yogurt": {"kcal": 90, "protein": 5.0, "fat": 4.0, "carbs": 7.0, "price": 38},
}


# =========================
# 3. БАЗА ДАННЫХ
# =========================

class Storage:
    def __init__(self, database_url: str) -> None:
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(database_url, future=True, connect_args=connect_args)
        self.metadata = MetaData()

        self.profiles = Table(
            "profiles",
            self.metadata,
            Column("user_id", BigInteger, primary_key=True),
            Column("kcal_goal", Float),
            Column("protein_goal", Float),
            Column("fat_goal", Float),
            Column("carbs_goal", Float),
            Column("created_at", String(32), nullable=False),
            Column("updated_at", String(32), nullable=False),
        )

        self.orders = Table(
            "orders",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("user_id", BigInteger, nullable=False),
            Column("username", String(255)),
            Column("meal_id", String(64), nullable=False),
            Column("dish_id", String(64), nullable=False),
            Column("items_json", Text, nullable=False),
            Column("totals_json", Text, nullable=False),
            Column("price", Float, nullable=False, default=0),
            Column("status", String(64), nullable=False),
            Column("created_at", String(32), nullable=False),
        )

        self.templates = Table(
            "templates",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("user_id", BigInteger, nullable=False),
            Column("name", String(255), nullable=False),
            Column("meal_id", String(64), nullable=False),
            Column("dish_id", String(64), nullable=False),
            Column("items_json", Text, nullable=False),
            Column("created_at", String(32), nullable=False),
            Column("updated_at", String(32), nullable=False),
        )

        self.item_overrides = Table(
            "item_overrides",
            self.metadata,
            Column("item_id", String(64), primary_key=True),
            Column("name", String(255)),
            Column("unit", String(16)),
            Column("step", Float),
            Column("kcal", Float),
            Column("protein", Float),
            Column("fat", Float),
            Column("carbs", Float),
            Column("price", Float),
            Column("is_active", Boolean, nullable=False, default=True),
            Column("updated_at", String(32), nullable=False),
        )

        self.menu_extensions = Table(
            "menu_extensions",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("meal_id", String(64), nullable=False),
            Column("dish_id", String(64), nullable=False),
            Column("category_id", String(64), nullable=False),
            Column("item_id", String(64), nullable=False),
            Column("created_at", String(32), nullable=False),
        )

    def init(self) -> None:
        self.metadata.create_all(self.engine)

    @staticmethod
    def now() -> str:
        return datetime.utcnow().isoformat(timespec="seconds")

    @staticmethod
    def row_to_dict(row: Any) -> dict[str, Any] | None:
        return dict(row._mapping) if row else None

    def get_profile(self, user_id: int) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = conn.execute(select(self.profiles).where(self.profiles.c.user_id == user_id)).first()
            return self.row_to_dict(row)

    def upsert_profile(self, user_id: int, kcal: float, protein: float, fat: float, carbs: float) -> None:
        now = self.now()
        with self.engine.begin() as conn:
            row = conn.execute(select(self.profiles.c.user_id).where(self.profiles.c.user_id == user_id)).first()
            values = {
                "kcal_goal": kcal,
                "protein_goal": protein,
                "fat_goal": fat,
                "carbs_goal": carbs,
                "updated_at": now,
            }
            if row:
                conn.execute(update(self.profiles).where(self.profiles.c.user_id == user_id).values(**values))
            else:
                conn.execute(insert(self.profiles).values(user_id=user_id, created_at=now, **values))

    def save_order(
        self,
        user_id: int,
        username: str | None,
        meal_id: str,
        dish_id: str,
        items: dict[str, float],
        totals: dict[str, float],
        price: float,
        status: str,
    ) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(
                insert(self.orders).values(
                    user_id=user_id,
                    username=username,
                    meal_id=meal_id,
                    dish_id=dish_id,
                    items_json=json.dumps(items, ensure_ascii=False),
                    totals_json=json.dumps(totals, ensure_ascii=False),
                    price=price,
                    status=status,
                    created_at=self.now(),
                )
            )
            return int(result.inserted_primary_key[0])

    def save_template(self, user_id: int, name: str, meal_id: str, dish_id: str, items: dict[str, float]) -> int:
        now = self.now()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.templates).where(
                    (self.templates.c.user_id == user_id) & (self.templates.c.name == name)
                )
            ).first()
            payload = {
                "meal_id": meal_id,
                "dish_id": dish_id,
                "items_json": json.dumps(items, ensure_ascii=False),
                "updated_at": now,
            }
            if row:
                template_id = int(row._mapping["id"])
                conn.execute(update(self.templates).where(self.templates.c.id == template_id).values(**payload))
                return template_id
            result = conn.execute(
                insert(self.templates).values(user_id=user_id, name=name, created_at=now, **payload)
            )
            return int(result.inserted_primary_key[0])

    def list_templates(self, user_id: int) -> list[dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(self.templates)
                .where(self.templates.c.user_id == user_id)
                .order_by(self.templates.c.updated_at.desc())
            ).all()
            return [dict(row._mapping) for row in rows]

    def get_template(self, user_id: int, template_id: int) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.templates).where(
                    (self.templates.c.user_id == user_id) & (self.templates.c.id == template_id)
                )
            ).first()
            return self.row_to_dict(row)

    def delete_template(self, user_id: int, template_id: int) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                delete(self.templates).where(
                    (self.templates.c.user_id == user_id) & (self.templates.c.id == template_id)
                )
            )
            return bool(result.rowcount)

    def get_item_override(self, item_id: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = conn.execute(select(self.item_overrides).where(self.item_overrides.c.item_id == item_id)).first()
            return self.row_to_dict(row)

    def list_item_overrides(self) -> list[dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = conn.execute(select(self.item_overrides)).all()
            return [dict(row._mapping) for row in rows]

    def upsert_item_override(
        self,
        item_id: str,
        *,
        name: str | None = None,
        unit: str | None = None,
        step: float | None = None,
        kcal: float | None = None,
        protein: float | None = None,
        fat: float | None = None,
        carbs: float | None = None,
        price: float | None = None,
        is_active: bool | None = None,
    ) -> None:
        now = self.now()
        with self.engine.begin() as conn:
            existing = conn.execute(select(self.item_overrides).where(self.item_overrides.c.item_id == item_id)).first()
            if existing:
                current = dict(existing._mapping)
                values = {
                    "name": name if name is not None else current.get("name"),
                    "unit": unit if unit is not None else current.get("unit"),
                    "step": step if step is not None else current.get("step"),
                    "kcal": kcal if kcal is not None else current.get("kcal"),
                    "protein": protein if protein is not None else current.get("protein"),
                    "fat": fat if fat is not None else current.get("fat"),
                    "carbs": carbs if carbs is not None else current.get("carbs"),
                    "price": price if price is not None else current.get("price"),
                    "is_active": is_active if is_active is not None else current.get("is_active", True),
                    "updated_at": now,
                }
                conn.execute(update(self.item_overrides).where(self.item_overrides.c.item_id == item_id).values(**values))
            else:
                conn.execute(
                    insert(self.item_overrides).values(
                        item_id=item_id,
                        name=name,
                        unit=unit,
                        step=step,
                        kcal=kcal,
                        protein=protein,
                        fat=fat,
                        carbs=carbs,
                        price=price,
                        is_active=True if is_active is None else is_active,
                        updated_at=now,
                    )
                )

    def add_menu_extension(self, meal_id: str, dish_id: str, category_id: str, item_id: str) -> None:
        now = self.now()
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.menu_extensions.c.id).where(
                    (self.menu_extensions.c.meal_id == meal_id)
                    & (self.menu_extensions.c.dish_id == dish_id)
                    & (self.menu_extensions.c.category_id == category_id)
                    & (self.menu_extensions.c.item_id == item_id)
                )
            ).first()
            if not row:
                conn.execute(
                    insert(self.menu_extensions).values(
                        meal_id=meal_id,
                        dish_id=dish_id,
                        category_id=category_id,
                        item_id=item_id,
                        created_at=now,
                    )
                )

    def list_menu_extensions(self, meal_id: str | None = None, dish_id: str | None = None) -> list[dict[str, Any]]:
        stmt = select(self.menu_extensions)
        if meal_id is not None:
            stmt = stmt.where(self.menu_extensions.c.meal_id == meal_id)
        if dish_id is not None:
            stmt = stmt.where(self.menu_extensions.c.dish_id == dish_id)
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).all()
            return [dict(row._mapping) for row in rows]


storage = Storage(DATABASE_URL)
storage.init()


# =========================
# 4. СОСТОЯНИЕ
# =========================

class AmountInput(StatesGroup):
    waiting_for_amount = State()


class NormInput(StatesGroup):
    waiting_for_norms = State()


class TemplateNameInput(StatesGroup):
    waiting_for_name = State()


@dataclass
class Cart:
    meal_id: str
    dish_id: str
    items: dict[str, float]


USER_CARTS: dict[int, Cart] = {}
router = Router()


# =========================
# 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def currency_symbol() -> str:
    return CURRENCY_SYMBOLS.get(PRICE_CURRENCY, PRICE_CURRENCY)


def format_money(value: float) -> str:
    return f"{value:.0f} {currency_symbol()}" if value == int(value) else f"{value:.2f} {currency_symbol()}"


def format_amount(amount: float, unit: str) -> str:
    if amount == int(amount):
        return f"{int(amount)} {unit}"
    return f"{amount:.1f} {unit}"


def format_macro(value: float) -> str:
    return f"{value:.0f}" if value == int(value) else f"{value:.1f}"


def parse_floats(values: list[str]) -> list[float] | None:
    try:
        return [float(value.replace(",", ".")) for value in values]
    except ValueError:
        return None


def base_item_def(item_id: str) -> dict[str, Any] | None:
    for dishes in MENU.values():
        for dish in dishes:
            for category in dish["categories"]:
                for item in category["items"]:
                    if item["id"] == item_id:
                        return dict(item)
    return None


def all_item_ids() -> list[str]:
    ids: set[str] = set(DEFAULT_ITEM_VALUES)
    for dishes in MENU.values():
        for dish in dishes:
            for category in dish["categories"]:
                for item in category["items"]:
                    ids.add(item["id"])
    for ext in storage.list_menu_extensions():
        ids.add(ext["item_id"])
    return sorted(ids)


def effective_item_meta(item_id: str) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "kcal": 0.0,
        "protein": 0.0,
        "fat": 0.0,
        "carbs": 0.0,
        "price": 0.0,
        "is_active": True,
    }
    meta.update(DEFAULT_ITEM_VALUES.get(item_id, {}))
    override = storage.get_item_override(item_id)
    if override:
        for key in ["name", "unit", "step", "kcal", "protein", "fat", "carbs", "price", "is_active"]:
            if override.get(key) is not None:
                meta[key] = override[key]
    return meta


def apply_item_overrides(item: dict[str, Any]) -> dict[str, Any]:
    item = dict(item)
    meta = effective_item_meta(item["id"])
    for key in ["name", "unit", "step", "is_active"]:
        if key in meta and meta[key] is not None:
            item[key] = meta[key]
    item.setdefault("is_active", True)
    return item


def get_dish(meal_id: str, dish_id: str) -> dict[str, Any] | None:
    base: dict[str, Any] | None = None
    for dish in MENU.get(meal_id, []):
        if dish["id"] == dish_id:
            base = copy.deepcopy(dish)
            break
    if not base:
        return None

    for category in base["categories"]:
        category["items"] = [apply_item_overrides(item) for item in category["items"]]

    for ext in storage.list_menu_extensions(meal_id, dish_id):
        category = next((cat for cat in base["categories"] if cat["id"] == ext["category_id"]), None)
        if not category:
            continue
        if any(item["id"] == ext["item_id"] for item in category["items"]):
            continue
        meta = effective_item_meta(ext["item_id"])
        category["items"].append(
            {
                "id": ext["item_id"],
                "name": meta.get("name") or ext["item_id"],
                "unit": meta.get("unit") or "г",
                "step": float(meta.get("step") or 10),
                "is_active": bool(meta.get("is_active", True)),
            }
        )
    return base


def get_category(dish: dict[str, Any] | None, category_id: str) -> dict[str, Any] | None:
    if not dish:
        return None
    for category in dish["categories"]:
        if category["id"] == category_id:
            return category
    return None


def get_item(dish: dict[str, Any] | None, item_id: str) -> dict[str, Any] | None:
    if not dish:
        return None
    for category in dish["categories"]:
        for item in category["items"]:
            if item["id"] == item_id:
                return item
    return None


def get_item_category(dish: dict[str, Any] | None, item_id: str) -> dict[str, Any] | None:
    if not dish:
        return None
    for category in dish["categories"]:
        if any(item["id"] == item_id for item in category["items"]):
            return category
    return None


def ensure_cart(user_id: int, meal_id: str, dish_id: str) -> Cart:
    current = USER_CARTS.get(user_id)
    if current is None or current.meal_id != meal_id or current.dish_id != dish_id:
        current = Cart(meal_id=meal_id, dish_id=dish_id, items={})
        USER_CARTS[user_id] = current
    return current


def calc_item(item: dict[str, Any], amount: float) -> dict[str, float]:
    meta = effective_item_meta(item["id"])
    unit = item.get("unit", meta.get("unit", "г"))
    factor = amount if unit == "шт" else amount / 100
    return {
        "kcal": float(meta.get("kcal") or 0) * factor,
        "protein": float(meta.get("protein") or 0) * factor,
        "fat": float(meta.get("fat") or 0) * factor,
        "carbs": float(meta.get("carbs") or 0) * factor,
        "price": float(meta.get("price") or 0) * factor,
    }


def calc_cart(cart: Cart) -> dict[str, Any]:
    dish = get_dish(cart.meal_id, cart.dish_id)
    totals = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "price": 0.0}
    rows: list[dict[str, Any]] = []
    if not dish:
        return {"rows": rows, "totals": totals}

    for category in dish["categories"]:
        for item in category["items"]:
            amount = cart.items.get(item["id"], 0)
            if amount <= 0:
                continue
            item_calc = calc_item(item, amount)
            for key in totals:
                totals[key] += item_calc[key]
            rows.append(
                {
                    "category": category["name"],
                    "item_id": item["id"],
                    "name": item["name"],
                    "unit": item["unit"],
                    "amount": amount,
                    **item_calc,
                }
            )
    return {"rows": rows, "totals": totals}


def nutrition_line(values: dict[str, float]) -> str:
    return (
        f"{format_macro(values['kcal'])} ккал; "
        f"Б {format_macro(values['protein'])} г; "
        f"Ж {format_macro(values['fat'])} г; "
        f"У {format_macro(values['carbs'])} г"
    )


def cart_lines(cart: Cart, with_details: bool = True) -> list[str]:
    calculated = calc_cart(cart)
    if not calculated["rows"]:
        return ["Пока ничего не добавлено."]

    lines: list[str] = []
    current_category = ""
    for row in calculated["rows"]:
        if row["category"] != current_category:
            current_category = row["category"]
            lines.append(f"{current_category}:")
        if with_details:
            lines.append(
                f"• {row['name']}: {format_amount(row['amount'], row['unit'])} — "
                f"{nutrition_line(row)} — {format_money(row['price'])}"
            )
        else:
            lines.append(f"• {row['name']}: {format_amount(row['amount'], row['unit'])}")
    return lines


def profile_text(user_id: int, totals: dict[str, float] | None = None) -> str:
    profile = storage.get_profile(user_id)
    if not profile:
        return (
            "🎯 Персональные нормы КБЖУ не заданы.\n"
            "Нажмите «Изменить нормы» или отправьте /norms и введите: ккал белки жиры углеводы."
        )

    text = (
        "🎯 Ваши нормы на день:\n"
        f"• Ккал: {format_macro(profile['kcal_goal'])}\n"
        f"• Белки: {format_macro(profile['protein_goal'])} г\n"
        f"• Жиры: {format_macro(profile['fat_goal'])} г\n"
        f"• Углеводы: {format_macro(profile['carbs_goal'])} г"
    )
    if totals:
        lines = ["", "Доля текущего блюда:"]
        mapping = [
            ("kcal", "kcal_goal", "Ккал", ""),
            ("protein", "protein_goal", "Белки", " г"),
            ("fat", "fat_goal", "Жиры", " г"),
            ("carbs", "carbs_goal", "Углеводы", " г"),
        ]
        for total_key, goal_key, label, suffix in mapping:
            goal = float(profile.get(goal_key) or 0)
            value = float(totals.get(total_key) or 0)
            percent = value / goal * 100 if goal > 0 else 0
            lines.append(f"• {label}: {format_macro(value)}{suffix} / {format_macro(goal)}{suffix} ({percent:.0f}%)")
        text += "\n" + "\n".join(lines)
    return text


def dish_text(meal_id: str, dish_id: str, user_id: int) -> str:
    dish = get_dish(meal_id, dish_id)
    cart = ensure_cart(user_id, meal_id, dish_id)
    if not dish:
        return "Блюдо не найдено."
    totals = calc_cart(cart)["totals"]
    return (
        f"{MEALS[meal_id]} → {dish['name']}\n\n"
        f"{dish['description']}\n\n"
        "Выберите категорию ингредиентов. Количество каждого ингредиента можно менять кнопками или ввести вручную.\n\n"
        "Текущий состав:\n"
        + "\n".join(cart_lines(cart, with_details=False))
        + "\n\n"
        + f"Итого сейчас: {nutrition_line(totals)}; цена {format_money(totals['price'])}"
    )


def item_text(meal_id: str, dish_id: str, item_id: str, user_id: int) -> str:
    dish = get_dish(meal_id, dish_id)
    item = get_item(dish, item_id)
    category = get_item_category(dish, item_id)
    if not dish:
        return "Блюдо не найдено."
    if not item or not category:
        return "Ингредиент не найден."

    cart = ensure_cart(user_id, meal_id, dish_id)
    current_amount = cart.items.get(item_id, 0)
    current_calc = calc_item(item, current_amount)
    step_calc = calc_item(item, float(item["step"]))
    basis = "на 1 шт" if item["unit"] == "шт" else f"на 100 {item['unit']}"
    meta = effective_item_meta(item_id)

    return (
        f"{category['name']} → {item['name']}\n\n"
        f"Текущее количество: {format_amount(current_amount, item['unit'])}\n"
        f"Шаг изменения: {format_amount(item['step'], item['unit'])}\n\n"
        f"КБЖУ текущего количества: {nutrition_line(current_calc)}\n"
        f"Цена текущего количества: {format_money(current_calc['price'])}\n\n"
        f"Справочно {basis}: {nutrition_line(meta)}; цена {format_money(float(meta.get('price') or 0))}\n"
        f"За один шаг: {nutrition_line(step_calc)}; цена {format_money(step_calc['price'])}\n\n"
        "Нажмите + / - или введите свое количество."
    )


def summary_text(user_id: int, meal_id: str, dish_id: str) -> str:
    cart = ensure_cart(user_id, meal_id, dish_id)
    dish = get_dish(meal_id, dish_id)
    dish_name = dish["name"] if dish else "Блюдо"
    calculated = calc_cart(cart)
    totals = calculated["totals"]
    return (
        f"📋 Итоговый состав\n"
        f"{MEALS[meal_id]} → {dish_name}\n\n"
        + "\n".join(cart_lines(cart, with_details=True))
        + "\n\n"
        + f"Итого: {nutrition_line(totals)}\n"
        + f"Цена: {format_money(totals['price'])}\n\n"
        + profile_text(user_id, totals)
    )


def save_current_order(user_id: int, username: str | None, cart: Cart, status: str) -> int:
    calculated = calc_cart(cart)
    totals = calculated["totals"]
    return storage.save_order(
        user_id=user_id,
        username=username,
        meal_id=cart.meal_id,
        dish_id=cart.dish_id,
        items=cart.items,
        totals=totals,
        price=totals["price"],
        status=status,
    )


async def safe_edit(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup)
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=reply_markup)
    await callback.answer()


# =========================
# 6. КЛАВИАТУРЫ
# =========================

def main_kb(user_id: int | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for meal_id, meal_name in MEALS.items():
        builder.button(text=meal_name, callback_data=f"meal:{meal_id}")
    builder.button(text="📁 Мои шаблоны", callback_data="templates")
    builder.button(text="🎯 Мои нормы", callback_data="profile")
    if user_id and is_admin(user_id):
        builder.button(text="⚙️ Админ-панель", callback_data="admin")
    builder.adjust(1)
    return builder.as_markup()


def dishes_kb(meal_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for dish in MENU[meal_id]:
        builder.button(text=dish["name"], callback_data=f"dish:{meal_id}:{dish['id']}")
    builder.button(text="⬅️ Назад", callback_data="main")
    builder.adjust(1)
    return builder.as_markup()


def builder_kb(meal_id: str, dish_id: str) -> InlineKeyboardMarkup:
    dish = get_dish(meal_id, dish_id)
    builder = InlineKeyboardBuilder()
    if dish:
        for category in dish["categories"]:
            builder.button(text=category["name"], callback_data=f"cat:{meal_id}:{dish_id}:{category['id']}")
    builder.button(text="📋 Показать итог", callback_data=f"summary:{meal_id}:{dish_id}")
    builder.button(text="💾 Сохранить как шаблон", callback_data=f"tpl_save:{meal_id}:{dish_id}")
    builder.button(text="🧹 Очистить", callback_data=f"clear:{meal_id}:{dish_id}")
    builder.button(text="⬅️ К блюдам", callback_data=f"meal:{meal_id}")
    builder.adjust(1)
    return builder.as_markup()


def category_kb(meal_id: str, dish_id: str, category_id: str) -> InlineKeyboardMarkup:
    dish = get_dish(meal_id, dish_id)
    category = get_category(dish, category_id)
    builder = InlineKeyboardBuilder()
    if category:
        for item in category["items"]:
            if item.get("is_active", True):
                builder.button(
                    text=item["name"],
                    callback_data=f"item:{meal_id}:{dish_id}:{category_id}:{item['id']}",
                )
    builder.button(text="⬅️ К категориям", callback_data=f"dish:{meal_id}:{dish_id}")
    builder.adjust(1)
    return builder.as_markup()


def item_kb(meal_id: str, dish_id: str, category_id: str, item_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➖", callback_data=f"dec:{meal_id}:{dish_id}:{category_id}:{item_id}")
    builder.button(text="➕", callback_data=f"inc:{meal_id}:{dish_id}:{category_id}:{item_id}")
    builder.button(text="✍️ Свое количество", callback_data=f"custom:{meal_id}:{dish_id}:{category_id}:{item_id}")
    builder.button(text="🗑 Убрать", callback_data=f"remove:{meal_id}:{dish_id}:{category_id}:{item_id}")
    builder.button(text="⬅️ К ингредиентам", callback_data=f"cat:{meal_id}:{dish_id}:{category_id}")
    builder.button(text="📋 Итог", callback_data=f"summary:{meal_id}:{dish_id}")
    builder.adjust(2, 1, 1, 1, 1)
    return builder.as_markup()


def summary_kb(meal_id: str, dish_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Сохранить заказ", callback_data=f"done:{meal_id}:{dish_id}")
    builder.button(text="💾 Сохранить шаблон", callback_data=f"tpl_save:{meal_id}:{dish_id}")
    builder.button(text="📄 Экспорт PDF", callback_data=f"pdf:{meal_id}:{dish_id}")
    builder.button(text="📊 Экспорт Google Sheets", callback_data=f"sheets:{meal_id}:{dish_id}")
    builder.button(text="⬅️ Продолжить сборку", callback_data=f"dish:{meal_id}:{dish_id}")
    builder.button(text="🧹 Очистить", callback_data=f"clear:{meal_id}:{dish_id}")
    builder.button(text="🏠 Главное меню", callback_data="main")
    builder.adjust(1)
    return builder.as_markup()


def profile_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Изменить нормы", callback_data="norms_edit")
    builder.button(text="🏠 Главное меню", callback_data="main")
    builder.adjust(1)
    return builder.as_markup()


def templates_kb(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    templates = storage.list_templates(user_id)
    for template in templates:
        builder.button(text=f"📥 {template['name']}", callback_data=f"tpl_load:{template['id']}")
        builder.button(text="🗑", callback_data=f"tpl_delete:{template['id']}")
    builder.button(text="🏠 Главное меню", callback_data="main")
    builder.adjust(2)
    return builder.as_markup()


def admin_panel_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🥦 Ингредиенты", callback_data="adm:items")
    builder.button(text="❔ Команды", callback_data="adm:help")
    builder.button(text="🏠 Главное меню", callback_data="main")
    builder.adjust(1)
    return builder.as_markup()


def admin_items_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item_id in all_item_ids():
        builder.button(text=item_id, callback_data=f"adm:item:{item_id}")
    builder.button(text="⬅️ Админ-панель", callback_data="admin")
    builder.adjust(2)
    return builder.as_markup()


# =========================
# 7. ЭКСПОРТ
# =========================

def generate_pdf(cart: Cart, user_id: int) -> Path:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise RuntimeError("Не установлен reportlab. Выполните: pip install reportlab") from exc

    font_name = "Helvetica"
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for font_path in font_candidates:
        if Path(font_path).exists():
            pdfmetrics.registerFont(TTFont("DejaVuSans", font_path))
            font_name = "DejaVuSans"
            break

    dish = get_dish(cart.meal_id, cart.dish_id)
    calculated = calc_cart(cart)
    totals = calculated["totals"]
    filename = EXPORT_DIR / f"order_{user_id}_{uuid.uuid4().hex[:8]}.pdf"

    doc = SimpleDocTemplate(str(filename), pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        style.fontName = font_name

    elements: list[Any] = []
    elements.append(Paragraph("Итоговый состав блюда", styles["Title"]))
    meal_label = MEALS.get(cart.meal_id, cart.meal_id).split(" ", 1)[-1]
    elements.append(Paragraph(f"{meal_label} -> {dish['name'] if dish else cart.dish_id}", styles["Heading2"]))
    elements.append(Spacer(1, 8))

    data = [["Категория", "Ингредиент", "Кол-во", "Ккал", "Б", "Ж", "У", "Цена"]]
    for row in calculated["rows"]:
        data.append(
            [
                row["category"],
                row["name"],
                format_amount(row["amount"], row["unit"]),
                format_macro(row["kcal"]),
                format_macro(row["protein"]),
                format_macro(row["fat"]),
                format_macro(row["carbs"]),
                format_money(row["price"]),
            ]
        )
    data.append(["", "Итого", "", format_macro(totals["kcal"]), format_macro(totals["protein"]), format_macro(totals["fat"]), format_macro(totals["carbs"]), format_money(totals["price"])])

    table = Table(data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, -1), (-1, -1), colors.whitesmoke),
            ]
        )
    )
    elements.append(table)
    elements.append(Spacer(1, 12))
    profile_for_pdf = profile_text(user_id, totals).replace("🎯 ", "").replace("\n", "<br/>")
    elements.append(Paragraph(profile_for_pdf, styles["BodyText"]))
    doc.build(elements)
    return filename


def export_to_google_sheets(user_id: int, username: str | None, cart: Cart) -> str:
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID:
        raise RuntimeError(
            "Google Sheets не настроен. Укажите GOOGLE_SERVICE_ACCOUNT_JSON и GOOGLE_SHEET_ID в переменных окружения."
        )
    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError("Не установлен gspread. Выполните: pip install gspread google-auth") from exc

    dish = get_dish(cart.meal_id, cart.dish_id)
    calculated = calc_cart(cart)
    totals = calculated["totals"]
    items_text = "; ".join(f"{row['name']} {format_amount(row['amount'], row['unit'])}" for row in calculated["rows"])

    gc = gspread.service_account(filename=GOOGLE_SERVICE_ACCOUNT_JSON)
    spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(GOOGLE_SHEET_WORKSHEET)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=GOOGLE_SHEET_WORKSHEET, rows=1000, cols=20)

    header = [
        "created_at",
        "user_id",
        "username",
        "meal",
        "dish",
        "items",
        "kcal",
        "protein",
        "fat",
        "carbs",
        "price",
        "currency",
    ]
    if not worksheet.row_values(1):
        worksheet.append_row(header, value_input_option="USER_ENTERED")

    worksheet.append_row(
        [
            Storage.now(),
            user_id,
            username or "",
            MEALS.get(cart.meal_id, cart.meal_id),
            dish["name"] if dish else cart.dish_id,
            items_text,
            round(totals["kcal"], 1),
            round(totals["protein"], 1),
            round(totals["fat"], 1),
            round(totals["carbs"], 1),
            round(totals["price"], 2),
            PRICE_CURRENCY,
        ],
        value_input_option="USER_ENTERED",
    )
    return f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}"


# =========================
# 8. ОСНОВНЫЕ ХЕНДЛЕРЫ
# =========================

@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Я бот-конструктор еды.\n\n"
        "Выберите прием пищи, затем блюдо и соберите его из ингредиентов в нужном количестве.\n\n"
        "Команды: /profile, /norms, /templates."
        + ("\nАдмин: /admin" if is_admin(message.from_user.id) else ""),
        reply_markup=main_kb(message.from_user.id),
    )


@router.callback_query(F.data == "main")
async def show_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(callback, "Выберите прием пищи:", reply_markup=main_kb(callback.from_user.id))


@router.callback_query(F.data.startswith("meal:"))
async def show_dishes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, meal_id = callback.data.split(":")
    meal_name = MEALS.get(meal_id)
    if not meal_name:
        await callback.answer("Прием пищи не найден", show_alert=True)
        return
    await safe_edit(callback, f"{meal_name}\n\nВыберите блюдо-конструктор:", reply_markup=dishes_kb(meal_id))


@router.callback_query(F.data.startswith("dish:"))
async def show_dish(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, meal_id, dish_id = callback.data.split(":")
    if not get_dish(meal_id, dish_id):
        await callback.answer("Блюдо не найдено", show_alert=True)
        return
    ensure_cart(callback.from_user.id, meal_id, dish_id)
    await safe_edit(callback, dish_text(meal_id, dish_id, callback.from_user.id), reply_markup=builder_kb(meal_id, dish_id))


@router.callback_query(F.data.startswith("cat:"))
async def show_category(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, meal_id, dish_id, category_id = callback.data.split(":")
    dish = get_dish(meal_id, dish_id)
    category = get_category(dish, category_id)
    if not category:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    await safe_edit(callback, f"{category['name']}\n\nВыберите ингредиент:", reply_markup=category_kb(meal_id, dish_id, category_id))


@router.callback_query(F.data.startswith("item:"))
async def show_item(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, meal_id, dish_id, category_id, item_id = callback.data.split(":")
    await safe_edit(callback, item_text(meal_id, dish_id, item_id, callback.from_user.id), reply_markup=item_kb(meal_id, dish_id, category_id, item_id))


@router.callback_query(F.data.startswith("inc:") | F.data.startswith("dec:"))
async def change_amount(callback: CallbackQuery) -> None:
    action, meal_id, dish_id, category_id, item_id = callback.data.split(":")
    dish = get_dish(meal_id, dish_id)
    item = get_item(dish, item_id)
    if not item:
        await callback.answer("Ингредиент не найден", show_alert=True)
        return

    cart = ensure_cart(callback.from_user.id, meal_id, dish_id)
    current_amount = cart.items.get(item_id, 0)
    step = float(item["step"])
    new_amount = current_amount + step if action == "inc" else max(0, current_amount - step)
    if new_amount == 0:
        cart.items.pop(item_id, None)
    else:
        cart.items[item_id] = new_amount

    await safe_edit(callback, item_text(meal_id, dish_id, item_id, callback.from_user.id), reply_markup=item_kb(meal_id, dish_id, category_id, item_id))


@router.callback_query(F.data.startswith("remove:"))
async def remove_item(callback: CallbackQuery) -> None:
    _, meal_id, dish_id, category_id, item_id = callback.data.split(":")
    cart = ensure_cart(callback.from_user.id, meal_id, dish_id)
    cart.items.pop(item_id, None)
    await safe_edit(callback, item_text(meal_id, dish_id, item_id, callback.from_user.id), reply_markup=item_kb(meal_id, dish_id, category_id, item_id))


@router.callback_query(F.data.startswith("custom:"))
async def ask_custom_amount(callback: CallbackQuery, state: FSMContext) -> None:
    _, meal_id, dish_id, category_id, item_id = callback.data.split(":")
    dish = get_dish(meal_id, dish_id)
    item = get_item(dish, item_id)
    if not item:
        await callback.answer("Ингредиент не найден", show_alert=True)
        return

    await state.update_data(meal_id=meal_id, dish_id=dish_id, category_id=category_id, item_id=item_id)
    await state.set_state(AmountInput.waiting_for_amount)
    await safe_edit(
        callback,
        f"Введите количество для ингредиента «{item['name']}» в {item['unit']}.\n\n"
        "Например: 150\n"
        "Чтобы убрать ингредиент, введите 0.",
        reply_markup=None,
    )


@router.message(AmountInput.waiting_for_amount)
async def set_custom_amount(message: Message, state: FSMContext) -> None:
    raw_value = (message.text or "").replace(",", ".").strip()
    try:
        amount = float(raw_value)
    except ValueError:
        await message.answer("Введите число. Например: 150")
        return
    if amount < 0 or amount > 10000:
        await message.answer("Количество должно быть от 0 до 10000.")
        return

    data = await state.get_data()
    meal_id = data["meal_id"]
    dish_id = data["dish_id"]
    category_id = data["category_id"]
    item_id = data["item_id"]
    cart = ensure_cart(message.from_user.id, meal_id, dish_id)
    if amount == 0:
        cart.items.pop(item_id, None)
    else:
        cart.items[item_id] = amount

    await state.clear()
    await message.answer(item_text(meal_id, dish_id, item_id, message.from_user.id), reply_markup=item_kb(meal_id, dish_id, category_id, item_id))


@router.callback_query(F.data.startswith("summary:"))
async def show_summary(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, meal_id, dish_id = callback.data.split(":")
    await safe_edit(callback, summary_text(callback.from_user.id, meal_id, dish_id), reply_markup=summary_kb(meal_id, dish_id))


@router.callback_query(F.data.startswith("clear:"))
async def clear_cart(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, meal_id, dish_id = callback.data.split(":")
    USER_CARTS[callback.from_user.id] = Cart(meal_id=meal_id, dish_id=dish_id, items={})
    await safe_edit(callback, dish_text(meal_id, dish_id, callback.from_user.id), reply_markup=builder_kb(meal_id, dish_id))


@router.callback_query(F.data.startswith("done:"))
async def finish_order(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, meal_id, dish_id = callback.data.split(":")
    cart = ensure_cart(callback.from_user.id, meal_id, dish_id)
    if not cart.items:
        await callback.answer("Сначала добавьте ингредиенты", show_alert=True)
        return
    order_id = save_current_order(callback.from_user.id, callback.from_user.username, cart, status="saved")
    await safe_edit(
        callback,
        summary_text(callback.from_user.id, meal_id, dish_id)
        + f"\n\n✅ Заказ #{order_id} сохранен в базе. Можно сделать новый конструктор через главное меню.",
        reply_markup=main_kb(callback.from_user.id),
    )


# =========================
# 9. ПРОФИЛЬ И НОРМЫ
# =========================

@router.message(Command("profile"))
async def show_profile_command(message: Message) -> None:
    await message.answer(profile_text(message.from_user.id), reply_markup=profile_kb())


@router.callback_query(F.data == "profile")
async def show_profile_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(callback, profile_text(callback.from_user.id), reply_markup=profile_kb())


@router.message(Command("norms"))
async def norms_command(message: Message, state: FSMContext) -> None:
    args = (message.text or "").split()[1:]
    if len(args) == 4:
        values = parse_floats(args)
        if values:
            kcal, protein, fat, carbs = values
            storage.upsert_profile(message.from_user.id, kcal, protein, fat, carbs)
            await state.clear()
            await message.answer("✅ Нормы КБЖУ сохранены.\n\n" + profile_text(message.from_user.id), reply_markup=main_kb(message.from_user.id))
            return
    await state.set_state(NormInput.waiting_for_norms)
    await message.answer(
        "Введите ваши дневные нормы в формате:\n\n"
        "ккал белки жиры углеводы\n\n"
        "Например: 2200 140 70 260"
    )


@router.callback_query(F.data == "norms_edit")
async def norms_edit_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NormInput.waiting_for_norms)
    await safe_edit(
        callback,
        "Введите ваши дневные нормы в формате:\n\nккал белки жиры углеводы\n\nНапример: 2200 140 70 260",
        reply_markup=None,
    )


@router.message(NormInput.waiting_for_norms)
async def set_norms(message: Message, state: FSMContext) -> None:
    values = parse_floats((message.text or "").split())
    if not values or len(values) != 4:
        await message.answer("Нужно 4 числа: ккал белки жиры углеводы. Например: 2200 140 70 260")
        return
    kcal, protein, fat, carbs = values
    if min(values) <= 0 or kcal > 10000:
        await message.answer("Значения должны быть положительными. Ккал — не больше 10000.")
        return
    storage.upsert_profile(message.from_user.id, kcal, protein, fat, carbs)
    await state.clear()
    await message.answer("✅ Нормы КБЖУ сохранены.\n\n" + profile_text(message.from_user.id), reply_markup=main_kb(message.from_user.id))


# =========================
# 10. ШАБЛОНЫ
# =========================

@router.message(Command("templates"))
async def templates_command(message: Message) -> None:
    templates = storage.list_templates(message.from_user.id)
    if not templates:
        await message.answer("У вас пока нет шаблонов. Соберите блюдо и нажмите «Сохранить шаблон».", reply_markup=main_kb(message.from_user.id))
        return
    await message.answer("📁 Ваши шаблоны:", reply_markup=templates_kb(message.from_user.id))


@router.callback_query(F.data == "templates")
async def templates_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    templates = storage.list_templates(callback.from_user.id)
    text = "📁 Ваши шаблоны:" if templates else "У вас пока нет шаблонов. Соберите блюдо и нажмите «Сохранить шаблон»."
    await safe_edit(callback, text, reply_markup=templates_kb(callback.from_user.id))


@router.callback_query(F.data.startswith("tpl_save:"))
async def ask_template_name(callback: CallbackQuery, state: FSMContext) -> None:
    _, meal_id, dish_id = callback.data.split(":")
    cart = ensure_cart(callback.from_user.id, meal_id, dish_id)
    if not cart.items:
        await callback.answer("Сначала добавьте ингредиенты", show_alert=True)
        return
    await state.update_data(meal_id=meal_id, dish_id=dish_id)
    await state.set_state(TemplateNameInput.waiting_for_name)
    await safe_edit(callback, "Введите название шаблона. Например: Мой завтрак на тренировочный день", reply_markup=None)


@router.message(TemplateNameInput.waiting_for_name)
async def save_template_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 80:
        await message.answer("Название должно быть от 1 до 80 символов.")
        return
    data = await state.get_data()
    meal_id = data["meal_id"]
    dish_id = data["dish_id"]
    cart = ensure_cart(message.from_user.id, meal_id, dish_id)
    template_id = storage.save_template(message.from_user.id, name, meal_id, dish_id, cart.items)
    await state.clear()
    await message.answer(f"✅ Шаблон «{name}» сохранен, ID {template_id}.", reply_markup=main_kb(message.from_user.id))


@router.callback_query(F.data.startswith("tpl_load:"))
async def load_template(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _, template_id_raw = callback.data.split(":")
    template = storage.get_template(callback.from_user.id, int(template_id_raw))
    if not template:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    items = json.loads(template["items_json"])
    USER_CARTS[callback.from_user.id] = Cart(meal_id=template["meal_id"], dish_id=template["dish_id"], items={k: float(v) for k, v in items.items()})
    await safe_edit(
        callback,
        f"✅ Шаблон «{template['name']}» загружен.\n\n" + dish_text(template["meal_id"], template["dish_id"], callback.from_user.id),
        reply_markup=builder_kb(template["meal_id"], template["dish_id"]),
    )


@router.callback_query(F.data.startswith("tpl_delete:"))
async def delete_template_callback(callback: CallbackQuery) -> None:
    _, template_id_raw = callback.data.split(":")
    deleted = storage.delete_template(callback.from_user.id, int(template_id_raw))
    await safe_edit(
        callback,
        "🗑 Шаблон удален." if deleted else "Шаблон не найден.",
        reply_markup=templates_kb(callback.from_user.id),
    )


# =========================
# 11. ЭКСПОРТ И КУХНЯ
# =========================

@router.callback_query(F.data.startswith("pdf:"))
async def export_pdf_callback(callback: CallbackQuery) -> None:
    _, meal_id, dish_id = callback.data.split(":")
    cart = ensure_cart(callback.from_user.id, meal_id, dish_id)
    if not cart.items:
        await callback.answer("Сначала добавьте ингредиенты", show_alert=True)
        return
    try:
        pdf_path = generate_pdf(cart, callback.from_user.id)
    except RuntimeError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if callback.message:
        await callback.message.answer_document(FSInputFile(pdf_path), caption="📄 PDF с итоговым составом")
    await callback.answer("PDF сформирован")


@router.callback_query(F.data.startswith("sheets:"))
async def export_sheets_callback(callback: CallbackQuery) -> None:
    _, meal_id, dish_id = callback.data.split(":")
    cart = ensure_cart(callback.from_user.id, meal_id, dish_id)
    if not cart.items:
        await callback.answer("Сначала добавьте ингредиенты", show_alert=True)
        return
    try:
        link = export_to_google_sheets(callback.from_user.id, callback.from_user.username, cart)
    except RuntimeError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if callback.message:
        await callback.message.answer(f"📊 Состав экспортирован в Google Sheets:\n{link}")
    await callback.answer("Экспортировано")



# =========================
# 12. АДМИН-ПАНЕЛЬ
# =========================

def admin_help_text() -> str:
    return (
        "⚙️ Админ-команды:\n\n"
        "/admin — открыть панель\n"
        "/item_info chicken — посмотреть ингредиент\n"
        "/set_item chicken 165 31 3.6 0 70 — задать КБЖУ и цену\n"
        "/set_item chicken 165 31 3.6 0 70 50 — то же + шаг\n"
        "/rename_item chicken Куриная грудка — переименовать\n"
        "/hide_item chicken — скрыть из меню\n"
        "/show_item chicken — вернуть в меню\n\n"
        "Добавить ингредиент в существующую категорию:\n"
        "/add_item meal_id dish_id category_id item_id | Название | unit | step | kcal | protein | fat | carbs | price\n\n"
        "Пример:\n"
        "/add_item breakfast oat_bowl fruit mango | Манго | г | 30 | 60 | 0.8 | 0.4 | 15 | 25\n\n"
        "КБЖУ и цена задаются на 100 г/мл, а для unit=шт — на 1 штуку."
    )


def item_info_text(item_id: str) -> str:
    base = base_item_def(item_id) or {}
    meta = effective_item_meta(item_id)
    unit = meta.get("unit") or base.get("unit", "г")
    step = meta.get("step") or base.get("step", 10)
    name = meta.get("name") or base.get("name", item_id)
    basis = "на 1 шт" if unit == "шт" else f"на 100 {unit}"
    active = "да" if meta.get("is_active", True) else "нет"
    return (
        f"🥦 {name}\n"
        f"ID: {item_id}\n"
        f"Активен: {active}\n"
        f"Единица: {unit}\n"
        f"Шаг: {format_amount(float(step), unit)}\n"
        f"КБЖУ {basis}: {nutrition_line(meta)}\n"
        f"Цена {basis}: {format_money(float(meta.get('price') or 0))}"
    )


async def deny_if_not_admin(message: Message) -> bool:
    if not is_admin(message.from_user.id):
        await message.answer("Эта команда доступна только администратору. Укажите ADMIN_IDS в .env/переменных окружения.")
        return True
    return False


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if await deny_if_not_admin(message):
        return
    await message.answer("⚙️ Админ-панель", reply_markup=admin_panel_kb())


@router.callback_query(F.data == "admin")
async def admin_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await safe_edit(callback, "⚙️ Админ-панель", reply_markup=admin_panel_kb())


@router.callback_query(F.data == "adm:items")
async def admin_items_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await safe_edit(callback, "Выберите ингредиент:", reply_markup=admin_items_kb())


@router.callback_query(F.data.startswith("adm:item:"))
async def admin_item_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    item_id = callback.data.split(":", 2)[2]
    await safe_edit(callback, item_info_text(item_id) + "\n\n" + admin_help_text(), reply_markup=admin_panel_kb())


@router.callback_query(F.data == "adm:help")
async def admin_help_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await safe_edit(callback, admin_help_text(), reply_markup=admin_panel_kb())


@router.message(Command("item_info"))
async def item_info_command(message: Message) -> None:
    if await deny_if_not_admin(message):
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2:
        await message.answer("Формат: /item_info chicken")
        return
    await message.answer(item_info_text(args[1].strip()))


@router.message(Command("set_item"))
async def set_item_command(message: Message) -> None:
    if await deny_if_not_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) not in {7, 8}:
        await message.answer("Формат: /set_item item_id kcal protein fat carbs price [step]")
        return
    item_id = parts[1]
    values = parse_floats(parts[2:])
    if not values:
        await message.answer("КБЖУ, цена и шаг должны быть числами.")
        return
    kcal, protein, fat, carbs, price = values[:5]
    step = values[5] if len(values) == 6 else None
    storage.upsert_item_override(item_id, kcal=kcal, protein=protein, fat=fat, carbs=carbs, price=price, step=step)
    await message.answer("✅ Ингредиент обновлен.\n\n" + item_info_text(item_id))


@router.message(Command("rename_item"))
async def rename_item_command(message: Message) -> None:
    if await deny_if_not_admin(message):
        return
    _, _, args = (message.text or "").partition(" ")
    item_id, _, name = args.partition(" ")
    if not item_id or not name.strip():
        await message.answer("Формат: /rename_item item_id Новое название")
        return
    storage.upsert_item_override(item_id.strip(), name=name.strip())
    await message.answer("✅ Название обновлено.\n\n" + item_info_text(item_id.strip()))


@router.message(Command("hide_item"))
async def hide_item_command(message: Message) -> None:
    if await deny_if_not_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: /hide_item item_id")
        return
    storage.upsert_item_override(parts[1], is_active=False)
    await message.answer("✅ Ингредиент скрыт.\n\n" + item_info_text(parts[1]))


@router.message(Command("show_item"))
async def show_item_command(message: Message) -> None:
    if await deny_if_not_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: /show_item item_id")
        return
    storage.upsert_item_override(parts[1], is_active=True)
    await message.answer("✅ Ингредиент снова активен.\n\n" + item_info_text(parts[1]))


@router.message(Command("add_item"))
async def add_item_command(message: Message) -> None:
    if await deny_if_not_admin(message):
        return
    raw = (message.text or "").partition(" ")[2].strip()
    chunks = [chunk.strip() for chunk in raw.split("|")]
    if len(chunks) != 9:
        await message.answer(
            "Формат:\n"
            "/add_item meal_id dish_id category_id item_id | Название | unit | step | kcal | protein | fat | carbs | price"
        )
        return
    ids = chunks[0].split()
    if len(ids) != 4:
        await message.answer("В первой части нужны 4 ID: meal_id dish_id category_id item_id")
        return
    meal_id, dish_id, category_id, item_id = ids
    if meal_id not in MEALS or not get_dish(meal_id, dish_id) or not get_category(get_dish(meal_id, dish_id), category_id):
        await message.answer("meal_id, dish_id или category_id не найдены.")
        return
    name, unit = chunks[1], chunks[2]
    values = parse_floats(chunks[3:])
    if not values or len(values) != 6:
        await message.answer("step, kcal, protein, fat, carbs и price должны быть числами.")
        return
    step, kcal, protein, fat, carbs, price = values
    storage.upsert_item_override(
        item_id,
        name=name,
        unit=unit,
        step=step,
        kcal=kcal,
        protein=protein,
        fat=fat,
        carbs=carbs,
        price=price,
        is_active=True,
    )
    storage.add_menu_extension(meal_id, dish_id, category_id, item_id)
    await message.answer("✅ Ингредиент добавлен в меню.\n\n" + item_info_text(item_id))


# =========================
# 14. ЗАПУСК
# =========================

async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не найден BOT_TOKEN. Создайте бота через @BotFather и передайте токен: "
            "export BOT_TOKEN='ваш_токен'"
        )

    bot = Bot(token=token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
