# venice-bridge на Windows

Порт для Windows 10/11. Python-часть (мост и адаптер) работает как есть. Заменены bash-лаунчер и автозапуск через launchd.

| macOS | Windows |
|---|---|
| `claude-venice` (bash) | `claude-venice.cmd` → `claude-venice.ps1` |
| `examples/*.plist` + `launchctl` | `start-venice.cmd [-Install]`, `stop-venice.cmd` |
| `python3` | `python` |
| `chmod 600` | не нужен: файлы в `%USERPROFILE%` и так доступны только вашей учётной записи |

## Установка

```powershell
winget install Python.Python.3.13
python -m pip install -r requirements.txt
```

`curl.exe` уже есть в Windows 10 1803 и новее, мост вызывает его напрямую.
Для `claude-venice` нужен Claude Code CLI: `irm https://claude.ai/install.ps1 | iex`.
Если установщик пишет, что `~\.local\bin` нет в PATH, добавьте эту папку в пользовательский PATH и перезапустите терминал.

## Cookie (один раз)

Это значение cookie `__client` с домена `clerk.venice.ai` из браузера, где вы залогинены на venice.ai
(F12 → Application → Cookies → `https://clerk.venice.ai` → `__client` → Value). Сохраните его:

```powershell
.\save-cookie.cmd
```

Скрипт спросит значение скрытым вводом, запишет его в `~\.config\venice-bridge\client-cookie.txt`
и попросит запущенный мост перечитать вход. Когда cookie истечёт, запустите его снова.

Запускайте всё через файлы `.cmd`: в Windows по умолчанию запуск `.ps1` запрещён, а обёртки
разрешают выполнение только для своего вызова и системную политику не меняют.

## Проверено вживую (Windows 11, Python 3.13, Claude Code 2.1.332)

- вход в Venice по cookie, `/health` → `venice_session: active`;
- запрос к модели через мост (`curl.exe` из Windows Cloudflare пропускает);
- вызов инструмента через адаптер в формате Anthropic Messages;
- `claude-venice -p ...`: ответ модели и создание файла инструментом Write.

## Запуск

```powershell
.\start-venice.cmd              # мост :18891 и адаптер :18892 в фоне
.\start-venice.cmd -BridgeOnly  # только мост (для OpenCode достаточно)
.\start-venice.cmd -Install     # плюс автозапуск при входе в Windows (Планировщик задач, без прав админа)
.\start-venice.cmd -Uninstall   # убрать автозапуск
.\stop-venice.cmd               # остановить
```

Логи пишутся в `%LOCALAPPDATA%\venice-bridge\logs\`.

## Claude Code

```powershell
.\claude-venice.cmd                          # модель по умолчанию deepseek-v4-1-flash-uncensored
.\claude-venice.cmd -m venice-auto --lean
.\claude-venice.cmd --models
.\claude-venice.cmd --quota
.\claude-venice.cmd --zai                    # и остальные бэкенды: --venice-api, --unquant, --go, --claude
```

Чтобы запускать просто `claude-venice` из любой папки, добавьте папку проекта в PATH.

Как и в оригинале, лаунчер по умолчанию **отключает запросы разрешений** (`--dangerously-skip-permissions`).
Чтобы вернуть запросы, задайте `$env:VENICE_CLAUDE_SKIP_PERMS = '0'`.

Отличие от bash-версии: с `--lean` пустой MCP-конфиг передаётся файлом `~\.claude-venice\empty-mcp.json`,
потому что Windows PowerShell 5.1 портит JSON, переданный строкой в аргументах.

## Тесты

```powershell
python test_venice_bridge.py
python test_venice_adapters.py
python test_claude_venice_windows.py
```
