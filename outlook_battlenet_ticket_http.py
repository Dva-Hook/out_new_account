# -*- coding: utf-8 -*-
"""Fetch a Battle.net account link from Outlook using HTTP only."""

from __future__ import annotations

import html
import re
import sys
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import parse_qs, urlsplit

try:
    import requests
except ImportError as exc:
    raise SystemExit("缺少 requests，请先运行：python -m pip install requests") from exc


SENDER = "noreply@battle.net"
TOKEN_ENDPOINTS = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    "https://login.live.com/oauth20_token.srf",
)
MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
TOKEN_SCOPE = "https://graph.microsoft.com/.default offline_access"
# Unregistered consumer clients reject cross-origin token redemption.
TOKEN_ORIGIN = None
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class AccessTokenExpired(RuntimeError):
    pass


class GraphMailUnauthorized(RuntimeError):
    pass


class HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.urls.append(value)


def parse_credential(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if "----" in raw and "|" not in raw:
        parts = raw.split("----")
        field_order = ("email", "password", "client_id", "refresh_token")
    else:
        parts = raw.split("|", 3)
        field_order = ("email", "password", "refresh_token", "client_id")
    if len(parts) not in (4, 6) or not all(parts[:4]):
        raise ValueError(
            "凭据格式应为：邮箱----密码----client_id----refresh_token，"
            "或再追加 ----辅助邮箱----辅助邮箱密码"
        )
    return dict(zip(field_order, (part.strip() for part in parts[:4])))


def get_access_token(
    session: requests.Session, client_id: str, refresh_token: str
) -> tuple[str, str]:
    errors: list[str] = []
    for endpoint in TOKEN_ENDPOINTS:
        try:
            token_headers = {"Accept": "application/json"}
            if TOKEN_ORIGIN:
                token_headers["Origin"] = TOKEN_ORIGIN
            response = session.post(
                endpoint,
                data={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": TOKEN_SCOPE,
                },
                headers=token_headers,
                timeout=20,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{urlsplit(endpoint).netloc}: {exc}")
            continue

        access_token = data.get("access_token")
        if access_token:
            return str(access_token), str(data.get("refresh_token") or refresh_token)

        detail = data.get("error_description") or data.get("error") or response.reason
        errors.append(f"{urlsplit(endpoint).netloc}: {str(detail)[:160]}")

    raise RuntimeError("access_token 获取失败；" + "；".join(errors))


def message_sender(message: dict) -> str:
    return str(
        message.get("from", {}).get("emailAddress", {}).get("address", "")
    ).strip().lower()


def candidate_urls(content: str) -> Iterable[str]:
    decoded = html.unescape(content or "")
    parser = HrefCollector()
    try:
        parser.feed(decoded)
    except Exception:
        pass

    yield from parser.urls
    for match in URL_PATTERN.finditer(decoded):
        yield match.group(0).rstrip(".,);]}")


def direct_battlenet_link(candidate: str, depth: int = 0) -> str | None:
    if depth > 2:
        return None

    candidate = html.unescape(candidate).strip().rstrip(".,);]}")
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None

    if (
        (parsed.hostname or "").lower() == "account.battle.net"
        and parsed.path.rstrip("/").lower() == "/overview"
        and parse_qs(parsed.query).get("ticket")
    ):
        return candidate

    query = parse_qs(parsed.query)
    for key in ("url", "target", "redirect", "redirecturl", "redirect_uri"):
        for value in query.get(key, ()):  # parse_qs already performs one URL decode.
            nested = direct_battlenet_link(value, depth + 1)
            if nested:
                return nested
    return None


def extract_battlenet_link(message: dict) -> str | None:
    body = message.get("body", {}) or {}
    sources = (
        str(body.get("content") or ""),
        str(message.get("bodyPreview") or ""),
    )
    seen: set[str] = set()
    for source in sources:
        for candidate in candidate_urls(source):
            if candidate in seen:
                continue
            seen.add(candidate)
            link = direct_battlenet_link(candidate)
            if link:
                return link
    return None


def find_link(
    session: requests.Session, access_token: str
) -> tuple[str | None, dict | None, int, int]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Prefer": 'outlook.body-content-type="html"',
    }
    params = {
        "$top": "50",
        "$select": "id,subject,from,receivedDateTime,bodyPreview,body",
        "$orderby": "receivedDateTime desc",
    }
    next_url: str | None = MESSAGES_URL
    scanned = 0
    sender_matches = 0

    while next_url:
        response = session.get(
            next_url,
            headers=headers,
            params=params if next_url == MESSAGES_URL else None,
            timeout=30,
        )
        if response.status_code == 401:
            raise AccessTokenExpired("Microsoft Graph access token 已过期")
        if response.status_code == 403:
            try:
                payload = response.json()
                detail = payload.get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                detail = ""
            suffix = f"：{detail}" if detail else ""
            raise GraphMailUnauthorized(
                "Graph 读信未授权（HTTP 403）"
                f"{suffix}；refresh token 不能新增 Mail.Read 权限，"
                "请使用已预先同意 Mail.Read 的 client_id/token"
            )
        response.raise_for_status()
        payload = response.json()
        messages = payload.get("value", [])

        for message in messages:
            scanned += 1
            if message_sender(message) != SENDER:
                continue
            sender_matches += 1
            link = extract_battlenet_link(message)
            if link:
                return link, message, scanned, sender_matches

        next_url = payload.get("@odata.nextLink")

    return None, None, scanned, sender_matches


def main() -> int:
    print("请输入完整邮箱凭证（邮箱----密码----client_id----refresh_token）：")
    credential = parse_credential(input("> ").strip())
    refresh_token = credential["refresh_token"]

    with requests.Session() as session:
        session.headers["User-Agent"] = "OutlookBattleNetLinkFetcher/1.0"
        access_token, refresh_token = get_access_token(
            session, credential["client_id"], refresh_token
        )

        while True:
            print(f"正在获取 {credential['email']} 的邮件...")
            try:
                link, message, scanned, sender_matches = find_link(session, access_token)
            except AccessTokenExpired:
                access_token, refresh_token = get_access_token(
                    session, credential["client_id"], refresh_token
                )
                continue
            except GraphMailUnauthorized as exc:
                print(f"本次获取失败：{exc}")
                return 1
            except (requests.RequestException, ValueError) as exc:
                print(f"本次获取出错：{exc}")
                input("按 Enter 重新获取邮件...")
                continue

            if link:
                print(f"已扫描 {scanned} 封邮件，匹配发件人 {sender_matches} 封。")
                print(f"主题：{message.get('subject') or '(无主题)'}")
                print(f"时间：{message.get('receivedDateTime') or '(未知)'}")
                print("\n找到链接：")
                print(link)
                return 0

            print(f"已扫描 {scanned} 封邮件，匹配发件人 {sender_matches} 封，尚未找到链接。")
            input("按 Enter 后重新获取邮件...")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已结束。")
        raise SystemExit(130)
