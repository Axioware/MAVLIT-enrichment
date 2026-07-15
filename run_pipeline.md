# wikidata_socials → shopify → google_social → tranco → meta_ads → youtube → twitter


## to run instagram_posts.py (apify)

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.instagram_posts import enrich_instagram_posts
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_instagram_posts(db, limit=1)
db.close()
"

OR

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.instagram_posts import enrich_instagram_posts
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_instagram_posts(db, brand_id=853)
db.close()
"


## to run youtube_sponsorship.py (daily limited run)

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.youtube_sponsorship import enrich_youtube_sponsorships
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal() 
enrich_youtube_sponsorships(db, limit=1)
db.close()
"

OR

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.youtube_sponsorship import enrich_youtube_sponsorships
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_youtube_sponsorships(db, brand_id=853)
db.close()
"

or

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.youtube_sponsorship import enrich_youtube_sponsorships
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_youtube_sponsorships(db, limit=1, niche='fashion')
db.close()
"


## to run tiktok_posts.py (apify)

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.tiktok_posts import enrich_tiktok_posts
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_tiktok_posts(db, limit=1)
db.close()
"

## to run meta_ads.py

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.meta_ads import enrich_meta_ads
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_meta_ads(db, limit=1)
db.close()
"

or

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.meta_ads import enrich_meta_ads
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_meta_ads(db, limit=1, niche='Food & Beverage')
db.close()
"


# Wikidata Socials (fetches instagram, youtube, twitter, facebook, tiktok handles)
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.wikidata_socials import enrich_wikidata_socials
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_wikidata_socials(db, limit=1)
db.close()
"

# Shopify detect (checks if brand website runs on Shopify)
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.shopify_detect import enrich_shopify
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_shopify(db, limit=1)
db.close()
"

or

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.shopify_detect import enrich_shopify
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_shopify(db, limit=1, niche='fashion')
db.close()
"


# Tranco (checks if brand domain is in top 1M website rankings)
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.tranco import enrich_tranco
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_tranco(db, limit=1)
db.close()
"

## run google_social_search.py
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.google_social_search import enrich_google_socials
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_google_socials(db, limit=1)
db.close()
"

## run twitter_posts.py

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.twitter_posts import enrich_twitter_posts
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_twitter_posts(db, limit=1)
db.close()
"
OR
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.twitter_posts import enrich_twitter_posts
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_twitter_posts(db, brand_id=123)
db.close()
"


## run intagram_users.py
python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.instagram_users import enrich_instagram_users
db = SessionLocal()
result = enrich_instagram_users(db, limit=1)
print('Posts processed:', result)
db.close()
"

## run brand_scoring.py

python -c "
from pipeline.db import SessionLocal
from pipeline.enrichment.initial_brand_scoring import run_brand_scoring

db = SessionLocal()
scored = run_brand_scoring(db, limit=1)
print(f'Scored {scored} brands')
db.close()
"

## run apollo_contacts.py
python3 -c "
from pipeline.db import SessionLocal
from pipeline.enrichment.apollo_contacts import run_apollo_contacts
db = SessionLocal()
processed = run_apollo_contacts(db, limit=1)
print('processed:', processed)
db.close()
"