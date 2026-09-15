# 🤖 Code Review Hub

Централизованная система автоматического AI code review для всех репозиториев.

## Возможности

- **🔍 Еженедельный полный AI-ревью** всех репозиториев (SOLID, безопасность, рефакторинг, тесты)
- **🔄 Авто-обнаружение** новых репозиториев
- **📤 Webhook уведомления** на любой endpoint
- **⚡ Context caching** для экономии на API-вызовах

## Быстрый старт

### 1. Создать репозиторий

```bash
# Этот репозиторий уже готов — просто запушьте его на GitHub
gh repo create code-review-hub --public --source . --push
```

### 2. Добавить секреты

В Settings → Secrets → Actions этого репозитория:

| Secret | Описание | Как получить |
|--------|----------|-------------|
| `GH_PAT` | GitHub Personal Access Token с правами `repo` + `workflow` | [Создать токен](https://github.com/settings/tokens/new?scopes=repo,workflow) |
| `WEBHOOK_URL` | *(опционально)* URL для получения отчётов | Ваш webhook endpoint |

### 3. Запустить вручную

В Actions → "Weekly AI Review — All Repos" → Run workflow

---

## Как подключить новый репозиторий

### Автоматически (рекомендуется)

1. Просто создайте репозиторий — hub обнаружит его при следующем запуске `sync-workflows`
2. Готово! Workflows качества кода появятся после синхронизации

### Исключить репозиторий

Добавьте имя в `exclude_repos` в `config.yml`:
```yaml
exclude_repos:
  - "code-review-hub"
  - "my-archived-project"
```

---

## Структура

```
code-review-hub/
├── config.yml                              # Настройки (модели, расписание, исключения)
├── scripts/
└── .github/workflows/
    ├── weekly-review-all.yml               # Еженедельный ревью ВСЕХ репо
    └── sync-workflows.yml                  # Авто-раскатка workflows качества кода в репо
```

## Workflows

| Workflow | Триггер | Что делает |
|----------|---------|-----------|
| `weekly-review-all` | Воскресенье 6:00 UTC / manual | Клонирует каждый репо → AI-ревью → GitHub Issue + webhook |
| `sync-workflows` | Понедельник 3:00 UTC / manual | Находит репо без PR workflow → создаёт его через API |

## Webhook

Отчёты отправляются как JSON POST на `WEBHOOK_URL`. Формат payload:

### Weekly Review
```json
{
  "event": "weekly_review_summary",
  "date": "2026-02-14",
  "owner": "st-VALVe",
  "repos": ["yotto-bot", "zvezdoball", "IGG"],
  "summary": "# Weekly AI Review Summary..."
}
```

### New Repos Synced
```json
{
  "event": "new_repos_synced",
  "new_repos": "new-project-name",
  "count": 1
}
```

## Конфигурация

Все настройки в `config.yml`:

```yaml
github_owner: "st-VALVe"          # Ваш GitHub username

exclude_repos:                     # Исключить из ревью
  - "code-review-hub"
  - "YOTTO-JS-bot"                 # renamed to yotto-bot

include_only: []                   # Если задан — ревьюятся ТОЛЬКО эти репо

skip_forks: true                   # Пропускать форки
skip_archived: true                # Пропускать архивные
```
