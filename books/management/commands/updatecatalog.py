from subprocess import run
import json
import os
import re
import shutil
import tarfile
from time import strftime
import time
import requests
from requests.adapters import HTTPAdapter

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError

from books import utils
from books.models import (
    Book,
    Bookshelf,
    Format,
    Language,
    Person,
    Subject,
    Summary,
)


TEMP_PATH = settings.CATALOG_TEMP_DIR

URL = 'https://gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2'
DOWNLOAD_PATH = os.path.join(TEMP_PATH, 'catalog.tar.bz2')
DOWNLOAD_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_MAX_ATTEMPTS = 3
DOWNLOAD_PROGRESS_PERCENT_STEP = 10
DOWNLOAD_PROGRESS_LOG_INTERVAL_SECONDS = 15

MOVE_SOURCE_PATH = os.path.join(TEMP_PATH, 'cache/epub')
MOVE_TARGET_PATH = settings.CATALOG_RDF_DIR

LOG_DIRECTORY = settings.CATALOG_LOG_DIR
LOG_FILE_NAME = strftime('%Y-%m-%d_%H%M%S') + '.txt'
LOG_PATH = os.path.join(LOG_DIRECTORY, LOG_FILE_NAME)


# This gives a set of the names of the subdirectories in the given file path.
def get_directory_set(path):
    directory_set = set()
    for directory_item in os.listdir(path):
        item_path = os.path.join(path, directory_item)
        if os.path.isdir(item_path):
            directory_set.add(directory_item)
    return directory_set


def log(*args):
    print(*args, flush=True)
    if not os.path.exists(LOG_DIRECTORY):
        os.makedirs(LOG_DIRECTORY)
    with open(LOG_PATH, 'a') as log_file:
        text = ' '.join(str(arg) for arg in args) + '\n'
        log_file.write(text)


