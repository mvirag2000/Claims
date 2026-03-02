##
## Retrieve Case objects from Salesforce and generate quality reports
##
import os
from typing import Optional
from datetime import datetime
import requests
from dotenv import load_dotenv
import anthropic

load_dotenv()

client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var
MODEL = "claude-sonnet-4-20250514"

CASE_FIELDS = [
    # Claim overview
    "CaseNumber", "Status", "Sub_Status__c", "Type", "Priority", "Origin",
    "Claim_Determination__c", "Resolution__c", "Who_Filed_Claim__c",
    "Denial_Reason__c", "Denial_Reason_Text__c", "Claim_Number__c",
    "Confirmation_Num__c", "Claim_Score__c", "Original_Owner_Toggle__c",
    # Unstructured / narrative
    "Subject", "Description", "Case_Summary__c",
    "Additional_Information__c", "Attempt_to_Rectify__c",
    # Vehicle
    "Vehicle_Year__c", "Vehicle_Make__c", "Vehicle_Model__c",
    # Product & damage
    "Product__c", "Product_Group__c", "Product_Type__c",
    "Damage_Type__c", "Location__c", "Side_of_Vehicle__c", "Vehicle_Row__c",
    # Warranty
    "Warranty_Status__c", "Warranty_Account_Name__c", "Warranty_Group_Code__c",
    # Timeline
    "Date_of_Loss__c", "Claim_Date__c", "CreatedDate", "ClosedDate",
    "Days_from_loss_to_claim__c", "Age__c",
    # Financials - estimated
    "Estimated_Cost__c", "EST_Parts_Cost__c", "EST_Labor_Cost__c",
    "EST_Labor_Amount__c", "EST_Tax_Cost__c", "EST_Total_Cost__c",
    # Financials - actual
    "ACT_Parts_Cost__c", "ACT_Labor_Cost__c", "ACT_Labor_Amount__c",
    "ACT_Tax_Cost__c", "ACT_Total_Cost__c",
    # Financials - amounts
    "Approved_Amount__c", "Permaplate_Amount__c", "Dealer_Amount__c",
    "Sales_Amount__c", "Invoice_Total__c", "Actual_Invoice_Amount__c",
    # Related names (parent relationship queries)
    "Account.Name", "Contact.Name", "Contact.Email", "Contact.Phone",
]

