from __future__ import annotations

import base64
from email.message import EmailMessage

SCOPES_CALENDAR_READ = ("https://www.googleapis.com/auth/calendar.readonly",)
SCOPES_CALENDAR_WRITE = ("https://www.googleapis.com/auth/calendar",)
SCOPES_GMAIL_READ = ("https://www.googleapis.com/auth/gmail.readonly",)
SCOPES_GMAIL_LABELS_AND_MODIFY = ("https://www.googleapis.com/auth/gmail.modify",)
SCOPES_GMAIL_DRAFTS = ("https://www.googleapis.com/auth/gmail.compose",)
MINIMUM_COMBINED_SCOPES = (
    *SCOPES_CALENDAR_READ,
    *SCOPES_CALENDAR_WRITE,
    *SCOPES_GMAIL_READ,
    *SCOPES_GMAIL_LABELS_AND_MODIFY,
    *SCOPES_GMAIL_DRAFTS,
)


class GoogleWorkspaceBackend:
    """Production backend. Imports Google libraries lazily inside the broker."""

    def __init__(self, credentials):
        from googleapiclient.discovery import build

        self.calendar = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self.gmail = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def list_calendars(self, params):
        return self.calendar.calendarList().list(maxResults=params.get("max_results")).execute()

    def list_events(self, params):
        kwargs = {"calendarId": params["calendar_id"]}
        for src, dst in (("time_min", "timeMin"), ("time_max", "timeMax"), ("query", "q"), ("max_results", "maxResults")):
            if src in params:
                kwargs[dst] = params[src]
        return self.calendar.events().list(**kwargs).execute()

    def get_event(self, params):
        return self.calendar.events().get(calendarId=params["calendar_id"], eventId=params["event_id"]).execute()

    def create_calendar(self, params):
        body = {k: params[k] for k in ("summary", "description", "location") if k in params}
        return self.calendar.calendars().insert(body=body).execute()

    def create_event(self, params):
        body = _event_body(params)
        return self.calendar.events().insert(calendarId=params["calendar_id"], body=body).execute()

    def update_event(self, params):
        body = _event_body(params)
        return self.calendar.events().patch(
            calendarId=params["calendar_id"], eventId=params["event_id"], body=body,
        ).execute()

    def search_messages(self, params):
        kwargs = {"userId": "me"}
        if "query" in params:
            kwargs["q"] = params["query"]
        if "max_results" in params:
            kwargs["maxResults"] = params["max_results"]
        return self.gmail.users().messages().list(**kwargs).execute()

    def get_message(self, params):
        return self.gmail.users().messages().get(userId="me", id=params["message_id"]).execute()

    def get_thread(self, params):
        return self.gmail.users().threads().get(userId="me", id=params["thread_id"]).execute()

    def list_labels(self, params):
        return self.gmail.users().labels().list(userId="me").execute()

    def create_label(self, params):
        return self.gmail.users().labels().create(userId="me", body={"name": params["name"]}).execute()

    def update_label(self, params):
        return self.gmail.users().labels().patch(
            userId="me", id=params["label_id"], body={"name": params["name"]},
        ).execute()

    def delete_label(self, params):
        return self.gmail.users().labels().delete(userId="me", id=params["label_id"]).execute()

    def modify_message_labels(self, params):
        body = {
            "addLabelIds": params.get("add_label_ids") or [],
            "removeLabelIds": params.get("remove_label_ids") or [],
        }
        return self.gmail.users().messages().modify(userId="me", id=params["message_id"], body=body).execute()

    def create_draft(self, params):
        msg = EmailMessage()
        msg["To"] = ", ".join(params["to"])
        if params.get("cc"):
            msg["Cc"] = ", ".join(params["cc"])
        if params.get("bcc"):
            msg["Bcc"] = ", ".join(params["bcc"])
        msg["Subject"] = params["subject"]
        msg.set_content(params["body_text"])
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        return self.gmail.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()


def _event_body(params):
    body = {k: params[k] for k in ("summary", "description", "location", "start", "end") if k in params}
    if params.get("attendee_emails"):
        body["attendees"] = [{"email": email} for email in params["attendee_emails"]]
    return body
