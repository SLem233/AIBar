# AIBar — дашборд лимитов AI-провайдеров для Windows

Аналог [CodexBar-KDE](https://github.com/EvilFreelancer/CodexBar-KDE) для Windows:
иконка в системном трее с радиальным индикатором квот + всплывающий дашборд
с карточками провайдеров.

![Дашборд](docs/dashboard.png)

## Что показывает

- **Иконка в трее** — два кольца: сессия (синее, внешнее) и неделя (оранжевое,
  внутреннее) самого загруженного провайдера, в центре — процент сессии.
- **Дашборд** (клик по иконке) — карточка на провайдера: радиальный индикатор,
  тариф, все окна лимитов с процентами и обратным отсчётом до сброса,
  дополнительные кредиты.
- **Виджет на рабочем столе** (включён по умолчанию) — полупрозрачная колонка
  круговых индикаторов по провайдерам, висит поверх всех окон:
  - перетаскивается мышью за любое место;
  - размер меняется за уголок справа внизу (позиция и размер запоминаются);
  - при наведении мыши рядом всплывает панель с расширенной информацией;
  - правый клик — меню (обновить / настройки / справка / скрыть / выход),
    включается и выключается из меню трея («Виджет поверх окон»).
- **Справка** — пункт «Справка» в меню виджета и трея открывает встроенную
  HTML-инструкцию (`aibar/resources/help.html`) в браузере.

## Провайдеры

| Провайдер | Источник авторизации | API |
|-----------|----------------------|-----|
| Claude (Claude Code) | `~/.claude/.credentials.json` | `api.anthropic.com/api/oauth/usage` |
| Codex (ChatGPT) | `~/.codex/auth.json` | `chatgpt.com/backend-api/wham/usage` |
| Cursor | `%APPDATA%\Cursor\...\state.vscdb` | `cursor.com/api/usage-summary` |
| Z.ai / zcode | API-ключ в настройках или `Z_AI_API_KEY` | `api.z.ai/api/monitor/usage/quota/limit` |
| OpenCode | cookie `auth` с opencode.ai (в настройках) | `opencode.ai/_server` (RPC) |

Провайдеры включаются в **Настройках** (меню трея или правый клик по виджету).
Claude, Codex и Cursor работают без ключей — приложение только читает токены,
которые поддерживают сами приложения (`claude`, `codex`, Cursor). Если токен
истёк, достаточно запустить соответствующий CLI/приложение.

Для **Z.ai (zcode)** нужен API-ключ coding-плана: z.ai → Manage API Key →
Coding Plan (или bigmodel.cn для китайского региона — переключатель в
настройках). Для **OpenCode** — cookie `auth` со страницы opencode.ai
(DevTools → Application → Cookies), workspace `wrk_…` определяется
автоматически.

## Установка и запуск

Готовый exe: `dist\AIBar.exe` — запускается двойным кликом, ничего
устанавливать не нужно.

Из исходников:

```powershell
pip install -r requirements.txt
# запуск без консоли
.\AIBar.bat
# или с консолью (для отладки)
python -m aibar.main
```

Пересборка exe:

```powershell
pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name AIBar --icon assets\aibar.ico --add-data "aibar/resources;aibar/resources" run_aibar.py
```

### Автозапуск

Скопировать ярлык на `AIBar.bat` в папку автозагрузки: `Win+R` → `shell:startup`.

## Настройки

`%APPDATA%\AIBar\config.json`:

```json
{
  "refresh_seconds": 300,
  "providers": ["Claude", "Codex", "Cursor"],
  "widget_enabled": true,
  "widget_geometry": [1650, 60, 120, 260],
  "zai_api_key": "",
  "zai_region": "global",
  "opencode_cookie": "",
  "opencode_workspace": ""
}
```

Всё это редактируется через диалог «Настройки…» — руками файл трогать не нужно.

Интервал обновления также переключается из контекстного меню иконки в трее.

## Структура

```
aibar/
├── main.py            # трей-приложение, поллинг по таймеру
├── config.py          # настройки в %APPDATA%\AIBar
├── theme.py           # тёмная палитра (валидирована по CVD/контрасту)
├── providers/
│   ├── base.py        # модель данных (RateWindow, ProviderSnapshot)
│   ├── claude.py      # Claude OAuth usage
│   ├── codex.py       # Codex (ChatGPT backend) usage
│   ├── cursor.py      # Cursor (usage-summary)
│   ├── zai.py         # Z.ai coding plan (zcode)
│   └── opencode.py    # OpenCode (opencode.ai)
└── ui/
    ├── gauge.py       # радиальный многокольцевой индикатор (QPainter)
    ├── dashboard.py   # всплывающее окно с карточками
    ├── settings.py    # диалог настроек
    └── widget.py      # виджет поверх окон + ховер-панель
```

## Как добавить провайдера

1. Создать `aibar/providers/<name>.py` с функцией `fetch() -> ProviderSnapshot`.
2. Зарегистрировать её в `PROVIDERS` в `aibar/providers/__init__.py`.
3. Добавить имя в `providers` в конфиге.
