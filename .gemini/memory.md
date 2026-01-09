# Project Memory & Context Log

**Last Updated**: 2026-01-03
**Status**: Active Development

## 1. Current Focus
We are currently focusing on **solidifying the Core Engine and Grid Strategy**. The primary goal is to ensure the bot is robust, type-safe, and follows the "Engine-Context-Strategy" separation pattern.

## 2. Recent Key Decisions
- **Unified Context**: We enforce `StrategyContext` as the *only* way for strategies to interact with the exchange. No direct API calls.
- **Project Structure**: Adopted `uv` for package management and strictly separated `src/engine` from `src/strategy`.
- **AI Rules**: Created `.cursorrules` to persist these constraints.
- **Documentation Policy**: Decided to disable generation of code descriptions/summaries to keep output concise.

## 3. Known Technical Debt / TODOs

## 4. Work in Progress
None.
