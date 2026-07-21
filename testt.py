"""use wikidata to find website from instahandle"""
import requests

HEADERS = {
    "User-Agent": "MAVLIT-Enrichment/1.0"
}

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"


def get_website_from_instagram_username(username: str):
    query = f"""
    SELECT ?company ?companyLabel ?website WHERE {{
      ?company wdt:P2003 "{username}" .

      OPTIONAL {{
        ?company wdt:P856 ?website .
      }}

      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en".
      }}
    }}
    """

    response = requests.get(
        SPARQL_ENDPOINT,
        params={
            "query": query,
            "format": "json"
        },
        headers=HEADERS,
        timeout=60
    )

    response.raise_for_status()

    data = response.json()

    results = data["results"]["bindings"]

    if not results:
        return None

    row = results[0]

    return {
        "instagram_username": username,
        "entity": row.get("companyLabel", {}).get("value"),
        "website": row.get("website", {}).get("value"),
    }


if __name__ == "__main__":
    for username in [
        "nike",
        "gymshark",
        "skims",
        "alo",
        "aloyoga",
    ]:
        result = get_website_from_instagram_username(username)
        print(result)