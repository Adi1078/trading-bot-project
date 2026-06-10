import os
from dotenv import load_dotenv

load_dotenv()

# 5paisa Credentials
APP_KEY = os.getenv("APP_KEY", "")
ENCRY_KEY = os.getenv("ENCRY_KEY", "")
USER_ID = os.getenv("USER_ID", "")
ALGO_ID = os.getenv("ALGO_ID", "")

# Email
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", "sigaramcapitals@gmail.com")

# App
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./trading_bot.db")
SECRET_KEY = os.getenv("SECRET_KEY", "change_this_to_a_random_secret_string")
CALLBACK_URL = os.getenv("CALLBACK_URL", "http://54.251.248.85:8181/api/auth/callback")
