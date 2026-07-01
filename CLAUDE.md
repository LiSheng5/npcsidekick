# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Start

At the start of every session, immediately scan all previous session files in the `.claude/sessions` directory. Identify the most recent incomplete goal by analyzing the session summaries and the user's request patterns. Present a concise recovery summary: what was last worked on, what steps were completed, what remained blocked or unfinished, and suggest three specific next actions. After presenting, ask the user if they'd like to continue where they left off or start fresh. If they choose to continue, automatically load any relevant context (branch, file state, dependencies) and proceed.

When checking project status, spawn an Agent to run git log, read state files, and scan directories in parallel.

## Core Behavior

When the user asks you to perform an action (find a file, create a skill, delete a directory, locate a session), directly execute the appropriate tool (Bash, Edit, Write) to complete it. Do not describe the steps they should take to do it themselves.

## Communication

After creating a file, modifying a file, or completing a multi-step task, explicitly state what you just accomplished (e.g., 'I have created the file `.claude/skills/search/SKILL.md` and committed it'). Do not leave the user guessing whether the task was done hypothetically or actually completed.

## TDD Multi-Agent Workflow

When implementing a feature using test-driven development, lead a team of three AI agents in a self-correcting loop:

- **AGENT 1 — IMPLEMENTER**: Write the minimal code needed to pass the failing test.
- **AGENT 2 — TESTER**: Run the full test suite using Bash (e.g., `pytest tests/ -v`). Parse the output to identify all failures. Categorize each failure as: compilation error, assertion failure, timeout, or missing import. Write structured results to `test_results.json`.
- **AGENT 3 — REVIEWER**: Read the implementer's code and test results. Check for code quality, edge cases, and adherence to project style. Suggest specific refactors.

**Workflow**: 1) IMPLEMENTER writes code → 2) TESTER runs tests and logs failures → 3) REVIEWER evaluates and suggests improvements → 4) Repeat until all tests pass. Use TodoWrite to track each cycle. Stop after 10 cycles or all tests green, whichever comes first.

## Project Scaffolding Workflow

When asked to set up a new project from scratch, act as an expert project scaffolder. Given a project description:

1. Ask clarifying questions if needed (max 2 questions) about language, framework, or license.
2. Initialize a git repository with an appropriate `.gitignore` for the language/framework.
3. Create a standard folder structure (`src/`, `tests/`, `docs/`, `config/`, etc.).
4. Set up a package manager (pip, npm, cargo, etc.) and install sensible default dependencies (testing, linting, formatting).
5. Write a basic `README.md` with project name, setup instructions, and usage.
6. Create a `CLAUDE.md` file that records the project structure, key decisions, and conventions for future sessions.
7. Make an initial git commit with message `Initial scaffold: [brief description]`.
8. Present a summary of everything created and three suggested next development steps.

Execute all steps autonomously, only pausing if a command fails critically. Use Bash for all shell commands, Write for file creation, and Edit for any modifications.

## Project Overview

Dagent is a 4-layer AI agent framework: **Planner → Executor → Tool Router → Memory**, with a pluggable **Provider** layer (5th layer) for LLM access (OpenAI/DeepSeek/compatible APIs). It's a Python-native architecture inspired by Reasonix (Go) patterns but ported idiomatically — port concepts, not code.

Phase 1–3 are complete: 148 tests, structured logging, injectable settings, streaming + Rich CLI, Skill concept, and multi-provider abstraction.

Current version: **v3.0** (per `pyproject.toml` and orchestrator docstring).

## Commands

```bash
# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run agent (interactive mode, standard)
python main.py

# Run agent (interactive mode, streaming Rich Live display)
python main.py --stream

# Run agent (single query)
python main.py "现在几点？"

# Run agent (streaming single query)
python main.py --stream "现在几点？"

# Run with specific model
python main.py --model gpt-4

# Run all tests
pytest tests/ -v

# Run a subset of tests
pytest tests/test_executor.py -v
pytest tests/ -k "test_run_single" -v

# Run with coverage
pytest tests/ -v --cov=agent --cov-report=term-missing
```

## Architecture

### Layer Flow

```
User Input → Planner.plan() → TaskPlan
           → [Loop]
             → Executor.execute_step() → (step, result)
             → Reflector.reflect() → CONTINUE | RETRY | REPLAN | STOP | ASK_USER
             → Memory.log_execution()
           → _synthesize() → answer

LLM calls in every layer delegate to LLMClient → ProviderProtocol (OpenAI/DeepSeek/any compatible API).
```

### Streaming Flow

