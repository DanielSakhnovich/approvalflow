# ApprovalFlow — Invoice & Expense Approval Platform

AI-assisted, microservice invoice/expense approval (course capstone). WIP.
Design: `docs/superpowers/specs/2026-07-03-approvalflow-design.md` · Decisions: `decisions.md`.

## System map

![ApprovalFlow system map — services, invoice lifecycle, and autonomy posture](docs/system-map.png)

Interactive version (light/dark themes): open [`docs/system-map.html`](docs/system-map.html) in a browser.

## Service relations

Who talks to whom: one synchronous path in, one sync service-to-service call (Intake → Audit), everything else over the event bus:

![ApprovalFlow service relations — pub/sub topics per service and the single sync call](docs/service-relations.png)

## Service internals

Each service on one card — what comes **in**, what happens **inside**, what goes **out**, with its tech stack and the state it owns:

![ApprovalFlow service internals — one IN/INSIDE/OUT card per service with tech stack](docs/service-maps.png)

Interactive version: open [`docs/service-maps.html`](docs/service-maps.html) in a browser.

## Run

```
docker compose up --build
```

### Try it

Submit an invoice:
```
curl -X POST http://localhost:8001/api/invoices \
  -H 'Content-Type: application/json' \
  -d '{"id":"INV-1001","submitter":"dana.cohen@northwind.example","department":"engineering-2026Q2","vendor":"Bistro 19","vendorKnown":true,"invoiceNumber":"NW-INV-7781","currency":"USD","category":"meals","attendees":1,"lineItems":[{"description":"Team lunch","quantity":1,"unitPrice":38.89}],"taxAmount":3.11,"total":42.0,"receiptPresent":true,"date":"2026-05-12","notes":"smoke"}'
```

Check status (replace `<trackingId>` with the ID from the response):
```
curl http://localhost:8001/api/invoices/<trackingId>
```

View dashboard:
```
curl http://localhost:8001/api/dashboard
```

## Test

```
pip install -r requirements-dev.txt -e libs/afcommon
pytest
```
