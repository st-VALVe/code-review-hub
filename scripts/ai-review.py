#!/usr/bin/env python3
"""AI Code Review — supports Gemini API and Claude via Vertex AI.
   Large repos are automatically split into chunks and reviewed in parts."""

import os
import sys
import json
import argparse
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CODE_EXTENSIONS = {
    '.js', '.ts', '.tsx', '.jsx', '.py', '.json', '.yml', '.yaml',
    '.css', '.scss', '.html', '.sql', '.sh',
}

SKIP_DIRS = {
    'node_modules', '.git', 'dist', 'build', '.next', 'coverage',
    '__pycache__', '.cache', '.github', 'vendor', '.vscode',
}

MAX_FILE_SIZE = 50_000
CHUNK_SIZE = 800_000       # ~800 KB per chunk → fits in context window
MAX_TOTAL_SIZE = 4_000_000 # 4 MB total limit across all chunks

# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def collect_files(project_dir):
    files = {}
    total = 0
    for root, dirs, names in os.walk(project_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            if Path(name).suffix.lower() not in CODE_EXTENSIONS:
                continue
            fp = os.path.join(root, name)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if sz > MAX_FILE_SIZE or total + sz > MAX_TOTAL_SIZE:
                continue
            try:
                with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                    files[os.path.relpath(fp, project_dir)] = f.read()
                total += sz
            except OSError:
                continue
    return files


def build_code_context(files):
    parts = []
    for path, content in files.items():
        parts.append(f"### File: {path}\n```\n{content}\n```")
    return "\n\n".join(parts)


def split_into_chunks(files):
    """Split files into chunks that fit within CHUNK_SIZE."""
    chunks = []
    current_chunk = {}
    current_size = 0

    for path, content in files.items():
        file_size = len(content.encode('utf-8'))
        if current_size + file_size > CHUNK_SIZE and current_chunk:
            chunks.append(current_chunk)
            current_chunk = {}
            current_size = 0
        current_chunk[path] = content
        current_size += file_size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_FULL = """\
You are an expert senior software architect performing a comprehensive weekly code review.
Respond in **Russian**.

Analyse the provided codebase and produce a structured report covering:

1. **SOLID Compliance** (score 1-10 per principle + overall)
2. **Security Issues** — hardcoded secrets, injection risks, XSS, insecure data handling, missing input validation
3. **Refactoring Opportunities** — duplication, god objects, long methods, magic values, dead code, complex conditionals
4. **Test Coverage Gaps** — missing test files, untested critical paths, edge-cases, error-handling
5. **Architecture & Best Practices** — design patterns, error handling, logging, configuration management

Use **exactly** this output template:

## 🤖 Еженедельный AI Code Review

### Overall Health Score: X/10

### 📐 SOLID: X/10
| Принцип | Оценка | Комментарий |
|---------|--------|-------------|
| SRP | … | … |
| OCP | … | … |
| LSP | … | … |
| ISP | … | … |
| DIP | … | … |

### 🔒 Безопасность: N проблем
(список с 🚨 Critical / ❌ High / ⚠️ Medium / ℹ️ Low)

### ♻️ Рефакторинг: N возможностей
(список с приоритетом и рекомендуемым подходом)

### 🧪 Тестирование: N пробелов
(список)

### 🏗️ Архитектура
(ключевые наблюдения)

### 📋 Приоритетный план действий
1. …
"""

SYSTEM_CHUNK = """\
You are an expert senior software architect reviewing a PART of a codebase.
Respond in **Russian**.

This is chunk {chunk_num} of {total_chunks} of the full codebase.
Analyse ONLY the files provided below. Focus on:

1. **Security Issues** — hardcoded secrets, injection risks, XSS
2. **Refactoring Opportunities** — duplication, god objects, long methods
3. **SOLID violations** — note any violations with scores
4. **Test Coverage Gaps** — if relevant test files are in this chunk
5. **Architecture concerns** — patterns, error handling, logging

Be concise. Use bullet points. Mark severity: 🚨 Critical / ❌ High / ⚠️ Medium / ℹ️ Low
Start with: **Часть {chunk_num}/{total_chunks} — файлы:** (list files)
"""

SYSTEM_MERGE = """\
You are an expert senior software architect.
Respond in **Russian**.
You have received {total_chunks} partial code reviews of different parts of the same codebase.
Merge them into a single, coherent, deduplicated report.

Use **exactly** this output template:

## 🤖 Еженедельный AI Code Review

### Overall Health Score: X/10

### 📐 SOLID: X/10
| Принцип | Оценка | Комментарий |
|---------|--------|-------------|
| SRP | … | … |
| OCP | … | … |
| LSP | … | … |
| ISP | … | … |
| DIP | … | … |

### 🔒 Безопасность: N проблем
(merged list, deduplicated, with 🚨/❌/⚠️/ℹ️)

### ♻️ Рефакторинг: N возможностей
(merged list, deduplicated, prioritized)

### 🧪 Тестирование: N пробелов
(merged list)

### 🏗️ Архитектура
(overall observations across all chunks)

### 📋 Приоритетный план действий
1. …
"""

SYSTEM_PR = """\
You are an expert code reviewer analysing a pull request diff.
Respond in **Russian**. Be concise and actionable.

Focus on:
1. **Баги / логические ошибки**
2. **Безопасность** изменённого кода
3. **Нарушения SOLID**
4. **Качество кода** — читаемость, именование, сложность
5. **Конкретные предложения** по улучшению

Use **exactly** this output template:

## 🔍 AI Review PR

### Резюме
(1-2 предложения)

### Проблемы: N
(список с 🚨/❌/⚠️/ℹ️)

### Предложения
(конкретные улучшения)

### Вердикт: ✅ Approve / ⚠️ Needs Changes / 🚨 Critical Issues
"""

# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------

def call_gemini(system, user_text, api_key, model):
    """Single Gemini API call."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        gen_cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            max_output_tokens=8192,
        )
        resp = client.models.generate_content(
            model=model,
            contents=[types.Content(
                role="user",
                parts=[types.Part(text=user_text)],
            )],
            config=gen_cfg,
        )
        return resp.text
    except ImportError:
        return call_gemini_rest(system, user_text, api_key, model)


def call_gemini_rest(system, user_text, api_key, model):
    """Gemini REST fallback."""
    import urllib.request, urllib.error

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent?key={api_key}"
    )
    payload = json.dumps({
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
    }).encode()

    req = urllib.request.Request(url, payload, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.loads(r.read())
            return body["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.HTTPError as exc:
        err = exc.read().decode()
        print(f"Gemini REST error {exc.code}: {err}", file=sys.stderr)
        return f"❌ Gemini review failed: HTTP {exc.code}"
    except Exception as exc:
        return f"❌ Gemini review failed: {exc}"

# ---------------------------------------------------------------------------
# Claude provider (Vertex AI)
# ---------------------------------------------------------------------------

def call_claude(system, user_text, model, project_id, region):
    """Single Claude API call via Vertex AI."""
    try:
        from anthropic import AnthropicVertex
        client = AnthropicVertex(project_id=project_id, region=region)
        message = client.messages.create(
            model=model,
            max_tokens=8192,
            temperature=0.2,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        return "".join(b.text for b in message.content if hasattr(b, 'text'))
    except ImportError:
        return call_claude_rest(system, user_text, model, project_id, region)


def call_claude_rest(system, user_text, model, project_id, region):
    """Claude via Vertex AI REST."""
    import urllib.request, urllib.error, subprocess

    try:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True
        ).strip()
    except Exception as exc:
        return f"❌ Failed to get GCP access token: {exc}"

    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/"
        f"projects/{project_id}/locations/{region}/"
        f"publishers/anthropic/models/{model}:rawPredict"
    )
    payload = json.dumps({
        "anthropic_version": "vertex-2023-10-16",
        "max_tokens": 8192,
        "temperature": 0.2,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(url, payload, headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.loads(r.read())
            return "".join(b["text"] for b in body["content"] if b["type"] == "text")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode()
        print(f"Claude Vertex REST error {exc.code}: {err}", file=sys.stderr)
        return f"❌ Claude review failed: HTTP {exc.code}"
    except Exception as exc:
        return f"❌ Claude review failed: {exc}"

# ---------------------------------------------------------------------------
# Unified caller
# ---------------------------------------------------------------------------

def ai_call(system, user_text, provider, model, **kwargs):
    """Route a single AI call to the right provider."""
    if provider == "gemini":
        api_key = kwargs.get("api_key") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            sys.exit("Error: GEMINI_API_KEY not set")
        return call_gemini(system, user_text, api_key, model)
    elif provider == "claude":
        project_id = kwargs.get("gcp_project") or os.environ.get("GCP_PROJECT_ID")
        region = kwargs.get("gcp_region") or os.environ.get("GCP_REGION", "us-east5")
        if not project_id:
            sys.exit("Error: GCP_PROJECT_ID not set")
        return call_claude(system, user_text, model, project_id, region)
    else:
        sys.exit(f"Unknown provider: {provider}")

# ---------------------------------------------------------------------------
# Review logic — single chunk or multi-chunk
# ---------------------------------------------------------------------------

def review_single(files, mode, provider, model, **kwargs):
    """Review when all files fit in one chunk."""
    context = build_code_context(files)
    system = SYSTEM_FULL if mode == 'full' else SYSTEM_PR
    print(f"→ Single-chunk review: {len(files)} files", file=sys.stderr)
    return ai_call(system, context + "\n\n---\nВыполни code review.", provider, model, **kwargs)


def review_chunked(files, provider, model, **kwargs):
    """Split large repos into chunks, review each, then merge."""
    chunks = split_into_chunks(files)
    total = len(chunks)
    print(f"→ Multi-chunk review: {len(files)} files split into {total} chunks", file=sys.stderr)

    partial_reviews = []
    for i, chunk_files in enumerate(chunks, 1):
        file_list = ", ".join(chunk_files.keys())
        print(f"  Chunk {i}/{total}: {len(chunk_files)} files ({file_list[:120]}...)", file=sys.stderr)

        system = SYSTEM_CHUNK.format(chunk_num=i, total_chunks=total)
        context = build_code_context(chunk_files)
        user_text = context + "\n\n---\nВыполни анализ этой части кодовой базы."

        result = ai_call(system, user_text, provider, model, **kwargs)
        partial_reviews.append(result)
        print(f"  ✓ Chunk {i}/{total} done ({len(result)} chars)", file=sys.stderr)

    # Merge all partial reviews
    print(f"→ Merging {total} partial reviews...", file=sys.stderr)
    merge_system = SYSTEM_MERGE.format(total_chunks=total)
    merge_input = "\n\n---\n\n".join(
        f"## Partial Review {i+1}/{total}\n\n{review}"
        for i, review in enumerate(partial_reviews)
    )
    merge_input += "\n\n---\nОбъедини все частичные ревью в один финальный отчёт."

    final = ai_call(merge_system, merge_input, provider, model, **kwargs)
    return final

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="AI Code Review — Gemini / Claude")
    ap.add_argument("--mode", choices=["full", "pr"], default="full")
    ap.add_argument("--provider", choices=["gemini", "claude"], default="gemini")
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--diff-file", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--output", default=None)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--gcp-project", default=None)
    ap.add_argument("--gcp-region", default=None)
    args = ap.parse_args()

    model = args.model
    if not model:
        model = "gemini-2.5-flash" if args.provider == "gemini" else "claude-opus-4-6"

    extra = dict(api_key=args.api_key, gcp_project=args.gcp_project, gcp_region=args.gcp_region)

    print(f"→ Provider: {args.provider}, Model: {model}", file=sys.stderr)

    # PR mode
    if args.mode == "pr" and args.diff_file:
        with open(args.diff_file, "r") as f:
            context = f.read()
        if not context.strip():
            report = "✅ Нет изменений для ревью."
        else:
            report = ai_call(SYSTEM_PR, context + "\n\n---\nВыполни review PR.", args.provider, model, **extra)
        if args.output:
            Path(args.output).write_text(report, encoding="utf-8")
        else:
            print(report)
        return

    # Full review mode
    files = collect_files(args.project_dir)
    if not files:
        sys.exit("No source files found.")

    total_size = sum(len(c.encode('utf-8')) for c in files.values())
    print(f"Collected {len(files)} files ({total_size:,} bytes) for review", file=sys.stderr)

    if total_size <= CHUNK_SIZE:
        report = review_single(files, 'full', args.provider, model, **extra)
    else:
        report = review_chunked(files, args.provider, model, **extra)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
