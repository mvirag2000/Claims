##
## Pull contract PDF from API and send to Claude for queries
##
from typing import Optional
import requests
import xml.etree.ElementTree as ET
import os
from requests.exceptions import HTTPError, Timeout
from dotenv import load_dotenv
import anthropic

load_dotenv()

client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var
MODEL = "claude-sonnet-4-20250514"

# endpoint = "https://webtest.test.perma-plate.com"
endpoint = "https://warranties.permaplate.com"
url = endpoint + "/api/ppapi.asmx"

def getContractPDF(contract: str) -> Optional[str]:
    print(f"Fetching contract {contract} from PermaPlate")
    headers = {
        "Content-Type": "text/xml; charset=utf-8"
    }
    username = os.environ["PERMAPLATE_USERNAME"]
    password = os.environ["PERMAPLATE_PASSWORD"]
    body = f"""<?xml version="1.0" encoding="utf-8"?>
    <env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope" xmlns:ns1="https://warranties.permaplate.com/api/" xmlns:ns2="https://warranties.permaplate.com/api/">
    <env:Header>
    <SecuredWebServiceHeader xmlns="https://warranties.permaplate.com/api/">
    <Username>{username}</Username>
    <Password>{password}</Password>
    </SecuredWebServiceHeader>
    </env:Header>
    <env:Body>
    <WarrantyNo xmlns="https://warranties.permaplate.com/api/">{contract}</WarrantyNo>
    </env:Body>
    </env:Envelope>"""

    try:
        response = requests.post(url, data=body, headers=headers, timeout=10)
        response.raise_for_status()
    except Timeout:
        print("Request timed out. Server may be offline or overloaded.")
        return None
    except HTTPError as http_err:
        print(f"HTTP error occurred: {http_err} (status code: {response.status_code})")
        return None

    root = ET.fromstring(response.text)
    ns = {'ns0': "https://warranties.permaplate.com/api/"}
    result = root.find('.//ns0:GetWarrantyPDFResult', ns)

    if result is None or result.text is None or len(result.text) <= 4:
        return None

    return result.text

def main():
    contract = input("Enter contract number: ")
    while contract != "":
        base64_pdf = getContractPDF(contract)
        if base64_pdf is not None:
            messages = [
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
                            "text": "This is a vehicle service agreement. I'm going to ask you questions about it.",
                        },
                    ],
                }
            ]
            response = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content[0].text})

            print("You can now ask questions about the contract. Type 'exit' to quit.\n")
            query_text = input("Enter query: ")
            while query_text != "exit":
                messages.append({"role": "user", "content": query_text})
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=1024,
                    messages=messages,
                )
                reply = response.content[0].text
                print(f"Claude: {reply}\n")
                messages.append({"role": "assistant", "content": reply})
                query_text = input("Enter query: ")
        else:
            print(f"Contract {contract} not found.")

        print(f"Exiting contract {contract}")
        contract = input("\nEnter another contract number: ")

if __name__ == "__main__":
    main()
