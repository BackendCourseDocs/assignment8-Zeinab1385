import psycopg2
import time

DB_NAME = "books"
DB_USER = "postgres"
DB_PASSWORD = "1234"
DB_HOST = "localhost"
DB_PORT = "5432"

def fill_db(n=20000):
    run_tag = f"Run{int(time.time())}"  # هر اجرا یک برچسب یکتا

    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
        )
        cur = conn.cursor()

        print(f"در حال واریز {n} کتاب جدید به دیتابیس... ({run_tag})")

        for i in range(n):
            title = f"{run_tag} FastAPI Guide {i}"
            author = f"Author {i % 500}"
            publisher = f"{run_tag} Publisher {i % 100}"
            year = 2000 + (i % 25)

            cur.execute(
                "INSERT INTO books (title, author, publisher, first_publish_year) VALUES (%s, %s, %s, %s)",
                (title, author, publisher, year),
            )

        conn.commit()
        print("انجام شد! دیتابیس سنگین‌تر شد.")
        cur.close()
        conn.close()

    except Exception as e:
        print(f"خطا در اتصال به دیتابیس: {e}")

if __name__ == "__main__":
    fill_db(20000)