```
Orchestrator.run_stream() → AsyncGenerator[StreamEvent]
  ├── Phase 1: StreamEvent.thinking() → StreamEvent.plan_ready()
  ├── Phase 2: StreamEvent.step_start() → StreamEvent.tool_result() → StreamEvent.step_done()
  │            → StreamEvent.reflection() (per step)
  └── Phase 3: StreamEvent(type=SYNTHESIS) → StreamEvent.done()

AgentDisplay (Rich Live) consumes events → 3-panel Layout (header/body/footer).
```

### Key Files

| File | Role |
|------|------|
| `agent/orchestrator.py` | Main loop: wires all 4 layers, runs Plan→Execute→Reflect cycle, `run()` + `run_chat()` + `run_stream()` (async generator) |
| `agent/planner/planner.py` | LLM-driven task decomposition: `plan()` returns `TaskPlan`, `replan()` for failed steps |
| `agent/planner/reflector.py` | 3-tier reflection: fast rules → LLM deep analysis → deterministic fallback |
| `agent/planner/task_plan.py` | Data model: `TaskPlan`, `Step`, `StepStatus` — the contract between Planner and Executor |
| `agent/executor/executor.py` | Step execution: LLM tool selection or direct dispatch, retry loop |
| `agent/executor/retry.py` | Retry policies: `SimpleRetry`, `ExponentialBackoff`, `NoRetry` |
| `agent/tools/router.py` | `ToolRouter`: validate → execute → record flow |
| `agent/tools/registry.py` | `ToolRegistry`: singleton, register/get/search/export tools |
| `agent/tools/schema.py` | Core contracts: `ToolProtocol`, `ToolCall`, `ToolResult`, `ToolSchema` |
| `agent/tools/skill.py` | `Skill` base class (decompose→dispatch→synthesize) + `AnalyzeCodeSkill` |
| `agent/tools/adapters/openai_adapter.py` | `OpenAIAdapter`: converts ToolRegistry schemas ↔ OpenAI function format |
| `agent/executor/step_context.py` | `StepContext`: per-step isolated execution context with observation log |
| `agent/memory/memory_manager.py` | Unified memory API: composes ShortTerm + LongTerm + VectorStore + Retriever |
| `agent/memory/short_term.py` | Sliding window conversation history with JSON persistence |
| `agent/memory/long_term.py` | Persistent facts, learnings, execution logs |
| `agent/memory/retriever.py` | Semantic search (vector) → keyword fallback → formatted context |
| `agent/memory/compressor.py` | Progressive summarization: oldest STM → LLM summary → LTM |
| `agent/memory/vector_store.py` | ChromaDB wrapper: semantic similarity search. Silently degrades to no-op if `chromadb` not installed |
| `agent/memory/token_counter.py` | tiktoken-based token counter with `len(text)//3` fallback when tiktoken is absent |
| `agent/llm/client.py` | Thin wrapper over ProviderProtocol: `chat()`, `stream()`, `chat_with_structured_output()` |
| `agent/llm/types.py` | Streaming data contracts: `StreamChunk`, `StreamEvent`, `StreamEventType` |
| `agent/providers/base.py` | `ProviderProtocol` abstract class — chat() + stream() interface |
| `agent/providers/openai_provider.py` | OpenAI/DeepSeek provider — single class, different base_urls |
| `agent/providers/factory.py` | `create_provider()` — auto-detect provider by model name |
| `agent/display.py` | Rich Live display: consumes `StreamEvent` stream, renders real-time UI |
| `agent/logging_config.py` | structlog config: console (Rich) / JSON (production) / test modes |
| `agent/settings.py` | `AgentSettings` injectable dataclass — all config in one place |
| `config.py` | Thin backward-compat wrapper exposing `AgentSettings` fields as module-level constants |
| `demo_v3.py` | Primary demo script: showcases AgentOrchestrator, ToolRouter dispatch, multi-tool coordination |
| `main.py` | CLI entry point: argparse (`--stream`, `--model`, positional query), Rich Prompt REPL |

### 16 Builtin Tools (15 atomic + 1 Skill)

| Category | Tools |
|----------|-------|
| File | `read_file`, `write_file`, `list_dir` |
| Code | `run_code` (AST sandbox, 30+ blacklisted modules), `lint_code` |
| Web | `web_search`, `web_fetch` |
| System | `get_time`, `calculator` (AST-safe expression evaluator) |
| Memory | `save_note`, `list_notes`, `remember_fact`, `search_memory`, `summarize_context` |
| Skill | `analyze_code` (composite: read_file → lint_code → synthesize report) |

## Key Patterns

### Settings Injection

Never use `config.XXX` directly in new code. Use `AgentSettings` injection:

