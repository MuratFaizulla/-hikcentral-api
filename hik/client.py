"""
HikCentral Professional клиент.
Делает аутентифицированные запросы к ISAPI/Bumblebee с правильными токенами.
"""
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from hik.crypto import create_append_info, cryptojs_rc4drop_decrypt


SESSION_FILE = Path(__file__).parent.parent / "session.json"


class HikClient:
    """
    Клиент держит SID и AES-ключ. Каждый запрос:
      - инкрементирует tokenKeyNum
      - генерит appendinfo
      - кладёт SID в query, appendinfo в заголовок
    """

    def __init__(
        self,
        base_url: str,
        sid: str,
        aes_key_hex: str,
        token_key_num: int = 11,
    ):
        self.base_url = base_url.rstrip("/")
        self.sid = sid
        self.aes_key_hex = aes_key_hex
        self._tkn = token_key_num
        self._lock = threading.Lock()
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=30.0,
            headers={
                "Accept": "application/xml, text/xml, */*;",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )

    # ---------------------- session save/load ----------------------

    @classmethod
    def from_session_file(cls, path: Path = SESSION_FILE) -> "HikClient":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            base_url=data["base_url"],
            sid=data["sid"],
            aes_key_hex=data["aes_key_hex"],
            token_key_num=int(data.get("token_key_num", 11)),
        )

    @staticmethod
    def load_saved_creds(path: Path = SESSION_FILE) -> dict:
        """Прочитать сохранённые credentials (username/password) для авто‑релогина."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                k: data[k]
                for k in ("username", "base_url")
                if data.get(k)
            }
        except Exception:
            return {}

    def save_session(self, path: Path = SESSION_FILE, creds: Optional[dict] = None) -> None:
        existing = {}
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        payload = {
            "base_url": self.base_url,
            "sid": self.sid,
            "aes_key_hex": self.aes_key_hex,
            "token_key_num": self._tkn,
        }
        if creds and creds.get("username"):
            payload["username"] = creds["username"]
        elif existing.get("username"):
            payload["username"] = existing["username"]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ---------------------- low-level: token + request ----------------------

    def _next_token(self) -> tuple[int, str]:
        """Возвращает (использованный tokenKeyNum, base64 токен) и инкрементирует счётчик."""
        with self._lock:
            n = self._tkn
            self._tkn += 1
        token = create_append_info(n, self.aes_key_hex)
        return n, token

    def request(
        self,
        path: str,
        mt: str = "GET",
        body: Any = None,
        extra_params: Optional[dict] = None,
    ) -> httpx.Response:
        """
        Физически всегда POST. Параметр MT даёт логический метод (GET/POST/PUT/DELETE).
        body — dict / list, будет JSON-сериализован (HikCentral ожидает JSON в теле).
        """
        _, append_info = self._next_token()
        params = {"SID": self.sid, "MT": mt}
        if extra_params:
            params.update(extra_params)

        if body is None:
            body_str = ""
        elif isinstance(body, (dict, list)):
            body_str = json.dumps(body, ensure_ascii=False)
        else:
            body_str = str(body)

        headers = {
            "Timeout": "120000",
            "AppendInfo": append_info,
        }
        return self._http.post(
            "/" + path.lstrip("/"),
            params=params,
            content=body_str.encode("utf-8"),
            headers=headers,
        )

    def request_json(self, path: str, mt: str = "GET", body: Any = None) -> dict:
        r = self.request(path, mt=mt, body=body)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text, "_status": r.status_code}

    def get_picture(self, vsm_url: str) -> bytes:
        """Снимок лица из события доступа (Storage/Picture с Vsm:// URL)."""
        _, append_info = self._next_token()
        r = self._http.get(
            "/ISAPI/Bumblebee/Platform/V0/Storage/Picture",
            params={"SID": self.sid, "URL": vsm_url, "Token": append_info},
        )
        r.raise_for_status()
        return r.content

    def get_photo(self, person_id: int | str, photo_type: int = 0) -> bytes:
        """Фото человека приходит как JPEG. Token идёт в query, не в заголовок."""
        _, append_info = self._next_token()
        params = {
            "SID": self.sid,
            "PHOTOTYPE": photo_type,
            "time": int(time.time() * 1000),
            "Token": append_info,
        }
        r = self._http.get(
            f"/ISAPI/Bumblebee/Platform/V0/PersonCredential/Persons/{person_id}/Photo",
            params=params,
        )
        r.raise_for_status()
        return r.content

    # ---------------------- high-level API ----------------------

    def keep_alive(self) -> dict:
        return self.request_json("ISAPI/Bumblebee/Platform/V0/KeepLive", mt="GET")

    def list_persons(self, page: int = 1, page_size: int = 50, search: str = "") -> dict:
        body = {
            "PersonListRequest": {
                "PageIndex": page,
                "PageSize": page_size,
                "SearchKey": search,
            }
        }
        return self.request_json(
            "ISAPI/Bumblebee/Platform/V1/PersonCredential/Persons", mt="GET", body=body
        )

    def get_person(self, person_id: int | str) -> dict:
        return self.request_json(
            f"ISAPI/Bumblebee/Platform/V1/PersonCredential/Persons/{person_id}",
            mt="GET",
        )

    def person_groups(self) -> dict:
        _, append_info = self._next_token()
        params = {"SID": self.sid, "MT": "GET"}
        headers = {"AppendInfo": append_info, "Timeout": "30000"}
        r = self._http.get(
            "/ISAPI/Bumblebee/Platform/V0/PersonCredential/PersonGroups",
            params=params,
            headers=headers,
        )
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text}

    def card_swipe_records(
        self,
        page: int = 1,
        page_size: int = 50,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        person_id: Optional[int] = None,
        person_name: Optional[str] = None,
        element_ids: str = "",
    ) -> dict:
        criteria: dict = {
            "Type": 0,
            "SearchType": 0,
            "ElementIDs": element_ids,
            "EventTypes": "",
        }
        if start_time:
            criteria["BeginTime"] = start_time
        if end_time:
            criteria["EndTime"] = end_time
        if person_id:
            criteria["PersonIDs"] = str(person_id)
        if person_name:
            criteria["PersonName"] = person_name
        body = {
            "CardSwipeRecordsRequest": {
                "PageIndex": page,
                "PageSize": page_size,
                "SearchCriteria": criteria,
            }
        }
        return self.request_json(
            "ISAPI/Bumblebee/ACSPlugin/V0/Record/CardSwipeRecords",
            mt="GET",
            body=body,
        )

    def logical_elements(self) -> dict:
        return self.request_json(
            "ISAPI/Bumblebee/DeviceResource/V1/LogicalResource/Elements",
            mt="GET",
            body={"ElementListRequest": {"PageIndex": 1, "PageSize": -1}},
        )

    def video_elements(self) -> dict:
        return self.request_json(
            "ISAPI/Bumblebee/DeviceResource/V1/LogicalResource/Elements",
            mt="GET",
            body={
                "ElementsRequest": {
                    "SiteID": 0,
                    "AreaID": -1,
                    "Types": "1002,1005",
                    "DepthTraversal": 1,
                    "PageIndex": -1,
                    "SearchCriteria": {"NameFilterCondition": "1"},
                    "Field": "DeviceInfo,RelatedElement,AbilityExtend",
                }
            },
        )

    def logical_areas(self) -> dict:
        return self.request_json(
            "ISAPI/Bumblebee/DeviceResource/V1/LogicalResource/Areas",
            mt="GET",
            body={"AreaListRequest": {"PageIndex": 1, "PageSize": -1}},
        )

    def acs_event_types(self) -> dict:
        return self.request_json(
            "ISAPI/Bumblebee/ACSPlugin/V1/ACSPluginEventType", mt="GET"
        )

    def sites(self) -> dict:
        return self.request_json(
            "ISAPI/Bumblebee/Platform/V0/RSM/Sites",
            mt="GET",
            body={"SiteListRequest": {"PageIndex": 1, "PageSize": -1}},
        )

    def video_preview_url(self, extra_id: int = 100, site_id: int = 0) -> dict:
        return self.request_json(
            "ISAPI/Bumblebee/BaseVideo/V0/PreviewCommonUrl",
            mt="GET",
            body={"CommonUrlRequest": {
                "IDType": 3, "GetStreamWay": 4, "adaptiveNetWork": 2,
                "extraType": 2002, "extraID": extra_id, "SiteID": site_id,
            }},
        )

    def device_info(self, device_id: int | str) -> dict:
        return self.request_json(
            f"ISAPI/Bumblebee/DeviceResource/V1/PhysicalResource/Devices/{device_id}",
            mt="GET",
        )

    def capture_preview(self, channel_id: int | str) -> bytes:
        """JPEG снимок с канала через HikCentral. channel_id = ResourceID элемента."""
        _, append_info = self._next_token()
        params = {"SID": self.sid, "MT": "GET"}
        headers = {"AppendInfo": append_info, "Timeout": "30000"}
        r = self._http.post(
            "/ISAPI/Bumblebee/BaseVideo/V0/CapturePreview",
            params=params,
            content=json.dumps({"CapturePreviewRequest": {"ChannelID": channel_id, "SnapNum": 1}}).encode(),
            headers=headers,
        )
        r.raise_for_status()
        return r.content

    def list_devices(self) -> dict:
        return self.request_json(
            "ISAPI/Bumblebee/DeviceResource/V1/PhysicalResource/Devices",
            mt="GET",
            body={"DeviceListRequest": {"PageIndex": 1, "PageSize": 500}},
        )

    def device_info_v2(self, device_id: int | str) -> dict:
        """Детали физического устройства — POST с пустым телом (как делает браузер)."""
        _, append_info = self._next_token()
        params = {"SID": self.sid, "MT": "GET"}
        headers = {"AppendInfo": append_info, "Timeout": "120000"}
        r = self._http.post(
            f"/ISAPI/Bumblebee/DeviceResource/V1/PhysicalResource/Devices/{device_id}",
            params=params,
            content=b"",
            headers=headers,
        )
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text}

    def card_swipe_records_count(
        self, start_time: Optional[str] = None, end_time: Optional[str] = None
    ) -> dict:
        criteria: dict = {}
        if start_time:
            criteria["BeginTime"] = start_time
        if end_time:
            criteria["EndTime"] = end_time
        body = {
            "CardSwipeRecordsRequest": {
                "PageIndex": 1,
                "PageSize": 1,
                "SearchCriteria": {
                    "Type": 0,
                    "SearchType": 0,
                    "ElementIDs": "",
                    "EventTypes": "",
                    **criteria,
                },
            }
        }
        return self.request_json(
            "ISAPI/Bumblebee/ACSPlugin/V0/Record/CardSwipeRecords",
            mt="GET",
            body=body,
        )


# ---------------------- helper: загрузить ключ + sid из захвата браузера ----------------------

def build_client_from_browser_capture(
    base_url: str,
    sid: str,
    encrypted_aes_b64: str,
    hostname: str,
    token_key_num: int = 11,
) -> HikClient:
    """
    Удобный конструктор когда взяли SID + system_session_token из localStorage браузера.
    Если encrypted_aes_b64 начинается с '__hex__:' — это уже готовый AES hex (не RC4Drop).
    """
    if encrypted_aes_b64.startswith("__hex__:"):
        aes_key = encrypted_aes_b64[8:]
    else:
        aes_key = cryptojs_rc4drop_decrypt(encrypted_aes_b64, hostname)
    return HikClient(base_url=base_url, sid=sid, aes_key_hex=aes_key, token_key_num=token_key_num)
