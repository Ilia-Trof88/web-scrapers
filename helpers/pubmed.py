"""Доступ к PubMed в обход proof-of-work заглушки.

Два независимых способа получить данные статьи:

1. ``fetch_articles`` — официальный E-utilities API (XML). Основной путь:
   стабилен, не требует обхода защиты, лимит 3 запроса/с без API-ключа.
2. ``fetch_via_html`` — парсинг обычной HTML-страницы. PubMed отдаёт на неё
   JS-заглушку с proof-of-work челленджем; ``PubMedSession`` решает его
   (sha256 с нужным числом ведущих нулевых бит), получает cookie
   ``N_POW_Challenge`` (живёт ~5 минут) и дальше отдаёт нормальные страницы.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://pubmed.ncbi.nlm.nih.gov"
EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _leading_zero_bits(digest: bytes) -> int:
    """Число ведущих нулевых бит в байтовой строке (как в JS-челлендже)."""
    total = 0
    for byte in digest:
        if byte == 0:
            total += 8
            continue
        for bit in range(7, -1, -1):
            if byte & (1 << bit) == 0:
                total += 1
            else:
                return total
    return total


def _solve_challenge(challenge_id: str, bits: int) -> int:
    """Подбирает nonce, при котором sha256(f'{challenge_id}:{nonce}') имеет >= bits нулей."""
    nonce = 0
    while True:
        digest = hashlib.sha256(f"{challenge_id}:{nonce}".encode()).digest()
        if _leading_zero_bits(digest) >= bits:
            return nonce
        nonce += 1


class PubMedChallengeError(RuntimeError):
    """Вместо статьи вернулась PoW-заглушка, и пройти челлендж не удалось.

    Раньше в этом случае молча возвращалась заглушка, и дальше ``soup.find(...)``
    давал ``None`` — ошибка всплывала далеко от причины. Теперь падаем сразу.
    """


class PubMedSession(requests.Session):
    """requests.Session, который сам проходит PoW-челлендж PubMed.

    Cookie ``N_POW_Challenge`` живёт всего ~5 минут, поэтому между запусками она
    не кэшируется: решить челлендж заново стоит ~10 мс, а протухшая cookie в
    jar — источник трудноуловимых 203-ответов.
    """

    def __init__(self, timeout: int = 30):
        super().__init__()
        self.headers.update(DEFAULT_HEADERS)
        self.timeout = timeout
        self.last_solve_error: str | None = None

    # --- challenge ----------------------------------------------------------

    @staticmethod
    def challenge_params(html: str) -> tuple[str, int] | None:
        """Возвращает (challenge_id, bits), если в ответе заглушка."""
        cid = re.search(r'const challengeId\s*=\s*"([^"]+)"', html)
        if not cid:
            return None
        bits = re.search(r"const bits\s*=\s*(\d+)", html)
        return cid.group(1), int(bits.group(1)) if bits else 12

    def _drop_pow_cookies(self) -> None:
        """Убирает старые N_POW_Challenge: с просроченной cookie сервер
        подтверждает solve, но продолжает отдавать заглушку."""
        for cookie in list(self.cookies):
            if cookie.name == "N_POW_Challenge":
                self.cookies.clear(cookie.domain, cookie.path, cookie.name)

    def solve_challenge(self, html: str, page_url: str) -> bool:
        params = self.challenge_params(html)
        if params is None:
            return False
        challenge_id, bits = params
        # Старую (просроченную) cookie надо убрать до POST: иначе сервер
        # подтверждает solve, но продолжает отдавать заглушку.
        self._drop_pow_cookies()
        nonce = _solve_challenge(challenge_id, bits)
        response = self.post(
            f"{BASE_URL}/_pow/solve",
            json={
                "challenge_id": challenge_id,
                "nonce": str(nonce),
                "redirect_url": page_url,
            },
            headers={"Content-Type": "application/json", "Referer": page_url},
            timeout=self.timeout,
        )
        # Неверный nonce сервер помечает как 429 invalid_pow и «сжигает» челлендж,
        # поэтому подбирать его повторно нельзя — лучше сразу упасть на ошибке.
        if response.status_code != 200:
            self.last_solve_error = f"HTTP {response.status_code}: {response.text[:200]}"
            logger.warning("challenge %s: /_pow/solve -> %s %s",
                           challenge_id, response.status_code, response.text[:200])
            return False
        self.last_solve_error = None
        return True

    # --- API ----------------------------------------------------------------

    def get_page(self, url: str, max_attempts: int = 3, strict: bool = True) -> requests.Response:
        """GET страницы; при заглушке решает челлендж и повторяет запрос.

        При ``strict=True`` (по умолчанию) вместо заглушки бросает
        PubMedChallengeError, а не возвращает страницу-челлендж.
        """
        response = self.get(url, timeout=self.timeout)
        for _ in range(max_attempts - 1):
            if response.status_code == 200 and not self.challenge_params(response.text):
                return response
            if not self.solve_challenge(response.text, url):
                break
            response = self.get(url, timeout=self.timeout)

        if self.challenge_params(response.text):
            reason = getattr(self, "last_solve_error", None) or "челлендж не решён"
            if strict:
                raise PubMedChallengeError(
                    f"{url}: вместо статьи вернулась PoW-заглушка "
                    f"(HTTP {response.status_code}, {len(response.text)} байт, {reason}).\n"
                    "Что делать: подождать ~1 минуту (429 invalid_pow — это троттлинг "
                    "NCBI), проверить, что запросы идут с той же машины/IP, или "
                    "использовать E-utilities вместо HTML."
                )
            logger.warning("%s: заглушка не пройдена (%s)", url, reason)
        return response


# --- E-utilities (основной путь) --------------------------------------------


def fetch_via_eutils(
    pmid: str | list[str],
    *,
    api_key: str | None = None,
    email: str | None = None,
    session: requests.Session | None = None,
) -> ET.Element:
    """XML-ответ E-utilities для одной или нескольких статей (до 200 id за запрос)."""
    pmids = pmid if isinstance(pmid, str) else ",".join(pmid)
    params = {"db": "pubmed", "id": pmids, "retmode": "xml"}
    if api_key:
        params["api_key"] = api_key
    if email:
        params["email"] = email
        params["tool"] = "webscrapers"

    session = session or requests.Session()
    response = session.get(EUTILS_URL, params=params, timeout=30)
    response.raise_for_status()
    return ET.fromstring(response.content)


def parse_eutils_article(article: ET.Element) -> dict[str, Any]:
    """Разбирает один <PubmedArticle> из ответа E-utilities."""

    def text(node: ET.Element | None) -> str:
        if node is None:
            return ""
        return " ".join("".join(node.itertext()).split())

    abstract = " ".join(
        part for part in (text(n) for n in article.findall(".//Abstract/AbstractText")) if part
    )
    authors = []
    for author in article.findall(".//AuthorList/Author"):
        last, initials = text(author.find("LastName")), text(author.find("Initials"))
        if last:
            authors.append(f"{last} {initials}".strip())

    doi = ""
    for node in article.findall(".//ArticleIdList/ArticleId"):
        if node.get("IdType") == "doi":
            doi = text(node)
            break

    return {
        "pmid": text(article.find(".//MedlineCitation/PMID")),
        "title": text(article.find(".//ArticleTitle")),
        "journal": text(article.find(".//Journal/Title")),
        "year": text(article.find(".//JournalIssue/PubDate/Year"))
        or text(article.find(".//JournalIssue/PubDate/MedlineDate")),
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
    }


def fetch_articles(pmids: list[str], **eutils_kwargs: Any) -> list[dict[str, Any]]:
    """Пакетная загрузка статей через E-utilities."""
    root = fetch_via_eutils(pmids, **eutils_kwargs)
    return [parse_eutils_article(a) for a in root.findall("PubmedArticle")]


# --- HTML (с решением PoW) --------------------------------------------------


def _text(node: Any, default: str = "") -> str:
    return node.get_text(" ", strip=True) if node is not None else default


def fetch_via_html(pmid: str, session: PubMedSession | None = None) -> dict[str, Any]:
    """Статья со страницы https://pubmed.ncbi.nlm.nih.gov/<pmid>/ (через PoW)."""
    session = session or PubMedSession()
    url = f"{BASE_URL}/{pmid}/"
    response = session.get_page(url)
    if response.status_code != 200:
        raise RuntimeError(f"PubMed вернул {response.status_code} для {url}")

    soup = BeautifulSoup(response.text, "html.parser")
    authors_list = soup.find("div", class_="authors-list")
    return {
        "pmid": pmid,
        "url": url,
        "title": _text(soup.find("h1", class_="heading-title")),
        "journal": _text(soup.find("button", id="full-view-journal-trigger")),
        "authors": [
            _text(node) for node in authors_list.select("a.full-name")
        ] if authors_list else [],
        "abstract": _text(soup.find("div", class_="abstract-content")),
        "doi": _text(soup.find("span", class_="citation-doi"))
        .removeprefix("doi:")
        .strip()
        .rstrip("."),
    }


if __name__ == "__main__":
    started = time.time()
    html_article = fetch_via_html("33126180")
    print("HTML   :", html_article["title"], "| abstract:", len(html_article["abstract"]), "chars")
    xml_article = fetch_articles(["33126180"])[0]
    print("EUTILS :", xml_article["title"], "| abstract:", len(xml_article["abstract"]), "chars")
    print(f"ok in {time.time() - started:.2f}s")
