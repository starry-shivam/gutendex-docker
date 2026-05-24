#!/usr/bin/env python
"""
Gutendex catalog update script.
Downloads and processes Project Gutenberg catalog data.
"""

import os
import sys
import json
import re
import shutil
import tarfile
import time
from pathlib import Path
from datetime import datetime
from typing import Set, Dict, Optional, List
from subprocess import run

import requests
from requests.adapters import HTTPAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add parent directory to path for app imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models import (
    Base, Book, Person, Bookshelf, Language, Subject, Summary, Format
)
from catalog.utils import get_book

# Configuration
TEMP_PATH = os.getenv("CATALOG_TEMP_DIR", "/app/data/catalog/temp")
CATALOG_RDF_DIR = os.getenv("CATALOG_RDF_DIR", "/app/data/catalog/rdf")
LOG_DIRECTORY = os.getenv("CATALOG_LOG_DIR", "/app/data/catalog/logs")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////app/data/gutendex.db")

URL = 'https://gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2'
DOWNLOAD_PATH = os.path.join(TEMP_PATH, 'catalog.tar.bz2')
DOWNLOAD_CHUNK_SIZE = 4 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_MAX_ATTEMPTS = 3

MOVE_SOURCE_PATH = os.path.join(TEMP_PATH, 'cache/epub')
MOVE_TARGET_PATH = CATALOG_RDF_DIR

# Database setup
engine = create_engine(DATABASE_URL)
Base.metadata.create_all(bind=engine)
SessionLocal = sessionmaker(bind=engine)

# Logging
LOG_FILE_NAME = datetime.now().strftime('%Y-%m-%d_%H%M%S') + '.txt'
LOG_PATH = os.path.join(LOG_DIRECTORY, LOG_FILE_NAME)


def log(*args):
    """Print and log messages."""
    message = ' '.join(str(arg) for arg in args)
    print(message, flush=True)
    if not os.path.exists(LOG_DIRECTORY):
        os.makedirs(LOG_DIRECTORY)
    with open(LOG_PATH, 'a') as log_file:
        log_file.write(message + '\n')