```python
from agent.settings import AgentSettings, get_settings

settings = get_settings()  # or AgentSettings(model_name="gpt-4")
```

`config.py` is a thin backward-compatibility wrapper that delegates to `AgentSettings`.

### Structured Logging

```python
from agent.logging_config import log

log.info("event_name", key1="value1", task_id="t1")
log.warning("replan_triggered", step_id=1, reason="timeout")
log.debug("step_detail", description="...")
```

Never use `print()` — use structured log calls. The lazy logger auto-configures on first use.

### Tests: In-Place Step Modification (Critical)

`Executor.execute_step()` modifies step objects **in-place** — it sets `step.status`, `step.result`, `step.error` directly. When mocking `execute_step` in orchestrator tests, use a `side_effect` function that mutates the passed step object. A `return_value` with a new Step object will NOT work because `plan.is_complete()` checks the original step objects.

```python
# Correct
def execute_step_mock(step, plan, prev_results):
    step.status = StepStatus.SUCCESS
    step.result = {"data": "ok"}
    return step, ToolResult(...)

# Wrong — plan.is_complete() will never see this
mock.return_value = (new_success_step, result)
```

### Tool Protocol

All tools implement `ToolProtocol`: `schema` (property), `validate(call) → (bool, str)`, `execute(call) → ToolResult`. The `name` property returns `self.schema.name`. Mock tools in tests must also expose `name`.

### Tests: Provider Injection for LLMClient

Never patch `openai.OpenAI` or `openai.AsyncOpenAI` in tests. Inject a `MagicMock(spec=ProviderProtocol)` into `LLMClient(provider=mock)` — this is the purpose of the Provider abstraction:

```python
from unittest.mock import MagicMock
from agent.providers.base import ProviderProtocol

provider = MagicMock(spec=ProviderProtocol)
provider.model_name = "mock-model"
provider.chat.return_value = LLMResponse(content="...", ...)
client = LLMClient(provider=provider)
```

For streaming tests, assign an async generator to `provider.stream`:

```python
async def mock_stream(**kwargs):
    yield StreamChunk(content="hello", ...)
    yield StreamChunk(finish_reason="stop", ...)
provider.stream = mock_stream
```

### Tests: conftest.py Fixtures

All tests share fixtures from `tests/conftest.py`. Key fixtures:

| Fixture | Returns |
|---------|---------|
| `mock_llm_client` | MagicMock with `.chat.return_value` = FakeLLMResponse |
| `mock_llm_response` | FakeLLMResponse(content="standard response", finish_reason="stop") |
| `mock_llm_response_with_tools` | FakeLLMResponse with `emit_task_plan` tool_call |
| `mock_reflection_response` | FakeLLMResponse with `emit_reflection` tool_call |
| `mock_memory_manager` | MagicMock with `.retrieve_for_planning()`, `.get_history_for_context()`, etc. |
| `mock_tool_registry` | MagicMock with 3 schemas (read_file, write_file, web_search) |
| `mock_tool_router` | MagicMock with `.dispatch.return_value` = success ToolResult |
| `valid_step` | Step(step_id=1, description="读取配置文件", tool="read_file") |
| `valid_plan` | TaskPlan with 2 steps |
| `success_result` | ToolResult(status=SUCCESS, data={...}) |
| `error_result` | ToolResult(status=ERROR, error="Connection timeout") |

Test file naming: `tests/test_<module>.py` matches source modules one-to-one.

### Skill Pattern (Composite Tools)

`Skill` extends `ToolProtocol` — ToolRouter calls `execute()` without knowing it's composite:

```
Skill.execute(call)
  → decompose(task, call) → List[ToolCall]    # break into sub-tasks
  → dispatcher(sub_call) for each             # delegate to ToolRouter.dispatch
  → synthesize(results, call) → ToolResult    # merge sub-results
```

