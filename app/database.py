import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

# Database setup with SQLite and WAL mode
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////app/data/gutendex.db")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )

    # Enable WAL mode for better concurrency
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=10000")

        # Required indexes for topic/language/author filters on SQLite.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_authors_book_id ON book_authors(book_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_authors_person_id ON book_authors(person_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_editors_book_id ON book_editors(book_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_editors_person_id ON book_editors(person_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_translators_book_id ON book_translators(book_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_translators_person_id ON book_translators(person_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_bookshelves_book_id ON book_bookshelves(book_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_bookshelves_bookshelf_id ON book_bookshelves(bookshelf_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_languages_book_id ON book_languages(book_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_languages_language_id ON book_languages(language_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_subjects_book_id ON book_subjects(book_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_subjects_subject_id ON book_subjects(subject_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_format_book_id ON format(book_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_summary_book_id ON summary(book_id)")

        cursor.close()
else:
    # PostgreSQL or other databases
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
