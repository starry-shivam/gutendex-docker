#!/usr/bin/env python
"""
Gutendex catalog update script.
Downloads and processes Project Gutenberg catalog data.
"""

import logging
import os
import shutil
import sqlite3
import sys
import tarfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

# Add parent directory to path for app imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.models import (
    Base,
    Book,
    Bookshelf,
    Format,
    Language,
    Person,
    Subject,
    Summary,
)
from catalog.utils import get_book


# =============================================================================
# Configuration
# =============================================================================


@dataclass(frozen=True)
class Config:
    temp_path: Path = Path(
        os.getenv("CATALOG_TEMP_DIR", "/app/data/catalog/temp")
    )
    rdf_dir: Path = Path(
        os.getenv("CATALOG_RDF_DIR", "/app/data/catalog/rdf")
    )
    log_dir: Path = Path(
        os.getenv("CATALOG_LOG_DIR", "/app/data/catalog/logs")
    )
    database_url: str = os.getenv(
        "DATABASE_URL",
        "sqlite:////app/data/gutendex.db",
    )

    download_url: str = (
        "https://gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2"
    )

    download_chunk_size: int = 4 * 1024 * 1024
    download_timeout_seconds: int = 60
    download_max_attempts: int = 3

    db_batch_size: int = 1000
    progress_interval_seconds: int = 5


CONFIG = Config()

DOWNLOAD_PATH = CONFIG.temp_path / "catalog.tar.bz2"
MOVE_SOURCE_PATH = CONFIG.temp_path / "cache" / "epub"
MOVE_TARGET_PATH = CONFIG.rdf_dir


# =============================================================================
# Logging
# =============================================================================


CONFIG.log_dir.mkdir(parents=True, exist_ok=True)

LOG_FILE_NAME = datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".txt"
LOG_PATH = CONFIG.log_dir / LOG_FILE_NAME


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger("gutendex_catalog")


# =============================================================================
# Database setup
# =============================================================================


engine = create_engine(
    CONFIG.database_url,
    future=True,
)


@event.listens_for(engine, "connect")
def configure_sqlite(dbapi_connection, connection_record):
    """Apply SQLite performance pragmas safely."""

    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()

        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA cache_size=-200000")
        cursor.execute("PRAGMA foreign_keys=ON")

        cursor.close()


Base.metadata.create_all(bind=engine)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


# =============================================================================
# Utility functions
# =============================================================================


LOCK_FILE_PATH = CONFIG.temp_path.parent / ".catalog_update.lock"


@contextmanager
def file_lock():
    """Prevent concurrent executions."""

    if LOCK_FILE_PATH.exists():
        raise RuntimeError(
            f"Another catalog update process is already running: {LOCK_FILE_PATH}"
        )

    LOCK_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE_PATH.write_text(str(os.getpid()))

    try:
        yield
    finally:
        try:
            LOCK_FILE_PATH.unlink(missing_ok=True)
        except Exception:
            pass


@contextmanager
def temporary_directory(path: Path):
    """Safely recreate temporary directory."""

    if path.exists():
        shutil.rmtree(path)

    path.mkdir(parents=True, exist_ok=True)

    try:
        yield path
    finally:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def format_file_size(size_in_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]

    size = float(max(size_in_bytes, 0))

    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024


def get_directory_set(path: Path) -> Set[str]:
    if not path.exists():
        return set()

    return {
        item.name
        for item in path.iterdir()
        if item.is_dir()
    }


# =============================================================================
# Download logic
# =============================================================================


SESSION = requests.Session()

adapter = HTTPAdapter(
    pool_connections=10,
    pool_maxsize=10,
    max_retries=0,
)

SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
}


