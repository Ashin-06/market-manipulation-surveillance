# Threat model

What the surveillance service protects, from whom, and what is still open. Written for the 1.0.0
code; nobody independent has reviewed it.

## What it protects

| Asset | Where it lives |
|---|---|
| Order flow submitted for surveillance (trader, account and IP per order) | request bodies; not stored, except inside the alerts it raises |
| Alerts and their evidence | `alerts` table (full alert JSON), linked to the run that raised them |
| Analyst decisions | `alert_reviews` table: who, when, what, why |
| Who submitted what | `surveillance_runs` table |
| API keys | callers hold the key; the service holds only SHA-256 hashes |

Alerts are sensitive in both directions: a leak tips off the trader under suspicion, and a lost
or altered disposition is a compliance failure.

## Trust boundaries

```
venue / OMS feed ──► mktsurv API ──► database (runs, alerts, reviews)
analyst console  ──►     ▲
                         └── TLS terminated at your ingress
```

1. **Feed → API.** A `feed` key can submit order flow and nothing else, so a compromised feed
   can't read or close alerts.
2. **Analyst → API.** `viewer` reads alerts; `analyst` also reviews them; the reviewer's name
   comes from the key, not the request.
3. **API → database.** Holds alerts and reviews in plain form; protect it like the surveillance
   records it is.

## Threats and what stops them

| Threat | Mitigation | Residual risk |
|---|---|---|
| Someone reads alerts they shouldn't (tipping off) | API keys with roles; `feed` keys can't read; no CORS unless origins are listed | Anyone with database access sees every alert |
| An insider quietly dismisses an alert | Every review is stored with the reviewer's name and note; the latest review sets the status; history is kept | Reviews are not tamper-evident: someone with database write access can edit or delete them. A hash chain or write-once storage would be needed for that. |
| Fake order flow to frame a trader or to bury real activity | Only `feed`/`analyst`/`admin` keys may submit; each run records who sent it | The service can't check that submitted events really happened; the feed must be trusted |
| Manipulator evades detection | — | **Thresholds are public and hand-set.** Anyone who reads them can stay just under them. See `docs/model-card.md`. |
| Denial of service through expensive requests | Body cap (2 MB), at most 5,000 events per request (the wash detector is quadratic in fills), per-client token bucket, cached demo endpoints | Rate limit is per process; several replicas multiply it |
| Error messages leak trader data | Validation errors carry no input; unhandled errors return only a request ID; unknown IDs aren't echoed | `400` messages from the order book name the offending order ID |
| Stolen API key | Per-client keys, revocable by removing the hash and restarting; requests are logged with request IDs | A stolen `analyst` key can review alerts until revoked |
| Misconfigured production start | `MKTSURV_ENV=production` refuses to start without hashed keys and a database; no public bind without keys | Production mode can't judge whether the keys are well protected |

## Not covered

- TLS (terminate it at the ingress).
- Record retention periods and legal hold; set them in the database per your regulator.
- Cross-venue and cross-product manipulation (one instrument at a time).
- Beneficial-ownership linkage between accounts (only the IP field links them).
