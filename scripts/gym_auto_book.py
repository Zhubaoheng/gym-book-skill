#!/usr/bin/env python3
"""
健身房自动预约脚本

当前结论：
1. `v3/api.php/*` 仍然走 AES-128-CBC 加密。
2. 场馆 H5 接口走 form-urlencoded，但现网验证显示不应主动附带 body 里的 `sign`。
3. `getStadiumList` 需要 uid/token/card_id 等登录参数。
4. `getInterval` 也需要部分登录参数，但不需要把所有字段都塞进 body。
5. `addOrder` 请求体应尽量贴近抓包里的纯业务参数结构。
"""

import base64
import hashlib
import html
import io
import json
import os
import random
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import ddddocr
import requests
from Crypto.Cipher import AES
from PIL import Image


BASE_URL = "https://byty.bupt.edu.cn"
REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_ENV_FILE = REPO_ROOT / ".env.local"
DEFAULT_CAPTURE_FILE = "gym.chlsj"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Mac "
    "MacWechat/WMPF MacWechat/3.8.7(0x13080712) UnifiedPCMacWechat(0xf2641702) "
    "XWEB/18788 miniProgram/wxf13a8dee385f2258"
)

SIGN_SALT = "rDJiNB9j7vD2"
H5_SIGN_SALT = "Lptiyu_123!%$"
APP_KEY = "wxf13a8dee385f2258"
DEFAULT_GYM_UID = ""
DEFAULT_GYM_STUDENT_NUM = ""
DEFAULT_GYM_TOKEN = ""
UID_ENV_ALIASES = ("SESSION_A", "GYM_UID")
STUDENT_ENV_ALIASES = ("SESSION_B", "GYM_STUDENT_NUM")
TOKEN_ENV_ALIASES = ("SESSION_C", "GYM_TOKEN", "BUPT_GYM_TOKEN", "TOKEN")
AES_KEY_ENV_ALIASES = ("SESSION_D", "GYM_AES_KEY")
AES_IV_ENV_ALIASES = ("SESSION_E", "GYM_AES_IV")
ORDER_STATUS_TEXT = {
    0: "待确认",
    1: "取消预约",
    2: "已核销",
    3: "已取消",
    4: "未核销",
    5: "已拒绝",
    6: "待支付",
}


def generate_sign(params: dict, salt: str = SIGN_SALT) -> str:
    """兼容小程序源码里的 SignMD5。"""
    sorted_keys = sorted(params.keys())
    sign_str = "".join(f"{key}{params[key]}" for key in sorted_keys) + salt
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()


def generate_h5_sign(params: dict) -> str:
    """H5 页面 common.js 里的 SignMD5。"""
    return generate_sign(params, salt=H5_SIGN_SALT)


def get_aes_material(key: Optional[str] = None, iv: Optional[str] = None) -> tuple[str, str]:
    load_local_env_file()
    resolved_key = key or env_first(*AES_KEY_ENV_ALIASES)
    resolved_iv = iv or env_first(*AES_IV_ENV_ALIASES)
    if not resolved_key or not resolved_iv:
        raise ValueError("缺少本地加密配置，请设置 SESSION_D、SESSION_E")
    return resolved_key, resolved_iv


def encrypt_aes(data: str, key: Optional[str] = None, iv: Optional[str] = None) -> str:
    """AES-128-CBC + PKCS7。"""
    key, iv = get_aes_material(key, iv)
    key_bytes = key.encode("utf-8")[:16].ljust(16, b"\x00")
    iv_bytes = iv.encode("utf-8")[:16].ljust(16, b"\x00")

    data_bytes = data.encode("utf-8")
    padding_len = 16 - (len(data_bytes) % 16)
    data_bytes += bytes([padding_len] * padding_len)

    cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
    encrypted = cipher.encrypt(data_bytes)
    return base64.b64encode(encrypted).decode("utf-8")


def decrypt_aes(encrypted_b64: str, key: Optional[str] = None, iv: Optional[str] = None) -> str:
    """AES-128-CBC 解密。"""
    key, iv = get_aes_material(key, iv)
    encrypted_bytes = base64.b64decode(encrypted_b64)
    key_bytes = key.encode("utf-8")[:16].ljust(16, b"\x00")
    iv_bytes = iv.encode("utf-8")[:16].ljust(16, b"\x00")

    cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
    decrypted = cipher.decrypt(encrypted_bytes)

    padding_len = decrypted[-1]
    if 1 <= padding_len <= 16:
        decrypted = decrypted[:-padding_len]

    return decrypted.decode("utf-8")


