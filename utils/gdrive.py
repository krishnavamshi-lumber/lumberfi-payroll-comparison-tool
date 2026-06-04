"""
Google Drive helpers for the comparison tool.
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2 import credentials as oauth2_credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CREDENTIALS_DIR = os.path.join(BASE_DIR, "credentials")
OAUTH_CLIENT_PATH = os.path.join(CREDENTIALS_DIR, "gdrive_credentials.json")
OAUTH_TOKEN_PATH = os.path.join(CREDENTIALS_DIR, "gdrive_token.json")
SERVICE_ACCOUNT_PATH = os.path.join(CREDENTIALS_DIR, "service_account.json")


def _escape_query_value(value: str) -> str:
    return value.replace("'", "\\'")


def create_drive_service(credentials_json_bytes: bytes | None = None):
    if credentials_json_bytes is not None:
        return _create_service_account_service(credentials_json_bytes)

    if os.path.exists(SERVICE_ACCOUNT_PATH):
        with open(SERVICE_ACCOUNT_PATH, "rb") as fh:
            return _create_service_account_service(fh.read())

    return _create_oauth_service()


def _create_service_account_service(credentials_json_bytes: bytes):
    credentials_data = json.loads(credentials_json_bytes.decode("utf-8"))
    if credentials_data.get("type") != "service_account":
        raise ValueError(
            "Stored credentials are not a Google service account JSON. "
            "Please place a valid service account JSON in the credentials folder."
        )

    credentials = service_account.Credentials.from_service_account_info(
        credentials_data,
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _create_oauth_service():
    if not os.path.exists(OAUTH_TOKEN_PATH):
        raise FileNotFoundError(
            f"OAuth token not found at {OAUTH_TOKEN_PATH}. "
            "Place your gdrive_token.json in the credentials folder."
        )

    with open(OAUTH_TOKEN_PATH, "r", encoding="utf-8") as fh:
        token_data = json.load(fh)

    if not token_data.get("client_id") or not token_data.get("client_secret"):
        if os.path.exists(OAUTH_CLIENT_PATH):
            with open(OAUTH_CLIENT_PATH, "r", encoding="utf-8") as client_fh:
                client_data = json.load(client_fh)
                installed = client_data.get("installed", {})
                token_data["client_id"] = token_data.get("client_id") or installed.get("client_id")
                token_data["client_secret"] = token_data.get("client_secret") or installed.get("client_secret")

    if not token_data.get("client_id") or not token_data.get("client_secret"):
        raise ValueError(
            "OAuth credentials are incomplete. Ensure gdrive_token.json contains client_id and client_secret, "
            "or place a suitable gdrive_credentials.json in the credentials folder."
        )

    credentials = oauth2_credentials.Credentials.from_authorized_user_info(
        token_data,
        scopes=SCOPES,
    )

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if credentials.expired:
        raise ValueError(
            "OAuth token is expired and could not be refreshed. "
            "Please regenerate gdrive_token.json or provide a valid refresh token."
        )

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _list_files(service: Any, query: str) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType)",
                pageToken=page_token,
                pageSize=1000,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def find_folder_by_name(service: Any, name: str, parent_id: str | None = None) -> str:
    escaped_name = _escape_query_value(name)
    query = (
        f"name = '{escaped_name}' and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"
    else:
        query += " and 'root' in parents"

    results = _list_files(service, query)
    if not results:
        parent_text = f" under parent {parent_id}" if parent_id else " in the Drive root"
        raise FileNotFoundError(f"Could not find folder '{name}'{parent_text}.")
    return results[0]["id"]


def get_or_create_folder(service: Any, name: str, parent_id: str) -> str:
    escaped_name = _escape_query_value(name)
    query = (
        f"name = '{escaped_name}' and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false "
        f"and '{parent_id}' in parents"
    )
    results = _list_files(service, query)
    if results:
        return results[0]["id"]

    metadata = {
        "name": name,
        "parents": [parent_id],
        "mimeType": "application/vnd.google-apps.folder",
    }
    created = service.files().create(body=metadata, fields="id").execute()
    return created["id"]


def list_files_in_folder(service: Any, folder_id: str) -> list[dict[str, str]]:
    query = (
        f"'{folder_id}' in parents and trashed = false "
        "and mimeType != 'application/vnd.google-apps.folder'"
    )
    return _list_files(service, query)


def list_folders_in_folder(service: Any, folder_id: str) -> list[dict[str, str]]:
    query = (
        f"'{folder_id}' in parents and trashed = false "
        "and mimeType = 'application/vnd.google-apps.folder'"
    )
    return _list_files(service, query)


def get_companies_list(service: Any) -> list[str]:
    prod_reports_id = find_folder_by_name(service, 'Prod_reports')
    query = f"'{prod_reports_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    folders = _list_files(service, query)
    return [f['name'] for f in folders]


def get_periods_list(service: Any, company_name: str, report_type: str) -> list[str]:
    prod_reports_id = find_folder_by_name(service, 'Prod_reports')
    company_id = find_folder_by_name(service, company_name, parent_id=prod_reports_id)
    report_folder_id = find_folder_by_name(service, report_type, parent_id=company_id)
    query = (
        f"'{report_folder_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    folders = _list_files(service, query)
    return [f['name'] for f in folders if f['name'] != 'difference']


def download_file(service: Any, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def upload_or_update_file(
    service: Any,
    name: str,
    mime_type: str,
    parent_folder_id: str,
    file_bytes: bytes,
) -> str:
    escaped_name = _escape_query_value(name)
    query = (
        f"name = '{escaped_name}' and trashed = false "
        f"and '{parent_folder_id}' in parents"
    )
    existing = _list_files(service, query)
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
    if existing:
        file_id = existing[0]["id"]
        service.files().update(fileId=file_id, media_body=media).execute()
        return file_id

    metadata = {"name": name, "parents": [parent_folder_id]}
    created = service.files().create(body=metadata, media_body=media, fields="id").execute()
    return created["id"]
