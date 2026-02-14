# \U0001f916 Code Review Hub

Централизованная система автоматического AI code review для всех репозиториев.

## Возможности

- **\U0001f50d Еженедельный полный AI-ревью** всех репозиториев (SOLID, безопасность, рефакторинг, тесты)
- **\U0001f4dd PR-ревью** каждого pull request через Gemini AI
- **\U0001f504 Авто-обнаружение** новых репозиториев
- **\U0001f4e4 Webhook уведомления** на любой endpoint
- **\u26a1 Context caching** для экономии на API-вызовах

## Быстрый старт

### 1. Добавить секреты

В Settings \u2192 Secrets \u2192 Actions этого репозитория:

| Secret | Описание | Как получить |
|--------|----------|-------------|
| `GH_PAT` | GitHub Personal Access Token с правами `repo` + `workflow` | [Создать токен](https://github.com/settings/tokens/new?scopes=repo,workflow) |
| `GEMINI_API_KEY` | API-ключ Google Gemini | [Google AI Studio](https://aistudio.google.com/apikey) |
| `WEBHOOK_URL` | *(опционально)* URL для получения отчётов | Ваш webhook endpoint |

### 2. Добавить `GEMINI_API_KEY` в каждый репозиторий

Для PR-ревью через reusable workflows нужен секрет `GEMINI_API_KEY` в каждом репо.

> **Совет:** Если у вас GitHub Organization \u2014 добавьте `GEMINI_API_KEY` как organization secret с доступом ко всем репо.

### 3. Запустить вручную

В Actions \u2192 \"Weekly AI Review \u2014 All Repos\" \u2192 Run workflow

---

## Как подключить новый репозиторий

### Автоматически (рекомендуется)

1. Просто создайте репозиторий \u2014 hub обнаружит его при следующем запуске `sync-workflows`
2. Добавьте секрет `GEMINI_API_KEY` в новый репозиторий
3. Готово! Еженедельный ревью включится автоматически, PR-ревью \u2014 после синхронизации

### Вручную (для немедленного подключения)

1. Добавьте секрет `GEMINI_API_KEY` в новый репозиторий

2. Создайте файл `.github/workflows/ai-pr-review.yml`:
```yaml
name: AI PR Review
on:
  pull_request:
    types: [opened, synchronize, reopened]
jobs:
  review:
    uses: st-VALVe/code-review-hub/.github/workflows/reusable-pr-review.yml@main
    secrets:
      GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
```

3. Запустите `sync-workflows` вручную или дождитесь понедельника

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
\u251c\u2500\u2500 config.yml                              # Настройки (модели, расписание, исключения)
\u251c\u2500\u2500 scripts/
\u2502   \u2514\u2500\u2500 ai-review.py                        # Скрипт AI-анализа (Gemini API)
\u2514\u2500\u2500 .github/workflows/
    \u251c\u2500\u2500 weekly-review-all.yml               # Еженедельный ревью ВСЕХ репо
    \u251c\u2500\u2500 sync-workflows.yml                  # Авто-раскатка PR workflow в репо
    \u2514\u2500\u2500 reusable-pr-review.yml              # Reusable workflow для PR-ревью
```

## Workflows

| Workflow | Триггер | Что делает |
|----------|---------|----------|
| `weekly-review-all` | Воскресенье 6:00 UTC / manual | Клонирует каждый репо \u2192 AI-ревью \u2192 GitHub Issue + webhook |
| `sync-workflows` | Понедельник 3:00 UTC / manual | Находит репо без PR workflow \u2192 создаёт его через API |
| `reusable-pr-review` | Вызывается из каждого репо | Анализирует diff PR \u2192 комментарий в PR |

## Webhook

Отчёты отправляются как JSON POST на `WEBHOOK_URL`. Формат payload:

### Weekly Review
```json
{
  "event": "weekly_review_summary",
  "date": "2026-02-14",
  "owner": "st-VALVe",
  "repos": ["YOTTO-JS-bot", "zvezdoball", "IGG"],
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

gemini:
  weekly_model: "gemini-2.5-flash" # Модель для weekly review
  pr_model: "gemini-2.5-flash"     # Модель для PR review

exclude_repos:                     # Исключить из ревью
  - "code-review-hub"

include_only: []                   # Если задан \u2014 ревьюятся ТОЛЬКО эти репо

skip_forks: true                   # Пропускать форки
skip_archived: true                # Пропускать архивные
```
