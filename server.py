import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from urllib.parse import urlparse
from datetime import datetime
import os
from webscraper import scrape_product

app = FastAPI()

VALID_CATEGORY = {"top", "bottom", "outerwear", "footwear", "full_body", "accessory"}
VALID_GENDER = {"masculine", "feminine", "unisex"}

class ScrapeRequest(BaseModel):
    product_url: str

@app.post("/scrape")
def scrape(req: ScrapeRequest):
    data = scrape_product(req.product_url)

    data["scraped_at"] = datetime.utcnow().isoformat()
    data["source_domain"] = urlparse(req.product_url).hostname or ""

    if data.get("category") not in VALID_CATEGORY:
        data["category"] = None

    if data.get("gender_target") not in VALID_GENDER:
        data["gender_target"] = None

    return data

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

