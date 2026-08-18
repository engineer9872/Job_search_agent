import os
import sys
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Expose FastAPI ASGI app so `uvicorn main:app` works from root
try:
    from api.main import app
except Exception:
    from main import app
