import os
import re
import time

import ijson
import requests
from dotenv import load_dotenv

from neo4j import GraphDatabase

load_dotenv()

database_url = os.getenv('DATABASE_URL', 'bolt://localhost:7687')
database_user = os.getenv('USER', 'neo4j')
database_secret_key = os.getenv('SECRET_KEY', 'testtest')
MAX_ITEMS = int(os.getenv('MAX_ITEMS_PROCESSED', '0'))

FILE_URL = os.getenv('FILE_URL')

driver = GraphDatabase.driver(database_url, auth=(database_user, database_secret_key))

### Cleaner
number_int_pattern = re.compile(r'NumberInt\(([^()]+)\)')
digit_pattern = re.compile(r'-?\d+')


def fix_numberint(text):
    def replace(match):
        value = match.group(1)
        if digit_pattern.fullmatch(value):
            return value
        else:
            return '0'

    return number_int_pattern.sub(replace, text)


def clean_csv_field(text):
    """Clean text field for safe CSV writing - very aggressive cleaning"""
    if not text:
        return ''

    # Convert to string and strip whitespace
    text = str(text).strip()

    # Replace line breaks and carriage returns with spaces
    text = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')

    # Remove control characters
    text = ''.join(char for char in text if ord(char) >= 32)

    # Handle quotes very aggressively - double escape them for CSV
    text = text.replace('"', '""')  # CSV standard: double quotes become double-double quotes

    # Remove any remaining problematic characters
    text = text.replace('\x00', '')  # Remove null bytes
    text = text.replace('\\', '/')  # Replace backslashes with forward slashes

    # Limit length to prevent extremely long fields
    if len(text) > 2000:
        text = text[:1997] + "..."

    return text


class CleanedStream:
    def __init__(self, response):
        self.iter_content = response.iter_content(chunk_size=16384 * 4, decode_unicode=True)
        self.buffer = ''

    def read(self, size=-1):
        while size < 0 or len(self.buffer) < size:
            try:
                chunk = next(self.iter_content)
                combined = self.buffer + chunk
                cleaned = fix_numberint(combined)
                self.buffer = cleaned
            except StopIteration:
                break

        if size < 0:
            result, self.buffer = self.buffer, ''
        else:
            result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result


def download_data(url, csv_file):
    csv_dir = csv_file

    articles_csv = os.path.join(csv_dir, "articles.csv")
    authors_csv = os.path.join(csv_dir, "authors.csv")
    authored_csv = os.path.join(csv_dir, "authored.csv")
    cites_csv = os.path.join(csv_dir, "cites.csv")

    # Open files with UTF-8 encoding
    articles_file = open(articles_csv, 'w', newline='', encoding='utf-8')
    authors_file = open(authors_csv, 'w', newline='', encoding='utf-8')
    authored_file = open(authored_csv, 'w', newline='', encoding='utf-8')
    cites_file = open(cites_csv, 'w', newline='', encoding='utf-8')

    # Write headers manually to avoid any CSV writer issues
    articles_file.write('id,title\n')
    authors_file.write('id,name\n')
    authored_file.write('author_id,article_id\n')
    cites_file.write('citing_id,cited_id\n')

    seen_authors = set()

    count = 1
    count_authored = 0
    count_cited = 0
    try:
        with requests.get(url, stream=True) as response:
            clean = CleanedStream(response)

            for article in ijson.items(clean, "item"):
                count += 1

                if '_id' not in article:
                    continue

                article_id = clean_csv_field(article['_id'])
                title = clean_csv_field(article.get('title', 'No title'))

                # Write manually with proper escaping
                articles_file.write(f'"{article_id}","{title}"\n')

                if 'authors' in article and article['authors']:
                    for author in article['authors']:
                        if '_id' in author and 'name' in author:
                            author_id = clean_csv_field(author['_id'])
                            author_name = clean_csv_field(author.get('name', ''))

                            if author_id not in seen_authors:
                                authors_file.write(f'"{author_id}","{author_name}"\n')
                                seen_authors.add(author_id)

                            authored_file.write(f'"{author_id}","{article_id}"\n')
                            count_authored+=1


                if 'references' in article and article['references']:
                    for ref_id in article['references']:
                        if ref_id and ref_id != article_id:
                            clean_ref_id = clean_csv_field(ref_id)
                            cites_file.write(f'"{article_id}","{clean_ref_id}"\n')
                        count_cited += 1

                if count % 100000 == 0:
                    print(f'processed {count}')
                    # Flush files periodically
                    articles_file.flush()
                    authors_file.flush()
                    authored_file.flush()
                    cites_file.flush()

                if 0 < MAX_ITEMS <= count:
                    print(f'break at {count}')
                    break

    finally:
        # Ensure files are properly closed
        articles_file.close()
        authors_file.close()
        authored_file.close()
        cites_file.close()

    print(f'finished {count} articles')
    print(f'finished {count_cited} cited')
    print(f'finished {count_authored} authored')

    return (articles_csv, authors_csv, authored_csv, cites_csv)


