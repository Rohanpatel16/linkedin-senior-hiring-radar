import os
import csv
import json
import time
import urllib.parse
import re
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 1. Environment Secrets & Smart Token Parsing
APIFY_TOKENS_RAW = os.getenv("APIFY_TOKENS", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

tokens = [t.strip() for t in re.split(r'[\s,]+', APIFY_TOKENS_RAW) if t.strip()]

if not tokens:
    print("❌ No Apify tokens provided! Exiting.")
    exit(1)

print(f"🔑 Loaded {len(tokens)} Apify Token(s).")

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
            "maxItems": 800,
            "postedLimit": "24h",
            "sortBy": "relevance",
            "easyApply": False,
            "under10Applicants": False
        })

TOTAL_SEARCHES = len(SEARCH_MATRIX)
print(f"📋 Firing {TOTAL_SEARCHES} searches simultaneously in parallel...")

# 3. Helper Functions
EXPLICIT_AGENCY_INDUSTRIES = [
    "staffing and recruiting", "human resources services", 
    "employment agencies", "executive search", "rpo", "placement agency"
]

EXPLICIT_AGENCY_KEYWORDS = [
    "staffing agency", "recruitment agency", "recruiting agency", 
    "executive search firm", "headhunting firm", "manpower agency", 
    "temp staffing", "contingent staffing", "rpo provider", "placement agency",
    "hiring solutions"
]

EXPLICIT_AGENCY_NAME_PATTERNS = [
    r"\bstaffing\b", r"\brecruitment\b", r"\brecruiting\b", r"\bheadhunters?\b",
    r"\bmanpower agency\b", r"\bplacement agency\b", r"\bhr solutions\b", r"\bexecutive search\b",
    r"\bjobtrade\b", r"\bnaukripay\b", r"\bzigsaw\b"
]

def clean_company_name(name):
    """Strip common legal suffixes for cleaner search queries."""
    if not name or name == "Unknown Company":
        return ""
    cleaned = re.sub(r'(?i)\b(pvt|ltd|private|limited|llp|inc|corp|corporation|gmbh|co)\b|\.', '', name)
    return cleaned.strip()

def extract_domain(website):
    """Extract clean root domain from website URL."""
    if not website:
        return ""
    try:
        url = website if website.startswith("http") else "http://" + website
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc or parsed.path
        netloc = netloc.replace("www.", "").split("/")[0]
        return netloc
    except:
        return ""

def classify_client_type(company_data, job_desc):
    comp_name = (company_data.get("name") or "").lower()
    
    industries_list = [i.get("name", "") if isinstance(i, dict) else str(i) for i in (company_data.get("industries") or [])]
    industries_str = " ".join(industries_list).lower()
    
    specialities = " ".join(company_data.get("specialities") or []).lower()
    comp_desc = (company_data.get("description") or "").lower()
    
    # 1. Check if Company Name clearly indicates a recruiting/staffing agency
    for pattern in EXPLICIT_AGENCY_NAME_PATTERNS:
        if re.search(pattern, comp_name):
            return "Staffing Agency / Marketplace"
            
    # 2. Check if Industry explicitly includes staffing / recruiting / HR services
    if any(ag_ind in industries_str for ag_ind in EXPLICIT_AGENCY_INDUSTRIES):
        return "Staffing Agency / Marketplace"
        
    # 3. Check if Specialities or Description explicitly define the business as a staffing agency
    combined_spec_desc = f"{specialities} {comp_desc}"
    if any(ag_kw in combined_spec_desc for ag_kw in EXPLICIT_AGENCY_KEYWORDS):
        return "Staffing Agency / Marketplace"
        
    return "Direct Employer"

def extract_industry(company_data, job_data):
    industries = []
    for ind in (company_data.get("industries") or []):
        name = ind.get("name") if isinstance(ind, dict) else str(ind)
        if name and name not in industries:
            industries.append(name)
    for ind in (job_data.get("industries") or []):
        if ind and ind not in industries:
            industries.append(ind)
    return "; ".join(industries) if industries else "Technology / General"

def extract_employee_info(company_data):
    count = company_data.get("employeeCount")
    range_info = company_data.get("employeeCountRange") or {}
    start = range_info.get("start")
    end = range_info.get("end")
    
    count_num = count if count else ""
    if start and end:
        range_str = f"{start}-{end}"
    elif start:
        range_str = f"{start}+"
    else:
        range_str = "Not Disclosed"
        
    return count_num, range_str

def extract_location(job_data):
    loc = job_data.get("location") or {}
    parsed = loc.get("parsed") or {}
    city = parsed.get("city") or parsed.get("state")
    country = parsed.get("country") or "India"
    if city:
        return f"{city}, {country}"
    return loc.get("linkedinText") or "India"

