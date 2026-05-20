"""Jenkins integration — trigger PR pipeline builds."""

import base64
import urllib.parse
import requests
import config

_cfg = config.read_config()
_JENKINS_URL = "https://scuti.lab.tail-f.com:8443"
_USERNAME = _cfg.get("username", "").strip()
_JENKINS_TOKEN = _cfg.get("jenkins-token", "").strip()

# Derive the Jenkins username from the GitHub username
_JENKINS_USER = _USERNAME.removesuffix("_cisco")
if _JENKINS_USER.endswith("-gen"):
    _JENKINS_USER = _JENKINS_USER.replace("-gen", ".gen")


def is_configured():
    """Return True if Jenkins integration is fully configured."""
    return bool(_JENKINS_TOKEN and _JENKINS_USER)


def _get_auth_header():
    """Build HTTP Basic Auth header for Jenkins."""
    credentials = f"{_JENKINS_USER}:{_JENKINS_TOKEN}"
    auth = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {auth}", "X-Atlassian-Token": "no-check"}


def _get_jenkins_crumb(session):
    """Fetch a Jenkins crumb (CSRF token) and return updated headers."""
    parsed = urllib.parse.urlparse(_JENKINS_URL)
    crumb_url = urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, "/crumbIssuer/api/json", "", "", "")
    )
    headers = _get_auth_header()
    resp = session.get(crumb_url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    headers[data["crumbRequestField"]] = data["crumb"]
    return headers


def start_test(pr_number, repo, head_ref, base_ref, pr_url=None):
    """Trigger a Jenkins PR pipeline for the given PR.

    Args:
        pr_number: The PR number (int or str).
        repo: The GitHub repo slug (e.g. "org/repo").
        head_ref: Source branch name.
        base_ref: Target/base branch name.
        pr_url: Full GitHub PR URL (optional, constructed if missing).

    Returns:
        (success: bool, message: str)
    """
    if not is_configured():
        return False, "Jenkins not configured (set jenkins-token in config)"

    if pr_url is None:
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"

    job_name = f"jjb_pr_start_linux-64_{base_ref}"
    start_job_url = f"{_JENKINS_URL}/job/{job_name}/buildWithParameters"

    params = {
        "FROM_REF": head_ref,
        "STASH_BUTTON_TRIGGER_TITLE": "PRTUI",
        "STASH_PULL_REQUEST_USER_NAME": _USERNAME,
        "PIPE_DRYRUN": "NO",
        "TAILF_ENV_VARS": "",
        "STASH_PULL_REQUEST_ID": str(pr_number),
        "STASH_PULL_REQUEST_URL": pr_url,
    }

    try:
        session = requests.Session()
        headers = _get_jenkins_crumb(session)
        encoded_params = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        resp = session.post(start_job_url, headers=headers, params=encoded_params, timeout=30)

        if resp.status_code == 201:
            return True, f"Job {job_name} scheduled"
        else:
            return False, f"Jenkins returned {resp.status_code}: {resp.text[:200]}"
    except requests.exceptions.ConnectionError:
        return False, "Connection failed — check VPN connection"
    except requests.exceptions.HTTPError as e:
        return False, f"HTTP error: {e}"
    except Exception as e:
        return False, f"Failed: {e}"
