# Endpoint recovery — round 2 (bespoke + previously-unswept)

Second sweep over the 52 companies left after round 1: the 22 verified-but-bespoke endpoints plus the 30 that had hit the session limit. **35 more recovered.**

A new source, `internships/sources/custom_boards.py`, handles the bespoke ones. Its engine is generic — each company is a verified fetch *spec* (method, JSON body, headers, an optional regex to pull an embedded JSON blob out of an HTML response, a dotted path to the job array, and dotted field keys / a URL template), so adding a company is a config row, not new code. Every spec was run through the real engine and confirmed to return live postings before landing.

## Recovered via custom_boards spec (26)

| Company | Platform / shape |
|---|---|
| Alibaba | talent.alibaba search |
| Apple | custom /api/v1/search |
| Baidu | talent.baidu getPostListNew |
| ByteDance | custom search/job/posts |
| Color Health | CareerPuck |
| D.E. Shaw | Next.js __NEXT_DATA__ |
| DocuSign | Jibe api/jobs |
| Garmin | Jibe api/jobs |
| GitHub | Jibe api/jobs |
| Goldman Sachs | GraphQL (campus) |
| Groq | Gem GraphQL |
| Group One Trading | ApplicantPro |
| LINE | Gatsby page-data |
| Luma AI | Gem GraphQL |
| Naver | loadJobList.do |
| Retool | Gem GraphQL |
| Rippling | Algolia index |
| Rivian | iCIMS Jibe api/jobs |
| Sea Limited | custom /api/job/list |
| Shein | custom jobPage |
| Skyworks | SF Jobs2Web |
| Susquehanna International Group | Jibe api/jobs |
| Teradata | GR8 GraphQL |
| Wayfair | custom job_search_data |
| Yandex | jobs/api/publications |
| Yelp | Phenom format=json |

## Recovered into companies.csv — standard ATS after all (9)

_The careers page was a skin over a normal board once you followed the redirects/acquisitions._

| Company | Platform | Note |
|---|---|---|
| Activision Blizzard | Workday | now Microsoft — xboxgaming Workday tenant |
| Cohesity | workday | migrated Greenhouse→Workday |
| Nebius | greenhouse | Greenhouse (standard host) |
| Niantic | ashby | gaming sold to Scopely; niantic-spatial Ashby is the remaining co |
| Nintendo | greenhouse | Greenhouse token nintendo |
| ServiceTitan | Workday | Workday |
| Supercell | ashby | Ashby |
| Uniswap Labs | ashby | Ashby (uniswap) |
| Windsurf | ashby | acquired by Cognition — shares cognition Ashby board |

## Dropped (2)

- **Whatnot** — agent's Ashby spec returned 0 (pointed at the HTML page, not the API)
- **Oracle Health** — same Oracle Fusion board as parent Oracle — would duplicate its listings

## Still unavailable (15)

_JS-only with no fetchable endpoint, Cloudflare-gated, HTML-fragment responses, or moved to another skin. Notes captured for future._

| Company | Why |
|---|---|
| ARM | Confirmed via curl: GET https://careers.arm.com/search-jobs/results?ActiveFacetID=0&CurrentPage=1&RecordsPerPage=15&Dist |
| Adept AI | careers_url (https://www.adept.ai/about-careers) is fully gated behind an active Cloudflare JS challenge ("Just a moment |
| DeepSeek | deepseek.com's own site has no jobs; footer links to https://talent.deepseek.com (a pure React SPA, index.html is a 530- |
| Disney | Disney careers runs on TalentBrew/TMP (tbcdn.talentbrew.com), NOT Phenom — the hint's &format=json guess doesn't apply h |
| Electronic Arts | Hint was stale: https://ea.gr8people.com/jobs 301-redirects to https://jobs.ea.com/en_US/careers, which is an Avature po |
| Epic Systems | careers.epic.com/jobs/ is a Next.js App Router site (not a standard ATS board; Avature at epic.avature.net is used only  |
| Innovaccer | careers.innovaccer.com/careers/jobs is a Webflow page whose server HTML has zero job listings and no CMS collection mark |
| King | The Workday tenant at activision.wd1.myworkdayjobs.com appears decommissioned. Direct page load (https://activision.wd1. |
| Lacework | lacework.com/careers 301-redirects to https://www.fortinet.com/products/forticnapp — a Fortinet product marketing page,  |
| Meituan | hr.meituan.com/en/jobs is a thin React shell (title "Keeta Careers", no HTTP client bundled at all in its JS) whose "Glo |
| NetApp | Verified GET https://careers.netapp.com/search-jobs/results?Keywords=intern returns 200 but {"results":""} empty unless  |
| Quantlab | Quantlab's careers page (quantlab.com/careers) embeds a Jobvite widget (data-careersite="quantlab", companyEId qLZaVfwz) |
| QuantumScape | Careers site runs on SAP SuccessFactors Career Site Builder / jobs2web (career41.sapsf.com, company=QUANTUMP), not one o |
| Shopify | https://www.shopify.com/careers.data returns HTTP 200 and IS valid JSON (json.loads succeeds directly, no regex/extract  |
| Wayve | careers_url redirects (HTTP 307) to https://wayve.firststage.co/jobs, a Next.js (App Router/turbopack) site on the "Firs |
