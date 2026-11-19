import pandas as pd

from scrapers.PubMedScraper import PubMedScraper

start_url = 'https://pubmed.ncbi.nlm.nih.gov/?term=covid%2019'

n_arts = 1000

scraper = PubMedScraper(start_url = start_url)

articles_info = scraper.perform_parsing(n_articles = n_arts)

df = pd.DataFrame(articles_info)

df.to_csv('data/pubmed_data/covid_data.csv', index = False)

print('Сбор информации успешно выполнен, данные сохранены!')