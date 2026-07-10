# Endpoint recovery (skipped / dead job boards)

Discovery sweep over the 127 companies that had **no working API** (98 `skipped` + 29 `dead`). One agent per company inspected the live careers page/network traffic and curl-verified any JSON endpoint it found.

**Result: 30 endpoints recovered and wired into `companies.csv`.** The rest are captured below so they aren't lost.

## Recovered — no code needed (drop-in ATS)

Ashby / Greenhouse / Workday / Pinpoint boards the existing extractor already parses:

- **Applied Intuition** — `ashby`
- **Cerebras** — `ashby`
- **Chewy** — `workday`
- **Circle** — `workday`
- **Cisco** — `workday`
- **Form Energy** — `ashby`
- **Helion Energy** — `ashby`
- **Hex** — `greenhouse`
- **Materialize** — `ashby`
- **Saronic** — `ashby`
- **Skydio** — `ashby`
- **Snyk** — `workday`
- **Sony** — `workday`
- **Unit** — `ashby`
- **Unity** — `workday`
- **Wolverine Trading** — `custom`
- **WorldQuant** — `greenhouse`
- **eBay** — `workday`

## Recovered — via new Oracle Fusion + Eightfold handlers

- **Akamai** — `oracle`
- **American Express** — `oracle`
- **Ericsson** — `eightfold`
- **Fortinet** — `oracle`
- **Infineon** — `eightfold`
- **JPMorgan Chase** — `oracle`
- **Netflix** — `eightfold`
- **Nokia** — `oracle`
- **Oracle** — `oracle`
- **STMicroelectronics** — `eightfold`
- **Texas Instruments** — `oracle`
- **Uber** — `oracle`

## Deliberately dropped

- **Splunk** — endpoint is Cisco's Workday board (Cisco acquired Splunk) — would mislabel Cisco jobs
- **Juniper Networks** — endpoint is HPE's Workday board (HPE acquired Juniper) — would mislabel HPE jobs
- **Bolt** — Ashby board 'bolt' is empty + slug identity ambiguous

## Verified but bespoke — NOT implemented (22)

Each returns real jobs but needs its own decoder (custom SPA JSON, GraphQL, Algolia, React-Router/Next data blobs). Endpoints captured here for future one-off work; not worth a brittle parser each right now.

- **ARM** — GET `https://careers.arm.com/search-jobs/results?ActiveFacetID=0&CurrentPage=1&RecordsPerPage=15&Distance=50&Radius`
- **Alibaba** — POST `https://talent.alibaba.com/position/search` body=`{"channel":"en_official_site","language":"en","pageNo":1,"pageSize":50}`
- **Apple** — POST `https://jobs.apple.com/api/v1/search` body=`{"query":"","filters":{},"page":1,"locale":"en-us","sort":"newest","format":{"longDate":"MMMM D, YYYY","mediumDate":"MMM`
- **ByteDance** — POST `https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts` body=`{"keyword":"intern","limit":10,"offset":0,"job_category_id_list":[],"tag_id_list":[],"location_code_list":[],"subject_id`
- **D.E. Shaw** — GET `https://www.deshaw.com/careers`
- **DocuSign** — GET `https://careers.docusign.com/api/jobs`
- **GitHub** — GET `https://www.github.careers/api/jobs?limit=20`
- **Goldman Sachs** — POST `https://api-higher.gs.com/gateway/api/v1/graphql` body=`{"operationName":"GetCampusRoles","query":"query GetCampusRoles($searchQueryInput: RoleSearchQueryInput!) {\n    roleSea`
- **Groq** — POST `https://jobs.gem.com/api/public/graphql` body=`{"operationName":"JobBoardList","variables":{"boardId":"groq"},"query":"query JobBoardList($boardId: String!) { oatsExte`
- **Group One Trading** — GET `https://group1.applicantpro.com/core/jobs/489?getParams=%7B%22cityUrl%22%3A%22%22%2C%22countryAbbreviation%22%`
- **Luma AI** — POST `https://jobs.gem.com/api/public/graphql` body=`{"operationName":"JobBoardList","variables":{"boardId":"lumalabs-ai"},"query":"query JobBoardList($boardId: String!) { o`
- **NetApp** — GET `https://careers.netapp.com/search-jobs/results?ActiveFacetID=0&CurrentPage=1&RecordsPerPage=15&TotalPages=19&T`
- **Retool** — POST `https://jobs.gem.com/api/public/graphql` body=`{"operationName":"JobBoardList","variables":{"boardId":"retool"},"query":"query JobBoardList($boardId: String!) { oatsEx`
- **Rippling** — POST `https://6FNAX3TBEF-dsn.algolia.net/1/indexes/careers_en-US_production/query` body=`{"params":"query=&hitsPerPage=1000"}`
- **Rivian** — GET `https://rivian.jibeapply.com/api/jobs?limit=50&offset=0`
- **Sea Limited** — GET `https://career.sea.com/api/user/job/list?externalEntityId=3&limit=1000&offset=0&postType=1`
- **Shopify** — GET `https://www.shopify.com/careers.data`
- **Skyworks** — POST `https://careers.skyworksinc.com/services/recruiting/v1/jobs` body=`{"keywords":"","locale":"en_US","location":"","pageNumber":0,"sortBy":"recent"}`
- **Susquehanna International Group** — GET `https://careers.sig.com/api/jobs`
- **Teradata** — POST `https://careers.teradata.com/graphql` body=`{"operationName":"searchJobs","query":"query searchJobs($query: String, $filters: GoogleJobDiscoverySearchFiltersInput, `
- **Wayfair** — POST `https://www.wayfair.com/a/careers/careers/job_search_data` body=`{"categoryIds":[],"teamIds":[],"locationIds":[],"countryIds":[],"teamCategoryIds":[],"stateIds":[],"selectedJobTypeIds":`
- **Yelp** — GET `https://www.yelp.careers/us/en/search-results?keywords=intern&format=json`

## Needs a browser to discover (19)

Jobs load only after JS runs and no endpoint was derivable from HTML:

AMD, Atlassian, Citadel, Citadel Securities, Cruise, Fidelity Investments, HashiCorp, IBM, Lam Research, Loom, Mercado Libre, Meta, Microsoft, Nutanix, Redfin, Revolut, Siemens EDA, TSMC, Tesla


## No public API found (23)

Bloomberg, G-Research, Google, Grammarly, Intuit, Klarna, Lattice Semiconductor, Midjourney, Opendoor, Procore, Qorvo, Qualcomm, Renaissance Technologies, Rivos, SAP, Safe Superintelligence (SSI), Sakana AI, Seagate, Synopsys, Two Sigma, VMware, Valve, Wise


## Not yet processed — hit session limit (30)

Re-run the discovery sweep for these; several are obvious Greenhouse/Ashby/Workday boards from their URLs:

Activision Blizzard, Adept AI, Baidu, Cohesity, Color Health, DeepSeek, Disney, Electronic Arts, Epic Systems, Garmin, Innovaccer, King, LINE, Lacework, Meituan, Naver, Nebius, Niantic, Nintendo, Oracle Health, Quantlab, QuantumScape, ServiceTitan, Shein, Supercell, Uniswap Labs, Wayve, Whatnot, Windsurf, Yandex
