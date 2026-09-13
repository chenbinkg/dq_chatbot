import os
import requests
import json
import time
import re
from typing import Any, Dict, List, Optional, Union, Tuple
import logging
import business_unit

logger = logging.getLogger(__name__)

CHANGE_REQUEST_EPIC_KEY = os.getenv("CHANGE_REQUEST_EPIC_KEY", "JGPV-1007") # Pre-created Epic for DQ Change Requests
CHANGE_REQUEST_PROJECT_KEY = os.getenv("CHANGE_REQUEST_PROJECT_KEY", "JGPV")
CHANGE_REQUEST_COMPONENT_NAME = os.getenv("CHANGE_REQUEST_COMPONENT_NAME", "DQ Change Requests") #Pre-created
DEFAULT_TASK_TYPE_ID = str(os.getenv("JIRA_TASK_TYPE_ID", "3"))  # Default to "Task" issue type ID
JIRA_API_TOKEN = os.getenv("X_ATLASSIAN_JIRA_PERSONAL_TOKEN") # JIRA_API_TOKEN
JIRA_BASE = os.getenv("X_ATLASSIAN_JIRA_URL", "https://jira.jnj.com")
SESSION = requests.Session()
SESSION.headers.update(
    {
        "Authorization": f"Bearer {JIRA_API_TOKEN}",
        "Content-Type": "application/json",
    }
)