def format_file_size(size_in_bytes: int) -> str:
    """Convert bytes to human readable format."""
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(max(size_in_bytes, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == 'B':
                return f'{int(size)} {unit}'
            return f'{size:.2f} {unit}'
        size /= 1024


def get_directory_set(path: str) -> Set[str]:
    """Get set of subdirectory names in a path."""
    directory_set = set()
    if not os.path.exists(path):
        return directory_set
    for directory_item in os.listdir(path):
        item_path = os.path.join(path, directory_item)
        if os.path.isdir(item_path):
            directory_set.add(directory_item)
    return directory_set


def download_file_with_progress(url: str, destination_path: str) -> Dict:
    """Download file with progress reporting and resume capability."""
    started_at = time.monotonic()
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    browser_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }

    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        downloaded = (
            os.path.getsize(destination_path)
            if os.path.exists(destination_path)
            else 0
        )

        headers = dict(browser_headers)

        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"
            mode = "ab"
        else:
            mode = "wb"

        try:
            log(f"Download attempt {attempt}/{DOWNLOAD_MAX_ATTEMPTS}")

            with session.get(
                url,
                headers=headers,
                stream=True,
                timeout=(10, DOWNLOAD_TIMEOUT_SECONDS),
                allow_redirects=True,
            ) as response:

                response.raise_for_status()

                content_range = response.headers.get("Content-Range")
                content_length = response.headers.get("Content-Length")

                total_size = None

                if content_range and "/" in content_range:
                    total_size = int(content_range.split("/")[-1])
                elif content_length:
                    total_size = downloaded + int(content_length)

                if total_size:
                    log(f"Expected size: {format_file_size(total_size)}")
                else:
                    log("Expected size: unknown")

                last_report = time.monotonic()
                last_downloaded = downloaded

                with open(
                    destination_path,
                    mode,
                    buffering=8 * 1024 * 1024,
                ) as f:

                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if not chunk:
                            continue

                        f.write(chunk)
                        downloaded += len(chunk)

                        now = time.monotonic()

                        if now - last_report >= 2:
                            instant_elapsed = now - last_report
                            speed = (downloaded - last_downloaded) / max(instant_elapsed, 1e-6)

                            if total_size:
                                percent = downloaded * 100 / total_size
                                log(
                                    f"{percent:6.2f}% | "
                                    f"{format_file_size(downloaded)} / "
                                    f"{format_file_size(total_size)} | "
                                    f"{format_file_size(speed)}/s"
                                )
                            else:
                                log(
                                    f"{format_file_size(downloaded)} | "
                                    f"{format_file_size(speed)}/s"
                                )

                            last_report = now
                            last_downloaded = downloaded

                if total_size and downloaded < total_size:
                    raise RuntimeError(
                        f"Download incomplete ({downloaded}/{total_size})"
                    )

                return {
                    "downloaded": downloaded,
                    "seconds": time.monotonic() - started_at,
                }

        except Exception as e:
            log(f"Attempt {attempt} failed: {e}")
            if attempt >= DOWNLOAD_MAX_ATTEMPTS:
                raise
            log("Retrying...")
            time.sleep(2)

    raise RuntimeError("Download failed")


def get_changed_book_ids(rsync_output_lines: List[str]) -> Set[int]:
    """Extract book IDs from rsync output."""
    book_ids = set()
    for line in rsync_output_lines:
        match = re.match(r'^(\d+)/', line.strip())
        if match:
            book_ids.add(int(match.group(1)))
    return book_ids


def get_or_create_person(
    db,
    data: Dict,
    cache: Optional[Dict] = None
) -> Person:
    """Get or create a Person in the database."""
    person_key = (data['name'], data['birth'], data['death'])

    if cache is not None and person_key in cache:
        return cache[person_key]

    person = db.query(Person).filter(
        Person.name == data['name'],
        Person.birth_year == data['birth'],
        Person.death_year == data['death']
    ).first()

    if person is None:
        person = Person(
            name=data['name'],
            birth_year=data['birth'],
            death_year=data['death']
        )
        db.add(person)
        db.flush()

    if cache is not None:
        cache[person_key] = person

    return person


def get_or_create_bookshelf(db, name: str, cache: Dict) -> Bookshelf:
    """Get or create a Bookshelf in the database."""
    if name in cache:
        return cache[name]

    bookshelf = db.query(Bookshelf).filter(Bookshelf.name == name).first()
    if bookshelf is None:
        bookshelf = Bookshelf(name=name)
        db.add(bookshelf)
        db.flush()

    cache[name] = bookshelf
    return bookshelf


def get_or_create_language(db, code: str, cache: Dict) -> Language:
    """Get or create a Language in the database."""
    if code in cache:
        return cache[code]

    language = db.query(Language).filter(Language.code == code).first()
    if language is None:
        language = Language(code=code)
        db.add(language)
        db.flush()

    cache[code] = language
    return language


def get_or_create_subject(db, name: str, cache: Dict) -> Subject:
    """Get or create a Subject in the database."""
    if name in cache:
        return cache[name]

    subject = db.query(Subject).filter(Subject.name == name).first()
    if subject is None:
        subject = Subject(name=name)
        db.add(subject)
        db.flush()

    cache[name] = subject
    return subject


def put_catalog_in_db(db, book_ids: Optional[List[int]] = None) -> Dict:
    """Import catalog data into the database."""
    if book_ids is None:
        book_ids = []
        for directory_item in os.listdir(CATALOG_RDF_DIR):
            item_path = os.path.join(CATALOG_RDF_DIR, directory_item)
            if os.path.isdir(item_path):
                try:
                    book_id = int(directory_item)
                    book_ids.append(book_id)
                except ValueError:
                    pass
    else:
        # Keep only valid numeric IDs that still exist on disk.
        validated_book_ids = []
        for id in book_ids:
            try:
                normalized_id = int(id)
            except (TypeError, ValueError):
                log(f'Skipping invalid catalog directory name: {id}')
                continue

            if os.path.isdir(os.path.join(CATALOG_RDF_DIR, str(normalized_id))):
                validated_book_ids.append(normalized_id)

        book_ids = validated_book_ids

    book_ids.sort()
    total_books = len(book_ids)
    progress_step = max(1, total_books // 20) if total_books else 1

    stats = {
        'processed': 0,
        'created': 0,
        'updated': 0,
        'total': total_books,
    }
    started_at = time.monotonic()
    last_progress_at = started_at
    last_progress_index = 0

    person_cache = {}
    bookshelf_cache = {}
    language_cache = {}
    subject_cache = {}

    if total_books:
        log(f'Import queue size: {total_books} books')

    for index, book_id in enumerate(book_ids, start=1):
        if index == 1 or index % progress_step == 0 or index == total_books:
            progress = int(index * 100 / total_books)
            now = time.monotonic()
            elapsed = max(now - started_at, 1e-9)
            overall_rate = index / elapsed

            if last_progress_index > 0:
                recent_elapsed = max(now - last_progress_at, 1e-9)
                recent_rate = (index - last_progress_index) / recent_elapsed
                rate_text = (
                    f'overall {overall_rate:.1f} books/s; '
                    f'recent {recent_rate:.1f} books/s'
                )
            else:
                rate_text = f'overall {overall_rate:.1f} books/s; recent warming up'

            log(f'DB import progress: {index}/{total_books} ({progress}%) {rate_text}')
            last_progress_at = now
            last_progress_index = index

        book_path = os.path.join(
            CATALOG_RDF_DIR,
            str(book_id),
            f'pg{book_id}.rdf'
        )

        try:
            book_data = get_book(book_id, book_path)

            # Make/update the book
            book_in_db = db.query(Book).filter(Book.gutenberg_id == book_id).first()

            if book_in_db is not None:
                book_in_db.copyright = book_data['copyright']
                book_in_db.download_count = book_data['downloads']
                book_in_db.media_type = book_data['type']
                book_in_db.title = book_data['title']
                stats['updated'] += 1
            else:
                book_in_db = Book(
                    gutenberg_id=book_id,
                    copyright=book_data['copyright'],
                    download_count=book_data['downloads'],
                    media_type=book_data['type'],
                    title=book_data['title']
                )
                db.add(book_in_db)
                db.flush()
                stats['created'] += 1

            # Make/update authors
            authors = []
            for author in book_data['authors']:
                person = get_or_create_person(db, author, person_cache)
                authors.append(person)
            book_in_db.authors = authors

            # Make/update editors
            editors = []
            for editor in book_data['editors']:
                person = get_or_create_person(db, editor, person_cache)
                editors.append(person)
            book_in_db.editors = editors

            # Make/update translators
            translators = []
            for translator in book_data['translators']:
                person = get_or_create_person(db, translator, person_cache)
                translators.append(person)
            book_in_db.translators = translators

            # Make/update bookshelves
            bookshelves = []
            for shelf in book_data['bookshelves']:
                shelf_in_db = get_or_create_bookshelf(db, shelf, bookshelf_cache)
                bookshelves.append(shelf_in_db)
            book_in_db.bookshelves = bookshelves

            # Make/update formats
            existing_formats = {
                (f.mime_type, f.url): f
                for f in db.query(Format).filter(Format.book_id == book_in_db.id)
            }
            expected_formats = set(book_data['formats'].items())

            missing_formats = expected_formats - set(existing_formats.keys())
            for mime_type, url in missing_formats:
                format_obj = Format(book_id=book_in_db.id, mime_type=mime_type, url=url)
                db.add(format_obj)

            stale_format_ids = [
                f.id for (mime_type, url), f in existing_formats.items()
                if (mime_type, url) not in expected_formats
            ]
            if stale_format_ids:
                db.query(Format).filter(Format.id.in_(stale_format_ids)).delete()

            # Make/update languages
            languages = []
            for language in book_data['languages']:
                language_in_db = get_or_create_language(db, language, language_cache)
                languages.append(language_in_db)
            book_in_db.languages = languages

            # Make/update subjects
            subjects = []
            for subject in book_data['subjects']:
                subject_in_db = get_or_create_subject(db, subject, subject_cache)
                subjects.append(subject_in_db)
            book_in_db.subjects = subjects

            # Make/update summaries
            existing_summaries = {
                s.text: s
                for s in db.query(Summary).filter(Summary.book_id == book_in_db.id)
            }
            expected_summaries = set(book_data['summaries'])

            missing_summaries = expected_summaries - set(existing_summaries.keys())
            for summary_text in missing_summaries:
                summary_obj = Summary(book_id=book_in_db.id, text=summary_text)
                db.add(summary_obj)

            stale_summary_ids = [
                s.id for text, s in existing_summaries.items()
                if text not in expected_summaries
            ]
            if stale_summary_ids:
                db.query(Summary).filter(Summary.id.in_(stale_summary_ids)).delete()

            db.commit()
            stats['processed'] += 1

        except Exception as error:
            log(f'Error while putting book {book_id} in the database:')
            log(str(error))
            db.rollback()
            raise error

    duration = time.monotonic() - started_at
    stats['duration_seconds'] = duration
    return stats


def main():
    """Main catalog update function."""
    try:
        db = SessionLocal()
        script_started_at = time.monotonic()
        date_and_time = datetime.now().strftime('%H:%M:%S on %B %d, %Y')
        log(f'Starting script at {date_and_time}')

        log('Making temporary directory...')
        if os.path.exists(TEMP_PATH):
            log('Temporary path already exists; removing stale directory...')
            shutil.rmtree(TEMP_PATH)
        os.makedirs(TEMP_PATH)
        log(f'Temporary directory ready at {TEMP_PATH}')

        log('Downloading compressed catalog...')
        log(f'Source URL: {URL}')
        download_stats = download_file_with_progress(URL, DOWNLOAD_PATH)
        log(
            f'Download complete: {format_file_size(download_stats["downloaded"])} '
            f'in {download_stats["seconds"]:.1f}s'
        )

        log('Decompressing catalog...')
        if not os.path.exists(DOWNLOAD_PATH):
            raise RuntimeError(f'Downloaded catalog archive not found at {DOWNLOAD_PATH}')
        
        decompress_started_at = time.monotonic()
        try:
            with tarfile.open(DOWNLOAD_PATH, 'r:bz2') as tar_archive:
                tar_archive.extractall(path=TEMP_PATH, filter='data')
        except (tarfile.TarError, OSError) as error:
            raise RuntimeError(f'Failed to decompress catalog: {str(error)}')
        
        log(f'Decompression complete in {time.monotonic() - decompress_started_at:.1f}s')

        log('Detecting stale directories...')
        if not os.path.exists(MOVE_TARGET_PATH):
            os.makedirs(MOVE_TARGET_PATH)
        
        new_directory_set = get_directory_set(MOVE_SOURCE_PATH)
        old_directory_set = get_directory_set(MOVE_TARGET_PATH)
        stale_directory_set = old_directory_set - new_directory_set
        
        log(f'New directories found: {len(new_directory_set)}')
        log(f'Existing directories found: {len(old_directory_set)}')
        log(f'Stale directories detected: {len(stale_directory_set)}')

        log('Moving new catalog data...')
        for directory_item in os.listdir(MOVE_SOURCE_PATH):
            source = os.path.join(MOVE_SOURCE_PATH, directory_item)
            target = os.path.join(MOVE_TARGET_PATH, directory_item)
            if os.path.isdir(source):
                if os.path.exists(target):
                    shutil.rmtree(target)
                shutil.copytree(source, target)

        log('Removing stale directories...')
        for directory_item in stale_directory_set:
            stale_path = os.path.join(MOVE_TARGET_PATH, directory_item)
            if os.path.exists(stale_path):
                shutil.rmtree(stale_path)

        log('Removing temporary directory...')
        shutil.rmtree(TEMP_PATH)

        log('Putting catalog in database...')
        changed_book_ids = [int(directory_name) for directory_name in new_directory_set if directory_name.isdigit()]

        skipped_non_book_directories = [
            directory_name for directory_name in new_directory_set
            if not directory_name.isdigit()
        ]
        if skipped_non_book_directories:
            log(
                'Skipping non-book directories during DB import: '
                + ', '.join(sorted(skipped_non_book_directories))
            )

        stats = put_catalog_in_db(db, changed_book_ids)

        log(f'Database import complete:')
        log(f'  Processed: {stats["processed"]} books')
        log(f'  Created: {stats["created"]} books')
        log(f'  Updated: {stats["updated"]} books')
        log(f'  Duration: {stats["duration_seconds"]:.1f}s')

        elapsed = time.monotonic() - script_started_at
        log(f'Script complete in {elapsed:.1f}s')

        db.close()

    except Exception as e:
        log(f'Fatal error: {str(e)}')
        raise


if __name__ == '__main__':
    main()
