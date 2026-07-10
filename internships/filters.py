"""Shared relevance filters, applied the same way regardless of source."""
import re

INTERN_RE = re.compile(r"\b(intern(ship)?|co[\- ]?op)\b", re.I)
SWE_RE = re.compile(
    r"\b(software|swe|develop(er|ment)|programmer|full[\- ]?stack|back[\- ]?end|"
    r"front[\- ]?end|infrastructure|platform|systems?|embedded|"
    r"machine learning|\bml\b|\bai\b|data engineer|security engineer)\b", re.I)
YEAR_RE = re.compile(r"\b20\d{2}\b")


def is_swe_internship(title: str) -> bool:
    return bool(title) and bool(INTERN_RE.search(title)) and bool(SWE_RE.search(title))


# Priority 1 = big tech / the absolute top tier (think Google, Meta, Apple,
# Nvidia, OpenAI, Anthropic, and the most elite/exclusive firms in other
# fields like top quant trading shops). Priority 2 = mid tech -- solid,
# well-known, legitimate companies that aren't quite top-tier-elite.
# Everything else (not in either set) defaults to priority 3 -- that's the
# majority bucket, so it needs no explicit list. Hand-curated against the
# real company names in companies.csv (case-insensitive, trimmed match --
# same naming-variant caveat as the old is_important_company: "Amazon Web
# Services" vs "AWS" etc. won't match unless spelled the same way).
PRIORITY_1_COMPANIES = {
    "google", "openai", "anthropic", "apple", "microsoft", "meta", "nvidia", "amazon",
    "google deepmind", "netflix", "tesla", "amazon web services", "xai", "spacex",
    "jane street", "citadel", "citadel securities", "two sigma", "hudson river trading",
    "stripe", "databricks", "palantir",
    "jump trading", "d.e. shaw", "optiver", "susquehanna international group", "drw",
    "imc trading", "five rings", "xtx markets", "point72", "millennium",
    "tsmc", "asml", "waymo", "goldman sachs", "jpmorgan chase", "morgan stanley",
    "renaissance technologies", "bridgewater associates", "samsung",
    "bytedance", "tencent", "alibaba", "alphabet (waymo)", "deepseek",
}

PRIORITY_2_COMPANIES = {
    "scale ai", "anduril", "mistral ai", "cohere", "perplexity", "hugging face",
    "anysphere (cursor)", "cognition", "safe superintelligence (ssi)", "thinking machines lab",
    "wiz", "cerebras", "groq", "coreweave",
    "elevenlabs", "runway", "midjourney", "character ai", "boston dynamics", "rivian",
    "rocket lab", "zoox", "cruise", "lucid motors", "blue origin",
    "figma", "notion", "ramp", "bloomberg", "airtable",
    "adobe", "salesforce", "servicenow", "workday", "atlassian", "intuit", "oracle",
    "sap", "ibm", "mongodb", "github", "gitlab", "crowdstrike", "palo alto networks",
    "okta", "docker", "twilio", "zoom", "dropbox", "box", "asana", "hubspot", "zendesk",
    "splunk", "vmware", "autodesk", "cisco", "dell technologies", "hp",
    "hewlett packard enterprise", "twitch", "1password", "globalfoundries",
    "broadcom", "amd", "qualcomm", "intel", "arm", "texas instruments", "micron",
    "marvell", "synopsys", "cadence", "applied materials", "lam research",
    "analog devices", "nxp semiconductors", "infineon",
    "coinbase", "snowflake", "datadog", "cloudflare", "uber", "airbnb", "spotify",
    "shopify", "block", "paypal", "robinhood", "plaid", "visa", "mastercard",
    "capital one", "american express", "fidelity investments", "affirm", "chime",
    "sofi", "wise", "revolut", "klarna", "kraken", "circle", "ripple", "adyen",
    "virtu financial", "aqr capital",
    "discord", "reddit", "pinterest", "snap", "roblox", "unity", "epic games",
    "riot games", "doordash", "instacart", "lyft", "etsy", "ebay", "booking.com",
    "expedia", "yelp", "zillow", "canva", "wayfair", "squarespace", "wix", "godaddy",
    "valve", "activision blizzard", "electronic arts", "ubisoft", "nintendo",
    "sony interactive entertainment", "take-two interactive", "rockstar games",
    "hoyoverse", "supercell", "niantic",
    "disney", "warner bros discovery", "sony",
    "epic systems", "illumina", "veeva systems", "hims & hers", "goodrx",
    "sea limited", "nubank", "mercado libre", "coupang", "baidu", "meituan", "jd.com",
    "shein", "grab", "rakuten", "naver", "logitech", "garmin", "dyson",
    "duolingo", "coursera",
    "servicetitan", "uipath",
}


def company_priority(company: str) -> int:
    """1 = big tech / absolute top tier, 2 = solid mid tech, 3 = everything
    else (default). See PRIORITY_1_COMPANIES/PRIORITY_2_COMPANIES above for
    the classification and how it was built."""
    name = (company or "").strip().lower()
    if name in PRIORITY_1_COMPANIES:
        return 1
    if name in PRIORITY_2_COMPANIES:
        return 2
    return 3


def year_relevance(title: str, location: str = "", extra_text: str = "") -> str:
    """'yes' if title/location say 2027, or say no year at all and the
    description body mentions 2027. 'no' if title/location explicitly name
    a different year -- that's authoritative and extra_text can't override
    it. 'maybe' if title/location name no year and the body doesn't
    mention 2027 either.

    extra_text (the job description body) is untrustworthy either way: full
    of years unrelated to the posting's own year (copyright footers,
    academic-year ranges, other programs' dates). It can only settle things
    when title/location are silent -- it must never veto an explicit
    title/location year, and must never override one either."""
    core_years = {y for t in (title, location) if t for y in YEAR_RE.findall(t)}
    if "2027" in core_years:
        return "yes"
    if core_years:
        return "no"
    return "yes" if extra_text and "2027" in YEAR_RE.findall(extra_text) else "maybe"


def selftest():
    assert is_swe_internship("Software Engineer Intern, Summer 2027")
    assert is_swe_internship("Backend Engineering Co-op")
    assert not is_swe_internship("Marketing Intern")
    assert not is_swe_internship("Staff Software Engineer")  # not an internship
    assert not is_swe_internship("")

    assert year_relevance("SWE Intern Summer 2027") == "yes"
    assert year_relevance("SWE Intern", "", "mentions 2027 in body") == "yes"
    assert year_relevance("SWE Intern Summer 2026") == "no"
    assert year_relevance("SWE Intern") == "maybe"
    assert year_relevance("SWE Intern 2026 and 2027 rotation") == "yes"  # 2027 wins if present
    # a year in the description body alone must never disqualify -- only
    # title/location can produce "no" (copyright footers, academic-year
    # ranges, and other programs' dates live in the body, not the title)
    assert year_relevance("SWE Intern", "SF", "copyright 2019 Acme Corp") == "maybe"
    # ...and a stray "2027" in the noisy body must never override an
    # explicit non-2027 year in the title either -- title/location are
    # authoritative in both directions
    assert year_relevance("SWE Intern Summer 2026", "SF", "mentions 2027 somewhere") == "no"

    assert company_priority("Google") == 1
    assert company_priority("  google  ") == 1  # trimmed + case-insensitive
    assert company_priority("Jane Street") == 1
    assert company_priority("Figma") == 2
    assert company_priority("Totally Unknown LLC") == 3  # default bucket
    assert company_priority("") == 3
    print("filters selftest OK")


if __name__ == "__main__":
    selftest()
