"""Reset and populate test_niches with the canonical niche taxonomy.

Run with:
    .venv/bin/python scripts/populate_test_niches.py
"""

import logging
import os

from dotenv import load_dotenv
from sqlalchemy import Column, MetaData, Table, Text, create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in environment")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
metadata = MetaData()

test_niches = Table(
    "test_niches",
    metadata,
    Column("niche", Text, primary_key=True),
    Column("description", Text),
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


CANONICAL_NICHES = [
    "Agriculture",
    "Art & Design",
    "Automotive & Motorsports",
    "Beauty",
    "Books & Publishing",
    "Business & Marketing",
    "Cannabis",
    "Comedy",
    "Education",
    "Events & Weddings",
    "Fashion",
    "Film & TV",
    "Fitness",
    "Food & Beverage",
    "Gaming",
    "Health & Wellness",
    "Home & Interior",
    "Lifestyle",
    "LGBTQ+",
    "Music",
    "Outdoors & Recreation",
    "Parenting & Family",
    "Pets",
    "Photography",
    "Politics & Society",
    "Retail & Ecommerce",
    "Sports",
    "Technology",
    "Travel",
]


NICHE_DESCRIPTIONS = {
    "Agriculture": "Farming, crop production, livestock, agricultural technology, sustainable farming, gardening at scale, farm life, and the business of agriculture.",
    "Art & Design": "Visual art and creative design including painting, drawing, illustration, digital art, graphic design, crafts, visual communication, and creative design practices.",
    "Automotive & Motorsports": "Cars, trucks, motorcycles, automotive technology, vehicle reviews, modifications, maintenance, racing vehicles, motorsports, and automotive culture.",
    "Beauty": "Makeup, skincare, cosmetics, beauty routines, hair styling, beauty products, tutorials, trends, and personal beauty care.",
    "Books & Publishing": "Books, literature, authors, publishing, reading, book reviews, recommendations, children's books, comics, editing, self-publishing, and the business of books.",
    "Business & Marketing": "Entrepreneurship, companies, startups, management, leadership, business strategy, finance, investing, marketing, advertising, branding, sales, and professional growth.",
    "Cannabis": "Cannabis products, culture, cultivation, industry, legalization, education, dispensaries, cannabis lifestyle, and developments in the cannabis market.",
    "Comedy": "Comedians, jokes, sketches, humorous commentary, stand-up comedy, satire, comedic characters, and content created primarily to entertain through humor.",
    "Education": "Teaching, learning, schools, universities, educational resources, study techniques, academic subjects, teachers, students, and educational development.",
    "Events & Weddings": "Events, conferences, festivals, parties, weddings, ceremonies, event planning, venues, decorations, receptions, vendors, and event production.",
    "Fashion": "Clothing, outfits, styling, fashion trends, designers, accessories, streetwear, luxury fashion, fashion shows, and personal style.",
    "Film & TV": "Movies, cinema, television, streaming content, actors, directors, filmmakers, screenwriters, film production, TV shows, series, and screen entertainment.",
    "Fitness": "Exercise, workouts, strength training, cardio, gym routines, fitness programs, physical performance, training techniques, and fitness lifestyles.",
    "Food & Beverage": "Food and drinks, restaurants, beverages, food brands, culinary products, dining experiences, recipes, cooking, baking, and food industry content.",
    "Gaming": "Video games, gamers, gaming platforms, game reviews, gameplay, esports, gaming hardware, game development, and gaming culture.",
    "Health & Wellness": "Health, wellness, physical and mental wellbeing, healthy living, nutrition, self-care, mindfulness, lifestyle improvement, prevention, and health education.",
    "Home & Interior": "Home decoration, interior design, furniture, household products, appliances, organization, home improvement, renovation, lighting, and interior styling.",
    "Lifestyle": "Everyday life, personal interests, routines, experiences, hobbies, relationships, personal development, travel, food, home, wellness, and general personal-interest content.",
    "LGBTQ+": "LGBTQ+ communities, identities, culture, experiences, representation, relationships, events, advocacy, drag culture, and LGBTQ+ lifestyle content.",
    "Music": "Music artists, singers, musicians, bands, songs, albums, performances, music production, genres, concerts, and music culture.",
    "Outdoors & Recreation": "Outdoor recreation and adventure including hiking, camping, climbing, cycling, skiing, golf, nature exploration, outdoor activities, recreational equipment, and experiences in natural environments.",
    "Parenting & Family": "Parenting, children, family life, child development, baby care, parenting strategies, family routines, education, discipline, and parent experiences.",
    "Pets": "Pet ownership and animal companions including dogs, cats, birds, reptiles, pet care, training, products, health, and pet lifestyle content.",
    "Photography": "Photography, photographers, cameras, photo editing, composition, portraits, landscapes, commercial photography, photography techniques, and visual storytelling.",
    "Politics & Society": "Politics, government, elections, public policy, political institutions, civic issues, political parties, religion, social issues, campaigns, and political commentary.",
    "Retail & Ecommerce": "Retail businesses, stores, shopping experiences, ecommerce, online businesses, digital storefronts, products, merchandising, consumer commerce, and online selling.",
    "Sports": "Sports content including athletes, teams, competitions, training, sporting events, coaching, sports news, athletic performance, basketball, football, baseball, tennis, combat sports, and other competitive sports.",
    "Technology": "Technology, software, hardware, gadgets, artificial intelligence, computers, consumer electronics, emerging technologies, software development, and technology trends.",
    "Travel": "Travel destinations, tourism, vacations, hotels, itineraries, travel tips, international travel, local exploration, and travel experiences.",
}


def populate_test_niches() -> tuple[int, int]:
    if len(CANONICAL_NICHES) != 29:
        raise ValueError(f"Expected 29 canonical niches, found {len(CANONICAL_NICHES)}")
    if set(NICHE_DESCRIPTIONS) != set(CANONICAL_NICHES):
        raise ValueError("Canonical niches and descriptions do not match exactly")
    if any(not description.strip() for description in NICHE_DESCRIPTIONS.values()):
        raise ValueError("Every canonical niche must have a non-empty description")

    rows = [
        {"niche": niche, "description": NICHE_DESCRIPTIONS[niche]}
        for niche in CANONICAL_NICHES
    ]

    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE test_niches"))
        connection.execute(test_niches.insert(), rows)

        total_count = connection.execute(
            text("SELECT COUNT(*) FROM test_niches")
        ).scalar_one()
        described_count = connection.execute(
            text("""
                SELECT COUNT(*)
                FROM test_niches
                WHERE description IS NOT NULL
                  AND TRIM(description) <> ''
            """)
        ).scalar_one()

    if total_count != 29:
        raise RuntimeError(f"Expected 29 rows in test_niches, found {total_count}")
    if described_count != 29:
        raise RuntimeError(
            f"Expected 29 rows with non-empty descriptions, found {described_count}"
        )

    logger.info("Inserted %d niche row(s) into test_niches", len(rows))
    logger.info("Total niches: %d", total_count)
    logger.info("Descriptions populated: %d", described_count)
    return total_count, described_count


if __name__ == "__main__":
    metadata.create_all(bind=engine)
    total_count, described_count = populate_test_niches()
    print(f"test_niches: {total_count} niche row(s).")
    print(f"test_niches: {described_count} row(s) have descriptions.")
