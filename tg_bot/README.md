# Telegram Travel Bot

## Быстрый старт

### 1. Клонируйте репозиторий
```sh
git clone <ваш-репозиторий>
cd <ваш-репозиторий>/tg_bot
```

### 2. Создайте виртуальное окружение (venv)
```sh
python3 -m venv venv
source venv/bin/activate  # macOS/Linux
# или
venv\Scripts\activate   # Windows
```

### 3. Установите зависимости
```sh
pip install -r requirements.txt
```

### 4. Создайте файл `.env` и заполните токены
Пример:
```
TG_BOT_TOKEN=ваш_токен_бота
AVIASALES_TOKEN=ваш_токен_авиасейлс
MISTRAL_TOKEN=ваш_токен_mistral
```

### 5. Запустите бота
```sh
python bot.py
```

---

## Примечания
- **.env** не коммитится в репозиторий (он в .gitignore).
- Все зависимости устанавливаются только в venv.
- Для деплоя на сервер повторите шаги 2–5.
- Для Docker-инструкции — напишите, подготовлю Dockerfile.

## Как это работает
- Пользователь пишет, что хочет найти.
- Бот отправляет запрос к leclat API для поиска по Ozon.
- Бот возвращает найденные товары с названиями и ссылками.

## Настройки
- `TELEGRAM_BOT_TOKEN` — токен вашего Telegram-бота.
- `LECLAT_API_KEY` — ключ для доступа к leclat API.
- `LECLAT_API_URL` — при необходимости замените на актуальный URL API. 