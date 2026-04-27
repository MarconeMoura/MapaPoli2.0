import os

from avatar import app
import uvicorn

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "9000"))
    uvicorn.run("avatar:app", host=host, port=port, reload=False)