**Dispatcher injection** avoids circular imports (tools don't import router):

```python
skill = AnalyzeCodeSkill()
skill.set_dispatcher(router.dispatch)  # Orchestrator._inject_skill_dispatchers() does this
```

`AnalyzeCodeSkill` is the built-in example: read file → lint → synthesize quality report.

### Provider Architecture

```
LLMClient (thin wrapper)
  → ProviderProtocol (abstract: chat + stream + model_name)
    → OpenAIProvider (single class, different base_urls)
      → OpenAI (sync) / AsyncOpenAI (async streaming)

create_provider(api_key, model_name, ...)  ← factory
  "deepseek-*" → https://api.deepseek.com
  "gpt-*"/"o1-*"/"o3-*"/"o4-*" → https://api.openai.com/v1
  unknown + custom base_url → that URL
```

ProviderProtocol is the **test seam**: inject `MagicMock(spec=ProviderProtocol)` — never patch `openai.OpenAI`.

### StreamEvent Factory Methods

Always use static factory methods, never construct `StreamEvent(type=...)` directly:

```python
StreamEvent.thinking("分析中...")
StreamEvent.plan_ready(goal="...", steps_count=3)
StreamEvent.step_start(step_id=1, description="...", tool="read_file")
StreamEvent.tool_result(tool_name="read_file", ok=True, step_id=1)
StreamEvent.step_done(step_id=1, success=True)
StreamEvent.reflection(decision="continue", reason="...")
StreamEvent.error(message="...")
StreamEvent.done(answer="...", duration_sec=1.5)
```

All factories auto-generate `timestamp`. Extra kwargs go to `metadata` dict.

### OpenAIAdapter

`OpenAIAdapter(registry)` bridges `ToolRegistry` ↔ OpenAI function-calling format:
- `to_openai_tools()` — converts all registered `ToolSchema`s to the OpenAI `tools` array
- `from_openai_response(tool_call)` — converts OpenAI tool_call → `ToolCall`

Used by Executor and Planner to present tools to the LLM.

### Reflection Decision Flow

```
Step result → Reflector.reflect()
  ├── Fast path: REJECTED → REPLAN, TIMEOUT → RETRY (no LLM cost)
  ├── LLM path: deep analysis of step result
  └── Fallback: deterministic rules if LLM fails
```

### API Key Loading Priority

1. Environment variable `OPENAI_API_KEY` or `DEEPSEEK_API_KEY`
2. `api_key.txt` file in project root (fallback)

The `AgentSettings._load_api_key()` method handles this chain. Never hardcode keys in source files.

### Dual Singleton in Config

`config.py` and `agent.settings.get_settings()` each maintain their own singleton instance. If a caller uses `config.XXX` directly and another uses `get_settings()`, they may reference different `AgentSettings` objects. Calling `config.reload_config()` resyncs both. **Always prefer `agent.settings.get_settings()` in new code.**

### Silent Dependency Degradation

- `vector_store.py`: if `chromadb` is not installed, `VectorStore` becomes a no-op — no semantic search, no error.
- `token_counter.py`: if `tiktoken` is not installed, falls back to `len(text)//3` token estimation.

Neither logs a warning. Install both packages (`chromadb`, `tiktoken`) to get full semantic search and accurate token budgeting.

### Chinese-Language Prompts

Reflection (`reflector.py`) and compression (`compressor.py`) prompts are hardcoded in Chinese. The agent works best with Chinese-language tasks. Internationalization is not yet implemented.

### Test Architecture

All 148 tests are unit tests with mocked LLM, memory, and tools. No integration or E2E tests exist. Tests use `pytest-asyncio` with `asyncio_mode = "auto"` — any `async def test_*` is auto-wrapped. Do NOT manually call `asyncio.run()` in test bodies.

### Streaming Synthesis Limitation

`run_stream()` yields streaming events for Phase 1 (planning) and Phase 2 (execution), but Phase 3 (final answer synthesis) is a non-streaming call wrapped in `asyncio.to_thread()`. True token-by-token streaming of the final answer requires an `asyncio.Queue` refactor (noted in `_synthesize_stream()` docstring).

## Project State

**Current: v3.0** — 148 tests all passing. Phase 1 (tests + structlog + settings), Phase 2 (streaming + Rich CLI + Skill), and Phase 3 (multi-provider abstraction) are complete.

Key files by phase:
- **Phase 1**: `agent/settings.py`, `agent/logging_config.py`, `config.py`, `pyproject.toml` — 79 tests
- **Phase 2**: `agent/llm/types.py`, `agent/tools/skill.py`, `agent/display.py`, `main.py` (argparse + Rich Prompt) — +48 tests
- **Phase 3**: `agent/providers/` (base + openai_provider + factory), LLMClient refactored as thin wrapper — +21 tests

### Known Issues
- `ARCHITECTURE.md`, `QUICKSTART.md`, and `PROJECT_DELIVERY.md` describe v2.0 architecture (AgentState, ToolPipeline, `ai.py` monolithic module) — they are outdated
- `memory_tools.py` bypasses `LongTermMemory` class by reading/writing JSON files directly. Notes saved via tools are invisible to semantic search (VectorStore)
- `WebSearchTool` uses DuckDuckGo Instant Answer API (limited); no real search engine integration
- Streaming synthesis is non-streaming (see Streaming Synthesis Limitation above)
- No integration/E2E tests — all 148 tests mock LLM, memory, and tools
