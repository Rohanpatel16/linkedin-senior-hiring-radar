import os
import csv
import json
import time
import urllib.parse
import re
import requests
from datetime import datetime
from apify_client import ApifyClient

# 1. Environment Secrets & Token Parsing
APIFY_TOKENS_RAW = os.getenv("APIFY_TOKENS", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Parse multiline or comma-separated tokens
tokens = [t.strip() for t in APIFY_TOKENS_RAW.replace(",", "\n").split("\n") if t.strip()]

if not tokens:
    print("❌ No Apify tokens provided! Exiting.")
    exit(1)

print(f"🔑 Loaded {len(tokens)} Apify API Token(s).")

# 2. Build the 12 Exact Search Combinations
EXPERIENCE_LEVELS = ["associate", "mid-senior", "director", "executive"]
WORKPLACE_TYPES = ["remote", "office", "hybrid"]

SEARCH_MATRIX = []
for wp in WORKPLACE_TYPES:
    for exp in EXPERIENCE_LEVELS:
        SEARCH_MATRIX.append({
            "employmentType": ["full-time"],
            "workplaceType": [wp],
            "experienceLevel": [exp],
            "locations": ["India"],
            "maxItems": 800,  # Max items per search
            "postedLimit": "24h",
            "sortBy": "relevance",
            "easyApply": False,
            "under10Applicants": False
        })

TOTAL_SEARCHES = len(SEARCH_MATRIX)  # Exactly 12
print(f"📋 Generated {TOTAL_SEARCHES} distinct search combinations.")

# 3. Helper Functions for Data Cleaning & Classification
AGENCY_KEYWORDS = [
    "staffing", "recruitment", "consulting", "hr solutions", "manpower", "talent acquisition", 
    "managed team", "offshore team", "rpo", "executive search", "workforce solutions", "headhunting"
]

def classify_client_type(company_data, job_desc):
    specialities = " ".join(company_data.get("specialities") or []).lower()
    industries = " ".join([i.get("name", "") if isinstance(i, dict) else str(i) for i in (company_data.get("industries") or [])]).lower()
    desc = (company_data.get("description") or "").lower()
    
    combined = f"{specialities} {industries} {desc}"
    if any(k in combined for k in AGENCY_KEYWORDS):
        return "Staffing Agency / Marketplace"
    return "Direct Employer"

def extract_industry(company_data, job_data):
    industries = []
    # From company industries
    for ind in (company_data.get("industries") or []):
        name = ind.get("name") if isinstance(ind, dict) else str(ind)
        if name and name not in industries:
            industries.append(name)
    # From root job industries
    for ind in (job_data.get("industries") or []):
        if ind and ind not in industries:
            industries.append(ind)
    return "; ".join(industries) if industries else "Technology / General"

def extract_employee_size(company_data):
    count = company_data.get("employeeCount")
    range_info = company_data.get("employeeCountRange") or {}
    start = range_info.get("start")
    end = range_info.get("end")
    
    if count and start and end:
        return f"{count:,} ({start}-{end} range)"
    elif count:
        return f"{count:,} employees"
    elif start and end:
        return f"{start}-{end} range"
    return "Not Disclosed"

def extract_location(job_data):
    loc = job_data.get("location") or {}
    parsed = loc.get("parsed") or {}
    city = parsed.get("city") or parsed.get("state")
    country = parsed.get("country") or "India"
    if city:
        return f"{city}, {country}"
    return loc.get("linkedinText") or "India"

# 4. Execute 12 Searches Across Provided Tokens
all_raw_jobs = []

for idx, search_input in enumerate(SEARCH_MATRIX):
    # Round-robin or split searches across available tokens
    token_index = idx % len(tokens)
    current_token = tokens[token_index]
    client = ApifyClient(current_token)

    wp_label = search_input["workplaceType"][0].upper()
    exp_label = search_input["experienceLevel"][0].upper()
    print(f"\n🚀 Running Search #{idx+1}/{TOTAL_SEARCHES} [{wp_label} | {exp_label}] on Token #{token_index+1}...")

    try:
        run = client.actor("zn01OAlzP853oqn4Z").call(run_input=search_input, timeout_secs=300)
        dataset_id = run.get("defaultDatasetId")
        
        items = list(client.dataset(dataset_id).iterate_items())
        print(f"✅ Search #{idx+1} completed: Fetched {len(items)} jobs.")
        all_raw_jobs.extend(items)
    except Exception as e:
        print(f"⚠️ Error on Search #{idx+1}: {e}")

print(f"\n📦 Total raw jobs fetched across all 12 runs: {len(all_raw_jobs)}")

# 5. Deduplicate and Enrich into Structured Records
seen_job_ids = set()
enriched_leads = []

for item in all_raw_jobs:
    job_id = item.get("id")
    if not job_id or job_id in seen_job_ids:
        continue
    seen_job_ids.add(job_id)

    company = item.get("company") or {}
    company_name = company.get("name") or "Unknown Company"
    title = item.get("title") or "Unknown Role"
    
    # Extract Hiring Team (Direct Poster DM Link)
    hiring_team = item.get("hiringTeam") or []
    poster_name = hiring_team[0].get("name", "Not Disclosed") if hiring_team else "Not Disclosed"
    poster_url = hiring_team[0].get("linkedinUrl", "") if hiring_team else ""

    # Generate Executive Search Dork
    dork_headhunting = f'https://www.google.com/search?q=site:linkedin.com/in+"{urllib.parse.quote(company_name)}"+("Founder"+OR+"CEO"+OR+"CTO"+OR+"Chief+People+Officer"+OR+"Head+of+Talent")'

    # Format Date
    posted_date = item.get("postedDate") or ""
    if posted_date:
        try:
            dt = datetime.fromisoformat(posted_date.replace("Z", "+00:00"))
            posted_date_str = dt.strftime("%d-%b-%Y %H:%M UTC")
        except:
            posted_date_str = posted_date[:16]
    else:
        posted_date_str = "Recent"

    record = {
        "Company Name": company_name,
        "Industry / Sector": extract_industry(company, item),
        "Company Employee Size": extract_employee_size(company),
        "Job Title": title,
        "Seniority Level": item.get("experienceLevel") or item.get("query", {}).get("experienceLevel", ["General"])[0].title(),
        "Workplace Type": (item.get("workplaceType") or "Office").title(),
        "City / Location": extract_location(item),
        "Client Classification": classify_client_type(company, item.get("descriptionText", "")),
        "Applicant Count": item.get("applicants", 0),
        "Job Poster Name": poster_name,
        "Job Poster LinkedIn Profile": poster_url,
        "Company Website": company.get("website") or "",
        "Company LinkedIn URL": company.get("linkedinUrl") or "",
        "Decision-Maker Headhunting URL": dork_headhunting,
        "Job Posting Link": item.get("linkedinUrl") or f"https://www.linkedin.com/jobs/view/{job_id}/",
        "Posted Date": posted_date_str
    }
    enriched_leads.append(record)

print(f"🎯 Total Unique Verified Leads: {len(enriched_leads)}")

# 6. Write CSV File
today_str = datetime.now().strftime("%d-%b-%Y").upper()
csv_filename = f"LinkedIn_India_Hiring_Leads_{today_str}.csv"

fieldnames = [
    "Company Name", "Industry / Sector", "Company Employee Size", "Job Title", 
    "Seniority Level", "Workplace Type", "City / Location", "Client Classification", 
    "Applicant Count", "Job Poster Name", "Job Poster LinkedIn Profile", 
    "Company Website", "Company LinkedIn URL", "Decision-Maker Headhunting URL", 
    "Job Posting Link", "Posted Date"
]

with open(csv_filename, mode="w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(enriched_leads)

print(f"💾 Successfully saved CSV: {csv_filename}")

# 7. Upload CSV Directly to Discord via Webhook
if not DISCORD_WEBHOOK_URL:
    print("⚠️ No DISCORD_WEBHOOK_URL configured. Skipping upload.")
    exit(0)

# Calculate summary stats for the Discord message
total_direct = sum(1 for x in enriched_leads if x["Client Classification"] == "Direct Employer")
total_agency = len(enriched_leads) - total_direct
total_with_poster = sum(1 for x in enriched_leads if x["Job Poster LinkedIn Profile"])

summary_text = (
    f"📊 **LINKEDIN INDIA HIRING RADAR — {today_str}**\n\n"
    f"• 🎯 **Total Unique Jobs Scraped:** {len(enriched_leads):,}\n"
    f"• 🏢 **Direct Employers:** {total_direct:,} | 🏛️ **Agency/Marketplace Mandates:** {total_agency:,}\n"
    f"• 👤 **Direct Poster DMs Available:** {total_with_poster:,} contacts\n"
    f"• ⚡ **Search Matrix:** 12 Combinations across Remote, Hybrid & On-site\n\n"
    f"📎 *The complete spreadsheet with Industry, Employee Size, and Headhunting links is attached below:*"
)

payload = {
    "content": summary_text,
    "username": "LinkedIn Lead Generator",
    "avatar_url": "https://cdn-icons-png.flaticon.com/512/3536/3536505.png"
}

try:
    with open(csv_filename, "rb") as f:
        files = {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "file": (csv_filename, f, "text/csv")
        }
        res = requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=60)
        
    if res.status_code in [200, 204]:
        print("🚀 CSV file and summary successfully uploaded to Discord!")
    else:
        print(f"Discord upload failed: {res.status_code}, {res.text}")
except Exception as e:
    print(f"Error posting file to Discord: {e}")
