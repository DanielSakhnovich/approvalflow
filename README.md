# ApprovalFlow — Invoice & Expense Approval Platform

AI-assisted, microservice invoice/expense approval (course capstone). WIP.
Design: `docs/superpowers/specs/2026-07-03-approvalflow-design.md` · Decisions: `decisions.md`.

## System map

![ApprovalFlow system map — services, invoice lifecycle, and autonomy posture](docs/system-map.png)

Interactive version (light/dark themes): open [`docs/system-map.html`](docs/system-map.html) in a browser.

## Run

```
docker compose up --build
```

## Test

```
pip install -r requirements-dev.txt -e libs/afcommon
pytest
```
