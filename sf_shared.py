##
## Shared Salesforce auth, case retrieval, and contract summary utilities
##
import os
import requests
from dotenv import load_dotenv
import anthropic
from Contract import getContractPDF

load_dotenv()

client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var
MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Field lists and queries
# ---------------------------------------------------------------------------

CASE_FIELDS = [
    # Claim overview
    "CaseNumber", "Status", "Type", "Origin",
    "Claim_Determination__c", "Resolution__c", "Who_Filed_Claim__c",
    "Denial_Reason__c", "Denial_Reason_Text__c", "Claim_Number__c",
    "Confirmation_Num__c", "Original_Owner_Toggle__c",
    # Unstructured / narrative
    "Subject", "Description", "Case_Summary__c",
    "Additional_Information__c", "Attempt_to_Rectify__c",
    # Vehicle
    "Vehicle_Year__c", "Vehicle_Make__c", "Vehicle_Model__c",
    # Product & damage
    "Product__c", "Product_Group__c", "Product_Type__c",
    "Damage_Type__c", "Location__c", "Side_of_Vehicle__c",
    # Warranty
    "Warranty_Status__c", "Warranty_Account_Name__c", "Warranty_Group_Code__c",
    # Timeline
    "Date_of_Loss__c", "Claim_Date__c", "CreatedDate", "ClosedDate",
    "Days_from_loss_to_claim__c", "Age__c",
    # Financials
    "Estimated_Cost__c", "Actual_Invoice_Amount__c",
    # Related names (parent relationship queries)
    "Account.Name", "Contact.Name", "Contact.Email", "Contact.Phone",
    # Service Contract (via custom Warranty__c lookup)
    "Warranty__c", "Warranty__r.Name", "Warranty__r.StartDate",
    "Warranty__r.EndDate", "Warranty__r.Term",
    "Warranty__r.New_Used__c",
    "Warranty__r.Aggregate_Windshield__c",
    "Warranty__r.Aggregate_Paintguard__c",
    "Warranty__r.Aggregate_Leatherguard__c",
    "Warranty__r.Aggregate_Fiberguard__c",
    "Warranty__r.Aggregate_Rental_Car__c",
    "Warranty__r.Aggregate_Vinyl_Awning__c",
]

DAMAGE_LINE_FIELDS = [
    "Damage_Line_Count__c", "Status__c", "Claim_Type__c", "Type_of_Damage__c",
    "Claim_Determination__c", "Cause_of_Damage__c",
    "Location__c", "Side_of_Vehicle__c", "Rectification_Method__c",
    "Estimate__c", "Approved_Amount__c", "Deductible_Amount__c",
    "Actual_Invoice_Amount__c", "Total_Invoice_Line_Amount__c",
    "Technician_Instructions__c",
    "Denial_Reason__c", "Denial_Reason_Text__c",
]

CASE_QUERY = (
    "SELECT " + ", ".join(CASE_FIELDS) +
    ", (SELECT " + ", ".join(DAMAGE_LINE_FIELDS) +
    " FROM Rectification_Lines__r ORDER BY Damage_Line_Count__c ASC)"
    ", (SELECT Id, CommentBody, CreatedDate, CreatedBy.Name FROM CaseComments ORDER BY CreatedDate ASC)"
    " FROM Case WHERE CaseNumber = '{}'"
)

# ---------------------------------------------------------------------------
# Salesforce auth & queries
# ---------------------------------------------------------------------------

def get_access_token(use_test=True):
    if use_test:
        token_url = "https://permaplate--full.sandbox.my.salesforce.com/services/oauth2/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": os.environ["SF_TEST_CLIENT_ID"],
            "client_secret": os.environ["SF_TEST_CLIENT_SECRET"],
        }
    else:
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


def sf_query(access_token, instance_url, query):
    """Execute a SOQL query and return raw response JSON."""
    response = requests.get(
        f"{instance_url}/services/data/v62.0/query",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": query},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def get_case(access_token, instance_url, case_number):
    query = CASE_QUERY.format(case_number)
    data = sf_query(access_token, instance_url, query)
    if data["totalSize"] == 0:
        return None
    return data["records"][0]

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _val(case, field, default="--"):
    """Get a field value, returning default for None."""
    val = case.get(field)
    if val is None:
        return default
    return val


def _get_comments(case):
    """Extract CaseComments into a list of strings."""
    comments_data = case.get("CaseComments")
    if not comments_data or not comments_data.get("records"):
        return []
    comments = []
    for c in comments_data["records"]:
        author = c.get("CreatedBy", {}).get("Name", "Unknown") if c.get("CreatedBy") else "Unknown"
        date = c.get("CreatedDate", "")[:10]
        body = c.get("CommentBody", "")
        comments.append(f"[{date} - {author}] {body}")
    return comments


def _get_damage_lines(case):
    """Extract Rectification_Lines__r into a list of dicts."""
    data = case.get("Rectification_Lines__r")
    if not data or not data.get("records"):
        return []
    return data["records"]

# ---------------------------------------------------------------------------
# Contract summary
# ---------------------------------------------------------------------------

def summarize_contract(base64_pdf):
    """Send contract PDF to Claude and get a concise coverage summary."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64_pdf,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Summarize this vehicle service agreement concisely. Include:\n"
                            "- Covered components/systems\n"
                            "- Coverage exclusions\n"
                            "- Term and mileage limits\n"
                            "- Deductible amount\n"
                            "- Any notable conditions or limitations\n"
                            "Use bullet points. Be direct."
                        ),
                    },
                ],
            }
        ],
    )
    return response.content[0].text


def fetch_contract_summary(case):
    """Given a case dict, fetch and summarize the contract PDF. Returns summary string or None."""
    warranty = case.get("Warranty__r")
    contract_number = warranty.get("Name") if warranty else None
    if not contract_number:
        return None

    print(f"  Fetching contract {contract_number} from PermaPlate...")
    base64_pdf = getContractPDF(contract_number)
    if not base64_pdf:
        print(f"  Contract {contract_number} not found in PermaPlate.")
        return None

    print(f"  Summarizing contract with Claude...")
    try:
        return summarize_contract(base64_pdf)
    except Exception as e:
        print(f"  Error summarizing contract: {e}")
        return None
