# Endpoint recovery — round 3 (browser / no-API bucket)

Third sweep over the 42 companies bucketed 'needs-browser' or 'no-API' in round 1. The `custom_boards` engine gained an **HTML `row_regex` mode** (finditer over a server-rendered page — one job per job-card match, tags stripped, entities decoded), so boards with no JSON feed at all are now config rows too. **22 more recovered.**

## Recovered via custom_boards spec (21)

| Company | Mode |
|---|---|
| AMD | JSON |
| Atlassian | JSON |
| Bloomberg | HTML row_regex |
| Citadel | HTML row_regex |
| Fidelity Investments | HTML row_regex |
| Google | JSON+regex |
| Intuit | HTML row_regex |
| Lam Research | JSON |
| Lattice Semiconductor | HTML row_regex |
| Microsoft | JSON |
| Procore | HTML row_regex |
| Qorvo | HTML row_regex |
| Qualcomm | JSON |
| Renaissance Technologies | HTML row_regex |
| SAP | HTML row_regex |
| Safe Superintelligence (SSI) | HTML row_regex |
| Sakana AI | HTML row_regex |
| Seagate | HTML row_regex |
| Synopsys | HTML row_regex |
| Two Sigma | HTML row_regex |
| Wise | HTML row_regex |

## Recovered into companies.csv (1)

- **Midjourney** — moved from an empty Breezy board to Ashby (`midjourney`).

## Dropped — recoverable but mislabeled (4)

Found on an acquirer's shared board where the listings are mostly the parent's, not the named company's — same policy as Splunk/Juniper. Endpoints noted for reference:

- **Cruise** → GM Workday (`generalmotors/Careers_GM`) — 376 GM jobs, ~none Cruise-specific
- **VMware** → Broadcom Workday (`broadcom/External_Career`) — mostly Broadcom semiconductor roles
- **Redfin** → Rocket Workday (`quickenloans/rocket_careers`) — Rocket Companies board
- **Citadel Securities** → only reachable via a Wayback snapshot (stale, not a live feed)

## Still unavailable (6 + 9 pending)

Genuinely browser-gated (Cloudflare/WAF/CSRF) or dead:

| Company | Why |
|---|---|
| Grammarly | Confirmed chain: grammarly.com/careers -> 301 -> superhuman.com/company/careers (Grammarly rebranded/merged in |
| Mercado Libre | Confirmed dead end, tried harder than the prior pass. careers-meli.mercadolibre.com is a Prismic-CMS marketing |
| Meta | Confirmed prior finding and dug further, still unavailable via curl-only. (1) /graphql needs fb_dtsg CSRF toke |
| Rivos | Rivos was acquired by Meta in a ~$2B deal announced 2025-09-30 (confirmed via Reuters, SiliconAngle, Dell Tech |
| TSMC | Entire careers.tsmc.com zone sits behind Cloudflare managed bot-management with a JS challenge (cf-mitigated:  |
| Tesla | Entire tesla.com domain is blocked at the Akamai edge for curl requests from this environment — not just /care |

**Hit the session limit, not yet swept (retry later):** Opendoor, G-Research, Revolut, HashiCorp, Valve, Klarna, Loom, Nutanix, Siemens EDA — several (Revolut `__NEXT_DATA__`, Valve/G-Research WordPress HTML, Opendoor Next.js) look recoverable on a re-run.
