"""Shared relevance filters, applied the same way regardless of source."""
import re

INTERN_RE = re.compile(r"\b(intern(ship)?|co[\- ]?op)\b", re.I)
# Note: bare "development" / "ai" / "platform" / "systems" are intentionally
# NOT matched alone -- they false-positive Marketing/AI Product/Electrical
# Platform/Systems Admin roles. Prefer software/SWE/SDE/developer + ML/data/
# security engineer / firmware / embedded / full-stack, etc.
SWE_RE = re.compile(
    r"\b("
    r"software|swe|\bsde\b|developer|software\s+development|development\s+engineer|"
    r"programmer|full[\- ]?stack|back[\- ]?end|front[\- ]?end|"
    r"infrastructure|firmware|embedded|"
    r"machine[\- ]?learning|\bml\b|data\s+engineer|security\s+engineer|"
    r"site\s+reliability|\bsre\b|"
    r"(?:software|cloud|data|ml|ai)\s+platform|"
    r"(?:software|computer|distributed)\s+systems?"
    r")\b",
    re.I,
)
YEAR_RE = re.compile(r"\b20\d{2}\b")
# Street-address years ("2026 Market Street", "2019 Mission St") must not count
# as posting-year signals -- otherwise a no-year title at that address becomes
# year_relevance "no" and the listing is dropped.
_ADDRESS_YEAR_RE = re.compile(
    r"\b20\d{2}\s+(?:[A-Za-z0-9.#'\-]+\s+){0,4}"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Ln|Lane|Dr|Drive|"
    r"Way|Ct|Court|Pl|Place|Plaza|Pkwy|Parkway|Hwy|Highway)\b",
    re.I,
)


def is_swe_internship(title: str) -> bool:
    return bool(title) and bool(INTERN_RE.search(title)) and bool(SWE_RE.search(title))


def _years_in(text: str) -> list:
    """4-digit 20xx years in text, ignoring street-address numbers."""
    if not text:
        return []
    cleaned = _ADDRESS_YEAR_RE.sub(" ", text)
    return YEAR_RE.findall(cleaned)


# Priority 1 = big tech / the absolute top tier (think Google, Meta, Apple,
# Nvidia, OpenAI, Anthropic, and the most elite/exclusive firms in other
# fields like top quant trading shops). Priority 2 = mid tech -- solid,
# well-known, legitimate companies that aren't quite top-tier-elite.
# Everything else (not in either set) defaults to priority 3 -- that's the
# majority bucket, so it needs no explicit list. Hand-curated against the
# real company names in companies.csv (case-insensitive, trimmed match --
# same naming-variant caveat as the old is_important_company: "Amazon Web
# Services" vs "AWS" etc. won't match unless spelled the same way).
# Include common source-name variants (spacing/abbreviation) so scrape labels
# like "D. E. Shaw" / "Susquehanna" / "TikTok" still land in the right tier.
PRIORITY_1_COMPANIES = {
    "google", "openai", "anthropic", "apple", "microsoft", "meta", "nvidia", "amazon",
    "google deepmind", "netflix", "tesla", "amazon web services", "xai", "spacex",
    "jane street", "citadel", "citadel securities", "two sigma", "hudson river trading",
    "stripe", "databricks", "palantir",
    "jump trading", "d.e. shaw", "d. e. shaw", "d. e. shaw & co.", "de shaw",
    "optiver", "susquehanna international group", "susquehanna",
    "susquehanna investment group", "drw",
    "imc trading", "imc", "five rings", "xtx markets", "point72", "millennium",
    "tsmc", "tsmc arizona", "asml", "waymo", "goldman sachs", "jpmorgan chase",
    "morgan stanley",
    "renaissance technologies", "bridgewater associates", "samsung",
    "bytedance", "tiktok", "tencent", "alibaba", "alphabet (waymo)", "deepseek",
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
    title/location year, and must never override one either.

    Street-address numbers that look like years (e.g. "2026 Market Street")
    in title/location are ignored -- they are not posting-year signals."""
    core_years = {y for t in (title, location) if t for y in _years_in(t)}
    if "2027" in core_years:
        return "yes"
    if core_years:
        return "no"
    return "yes" if extra_text and "2027" in _years_in(extra_text) else "maybe"


def selftest():
    assert is_swe_internship("Software Engineer Intern, Summer 2027")
    assert is_swe_internship("Backend Engineering Co-op")
    assert is_swe_internship("Software Development Intern")
    assert is_swe_internship("Developer Intern")
    assert is_swe_internship("Network Development Engineer Intern")
    assert not is_swe_internship("Marketing Intern")
    assert not is_swe_internship("Staff Software Engineer")  # not an internship
    assert not is_swe_internship("")
    # bare "development" is NOT SWE -- Business/Talent/Learning & Development
    assert not is_swe_internship("Business Development Intern")
    assert not is_swe_internship("Business Development Representative Intern")
    assert not is_swe_internship("Sales Development Intern")
    assert not is_swe_internship("Learning & Development (Instructional Design) Intern")
    assert not is_swe_internship("Human Resources Intern, Talent Development")
    assert not is_swe_internship("Strategy and Business Development Intern")
    # bare ai / platform / systems — not SWE
    assert not is_swe_internship("B2B Marketing Content & AI Intern")
    assert not is_swe_internship("AI Product Manager Intern")
    assert not is_swe_internship("Electrical Platform Intern")
    assert not is_swe_internship("Systems Administrator Intern")
    assert not is_swe_internship("Platform Support Intern")
    # real SWE short forms / firmware
    assert is_swe_internship("SDE Intern")
    assert is_swe_internship("Co-op Firmware Engineer")
    assert is_swe_internship("Firmware Intern")
    assert is_swe_internship("Machine Learning Intern")
    assert is_swe_internship("Software Platform Intern")

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
    # street-address years are not posting years (would false-"no" otherwise)
    assert year_relevance("SWE Intern", "2026 Market Street, SF") == "maybe"
    assert year_relevance("SWE Intern", "2019 Mission St, San Francisco") == "maybe"
    assert year_relevance("SWE Intern Summer 2027", "2026 Market Street") == "yes"
    # real program year in location still counts
    assert year_relevance("SWE Intern", "Summer 2026") == "no"
    assert year_relevance("SWE Intern", "United States - 2027") == "yes"

    assert company_priority("Google") == 1
    assert company_priority("  google  ") == 1  # trimmed + case-insensitive
    assert company_priority("Jane Street") == 1
    assert company_priority("D.E. Shaw") == 1
    assert company_priority("D. E. Shaw") == 1  # spacing variant from sources
    assert company_priority("D. E. Shaw & Co.") == 1
    assert company_priority("Susquehanna") == 1
    assert company_priority("Susquehanna International Group") == 1
    assert company_priority("IMC") == 1
    assert company_priority("IMC Trading") == 1
    assert company_priority("TikTok") == 1
    assert company_priority("TSMC Arizona") == 1
    assert company_priority("Figma") == 2
    assert company_priority("Totally Unknown LLC") == 3  # default bucket
    assert company_priority("") == 3
    print("filters selftest OK")


if __name__ == "__main__":
    selftest()