def random_nonce(length: int = 6) -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(length))


def load_local_env_file(env_file: Path = LOCAL_ENV_FILE) -> None:
    """从本地忽略文件加载环境变量，不覆盖已有环境变量。"""
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_first(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


class GymSession:
    """健身房预约会话。"""

    def __init__(self):
        self.session = requests.Session()
        self.token = None
        self.refresh_token = None
        self.refresh_expire = None
        self.uid = None
        self.card_id = None
        self.student_num = None
        self.school_id = None
        self.user_type = "2"
        self.role = None
        self.login_type = "4"
        self.course_id = "0"
        self.class_id = "0"
        self.page_type = "1"
        self.term_id = ""
        self.phpsessid = None

    @staticmethod
    def _parse_form_body(body_text: str) -> dict:
        return {
            key: value
            for key, value in urllib.parse.parse_qsl(body_text or "", keep_blank_values=True)
        }

    def _update_cookies_from_headers(self, headers: list) -> None:
        for header in headers:
            if header.get("name", "").lower() != "cookie":
                continue
            for chunk in header.get("value", "").split(";"):
                item = chunk.strip()
                if "=" not in item:
                    continue
                name, value = item.split("=", 1)
                self.session.cookies.set(name, value, domain="byty.bupt.edu.cn", path="/")
                if name == "PHPSESSID":
                    self.phpsessid = value

    def _update_cookies_from_response(self, response: requests.Response) -> None:
        for cookie in self.session.cookies:
            if cookie.name == "PHPSESSID":
                self.phpsessid = cookie.value
                break

        raw_headers = getattr(response.raw, "headers", None)
        if raw_headers is None:
            return

        try:
            set_cookies = raw_headers.getlist("Set-Cookie")
        except AttributeError:
            value = response.headers.get("Set-Cookie")
            set_cookies = [value] if value else []

        for header in set_cookies:
            chunk = header.split(";", 1)[0]
            if chunk.startswith("PHPSESSID="):
                self.phpsessid = chunk.split("=", 1)[1]
                break

    def _load_identity_from_params(self, params: dict) -> None:
        self.uid = params.get("uid") or self.uid
        self.card_id = params.get("card_id") or self.card_id
        self.student_num = params.get("student_num") or self.student_num
        self.school_id = params.get("school_id") or self.school_id
        self.user_type = params.get("user_type") or self.user_type
        self.login_type = params.get("login_type") or self.login_type
        self.course_id = params.get("course_id") or self.course_id
        self.class_id = params.get("class_id") or self.class_id
        self.page_type = params.get("type") or self.page_type
        self.term_id = params.get("term_id") if "term_id" in params else self.term_id

    def _apply_env_overrides(self) -> None:
        self.uid = env_first(*UID_ENV_ALIASES) or self.uid
        self.student_num = env_first(*STUDENT_ENV_ALIASES) or self.student_num

    def _apply_fixed_defaults(self) -> None:
        self.card_id = self.student_num or self.card_id
        self.school_id = "798"
        self.user_type = "2"
        self.login_type = "4"
        self.course_id = "0"
        self.class_id = self.class_id or "0"
        self.page_type = "1"
        self.term_id = self.term_id if self.term_id is not None else ""

    def _load_identity_from_capture(self, capture_file: str = DEFAULT_CAPTURE_FILE) -> dict:
        records = json.loads(Path(capture_file).read_text())
        latest = None

        for item in records:
            if item.get("path") == "/bdlp_h5_fitness_test/public/index.php/index/Index/checkLogin":
                latest = item

        if latest is None:
            for item in records:
                if item.get("path") == "/bdlp_h5_fitness_test/public/index.php/index/Stadium/getStadiumList":
                    latest = item

        if latest is None:
            raise ValueError(f"抓包中未找到 checkLogin/getStadiumList: {capture_file}")

        body_text = latest.get("request", {}).get("body", {}).get("text", "")
        params = self._parse_form_body(body_text)
        headers = latest.get("request", {}).get("header", {}).get("headers", [])

        self._load_identity_from_params(params)
        self._update_cookies_from_headers(headers)
        return params

    def load_session_from_capture(self, capture_file: str = DEFAULT_CAPTURE_FILE) -> dict:
        """
        从抓包文件恢复最近一次 getStadiumList 的登录参数。

        这一步的目的不是长期依赖抓包，而是先把第一个接口跑通。
        """
        params = self._load_identity_from_capture(capture_file)
        self.token = params.get("token")

        return self.get_token_info()

    def load_session_from_env(self, capture_file: str = DEFAULT_CAPTURE_FILE) -> dict:
        load_local_env_file()
        code = (
            os.getenv("GYM_CODE")
            or os.getenv("BUPT_GYM_CODE")
            or os.getenv("CODE")
        )
        if code:
            self.load_session_from_code(code)
            return self.get_token_info()

        token = (
            env_first(*TOKEN_ENV_ALIASES)
            or DEFAULT_GYM_TOKEN
        )
        uid = env_first(*UID_ENV_ALIASES) or DEFAULT_GYM_UID
        student_num = env_first(*STUDENT_ENV_ALIASES) or DEFAULT_GYM_STUDENT_NUM
        if not all([token, uid, student_num]):
            raise ValueError("未找到完整登录信息，请在环境变量或 .env.local 中设置 SESSION_A、SESSION_B、SESSION_C")

        self.uid = uid
        self.student_num = student_num
        self.card_id = student_num
        self.token = token
        self._apply_fixed_defaults()
        self.phpsessid = None
        try:
            self.session.cookies.clear(domain="byty.bupt.edu.cn", path="/", name="PHPSESSID")
        except KeyError:
            pass
        self.refresh_h5_session()
        return self.get_token_info()

    @staticmethod
    def _clean_v3_params(params: dict) -> dict:
        cleaned = {}
        for key, value in params.items():
            if value is None:
                continue
            if key == "uid" and value in ("", 0, "0"):
                continue
            cleaned[key] = value
        return cleaned

    @staticmethod
    def _role_to_user_type(role: Optional[int]) -> str:
        mapping = {
            1: "2",
            2: "1",
            3: "3",
        }
        return mapping.get(role, "2")

    def _build_v3_payload(self, data: dict) -> dict:
        base_params = {
            "school_id": int(self.school_id or 798),
            "term_id": int(self.term_id or 0),
            "course_id": int(self.course_id or 0),
            "class_id": int(self.class_id or 0),
            "student_num": self.student_num or 0,
            "card_id": self.card_id or self.student_num or 0,
            "uid": self.uid or "",
            "token": self.token or "",
            "timestamp": round(time.time(), 3),
            "version": 1,
            "nonce": random_nonce(),
            "ostype": 5,
        }
        merged = self._clean_v3_params({**base_params, **data})
        merged["sign"] = generate_sign(merged)
        return merged

    def v3_api_request(self, endpoint: str, data: dict) -> dict:
        """v3 API 请求。"""
        full_data = self._build_v3_payload(data)
        payload = {
            "ostype": "5",
            "data": encrypt_aes(json.dumps(full_data, ensure_ascii=False)),
        }

        url = f"{BASE_URL}/v3/api.php/{endpoint}"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": "https://servicewechat.com/wxf13a8dee385f2258/20/page-frame.html",
        }

        response = self.session.post(url, data=payload, headers=headers, timeout=10)
        self._update_cookies_from_response(response)
        result = response.json()

        if result.get("is_encrypt") == 1 and result.get("data"):
            decrypted = decrypt_aes(result["data"])
            return json.loads(decrypted)

        return result

    def login_by_code(
        self,
        code: str,
        *,
        nickname: str = "微信用户",
        avatar: str = "",
        ty: str = "",
    ) -> dict:
        result = self.v3_api_request(
            "WpLogin/loginByCode",
            {
                "app_key": APP_KEY,
                "code": code,
                "nickname": nickname,
                "avatar": avatar,
                "ty": ty,
            },
        )
        self.uid = str(result.get("uid") or self.uid or "")
        self.role = result.get("role", self.role)
        self.user_type = self._role_to_user_type(self.role)
        token_data = result.get("token_data") or {}
        self.token = token_data.get("access_token") or self.token
        self.refresh_token = token_data.get("refresh_token") or self.refresh_token
        self.refresh_expire = token_data.get("refresh_expire") or self.refresh_expire
        return result

    def fetch_user_info(self) -> dict:
        result = self.v3_api_request("WpLogin/UserInfo", {})
        self.uid = str(result.get("uid") or self.uid or "")
        self.student_num = str(result.get("student_num") or result.get("number") or self.student_num or "")
        self.card_id = self.student_num or self.card_id
        self.school_id = str(result.get("school_id") or self.school_id or "")
        self.class_id = str(result.get("class_id") or self.class_id or "0")
        if result.get("role") is not None:
            self.role = result.get("role")
            self.user_type = self._role_to_user_type(self.role)
        return result

    def load_session_from_code(self, code: str) -> dict:
        self._apply_env_overrides()
        self.uid = None
        self.token = None
        self.student_num = None
        self.card_id = None
        self._apply_fixed_defaults()
        self.phpsessid = None
        try:
            self.session.cookies.clear(domain="byty.bupt.edu.cn", path="/", name="PHPSESSID")
        except KeyError:
            pass

        login_result = self.login_by_code(
            code,
            nickname="微信用户",
            avatar="",
            ty="",
        )
        if not self.token or not self.uid:
            raise ValueError(f"loginByCode 失败: {login_result}")
        self.fetch_user_info()
        self._apply_fixed_defaults()
        self.refresh_h5_session()
        return self.get_token_info()

    def stadium_api_request(
        self,
        endpoint: str,
        params: dict,
        *,
        referer: str = None,
        include_timestamp: bool = False,
        need_sign: bool = False,
    ) -> dict:
        """
        场馆 H5 接口请求。

        注意：现网验证显示，这套接口当前通常不要在 body 里主动加 `sign`。
        """
        full_params = dict(params)

        if include_timestamp:
            full_params.setdefault("timestamp", str(int(time.time())))
            full_params.setdefault("nonce", random_nonce())

        if need_sign:
            full_params["sign"] = generate_sign(full_params)

        url = f"{BASE_URL}/bdlp_h5_fitness_test/public/index.php/index/{endpoint}"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": referer or f"{BASE_URL}/bdlp_h5_fitness_test/view/stadium/home.html",
        }

        response = self.session.post(url, data=full_params, headers=headers, timeout=12)
        self._update_cookies_from_response(response)
        return response.json()

    def _build_check_login_params(self) -> dict:
        if not all([self.token, self.uid, self.card_id, self.student_num, self.school_id]):
            raise ValueError("checkLogin 缺少必要身份字段")

        params = {
            "timestamp": str(int(time.time())),
            "nonce": random_nonce(),
            "course_id": self.course_id or "0",
            "uid": self.uid,
            "card_id": self.card_id,
            "login_type": self.login_type or "4",
            "type": self.page_type or "1",
            "school_id": self.school_id,
            "student_num": self.student_num,
            "user_type": self.user_type or "2",
            "token": self.token,
            "term_id": self.term_id or "",
            "id": "",
        }
        params["sign"] = generate_sign(params)
        return params

    def refresh_h5_session(self) -> dict:
        """
        用当前 token 重建 H5 会话，并从响应里拿新的 PHPSESSID。
        """
        params = self._build_check_login_params()
        referer = (
            f"{BASE_URL}/bdlp_h5_fitness_test/view/stadium/bjyd.html?"
            f"{urllib.parse.urlencode(params)}"
        )
        result = self.stadium_api_request(
            "Index/checkLogin",
            params,
            referer=referer,
            include_timestamp=False,
            need_sign=False,
        )
        if result.get("status") != 1:
            raise ValueError(f"checkLogin 失败: {result}")
        if not self.phpsessid:
            raise ValueError("checkLogin 成功，但未拿到新的 PHPSESSID")
        return result

    def login(self, username: str, password: str) -> dict:
        """v3 登录。"""
        print(f"正在登录用户: {username}...")

        result = self.v3_api_request(
            "WpLogin/getSchoolInfo",
            {
                "student_num": username,
                "password": password,
                "login_type": "4",
            },
        )

        if result.get("status") == 1:
            print("✅ 登录成功")
            for cookie in self.session.cookies:
                if cookie.name == "PHPSESSID":
                    self.phpsessid = cookie.value
                    break

            data = result.get("data") or {}
            self.token = data.get("token")
            self.uid = data.get("uid")
            self.card_id = data.get("card_id")
            self.student_num = data.get("student_num")
            self.school_id = data.get("school_id")
        else:
            print(f"❌ 登录失败: {result}")

        return result

    def get_stadium_list(self) -> dict:
        """获取场地列表。"""
        if not all([self.token, self.uid, self.card_id, self.student_num, self.school_id]):
            return {"status": 0, "info": "未登录或登录信息不完整"}

        params = {
            "course_id": self.course_id or "0",
            "uid": self.uid,
            "card_id": self.card_id,
            "login_type": self.login_type or "4",
            "type": self.page_type or "1",
            "school_id": self.school_id,
            "student_num": self.student_num,
            "user_type": self.user_type or "2",
            "token": self.token,
        }

        return self.stadium_api_request(
            "Stadium/getStadiumList",
            params,
            include_timestamp=True,
            need_sign=False,
        )

    def get_stadium_details(self, stadium_id: str) -> dict:
        return self.stadium_api_request(
            "Stadium/getStadiumDetails",
            {"id": stadium_id},
            referer=f"{BASE_URL}/bdlp_h5_fitness_test/view/stadium/detail.html?id={stadium_id}",
        )

    def get_interval(self, venue_id: str, stadium_id: str, category_id: str, area_id: str = "28") -> dict:
        """
        获取时间段。

        现网验证结果：
        - 只传业务参数会报“登录信息失效”
        - 需要补 uid/student_num/school_id/token
        - 不需要 sign
        """
        if not all([self.token, self.uid, self.student_num, self.school_id]):
            return {"status": 0, "info": "未登录或登录信息不完整"}

        raw_area = str(area_id).strip()
        if raw_area.startswith("[") and raw_area.endswith("]"):
            user_range = raw_area
        elif "," in raw_area:
            user_range = f"[{raw_area}]"
        else:
            user_range = f"[{raw_area}]"

        params = {
            "venue_id": venue_id,
            "stadium_id": stadium_id,
            "user_range": user_range,
            "category_id": category_id,
            "uid": self.uid,
            "student_num": self.student_num,
            "school_id": self.school_id,
            "token": self.token,
        }

        referer = (
            f"{BASE_URL}/bdlp_h5_fitness_test/view/stadium/choose.html"
            f"?stadium_id={stadium_id}&venue_id={venue_id}&user_range={user_range}&category_id={category_id}"
        )
        return self.stadium_api_request("Stadium/getInterval", params, referer=referer)

    def get_venue_config(self, venue_id: str, stadium_id: str, category_id: str, *, referer: str = None) -> dict:
        """
        获取确认页场馆配置。

        抓包里的请求体只有：
        - stadium_id
        - venue_id
        - category_id
        """
        params = {
            "stadium_id": stadium_id,
            "venue_id": venue_id,
            "category_id": category_id,
        }
        return self.stadium_api_request(
            "Stadium/getVenueConfig",
            params,
            referer=referer or f"{BASE_URL}/bdlp_h5_fitness_test/view/stadium/confirm.html",
        )

    def add_order(self, order_data: dict) -> dict:
        """
        提交预约。

        这里请求体按抓包保留纯业务字段，不再混入 uid/token/sign。
        """
        details = order_data.get("details", [])
        payload = {
            "stadium_id": order_data.get("stadium_id", ""),
            "venue_id": order_data.get("venue_id", ""),
            "stadium_name": order_data.get("stadium_name", ""),
            "project_name": order_data.get("project_name", ""),
            "is_academy": order_data.get("is_academy", "1"),
            "academy_name": order_data.get("academy_name", ""),
            "mark": order_data.get("mark", ""),
            "uids": order_data.get("uids", ""),
            "captcha": order_data.get("captcha", ""),
            "category_id": order_data.get("category_id", ""),
            "is_vip": order_data.get("is_vip", "0"),
            "pay_type": order_data.get("pay_type", "1"),
        }

        for idx, detail in enumerate(details):
            payload[f"details[{idx}][date]"] = detail.get("date", "")
            payload[f"details[{idx}][week]"] = detail.get("week", "")
            payload[f"details[{idx}][week_msg]"] = detail.get("week_msg", "")
            payload[f"details[{idx}][area_name]"] = detail.get("area_name", "")
            payload[f"details[{idx}][interval_time]"] = detail.get("interval_time", "")
            payload[f"details[{idx}][interval_id]"] = detail.get("interval_id", "")
            payload[f"details[{idx}][area_id]"] = detail.get("area_id", "")

        referer_params = {
            "stadium_id": order_data.get("stadium_id", ""),
            "venue_id": order_data.get("venue_id", ""),
            "category_id": order_data.get("category_id", ""),
            "project_name": order_data.get("project_name", ""),
            "list": json.dumps(details, ensure_ascii=False),
            "name": order_data.get("project_name", ""),
            "is_academy": order_data.get("is_academy", "1"),
            "academy_name": order_data.get("academy_name", ""),
            "mark": order_data.get("mark", ""),
            "price": order_data.get("price", "10"),
            "p_count": order_data.get("p_count", "0"),
            "ids": order_data.get("ids", ""),
        }
        referer = (
            f"{BASE_URL}/bdlp_h5_fitness_test/view/stadium/confirm.html?"
            f"{urllib.parse.urlencode(referer_params, doseq=True)}"
        )
        return self.stadium_api_request("Stadium/addOrder", payload, referer=referer)

    def get_order_details(self, order_id: str = "", order_num: str = "") -> dict:
        """获取预约详情页数据。`order_id` 和 `order_num` 任意一个可用即可。"""
        if not order_id and not order_num:
            raise ValueError("orderDetails 缺少 order_id/order_num")

        query = {"from": "pay"}
        if order_num:
            query["orderNum"] = order_num
        elif order_id:
            query["id"] = order_id

        referer = (
            f"{BASE_URL}/bdlp_h5_fitness_test/view/stadium/order.html?"
            f"{urllib.parse.urlencode(query)}"
        )
        return self.stadium_api_request(
            "stadium/orderDetails",
            {
                "order_id": order_id,
                "order_num": order_num,
            },
            referer=referer,
        )

    def get_use_records(self, page: int = 1) -> dict:
        """获取使用记录/预约记录列表。"""
        return self.stadium_api_request(
            "Stadium/useRecord",
            {"page": str(page)},
            referer=f"{BASE_URL}/bdlp_h5_fitness_test/view/stadium/records.html?from=center",
        )

    @staticmethod
    def _build_order_detail_html(order: dict, pay_url: str = "") -> str:
        details = order.get("details") or []
        qrcode_text = order.get("qrcode") or ""
        audit_status = int(order.get("audit_status") or 0)
        time_left = int(order.get("time") or 0)
        companions_html = ""
        if order.get("p_count"):
            companions_html = (
                '<div class="body-item"><div class="title">随行人员</div>'
                f'<div class="detail">{html.escape(str(order.get("p_count")))}人</div></div>'
            )
        container_classes = "f-container stadium-order"
        if audit_status == 6 and pay_url:
            container_classes += " fix_bottom"
        pay_target_attr = 'target="_blank"' if pay_url else ""

        detail_cards = []
        for idx, detail in enumerate(details):
            status = int(detail.get("status") or 0)
            status_text = ORDER_STATUS_TEXT.get(status, "")
            active_class = " active" if idx == 0 else ""
            detail_cards.append(
                f"""
            <div class="m-tab-item{active_class}" data-details-id="{html.escape(str(detail.get("details_id", "")))}" data-status="{status}">
              <span class="title">{html.escape(str(detail.get("date", "")))} {html.escape(str(detail.get("week", "")))}</span>
              <span class="addr">{html.escape(str(detail.get("area_name", "")))}</span>
              <span class="time">{html.escape(str(detail.get("interval_time", "")))}</span>
              <span class="btn" style="border: 0px; color: #656565">{html.escape(status_text)}</span>
            </div>
                """.rstrip()
            )

        detail_cards_html = "\n".join(detail_cards)
        qrcode_title_style = "" if qrcode_text else "display:none;"
        pay_button_class = "u-fixed-bottom u-text-center u-flex show" if audit_status == 6 and pay_url else "u-fixed-bottom u-text-center u-flex"
        pay_countdown = f"(还剩{time_left // 60:02d}:{time_left % 60:02d})" if time_left > 0 else ""

        return f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
    <title>预约详情</title>
    <link rel="stylesheet" href="{BASE_URL}/bdlp_h5_fitness_test/public/static/lib/layui/css/layui.css" />
    <link rel="stylesheet" href="{BASE_URL}/bdlp_h5_fitness_test/public/static/css/style.css" />
    <style>
      .max_font * {{
        font-size: 1.1rem !important;
      }}
      .u-fixed-bottom {{
        cursor: pointer;
        line-height: 50px;
        font-size: 16px;
        opacity: 1;
        color: #fff;
        display: none;
      }}
      .lock {{
        opacity: 0.7;
      }}
      #content.fix_bottom {{
        padding-bottom: 50px;
      }}
      .show {{
        display: block;
      }}
      .pay-less {{
        font-size: 14px;
      }}
      .m-tab-item {{
        cursor: pointer;
      }}
      .m-tab-item.active {{
        border-color: #17c182;
        box-shadow: 0 0 0 1px rgba(23, 193, 130, 0.15);
      }}
    </style>
  </head>
  <body>
    <div class="{container_classes}" id="content">
      <header class="mod-header header-border">
        <a class="back layui-icon layui-icon-left" href="javascript:history.back(-1)"></a>
        <h1 class="pagename">订单详情</h1>
        <a class="right-txt" href="javascript:;" onclick="return false;">取消预约</a>
      </header>
      <div class="info">
        <div class="detail-box">
          <div class="body-item">
            <div class="title">场馆名称</div>
            <div class="detail">{html.escape(str(order.get("stadium_name", "")))}</div>
          </div>
          <div class="body-item">
            <div class="title">场馆地址</div>
            <div class="detail">{html.escape(str(order.get("location", "")))}</div>
          </div>
          <div class="body-item">
            <div class="title">预约编号</div>
            <div class="detail">{html.escape(str(order.get("order_num", "")))}</div>
          </div>
          <div class="body-item">
            <div class="title">项目名称</div>
            <div class="detail">{html.escape(str(order.get("project_name", "")))}</div>
          </div>
          <div class="body-item">
            <div class="title">订单价格</div>
            <div class="detail">{html.escape(str(order.get("price", "")))}</div>
          </div>
          <div class="body-item">
            <div class="title">预约类型</div>
            <div class="detail">{html.escape(str(order.get("type", "")))}</div>
          </div>
          {companions_html}
        </div>
        <div class="head">
          <div class="head-title">预约场次</div>
          <div class="m-tab">
{detail_cards_html}
          </div>
        </div>
        <div class="foot">
          <div class="foot-title" id="voucher-title" style="{qrcode_title_style}">
            进场凭证<span style="color: #656565; font-size: 12px; font-weight: 200">(预约中场次点击可生成二维码)</span>
          </div>
          <div class="qrcode">
            <div id="qrcode"></div>
            <div class="tips" id="qrcode-tips" style="display:none">到场后凭此二维码入场</div>
          </div>
        </div>
      </div>
      <a class="{pay_button_class}" href="{html.escape(pay_url)}" {pay_target_attr}>
        <div class="bg-succ u-flex-1">
          继续支付
          <span class="pay-less">{html.escape(pay_countdown)}</span>
        </div>
      </a>
    </div>

    <script src="{BASE_URL}/bdlp_h5_fitness_test/public/static/js/qrcode.min.js"></script>
    <script>
      const ORDER_DATA = {json.dumps(order, ensure_ascii=False)};
      const QR_TEXT = {json.dumps(qrcode_text, ensure_ascii=False)};

      function renderQRCode(detailsId, status) {{
        const box = document.getElementById('qrcode');
        const tips = document.getElementById('qrcode-tips');
        box.innerHTML = '';
        tips.style.display = 'none';
        if (status !== 1 || !QR_TEXT) {{
          return;
        }}
        tips.style.display = 'block';
        new QRCode(box, String(detailsId) + '|' + QR_TEXT);
      }}

      document.querySelectorAll('.m-tab-item').forEach((node) => {{
        node.addEventListener('click', () => {{
          document.querySelectorAll('.m-tab-item').forEach((item) => item.classList.remove('active'));
          node.classList.add('active');
          renderQRCode(node.dataset.detailsId, Number(node.dataset.status));
        }});
      }});

      if (ORDER_DATA.details && ORDER_DATA.details.length > 0) {{
        const first = ORDER_DATA.details[0];
        renderQRCode(first.details_id, Number(first.status || 0));
      }}
    </script>
  </body>
