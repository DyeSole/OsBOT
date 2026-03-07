# OsBOT Layer Responsibilities

## 1) app/adapters
- Receive Telegram updates and normalize inbound events.
- Call service layer with unified message context.
- Send final responses back to Telegram.
- Keep SDK-specific code here only.

## 2) app/services
- Drive end-to-end response flow.
- Coordinate policy checks, memory retrieval, emotion strategy, and LLM call.
- No direct Telegram SDK access.

## 3) app/core
- Pure business rules and domain concepts.
- Persona constraints, safety boundaries, reply policy.
- Deterministic logic, easy to unit test.

## 4) app/infra
- LLM API client, storage, and scheduler abstractions.
- Isolate third-party APIs and IO from core logic.

## 5) app/config
- Parse `.env` values into typed settings.
- Validate required tokens and defaults.

## 6) prompts
- System prompts and behavior templates.
- Separate style from code.

## 7) data
- Local runtime artifacts:
  - `chat_history/`
  - `state/`
  - `memory/`

## 8) tests/scripts/docs
- `tests/`: coverage for core + service flow.
- `scripts/`: run and maintenance helpers.
- `docs/`: architecture and operational notes.
