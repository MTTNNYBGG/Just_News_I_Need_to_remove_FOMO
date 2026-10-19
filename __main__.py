import os, uvicorn
from .main import create_app
if __name__ == "__main__":
    uvicorn.run(create_app(), host=os.environ.get("INEWS_HOST", "127.0.0.1"), port=int(os.environ.get("INEWS_PORT", "8000")))