def normalize_workplace_type(wp):
    if not wp:
        return "On-Site"
    wp_str = str(wp).lower().replace("_", "-").replace(" ", "-")
    if "remote" in wp_str:
        return "Remote"
    elif "hybrid" in wp_str:
        return "Hybrid"
    elif "site" in wp_str or "office" in wp_str:
        return "On-Site"
    return wp.title()

# 4. Single Search Worker Function (For Parallel Threads)
ACTOR_ID = "zn01OAlzP853oqn4Z"

def execute_search_job(idx, search_input, token, token_idx):
    wp_label = search_input["workplaceType"][0].upper()
    exp_label = search_input["experienceLevel"][0].upper()
    print(f"🚀 [Thread {idx+1}] Starting: [{wp_label} | {exp_label}] on Token #{token_idx+1}")

    run_url = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs?token={token}&waitForFinish=300"
    headers = {"Content-Type": "application/json"}
    
    try:
        res = requests.post(run_url, headers=headers, json=search_input, timeout=360)
        if res.status_code in [200, 201]:
            run_data = res.json().get("data", {})
            dataset_id = run_data.get("defaultDatasetId")
            
            # Fetch dataset items
            dataset_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={token}&clean=true&format=json"
            items_res = requests.get(dataset_url, timeout=60)
            if items_res.status_code == 200:
                items = items_res.json()
                print(f"✅ [Thread {idx+1}] Finished! Retrieved {len(items)} jobs.")
                return items
            else:
                print(f"⚠️ [Thread {idx+1}] Failed to fetch dataset: {items_res.status_code}")
        else:
            print(f"⚠️ [Thread {idx+1}] Apify Error: {res.status_code}, {res.text}")
    except Exception as e:
        print(f"⚠️ [Thread {idx+1}] Network Error: {e}")
    return []

# 5. Execute All Searches Concurrently (Parallel Execution)
all_raw_jobs = []
max_parallel_workers = min(12, max(4, len(tokens) * 3))

print(f"⚡ Launching ThreadPoolExecutor with {max_parallel_workers} concurrent threads...")

with ThreadPoolExecutor(max_workers=max_parallel_workers) as executor:
    futures = []
    for idx, search_input in enumerate(SEARCH_MATRIX):
        token_index = idx % len(tokens)
        token = tokens[token_index]
        futures.append(executor.submit(execute_search_job, idx, search_input, token, token_index))

    for future in as_completed(futures):
        result = future.result()
        if result:
            all_raw_jobs.extend(result)

print(f"\n📦 Finished Parallel Runs! Total raw items: {len(all_raw_jobs)}")

# 6. Deduplicate & Group by Company into Structured Records
seen_job_ids = set()
unique_raw_jobs = []

for item in all_raw_jobs:
    job_id = item.get("id")
    if not job_id or job_id in seen_job_ids:
        continue
    seen_job_ids.add(job_id)
    unique_raw_jobs.append(item)

# Group jobs by Company Name
company_groups = {}
for item in unique_raw_jobs:
    company = item.get("company") or {}
    comp_name = company.get("name") or "Unknown Company"
    if comp_name not in company_groups:
        company_groups[comp_name] = []
    company_groups[comp_name].append(item)

company_leads = []

