# Contributing

Bug reports and pull requests are welcome.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests -q
```

The tests need no market data. `python examples/make_synthetic_data.py --out demo_data`
creates fake ticks if you want to run the CLI end to end.

## Guidelines

- Read [docs/architecture.md](docs/architecture.md) first, especially the
  no-look-ahead rule.
- A change to fills, bars or the prop-firm model needs a test with a
  hand-computed expected value (see `tests/conftest.py::make_day`).
- Anything that changes cached output must bump `CACHE_VERSION` or
  `BARS_VERSION` in `backtester/data.py`.
- Keep the dependency list short. There is no pandas on purpose.
- Times shown to users are US/Eastern. Internals are int64 nanoseconds UTC.
- Do not commit market data, vendor binaries, or anyone else's source code.
- CI runs the test suite on Python 3.12-3.14. Please make sure it passes.

By contributing you agree your work is released under the [MIT License](LICENSE).
