# Security

## Reporting a leaked secret

If you find a key, token, password or database dump in the repository, its history, a log or a chat, tell the maintainers at once (email or message, not a public issue or pull request) and say **where** you saw it. Never paste the secret itself.

## Rotate first

Removing a secret from the code or from git history does not make it safe: anyone who cloned the repository, and GitHub's caches, may still have it. So the order is always:

1. **Rotate** the credential at its provider (Apify, OpenAI, Apollo, Hunter, Google, PostHog, database passwords, `JWT_SECRET`, `ADMIN_PASSKEY`, the deploy SSH key) and update the real `.env` on the server. For leaked user data, force password resets.
2. Only then remove it from the code and, if needed, from history (`scripts/purge_history.sh`).
3. Add a test or scanner rule so it cannot return.

## Keeping secrets out

- Real env files (`.env`, `.env.*`) are git-ignored; only `.env.example` and `.env.migrate.example` are tracked, and they hold placeholders only.
- Database dumps (`*.dump`, `*.sql.gz`, `*.pgdump`) are git-ignored and `tests/test_repo_hygiene.py` fails if one is tracked.
- Gitleaks runs on every push and pull request (`.github/workflows/secret-scan.yml`) and as a pre-commit hook (`.pre-commit-config.yaml`).
