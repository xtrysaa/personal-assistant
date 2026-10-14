# 🎓 Личный ИИ-ассистент Альнура (Telegram-бот)

Персональный ассистент по расписанию, дедлайнам и подготовке к поступлению.
Понимает свободный текст и голосовые, сам ставит задачи в базу, сам пишет напоминания
и умеет планировать — с учётом твоих целей: **Air Astana Ab Initio**, SAT, IELTS, ЕНТ,
школьный GPA, тренировки и время на отдых.

```
Ты:  15 ноября дедлайн подачи документов в Air Astana Ab Initio
Бот:  ✅ Записал: #1 Air Astana Ab Initio — дедлайн документов
      Срок: сб, 15 ноя, 23:59 (через 31 день)
      Напомню за неделю, за сутки и за час.

Ты:  Помоги распланировать повторение математики к субботе
Бот:  Взял твои задачи на неделю. Предлагаю блоки: ...
```

---

## Содержание

1. [Что умеет](#1-что-умеет)
2. [Какой хостинг выбрать](#2-какой-хостинг-выбрать)
3. [Структура проекта](#3-структура-проекта)
4. [Шаг 1. Бот в Telegram (BotFather)](#шаг-1-бот-в-telegram-botfather)
5. [Шаг 2. Ключи API](#шаг-2-ключи-api)
6. [Шаг 3. Запуск локально](#шаг-3-запуск-локально)
7. [Шаг 4. Деплой 24/7 на Oracle Cloud (бесплатно)](#шаг-4-деплой-247-на-oracle-cloud-бесплатно)
8. [Альтернатива: Render](#альтернатива-render-если-не-хочется-возиться-с-сервером)
9. [Как устроены напоминания](#9-как-устроены-напоминания)
10. [Проверка и диагностика](#10-проверка-и-диагностика)
11. [Если что-то не работает](#11-если-что-то-не-работает)
12. [Сколько это стоит](#12-сколько-это-стоит)
13. [Что можно добавить дальше](#13-что-можно-добавить-дальше)

---

## 1. Что умеет

| Возможность | Как это работает |
|---|---|
| **Понимание свободного текста** | LLM (DeepSeek через OpenRouter) с function calling сам решает, что это задача, и вызывает `add_task` |
| **Голосовые сообщения** 🎤 | Скачиваю аудио, расшифровываю через Whisper (Groq — бесплатно), дальше как обычный текст |
| **Проактивные напоминания** | За неделю, за сутки, за час до срока — бот пишет сам, без твоего запроса |
| **Утренний дайджест** | Каждый день в 07:30: что сегодня, что просрочено, что на неделе |
| **Недельный обзор** | В воскресенье 19:00: итоги, план на неделю, где перегруз |
| **Планирование** | «Помоги распланировать повторение к субботе» — сначала смотрит реальные задачи в базе, потом предлагает план |
| **Контекст о тебе** | В системном промпте: РФМШ, 11 класс, цель — пилот, SAT/IELTS/ЕНТ, тренировки, девушка — чтобы советы были по делу |
| **Кнопки** | Под списком дел — кнопки «✅ #12», закрывают задачу одним тапом |
| **Приватность** | Бот отвечает только владельцу по `OWNER_TELEGRAM_ID` |

Бот **не выдумывает** задачи: список дел он всегда берёт из базы через `list_tasks`,
а после создания видит реальный `id` и дату. Это ключевая часть архитектуры — см. [раздел 9](#9-как-устроены-напоминания).

---

## 2. Какой хостинг выбрать

Коротко: **Oracle Cloud Always Free** — единственный вариант, который реально даёт 24/7
и не теряет базу данных. Остальное — компромиссы.

| Вариант | 24/7? | Данные сохраняются? | Сложность | Вердикт |
|---|---|---|---|---|
| **Oracle Cloud Always Free** (VM, AMD или ARM) | ✅ да | ✅ диск сохраняется | средняя (нужен SSH) | **Рекомендую** |
| Render Free (Web Service) | ⚠️ засыпает через 15 мин простоя | ❌ ФС эфемерная | низкая | Ок для старта, нужен внешний пинг + бэкап |
| Railway | ⚠️ только триал-кредиты | ❌ | низкая | После кредитов платно |
| PythonAnywhere Free | ❌ нет always-on фоновых процессов | ✅ | низкая | **Не подходит** для напоминаний |
| Koyeb / Hugging Face Spaces | ⚠️ засыпает | ❌ | низкая | Для теста |
| Свой ПК / старый ноутбук | ⚠️ пока включён | ✅ | низкая | Бесплатно, но зависит от света и интернета |

**Почему не PythonAnywhere:** на бесплатном тарифе там нет постоянно работающих фоновых
процессов (Always-on tasks только на платном), а бот без планировщика — это просто блокнот.

**Почему Oracle:** это настоящая виртуальная машина (ARM Ampere, до 4 ядра / 24 ГБ RAM),
которая в рамках Always Free работает круглосуточно и не списывает деньги. Бот с SQLite
живёт на её диске, ничего не теряется, публичный домен и SSL не нужны — Telegram long polling
работает через исходящие соединения. Главный минус — нужно один раз зайти по SSH.

---

## 3. Структура проекта

```
PersonalAssistent/
├── run.py                  # точка входа: бот + планировщик + health-check сервер
├── requirements.txt        # зависимости
├── .env.example            # шаблон настроек (копируется в .env)
├── .gitignore              # .env и data/ не попадают в git
├── Dockerfile              # для деплоя в Docker (опционально)
├── README.md
├── bot/
│   ├── __init__.py
│   ├── config.py           # чтение и валидация .env, объект Settings
│   ├── timeutil.py         # парсинг дат, часовые пояса, «через 3 дня»
│   ├── db.py               # SQLite: задачи, напоминания, история диалога
│   ├── formatting.py       # рендер задач в HTML для Telegram
│   ├── prompts.py          # системный промпт: кто ты, цели, правила, инструменты
│   ├── llm.py              # LLM + function calling (add/list/update/complete/delete)
│   ├── stt.py              # расшифровка голосовых (Whisper)
│   ├── scheduler.py        # APScheduler: напоминания, дайджест, обзор недели
│   ├── handlers.py         # команды, свободный текст, голосовые, кнопки
│   └── health.py           # HTTP /health для Render и подобных
├── scripts/
│   └── smoke_test.py       # 40 проверок логики без Telegram и без сети
└── data/                   # создаётся автоматически: assistant.db
```

---

## Шаг 1. Бот в Telegram (BotFather)

1. Открой Telegram, найди **@BotFather** → `/newbot`.
2. Задай имя (например `Alnur Assistant`) и username, оканчивающийся на `bot`
   (например `alnur_assistant_bot`).
3. BotFather выдаст токен вида `8123456789:AAH...` — это `BOT_TOKEN`. **Никому не показывай.**
4. Полезно сразу:
   - `/setdescription` — описание бота;
   - `/setcommands` — можно не заполнять, бот сам выставит меню команд при запуске.
5. Узнай **свой** Telegram ID: напиши **@userinfobot** → он пришлёт `Id: 123456789`.
   Это `OWNER_TELEGRAM_ID`. Без него бот никого не пустит.

---

## Шаг 2. Ключи API

### 2.1. Чат (OpenRouter + DeepSeek) — обязательно

1. Зайди на **openrouter.ai** → Sign in → **Keys** → `Create Key`.
2. Скопируй ключ `sk-or-v1-...` → это `OPENAI_API_KEY`.
3. Проверь точный slug модели (на странице модели есть кнопка «Copy model slug»):

```bash
curl -s https://openrouter.ai/api/v1/models | python -c "import sys,json; print('\n'.join(m['id'] for m in json.load(sys.stdin)['data'] if 'deepseek' in m['id']))"
```

   В проекте по умолчанию стоит `deepseek/deepseek-v4.1-flash` — это дешёвый и быстрый
   вариант, который хорошо держит вызов инструментов.

### 2.2. Голосовые (Groq) — чтобы бот понимал голос

OpenRouter **не умеет** расшифровывать аудио, поэтому распознавание речи — отдельный сервис.

1. Зарегистрируйся на **console.groq.com** → **API Keys** → `Create API Key` (бесплатно).
2. Ключ `gsk_...` → `STT_API_KEY`. `STT_BASE_URL` можно не заполнять — подставится Groq автоматически.

Если `STT_API_KEY` не задан, голосовые будут работать только с официальным OpenAI
(`STT_MODEL=whisper-1`). Без ключа бот честно ответит «голосовые не настроены» и попросит текст.

---

## Шаг 3. Запуск локально

Нужен Python 3.10+ (проверь: `python --version`).

```bash
cd PersonalAssistent

# 1. Виртуальное окружение
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2. Зависимости
pip install -r requirements.txt

# 3. Настройки
# Windows:  copy .env.example .env
# macOS/Linux:  cp .env.example .env
```

Открой `.env` и заполни минимум это:

```dotenv
BOT_TOKEN=8123456789:AAH...              # из BotFather
OWNER_TELEGRAM_ID=123456789              # твой id из @userinfobot

OPENAI_API_KEY=sk-or-v1-...              # ключ OpenRouter
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_MODEL=deepseek/deepseek-v4.1-flash

STT_API_KEY=gsk_...                      # ключ Groq (для голосовых)

TZ=Asia/Almaty
DB_PATH=./data/assistant.db
DAILY_DIGEST_TIME=07:30
WEEKLY_REVIEW_DAY=sun
WEEKLY_REVIEW_TIME=19:00
```

Проверить логику, не запуская бота (без Telegram и без сети):

```bash
python scripts/smoke_test.py      # ожидается «✅ Все проверки пройдены»
```

Запустить бота:

```bash
python run.py
```

В логе должно появиться:

```
INFO Бот запущен: @alnur_assistant_bot | часовой пояс: Asia/Almaty | модель: deepseek/deepseek-v4.1-flash | голосовые: включены
```

Теперь напиши боту в Telegram — он должен ответить.

### Что проверить в первую очередь

```
15 ноября дедлайн подачи документов в Air Astana Ab Initio
Завтра в 15:00 контрольная по физике
Что у меня на этой неделе?
Помоги распланировать повторение математики к субботе
```

и надиктуй голосовое: «в субботу в 10 утра тренировка на шоссейнике».

---

## Шаг 4. Деплой 24/7 на Oracle Cloud (бесплатно)

### 4.1. Создать виртуальную машину

1. Регистрируйся на **cloud.oracle.com** (потребуется карта для верификации — списаний
   в рамках Always Free нет). Регион выбирай ближайший доступный (например Frankfurt).
2. Меню → **Compute → Instances → Create instance**:
   - **Image**: `Oracle Linux 9` (или `Ubuntu 24.04` — тогда ниже замени `dnf` на `apt`, а `opc` на `ubuntu`);
   - **Shape**: `VM.Standard.E2.1.Micro` (AMD, 1 OCPU / 1 ГБ RAM) — гарантированно Always Free
     и capacity всегда есть. ARM `VM.Standard.A1.Flex` мощнее, но часто пишет
     *Out of host capacity*;
   - **Add SSH keys**: `Generate a key pair` → скачай приватный ключ (`.key`);
   - **Create**.
3. Скопируй **Public IP address** инстанса.

> На E2.1.Micro всего 1 ГБ RAM. Боту этого хватает с запасом (он почти всё время спит
> в ожидании апдейтов), но `pip install` на такой памяти иногда падает — поэтому шаг 4.2
> начинается с создания swap-файла.

### 4.2. Зайти по SSH и поставить Python

Для Oracle Linux 9 пользователь по умолчанию — **`opc`** (не `ubuntu`):

```bash
ssh -i путь/до/ключа.key opc@<PUBLIC_IP>

# 2 ГБ swap — страховка от OOM при установке зависимостей на 1 ГБ RAM
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

sudo dnf update -y
sudo dnf install -y git python3.11 python3.11-pip
```

**Зачем именно python3.11, а не системный python3:** в Oracle Linux 9 по умолчанию
Python 3.9, а проекту нужен **3.10 минимум** (`@dataclass(slots=True)` в
[bot/config.py](bot/config.py) на 3.9 не работает).

Проверить, что нужная версия встала, и при желании найти другие:

```bash
python3.11 --version                  # ожидаем Python 3.11.x
dnf list available 'python3.1*'       # если нужен 3.12 — он тоже есть в AppStream
```

На заметку: `ufw`/`firewalld` и порты открывать **не нужно** — Telegram работает через
исходящие соединения, публичный домен и SSL не требуются. Единственное, что стоит разрешить
в VCN Security List — входящий SSH (22), он открыт по умолчанию.

### 4.3. Загрузить проект

Вариант А (через GitHub — рекомендую, потом легко обновлять):

```bash
# локально: залей проект в свой репозиторий (в .gitignore уже исключены .env и data/)
git init && git add . && git commit -m "Personal assistant bot"
git remote add origin https://github.com/<username>/<repo>.git && git push -u origin main

# на сервере:
git clone https://github.com/<username>/<repo>.git assistant
```

Вариант Б (копированием файлов, без git):

```bash
# локально, из папки проекта (исключаем .venv и базу):
scp -i ключ.key -r bot scripts run.py requirements.txt Dockerfile README.md opc@<IP>:/home/opc/assistant/
```

Дальше на сервере:

```bash
cd ~/assistant
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# создаём .env и заполняем значения из локального .env (в nano: Ctrl+O сохранить, Ctrl+X выйти)
nano .env
chmod 600 .env
```

> `.env` в репозиторий не попадает — перенеси значения вручную или скопируй файл
> отдельной командой `scp -i ключ.key .env opc@<IP>:/home/opc/assistant/.env`.
>
> Если репозиторий приватный, `git clone` попросит логин и пароль — вместо пароля
> нужен Personal Access Token: `git clone https://<USERNAME>:<TOKEN>@github.com/<USERNAME>/<repo>.git`

Проверка, что всё живое:

```bash
.venv/bin/python scripts/smoke_test.py    # Все проверки пройдены
.venv/bin/python run.py                   # запустится, напиши боту, потом Ctrl+C
```

### 4.4. Автозапуск через systemd

Создай юнит:

```bash
sudo nano /etc/systemd/system/assistant-bot.service
```

```ini
[Unit]
Description=Personal AI assistant Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/assistant
ExecStart=/home/opc/assistant/.venv/bin/python run.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Включи и запусти:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now assistant-bot
sudo systemctl status assistant-bot        # active (running)
journalctl -u assistant-bot -f             # живой лог (Ctrl+C — выйти)
```

Всё: бот работает 24/7 и сам поднимется после перезагрузки сервера.
Порт наружу открывать не нужно — Telegram работает через исходящие соединения,
`HEALTH_PORT` оставь пустым.

### 4.5. Обновить код на сервере

```bash
cd ~/assistant
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart assistant-bot
```

Совет: сделай `.env` доступным только тебе — `chmod 600 .env`.
Бэкап базы: `cp data/assistant.db ~/backup-$(date +%F).db` (можно повесить на cron раз в сутки).

SELinux в Oracle Linux включён, но настроек не требует: бот работает из `/home/opc/assistant`
и ничего не публикует наружу.

---

## Альтернатива: Render (если не хочется возиться с сервером)

Плюс — деплой в пару кликов из GitHub. Минусы — бесплатный инстанс засыпает после
15 минут без входящих запросов, **и файловая система эфемерная**: база SQLite обнулится
при каждом деплое/перезапуске (постоянный диск — платный).

1. Залей проект в GitHub (как в 4.3).
2. Render → **New → Web Service** → подключи репозиторий.
3. Настройки:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python run.py`
   - **Health Check Path**: `/health`
   - **Environment Variables**: всё из `.env` + `HEALTH_PORT=10000`
     (`10000` — внутренний порт Render; наш мини-сервер слушает именно его,
     чтобы Render считал сервис живым).
4. Чтобы бот не засыпал, заведи бесплатный монитор на **cron-job.org**:
   каждые 10 минут дёргать `https://<твой-сервис>.onrender.com/health`.

Напоминания при этом продолжат работать: они хранятся в базе, а не в памяти планировщика.
Но перед сном инстанса придётся мириться с потерей задач — поэтому для длительного
использования всё-таки Oracle.

---

## 9. Как устроены напоминания

Это самое важное архитектурное решение, поэтому объясню.

**Наивный подход** — держать отложенные задачи в памяти планировщика (`scheduler.add_job`).
На бесплатном хостинге он ломается: сервер перезапустился — все напоминания исчезли.

**Как сделано здесь:** напоминание — это строка в таблице `reminders`
(`task_id`, `offset_minutes`, `fire_at`, `sent_at`).

```
создаёшь задачу  →  _sync_reminders() создаёт строки на каждый интервал
раз в 60 секунд  →  _tick_reminders() берёт те, где fire_at <= сейчас и sent_at IS NULL
                 →  отправляет в Telegram  →  сразу помечает sent_at
```

Что это даёт:

- **Ничего не теряется** при перезапуске, деплое и падении сервера.
- **Нет дублей**: отправленное помечается в базе сразу после ответа Telegram.
- **Нет спама после простоя**: напоминания, просроченные больше чем на 6 часов,
  помечаются отправленными молча (`REMINDER_GRACE` в [bot/llm.py](bot/llm.py)).
- При переносе срока напоминания **пересчитываются** под новую дату.

Расписание по умолчанию — за неделю, за сутки, за час (`DEFAULT_REMIND_OFFSETS`).
Интервалы, которые уже в прошлом, не создаются: задача со сроком через 2 часа
получит только напоминание «за час».

---

## 10. Проверка и диагностика

```bash
# Логика без сети и Telegram (40 проверок)
python scripts/smoke_test.py

# Живые логи на сервере
journalctl -u assistant-bot -f

# Прямо в Telegram
/help      — справка и примеры
/today     — что сегодня
/week      — что на неделе
/all       — все открытые дела
/overdue   — просроченное
/done 12   — закрыть задачу
/reset     — сбросить контекст диалога (задачи остаются)
```

Состояние базы удобно смотреть через `sqlite3`:

```bash
sqlite3 data/assistant.db "SELECT id, title, due_at, status FROM tasks ORDER BY due_at;"
sqlite3 data/assistant.db "SELECT task_id, offset_minutes, fire_at, sent_at FROM reminders;"
```

---

## 11. Если что-то не работает

| Симптом | Причина и решение |
|---|---|
| `Ошибка конфигурации: Не задана переменная BOT_TOKEN` | Нет файла `.env` или не заполнены поля. Скопируй `.env.example` → `.env` |
| `TelegramUnauthorizedError` | Неверный `BOT_TOKEN` (скопирован не полностью) |
| `Conflict: terminated by other getUpdates request` | Запущено **два** экземпляра бота. Останови лишний: `sudo systemctl stop assistant-bot` или закрой локальное окно |
| Бот молчит, в логе `🔒 Отказано в доступе: id=...` | Пишешь не с того аккаунта. Свой id можно узнать командой `/id` |
| Модель отвечает ошибкой `No endpoints found` / `400` | Неверный `OPENAI_MODEL`. Возьми точный slug на openrouter.ai |
| `401` / `402` / `429` от OpenRouter | Неверный ключ или кончились кредиты/лимит |
| `⚠️ Не получилось получить ответ от модели` | Проверь `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` и баланс провайдера |
| Голосовое → «не настроены» | Нужен `STT_API_KEY` (Groq). OpenRouter аудио не расшифровывает |
| Напоминания не приходят | Проверь `TZ` (должен быть `Asia/Almaty`) и что задача не в прошлом. В базе: `SELECT * FROM reminders WHERE sent_at IS NULL;` |
| Время в задачах сдвинуто на 5 часов | Неверный `TZ` → напоминания считаются по другой зоне |
| `ModuleNotFoundError: tzdata` (Windows) | `pip install tzdata` |
| `database is locked` | Не запускай два процесса с одной базой |
| `UserWarning: Timezone offset does not match system offset` | Безвредно: системное время сервера не совпадает с `TZ`. Все даты бот считает по `TZ` из `.env` |

---

## 12. Сколько это стоит

| Компонент | Цена |
|---|---|
| Oracle Cloud Always Free (ARM VM) | **0 ₽** навсегда |
| OpenRouter + `deepseek/deepseek-v4.1-flash` | ~$0.0026 / млн входных, $0.60 / млн выходных токенов |
| Groq Whisper for голосовых | бесплатный тариф |

В реальности: обычное сообщение — это ~2–3 тыс. токенов промпта и пара сотен в ответе,
то есть **доли цента**. При активном использовании (несколько десятков сообщений в день)
выходит порядка **$1–3 в месяц**. Проверить расход можно в личном кабинете OpenRouter.

Хочешь совсем ноль — поставь бесплатную модель с пометкой `:free` в `OPENAI_MODEL`
(например `deepseek/deepseek-v4.1-flash:free`, если доступна). Минус: такие модели
медленнее и хуже держат вызов инструментов, что для трекера задач критично.

---

## 13. Что можно добавить дальше

Осмысленные следующие шаги, если захочешь развивать:

- **Фото и скриншоты** — модель `deepseek-v4.1-flash` мультимодальная, так что
  можно отправлять фото расписания или задания, и бот будет вытаскивать оттуда дедлайны.
- **Повторяющиеся задачи** («каждую неделю по воскресеньям разбор ошибок SAT») —
  добавить поле `recurrence` и пересоздавать задачу в `_tick_reminders`.
- **Дашборд подготовки** — отдельная таблица `progress` по SAT/IELTS/ЕНТ
  и команда `/progress` с графиком баллов.
- **Экспорт в календарь** — генерация `.ics` для Google Calendar.
- **Вечерний чек-ин** — «что успел сегодня?» с записью в базу, чтобы видеть реальный
  темп подготовки, а не только планы.
- **Трекер тренировок** — блоки под велосипед/хайкинг с учётом нагрузки, чтобы
  подготовка к экзаменам не съедала восстановление.
