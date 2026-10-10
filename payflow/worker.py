import time
from .config import Settings
from .database import Database
from .service import PaymentService


def main():
    settings = Settings.from_env()
    settings.validate()
    db = Database(settings.database_url)
    service = PaymentService(db, settings)
    print("PayFlow worker started. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                service.drain(limit=20)
            except Exception as exc:
                print("Worker waiting after database error:", type(exc).__name__)
            time.sleep(0.75)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()


if __name__ == "__main__":
    main()
