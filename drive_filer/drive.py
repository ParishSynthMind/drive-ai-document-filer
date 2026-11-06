"""The Drive API v3 calls the filer needs, and nothing else.

There is deliberately no delete method. The filer only reads, renames, moves
and labels files (and creates folders), so nothing it does can lose a file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

from .http import GoogleHttp

API = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
FILE_FIELDS = "id,name,mimeType,parents,md5Checksum,size,modifiedTime,webViewLink,description,appProperties,trashed"

# Google-native files have no bytes to download; they are exported instead.
EXPORTS = {
    GOOGLE_DOC: ("text/plain", ".txt"),
    GOOGLE_SHEET: ("text/csv", ".csv"),
}


def q_escape(value: str) -> str:
    """Escape a value for a Drive search query (q=...)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    parents: list[str] = field(default_factory=list)
    md5: Optional[str] = None
    size: Optional[int] = None
    modified: str = ""
    link: str = ""
    description: str = ""
    app_properties: dict = field(default_factory=dict)
    trashed: bool = False

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER

    @property
    def is_google_native(self) -> bool:
        return self.mime_type.startswith("application/vnd.google-apps.")

    @classmethod
    def from_api(cls, d: dict) -> "DriveFile":
        return cls(
            id=d["id"], name=d.get("name", ""), mime_type=d.get("mimeType", ""),
            parents=list(d.get("parents") or []), md5=d.get("md5Checksum"),
            size=int(d["size"]) if d.get("size") else None, modified=d.get("modifiedTime", ""),
            link=d.get("webViewLink", ""), description=d.get("description", "") or "",
            app_properties=dict(d.get("appProperties") or {}), trashed=bool(d.get("trashed", False)),
        )


class Drive:
    def __init__(self, http: GoogleHttp):
        self.http = http

    # --- reading ---------------------------------------------------------
    def get(self, file_id: str) -> DriveFile:
        r = self.http.request("GET", f"{API}/files/{file_id}",
                              params={"fields": FILE_FIELDS, "supportsAllDrives": "true"})
        return DriveFile.from_api(r.json())

    def search(self, q: str, page_size: int = 100) -> Iterator[DriveFile]:
        token = None
        while True:
            params = {"q": q, "pageSize": page_size, "fields": f"nextPageToken,files({FILE_FIELDS})",
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            data = self.http.request("GET", f"{API}/files", params=params).json()
            for f in data.get("files", []):
                yield DriveFile.from_api(f)
            token = data.get("nextPageToken")
            if not token:
                return

    def children(self, folder_id: str, folders: Optional[bool] = None) -> Iterator[DriveFile]:
        q = f"'{q_escape(folder_id)}' in parents and trashed = false"
        if folders is True:
            q += f" and mimeType = '{FOLDER}'"
        elif folders is False:
            q += f" and mimeType != '{FOLDER}'"
        return self.search(q)

    def find_by_app_properties(self, props: dict, extra: str = "") -> Iterator[DriveFile]:
        parts = [f"appProperties has {{ key='{q_escape(k)}' and value='{q_escape(str(v))}' }}" for k, v in props.items()]
        parts.append("trashed = false")
        if extra:
            parts.append(extra)
        return self.search(" and ".join(parts))

    def download(self, f: DriveFile) -> tuple[bytes, str]:
        """(bytes, extension to treat them as). Google Docs/Sheets are exported as text/CSV."""
        if f.mime_type in EXPORTS:
            mime, ext = EXPORTS[f.mime_type]
            r = self.http.request("GET", f"{API}/files/{f.id}/export", params={"mimeType": mime})
            return r.content, ext
        if f.is_google_native:
            raise ValueError(f"{f.name}: Google file type {f.mime_type} cannot be exported for reading")
        r = self.http.request("GET", f"{API}/files/{f.id}", params={"alt": "media", "supportsAllDrives": "true"})
        dot = f.name.rfind(".")
        return r.content, f.name[dot:].lower() if dot > 0 else ""

    # --- changes feed (cheap way to notice new files) --------------------
    def start_page_token(self) -> str:
        r = self.http.request("GET", f"{API}/changes/startPageToken", params={"supportsAllDrives": "true"})
        return r.json()["startPageToken"]

    def changes(self, page_token: str) -> tuple[list[dict], str]:
        """All changes since page_token, and the token to use next time."""
        out = []
        while True:
            params = {"pageToken": page_token, "pageSize": 100, "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true",
                      "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))"}
            data = self.http.request("GET", f"{API}/changes", params=params).json()
            out.extend(data.get("changes", []))
            if data.get("newStartPageToken"):
                return out, data["newStartPageToken"]
            page_token = data["nextPageToken"]

    # --- writing (rename, move, label, make folders) ---------------------
    def create_folder(self, name: str, parent_id: str) -> DriveFile:
        body = {"name": name, "mimeType": FOLDER, "parents": [parent_id]}
        r = self.http.request("POST", f"{API}/files", params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
                              json=body)
        return DriveFile.from_api(r.json())

    def update(self, file_id: str, *, name: Optional[str] = None, description: Optional[str] = None,
               app_properties: Optional[dict] = None, add_parents: Optional[list[str]] = None,
               remove_parents: Optional[list[str]] = None) -> DriveFile:
        """One PATCH that renames, moves and labels together, so a file is never half-filed.
        In app_properties a value of None removes that key."""
        params = {"fields": FILE_FIELDS, "supportsAllDrives": "true"}
        if add_parents:
            params["addParents"] = ",".join(add_parents)
        if remove_parents:
            params["removeParents"] = ",".join(remove_parents)
        body: dict = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if app_properties is not None:
            body["appProperties"] = app_properties
        r = self.http.request("PATCH", f"{API}/files/{file_id}", params=params, json=body)
        return DriveFile.from_api(r.json())
