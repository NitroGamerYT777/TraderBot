# Auto Trader project workflow

The user wants this project saved to https://github.com/NitroGamerYT777/TraderBot as work progresses.

- Use the configured `origin` repository and preserve its history. Inspect local and remote changes before committing or pushing. Do not force-push.
- After completing and verifying a meaningful change, commit the project source and push to its tracked branch. The initial tracked branch is `main`. This standing instruction authorizes saving completed project changes; it does not authorize starting live trading or publishing private runtime data.
- Never commit API keys, secrets, environment files, `.local/`, virtual environments, databases, account state, or screenshots containing real account information. API keys stay in the UI/session or OS credential store.
- Preserve Alpaca Paper/Live separation, explicit bot start/close confirmation, order idempotency, persistent fill reconciliation, quote freshness checks, and provider-clock calibration.
- Keep UI copy in Uzbek and preserve the Matrix visual direction. Animations must respect reduced-motion preferences and must not obscure prices or move trading confirmation targets.
- Run the relevant Python tests and JavaScript syntax checks for code changes. Use a separate demo data directory for UI QA; never start a real bot for a design test.