</html>
"""

    def render_order_detail_html(self, order_details: dict, output_path: str, pay_url: str = "") -> str:
        """把 orderDetails 响应渲染成本地详情页。"""
        data = order_details.get("data") if "data" in order_details else order_details
        if not isinstance(data, dict) or not data:
            raise ValueError(f"预约详情数据异常: {order_details}")

        target = Path(output_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._build_order_detail_html(data, pay_url=pay_url), encoding="utf-8")
        return str(target)

    def get_captcha(self) -> bytes:
        """获取验证码图片原始字节。"""
        url = f"{BASE_URL}/bdlp_h5_fitness_test/public/index.php/index/index/captcha"
        query = {
            "r": str(random.random()),
            "t": str(int(time.time())),
            "uid": self.uid or "",
        }
        query["sign"] = generate_h5_sign(
            {
                "r": query["r"],
                "t": query["t"],
                "uid": query["uid"],
            }
        )
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "image/wxpic,image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": f"{BASE_URL}/bdlp_h5_fitness_test/view/stadium/confirm.html",
        }
        response = self.session.get(url, params=query, headers=headers, timeout=10)
        response.raise_for_status()
        return response.content

    def get_and_recognize_captcha(self, max_retries: int = 3) -> str:
        """
        获取并识别验证码，带有重试逻辑。

        如果识别结果不符合预期（非 4 位字母数字），则自动重试。
        返回识别到的验证码字符串。
        """
        for attempt in range(1, max_retries + 1):
            image_bytes = self.get_captcha()
            code = recognize_captcha(image_bytes)
            if code and len(code) == 4 and code.isalnum():
                print(f"✅ 验证码识别成功 (第{attempt}次): {code}")
                return code
            print(f"⚠️  验证码识别结果异常 (第{attempt}次): '{code}'，重试...")
            time.sleep(0.5)

        # 最后一次不管结果直接返回
        image_bytes = self.get_captcha()
        code = recognize_captcha(image_bytes)
        print(f"⚠️  验证码最终识别结果: '{code}'")
        return code

    def get_token_info(self) -> dict:
        return {
            "token": self.token,
            "refresh_token": self.refresh_token,
            "refresh_expire": self.refresh_expire,
            "uid": self.uid,
            "card_id": self.card_id,
            "student_num": self.student_num,
            "school_id": self.school_id,
            "role": self.role,
            "class_id": self.class_id,
            "user_type": self.user_type,
            "phpsessid": self.phpsessid,
        }


# ───────────────── 验证码识别 ─────────────────

# 全局懒加载 OCR 实例（ddddocr 初始化较慢，只初始化一次）
_ocr_instance: Optional[ddddocr.DdddOcr] = None


def _get_ocr() -> ddddocr.DdddOcr:
    """懒加载 ddddocr 实例。"""
    global _ocr_instance
    if _ocr_instance is None:
        _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance


def recognize_captcha(
    image_bytes: bytes,
    *,
    preprocess: bool = True,
    save_debug: bool = False,
    debug_path: str = "captcha_debug.png",
) -> str:
    """
    识别 4 位验证码图片。

    Args:
        image_bytes: 验证码图片的原始字节 (PNG/JPEG/GIF 等)。
        preprocess:  是否做灰度 + 二值化预处理（对简单彩色背景验证码有帮助）。
        save_debug:  是否将预处理后的图片保存到本地以便排查。
        debug_path:  调试图片的保存路径。

    Returns:
        识别出的验证码字符串（已去除空格并转小写）。
    """
    ocr = _get_ocr()

    if preprocess:
        try:
            img = Image.open(io.BytesIO(image_bytes))
            # 转灰度
            img = img.convert("L")
            # 二值化：阈值 140 适用于大多数简单验证码
            img = img.point(lambda px: 255 if px > 140 else 0, "1")

            if save_debug:
                img.save(debug_path)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            image_bytes = buf.getvalue()
        except Exception as exc:
            # 预处理失败就直接把原图交给 OCR
            print(f"⚠️  验证码预处理失败，使用原图: {exc}")

    result: str = ocr.classification(image_bytes)
    return result.strip().lower()


# ───────────────── 工具函数 ─────────────────

def test_connection() -> bool:
    print("=" * 60)
    print("测试基本连接...")
    print("=" * 60)
    try:
        response = requests.get(BASE_URL, timeout=10)
        print(f"✅ 服务器响应: {response.status_code}")
        return True
    except Exception as exc:
        print(f"❌ 连接失败: {exc}")
        return False


def main():
    print("=" * 60)
    print("健身房自动预约脚本")
    print("=" * 60)

    gym = GymSession()

    if not test_connection():
        print("服务器连接失败，退出")
        return

    try:
        token_info = gym.load_session_from_env(DEFAULT_CAPTURE_FILE)
        print("已从本地配置加载会话，并刷新 H5 会话")
        print(json.dumps(token_info, ensure_ascii=False, indent=2))
    except Exception as env_exc:
        print(f"从本地配置恢复会话失败: {env_exc}")
        print("请先设置 SESSION_A、SESSION_B、SESSION_C")
        return

    print("\n" + "=" * 60)
    print("测试获取场地列表...")
    print("=" * 60)
    result = gym.get_stadium_list()
    print(json.dumps(result, ensure_ascii=False, indent=2)[:1500])


if __name__ == "__main__":
    main()
