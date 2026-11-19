import requests

from bs4 import BeautifulSoup

from helpers.pubmed import PubMedSession


class PubMedScraper:
    """

    Класс для сбора информацию о статьях с сайта https://pubmed.ncbi.nlm.nih.gov.

    В отличии от остальных классов в scrapers/*, класс не наследуется от привычного BaseScraper,
    поскольку собирается информация другого порядка
    """

    DEFAULT_URL = "https://pubmed.ncbi.nlm.nih.gov"

    def __init__(self, start_url: str | None = None):

        self.session = PubMedSession()

        self.start_url = start_url

        print("PubMedScraper инициализирован!")

    def _get_bs_object(self, page_url: str) -> BeautifulSoup:
        """
        Метод для получения объекта BeautifulSoup.

        Args:
            page_url (str): URL-страницы с сайта PubMed

        Args:
            BeautifulSoup: Объект BeautifulSoup. При status_code != 200, возвращается пустой объект BS.
        """

        result = self.session.get_page(url=page_url)

        if result.status_code != 200:

            print("Произошла ошибка при получении объекта bs4")

            return (
                BeautifulSoup()
            )  # Возвращаем пустой BeautifulSoup, чтобы при ошибки в данном методе, остальные методы не падали с ошибкой

        bs_object = BeautifulSoup(result.text, "html.parser")

        return bs_object

    def _parse_heading(self, bs_object: BeautifulSoup) -> str | None:
        """
        Метод для автоматизированного сбора названия статьи.

        Args:
            bs_object (BeautifulSoup): Объект BeautifulSoup.

        Returns:
            str | None: Название статьи или None, если не удалось выделить название.
        """

        title_tag = bs_object.find("h1", class_="heading-title")

        if title_tag is None:

            print("Не удалось выделить заголовок!")
            return None

        final_title = title_tag.get_text(strip=True)

        return final_title

    def _parse_authors(self, bs_object: BeautifulSoup) -> str | None:
        """
        Метод для автоматизированного сбора информации об авторах статьи.

        Args:
            bs_object (BeautifulSoup): Объект BeautifulSoup.

        Returns:
            str | None: Авторы статьи в формате str (sep = ',') или None, если авторов выделить не удалось.
        """

        authors_tag = bs_object.find("div", class_="authors-list")

        if authors_tag is None:

            print("Не удалось выделить автора!")
            return None

        aut = authors_tag.find_all("span", "authors-list-item")

        final_authors = ", ".join(
            [x.find("a").get("data-ga-label", "").strip() for x in aut]
        )

        return final_authors

    def _parse_abstract(self, bs_object: BeautifulSoup) -> str | None:
        """
        Метод для автоматизированного сбора аннотации статьи.

        Args:
            bs_object (BeautifulSoup): Объект BeautifulSoup

        Returns:
            str | None: Аннотация в формате str или None, если не удалось выделить аннотацию.
        """

        abstract_tag = bs_object.find("div", class_="abstract-content")

        if abstract_tag is None:

            print("Не удалось выделить аннотацию!")

            return None

        final_abstract = abstract_tag.get_text(strip=True)

        return final_abstract

    def _parse_full_text_links(self, bs_object: BeautifulSoup) -> list[str] | None:
        """
        Метод для автоматизированного сбора ссылок на полный текст статьи.

        Args:
            bs_object (BeautifulSoup): Объект BeautifulSoup.

        Returns:
            list[str] | None: Массив URL'ов на полный текст статьи или None, если не удалось извлечь ссылки.
        """

        links_tags = bs_object.select("div.full-text-links a.link-item")

        if links_tags is None:

            print("Не удалось выделить ссылки на полные текста")
            return None

        final_links_list = [x.get("href") for x in links_tags]

        return final_links_list

    def _parse_current_page_article_ids(
        self, bs_object: BeautifulSoup
    ) -> list[str] | None:
        """
        Метод для автоматизированного сбора всех URL статей на текущей странице.

        Args:
            bs_object (BeautifulSoup): Объект BeautifulSoup.

        Returns:
            list[str]: Массив строк, содержащий URL статей на текущей странице или None, если не удалось выделить.
        """

        links_tag = bs_object.find_all("a", class_="docsum-title")

        if links_tag is None:
            print("На странице не найдено ссылок!")
            return None

        final_links_list = [self.DEFAULT_URL + str(x.get("href")) for x in links_tag]

        return final_links_list

    def _get_next_page_url(self, bs_object: BeautifulSoup) -> str | None:
        """
        Метод для автоматизированного сбора URL следующей страницы.

        К примеру, если мы находимся на ?page=1, то метод соберет ?page=2.

        Args:
            bs_object (BeautifulSoup): Объект BeautifulSoup

        Returns:
            str | None: URL следующей страницы в формате строки или None, если не удалось выделить URL.
        """

        next_page_tag = bs_object.find(
            "div", class_="search-results-chunk results-chunk"
        )

        if next_page_tag is None:

            print("Не удалось обнаружить ссылку на следующую страницу!")
            # В данном контексте имеется ввиду в принципе ТЭГ следующей страницы
            # Вероятнее всего, страница не изначально не имела данного элемента

            return None

        next_page_url = next_page_tag.get("data-next-page-url", None)

        if next_page_url is None:

            print("Не удалось обнаружить ссылку на следующую страницу!")
            # В данном контексте имеется ввиду href (URL) следующей страницы.
            # Если не удалось обнаружить следующий по счету URL, значит дошли до верхней границы

            return None

        return self.DEFAULT_URL + next_page_url

    def _parse_single_article_information(self, article_url: str) -> dict[str, str]:
        """
        Метод автоматизированного сбора доступной информации по ОДНОЙ конкретной статье.

        В методе использованные следующие методы текущего класса:
            - _get_bs_object: Для получения объекта BeautifulSoup.
            - _parse_authors: Для получения авторов статьи.
            - _parse_abstract: Для получения аннотации статьи.
            - _parse_heading: Для получения названия статьи.
            - _parse_full_text_links: Для получения ссылок на полный текст статьи.
        """

        result = self._get_bs_object(page_url=article_url)  # bs_object

        authors = self._parse_authors(bs_object=result)

        abstract = self._parse_abstract(bs_object=result)

        heading = self._parse_heading(bs_object=result)

        links = self._parse_full_text_links(bs_object=result)

        result_dict = {
            "title": heading,
            "abstract": abstract,
            "authors": authors,
            "full_text_links": links,
            "pubmed_url": article_url,
        }

        return result_dict

    def perform_parsing(self, start_url: str | None = None, n_articles: int = 100):
        """
        Метод для сбора информации по N-статьям
        с заранее выбранной стартовой страницей (start_url).

        start_url может быть задан либо в конструкторе

        I. Установление текущей страницы
        II. Сбор всех новостей с текущей страницы
        III. Поиск ссылки на новую страницу
        IV. Переход на новую страницу
        * Повторять до достижения желаемого кол-ва новостей

        TODO
        """

        if (start_url is None) and (self.start_url is None):
            raise ValueError("Отсутствует начальный URL!")

        if (start_url is not None) and (self.start_url is not None):
            raise ValueError("start_url задан дважды!")

        if self.start_url is not None:
            start_url = self.start_url

        collected_articles = []

        while len(collected_articles) < n_articles:

            print(f"Сбор новостей со страницы со следующим URL: {start_url}")

            # Достаем информацию по статьям на текущей странице

            current_page_bs_object = self._get_bs_object(
                page_url=start_url
            )  # Объект BeautifulSoup текущей страницы

            current_page_articles = self._parse_current_page_article_ids(
                bs_object=current_page_bs_object
            )  # Список новостей с текущей страницы

            for url in current_page_articles:

                temp_article_info = self._parse_single_article_information(url)  # dict

                collected_articles.append(temp_article_info)

            # Узнаем URL следующей страницы

            next_page_url = self._get_next_page_url(bs_object=current_page_bs_object)

            print(f"URL следующей страницы")

            start_url = next_page_url  # Меняем URL

            print(f"Собрано новостей: {len(collected_articles)} Из {n_articles}")

        print("Сбор информации закончен!")
        print(f"Собрано статей: {len(collected_articles)}")

        return collected_articles
