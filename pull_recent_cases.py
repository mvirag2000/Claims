##
## Pull recent closed cases from Salesforce and write case numbers to file
##
import os
import requests
from dotenv import load_dotenv

load_dotenv()

def get_access_token():
    token_url = "https://login.salesforce.com/services/oauth2/token"
    data = {
        "grant_type": "password",
        "client_id": os.environ["SF_CLIENT_ID"],
        "client_secret": os.environ["SF_CLIENT_SECRET"],
        "username": os.environ["SF_USERNAME"],
        "password": os.environ["SF_PASSWORD"],
    }
    response = requests.post(token_url, data=data, timeout=15)
    if not response.ok:
        print(f"Auth failed ({response.status_code}): {response.text}")
        response.raise_for_status()
    token_data = response.json()
    return token_data["access_token"], token_data["instance_url"]

def pull_recent_closed(access_token, instance_url):
    query = (
        "SELECT CaseNumber, ClosedDate, Subject "
        "FROM Case "
        "WHERE IsClosed = true AND ClosedDate = LAST_N_DAYS:1 "
        "ORDER BY ClosedDate DESC"
    )
    response = requests.get(
        f"{instance_url}/services/data/v62.0/query",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": query},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data["records"], data["totalSize"]

def main():
    print("Authenticating with Salesforce (production)...")
    access_token, instance_url = get_access_token()
    print(f"Connected to {instance_url}")

    print("Querying closed cases from last day...")
    records, total = pull_recent_closed(access_token, instance_url)
    print(f"Found {total} closed cases")

    with open("case-numbers.txt", "w") as f:
        for rec in records:
            case_num = rec["CaseNumber"]
            closed = rec["ClosedDate"][:10]
            subject = rec.get("Subject") or "(no subject)"
            f.write(f"{case_num}\t{closed}\t{subject}\n")
            print(f"  {case_num}  {closed}  {subject[:60]}")

    print(f"\nWrote {total} case numbers to case-numbers.txt")

if __name__ == "__main__":
    main()
