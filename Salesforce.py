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


def _build_structured_summary(case):
    """Build a concise summary of structured fields for LLM context."""
    parts = []
    parts.append(f"Status: {_val(case, 'Status')}")
    parts.append(f"Determination: {_val(case, 'Claim_Determination__c')}")
    parts.append(f"Vehicle: {_val(case, 'Vehicle_Year__c')} {_val(case, 'Vehicle_Make__c')} {_val(case, 'Vehicle_Model__c')}")
    parts.append(f"Product: {_val(case, 'Product__c')}")
    parts.append(f"Date of Loss: {_val(case, 'Date_of_Loss__c')}")
    parts.append(f"Days Loss to Claim: {_val(case, 'Days_from_loss_to_claim__c')}")
    parts.append(f"Warranty Status: {_val(case, 'Warranty_Status__c')}")

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


def analyze_narrative(case):
    """Send structured summary and narrative fields to Claude for quality analysis."""
    comments = _get_comments(case)
    comments_text = "\n".join(comments) if comments else "(none)"
    structured = _build_structured_summary(case)

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

    system_prompt = Path("qa_instructions.txt").read_text(encoding="utf-8")

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def generate_report(case):
    """Build a structured quality report for a case and write to file."""
    case_number = case["CaseNumber"]
    lines = []

    def out(text=""):
        lines.append(text)

    out(f"# Case Quality Report — {case_number}")

    # Claim Overview
    out("\n## Claim Overview")
    out(f"- **Case Number:** {_val(case, 'CaseNumber')}")
    out(f"- **Status:** {_val(case, 'Status')}")
    out(f"- **Determination:** {_val(case, 'Claim_Determination__c')}")
    out(f"- **Resolution:** {_val(case, 'Resolution__c')}")
    out(f"- **Origin:** {_val(case, 'Origin')}")
    out(f"- **Who Filed:** {_val(case, 'Who_Filed_Claim__c')}")
    if case.get("Denial_Reason__c"):
        out(f"- **Denial Reason:** {case['Denial_Reason__c']}")
        if case.get("Denial_Reason_Text__c"):
            out(f"- **Denial Detail:** {case['Denial_Reason_Text__c']}")

    # Contact
    contact = case.get("Contact")
    if contact:
        out("\n## Contact")
        out(f"- **Name:** {contact.get('Name', '--')}")
        out(f"- **Email:** {contact.get('Email', '--')}")
        out(f"- **Phone:** {contact.get('Phone', '--')}")

    # Vehicle
    out("\n## Vehicle")
    year = _val(case, "Vehicle_Year__c")
    make = _val(case, "Vehicle_Make__c")
    model = _val(case, "Vehicle_Model__c")
    out(f"{year} {make} {model}")

    # Product & Damage
    out("\n## Product & Damage")
    out(f"- **Product:** {_val(case, 'Product__c')}")
    out(f"- **Product Group:** {_val(case, 'Product_Group__c')}")
    out(f"- **Damage Type:** {_val(case, 'Damage_Type__c')}")
    out(f"- **Location:** {_val(case, 'Location__c')}")
    out(f"- **Side:** {_val(case, 'Side_of_Vehicle__c')}")

    # Warranty
    out("\n## Warranty")
    warranty = case.get("Warranty__r")
    out(f"- **Contract #:** {warranty.get('Name', '--') if warranty else '--'}")
    out(f"- **Status:** {_val(case, 'Warranty_Status__c')}")
    out(f"- **Account:** {_val(case, 'Warranty_Account_Name__c')}")
    out(f"- **Group Code:** {_val(case, 'Warranty_Group_Code__c')}")
    warranty_start = warranty.get('StartDate', '--') if warranty else '--'
    out(f"- **Start Date:** {warranty_start}")

    # Timeline
    out("\n## Timeline")
    out(f"- **Date of Loss:** {_date(case, 'Date_of_Loss__c')}")
    out(f"- **Claim Date:** {_date(case, 'Claim_Date__c')}")
    out(f"- **Created:** {_date(case, 'CreatedDate')}")
    out(f"- **Closed:** {_date(case, 'ClosedDate')}")
    out(f"- **Days Loss→Claim:** {_val(case, 'Days_from_loss_to_claim__c')}")
    out(f"- **Age:** {_val(case, 'Age__c')}")

    # Financials
    out("\n## Financials")
    out(f"- **Estimated Cost:** {_money(case, 'Estimated_Cost__c')}")
    out(f"- **Posted Invoice:** {_money(case, 'Actual_Invoice_Amount__c')}")

    # Damage Lines
    damage_lines = _get_damage_lines(case)
    if damage_lines:
        out(f"\n## Damage Lines ({len(damage_lines)})")
        for dl in damage_lines:
            line_num = dl.get("Damage_Line_Count__c", "?")
            out(f"\n### Line {int(line_num) if line_num else '?'}")
            out(f"- **Status:** {dl.get('Status__c', '--')}")
            out(f"- **Claim Type:** {dl.get('Claim_Type__c', '--')}")
            out(f"- **Type of Damage:** {dl.get('Type_of_Damage__c', '--')}")
            out(f"- **Determination:** {dl.get('Claim_Determination__c', '--')}")
            out(f"- **Cause:** {dl.get('Cause_of_Damage__c', '--')}")
            out(f"- **Location:** {dl.get('Location__c', '--')}")
            out(f"- **Side:** {dl.get('Side_of_Vehicle__c', '--')}")
            out(f"- **Method:** {dl.get('Rectification_Method__c', '--')}")
            estimate = dl.get('Estimate__c')
            approved = dl.get('Approved_Amount__c')
            deductible = dl.get('Deductible_Amount__c')
            invoice = dl.get('Actual_Invoice_Amount__c')
            total_inv = dl.get('Total_Invoice_Line_Amount__c')
            if any(v is not None for v in [estimate, approved, deductible, invoice, total_inv]):
                out(f"- **Estimate:** {'${:,.2f}'.format(estimate) if estimate is not None else '--'}")
                out(f"- **Approved:** {'${:,.2f}'.format(approved) if approved is not None else '--'}")
                out(f"- **Deductible:** {'${:,.2f}'.format(deductible) if deductible is not None else '--'}")
                out(f"- **Invoice:** {'${:,.2f}'.format(invoice) if invoice is not None else '--'}")
                out(f"- **Total Invoice Line:** {'${:,.2f}'.format(total_inv) if total_inv is not None else '--'}")
            tech = dl.get('Technician_Instructions__c')
            if tech:
                out(f"- **Tech Instructions:** {tech}")
            if dl.get('Denial_Reason__c'):
                out(f"- **Denial Reason:** {dl['Denial_Reason__c']}")
                if dl.get('Denial_Reason_Text__c'):
                    out(f"- **Denial Detail:** {dl['Denial_Reason_Text__c']}")
    else:
        out("\n## Damage Lines\nNone")

    # Narrative fields
    out("\n## Narrative")
    out(f"**Subject:** {_val(case, 'Subject')}")
    out(f"\n**Description:** {_val(case, 'Description')}")
    summary = _val(case, "Case_Summary__c")
    if summary != "--":
        summary = summary.replace("\r\n", "\n").replace("\r", "\n")
        out(f"\n**Case Summary:**\n{summary}")
    else:
        out(f"\n**Case Summary:** --")

    # Comments
    comments = _get_comments(case)
    if comments:
        out(f"\n## Comments ({len(comments)})")
        for c in comments:
            out(f"- {c}")
    else:
        out("\n## Comments\nNone")

    # Data completeness
    flags = _check_completeness(case)
    if flags:
        out("\n## Data Completeness Flags")
        for flag in flags:
            out(f"- {flag}")
    else:
        out("\n## Data Completeness\nAll key fields populated.")

    # LLM Analysis
    out("\n## LLM Analysis")
    print(f"  Analyzing {case_number} with Claude...")
    try:
        analysis = analyze_narrative(case)
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
            generate_report(case)
        else:
            print(f"Case {case_number} not found.\n")
        case_number = input("Enter case number: ")

if __name__ == "__main__":
    main()
