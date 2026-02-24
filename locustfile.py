from locust import HttpUser, task, between
import random

SEARCH_TERMS = [
    "python", "data", "book", "author", "press",
    "2010", "1999", "programming", "api", "fast"
]

def rand_term():
    t = random.choice(SEARCH_TERMS)
    if random.random() < 0.3:
        t += " " + random.choice(SEARCH_TERMS)
    return t

class WebsiteUser(HttpUser):
    wait_time = between(0.2, 1.0)

    @task(8)
    def search_books(self):
        q = rand_term()
        skip = random.choice([0, 0, 10, 20])
        limit = random.choice([10, 20, 50])

        self.client.get(
            "/books",
            params={"q": q, "skip": skip, "limit": limit},
            name="/books"
        )

    @task(2)
    def search_authors(self):
        q = random.choice(SEARCH_TERMS)

        # این بخش باعث میشه 404 برای سرچ نویسنده Fail حساب نشه
        with self.client.get(
            "/authors",
            params={"q": q},
            name="/authors",
            catch_response=True
        ) as r:
            if r.status_code in (200, 404):
                r.success()
            else:
                r.failure(f"Unexpected status: {r.status_code}")