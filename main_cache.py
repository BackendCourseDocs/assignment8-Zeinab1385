from fastapi import FastAPI, Query, Form, File, UploadFile, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, List, Generator, Any, Dict, Tuple
import os
import uuid
import psycopg2
import requests
import time
import threading

DB_NAME = "books"
DB_USER = "postgres"
DB_PASSWORD = "1234"
DB_HOST = "localhost"
DB_PORT = "5432"

IMAGES_DIR = "images"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

os.makedirs(IMAGES_DIR, exist_ok=True)

app = FastAPI()
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")

seed_books: List[dict] = []

class BookIn(BaseModel):
    title: str = Field(..., min_length=3, max_length=100)
    author: str = Field(..., min_length=3, max_length=100)
    publisher: str = Field(..., min_length=3, max_length=100)
    first_publish_year: int = Field(..., ge=0)

class BookOut(BookIn):
    id: int
    image_url: Optional[str] = None
    source: str

def db_connect():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )

def get_db() -> Generator:
    conn = db_connect()
    try:
        yield conn
    finally:
        conn.close()

def safe_ext(filename: str) -> str:
    _, ext = os.path.splitext(filename or "")
    ext = (ext or "").lower()
    return ext if ext in ALLOWED_EXTS else ""

def save_upload(image: UploadFile) -> str:
    ct = image.content_type or ""
    if not ct.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed.")
    ext = safe_ext(image.filename or "")
    if not ext:
        raise HTTPException(status_code=400, detail="Unsupported image extension.")
    name = f"{uuid.uuid4()}{ext}"
    path = os.path.join(IMAGES_DIR, name)
    written = 0
    with open(path, "wb") as f:
        while True:
            chunk = image.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                f.close()
                if os.path.exists(path):
                    os.remove(path)
                raise HTTPException(status_code=413, detail="File too large.")
            f.write(chunk)
    return name

def remove_image(filename: Optional[str]) -> None:
    if not filename:
        return
    path = os.path.join(IMAGES_DIR, filename)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass

def to_image_url(filename: Optional[str]) -> Optional[str]:
    return f"/images/{filename}" if filename else None

def load_seed():
    global seed_books
    url = "https://openlibrary.org/search.json"
    params = {"q": "python", "limit": 58}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        seed_books = []
        return
    out = []
    docs = data.get("docs") or []
    for i, b in enumerate(docs):
        out.append(
            {
                "id": 999 + i,
                "title": b.get("title") or "Unknown",
                "author": (b.get("author_name") or ["Unknown"])[0] if isinstance(b.get("author_name"), list) else "Unknown",
                "publisher": (b.get("publisher") or ["Unknown"])[0] if isinstance(b.get("publisher"), list) else "Unknown",
                "first_publish_year": int(b.get("first_publish_year") or 0),
                "image_url": None,
                "source": "OpenLibrary",
            }
        )
    seed_books = out

class TTLCache:
    def __init__(self, ttl_seconds: int, max_items: int):
        self.ttl = int(ttl_seconds)
        self.max_items = int(max_items)
        self._data: Dict[Any, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any):
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            ts, val = item
            if now - ts > self.ttl:
                self._data.pop(key, None)
                return None
            return val

    def set(self, key: Any, value: Any):
        now = time.time()
        with self._lock:
            if len(self._data) >= self.max_items:
                oldest_key = None
                oldest_ts = None
                for k, (ts, _) in self._data.items():
                    if oldest_ts is None or ts < oldest_ts:
                        oldest_ts = ts
                        oldest_key = k
                if oldest_key is not None:
                    self._data.pop(oldest_key, None)
            self._data[key] = (now, value)

    def delete(self, key: Any):
        with self._lock:
            self._data.pop(key, None)

    def clear(self):
        with self._lock:
            self._data.clear()

books_query_cache = TTLCache(ttl_seconds=20, max_items=500)
book_by_id_cache = TTLCache(ttl_seconds=60, max_items=2000)
authors_query_cache = TTLCache(ttl_seconds=30, max_items=500)

def invalidate_all_reads():
    books_query_cache.clear()
    authors_query_cache.clear()
    book_by_id_cache.clear()

