##
## Retrieve Case objects from Salesforce and generate quality reports
##
import os
from typing import Optional
from datetime import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv
import anthropic
from Contract import getContractPDF

load_dotenv()

client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var
MODEL = "claude-sonnet-4-6"

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


def get_case(access_token, instance_url, case_number):
    query = CASE_QUERY.format(case_number)
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


LINE_ITEM_FIELDS = [
    "LineItemNumber", "ProductName__c", "Description",
    "Repair__c", "StartDate", "EndDate", "Status",
]

LINE_ITEM_QUERY = (
    "SELECT " + ", ".join(LINE_ITEM_FIELDS) +
    " FROM ContractLineItem WHERE ServiceContractId = '{}'"
    " ORDER BY LineItemNumber ASC"
)


def get_line_items(access_token, instance_url, service_contract_id):
    """Fetch ContractLineItems for a ServiceContract."""
    query = LINE_ITEM_QUERY.format(service_contract_id)
    response = requests.get(
        f"{instance_url}/services/data/v62.0/query",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": query},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    return data["records"] if data["totalSize"] > 0 else []


def _val(case, field, default="--"):
    """Get a field value, returning default for None."""
    val = case.get(field)
    if val is None:
        return default
    return val


def _money(case, field):
    """Format a currency field."""
    val = case.get(field)
    if val is None:
        return "--"
    return f"${val:,.2f}"


def _date(case, field):
    """Format a date or datetime field."""
    val = case.get(field)
    if val is None:
        return "--"
    # Datetime fields come as "2024-10-17T18:18:02.000+0000"
    if "T" in str(val):
        try:
            dt = datetime.fromisoformat(val.replace("+0000", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, AttributeError):
            return str(val)
    return str(val)


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


def _check_completeness(case):
    """Flag key fields that are empty/missing."""
    flags = []
    if not case.get("Description"):
        flags.append("No Description")
    if not case.get("Case_Summary__c"):
        flags.append("No Case Summary")
    if not case.get("Additional_Information__c"):
        flags.append("No Additional Information")
    if not case.get("Attempt_to_Rectify__c"):
        flags.append("No Attempt to Rectify")

    if case.get("Actual_Invoice_Amount__c") is None:
        flags.append("No invoice amount")

    comments = _get_comments(case)
    if not comments:
        flags.append("No CaseComments")

    if not case.get("Date_of_Loss__c"):
        flags.append("No Date of Loss")

    if not _get_damage_lines(case):
        flags.append("No Damage Lines")

    return flags


def _build_structured_summary(case, line_items=None):
    """Build a concise summary of structured fields for LLM context."""
    parts = []
    parts.append(f"Status: {_val(case, 'Status')}")
    parts.append(f"Determination: {_val(case, 'Claim_Determination__c')}")
    parts.append(f"Vehicle: {_val(case, 'Vehicle_Year__c')} {_val(case, 'Vehicle_Make__c')} {_val(case, 'Vehicle_Model__c')}")
    parts.append(f"Product: {_val(case, 'Product__c')}")
    parts.append(f"Date of Loss: {_val(case, 'Date_of_Loss__c')}")
    parts.append(f"Days Loss to Claim: {_val(case, 'Days_from_loss_to_claim__c')}")
    parts.append(f"Warranty Status: {_val(case, 'Warranty_Status__c')}")

    # Warranty detail from ServiceContract
    warranty = case.get("Warranty__r")
    if warranty:
        parts.append(f"Warranty: {warranty.get('Name', '--')} | Start: {warranty.get('StartDate', '--')} | End: {warranty.get('EndDate', '--')} | Term: {warranty.get('Term', '--')} | New/Used: {warranty.get('New_Used__c', '--')}")
        agg_fields = [
            ("Aggregate_Windshield__c", "Windshield"),
            ("Aggregate_Paintguard__c", "Paintguard"),
            ("Aggregate_Leatherguard__c", "Leatherguard"),
            ("Aggregate_Fiberguard__c", "Fiberguard"),
            ("Aggregate_Rental_Car__c", "Rental Car"),
            ("Aggregate_Vinyl_Awning__c", "Vinyl/Awning"),
        ]
        aggs = [f"{label}: ${warranty[f]:,.2f}" for f, label in agg_fields if warranty.get(f) is not None]
        if aggs:
            parts.append(f"Aggregate Limits: {' | '.join(aggs)}")

    # Warranty line items
    if line_items:
        parts.append(f"Warranty Line Items: {len(line_items)}")
        for li in line_items:
            items = [li.get('ProductName__c', '?')]
            if li.get('Repair__c'):
                items.append("Repair: Yes")
            if li.get('Status'):
                items.append(f"Status: {li['Status']}")
            if li.get('EndDate'):
                items.append(f"Ends: {li['EndDate']}")
            parts.append("  " + " | ".join(items))

    damage_lines = _get_damage_lines(case)
    if damage_lines:
        parts.append(f"Damage Lines: {len(damage_lines)}")
        for dl in damage_lines:
            line_num = int(dl.get("Damage_Line_Count__c", 0)) if dl.get("Damage_Line_Count__c") else "?"
            items = [f"Line {line_num}"]
            for field, label in [
                ("Claim_Type__c", "Claim Type"),
                ("Type_of_Damage__c", "Damage"),
                ("Cause_of_Damage__c", "Cause"),
                ("Location__c", "Location"),
                ("Side_of_Vehicle__c", "Side"),
                ("Rectification_Method__c", "Method"),
                ("Claim_Determination__c", "Determination"),
            ]:
                v = dl.get(field)
                if v:
                    items.append(f"{label}: {v}")
            for field, label in [
                ("Estimate__c", "Estimate"),
                ("Approved_Amount__c", "Approved"),
                ("Actual_Invoice_Amount__c", "Invoice"),
            ]:
                v = dl.get(field)
                if v is not None:
                    items.append(f"{label}: ${v:,.2f}")
            parts.append("  " + " | ".join(items))
    else:
        parts.append("Damage Lines: none")

    return "\n".join(parts)


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


def analyze_narrative(case, coverage_summary=None, line_items=None):
    """Send structured summary and narrative fields to Claude for quality analysis."""
    comments = _get_comments(case)
    comments_text = "\n".join(comments) if comments else "(none)"
    structured = _build_structured_summary(case, line_items)

    prompt = f"""STRUCTURED DATA ON FILE:
{structured}

NARRATIVE FIELDS:
Subject: {_val(case, 'Subject', '(empty)')}

Description: {_val(case, 'Description', '(empty)')}

Case Summary: {_val(case, 'Case_Summary__c', '(empty)')}

Additional Information: {_val(case, 'Additional_Information__c', '(empty)')}

Attempt to Rectify: {_val(case, 'Attempt_to_Rectify__c', '(empty)')}

Case Comments:
{comments_text}"""

    if coverage_summary:
        prompt += f"""

CONTRACT COVERAGE SUMMARY:
{coverage_summary}"""

    system_prompt = Path("qa_instructions.txt").read_text(encoding="utf-8")

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def generate_report(case, line_items=None):
    """Build a structured quality report for a case and write to file."""
    if line_items is None:
        line_items = []
    case_number = case["CaseNumber"]
    lines = []

    def out(text=""):
        lines.append(text)

    out(f"# Case Quality Report — {case_number}")

    # Claim Overview
    out("\n## Claim Overview")
    out(f"- **Case Number:** {_val(case, 'CaseNumber')}")
    out(f"- **Status:** {_val(case, 'Status')}")
    out(f"- **Who Filed:** {_val(case, 'Who_Filed_Claim__c')}")

    # Contact
    contact = case.get("Contact")
    if contact:
        out("\n## Contact")
        out(f"- **Name:** {contact.get('Name', '--')}")
        out(f"- **Email:** {contact.get('Email', '--')}")
        out(f"- **Phone:** {contact.get('Phone', '--')}")

    # Vehicle
    out("\n## Vehicle")
    out(f"- **Make:** {_val(case, 'Vehicle_Make__c')}")
    out(f"- **Model:** {_val(case, 'Vehicle_Model__c')}")
    out(f"- **Year:** {_val(case, 'Vehicle_Year__c')}")

    # Damage Lines (includes product & damage detail per Jordan's layout)
    damage_lines = _get_damage_lines(case)
    if damage_lines:
        out(f"\n## Damage Lines ({len(damage_lines)})")
        for dl in damage_lines:
            line_num = dl.get("Damage_Line_Count__c", "?")
            out(f"\n### Line {int(line_num) if line_num else '?'}")
            out(f"- **Type of Damage:** {dl.get('Type_of_Damage__c', '--')}")
            out(f"- **Determination:** {dl.get('Claim_Determination__c', '--')}")
            if dl.get('Denial_Reason__c'):
                out(f"- **Denial Reason:** {dl['Denial_Reason__c']}")
                if dl.get('Denial_Reason_Text__c'):
                    out(f"- **Denial Detail:** {dl['Denial_Reason_Text__c']}")
            out(f"- **Rectification Method:** {dl.get('Rectification_Method__c', '--')}")
            approved = dl.get('Approved_Amount__c')
            invoice = dl.get('Actual_Invoice_Amount__c')
            out(f"- **Approved Amount:** {'${:,.2f}'.format(approved) if approved is not None else '--'}")
            out(f"- **Posted Invoice:** {'${:,.2f}'.format(invoice) if invoice is not None else '--'}")
            tech = dl.get('Technician_Instructions__c')
            if tech:
                out(f"- **Technician Instructions:** {tech}")
    else:
        out("\n## Damage Lines\nNone")

    # Timeline
    out("\n## Timeline")
    out(f"- **Date of Loss:** {_date(case, 'Date_of_Loss__c')}")
    out(f"- **Claim Date:** {_date(case, 'Claim_Date__c')}")
    out(f"- **Created:** {_date(case, 'CreatedDate')}")
    out(f"- **Closed:** {_date(case, 'ClosedDate')}")

    # Financials (case-level totals)
    out("\n## Financials")
    # Sum approved from damage lines since there's no case-level Approved_Amount__c
    total_approved = sum(
        dl.get('Approved_Amount__c', 0) or 0 for dl in damage_lines
    )
    out(f"- **Total Approved:** {'${:,.2f}'.format(total_approved) if damage_lines else '--'}")
    out(f"- **Posted Invoice:** {_money(case, 'Actual_Invoice_Amount__c')}")

    # Narrative
    out("\n## Narrative")
    summary = _val(case, "Case_Summary__c")
    if summary != "--":
        summary = summary.replace("\r\n", "\n").replace("\r", "\n")
        out(f"**Case Summary:**\n{summary}")
    else:
        out(f"**Case Summary:** --")

    # Data completeness
    flags = _check_completeness(case)
    if flags:
        out("\n## Data Completeness Flags")
        for flag in flags:
            out(f"- {flag}")
    else:
        out("\n## Data Completeness\nAll key fields populated.")

    # Contract coverage summary (fed to LLM, not shown in report)
    coverage_summary = None
    warranty = case.get("Warranty__r")
    contract_number = warranty.get("Name") if warranty else None
    if contract_number:
        print(f"  Fetching contract {contract_number} from PermaPlate...")
        base64_pdf = getContractPDF(contract_number)
        if base64_pdf:
            print(f"  Summarizing contract with Claude...")
            try:
                coverage_summary = summarize_contract(base64_pdf)
            except Exception as e:
                print(f"  Error summarizing contract: {e}")
        else:
            print(f"  Contract {contract_number} not found in PermaPlate.")

    # LLM Analysis
    out("\n## LLM Analysis")
    print(f"  Analyzing {case_number} with Claude...")
    try:
        analysis = analyze_narrative(case, coverage_summary, line_items)
        out(analysis)
    except Exception as e:
        out(f"*Error during analysis: {e}*")

    # Write to file
    filename = f"reports/{case_number}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  Report written to {filename}")
    return filename


def main():
    print("Authenticating with Salesforce...")
    access_token, instance_url = get_access_token(use_test=False)
    print(f"Connected to {instance_url}\n")

    case_number = input("Enter case number: ")
    while case_number != "":
        case = get_case(access_token, instance_url, case_number)
        if case:
            line_items = []
            warranty_id = case.get("Warranty__c")
            if warranty_id:
                line_items = get_line_items(access_token, instance_url, warranty_id)
            generate_report(case, line_items)
        else:
            print(f"Case {case_number} not found.\n")
        case_number = input("Enter case number: ")

if __name__ == "__main__":
    main()
