# Contributing to icaloxide

Thank you for your interest in contributing to `icaloxide`!

## Development Prerequisites

- **Rust**: 1.82+ (`rustfmt` and `clippy` components installed)
- **Python**: 3.9+ (tested up to 3.14)
- **uv** (recommended) and **maturin** (`>=1.7`)

## Local Setup

```bash
git clone https://github.com/Pastalikek65/icaloxide.git
cd icaloxide

uv venv
uv pip install -e ".[dev]"
uv run --no-sync maturin develop --release
```

## Running Checks & Tests

Every pull request must pass all Rust and Python checks with zero warnings:

```bash
# Rust formatting, strict pedantic clippy, and unit/integration tests
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace

# Python lint and pytest suite (including differential tests)
uv run --no-sync ruff check python tests benchmarks
uv run --no-sync pytest -v
```

## Benchmarks

Always build the extension in `--release` mode before benchmarking:

```bash
uv run --no-sync maturin develop --release
uv run --no-sync python benchmarks/bench.py
```

## Commit Style

Use [Conventional Commits](https://www.conventionalcommits.org/) (e.g., `feat(rrule): ...`, `fix(tz): ...`, `docs: ...`, `perf(lexer): ...`).
