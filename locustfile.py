from locust import HttpUser, task, between
import random
import string
import time


SEARCH_TERMS = [
    "python", "data", "book", "author", "press",
    "2010", "1999", "programming", "api", "fast"
]


def rand_term() -> str:
    t = random.choice(SEARCH_TERMS)
    if random.random() < 0.30:
        t += " " + random.choice(SEARCH_TERMS)
    return t


def rand_word(n: int = 8) -> str:
    return "".join(random.choice(string.ascii_lowercase) for _ in range(n))


class WebsiteUser(HttpUser):
    wait_time = between(0.2, 1.0)

    def on_start(self):
        self.known_book_ids = []
        self.my_created_ids = []

    # -----------------------------
    # READ endpoints
    # -----------------------------

    @task(8)
    def search_books(self):
        q = rand_term()
        skip = random.choice([0, 0, 10, 20, 50])
        limit = random.choice([10, 20, 50, 100])

        with self.client.get(
            "/books",
            params={"q": q, "skip": skip, "limit": limit},
            name="GET /books",
            catch_response=True
        ) as r:
            if r.status_code != 200:
                r.failure(f"Unexpected status: {r.status_code}")
                return

            try:
                data = r.json()
                results = data.get("results") or []
                ids = [b.get("id") for b in results if isinstance(b, dict) and isinstance(b.get("id"), int)]
                if ids:
                    random.shuffle(ids)
                    self.known_book_ids.extend(ids[:20])
                    self.known_book_ids = list(dict.fromkeys(self.known_book_ids))[-500:]
                r.success()
            except Exception as e:
                r.failure(f"Bad JSON: {e}")

    @task(3)
    def search_authors(self):
        q = random.choice(SEARCH_TERMS)

        with self.client.get(
            "/authors",
            params={"q": q},
            name="GET /authors",
            catch_response=True
        ) as r:
            # این endpoint ممکنه 404 بده (No authors found) و ما fail حسابش نمی‌کنیم
            if r.status_code in (200, 404):
                r.success()
            else:
                r.failure(f"Unexpected status: {r.status_code}")

    @task(4)
    def get_book_by_id(self):
        # اگر id نداریم، یک سرچ سریع می‌زنیم تا جمع کنیم
        if not self.known_book_ids and random.random() < 0.7:
            self.search_books()

        if self.known_book_ids:
            book_id = random.choice(self.known_book_ids)
        else:
            # fallback: احتمال 404 هست
            book_id = random.randint(1, 200000)

        with self.client.get(
            f"/books/{book_id}",
            name="GET /books/{id}",
            catch_response=True
        ) as r:
            # 404 طبیعی است
            if r.status_code in (200, 404):
                r.success()
            else:
                r.failure(f"Unexpected status: {r.status_code}")

    # -----------------------------
    # WRITE endpoints (کم‌وزن)
    # -----------------------------

    @task(1)
    def create_book(self):
        # با Form می‌فرستیم (طبق main.py)
        now = int(time.time() * 1000)
        title = f"locust-{now}-{rand_word(6)}"
        author = f"Locust Author {random.randint(1, 500)}"
        publisher = f"Locust Pub {random.randint(1, 200)}"
        year = random.randint(1900, 2026)

        payload = {
            "title": title,
            "author": author,
            "publisher": publisher,
            "first_publish_year": str(year),
        }

        with self.client.post(
            "/books",
            data=payload,
            name="POST /books",
            catch_response=True
        ) as r:
            if r.status_code != 201:
                r.failure(f"Unexpected status: {r.status_code}")
                return

            try:
                data = r.json()
                new_id = data.get("id")
                if isinstance(new_id, int):
                    self.my_created_ids.append(new_id)
                    self.known_book_ids.append(new_id)
                    self.my_created_ids = self.my_created_ids[-200:]
                    self.known_book_ids = list(dict.fromkeys(self.known_book_ids))[-500:]
                r.success()
            except Exception as e:
                r.failure(f"Bad JSON: {e}")

    @task(1)
    def update_my_book(self):
        if not self.my_created_ids:
            return

        book_id = random.choice(self.my_created_ids)
        payload = {
            "title": f"updated-{rand_word(6)}",
            "author": f"Updated Author {random.randint(1, 500)}",
            "publisher": f"Updated Pub {random.randint(1, 200)}",
            "first_publish_year": str(random.randint(1900, 2026)),
        }

        with self.client.put(
            f"/books/{book_id}",
            data=payload,
            name="PUT /books/{id}",
            catch_response=True
        ) as r:
            if r.status_code in (200, 404):
                # اگر 404 شد یعنی کتاب حذف شده/وجود نداره (مشکلی نیست)
                if r.status_code == 404 and book_id in self.my_created_ids:
                    self.my_created_ids = [x for x in self.my_created_ids if x != book_id]
                r.success()
            else:
                r.failure(f"Unexpected status: {r.status_code}")

    @task(1)
    def delete_my_book(self):
        if not self.my_created_ids:
            return

        # خیلی کم حذف کن که دیتابیس سبک نشه
        if random.random() > 0.25:
            return

        book_id = random.choice(self.my_created_ids)

        with self.client.delete(
            f"/books/{book_id}",
            name="DELETE /books/{id}",
            catch_response=True
        ) as r:
            if r.status_code in (200, 404):
                self.my_created_ids = [x for x in self.my_created_ids if x != book_id]
                self.known_book_ids = [x for x in self.known_book_ids if x != book_id]
                r.success()
            else:
                r.failure(f"Unexpected status: {r.status_code}")