##
## Quick utility to describe a Salesforce object's fields
##
import os
import sys
import requests
from dotenv import load_dotenv
from Salesforce import get_access_token

load_dotenv()

def describe(object_name):
    access_token, instance_url = get_access_token(use_test=False)
    response = requests.get(
        f"{instance_url}/services/data/v62.0/sobjects/{object_name}/describe",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    print(f"\n=== {object_name} Fields ===\n")
    for f in sorted(data["fields"], key=lambda x: x["name"]):
        custom = " (custom)" if f["custom"] else ""
        print(f"  {f['name']:50s} {f['type']:15s}{custom}")

    child_rels = [r for r in data["childRelationships"] if r["relationshipName"]]
    if child_rels:
        print(f"\n=== {object_name} Child Relationships ===\n")
        for r in sorted(child_rels, key=lambda x: x["relationshipName"]):
            print(f"  {r['relationshipName']:50s} -> {r['childSObject']}.{r['field']}")

if __name__ == "__main__":
    obj = sys.argv[1] if len(sys.argv) > 1 else "ServiceContract"
    describe(obj)