def setup_indexes():
    """Create indexes and constraints for optimal performance"""
    with driver.session() as session:
        print("Setting up indexes and constraints...")

        # Create constraints (which automatically create indexes)
        queries = [
            "CREATE CONSTRAINT article_id IF NOT EXISTS FOR (a:ARTICLE) REQUIRE a.id IS UNIQUE",
            "CREATE CONSTRAINT author_id IF NOT EXISTS FOR (a:AUTHOR) REQUIRE a.id IS UNIQUE",

            # Additional indexes for performance
            "CREATE INDEX article_title IF NOT EXISTS FOR (a:ARTICLE) ON (a.title)",
            "CREATE INDEX author_name IF NOT EXISTS FOR (a:AUTHOR) ON (a.name)"
        ]

        for query in queries:
            try:
                session.run(query)
                print(f"✓ {query[:50]}...")
            except Exception as e:
                print(f"⚠ {query[:50]}... - {e}")


def process_articles(articles_csv_path):
    """Process articles using APOC with better error handling"""
    with driver.session() as session:
        print("Processing articles...")

        # First, let's create the articles
        result = session.run(
            """
            CALL apoc.periodic.iterate(
              "LOAD CSV WITH HEADERS FROM $csvPath AS row RETURN row",
              "MERGE (n:ARTICLE { id: row.id }) SET n.title = row.title",
              {batchSize: 10000, parallel: false, params: {csvPath: $csvPath}}
            )
            """,
            csvPath=articles_csv_path
        )

        summary = result.consume()
        print(f"Articles processed: {summary}")


def process_authors(authors_csv_path):
    """Process authors"""
    with driver.session() as session:
        print("Processing authors...")

        result = session.run(
            """
            CALL apoc.periodic.iterate(
              "LOAD CSV WITH HEADERS FROM $csvPath AS row RETURN row",
              "MERGE (n:AUTHOR { id: row.id }) SET n.name = row.name",
              {batchSize: 10000, parallel: false, params: {csvPath: $csvPath}}
            )
            """,
            csvPath=authors_csv_path
        )

        summary = result.consume()
        print(f"Authors processed: {summary}")


def process_authored_relationships(authored_csv_path):
    """Process authored relationships"""
    with driver.session() as session:
        print("Processing authored relationships...")

        result = session.run(
            """
            CALL apoc.periodic.iterate(
              "LOAD CSV WITH HEADERS FROM $csvPath AS row RETURN row",
              "MATCH (author:AUTHOR {id: row.author_id}), (article:ARTICLE {id: row.article_id}) MERGE (author)-[:AUTHORED]->(article)",
              {batchSize: 10000, parallel: false, params: {csvPath: $csvPath}}
            )
            """,
            csvPath=authored_csv_path
        )

        summary = result.consume()
        print(f"Authored relationships processed: {summary}")


def process_citations(cites_csv_path):
    """Process citation relationships"""
    with driver.session() as session:
        print("Processing citations...")

        result = session.run(
            """
            CALL apoc.periodic.iterate(
              "LOAD CSV WITH HEADERS FROM $csvPath AS row RETURN row",
              "
              OPTIONAL MATCH (citing:ARTICLE {id: row.citing_id})
              OPTIONAL MATCH (cited:ARTICLE {id: row.cited_id})
              WITH citing, cited, row
              WHERE citing IS NOT NULL AND cited IS NOT NULL
              MERGE (citing)-[:CITES]->(cited)
              ",
              {batchSize: 50000, parallel: false, params: {csvPath: $csvPath}}
            )
            """,
            csvPath=cites_csv_path
        )

        summary = result.consume()
        print(f"Citations processed: {summary}")


if __name__ == "__main__":
    print(f'Starting process at {time.time()}')

    start = time.time()

    articles_csv, authors_csv, authored_csv, cites_csv = download_data(url=FILE_URL, csv_file="/import")

    setup_indexes()

    # Process each CSV file separately with proper file:/// URLs
    process_articles("file:///articles.csv")
    process_authors("file:///authors.csv")
    process_authored_relationships("file:///authored.csv")
    process_citations("file:///cites.csv")

    driver.close()

    end = time.time()

    print(f'Finished in {end - start} seconds')