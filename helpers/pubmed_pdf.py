"""Загрузка PDF статей по данным, собранным PubMedScraper.

Модуль отвечает только за сеть и файлы; про BeautifulSoup и про класс скрапера
он ничего не знает. Точка входа — ``download_for_records``.

Откуда берётся PDF
------------------

1. PMCID. В PubMedScraper PMCID не парсится, поэтому он уточняется батчами через
   Europe PMC REST (``/search``, до 50 PMID на запрос, без API-ключа).
2. Сам файл. PDF лежит в официальном публичном бакете PMC Open Access на AWS:
   ``https://pmc-oa-opendata.s3.amazonaws.com/<PMCID>.<версия>/<PMCID>.<версия>.pdf``.
   Без логина, без текстовой капчи и без proof-of-work — это штатный способ NCBI
   (https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/), которым заменили FTP-раздачу.

Чего модуль не делает
---------------------

Статьи, которых нет в Open Access Subset (авторские рукописи без CC-лицензии и
статьи по подписке), не скачиваются: у них ``pdf_status == "not_in_oa_subset"``.
Издательские пейволлы (Elsevier, Wiley, OUP и т.п.) не обходятся.

Ограничение скорости
--------------------

S3 отдаёт примерно 25 КБ/с на соединение, поэтому по умолчанию качаем в 4 потока.
Полный датасет из 1000 статей — это ~0.9 ГБ и порядка 2.5–3 часов.
"""

from __future__ import annotations

import ast
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from helpers.pubmed import DEFAULT_HEADERS

EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PMC_OA_BUCKET_URL = "https://pmc-oa-opendata.s3.amazonaws.com"

EPMC_BATCH_SIZE = 50
S3_LIST_PAGE_SIZE = 100
PDF_MAGIC = b"%PDF-"

_PMID_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")
_PMCID_RE = re.compile(r"(PMC\d+)")
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+")

# Статусы, которые попадают в запись. Список закрытый, чтобы по нему можно было
# фильтровать датафрейм, не разбираясь в текстах ошибок.
STATUS_DOWNLOADED = "downloaded"
STATUS_EXISTS = "exists"
STATUS_NO_PMCID = "no_pmcid"
STATUS_NOT_IN_OA = "not_in_oa_subset"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


@dataclass
class PdfInfo:
    """Промежуточный результат по одной статье."""

    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    license: str | None = None
    status: str = "pending"
    path: str | None = None
    url: str | None = None
    source: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# Идентификаторы
# --------------------------------------------------------------------------- #


def extract_pmid(pubmed_url: str | None) -> str | None:
    """PMID из ссылки вида https://pubmed.ncbi.nlm.nih.gov/33126180/."""
    # isinstance нужен из-за pandas: незаполненное поле приезжает как nan (float).
    text = pubmed_url if isinstance(pubmed_url, str) else ""
    match = _PMID_RE.search(text)
    return match.group(1) if match else None


