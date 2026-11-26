import os
import requests
from requests.auth import HTTPBasicAuth


def create_servicenow_change(ticket_data):
    """
    Creates a new Change Request (CHG) in ServiceNow using the Table REST API
    and returns the direct URL and the CHG number (e.g., CHG0030001).
    """
    snow_url = os.getenv("SERVICENOW_URL", "https://dev489662.service-now.com").rstrip("/")
    snow_user = os.getenv("SERVICENOW_USER")
    snow_password = os.getenv("SERVICENOW_PASSWORD")

    if not snow_url or not snow_user or not snow_password:
        return {
            "success": False,
            "message": "ServiceNow credentials (SERVICENOW_URL, SERVICENOW_USER, SERVICENOW_PASSWORD) are missing in .env!"
        }

    endpoint = f"{snow_url}/api/now/table/change_request"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    ticket_key = ticket_data.get("ticket", "")
    summary = ticket_data.get("summary", "Jira Deployment Change")
    description = ticket_data.get("description", "")
    pr_to_prod = ticket_data.get("pr_to_prod", "")

    payload = {
        "short_description": f"[{ticket_key}] {summary}",
        "description": (
            f"Jira Ticket: {ticket_key}\n"
            f"Assignee: {ticket_data.get('assignee', 'Unassigned')}\n"
            f"Production PR: {pr_to_prod}\n\n"
            f"Details:\n{description}"
        ),
        "category": "Software",
        "type": "normal"
    }

    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers=headers,
            auth=HTTPBasicAuth(snow_user, snow_password),
            timeout=30
        )

        if response.status_code in [200, 201]:
            result = response.json().get("result", {})
            chg_number = result.get("number", "CHG")
            record_url = f"{snow_url}/nav_to.do?uri=change_request.do?sysparm_query=number={chg_number}"

            return {
                "success": True,
                "url": record_url,
                "number": chg_number
            }
        else:
            return {
                "success": False,
                "message": f"ServiceNow API returned status {response.status_code}: {response.text}"
            }

    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }
