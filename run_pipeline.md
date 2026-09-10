# wikidata_socials → shopify → tranco → meta_ads → youtube


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

or

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.instagram_posts import enrich_instagram_posts
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_instagram_posts(db, limit=1, niche='fashion')
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

or (target one specific brand by brand_raw id)

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.meta_ads import enrich_meta_ads
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_meta_ads(db, brand_id=5318)
db.close()
"


## Wikidata Socials (fetches instagram, youtube, facebook handles)
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.wikidata_socials import enrich_wikidata_socials
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_wikidata_socials(db, limit=1000)
db.close()
"

or (target one specific brand by brand_raw id)

python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.wikidata_socials import enrich_wikidata_socials
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_wikidata_socials(db, brand_id=5318)
db.close()
"

## Shopify detect (checks if brand website runs on Shopify)
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.shopify_detect import enrich_shopify
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_shopify(db, limit=1000)
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


## Tranco (checks if brand domain is in top 1M website rankings)
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.tranco import enrich_tranco
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
db = SessionLocal()
enrich_tranco(db, limit=1000)
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

OR (target one specific instagram_posts row by its primary key id)

python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.instagram_users import enrich_instagram_users
db = SessionLocal()
result = enrich_instagram_users(db, limit=1, row_id=2651)
print('Posts processed:', result)
db.close()
"

OR (target all instagram_posts rows for one brand)

python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.instagram_users import enrich_instagram_users
db = SessionLocal()
result = enrich_instagram_users(db, limit=5, brand_raw_id=5318)
print('Posts processed:', result)
db.close()
"

## run brand_scoring.py
python -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.initial_brand_scoring import run_brand_scoring
db = SessionLocal()
scored = run_brand_scoring(db, limit=1)
print(f'Scored {scored} brands')
db.close()
"

OR

python -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.initial_brand_scoring import run_brand_scoring
db = SessionLocal()
scored = run_brand_scoring(db, brand_id=5318)
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


# Reverse Engineering 

## content_creator_re
python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment_re.content_creator_re import enrich_content_creator_re
db = SessionLocal()
enrich_content_creator_re(db, limit=1)
db.close()
"

## brand_wikidata_lookup (reverse-lookup bare brands by instagram_handle)
python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment_re.brand_wikidata_lookup import enrich_brand_wikidata_lookup
db = SessionLocal()
enrich_brand_wikidata_lookup(db, limit=50)
db.close()
"
    
## brand_instagram_profile (Instagram bio/linktree/Google-search website resolution — backfills bare brands, verifies/corrects the website on already-named ones)
python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment_re.brand_instagram_profile import enrich_brand_instagram_profile
db = SessionLocal()
enrich_brand_instagram_profile(db, brand_id=73)
db.close()
"

OR (batch, no brand_id — processes up to `limit` pending rows)

python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment_re.brand_instagram_profile import enrich_brand_instagram_profile
db = SessionLocal()
enrich_brand_instagram_profile(db, limit=1)
db.close()
"

## to run LLM per post re
python -m pipeline.enrichment_re.score_post_sponsorship


## to run LLM per post 
python -m pipeline.enrichment.score_instagram_post_sponsorship

## to run brand tier
python -m pipeline.enrichment.brand_tier
python -m pipeline.enrichment.brand_tier --brand-id 1614

## to run geo_reach.py (geographic market reach scoring, scrapes brands_raw.website)
python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.geo_reach.geo_reach import enrich_geo_reach
db = SessionLocal()
enrich_geo_reach(db, limit=1)
db.close()
"

OR (target one specific brand by brand_raw id)

python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.geo_reach.geo_reach import enrich_geo_reach
db = SessionLocal()
enrich_geo_reach(db, brand_id=5318)
db.close()
"

or

python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.geo_reach.geo_reach import enrich_geo_reach
db = SessionLocal()
enrich_geo_reach(db, limit=1, niche='fashion')
db.close()
"

or (filter by the NICHE OF THE CONTENT CREATOR who discovered the brand — i.e. content_creator_re.niche via
test_creator_brand_partnership_posts — rather than the brand's own brands_raw.niche; useful for
reverse-engineering-sourced brands that don't carry their own niche)

python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
from dotenv import load_dotenv; load_dotenv()
from pipeline.db import SessionLocal
from pipeline.enrichment.geo_reach.geo_reach import enrich_geo_reach
db = SessionLocal()
enrich_geo_reach(db, limit=1, creator_niche='Fitness')
db.close()
"

or (CLI form)

python3 -m pipeline.enrichment.geo_reach.geo_reach --limit 1
python3 -m pipeline.enrichment.geo_reach.geo_reach --brand-id 5318
python3 -m pipeline.enrichment.geo_reach.geo_reach --limit 1 --creator-niche Fitness


## TASKS
### after all have to run LLM(one time) for niche of creators for specific selected niche
### have to make another pipeline so that it scrape creator re and brands after month or two month for recency posts