def as_links(value: Any) -> list[str]:
    """Приводит поле full_text_links к списку строк.

    Список из perform_parsing возвращается как есть, но из CSV то же поле
    приезжает строкой вида "['https://...', 'https://...']" — её тоже разбираем,
    чтобы метод работал и на данных, прочитанных обратно из файла.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(link) for link in value if link]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                return [text]
            return [str(link) for link in parsed if link]
        return [text]
    return []


def extract_doi(full_text_links: Any) -> str | None:
    """DOI из собранных ссылок на полный текст (doi.org / dx.doi.org)."""
    for link in as_links(full_text_links):
        for prefix in ("doi.org/", "dx.doi.org/"):
            if prefix in link:
                match = _DOI_RE.search(link.split(prefix, 1)[1])
                if match:
                    return match.group(0).rstrip(".")
    return None


def extract_pmcid(full_text_links: Any) -> str | None:
    """PMCID, если он уже есть в ссылках.

    В PubMed ссылки на PMC обычно строятся по PMID (/articles/pmid/33126180/),
    поэтому PMCID там встречается редко — в этом случае вернётся None.
    """
    for link in as_links(full_text_links):
        match = _PMCID_RE.search(link)
        if match:
            return match.group(1)
    return None


# --------------------------------------------------------------------------- #
# Europe PMC: PMID -> PMCID / DOI / лицензия
# --------------------------------------------------------------------------- #


def lookup_ids(
    pmids: Iterable[str | None],
    session: requests.Session | None = None,
    delay: float = 0.12,
) -> dict[str, dict[str, Any]]:
    """Сопоставляет PMID с PMCID и метаданными через Europe PMC.

    Args:
        pmids (Iterable[str | None]): Список PMID (None и дубли отбрасываются).
        session (requests.Session | None): Сессия; по умолчанию создаётся своя.
        delay (float): Пауза между батчами в секундах.

    Returns:
        dict[str, dict]: PMID -> {"pmcid", "doi", "license", "is_open_access"}.
    """
    session = session or requests.Session()
    unique = [pmid for pmid in dict.fromkeys(pmids) if pmid]
    result: dict[str, dict[str, Any]] = {}

    for start in range(0, len(unique), EPMC_BATCH_SIZE):
        chunk = unique[start : start + EPMC_BATCH_SIZE]
        query = " OR ".join(f"EXT_ID:{pmid}" for pmid in chunk)
        try:
            response = session.get(
                EUROPEPMC_SEARCH_URL,
                params={
                    "query": query,
                    "format": "json",
                    "resultType": "core",
                    "pageSize": EPMC_BATCH_SIZE,
                },
                headers=DEFAULT_HEADERS,
                timeout=60,
            )
        except requests.RequestException as exc:
            print(f"Europe PMC недоступен: {type(exc).__name__}: {exc}")
            continue

        if response.status_code != 200:
            print(f"Europe PMC вернул {response.status_code} на батче {start // EPMC_BATCH_SIZE}")
            continue

        for record in response.json().get("resultList", {}).get("result", []):
            pmid = record.get("pmid")
            if not pmid:
                continue
            result[pmid] = {
                "pmcid": record.get("pmcid"),
                "doi": record.get("doi"),
                "license": record.get("license"),
                "is_open_access": record.get("isOpenAccess"),
            }

        time.sleep(delay)

    return result


# --------------------------------------------------------------------------- #
# PMC Open Access в AWS S3
# --------------------------------------------------------------------------- #


def find_pdf_key(pmcid: str, session: requests.Session | None = None) -> tuple[str | None, int | None]:
    """Ищет ключ главного PDF статьи в бакете PMC Open Access.

    Главный файл всегда лежит как ``<PMCID>.<версия>/<PMCID>.<версия>.pdf``.
    Листинг нужен, потому что версия бывает не только ``.1`` (на разобранной
    выборке из 638 статей 14 штук имели версию ``.2``), а угадывать её нельзя.
    Прочие PDF в папке (``mmc1.pdf``, ``MOESM1_ESM.pdf``) — supplementary
    материалы, они не подходят.

    Args:
        pmcid (str): Идентификатор статьи в PMC (например, "PMC7584564").
        session (requests.Session | None): Сессия; по умолчанию создаётся своя.

    Returns:
        tuple[str | None, int | None]: (ключ объекта, размер в байтах).
        (None, None) — если статьи нет в Open Access Subset или S3 недоступен.
    """
    session = session or requests.Session()
    try:
        response = session.get(
            f"{PMC_OA_BUCKET_URL}/",
            params={
                "list-type": "2",
                "prefix": pmcid,
                "max-keys": S3_LIST_PAGE_SIZE,
            },
            headers=DEFAULT_HEADERS,
            timeout=60,
        )
    except requests.RequestException:
        return None, None

    if response.status_code != 200:
        return None, None

    keys = re.findall(r"<Key>([^<]+)</Key>", response.text)
    sizes = [int(size) for size in re.findall(r"<Size>(\d+)</Size>", response.text)]

    for key, size in zip(keys, sizes):
        folder, _, name = key.partition("/")
        if name.lower() == f"{folder.lower()}.pdf":
            return key, size

    return None, None


def build_pdf_url(key: str) -> str:
    """Публичный HTTPS-адрес файла по ключу объекта."""
    return f"{PMC_OA_BUCKET_URL}/{key}"


def is_valid_pdf(path: Path | str) -> bool:
    """Проверяет, что файл похож на целый PDF.

    Нужна из-за прерванных загрузок: если процесс убили посреди скачивания,
    на диске остаётся обрезанный файл. Без такой проверки следующий запуск
    принял бы его за готовый (статус ``exists``) и оставил битым.
    """
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size < 1024:
            return False
        with path.open("rb") as handle:
            if handle.read(len(PDF_MAGIC)) != PDF_MAGIC:
                return False
            handle.seek(max(path.stat().st_size - 4096, 0))
            tail = handle.read()
    except OSError:
        return False
    return b"%%EOF" in tail


def download(
    url: str,
    destination: Path | str,
    session: requests.Session | None = None,
    timeout: int = 180,
) -> tuple[bool, str]:
    """Скачивает файл потоком и проверяет, что это действительно PDF.

    Файл пишется во временное имя ``*.part`` и переименовывается только после
    успешной проверки: так прерванная загрузка не оставляет «готовый» обрезанный
    PDF, из-за которого следующий запуск решил бы, что файл уже скачан.

    Args:
        url (str): Адрес файла.
        destination (Path | str): Куда сохранять.
        session (requests.Session | None): Сессия; по умолчанию создаётся своя.
        timeout (int): Таймаут в секундах (на каждое чтение из сокета).

    Returns:
        tuple[bool, str]: (успех, пояснение). Неполный файл удаляется.
    """
    session = session or requests.Session()
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")

    try:
        with session.get(url, headers=DEFAULT_HEADERS, timeout=timeout, stream=True) as response:
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"

            head = b""
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    if not head:
                        head = chunk[: len(PDF_MAGIC)]
                    handle.write(chunk)
    except (requests.RequestException, OSError) as exc:
        partial.unlink(missing_ok=True)
        return False, f"{type(exc).__name__}: {exc}"

    if head != PDF_MAGIC:
        partial.unlink(missing_ok=True)
        return False, "ответ не является PDF"

    partial.replace(path)
    return True, "ok"


# --------------------------------------------------------------------------- #
# Пакетная загрузка для записей PubMedScraper
# --------------------------------------------------------------------------- #


def download_for_records(
    records: list[dict[str, Any]],
    output_dir: str | Path = "data/pubmed_data/pdfs",
    *,
    max_workers: int = 4,
    overwrite: bool = False,
    limit: int | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Дополняет записи PubMedScraper путями к PDF.

    На вход идут те же словари, что вернул ``perform_parsing``. Функция их
    мутирует и возвращает тот же список, добавляя поля:

        pmid, pmcid, doi, pdf_license, pdf_path, pdf_url, pdf_source,
        pdf_status (downloaded | exists | no_pmcid | not_in_oa_subset | error | skipped),
        pdf_error (только при ошибке).

    Файлы качаются по одному на PMCID: в датасете бывают дубликаты PMID,
    и второй раз тот же PDF не нужен. Существующий файл считается готовым только
    после проверки ``is_valid_pdf``: обрезанный после прерванной загрузки файл
    будет перекачан.

    Args:
        records (list[dict]): Записи из perform_parsing.
        output_dir (str | Path): Каталог для PDF.
        max_workers (int): Потоков загрузки. Default = 4.
        overwrite (bool): Перекачивать уже существующие файлы. Default = False.
        limit (int | None): Сколько файлов скачать за вызов (для пробных прогонов).
        session (requests.Session | None): Сессия; по умолчанию создаётся своя.

    Returns:
        list[dict]: Те же записи с добавленными полями.
    """
    session = session or requests.Session()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # 1. Разбираем записи и собираем PMCID.
    infos: list[PdfInfo] = []
    for record in records:
        info = PdfInfo(
            pmid=extract_pmid(record.get("pubmed_url")),
            doi=extract_doi(record.get("full_text_links")),
            pmcid=extract_pmcid(record.get("full_text_links")),
        )
        infos.append(info)

    resolved = lookup_ids([info.pmid for info in infos], session)
    for info in infos:
        meta = resolved.get(info.pmid or "", {})
        info.pmcid = info.pmcid or meta.get("pmcid")
        info.doi = info.doi or meta.get("doi")
        info.license = meta.get("license")
        if not info.pmcid:
            info.status = STATUS_NO_PMCID

    # 2. Для каждого PMCID выясняем, есть ли файл в Open Access Subset.
    #    Запросы независимы, поэтому идём в несколько потоков: 675 PMCID
    #    последовательно — это ~6 минут, в 8 потоков — меньше минуты.
    pmcids = list(
        dict.fromkeys(info.pmcid for info in infos if info.pmcid and info.status != STATUS_NO_PMCID)
    )
    keys: dict[str, str] = {}
    if pmcids:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for pmcid, (key, _size) in zip(pmcids, pool.map(lambda p: find_pdf_key(p, session), pmcids)):
                keys[pmcid] = key or ""

    for info in infos:
        if info.status == STATUS_NO_PMCID or not info.pmcid:
            continue
        if not keys.get(info.pmcid):
            info.status = STATUS_NOT_IN_OA

    # 3. Один файл на PMCID; путь общий для всех записей с этим PMCID.
    targets: dict[str, Path] = {}
    for info in infos:
        if info.pmcid and keys.get(info.pmcid):
            targets.setdefault(info.pmcid, output / Path(keys[info.pmcid]).name)
            info.status = "pending"

    planned = list(targets.items())
    if limit is not None:
        planned = planned[: max(limit, 0)]

    def work(item: tuple[str, Path]) -> tuple[str, bool, str]:
        pmcid, path = item
        if path.exists() and not overwrite and is_valid_pdf(path):
            return pmcid, True, STATUS_EXISTS
        return pmcid, *download(build_pdf_url(keys[pmcid]), path, session)

    outcomes: dict[str, tuple[bool, str]] = {}
    if planned:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for pmcid, ok, note in pool.map(work, planned):
                outcomes[pmcid] = (ok, note)

    # 4. Переносим результат в записи.
    for info in infos:
        if info.status != "pending":
            continue
        pmcid = info.pmcid or ""
        if pmcid not in outcomes:
            info.status = STATUS_SKIPPED
            continue
        ok, note = outcomes[pmcid]
        if ok:
            info.status = note if note == STATUS_EXISTS else STATUS_DOWNLOADED
            info.path = str(targets[pmcid])
            info.url = build_pdf_url(keys[pmcid])
            info.source = "pmc_oa_s3"
        else:
            info.status = STATUS_ERROR
            info.error = note

    for record, info in zip(records, infos):
        record["pmid"] = info.pmid
        record["pmcid"] = info.pmcid
        record["doi"] = info.doi
        record["pdf_license"] = info.license
        record["pdf_path"] = info.path
        record["pdf_url"] = info.url
        record["pdf_source"] = info.source
        record["pdf_status"] = info.status
        if info.error:
            record["pdf_error"] = info.error

    return records
