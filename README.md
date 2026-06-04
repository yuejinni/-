# Qihang JDY Local Workbench

This is the source-code-only repository for the Qihang local JDY/Kingdee workbench.

## What is included

- Flask backend under `customs_server/`
- JDY API wrapper and local-cache logic
- Browser UI templates under `customs_server/templates/`
- Chrome extension under `customs_extension/`
- Development notes under `docs/`

## What is intentionally excluded

Do not commit local runtime data or secrets:

- `ai_config.json`
- `server_secret.key`
- `auth_users.sqlite3`
- `_sales_cache/`, `_cache/`, `_api_cache/`, `_items_store/`
- `logs/`, `_updates/`
- build/dist output
- Excel, PDF, Word business documents

Use `ai_config.example.json` as the template for local configuration.

## Local development

```powershell
cd customs_server
python -m pip install -r requirements.txt
python server.py
```

Open:

```text
http://127.0.0.1:5008/jdy
```

## Collaboration workflow

1. Create a branch for each change.
2. Commit only code/config templates/docs.
3. Open a pull request for review.
4. Never upload runtime database files or real JDY/API credentials.
