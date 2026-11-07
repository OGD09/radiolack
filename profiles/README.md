# profiles/

Personal band profiles (TOML). Only this README and `example.toml` are
versioned; other files here stay out of version control.

- Built-in profiles ship with the package: `radiolack profiles`.
- Start from `example.toml`, which documents every key, and use the copy with
  `--profile profiles/NAME.toml`.
- `radiolack plan --profile profiles/NAME.toml` shows the window plan and
  checks that every grid channel is covered.
- Regional band plans must come from official sources (for Canada: ISED,
  NAV CANADA, RAC) before a profile relies on them.
