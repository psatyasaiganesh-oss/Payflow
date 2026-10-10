import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./payflow.db"
    provider: str = "mock"
    api_token: str = ""
    webhook_secret: str = "whsec_local_payflow_demo"
    stripe_secret_key: str = ""
    auto_worker: bool = True
    lease_seconds: int = 30
    max_attempts: int = 5

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///./payflow.db"),
            provider=os.getenv("PAYFLOW_PROVIDER", "mock"),
            api_token=os.getenv("PAYFLOW_API_TOKEN", ""),
            webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_local_payflow_demo"),
            stripe_secret_key=os.getenv("STRIPE_SECRET_KEY", ""),
            auto_worker=os.getenv("PAYFLOW_AUTO_WORKER", "true").lower() == "true",
        )

    def validate(self):
        if self.provider not in {"mock", "stripe_test"}:
            raise ValueError("PAYFLOW_PROVIDER must be mock or stripe_test")
        if self.provider == "stripe_test":
            if not self.stripe_secret_key.startswith("sk_test_"):
                raise ValueError("Stripe mode requires a test secret key beginning sk_test_")
            if self.webhook_secret == "whsec_local_payflow_demo":
                raise ValueError("Set your actual Stripe test webhook signing secret")
            if not self.api_token:
                raise ValueError("Set PAYFLOW_API_TOKEN before enabling the Stripe test adapter")