@app.on_event("startup")
def startup():
    conn = db_connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS books (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    publisher TEXT NOT NULL,
                    first_publish_year INT NOT NULL DEFAULT 0,
                    image_url TEXT
                );
                """
            )
        conn.commit()
    finally:
        conn.close()
    load_seed()

@app.get("/books")
def search_books(
    q: str = Query(..., min_length=1, max_length=100),
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    conn=Depends(get_db),
):
    ql = q.lower()
    cache_key = ("books", ql)
    cached = books_query_cache.get(cache_key)
    if cached is None:
        sql = """
            SELECT id, title, author, publisher, first_publish_year, image_url
            FROM books
            WHERE LOWER(title) LIKE %s OR LOWER(author) LIKE %s OR LOWER(publisher) LIKE %s OR CAST(first_publish_year AS TEXT) LIKE %s
            ORDER BY id
        """
        like = f"%{ql}%"
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, (like, like, like, like))
                rows = cursor.fetchall()
        except Exception:
            raise HTTPException(status_code=500, detail="Database query failed")
        db_results = [
            {
                "id": r[0],
                "title": r[1],
                "author": r[2],
                "publisher": r[3],
                "first_publish_year": r[4],
                "image_url": to_image_url(r[5]),
                "source": "Database",
            }
            for r in rows
        ]
        ext_results = [
            b
            for b in seed_books
            if ql in (b["title"] or "").lower()
            or ql in (b["author"] or "").lower()
            or ql in (b["publisher"] or "").lower()
            or ql in str(b["first_publish_year"])
        ]
        all_results = db_results + ext_results
        cached = {"query": q, "results": all_results}
        books_query_cache.set(cache_key, cached)
    all_results = cached["results"]
    total = len(all_results)
    end = skip + limit
    return {"query": q, "count": total, "results": all_results[skip:end], "skip": skip, "limit": limit}

@app.get("/books/{book_id}")
def get_book(book_id: int, conn=Depends(get_db)):
    cache_key = ("book", int(book_id))
    cached = book_by_id_cache.get(cache_key)
    if cached is not None:
        return cached
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, title, author, publisher, first_publish_year, image_url FROM books WHERE id=%s",
            (book_id,),
        )
        row = cursor.fetchone()
    if not row:
        for b in seed_books:
            if b["id"] == book_id:
                book_by_id_cache.set(cache_key, b)
                return b
        raise HTTPException(status_code=404, detail="Book not found")
    out = {
        "id": row[0],
        "title": row[1],
        "author": row[2],
        "publisher": row[3],
        "first_publish_year": row[4],
        "image_url": to_image_url(row[5]),
        "source": "Database",
    }
    book_by_id_cache.set(cache_key, out)
    return out

@app.get("/authors")
def get_authors(q: str = Query(..., min_length=1, max_length=100), conn=Depends(get_db)):
    term = q.strip().lower()
    cache_key = ("authors", term)
    cached = authors_query_cache.get(cache_key)
    if cached is not None:
        if not cached["results"]:
            raise HTTPException(status_code=404, detail="No authors found")
        return {"query": q, "results": cached["results"]}
    pattern = f"%{term}%"
    combined = {}
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT author, COUNT(*) AS book_count
                FROM books
                WHERE LOWER(author) LIKE %s
                GROUP BY author
                """,
                (pattern,),
            )
            for author, cnt in cursor.fetchall():
                combined[("Database", author)] = int(cnt)
    except Exception:
        raise HTTPException(status_code=500, detail="Database query failed")
    for b in seed_books:
        author = (b.get("author") or "").strip()
        if author and term in author.lower():
            key = ("OpenLibrary", author)
            combined[key] = combined.get(key, 0) + 1
    results = [
        {"author": author, "book_count": count, "source": source}
        for (source, author), count in combined.items()
    ]
    results.sort(key=lambda x: (-x["book_count"], x["author"]))
    authors_query_cache.set(cache_key, {"results": results})
    if not results:
        raise HTTPException(status_code=404, detail="No authors found")
    return {"query": q, "results": results}

@app.post("/books", status_code=201)
def add_book(
    title: str = Form(..., min_length=3, max_length=100),
    author: str = Form(..., min_length=3, max_length=100),
    publisher: str = Form(..., min_length=3, max_length=100),
    first_publish_year: int = Form(..., ge=0),
    image: Optional[UploadFile] = File(None),
    conn=Depends(get_db),
):
    image_name = None
    if image:
        image_name = save_upload(image)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO books (title, author, publisher, first_publish_year, image_url)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (title, author, publisher, first_publish_year, image_name),
            )
            new_id = cursor.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        remove_image(image_name)
        raise HTTPException(status_code=500, detail="Failed to add book")
    invalidate_all_reads()
    return {
        "id": new_id,
        "title": title,
        "author": author,
        "publisher": publisher,
        "first_publish_year": first_publish_year,
        "image_url": to_image_url(image_name),
        "source": "Database",
    }

@app.put("/books/{book_id}")
def update_book(
    book_id: int,
    title: str = Form(..., min_length=3, max_length=100),
    author: str = Form(..., min_length=3, max_length=100),
    publisher: str = Form(..., min_length=3, max_length=100),
    first_publish_year: int = Form(..., ge=0),
    image: Optional[UploadFile] = File(None),
    conn=Depends(get_db),
):
    with conn.cursor() as cursor:
        cursor.execute("SELECT image_url FROM books WHERE id=%s", (book_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")
    old_image = row[0]
    new_image = old_image
    if image:
        new_image = save_upload(image)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE books
                SET title=%s, author=%s, publisher=%s, first_publish_year=%s, image_url=%s
                WHERE id=%s
                RETURNING id
                """,
                (title, author, publisher, first_publish_year, new_image, book_id),
            )
            updated = cursor.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        if image and new_image != old_image:
            remove_image(new_image)
        raise HTTPException(status_code=500, detail="Failed to update book")
    if image and old_image and new_image != old_image:
        remove_image(old_image)
    invalidate_all_reads()
    return {"status": "updated", "id": book_id, "image_url": to_image_url(new_image)}

@app.delete("/books/{book_id}")
def delete_book(book_id: int, conn=Depends(get_db)):
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM books WHERE id=%s RETURNING image_url",
                (book_id,),
            )
            row = cursor.fetchone()
        if not row:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Book not found")
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete book")
    remove_image(row[0])
    invalidate_all_reads()
    return {"status": "deleted", "id": book_id}