def download_file_with_progress(url: str, destination_path: Path) -> Dict:
    """Download file with progress reporting and resume support."""

    started_at = time.monotonic()

    for attempt in range(1, CONFIG.download_max_attempts + 1):
        downloaded = destination_path.stat().st_size if destination_path.exists() else 0

        headers = dict(HEADERS)

        if downloaded > 0:
            headers["Range"] = f"bytes={downloaded}-"
            mode = "ab"
        else:
            mode = "wb"

        try:
            logger.info(
                "Download attempt %s/%s",
                attempt,
                CONFIG.download_max_attempts,
            )

            with SESSION.get(
                url,
                headers=headers,
                stream=True,
                timeout=(10, CONFIG.download_timeout_seconds),
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

                logger.info(
                    "Expected size: %s",
                    format_file_size(total_size) if total_size else "unknown",
                )

                last_report = time.monotonic()
                last_downloaded = downloaded

                with open(destination_path, mode, buffering=8 * 1024 * 1024) as f:
                    for chunk in response.iter_content(
                        chunk_size=CONFIG.download_chunk_size
                    ):
                        if not chunk:
                            continue

                        f.write(chunk)
                        downloaded += len(chunk)

                        now = time.monotonic()

                        if now - last_report >= 2:
                            instant_elapsed = max(now - last_report, 1e-6)

                            speed = (
                                downloaded - last_downloaded
                            ) / instant_elapsed

                            if total_size:
                                percent = downloaded * 100 / total_size

                                logger.info(
                                    "%.2f%% | %s / %s | %s/s",
                                    percent,
                                    format_file_size(downloaded),
                                    format_file_size(total_size),
                                    format_file_size(speed),
                                )
                            else:
                                logger.info(
                                    "%s | %s/s",
                                    format_file_size(downloaded),
                                    format_file_size(speed),
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

        except Exception:
            logger.exception("Download attempt failed")

            if attempt >= CONFIG.download_max_attempts:
                raise

            logger.info("Retrying download...")
            time.sleep(2)

    raise RuntimeError("Download failed")


# =============================================================================
# Cache preload
# =============================================================================


class EntityCache:
    def __init__(self, db: Session):
        self.people: Dict[Tuple[str, int, int], Person] = {
            (p.name, p.birth_year, p.death_year): p
            for p in db.query(Person).all()
        }

        self.bookshelves: Dict[str, Bookshelf] = {
            b.name: b
            for b in db.query(Bookshelf).all()
        }

        self.languages: Dict[str, Language] = {
            language.code: language
            for language in db.query(Language).all()
        }

        self.subjects: Dict[str, Subject] = {
            s.name: s
            for s in db.query(Subject).all()
        }


# =============================================================================
# Entity helpers
# =============================================================================


def get_or_create_person(
    db: Session,
    cache: EntityCache,
    data: Dict,
) -> Person:
    key = (data["name"], data["birth"], data["death"])

    person = cache.people.get(key)

    if person is not None:
        return person

    person = Person(
        name=data["name"],
        birth_year=data["birth"],
        death_year=data["death"],
    )

    db.add(person)
    db.flush()

    cache.people[key] = person

    return person


def get_or_create_bookshelf(
    db: Session,
    cache: EntityCache,
    name: str,
) -> Bookshelf:
    shelf = cache.bookshelves.get(name)

    if shelf is not None:
        return shelf

    shelf = Bookshelf(name=name)

    db.add(shelf)
    db.flush()

    cache.bookshelves[name] = shelf

    return shelf


def get_or_create_language(
    db: Session,
    cache: EntityCache,
    code: str,
) -> Language:
    language = cache.languages.get(code)

    if language is not None:
        return language

    language = Language(code=code)

    db.add(language)
    db.flush()

    cache.languages[code] = language

    return language


def get_or_create_subject(
    db: Session,
    cache: EntityCache,
    name: str,
) -> Subject:
    subject = cache.subjects.get(name)

    if subject is not None:
        return subject

    subject = Subject(name=name)

    db.add(subject)
    db.flush()

    cache.subjects[name] = subject

    return subject


# =============================================================================
# Import logic
# =============================================================================


def put_catalog_in_db(
    db: Session,
    book_ids: Optional[List[int]] = None,
) -> Dict:
    """Import catalog data into the database."""

    if book_ids is None:
        book_ids = []

        for item in CONFIG.rdf_dir.iterdir():
            if item.is_dir() and item.name.isdigit():
                book_ids.append(int(item.name))

    book_ids.sort()

    total_books = len(book_ids)

    stats = {
        "processed": 0,
        "created": 0,
        "updated": 0,
        "total": total_books,
    }

    started_at = time.monotonic()
    last_progress_at = started_at

    logger.info("Preloading entity caches...")

    cache = EntityCache(db)

    logger.info("Import queue size: %s books", total_books)

    for index, book_id in enumerate(book_ids, start=1):
        now = time.monotonic()

        if now - last_progress_at >= CONFIG.progress_interval_seconds:
            elapsed = max(now - started_at, 1e-9)
            rate = index / elapsed

            logger.info(
                "DB import progress: %s/%s (%.2f%%) %.1f books/s",
                index,
                total_books,
                (index * 100 / total_books) if total_books else 0,
                rate,
            )

            last_progress_at = now

        book_path = CONFIG.rdf_dir / str(book_id) / f"pg{book_id}.rdf"

        try:
            book_data = get_book(book_id, str(book_path))

            book_in_db = (
                db.query(Book)
                .filter(Book.gutenberg_id == book_id)
                .first()
            )

            if book_in_db is None:
                book_in_db = Book(
                    gutenberg_id=book_id,
                    copyright=book_data["copyright"],
                    download_count=book_data["downloads"],
                    media_type=book_data["type"],
                    title=book_data["title"],
                )

                db.add(book_in_db)
                db.flush()

                stats["created"] += 1

            else:
                book_in_db.copyright = book_data["copyright"]
                book_in_db.download_count = book_data["downloads"]
                book_in_db.media_type = book_data["type"]
                book_in_db.title = book_data["title"]

                stats["updated"] += 1

            # Authors
            book_in_db.authors = [
                get_or_create_person(db, cache, author)
                for author in book_data["authors"]
            ]

            # Editors
            book_in_db.editors = [
                get_or_create_person(db, cache, editor)
                for editor in book_data["editors"]
            ]

            # Translators
            book_in_db.translators = [
                get_or_create_person(db, cache, translator)
                for translator in book_data["translators"]
            ]

            # Bookshelves
            book_in_db.bookshelves = [
                get_or_create_bookshelf(db, cache, shelf)
                for shelf in book_data["bookshelves"]
            ]

            # Languages
            book_in_db.languages = [
                get_or_create_language(db, cache, lang)
                for lang in book_data["languages"]
            ]

            # Subjects
            book_in_db.subjects = [
                get_or_create_subject(db, cache, subject)
                for subject in book_data["subjects"]
            ]

            # Formats
            existing_formats = {
                (f.mime_type, f.url): f
                for f in db.query(Format)
                .filter(Format.book_id == book_in_db.id)
            }

            expected_formats = set(book_data["formats"].items())

            new_format_rows = [
                {
                    "book_id": book_in_db.id,
                    "mime_type": mime_type,
                    "url": url,
                }
                for mime_type, url in expected_formats
                if (mime_type, url) not in existing_formats
            ]

            if new_format_rows:
                db.bulk_insert_mappings(
                    Format,
                    new_format_rows,
                )

            stale_format_ids = [
                obj.id
                for key, obj in existing_formats.items()
                if key not in expected_formats
            ]

            if stale_format_ids:
                db.query(Format).filter(
                    Format.id.in_(stale_format_ids)
                ).delete(synchronize_session=False)

            # Summaries
            existing_summaries = {
                s.text: s
                for s in db.query(Summary)
                .filter(Summary.book_id == book_in_db.id)
            }

            expected_summaries = set(book_data["summaries"])

            new_summary_rows = [
                {
                    "book_id": book_in_db.id,
                    "text": summary_text,
                }
                for summary_text in expected_summaries
                if summary_text not in existing_summaries
            ]

            if new_summary_rows:
                db.bulk_insert_mappings(
                    Summary,
                    new_summary_rows,
                )

            stale_summary_ids = [
                obj.id
                for text, obj in existing_summaries.items()
                if text not in expected_summaries
            ]

            if stale_summary_ids:
                db.query(Summary).filter(
                    Summary.id.in_(stale_summary_ids)
                ).delete(synchronize_session=False)

            stats["processed"] += 1

            # Batch commit for huge speedups
            if index % CONFIG.db_batch_size == 0:
                db.commit()
                db.expire_all()

                logger.info(
                    "Committed batch at %s books",
                    index,
                )

        except Exception:
            logger.exception(
                "Error while importing book %s",
                book_id,
            )

            db.rollback()
            raise

    db.commit()

    duration = time.monotonic() - started_at

    stats["duration_seconds"] = duration

    return stats


# =============================================================================
# Archive extraction
# =============================================================================


SAFE_TAR_TYPES = {
    tarfile.REGTYPE,
    tarfile.AREGTYPE,
    tarfile.DIRTYPE,
}


def safe_extract_tar(archive_path: Path, destination: Path):
    """Safely extract tar archive."""

    with tarfile.open(archive_path, "r:bz2") as tar:
        for member in tar.getmembers():
            if member.type not in SAFE_TAR_TYPES:
                raise RuntimeError(
                    f"Unsafe tar member detected: {member.name}"
                )

            target_path = destination / member.name

            if not str(target_path.resolve()).startswith(
                str(destination.resolve())
            ):
                raise RuntimeError(
                    f"Unsafe extraction path detected: {member.name}"
                )

        tar.extractall(destination, filter="data")


# =============================================================================
# Main
# =============================================================================


def main():
    script_started_at = time.monotonic()

    logger.info(
        "Starting script at %s",
        datetime.now().strftime("%H:%M:%S on %B %d, %Y"),
    )

    with file_lock():
        with temporary_directory(CONFIG.temp_path):
            db = SessionLocal()

            try:
                # Download
                logger.info("Downloading compressed catalog...")
                logger.info("Source URL: %s", CONFIG.download_url)

                download_stats = download_file_with_progress(
                    CONFIG.download_url,
                    DOWNLOAD_PATH,
                )

                logger.info(
                    "Download complete: %s in %.1fs",
                    format_file_size(download_stats["downloaded"]),
                    download_stats["seconds"],
                )

                # Extract
                logger.info("Decompressing catalog...")

                extract_started = time.monotonic()

                safe_extract_tar(
                    DOWNLOAD_PATH,
                    CONFIG.temp_path,
                )

                logger.info(
                    "Decompression complete in %.1fs",
                    time.monotonic() - extract_started,
                )

                # Detect stale directories
                logger.info("Detecting stale directories...")

                MOVE_TARGET_PATH.mkdir(parents=True, exist_ok=True)

                new_directory_set = get_directory_set(MOVE_SOURCE_PATH)
                old_directory_set = get_directory_set(MOVE_TARGET_PATH)

                stale_directory_set = old_directory_set - new_directory_set

                logger.info(
                    "New directories found: %s",
                    len(new_directory_set),
                )

                logger.info(
                    "Existing directories found: %s",
                    len(old_directory_set),
                )

                logger.info(
                    "Stale directories detected: %s",
                    len(stale_directory_set),
                )

                # Atomic replacement moves
                logger.info("Moving new catalog data...")

                for source_dir in MOVE_SOURCE_PATH.iterdir():
                    if not source_dir.is_dir():
                        continue

                    target_dir = MOVE_TARGET_PATH / source_dir.name
                    temp_target = target_dir.with_suffix(".tmp")

                    if temp_target.exists():
                        shutil.rmtree(temp_target)

                    shutil.move(str(source_dir), str(temp_target))

                    if target_dir.exists():
                        shutil.rmtree(target_dir)

                    os.replace(temp_target, target_dir)

                # Remove stale dirs
                logger.info("Removing stale directories...")

                for stale_name in stale_directory_set:
                    stale_path = MOVE_TARGET_PATH / stale_name

                    if stale_path.exists():
                        shutil.rmtree(stale_path)

                # Import into DB
                logger.info("Putting catalog in database...")

                changed_book_ids = sorted(
                    int(name)
                    for name in new_directory_set
                    if name.isdigit()
                )

                skipped_non_book_directories = sorted(
                    name
                    for name in new_directory_set
                    if not name.isdigit()
                )

                if skipped_non_book_directories:
                    logger.info(
                        "Skipping non-book directories during DB import: %s",
                        ", ".join(skipped_non_book_directories),
                    )

                stats = put_catalog_in_db(
                    db,
                    changed_book_ids,
                )

                logger.info("Database import complete:")
                logger.info("  Processed: %s books", stats["processed"])
                logger.info("  Created: %s books", stats["created"])
                logger.info("  Updated: %s books", stats["updated"])
                logger.info(
                    "  Duration: %.1fs",
                    stats["duration_seconds"],
                )

                elapsed = time.monotonic() - script_started_at

                logger.info(
                    "Script complete in %.1fs",
                    elapsed,
                )

            finally:
                db.close()


if __name__ == "__main__":
    main()