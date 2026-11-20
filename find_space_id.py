import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

# Load credentials from your .env
load_dotenv()

JIRA_URL = os.getenv("JIRA_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_TOKEN = os.getenv("JIRA_TOKEN")

# Confluence API v2 endpoint to list spaces
url = f"{JIRA_URL}/wiki/api/v2/spaces"

headers = {
    "Accept": "application/json"
}

response = requests.get(
    url,
    headers=headers,
    auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_TOKEN)
)

if response.status_code == 200:
    spaces = response.json().get("results", [])
    print("\n✅ Confluence Spaces Found:")
    for space in spaces:
        print(f"Name: {space.get('name')} | Key: {space.get('key')} | ID: {space.get('id')}")
else:
    print(f"❌ Failed to fetch spaces. Status: {response.status_code}")
    print(response.text)