for comp_name, job_items in company_groups.items():
    cleaned_name = clean_company_name(comp_name)
    first_item = job_items[0]
    first_company = first_item.get("company") or {}

    website = first_company.get("website") or ""
    domain = extract_domain(website)
    industry = extract_industry(first_company, first_item)
    emp_count, emp_range = extract_employee_info(first_company)
    client_class = classify_client_type(first_company, first_item.get("descriptionText", ""))

    # Generate targeted Google Search Dorks for Decision-Makers
    if cleaned_name:
        encoded_name = urllib.parse.quote_plus(f'"{cleaned_name}"')
        c_suite_dork = f'https://www.google.com/search?q=site:linkedin.com/in+{encoded_name}+%28%22Founder%22+OR+%22CEO%22+OR+%22CTO%22+OR+%22Managing+Director%22%29'
        hr_lead_dork = f'https://www.google.com/search?q=site:linkedin.com/in+{encoded_name}+%28%22Head+of+Talent%22+OR+%22VP+HR%22+OR+%22Talent+Acquisition%22+OR+%22CHRO%22%29'
    else:
        c_suite_dork = ""
        hr_lead_dork = ""

    # Aggregate lists across all job postings for this company
    titles = []
    seniorities = set()
    workplaces = set()
    locations = set()
    job_links = []
    posters = set()
    poster_links = set()
    total_applicants = 0
    posted_dates = []

    for item in job_items:
        t = item.get("title") or "Unknown Role"
        if t and t not in titles:
            titles.append(t)

        exp = item.get("experienceLevel") or item.get("query", {}).get("experienceLevel", ["General"])[0].title()
        if exp:
            seniorities.add(exp)

        wp = normalize_workplace_type(item.get("workplaceType"))
        if wp:
            workplaces.add(wp)

        loc = extract_location(item)
        if loc:
            locations.add(loc)

        j_url = item.get("linkedinUrl") or f"https://www.linkedin.com/jobs/view/{item.get('id')}/"
        if j_url and j_url not in job_links:
            job_links.append(j_url)

        hiring_team = item.get("hiringTeam") or []
        if hiring_team:
            p_name = hiring_team[0].get("name")
            p_url = hiring_team[0].get("linkedinUrl")
            if p_name and p_name != "Not Disclosed":
                posters.add(p_name)
            if p_url:
                poster_links.add(p_url)

        try:
            total_applicants += int(item.get("applicants", 0))
        except:
            pass

        p_date = item.get("postedDate") or ""
        if p_date:
            try:
                dt = datetime.fromisoformat(p_date.replace("Z", "+00:00"))
                posted_dates.append(dt.strftime("%Y-%m-%d %H:%M UTC"))
            except:
                posted_dates.append(p_date[:16])

    record = {
        "Company Name": comp_name,
        "Open Job Count": len(job_items),
        "Client Classification": client_class,
        "Industry / Sector": industry,
        "Company Website": website,
        "Company Domain": domain,
        "Employee Count": emp_count,
        "Employee Size Range": emp_range,
        "Job Titles": " | ".join(titles),
        "Seniority Levels": "; ".join(sorted(seniorities)),
        "Workplace Types": "; ".join(sorted(workplaces)),
        "Locations": "; ".join(sorted(locations)),
        "Total Applicants": total_applicants,
        "Job Poster Name(s)": "; ".join(sorted(posters)) if posters else "Not Disclosed",
        "Job Poster LinkedIn Profile(s)": " | ".join(sorted(poster_links)),
        "Company LinkedIn URL": first_company.get("linkedinUrl") or "",
        "C-Suite Dork Search": c_suite_dork,
        "HR / TA Lead Dork Search": hr_lead_dork,
        "Job Posting Links": " | ".join(job_links),
        "Latest Posted Date": posted_dates[0] if posted_dates else "Recent"
    }
    company_leads.append(record)

print(f"🎯 Total Unique Hiring Companies Discovered: {len(company_leads)} (from {len(unique_raw_jobs)} job posts)")

# 7. Write Company-Grouped CSV File
today_str = datetime.now().strftime("%d-%b-%Y").upper()
csv_filename = f"LinkedIn_India_Hiring_Leads_{today_str}.csv"

fieldnames = [
    "Company Name", "Open Job Count", "Client Classification", "Industry / Sector", 
    "Company Website", "Company Domain", "Employee Count", "Employee Size Range", 
    "Job Titles", "Seniority Levels", "Workplace Types", "Locations", "Total Applicants", 
    "Job Poster Name(s)", "Job Poster LinkedIn Profile(s)", "Company LinkedIn URL", 
    "C-Suite Dork Search", "HR / TA Lead Dork Search", "Job Posting Links", "Latest Posted Date"
]

with open(csv_filename, mode="w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(company_leads)

print(f"💾 Saved Company Leads CSV file: {csv_filename}")

# 8. Upload CSV & Company-Centric Summary to Discord
if not DISCORD_WEBHOOK_URL:
    print("⚠️ No DISCORD_WEBHOOK_URL configured. Done.")
    exit(0)

total_companies = len(company_leads)
total_jobs = len(unique_raw_jobs)
total_direct_companies = sum(1 for c in company_leads if c["Client Classification"] == "Direct Employer")
total_agency_companies = total_companies - total_direct_companies
companies_with_posters = sum(1 for c in company_leads if c["Job Poster LinkedIn Profile(s)"])

summary_text = (
    f"📊 **LINKEDIN INDIA HIRING RADAR — {today_str}**\n\n"
    f"• 🏢 **Total Hiring Companies Discovered:** {total_companies:,} ({total_jobs:,} active job openings)\n"
    f"• 🎯 **Direct Employer Companies:** {total_direct_companies:,} | 🏛️ **Agency Mandates:** {total_agency_companies:,}\n"
    f"• 👤 **Companies with Named Recruiter DMs:** {companies_with_posters:,}\n"
    f"• ⚡ **Search Matrix:** 12 Parallel Combinations across Remote, Hybrid & On-site\n\n"
    f"📎 *The complete company lead spreadsheet with Industry, Headcount, and Headhunting links is attached below:*"
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
        print("🚀 CSV file and company summary successfully uploaded to Discord!")
    else:
        print(f"Discord upload failed: {res.status_code}, {res.text}")
except Exception as e:
        print(f"Error posting file to Discord: {e}")