CASE_QUERY = (
    "SELECT " + ", ".join(CASE_FIELDS) +
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

    # EST breakdown
    est_fields = ["EST_Parts_Cost__c", "EST_Labor_Cost__c", "EST_Tax_Cost__c"]
    if all(case.get(f) is None for f in est_fields):
        flags.append("No EST cost breakdown")

    # ACT breakdown
    act_fields = ["ACT_Parts_Cost__c", "ACT_Labor_Cost__c", "ACT_Tax_Cost__c"]
    if all(case.get(f) is None for f in act_fields):
        flags.append("No ACT cost breakdown")

    if case.get("Invoice_Total__c") is None and case.get("Actual_Invoice_Amount__c") is None:
        flags.append("No invoice amount")

    comments = _get_comments(case)
    if not comments:
        flags.append("No CaseComments")

    if not case.get("Date_of_Loss__c"):
        flags.append("No Date of Loss")

    return flags


def analyze_narrative(case):
    """Send unstructured text fields to Claude for quality analysis."""
    comments = _get_comments(case)
    comments_text = "\n".join(comments) if comments else "(none)"

    narrative = f"""Subject: {_val(case, 'Subject', '(empty)')}

Description: {_val(case, 'Description', '(empty)')}

Case Summary: {_val(case, 'Case_Summary__c', '(empty)')}

Additional Information: {_val(case, 'Additional_Information__c', '(empty)')}

Attempt to Rectify: {_val(case, 'Attempt_to_Rectify__c', '(empty)')}

Case Comments:
{comments_text}"""

    system_prompt = """You are a claims quality analyst reviewing vehicle service agreement claims.
Analyze the claim narrative below for:
1. Completeness -- are the key facts present (what happened, when, where on vehicle, damage description)?
2. Clarity -- is the narrative clear and understandable?
3. Red flags -- anything unusual, inconsistent, or concerning?
4. Missing information -- what important details are absent that should be captured?

Be concise and direct. Use bullet points. Focus on actionable observations."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": narrative}],
    )
    return response.content[0].text


def generate_report(case):
    """Build a structured quality report for a case and write to file."""
    case_number = case["CaseNumber"]
    lines = []

    def out(text=""):
        lines.append(text)

    out("=" * 70)
    out(f"  CASE QUALITY REPORT -- {case_number}")
    out("=" * 70)

    # Claim Overview
    out("\n-- Claim Overview --")
    out(f"  Case Number:   {_val(case, 'CaseNumber')}")
    out(f"  Status:        {_val(case, 'Status')}")
    out(f"  Sub-Status:    {_val(case, 'Sub_Status__c')}")
    out(f"  Determination: {_val(case, 'Claim_Determination__c')}")
    out(f"  Resolution:    {_val(case, 'Resolution__c')}")
    out(f"  Priority:      {_val(case, 'Priority')}")
    out(f"  Origin:        {_val(case, 'Origin')}")
    out(f"  Who Filed:     {_val(case, 'Who_Filed_Claim__c')}")
    out(f"  Claim Score:   {_val(case, 'Claim_Score__c')}")
    if case.get("Denial_Reason__c"):
        out(f"  Denial Reason: {case['Denial_Reason__c']}")
        if case.get("Denial_Reason_Text__c"):
            out(f"  Denial Detail: {case['Denial_Reason_Text__c']}")

    # Contact
    contact = case.get("Contact")
    if contact:
        out("\n-- Contact --")
        out(f"  Name:  {contact.get('Name', '--')}")
        out(f"  Email: {contact.get('Email', '--')}")
        out(f"  Phone: {contact.get('Phone', '--')}")

    # Vehicle
    out("\n-- Vehicle --")
    year = _val(case, "Vehicle_Year__c")
    make = _val(case, "Vehicle_Make__c")
    model = _val(case, "Vehicle_Model__c")
    out(f"  {year} {make} {model}")

    # Product & Damage
    out("\n-- Product & Damage --")
    out(f"  Product:       {_val(case, 'Product__c')}")
    out(f"  Product Group: {_val(case, 'Product_Group__c')}")
    out(f"  Damage Type:   {_val(case, 'Damage_Type__c')}")
    out(f"  Location:      {_val(case, 'Location__c')}")
    out(f"  Side:          {_val(case, 'Side_of_Vehicle__c')}")
    out(f"  Row:           {_val(case, 'Vehicle_Row__c')}")

    # Warranty
    out("\n-- Warranty --")
    out(f"  Status:       {_val(case, 'Warranty_Status__c')}")
    out(f"  Account:      {_val(case, 'Warranty_Account_Name__c')}")
    out(f"  Group Code:   {_val(case, 'Warranty_Group_Code__c')}")

    # Timeline
    out("\n-- Timeline --")
    out(f"  Date of Loss:       {_date(case, 'Date_of_Loss__c')}")
    out(f"  Claim Date:         {_date(case, 'Claim_Date__c')}")
    out(f"  Created:            {_date(case, 'CreatedDate')}")
    out(f"  Closed:             {_date(case, 'ClosedDate')}")
    out(f"  Days Loss->Claim:   {_val(case, 'Days_from_loss_to_claim__c')}")
    out(f"  Age:                {_val(case, 'Age__c')}")

    # Financials
    out("\n-- Financials --")
    out(f"  Estimated Cost:     {_money(case, 'Estimated_Cost__c')}")
    out(f"    EST Parts:        {_money(case, 'EST_Parts_Cost__c')}")
    out(f"    EST Labor:        {_money(case, 'EST_Labor_Cost__c')}")
    out(f"    EST Labor Amt:    {_money(case, 'EST_Labor_Amount__c')}")
    out(f"    EST Tax:          {_money(case, 'EST_Tax_Cost__c')}")
    out(f"    EST Total:        {_money(case, 'EST_Total_Cost__c')}")
    out(f"  ACT Parts:          {_money(case, 'ACT_Parts_Cost__c')}")
    out(f"  ACT Labor:          {_money(case, 'ACT_Labor_Cost__c')}")
    out(f"  ACT Labor Amt:      {_money(case, 'ACT_Labor_Amount__c')}")
    out(f"  ACT Tax:            {_money(case, 'ACT_Tax_Cost__c')}")
    out(f"  ACT Total:          {_money(case, 'ACT_Total_Cost__c')}")
    out(f"  Approved Amount:    {_money(case, 'Approved_Amount__c')}")
    out(f"  Permaplate Amount:  {_money(case, 'Permaplate_Amount__c')}")
    out(f"  Dealer Amount:      {_money(case, 'Dealer_Amount__c')}")
    out(f"  Sales Amount:       {_money(case, 'Sales_Amount__c')}")
    out(f"  Invoice Total:      {_money(case, 'Invoice_Total__c')}")
    out(f"  Posted Invoice:     {_money(case, 'Actual_Invoice_Amount__c')}")

    # Narrative fields (raw)
    out("\n-- Narrative --")
    out(f"  Subject:     {_val(case, 'Subject')}")
    out(f"  Description: {_val(case, 'Description')}")
    summary = _val(case, "Case_Summary__c")
    if summary != "--":
        summary = summary.replace("\r\n", "\n").replace("\r", "\n")
        summary_lines = summary.split("\n")
        out(f"  Case Summary: {summary_lines[0]}")
        for line in summary_lines[1:]:
            out(f"                {line}")
    else:
        out(f"  Case Summary: --")

    # Comments
    comments = _get_comments(case)
    if comments:
        out(f"\n-- Comments ({len(comments)}) --")
        for c in comments:
            out(f"  {c}")
    else:
        out("\n-- Comments: none --")

    # Data completeness
    flags = _check_completeness(case)
    if flags:
        out("\n-- Data Completeness Flags --")
        for flag in flags:
            out(f"  * {flag}")
    else:
        out("\n-- Data Completeness: all key fields populated --")

    # LLM Analysis
    out("\n-- LLM Analysis --")
    print(f"  Analyzing {case_number} with Claude...")
    try:
        analysis = analyze_narrative(case)
        for line in analysis.split("\n"):
            out(f"  {line}")
    except Exception as e:
        out(f"  Error during analysis: {e}")

    out("\n" + "=" * 70)

    # Write to file
    filename = f"reports/{case_number}.txt"
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
