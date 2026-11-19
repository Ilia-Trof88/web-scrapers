import requests

from bs4 import BeautifulSoup

from helpers.pubmed import PubMedSession, fetch_articles

class PubMedScraper:
    '''
    Класс не наследуется от привычного BaseScraper,
    поскольку собирает не текстовые новости
    '''

    def __init__(self,
                 start_url: str | None = None):

        self.session = PubMedSession()

        print('PubMedScraper инициализирован!')

    def _get_bs_object(self,
                       page_url: str) -> BeautifulSoup | None:
        '''
        TODO
        '''

        result = self.session.get_page(url = page_url)

        if result.status_code != 200:

            print('Произошла ошибка при получении объекта bs4')

            raise ValueError('Произошла ошибка при создании объекта BeautifulSoup!')

        bs_object = BeautifulSoup(result.text, 'html.parser')

        return bs_object
    
    def _parse_heading(self,
                       bs_object: BeautifulSoup) -> str | None:
        '''
        TODO
        '''

        title_tag = bs_object.find('h1', class_='heading-title')

        if title_tag is None:

            print('Не удалось выделить заголовок!')
            return None

        final_title = title_tag.get_text(strip = True)

        return final_title

    def _parse_authors(self,
                       bs_object) -> str | None:
        '''
        TODO
        '''

        authors_tag = bs_object.find('div', class_='authors-list')

        if authors_tag is None:

            print('Не удалось выделить автора!')
            return None

        aut = authors_tag.find_all('span', 'authors-list-item')

        final_authors = ', '.join([x.find('a').get('data-ga-label', '').strip() for x in aut])

        return final_authors

    def _parse_abstract(self,
                        bs_object: BeautifulSoup) -> str | None:
        '''
        TODO
        '''

        abstract_tag = bs_object.find("div", class_="abstract-content")

        if abstract_tag is None:

            print('Не удалось выделить аннотацию!')
            
            return None

        final_abstract = abstract_tag.get_text(strip = True)

        return final_abstract

    def _fetch_single_article(self,
                              article_url: str) -> dict[str, str]:
        '''
        TODO
        '''
        
        result = self._get_bs_object(page_url = article_url)

        authors = self._parse_authors(bs_object = result)

        abstract = self._parse_abstract(bs_object = result)

        heading = self._parse_heading(bs_object = result)

        result_dict = {
            "title": heading,
            "abstract": abstract,
            "authors": authors
        }

        return result_dict
        
        