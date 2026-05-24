from typing import Optional
from urllib.parse import urlencode

from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.database import get_db
from app.models import Book, Person, Bookshelf, Language, Subject, Format
from app.schemas import BookListResponse

router = APIRouter(prefix="/books", tags=["books"])


BOOK_LOAD_OPTIONS = (
    selectinload(Book.authors),
    selectinload(Book.editors),
    selectinload(Book.translators),
    selectinload(Book.bookshelves),
    selectinload(Book.languages),
    selectinload(Book.subjects),
    selectinload(Book.formats),
    selectinload(Book.summaries),
)


def build_page_url(request: Request, page: int) -> str:
    query_items = [item for item in request.query_params.multi_items() if item[0] != "page"]
    if all(key != "page_size" for key, _ in query_items):
        query_items = [item for item in query_items if item[0] != "page_size"]

    query = urlencode([("page", str(page)), *query_items], doseq=True)
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/books?{query}"


def serialize_book(book: Book) -> dict:
    """Convert a Book model to the exact JSON format expected by clients."""
    # Get authors, editors, translators in expected format
    authors = [{"name": a.name, "birth_year": a.birth_year, "death_year": a.death_year} for a in book.authors if a and a.name is not None]
    editors = [{"name": e.name, "birth_year": e.birth_year, "death_year": e.death_year} for e in book.editors if e and e.name is not None]
    translators = [{"name": t.name, "birth_year": t.birth_year, "death_year": t.death_year} for t in book.translators if t and t.name is not None]
    
    # Get bookshelves sorted
    bookshelves = sorted([b.name for b in book.bookshelves if b and b.name is not None])
    
    # Get languages sorted
    languages = sorted([
        language.code
        for language in book.languages
        if language and language.code is not None
    ])
    
    # Get subjects sorted
    subjects = sorted([s.name for s in book.subjects if s and s.name is not None])
    
    # Get formats as dict
    formats = {f.mime_type: f.url for f in book.formats if f and f.mime_type is not None and f.url is not None}
    
    # Get summaries sorted
    summaries = sorted([s.text for s in book.summaries if s and s.text is not None])
    
    return {
        "id": book.gutenberg_id,
        "title": book.title,
        "authors": authors,
        "summaries": summaries,
        "editors": editors,
        "translators": translators,
        "subjects": subjects,
        "bookshelves": bookshelves,
        "languages": languages,
        "copyright": book.copyright,
        "media_type": book.media_type,
        "formats": formats,
        "download_count": book.download_count,
    }


@router.get("", include_in_schema=False, response_model=BookListResponse, response_model_by_alias=False)
@router.get("/", response_model=BookListResponse, response_model_by_alias=False)
def list_books(
    request: Request,
    db: Session = Depends(get_db),
    sort: Optional[str] = Query(None),
    author_year_end: Optional[int] = Query(None),
    author_year_start: Optional[int] = Query(None),
    copyright: Optional[str] = Query(None),
    ids: Optional[str] = Query(None),
    languages: Optional[str] = Query(None),
    mime_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    topic: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(32, ge=1, le=100),
):
    """
    List books with filtering, sorting, and pagination.
    Exact replica of Django REST Framework behavior.
    """
    # Start with base queryset: exclude null download_count and title
    queryset = db.query(Book).options(*BOOK_LOAD_OPTIONS).filter(
        Book.download_count.isnot(None),
        Book.title.isnot(None)
    )
    
    # Apply copyright filter
    if copyright is not None:
        copyright_strings = copyright.split(',')
        copyright_values = set()
        for copyright_string in copyright_strings:
            if copyright_string == 'true':
                copyright_values.add(True)
            elif copyright_string == 'false':
                copyright_values.add(False)
            elif copyright_string == 'null':
                copyright_values.add(None)
        
        # Build filter: keep only books whose copyright is in copyright_values
        copyright_conditions = []
        if True in copyright_values:
            copyright_conditions.append(Book.copyright.is_(True))
        if False in copyright_values:
            copyright_conditions.append(Book.copyright.is_(False))
        if None in copyright_values:
            copyright_conditions.append(Book.copyright.is_(None))
        
        if copyright_conditions:
            queryset = queryset.filter(or_(*copyright_conditions))
    
    # Apply author year end filter
    if author_year_end is not None:
        queryset = queryset.join(Book.authors).filter(
            or_(
                Person.birth_year <= author_year_end,
                Person.death_year <= author_year_end
            )
        ).distinct()
    
    # Apply author year start filter
    if author_year_start is not None:
        queryset = queryset.join(Book.authors).filter(
            or_(
                Person.birth_year >= author_year_start,
                Person.death_year >= author_year_start
            )
        ).distinct()
    
    # Apply ID filter
    if ids is not None:
        try:
            id_list = [int(id.strip()) for id in ids.split(',')]
            queryset = queryset.filter(Book.gutenberg_id.in_(id_list))
        except ValueError:
            pass
    
    # Apply language filter
    if languages is not None:
        language_codes = [code.lower() for code in languages.split(',')]
        queryset = queryset.join(Book.languages).filter(
            Language.code.in_(language_codes)
        ).distinct()
    
    # Apply mime_type filter
    if mime_type is not None:
        queryset = queryset.join(Book.formats).filter(
            Format.mime_type.startswith(mime_type)
        ).distinct()
    
    # Apply search filter (search in authors and title)
    if search is not None:
        search_terms = search.split(' ')[:32]  # Limit to 32 terms like Django
        for term in search_terms:
            queryset = queryset.filter(
                or_(
                    Book.authors.any(Person.name.ilike(f"%{term}%")),
                    Book.title.ilike(f"%{term}%")
                )
            ).distinct()
    
    # Apply topic filter (search in bookshelves and subjects)
    if topic is not None:
        queryset = queryset.filter(
            or_(
                Book.bookshelves.any(Bookshelf.name.ilike(f"%{topic}%")),
                Book.subjects.any(Subject.name.ilike(f"%{topic}%"))
            )
        ).distinct()
    
    # Apply sorting (default: descending by download_count)
    if sort == 'ascending':
        queryset = queryset.order_by(Book.id.asc())
    elif sort == 'descending':
        queryset = queryset.order_by(Book.id.desc())
    else:
        queryset = queryset.order_by(Book.download_count.desc())
    
    # Get total count before pagination
    total_count = queryset.count()
    
    # Apply pagination
    offset = (page - 1) * page_size
    books = queryset.offset(offset).limit(page_size).all()
    
    # Serialize books
    serialized_books = [serialize_book(book) for book in books]
    
    # Calculate pagination URLs
    next_page = None
    previous_page = None
    
    if offset + page_size < total_count:
        next_page = build_page_url(request, page + 1)
    
    if offset > 0:
        previous_page = build_page_url(request, page - 1)
    
    return {
        "count": total_count,
        "next": next_page,
        "previous": previous_page,
        "results": serialized_books,
    }


@router.get("/{gutenberg_id}", include_in_schema=False)
def redirect_book(gutenberg_id: int, request: Request):
    return redirect_to_canonical(f"/books/{gutenberg_id}/", request)


@router.get("/{gutenberg_id}/")
def get_book(gutenberg_id: int, db: Session = Depends(get_db)):
    """Get a single book by gutenberg_id."""
    book = db.query(Book).options(*BOOK_LOAD_OPTIONS).filter(Book.gutenberg_id == gutenberg_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="No Book matches the given query.")
    return serialize_book(book)
