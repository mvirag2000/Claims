##
## Retrieve Case objects from Salesforce
##
import os
import requests
from dotenv import load_dotenv

load_dotenv()

def get_access_token(use_test=True):
    if use_test:
        client_id = os.environ["SF_TEST_CLIENT_ID"]
        client_secret = os.environ["SF_TEST_CLIENT_SECRET"]
    else:
        client_id = os.environ["SF_CLIENT_ID"]
        client_secret = os.environ["SF_CLIENT_SECRET"]

    response = requests.post(
        os.environ["SF_TOKEN_ENDPOINT"],
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": os.environ["SF_USERNAME"],
            "password": os.environ["SF_PASSWORD"],
        },
        timeout=15,
    )
    response.raise_for_status()
    token_data = response.json()
    return token_data["access_token"], token_data["instance_url"]

def get_case(access_token, instance_url, case_number):
    query = f"SELECT Id, CaseNumber, Subject, Status, Description FROM Case WHERE CaseNumber = '{case_number}'"
    response = requests.get(
        f"{instance_url}/services/data/v62.0/query",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": query},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    if data["totalSize"] == 0:
        return None
    return data["records"][0]

def main():
    print("Authenticating with Salesforce...")
    access_token, instance_url = get_access_token(use_test=False)
    print(f"Connected to {instance_url}\n")

    case_number = input("Enter case number: ")
    while case_number != "":
        case = get_case(access_token, instance_url, case_number)
        if case:
            print(f"\n  Case:    {case['CaseNumber']}")
            print(f"  Subject: {case['Subject']}")
            print(f"  Status:  {case['Status']}")
            print(f"  Description: {case['Description']}\n")
        else:
            print(f"Case {case_number} not found.\n")
        case_number = input("Enter case number: ")

if __name__ == "__main__":
    main()