def format_file_size(size_in_bytes):
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = float(max(size_in_bytes, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == 'B':
                return f'{int(size)} {unit}'
            return f'{size:.2f} {unit}'
        size /= 1024


def download_file_with_progress(url, destination_path):
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
            print(
                f"Download attempt "
                f"{attempt}/{DOWNLOAD_MAX_ATTEMPTS}"
            )

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
                    print(
                        f"Expected size: "
                        f"{format_file_size(total_size)}"
                    )
                else:
                    print("Expected size: unknown")

                last_report = time.monotonic()
                last_downloaded = downloaded

                with open(
                    destination_path,
                    mode,
                    buffering=8 * 1024 * 1024,
                ) as f:

                    for chunk in response.iter_content(
                        chunk_size=DOWNLOAD_CHUNK_SIZE
                    ):
                        if not chunk:
                            continue

                        f.write(chunk)
                        downloaded += len(chunk)

                        now = time.monotonic()

                        # report every 2 seconds
                        if now - last_report >= 2:

                            instant_elapsed = now - last_report

                            speed = (
                                (downloaded - last_downloaded)
                                / max(instant_elapsed, 1e-6)
                            )

                            if total_size:
                                percent = (
                                    downloaded * 100 / total_size
                                )

                                print(
                                    f"{percent:6.2f}% | "
                                    f"{format_file_size(downloaded)} / "
                                    f"{format_file_size(total_size)} | "
                                    f"{format_file_size(speed)}/s"
                                )
                            else:
                                print(
                                    f"{format_file_size(downloaded)} | "
                                    f"{format_file_size(speed)}/s"
                                )

                            last_report = now
                            last_downloaded = downloaded

                # validate complete download
                if total_size and downloaded < total_size:
                    raise RuntimeError(
                        "Download incomplete "
                        f"({downloaded}/{total_size})"
                    )

                return {
                    "downloaded": downloaded,
                    "seconds": time.monotonic() - started_at,
                }

        except Exception as e:
            print(f"Attempt {attempt} failed: {e}")

            if attempt >= DOWNLOAD_MAX_ATTEMPTS:
                raise

            print("Retrying...")
            time.sleep(2)

    raise RuntimeError("Download failed")


def get_changed_book_ids(rsync_output_lines):
    book_ids = set()

    for line in rsync_output_lines:
        match = re.match(r'^(\d+)/', line.strip())
        if match:
            book_ids.add(int(match.group(1)))

    return book_ids


def put_catalog_in_db(book_ids=None):
    if book_ids is None:
        book_ids = []
        for directory_item in os.listdir(settings.CATALOG_RDF_DIR):
            item_path = os.path.join(settings.CATALOG_RDF_DIR, directory_item)
            if os.path.isdir(item_path):
                try:
                    book_id = int(directory_item)
                except ValueError:
                    # Ignore the item if it's not a book ID number.
                    pass
                else:
                    book_ids.append(book_id)
    else:
        # Keep only IDs that still exist after rsync/deletion handling.
        book_ids = [
            id for id in book_ids
            if os.path.isdir(os.path.join(settings.CATALOG_RDF_DIR, str(id)))
        ]

    book_ids.sort()
    book_directories = [str(id) for id in book_ids]
    total_books = len(book_directories)
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
        log('    Import queue size:', total_books, 'books')

    for index, directory in enumerate(book_directories, start=1):
        book_id = int(directory)

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

            log('    DB import progress:', f'{index}/{total_books} ({progress}%)', rate_text)
            last_progress_at = now
            last_progress_index = index

        book_path = os.path.join(
            settings.CATALOG_RDF_DIR,
            directory,
            'pg' + directory + '.rdf'
        )

        book = utils.get_book(book_id, book_path)

        try:
            '''Make/update the book.'''

            book_in_db = Book.objects.filter(gutenberg_id=book_id).first()

            if book_in_db is not None:
                book_in_db.copyright = book['copyright']
                book_in_db.download_count = book['downloads']
                book_in_db.media_type = book['type']
                book_in_db.title = book['title']
                book_in_db.save()
                stats['updated'] += 1
            else:
                book_in_db = Book.objects.create(
                    gutenberg_id=book_id,
                    copyright=book['copyright'],
                    download_count=book['downloads'],
                    media_type=book['type'],
                    title=book['title']
                )
                stats['created'] += 1

            ''' Make/update the authors. '''

            authors = []
            for author in book['authors']:
                person = get_or_create_person(author, person_cache)
                authors.append(person)

            book_in_db.authors.set(authors)

            ''' Make/update the editors. '''

            editors = []
            for editor in book['editors']:
                person = get_or_create_person(editor, person_cache)
                editors.append(person)

            book_in_db.editors.set(editors)

            ''' Make/update the translators. '''

            translators = []
            for translator in book['translators']:
                person = get_or_create_person(translator, person_cache)
                translators.append(person)

            book_in_db.translators.set(translators)

            ''' Make/update the book shelves. '''

            bookshelves = []
            for shelf in book['bookshelves']:
                shelf_in_db = get_or_create_bookshelf(shelf, bookshelf_cache)
                bookshelves.append(shelf_in_db)

            book_in_db.bookshelves.set(bookshelves)

            ''' Make/update the formats. '''

            existing_formats = {
                (existing_format.mime_type, existing_format.url): existing_format
                for existing_format in Format.objects.filter(book=book_in_db)
            }
            expected_formats = {
                (mime_type, url)
                for mime_type, url in book['formats'].items()
            }

            missing_formats = expected_formats - set(existing_formats.keys())
            for mime_type, url in missing_formats:
                Format.objects.create(book=book_in_db, mime_type=mime_type, url=url)

            stale_format_ids = [
                existing_format.id
                for format_key, existing_format in existing_formats.items()
                if format_key not in expected_formats
            ]
            if stale_format_ids:
                Format.objects.filter(id__in=stale_format_ids).delete()

            ''' Make/update the languages. '''

            languages = []
            for language in book['languages']:
                language_in_db = get_or_create_language(language, language_cache)
                languages.append(language_in_db)

            book_in_db.languages.set(languages)

            ''' Make/update subjects. '''

            subjects = []
            for subject in book['subjects']:
                subject_in_db = get_or_create_subject(subject, subject_cache)
                subjects.append(subject_in_db)

            book_in_db.subjects.set(subjects)

            ''' Make/update summaries. '''

            existing_summaries = {
                existing_summary.text: existing_summary
                for existing_summary in Summary.objects.filter(book=book_in_db)
            }
            expected_summaries = set(book['summaries'])

            missing_summaries = expected_summaries - set(existing_summaries.keys())
            for summary_text in missing_summaries:
                Summary.objects.create(book=book_in_db, text=summary_text)

            stale_summary_ids = [
                existing_summary.id
                for summary_text, existing_summary in existing_summaries.items()
                if summary_text not in expected_summaries
            ]
            if stale_summary_ids:
                Summary.objects.filter(id__in=stale_summary_ids).delete()

            stats['processed'] += 1

        except Exception as error:
            book_json = json.dumps(book, indent=4)
            log(
                '  Error while putting this book info in the database:\n',
                book_json,
                '\n'
            )
            raise error

    duration = time.monotonic() - started_at
    stats['duration_seconds'] = duration
    return stats


def get_or_create_person(data, cache=None):
    person_key = (data['name'], data['birth'], data['death'])

    if cache is not None and person_key in cache:
        return cache[person_key]

    person = Person.objects.filter(
        name=data['name'],
        birth_year=data['birth'],
        death_year=data['death']
    ).first()

    if person is None:
        person = Person.objects.create(
            name=data['name'],
            birth_year=data['birth'],
            death_year=data['death']
        )

    if cache is not None:
        cache[person_key] = person

    return person


def get_or_create_bookshelf(name, cache):
    if name in cache:
        return cache[name]

    bookshelf = Bookshelf.objects.filter(name=name).first()
    if bookshelf is None:
        bookshelf = Bookshelf.objects.create(name=name)

    cache[name] = bookshelf
    return bookshelf


def get_or_create_language(code, cache):
    if code in cache:
        return cache[code]

    language = Language.objects.filter(code=code).first()
    if language is None:
        language = Language.objects.create(code=code)

    cache[code] = language
    return language


def get_or_create_subject(name, cache):
    if name in cache:
        return cache[name]

    subject = Subject.objects.filter(name=name).first()
    if subject is None:
        subject = Subject.objects.create(name=name)

    cache[name] = subject
    return subject


def send_log_email():
    if not (settings.ADMIN_EMAILS or settings.EMAIL_HOST_ADDRESS):
        return

    log_text = ''
    with open(LOG_PATH, 'r') as log_file:
        log_text = log_file.read()

    email_html = '''
        <h1 style="color: #333;
                   font-family: 'Helvetica Neue', sans-serif;
                   font-size: 64px;
                   font-weight: 100;
                   text-align: center;">
            Gutendex
        </h1>

        <p style="color: #333;
                  font-family: 'Helvetica Neue', sans-serif;
                  font-size: 24px;
                  font-weight: 200;">
            Here is the log from your catalog retrieval:
        </p>

        <pre style="color:#333;
                    font-family: monospace;
                    font-size: 16px;
                    margin-left: 32px">''' + log_text + '</pre>'

    email_text = '''GUTENDEX

    Here is the log from your catalog retrieval:

    ''' + log_text

    send_mail(
        subject='Catalog retrieval',
        message=email_text,
        html_message=email_html,
        from_email=settings.EMAIL_HOST_ADDRESS,
        recipient_list=settings.ADMIN_EMAILS
    )


class Command(BaseCommand):
    help = 'This replaces the catalog files with the latest ones.'

    def handle(self, *args, **options):
        try:
            script_started_at = time.monotonic()
            date_and_time = strftime('%H:%M:%S on %B %d, %Y')
            log('Starting script at', date_and_time)

            log('  Making temporary directory...')
            if os.path.exists(TEMP_PATH):
                log('    Temporary path already exists; removing stale directory...')
                shutil.rmtree(TEMP_PATH)
            os.makedirs(TEMP_PATH)
            log('    Temporary directory ready at', TEMP_PATH)

            log('  Downloading compressed catalog...')
            log('    Source URL:', URL)
            download_stats = download_file_with_progress(URL, DOWNLOAD_PATH)
            log(
                '    Download complete:',
                format_file_size(download_stats['downloaded']),
                f"in {download_stats['seconds']:.1f}s"
            )

            log('  Decompressing catalog...')
            if not os.path.exists(DOWNLOAD_PATH):
                raise CommandError(
                    'Downloaded catalog archive not found at ' + DOWNLOAD_PATH
                )
            decompress_started_at = time.monotonic()
            try:
                with tarfile.open(DOWNLOAD_PATH, 'r:bz2') as tar_archive:
                    tar_archive.extractall(path=TEMP_PATH)
            except (tarfile.TarError, OSError) as error:
                raise CommandError(
                    'Failed to decompress compressed catalog tarball: ' + str(error)
                )
            log(
                '    Decompression complete in',
                f'{time.monotonic() - decompress_started_at:.1f}s'
            )

            log('  Detecting stale directories...')
            if not os.path.exists(MOVE_TARGET_PATH):
                os.makedirs(MOVE_TARGET_PATH)
            new_directory_set = get_directory_set(MOVE_SOURCE_PATH)
            old_directory_set = get_directory_set(MOVE_TARGET_PATH)
            stale_directory_set = old_directory_set - new_directory_set
            log('    New directories found:', len(new_directory_set))
            log('    Existing directories found:', len(old_directory_set))
            log('    Stale directories detected:', len(stale_directory_set))

            log('  Removing stale directories and books...')
            stale_total = len(stale_directory_set)
            deleted_stale_count = 0
            stale_progress_step = max(1, stale_total // 10) if stale_total else 1
            for index, directory in enumerate(sorted(stale_directory_set), start=1):
                try:
                    book_id = int(directory)
                except ValueError:
                    # Ignore the directory if its name isn't a book ID number.
                    continue
                book = Book.objects.filter(gutenberg_id=book_id)
                book.delete()
                path = os.path.join(MOVE_TARGET_PATH, directory)
                shutil.rmtree(path)
                deleted_stale_count += 1
                if index % stale_progress_step == 0 or index == stale_total:
                    log(
                        '    Stale cleanup progress:',
                        f'{index}/{stale_total}'
                    )
            log('    Stale directories removed:', deleted_stale_count)

            log('  Replacing old catalog files...')
            rsync_started_at = time.monotonic()
            with open(LOG_PATH, 'a') as log_file:
                rsync_result = run(
                    [
                        'rsync',
                        '-a',
                        '--delete-after',
                        '--out-format=%n',
                        MOVE_SOURCE_PATH + '/',
                        MOVE_TARGET_PATH
                    ],
                    capture_output=True,
                    text=True
                )

                if rsync_result.stderr:
                    log_file.write(rsync_result.stderr)

            if rsync_result.returncode != 0:
                raise CommandError('Rsync failed while replacing catalog files.')

            rsync_duration = time.monotonic() - rsync_started_at
            rsync_lines = [line for line in rsync_result.stdout.splitlines() if line.strip()]
            log(
                '    Rsync complete in',
                f'{rsync_duration:.1f}s with {len(rsync_lines)} changed paths'
            )

            changed_book_ids = get_changed_book_ids(rsync_result.stdout.splitlines())
            log('    Changed book directories detected:', len(changed_book_ids))

            if changed_book_ids:
                log('  Putting changed catalog books in the database...')
                import_stats = put_catalog_in_db(changed_book_ids)
                log('    Import summary:')
                log('      Processed:', import_stats['processed'])
                log('      Created:', import_stats['created'])
                log('      Updated:', import_stats['updated'])
                log('      Duration:', f"{import_stats['duration_seconds']:.1f}s")
            elif stale_directory_set:
                log('  No changed books to import (stale books were removed).')
            else:
                log('  No catalog changes detected; skipping database import.')

            log('  Removing temporary files...')
            shutil.rmtree(TEMP_PATH)
            log('    Temporary files removed.')

            log('  Total runtime:', f'{time.monotonic() - script_started_at:.1f}s')

            log('Done!\n')
        except Exception as error:
            error_message = str(error)
            log('Error:', error_message)
            log('')
            if os.path.exists(TEMP_PATH):
                shutil.rmtree(TEMP_PATH)

        send_log_email()
