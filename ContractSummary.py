##
## Look up a case in Salesforce and summarize its contract coverage
##
from sf_shared import get_access_token, get_case, fetch_contract_summary, _val


def build_output(case, coverage):
    """Build the contract summary output as a string."""
    lines = []
    case_number = _val(case, 'CaseNumber')

    lines.append(f"# Contract Summary — {case_number}")
    lines.append(f"\n## Case Overview")
    lines.append(f"- **Case Number:** {case_number}")
    lines.append(f"- **Status:** {_val(case, 'Status')}")
    lines.append(f"- **Determination:** {_val(case, 'Claim_Determination__c')}")
    lines.append(f"- **Product:** {_val(case, 'Product__c')}")
    lines.append(f"- **Vehicle:** {_val(case, 'Vehicle_Year__c')} {_val(case, 'Vehicle_Make__c')} {_val(case, 'Vehicle_Model__c')}")
    lines.append(f"- **Date of Loss:** {_val(case, 'Date_of_Loss__c')}")
    lines.append(f"- **Claim Date:** {_val(case, 'Claim_Date__c')}")

    warranty = case.get("Warranty__r")
    if warranty:
        lines.append(f"\n## Contract Details")
        lines.append(f"- **Contract:** {warranty.get('Name', '--')}")
        lines.append(f"- **Coverage:** {warranty.get('StartDate', '--')} to {warranty.get('EndDate', '--')} ({warranty.get('Term', '--')} months)")
        lines.append(f"- **New/Used:** {warranty.get('New_Used__c', '--')}")

    if coverage:
        lines.append(f"\n## Coverage Summary\n")
        lines.append(coverage)
    else:
        lines.append(f"\n## Coverage Summary\nNo contract summary available.")

    return "\n".join(lines) + "\n"


def main():
    print("Authenticating with Salesforce...")
    access_token, instance_url = get_access_token(use_test=False)
    print(f"Connected to {instance_url}\n")

    case_number = input("Enter case number: ")
    while case_number != "":
        case = get_case(access_token, instance_url, case_number)
        if case:
            coverage = fetch_contract_summary(case)
            output = build_output(case, coverage)
            filename = f"contract_summary/{case_number}.md"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"  Written to {filename}")
        else:
            print(f"Case {case_number} not found.\n")
        case_number = input("Enter case number: ")


if __name__ == "__main__":
    main()
