#!/usr/bin/env python3
"""
jules_client.py — thin wrapper around the Jules REST API
(https://jules.googleapis.com/v1alpha), used by bridge_jules.py.

Kept dependency-free (stdlib urllib only) so it doesn't add a requirement
beyond what bridge.py already needs (google-genai).

Reference: https://developers.google.com/jules/api/reference/rest
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

API_ROOT = "https://jules.googleapis.com/v1alpha"

TERMINAL_STATES = {"COMPLETED", "FAILED"}
AWAITING_STATES = {"AWAITING_USER_FEEDBACK", "AWAITING_PLAN_APPROVAL"}


class JulesAPIError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Jules API error {status}: {body}")
        self.status = status
        self.body = body


class JulesClient:
    def __init__(self, api_key: str):
        self._api_key = api_key

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = f"{API_ROOT}/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("x-goog-api-key", self._api_key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise JulesAPIError(exc.code, exc.read().decode("utf-8", errors="replace")) from exc
        return json.loads(body) if body else {}

    def list_sources(self) -> list[dict]:
        return self._request("GET", "sources").get("sources", [])

    def create_session(
        self,
        prompt: str,
        source: str,
        starting_branch: str,
        title: Optional[str] = None,
    ) -> dict:
        """source: 'sources/github/{owner}/{repo}' as returned by list_sources()."""
        payload = {
            "prompt": prompt,
            "sourceContext": {
                "source": source,
                "githubRepoContext": {"startingBranch": starting_branch},
            },
            "automationMode": "AUTO_CREATE_PR",
            "requirePlanApproval": False,
        }
        if title:
            payload["title"] = title
        return self._request("POST", "sessions", payload)

    def get_session(self, session_name: str) -> dict:
        return self._request("GET", session_name)

    def list_activities(self, session_name: str, page_token: Optional[str] = None) -> dict:
        path = f"{session_name}/activities"
        if page_token:
            path += f"?pageToken={page_token}"
        return self._request("GET", path)

    def list_all_activities(self, session_name: str) -> list[dict]:
        activities: list[dict] = []
        page_token = None
        while True:
            page = self.list_activities(session_name, page_token)
            activities.extend(page.get("activities", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return activities

    def send_message(self, session_name: str, message: str) -> dict:
        return self._request("POST", f"{session_name}:sendMessage", {"message": message})


def render_activity_transcript(activities: list[dict], limit_chars: int) -> str:
    lines = []
    for activity in activities:
        lines.append(json.dumps(activity, ensure_ascii=False))
    text = "\n".join(lines)
    if len(text) <= limit_chars:
        return text
    marker = "\n...[truncated]...\n"
    keep = limit_chars - len(marker)
    return text[: keep // 2] + marker + text[-(keep - keep // 2):]