_JIRA_RETRY_ATTEMPTS = 5
_JIRA_RETRY_BASE_DELAY = 10.0  # seconds
# 429 = rate limit, 5xx = transient gateway/proxy failures in front of Jira
_JIRA_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _handle_retryable_response(response: requests.Response, attempt: int) -> None:
    """Sleep before retrying a rate-limited or transient server error, honouring Retry-After if present."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            delay = _JIRA_RETRY_BASE_DELAY * (2 ** attempt)
    else:
        delay = _JIRA_RETRY_BASE_DELAY * (2 ** attempt)
    logger.warning(
        "Jira returned %s. Retrying in %.1fs (attempt %d/%d)...",
        response.status_code,
        delay,
        attempt + 1,
        _JIRA_RETRY_ATTEMPTS,
    )
    time.sleep(delay)


def _post(url: str, payload: Dict[str, Any]) -> Any:
    for attempt in range(_JIRA_RETRY_ATTEMPTS):
        response = SESSION.post(
            f"{JIRA_BASE}{url}",
            data=json.dumps(payload),
            timeout=60,
            verify=False,  # Disable SSL verification for Jira API requests
        )
        if response.status_code in _JIRA_RETRY_STATUSES and attempt < _JIRA_RETRY_ATTEMPTS - 1:
            _handle_retryable_response(response, attempt)
            continue
        if response.status_code >= 400:
            logger.error("Jira error: %s %s", response.status_code, response.text[:1000])
        response.raise_for_status()
        if response.text and response.text.strip():
            return response.json()
        return None
    return None  # unreachable but satisfies type checkers


def _get(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    for attempt in range(_JIRA_RETRY_ATTEMPTS):
        response = SESSION.get(
            f"{JIRA_BASE}{url}",
            params=params,
            timeout=30,
            verify=False,
        )
        if response.status_code in _JIRA_RETRY_STATUSES and attempt < _JIRA_RETRY_ATTEMPTS - 1:
            _handle_retryable_response(response, attempt)
            continue
        if response.status_code >= 400:
            logger.error("Jira error: %s %s", response.status_code, response.text[:1000])
        response.raise_for_status()
        if response.text and response.text.strip():
            return response.json()
        return None
    return None


def _put(url: str, payload: Dict[str, Any]) -> Any:
    for attempt in range(_JIRA_RETRY_ATTEMPTS):
        response = SESSION.put(
            f"{JIRA_BASE}{url}",
            data=json.dumps(payload),
            timeout=60,
            verify=False,  # Disable SSL verification for Jira API requests
        )
        if response.status_code in _JIRA_RETRY_STATUSES and attempt < _JIRA_RETRY_ATTEMPTS - 1:
            _handle_retryable_response(response, attempt)
            continue
        if response.status_code >= 400:
            logger.error("Jira error: %s %s", response.status_code, response.text[:1000])
        response.raise_for_status()
        if response.text and response.text.strip():
            return response.json()
        return None
    return None


def _search_jql(jql: str, fields: Optional[Union[List[str], str]] = None, max_results: int = 50) -> Any:
    if fields is None:
        fields_list: Union[str, List[str]] = ["summary", "key"]
    elif isinstance(fields, str):
        if fields.strip() in ("*all", "*navigable"):
            fields_list = fields.strip()
        else:
            fields_list = [field.strip() for field in fields.split(",") if field.strip()]
    else:
        fields_list = list(fields)

    payload = {"jql": jql, "maxResults": max_results, "fields": fields_list}
    return _post("/rest/api/2/search", payload)


# ---------- Field discovery ----------
def get_epic_link_customfield_id() -> Optional[str]:
    """Return the custom field ID for 'Epic Link', or None if not found.
    This is needed because the custom field ID can vary between Jira instances.
    for JGPV, it is customfield_10006, but we should not hardcode that.
    """
    data = _get("/rest/api/2/field")
    for field in data:
        if field.get("name") == "Epic Link" and field.get("schema", {}).get("custom"):
            return field.get("id")
    return None


def get_versions_map(project_key: str) -> Dict[str, str]:
    versions = _get(f"/rest/api/2/project/{project_key}/versions")
    return {version["name"]: version["id"] for version in versions if not version.get("archived", False)}


def get_components_map(project_key: str) -> Dict[str, str]:
    components = _get(f"/rest/api/2/project/{project_key}/components")
    return {component["name"]: component["id"] for component in components}


# ---------- Parent Task ----------
def _sanitize_for_text_query(phrase: str) -> str:
    return re.sub(r"[+\-&|!(){}\[\]^~*?\\:]", " ", phrase).strip()


def build_labels_for_task() -> List[str]:
    return ["dq_chatbot", "dq_change_request"]


def find_epic_by_exact_name(project_key: str, epic_name: str) -> Optional[str]:
    epic_name_clean = epic_name.replace('"', '\\"')
    jql = (
        f'project = "{project_key}" AND issuetype = Epic '
        f'AND "Epic Name" = "{epic_name_clean}" '
        f"ORDER BY updated DESC"
    )
    response = _search_jql(jql, fields=["key", "summary"], max_results=3)
    issues = response.get("issues", [])
    return issues[0]["key"] if issues else None


# ---------- Reopen and update ----------
def is_issue_already_open(issue_key: str) -> bool:
    issue = _get(f"/rest/api/2/issue/{issue_key}?fields=status")
    status = str(issue["fields"]["status"]["name"]).lower()
    return status in {"open", "reopened", "to do", "in progress"}


def reopen_issue(issue_key: str, transition_id: str) -> None:
    payload = {"transition": {"id": str(transition_id)}}
    _post(f"/rest/api/2/issue/{issue_key}/transitions", payload)


def add_comment(issue_key: str, body: str) -> None:
    payload = {"body": body}
    _post(f"/rest/api/2/issue/{issue_key}/comment", payload)


def update_description(issue_key: str, description: str) -> None:
    payload = {"fields": {"description": description}}
    _put(f"/rest/api/2/issue/{issue_key}", payload)


def get_reopen_transition_id(issue_key: str) -> Optional[str]:
    response = _get(f"/rest/api/2/issue/{issue_key}/transitions")
    for transition in response.get("transitions", []):
        name = str(transition["name"]).lower()
        target = str(transition["to"]["name"]).lower()
        if "reopen" in name or target == "open":
            return transition["id"]
    return None


def reopen_and_update_description(issue_key: str, new_description_text: str) -> None:
    '''
    reopen jira issue if it is closed and 
    update with new description text. 
    if the issue is already open, just update the description.
    return the subtask status if it was reopened, otherwise None
    '''
    status = None
    if is_issue_already_open(issue_key):
        update_description(issue_key, new_description_text)
        logger.info("Updated description for issue %s", issue_key)
        return status

    reopen_transition_id = get_reopen_transition_id(issue_key)
    if not reopen_transition_id:
        logger.warning("No reopen transition available for %s", issue_key)
        return status

    reopen_issue(issue_key, reopen_transition_id)
    logger.info("Reopened issue %s", issue_key)
    update_description(issue_key, new_description_text)
    logger.info("Updated description for issue %s", issue_key)
    return "reopened"


def jira_get_transitions(issue_key: str) -> List[Dict[str, Any]]:
    response = SESSION.get(
        f"{JIRA_BASE}/rest/api/2/issue/{issue_key}/transitions",
        timeout=30,
        verify=False,  # Disable SSL verification for Jira API requests
    )
    if response.status_code >= 400:
        raise requests.HTTPError(f"Jira transitions {issue_key} failed {response.status_code}: {response.text[:500]}")
    return response.json().get("transitions", [])


def jira_transition(issue_key: str, transition_id: str) -> None:
    response = SESSION.post(
        f"{JIRA_BASE}/rest/api/2/issue/{issue_key}/transitions",
        data=json.dumps({"transition": {"id": str(transition_id)}}),
        timeout=30,
        verify=False,  # Disable SSL verification for Jira API requests
    )
    if response.status_code >= 400:
        raise requests.HTTPError(f"Jira transition {issue_key} failed {response.status_code}: {response.text[:500]}")


def find_transition_id(
    transitions: List[Dict[str, Any]],
    *,
    to_status: Optional[str] = None,
    name_contains: Optional[str] = None,
) -> Optional[str]:
    to_status_lower = (to_status or "").strip().lower()
    name_contains_lower = (name_contains or "").strip().lower()

    for transition in transitions or []:
        t_name = str(transition.get("name", "")).strip().lower()
        t_to = str((transition.get("to") or {}).get("name", "")).strip().lower()

        if to_status_lower and t_to == to_status_lower:
            return transition.get("id")
        if name_contains_lower and name_contains_lower in t_name:
            return transition.get("id")
    return None


def get_status_name(issue_json: Dict[str, Any]) -> str:
    try:
        return str(((issue_json.get("fields") or {}).get("status") or {}).get("name") or "")
    except Exception:
        return ""


def jira_get_issue(issue_key: str, fields: str = "summary,status,description,comment") -> Dict[str, Any]:
    response = SESSION.get(
        f"{JIRA_BASE}/rest/api/2/issue/{issue_key}",
        params={"fields": fields},
        timeout=30,
        verify=False,  # Disable SSL verification for Jira API requests
    )
    if response.status_code >= 400:
        raise requests.HTTPError(f"Jira GET {issue_key} failed {response.status_code}: {response.text[:500]}")
    return response.json()


def close_issue_with_path(issue_key: str) -> Tuple[bool, str]:
    issue = jira_get_issue(issue_key, fields="status")
    status_name = get_status_name(issue)

    if status_name.lower().strip() != "in progress":
        transitions = jira_get_transitions(issue_key)
        transition_id = (
            find_transition_id(transitions, to_status="In Progress")
            or find_transition_id(transitions, name_contains="start progress")
            or find_transition_id(transitions, name_contains="start")
        )
        if transition_id:
            jira_transition(issue_key, transition_id)
        else:
            return False, f"No transition found to move into In Progress from '{status_name}'."

    transitions = jira_get_transitions(issue_key)
    for target in ["Completed", "Done", "Closed", "Resolved"]:
        transition_id = find_transition_id(transitions, to_status=target)
        if transition_id:
            jira_transition(issue_key, transition_id)
            return True, f"Transitioned to {target}."

    for token in ["complete", "done", "close", "resolve"]:
        transition_id = find_transition_id(transitions, name_contains=token)
        if transition_id:
            jira_transition(issue_key, transition_id)
            return True, f"Transitioned via '{token}'."

    return False, "No close/complete transition available after moving to In Progress."


def merge_description_with_investigation(original_description: str, new_description: str) -> str:
    description = original_description or ""
    marker = "----------------------------------------------------------------------"

    if marker in description:
        description = description.split(marker, 1)[0].rstrip()

    if description and not description.startswith("\n"):
        description = "\n\n" + description.strip()

    return  new_description + description


def find_change_task_key(project_key: str, dataset_name: str) -> Optional[str]:
    safe_phrase = _sanitize_for_text_query(dataset_name)
    jql = (
        f'project = "{project_key}" AND issuetype = Task '
        f'AND summary ~ "DQ Change Request: {safe_phrase}" ORDER BY created DESC'
    )

    result = _get("/rest/api/2/search", params={"jql": jql, "maxResults": 20, "fields": "summary,key"})

    for issue in (result or {}).get("issues", []):
        if issue.get("fields", {}).get("summary") == f"DQ Change Request: {dataset_name}":
            return issue.get("key")
    return None


def create_change_task(
    dataset_name: str,
    description: str,
    change_by: str,
    market: Optional[str] = None,
) -> str:
    fields: Dict[str, Any] = {
        "project": {"key": CHANGE_REQUEST_PROJECT_KEY},
        "issuetype": {"id": DEFAULT_TASK_TYPE_ID},
        "summary": f"DQ Change Request: {dataset_name}",
        "description": description,
        "labels": build_labels_for_task(),
        "assignee": {"name": change_by}
    }
    components_by_name = get_components_map(CHANGE_REQUEST_PROJECT_KEY)
    component_id = components_by_name.get(CHANGE_REQUEST_COMPONENT_NAME)
    if component_id:
        fields["components"] = [{"id": component_id}]

    epic_link_cf = get_epic_link_customfield_id()
    if epic_link_cf:
        fields[epic_link_cf] = CHANGE_REQUEST_EPIC_KEY

    
    if not market:
        market = business_unit.infer_market(dataset_name)

    versions_by_name = get_versions_map(CHANGE_REQUEST_PROJECT_KEY)
    version_id = versions_by_name.get(market) if market else None
    if version_id:
        fields["versions"] = [{"id": version_id}]
        fields["fixVersions"] = [{"id": version_id}]

    payload = {"fields": fields}
    response = _post("/rest/api/2/issue", payload)
    return response["key"]


def log_jira_change_request(
    dataset_name: str,
    description: str,
    change_by: str,
    market: Optional[str] = None,
) -> str:
    # check if a Jira task already exists for this dataset
    existing_task_key = find_change_task_key(CHANGE_REQUEST_PROJECT_KEY, dataset_name)
    if existing_task_key:
        logger.info("Found existing Jira task for dataset '%s': %s", dataset_name, existing_task_key)
        status = reopen_and_update_description(existing_task_key, description)
        return existing_task_key

    new_task_key = create_change_task(dataset_name, description, change_by, market=market)
    logger.info("Created new Jira task for dataset '%s': %s", dataset_name, new_task_key)
    return new_task_key
